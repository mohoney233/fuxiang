# -*- coding: utf-8 -*-
"""预览渲染 —— 复用引擎自己的帧循环出图，不介入渲染管线。

设计依据见 PREVIEW_RENDER.md（2026-09-16 实机取证）。核心约束：

  ⛔ 不创建相机、不销毁相机、不创建/释放 RenderTarget、不回读像素。
     这四件事在本客户端分别会触发「非 render frame」报错、
     「Texture Readback not implemented」（MobileEngine）以及**进程崩溃**。
  ✅ 只用现有的编辑器相机 room.cam 摆位，然后调
     scene.capture_scene_snapshot_to_file() 让引擎把当前帧写成文件。

因此本模块的状态机是：备份相机 → 摆位 → （跨帧等待，由主机侧做）→ 出图 → 还原相机。
模块本身**不做任何等待、不 sleep、不轮询**，符合「主线程只做短操作」的项目纪律。

调用约定与 fuxiang_edit_ops / fuxiang_scene 一致：函数签名 (edit, args)。
"""
import math
import os
import re

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
STATE_ATTR = "_fuxiang_preview"

#: 该坐标绝对值以上视为哨兵值（未落位物件会挂 [0, -65536, 0]），不参与取景。
SENTINEL_ABS = 100000.0

#: Y 轴哨兵阈值。逻辑类组件（运动途经点 203912、胜利目标 10002001、
#: 坐标中心偏移 203930、替身人格播放模式 103002 等）未配置时挂在 y=-65536，
#: 会把包围盒撑到不可用，必须单独判据剔除。
Y_SENTINEL_ABS = 1000.0

#: 「地图边界锚点」组件类型。这 4 个物件定义可建造区域四边形，
#: 不是哨兵值，反而是取景时判断"整张地图多大"的依据。
BOUNDARY_COMPONENT_TYPES = {201009}

#: 界面类组件（UGCUiNativeComponent），不属于场景世界，不参与取景与平面图。
UI_COMPONENT_TYPES = {200001}

#: 目录分组（沿用 fuxiang_catalog 的 item_type 编码）：
#:   1=scene 场景物件 / 2=global 全局功能组件 / 3=ui 界面组件 / 10=restrict 限制区域
#: global 与 ui 挂在原点 (0,0,0) 或纯逻辑，会把包围盒撑到整个地图，必须剔除。
ITEM_TYPE_GROUPS = {1: "scene", 2: "global", 3: "ui", 10: "restrict"}
NON_SPATIAL_GROUPS = {"global", "ui"}

#: 出图默认格式（render.IFF_PNG）。
IFF_PNG = 3

# （2026-09-22 删除 DEFAULT_PREVIEW_DIR：未被引用的死常量，且硬编码工作区路径；
#  实际出图路径一律由调用方通过 op 参数 path 显式传入。）

_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


# --------------------------------------------------------------------------
# 访问器
# --------------------------------------------------------------------------
def _room(edit):
    return edit.get_room()


def _scene(room):
    return getattr(room, "scene", None)


def _cam(room):
    return getattr(room, "cam", None)


def _cm(edit):
    room = _room(edit)
    return getattr(room, "ugc_component_manager", None) if room is not None else None


def _dm(edit):
    room = _room(edit)
    if room is None:
        return None
    try:
        return room.create_unit_with_info.__globals__["GK"].DM
    except Exception:
        return None


def _state(room):
    return vars(room).setdefault(STATE_ATTR, {"backup": None, "posed": 0, "captured": 0,
                                              "history": []})


# --------------------------------------------------------------------------
# 矩阵 / 向量工具
# --------------------------------------------------------------------------
def _floats(mat):
    """把 math3d.matrix / vector 的 repr 解析成 float 列表（引擎没有索引接口）。"""
    if mat is None:
        return None
    try:
        text = str(mat)
    except Exception:
        return None
    vals = [float(x) for x in _NUM_RE.findall(text)]
    return vals or None


def _vec3(value, name):
    """接受 [x,y,z] / {x,y,z} / 逗号字符串，返回 3 元素 float 列表。"""
    if value is None:
        raise ValueError("缺少参数 %s" % name)
    if isinstance(value, (list, tuple)):
        out = [float(v) for v in value]
    elif isinstance(value, dict):
        keys = sorted(value.keys(), key=lambda k: str(k))
        if all(k in value for k in ("x", "y", "z")):
            out = [float(value["x"]), float(value["y"]), float(value["z"])]
        elif len(keys) == 3:
            out = [float(value[k]) for k in keys]
        else:
            raise ValueError("%s 需要 3 个分量" % name)
    elif hasattr(value, "x"):
        out = [float(value.x), float(value.y), float(value.z)]
    elif isinstance(value, str):
        out = [float(x) for x in _NUM_RE.findall(value)][:3]
    else:
        raise ValueError("%s 类型不支持: %r" % (name, type(value).__name__))
    if len(out) != 3:
        raise ValueError("%s 需要 3 个分量，收到 %d" % (name, len(out)))
    return out


def _math3d():
    import importlib
    return importlib.import_module("math3d")


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _norm(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n <= 1e-9:
        raise ValueError("零长度方向向量")
    return [v[0] / n, v[1] / n, v[2] / n]


def _scale(v, f):
    return [v[0] * f, v[1] * f, v[2] * f]


def _round3(v):
    return [round(float(x), 4) for x in v]


# --------------------------------------------------------------------------
# 相机姿态：备份 / 还原
# --------------------------------------------------------------------------
def camera_pose(cam):
    """读取相机完整姿态。矩阵行序为 right / up / forward / position（已实测确认）。"""
    out = {"ok": False}
    m = _floats(getattr(cam, "transformation", None))
    if m and len(m) >= 16:
        out["right"] = _round3(m[0:3])
        out["up"] = _round3(m[4:7])
        out["forward"] = _round3(m[8:11])
        out["position"] = _round3(m[12:15])
        out["ok"] = True

    proj = _floats(getattr(cam, "projection_matrix", None))
    if proj and len(proj) >= 16:
        # 引擎用**行向量约定**（实测 editor 相机 v1.8.1）：
        #   透视: m[2][3] == 1 且 m[3][3] == 0   （proj[11]=1, proj[15]=0）
        #   正交: m[2][3] == 0 且 m[3][3] == 1
        perspective = abs(proj[11] - 1.0) < 0.05
        out["perspective"] = perspective
        out["projection"] = [round(x, 6) for x in proj]
        if not perspective and proj[0] and proj[5]:
            out["ortho_width"] = round(2.0 / proj[0], 4)
            out["ortho_height"] = round(2.0 / proj[5], 4)

    for attr, key in (("fov", "fov"), ("aspect", "aspect")):
        try:
            v = getattr(cam, attr, None)
            if v is not None:
                out[key] = round(float(v), 4)
        except Exception:
            pass
    try:
        zr = getattr(cam, "z_range", None)
        if zr is not None:
            vals = _floats(zr) or []
            if len(vals) >= 2:
                out["min_z"], out["max_z"] = round(vals[0], 4), round(vals[1], 4)
    except Exception:
        pass
    return out


def _apply_projection(cam, mode, params):
    """按模式设置投影。params 缺省时给保守默认值。"""
    p = params or {}
    min_z = float(p.get("min_z", 0.5))
    max_z = float(p.get("max_z", 3000.0))
    if mode == "ortho":
        cam.set_ortho(float(p.get("width", 200.0)), float(p.get("height", 200.0)),
                      min_z, max_z)
    else:
        cam.set_perspect(float(p.get("angle", p.get("fov", 55.0))),
                         float(p.get("aspect", 1.6)), min_z, max_z)


def _apply_pose(cam, view):
    """摆位：先投影，再位置与朝向。"""
    m3 = _math3d()
    pos = _vec3(view.get("position"), "position")
    fwd = _vec3(view.get("forward"), "forward")
    up = _vec3(view.get("up") or [0.0, 1.0, 0.0], "up")
    mode = view.get("mode") or ("ortho" if view.get("ortho") else "perspect")
    params = view.get("ortho") if mode == "ortho" else view.get("perspect")
    _apply_projection(cam, mode, params)
    cam.set_placement(m3.vector(*pos), m3.vector(*fwd), m3.vector(*up))


def _restore_pose(cam, backup):
    """把相机还原到备份姿态。"""
    m3 = _math3d()
    if backup.get("perspective") is False:
        _apply_projection(cam, "ortho", {"width": backup.get("ortho_width") or 200.0,
                                         "height": backup.get("ortho_height") or 200.0,
                                         "min_z": backup.get("min_z", 0.5),
                                         "max_z": backup.get("max_z", 3000.0)})
    else:
        _apply_projection(cam, "perspect", {"angle": backup.get("fov") or 55.0,
                                            "aspect": backup.get("aspect") or 1.6,
                                            "min_z": backup.get("min_z", 0.5),
                                            "max_z": backup.get("max_z", 3000.0)})
    cam.set_placement(m3.vector(*backup["position"]),
                      m3.vector(*backup["forward"]),
                      m3.vector(*backup["up"]))


# --------------------------------------------------------------------------
# 取景数据：从已登记组件算包围盒
# --------------------------------------------------------------------------
def _item_group(dm, item_id):
    """查物件目录得到分组名（scene / global / ui / restrict / unknown）。"""
    if dm is None or item_id is None:
        return None
    try:
        items = dm.ugc_item_data
    except Exception:
        return None
    rec = None
    for key in (item_id, str(item_id)):
        try:
            rec = items.get(key)
        except Exception:
            rec = None
        if rec is not None:
            break
    if rec is None:
        return None
    try:
        itype = rec.get("item_type")
    except Exception:
        return None
    try:
        itype = int(itype)
    except Exception:
        return None
    return ITEM_TYPE_GROUPS.get(itype, "unknown")


def _collect(edit, args):
    """枚举已登记组件并按空间性分类。

    返回 dict：
      objects   真实空间物件（取景与平面图的主体）
      boundary  「地图边界锚点」，用于按整张地图取景
      non_spatial  非空间组件（全局规则 / 界面控件 / 未落位逻辑），只登记不入图
      excluded  读不到位置的
    """
    cm = _cm(edit)
    if cm is None:
        raise RuntimeError("组件管理器不可用，确认已在随笔浮想编辑器内")
    dm = _dm(edit)
    ui_types = set(args.get("exclude_component_types") or []) | UI_COMPONENT_TYPES
    boundary_types = set(args.get("boundary_component_types") or []) | \
        BOUNDARY_COMPONENT_TYPES
    sentinel = float(args.get("sentinel_abs") or SENTINEL_ABS)
    y_sentinel = float(args.get("y_sentinel_abs") or Y_SENTINEL_ABS)
    keep_groups = set(args.get("include_groups") or [])

    out = {"objects": [], "boundary": [], "non_spatial": [], "excluded": []}
    for ugc_id, comp in sorted(cm._ugc_id2component.items(), key=lambda x: str(x[0])):
        unit = getattr(comp, "_owner", None)
        if unit is None:
            continue
        ct = getattr(comp, "COMPONENT_TYPE", None)
        item_id = getattr(unit, "ugc_type", None)

        pos = None
        for getter in ("get_position3", "position"):
            f = getattr(unit, getter, None)
            if f is None:
                continue
            try:
                v = f() if callable(f) else f
                pos = [float(v.x), float(v.y), float(v.z)]
                break
            except Exception:
                continue

        row = {"ugc_id": getattr(comp, "_ugc_id", None),
               "owner_uid": getattr(unit, "uid", None),
               "component_type": ct,
               "component_class": type(comp).__name__,
               "item_id": item_id,
               "item_group": _item_group(dm, item_id),
               "position": _round3(pos) if pos else None}

        if pos is None:
            row["reason"] = "读不到位置"
            out["excluded"].append(row)
            continue
        if ct in ui_types:
            row["reason"] = "界面类组件"
            out["non_spatial"].append(row)
            continue
        if abs(pos[1]) >= y_sentinel or max(abs(v) for v in pos) >= sentinel:
            row["reason"] = "哨兵坐标（未落位）"
            out["non_spatial"].append(row)
            continue
        if row["item_group"] in NON_SPATIAL_GROUPS and \
                row["item_group"] not in keep_groups:
            row["reason"] = "目录分组为 %s（非空间）" % row["item_group"]
            out["non_spatial"].append(row)
            continue
        if ct in boundary_types:
            row["role"] = "boundary"
            out["boundary"].append(row)
            continue
        out["objects"].append(row)
    return out


def _select(parts, fit):
    """按 fit 选择参与取景的对象集合。

    content = 只按实际摆放的空间物件取景（细节最好）
    map     = 只按地图边界锚点取景（整张地图）
    union   = 并集（默认，真正的"全局"）
    """
    objects = parts["objects"]
    boundary = parts["boundary"]
    fit = (fit or "union").lower()
    if fit == "content":
        return list(objects), "content"
    if fit == "map":
        return list(boundary), "map"
    if fit == "union":
        return list(objects) + list(boundary), "union"
    raise ValueError("未知 fit=%r（可用 content / map / union）" % fit)


def _bounds(objects):
    """由对象位置算 XZ 平面范围与高度范围。"""
    xs = [o["position"][0] for o in objects]
    ys = [o["position"][1] for o in objects]
    zs = [o["position"][2] for o in objects]
    lo = [min(xs), min(ys), min(zs)]
    hi = [max(xs), max(ys), max(zs)]
    center = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
    span = [hi[i] - lo[i] for i in range(3)]
    return {"min": _round3(lo), "max": _round3(hi), "center": _round3(center),
            "span": _round3(span),
            "radius": round(max(span[0], span[2]) / 2.0, 4),
            "count": len(objects)}


def build_views(bounds, args):
    """按包围盒生成视角列表。默认：正俯视（正交）+ 四角斜视（透视）。

    args:
      views      需要的视角名（默认 top / iso_ne / iso_nw / iso_se / iso_sw）
      margin     取景外扩系数，默认 1.18
      dist_mul   斜视距离倍率，默认 1.0（<1 拉近，看细节；>1 拉远，看得更全）
      span_mul   正俯视正交跨度倍率，默认 1.0
      fov        斜视竖直视角，默认 55
      aspect     视口宽高比，默认 1.6（1920x1200）
      elevation  斜视仰角（度），默认 35
    """
    margin = float(args.get("margin") or 1.18)
    fov = float(args.get("fov") or 55.0)
    aspect = float(args.get("aspect") or 1.6)
    dist_mul = float(args.get("dist_mul") or 1.0)
    span_mul = float(args.get("span_mul") or 1.0)
    elevation = math.radians(float(args.get("elevation") or 35.0))
    wanted = args.get("views")

    cn = bounds["center"]
    span_x = max(bounds["span"][0], 1.0)
    span_z = max(bounds["span"][2], 1.0)
    radius = max(span_x, span_z) / 2.0
    diag = math.sqrt(span_x ** 2 + span_z ** 2) / 2.0

    out = []

    def add_top():
        width = max(span_x * margin, span_z * margin * aspect) * span_mul
        height = width / aspect
        top_y = bounds["max"][1] + max(span_x, span_z, 50.0) + 200.0
        out.append({
            "name": "top",
            "mode": "ortho",
            "position": _round3([cn[0], top_y, cn[2]]),
            "forward": [0.0, -1.0, 0.0],
            "up": [0.0, 0.0, 1.0],
            "ortho": {"width": round(width, 3), "height": round(height, 3),
                      "min_z": 1.0, "max_z": round(top_y - bounds["min"][1] + 400.0, 3)},
            "covers": "整图正俯视（正交）",
        })

    def add_iso(name, sx, sz):
        dist = diag * margin / max(math.tan(math.radians(fov) / 2.0), 1e-3) * dist_mul
        horiz = math.cos(elevation)
        d = _norm([sx * horiz, math.sin(elevation), sz * horiz])
        pos = [cn[0] + d[0] * dist, cn[1] + d[1] * dist, cn[2] + d[2] * dist]
        out.append({
            "name": name,
            "mode": "perspect",
            "position": _round3(pos),
            "forward": _round3(_sub(cn, pos)),
            "up": [0.0, 1.0, 0.0],
            "perspect": {"angle": fov, "aspect": aspect, "min_z": 0.5,
                         "max_z": round(dist * 4.0 + 500.0, 3)},
            "covers": "斜视（透视）",
        })

    if not wanted:
        wanted = ["top", "iso_ne", "iso_nw", "iso_se", "iso_sw"]
    wanted = list(wanted)
    for name in wanted:
        if name == "top":
            add_top()
        elif name == "iso_ne":
            add_iso("iso_ne", 1.0, -1.0)
        elif name == "iso_nw":
            add_iso("iso_nw", -1.0, -1.0)
        elif name == "iso_se":
            add_iso("iso_se", 1.0, 1.0)
        elif name == "iso_sw":
            add_iso("iso_sw", -1.0, 1.0)
        elif name == "current":
            out.append({"name": "current", "mode": "keep",
                        "covers": "不摆位，直接拍当前视角"})
        else:
            raise ValueError("未知视角 %r（可用：top / iso_ne / iso_nw / iso_se / "
                             "iso_sw / current）" % name)
    for v in out:
        v.setdefault("radius", round(radius, 4))
    return out


# --------------------------------------------------------------------------
# op: 取景规划
# --------------------------------------------------------------------------
def op_preview_plan(edit, args):
    """只算不拍：返回包围盒、视角列表与排除清单。

    args:
      fit      content | map | union（默认 union）
      views    需要的视角名列表，默认全部
      margin   取景外扩系数，默认 1.18
      fov      斜视图竖直视角，默认 55
      elevation 斜视仰角（度），默认 35
    """
    parts = _collect(edit, args)
    chosen, fit = _select(parts, args.get("fit"))
    if not chosen:
        return {"ok": False,
                "error": "fit=%s 下没有可参与取景的物件" % fit,
                "content_count": len(parts["objects"]),
                "boundary_count": len(parts["boundary"]),
                "non_spatial_count": len(parts["non_spatial"])}
    bounds = _bounds(chosen)
    views = build_views(bounds, args)
    cam = _cam(_room(edit))
    return {
        "ok": True,
        "fit": fit,
        "content_count": len(parts["objects"]),
        "boundary_count": len(parts["boundary"]),
        "non_spatial_count": len(parts["non_spatial"]),
        "excluded_count": len(parts["excluded"]),
        "bounds": bounds,
        "bounds_content": _bounds(parts["objects"]) if parts["objects"] else None,
        "bounds_map": _bounds(parts["boundary"]) if parts["boundary"] else None,
        "views": views,
        "camera": camera_pose(cam) if cam is not None else None,
        "note": "views 可直接回传给 fuxiang_preview_shot 的 view 参数。"
                "fit=content 出细节、fit=map 出整张地图、fit=union 出全局。",
    }


# --------------------------------------------------------------------------
# op: 摆位 / 出图 / 还原
# --------------------------------------------------------------------------
def op_preview_pose(edit, args):
    """把编辑器相机摆到指定视角；首次会备份原姿态。"""
    room = _room(edit)
    cam = _cam(room)
    if cam is None:
        return {"ok": False, "error": "拿不到 room.cam（不在编辑器内？）"}

    view = args.get("view")
    if isinstance(view, str):
        chosen, fit = _select(_collect(edit, args), args.get("fit"))
        if not chosen:
            return {"ok": False, "error": "fit=%s 下没有可参与取景的物件" % fit}
        found = [v for v in build_views(_bounds(chosen), args) if v["name"] == view]
        if not found:
            return {"ok": False, "error": "未知视角 %r" % view}
        view = found[0]
    if not isinstance(view, dict):
        return {"ok": False, "error": "view 需要是视角名或视角对象"}

    st = _state(room)
    if st.get("backup") is None or args.get("force_backup"):
        st["backup"] = camera_pose(cam)
        st["backup"]["_taken_at"] = args.get("_ts")
    st["current_view"] = view.get("name")
    st["posed"] = int(st.get("posed") or 0) + 1

    if view.get("mode") != "keep":
        _apply_pose(cam, view)

    st["last_pose"] = camera_pose(cam)
    st["history"] = (st.get("history") or [])[-19:] + [
        {"op": "pose", "view": view.get("name"), "mode": view.get("mode")}]
    return {"ok": True, "view": view.get("name"), "mode": view.get("mode"),
            "backup_taken": st["backup"] is not None,
            "applied": st["last_pose"],
            "note": "相机已摆位。请等待 2 帧（主机侧间隔约 0.4s）后再调 fuxiang_preview_fire。"}


def op_preview_fire(edit, args):
    """调引擎把当前帧写成文件。异步落盘，返回后需轮询文件。"""
    room = _room(edit)
    scene = _scene(room)
    if scene is None:
        return {"ok": False, "error": "拿不到 room.scene"}

    path = args.get("path")
    if not path:
        return {"ok": False, "error": "缺少 path（需绝对路径）"}
    path = os.path.abspath(path)
    if not os.path.isabs(path):
        return {"ok": False, "error": "path 必须是绝对路径"}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": "创建目录失败: %s" % exc}

    existed = os.path.exists(path)
    try:
        if existed:
            os.remove(path)
    except Exception:
        return {"ok": False, "error": "目标文件已存在且无法删除: %s" % path}

    fmt = int(args.get("format") or IFF_PNG)
    try:
        accepted = scene.capture_scene_snapshot_to_file(path, fmt)
    except Exception as exc:
        return {"ok": False, "error": "capture_scene_snapshot_to_file 抛错: %s" % exc}

    st = _state(room)
    st["captured"] = int(st.get("captured") or 0) + 1
    st["last_path"] = path
    st["history"] = (st.get("history") or [])[-19:] + [
        {"op": "fire", "path": path, "accepted": bool(accepted)}]
    return {"ok": True, "accepted": bool(accepted), "path": path,
            "format": fmt,
            "note": "引擎已受理；文件在 1~2 秒后出现，请轮询 path 而非相信返回值。"}


def op_preview_restore(edit, args):
    """把相机还原到备份姿态。"""
    room = _room(edit)
    cam = _cam(room)
    if cam is None:
        return {"ok": False, "error": "拿不到 room.cam"}
    st = _state(room)
    backup = st.get("backup")
    if not backup:
        return {"ok": False, "error": "没有备份（本 MCP 会话未摆过位）"}

    _restore_pose(cam, backup)
    now = camera_pose(cam)
    ok = True
    detail = {}
    for key in ("position", "forward", "up"):
        try:
            d = max(abs(a - b) for a, b in zip(now[key], backup[key]))
        except Exception:
            d = None
        detail[key] = d
        if d is None or d > 0.05:
            ok = False
    cleared = bool(args.get("clear", True))
    if cleared:
        st["backup"] = None
    st["history"] = (st.get("history") or [])[-19:] + [
        {"op": "restore", "verified": ok}]
    return {"ok": True, "verified": ok, "delta": detail,
            "restored_to": _round3(backup["position"]),
            "current": now,
            "backup_cleared": cleared}


def op_preview_state(edit, _args):
    """诊断：相机姿态、备份状态、场景就绪度。"""
    room = _room(edit)
    cam = _cam(room)
    scene = _scene(room)
    st = _state(room)
    out = {"ok": True, "has_cam": cam is not None, "has_scene": scene is not None}
    if cam is not None:
        out["camera"] = camera_pose(cam)
    if scene is not None:
        try:
            out["readable"] = bool(scene.renderable)
        except Exception:
            pass
        try:
            out["active_camera"] = scene.active_camera
        except Exception:
            pass
        try:
            out["has_capture_api"] = hasattr(scene, "capture_scene_snapshot_to_file")
        except Exception:
            pass
    out["backup_present"] = st.get("backup") is not None
    out["posed"] = st.get("posed")
    out["captured"] = st.get("captured")
    out["last_path"] = st.get("last_path")
    return out


# --------------------------------------------------------------------------
# op: 平面图数据（离线绘图的输入）
# --------------------------------------------------------------------------
def op_preview_layout(edit, args):
    """导出俯视平面图所需的数据。绘制在主机侧完成，游戏主线程只取数。"""
    parts = _collect(edit, args)
    objects, boundary = parts["objects"], parts["boundary"]
    dm = _dm(edit)
    items = None
    tids = None
    if dm is not None:
        try:
            items = dm.ugc_item_data
        except Exception:
            items = None
        try:
            tids = dm.all_tid_data
        except Exception:
            tids = None

    def name_of(item_id):
        if items is None:
            return None
        rec = None
        for key in (item_id, str(item_id)):
            try:
                rec = items.get(key)
            except Exception:
                rec = None
            if rec is not None:
                break
        if rec is None:
            return None
        try:
            tid = rec.get("item_tid")
        except Exception:
            return None
        if not tid or tids is None:
            return None
        try:
            v = tids.get(tid)
        except Exception:
            return None
        return v if isinstance(v, str) and v else None

    cm = _cm(edit)

    def enrich(obj):
        comp = None
        try:
            comp = cm._ugc_id2component.get(int(obj["ugc_id"]))
        except Exception:
            comp = None
        unit = getattr(comp, "_owner", None) if comp is not None else None
        rot = scale = None
        for getter, target in (("ugc_dir", "rot"), ("ugc_size", "scale")):
            f = getattr(unit, getter, None) if unit is not None else None
            if f is None:
                continue
            try:
                v = f() if callable(f) else f
                vals = [float(v.x), float(v.y), float(v.z)]
            except Exception:
                continue
            if target == "rot":
                rot = _round3(vals)
            else:
                scale = _round3(vals)
        row = dict(obj)
        row["name_cn"] = name_of(obj["item_id"])
        row["rotation"] = rot
        row["scale"] = scale
        return row

    rows = [enrich(o) for o in objects]
    bnd_rows = [enrich(o) for o in boundary]
    non_spatial = [enrich(o) for o in parts["non_spatial"]]

    all_pts = rows + bnd_rows
    return {"ok": True,
            "count": len(rows), "boundary_count": len(bnd_rows),
            "non_spatial_count": len(non_spatial),
            "excluded_count": len(parts["excluded"]),
            "bounds": _bounds(all_pts) if all_pts else None,
            "bounds_content": _bounds(rows) if rows else None,
            "bounds_map": _bounds(bnd_rows) if bnd_rows else None,
            "objects": rows, "boundary": bnd_rows, "non_spatial": non_spatial,
            "note": "坐标口径：Y 轴向上，地面为 X-Z 平面；俯视图取 (x, z)。"
                    "引擎未提供可靠模型包围盒（bounding_info 为 None），"
                    "故平面图按标记绘制，尺寸取自 scale。"}
