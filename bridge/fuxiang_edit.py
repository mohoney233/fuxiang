# -*- coding: utf-8 -*-
"""
随笔浮想 (UGC) 编辑器 API 层 — 运行在 dwrg.exe 进程内。

由 MCP 服务器通过 fuxiang_bridge 的 eval op 调用：
    import importlib, json
    m = importlib.reload(importlib.import_module('fuxiang_edit'))
    __result__ = m.dispatch(<json str>)

设计原则：
  - 本文件可随时热更新：每次 dispatch 都通过 reload 重新加载。
  - 所有方法在主线程执行（由桥保证），不得阻塞。
  - 只做本地编辑；不触碰网络发布接口。
  - 任何异常都转成 {"ok": false, "error": ...}，绝不逃逸。
"""
import sys, os, json, gc, importlib, traceback, inspect, time, types, copy

# 确保本目录在 sys.path，便于 reload
_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else None
if _HERE and _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 热路径一律走 smart_reload（按 mtime+size 缓存）。2026-09-19 实测：无条件 reload
# 每次要 fuxiang_edit 23.7ms + fuxiang_scene 10.2ms + fuxiang_placement 4.8ms ≈ 39ms，
# 而真正的 dispatch 只要 0.015ms。详见 bridge/fuxiang_reload.py。
from fuxiang_reload import smart_reload  # noqa: E402


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _safe(v, depth=0):
    """把任意值转成 JSON 友好的结构。"""
    if depth > 4:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if all(hasattr(v, k) for k in ('x', 'y', 'z')):
        try:
            return [float(v.x), float(v.y), float(v.z)]
        except (TypeError, ValueError):
            pass
    if isinstance(v, (list, tuple)):
        return [_safe(x, depth + 1) for x in v[:200]]
    if isinstance(v, dict):
        out = {}
        for k, x in list(v.items())[:200]:
            try:
                out[str(k)] = _safe(x, depth + 1)
            except Exception:
                out[str(k)] = "<err>"
        return out
    # 引擎注册表返回的是 C 扩展类型 taggeddict：MRO 只有 (taggeddict, object)，
    # 既不继承 dict 也不注册 Mapping，因此上面两条 isinstance 分支都接不住它，
    # 只能按 mapping 协议（keys / __getitem__）拆。
    if hasattr(v, "keys"):
        try:
            keys = list(v.keys())[:200]
        except Exception:
            return repr(v)[:200]
        out = {}
        for k in keys:
            try:
                out[str(k)] = _safe(v[k], depth + 1)
            except Exception:
                out[str(k)] = "<err>"
        return out
    return repr(v)[:200]


def _jdump(obj, **kw):
    """eval 侧的推荐序列化：先经 _safe 兜住非 JSON 类型，再 dumps。

    直接 json.dumps 引擎数据会在 taggeddict 上抛
    「Object of type taggeddict is not JSON serializable」；用 default=str 虽然
    不报错，但会把嵌套结构压成 repr 字符串，读到的值是坏的。
    """
    kw.setdefault("ensure_ascii", False)
    kw.setdefault("default", _safe)
    return json.dumps(obj, **kw)


def _find_instance(type_name):
    """在 gc 对象里找指定类型的实例（管理器多为单例）。"""
    for obj in gc.get_objects():
        try:
            if type(obj).__name__ == type_name:
                return obj
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# 访问器
# --------------------------------------------------------------------------
def get_core():
    mod = sys.modules.get("common_cs.ugc_edit_core.core.UGCEditCore")
    if mod is None:
        try:
            mod = importlib.import_module("common_cs.ugc_edit_core.core.UGCEditCore")
        except Exception:
            return None
    Core = getattr(mod, "UGCEditCore", None)
    if Core is None:
        return None
    try:
        return Core.get_instance()
    except Exception:
        return None


def get_component_manager():
    room = get_room()
    return getattr(room, "ugc_component_manager", None)


def get_event_manager():
    room = get_room()
    return getattr(room, "ugc_event_manager", None)


def get_type_names(cm):
    """组件类型 -> 名称映射。"""
    t2str = getattr(cm, "_component_type2str", None) or {}
    return {str(k): v for k, v in t2str.items()}


# --------------------------------------------------------------------------
# 组件工厂补齐
# --------------------------------------------------------------------------
# 游戏的 _create_component 是「动态 import 组件类」的：
#   module = common_cs.combat_core.components.unit_ugc.UGC<Name>Component
#   _component_factory 是按需加载缓存，启动时可能只有 14 种。
# 下面保留旧探测辅助函数；正常操作由游戏自行加载，不批量注册。
_FACTORY_PKG = "common_cs.combat_core.components.unit_ugc."


def _component_class_name(type_str):
    """OBSTACLE -> UGCObstacleComponent；MOTION_POINT -> UGCMotionPointComponent"""
    return "UGC" + "".join(p[:1] + p[1:].lower()
                           for p in str(type_str).split("_")) + "Component"


def ensure_factory(cm):
    """把 _component_type2str 里登记的所有类型补进 _component_factory。

    返回 (已登记数量, 失败列表)。幂等。
    """
    t2str = getattr(cm, "_component_type2str", None) or {}
    factory = getattr(cm, "_component_factory", None)
    if not isinstance(t2str, dict) or not isinstance(factory, dict):
        return 0, ["索引结构异常"]
    failed = []
    for t, s in t2str.items():
        ti = int(t) if str(t).isdigit() else t
        if ti in factory:
            continue
        cn = _component_class_name(s)
        try:
            m = importlib.import_module(_FACTORY_PKG + cn)
            factory[ti] = getattr(m, cn)
        except Exception as e:
            failed.append({"type": _safe(ti), "name": str(s),
                           "class": cn, "err": type(e).__name__})
    return len(factory), failed


# --------------------------------------------------------------------------
# 物件目录（ugc_item_data）与 room
# --------------------------------------------------------------------------
def get_room():
    core = get_core()
    if core is not None:
        r = getattr(core, "room", None)
        if r is not None:
            return r
    return None


def component_item_id(room, gid, fallback=None):
    """组件真正的 item_id —— **不能**用 `unit.ugc_type` 代替。

    一个单元可以挂多个组件，而 `unit.ugc_type` 只是**第一件**的 item_id
    （界面编辑器实测：全图所有 UI_CUSTOM 控件共用一个 `UgcGlobalUnit`，
    拿 ugc_type 当身份会把 30 个不同控件全读成同一个；见 UI_EDIT.md §0）。
    共用单元上第 2..N 件必须问 `room.get_component_item_id(ugc_id)`。

    ⚠️ 返回值要校验成正整数再用：本客户端的 `getattr` 对**不存在的方法**返回
    `Swallow` 哨兵而不是抛错（调用它也不会抛），不校验就会把哨兵当成 item_id
    写进快照（见记忆「游戏代码只能反汇编读」）。取不到就回落 `fallback`，
    回落值只是**参考**（它可能是同组第一件的 id），调用方要能分辨。
    """
    getter = getattr(room, "get_component_item_id", None)
    if not callable(getter):
        return fallback
    try:
        value = getter(gid)
    except Exception:
        return fallback
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return fallback                       # Swallow / None / repr 字符串
    return iv if iv > 0 else fallback


def get_item_data():
    """返回可放置物件目录 dict（约 3963 项）。"""
    room = get_room()
    if room is None:
        return None
    try:
        G = room.create_unit_with_info.__globals__
        GK = G.get("GK")
        return GK.DM.ugc_item_data
    except Exception:
        return None


def next_owner_uid(cm, room=None):
    """生成一个当前未使用的 owner uid。"""
    mx = 0
    for c in (getattr(cm, "_ugc_id2component", None) or {}).values():
        u = getattr(c, "_owner_uid", None)
        try:
            if u is not None and int(u) > mx:
                mx = int(u)
        except Exception:
            continue
    return mx + 1000 if mx else 1000001


# --------------------------------------------------------------------------
# component 序列化
# --------------------------------------------------------------------------
_POS_KEYS = ("_pos", "_position", "pos", "position", "_world_pos")
_ROT_KEYS = ("_rot", "_rotation", "rot", "rotation", "_euler")
_SCALE_KEYS = ("_scale", "scale")


def _probe_attr(comp, keys):
    for k in keys:
        try:
            v = getattr(comp, k, None)
        except Exception:
            continue
        if v is None:
            continue
        try:
            v = v() if callable(v) else v
        except Exception:
            continue
        return _safe(v)
    return None


def _unit_of(comp):
    """UGC 组件的实体 unit（位置/旋转/缩放都在 unit 上）。"""
    for k in ("_owner", "unit", "_unit"):
        u = getattr(comp, k, None)
        if u is not None:
            return u
    for m in ("get_unit", "get_owner"):
        f = getattr(comp, m, None)
        if callable(f):
            try:
                u = f()
                if u is not None:
                    return u
            except Exception:
                pass
    return None


def _unit_transform(comp):
    """读取挂在 unit 上的 position / rotation / scale。"""
    u = _unit_of(comp)
    if u is None:
        return None, None, None
    out = []
    for names in (("get_position3", "get_position", "GetPosition", "get_pos", "position"),
                  ("ugc_dir", "get_rotation", "GetRotation", "get_rot", "rotation", "get_euler"),
                  ("ugc_size", "get_scale", "GetScale", "scale")):
        v = None
        for n in names:
            f = getattr(u, n, None)
            if f is None:
                continue
            try:
                v = f() if callable(f) else f
            except Exception:
                v = None
            if v is not None:
                break
        out.append(_safe(v))
    return out[0], out[1], out[2]


def describe_component(comp, full=False):
    """把一个 UGC component 序列化成 dict。"""
    if comp is None:
        return None
    d = {
        "class": type(comp).__name__,
        "component_type": _safe(getattr(comp, "COMPONENT_TYPE", None)),
        "owner_uid": _safe(getattr(comp, "_owner_uid", None)),
    }
    # ugc_id：优先属性，其次方法
    for k in ("ugc_id", "_ugc_id", "get_ugc_id"):
        try:
            v = getattr(comp, k, None)
            if v is None:
                continue
            v = v() if callable(v) else v
            if v is not None:
                d["ugc_id"] = _safe(v)
                break
        except Exception:
            continue

    pos, rot, scl = _unit_transform(comp)
    if pos is None:
        pos = _probe_attr(comp, _POS_KEYS)
    if rot is None:
        rot = _probe_attr(comp, _ROT_KEYS)
    if scl is None:
        scl = _probe_attr(comp, _SCALE_KEYS)
    d["position"] = pos
    d["rotation"] = rot
    d["scale"] = scl

    # component_info：组件的参数与事件（随笔浮想真正的数据面）
    try:
        ci = getattr(comp, "component_info", None)
        if ci is not None:
            d["component_info"] = _safe(ci)
    except Exception:
        pass

    # 名称（若有）
    for k in ("_name", "name", "get_name"):
        try:
            v = getattr(comp, k, None)
            if v is None:
                continue
            v = v() if callable(v) else v
            if isinstance(v, str):
                d["name"] = v
                break
        except Exception:
            continue

    if full:
        attrs = {}
        for a in dir(comp):
            if a.startswith("__"):
                continue
            try:
                v = getattr(comp, a)
            except Exception:
                continue
            if callable(v):
                continue
            try:
                attrs[a] = _safe(v)
            except Exception:
                pass
        d["attrs"] = attrs
        d["methods"] = [m for m in dir(comp)
                        if not m.startswith("__") and callable(getattr(comp, m, None))][:200]
    return d


# --------------------------------------------------------------------------
# 操作实现
# --------------------------------------------------------------------------
def _session_health():
    """客户端↔服务器会话健康（只读）。

    随笔浮想的**退出与保存都是服务端中介的流程**：room 和 UI 都还在、
    `Avatar.soul_server` 却已是 None 时（断线/网络切换后客户端拒绝重连，
    进程只剩 CLOSE_WAIT 套接字），点退出毫无反应、save_map 也永无回执。
    2026-09-20 09:18 实机命中（5518 件图，非工具链故障）。
    把这个状态显式报出来，免得智能体继续往一个不可能上送的会话里摆放。
    """
    gk = _get_GK()
    out = {"gk_available": gk is not None}
    try:
        av = getattr(gk, "g_avatar", None) if gk is not None else None
        out["avatar_present"] = av is not None
        out["soul_server_present"] = (getattr(av, "soul_server", None) is not None
                                      if av is not None else False)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["soul_server_present"] = False
    out["connected"] = bool(out["gk_available"] and out.get("avatar_present")
                            and out.get("soul_server_present"))
    if not out["connected"]:
        out["warning"] = ("客户端与游戏服务器的会话已断：编辑器退出按钮与 save_map 都不会有结果。"
                          "已落库的内容仍在服务端；请先恢复网络并重启游戏客户端重进地图，"
                          "不要再向本会话摆放物件。")
    return out


def op_status(_a):
    core = get_core()
    cm = get_component_manager()
    em = get_event_manager()
    out = {
        "pid": os.getpid(),
        "bridge_alive": True,
        "editor_core": None if core is None else {
            "repr": repr(core)[:120],
            "is_client": (lambda: (core.is_client() if callable(getattr(core, 'is_client', None)) else None))(),
            "room": repr(getattr(core, "room", None))[:120],
        },
        "component_manager": cm is not None,
        "event_manager": em is not None,
        "server_session": _session_health(),
    }
    try:
        import game3d
        out["frame_count"] = game3d.get_frame_count()
        out["frame_rate"] = game3d.get_frame_rate()
    except Exception:
        pass
    if cm is not None:
        try:
            out["component_count"] = len(getattr(cm, "_ugc_id2component", None) or {})
        except Exception:
            pass
    return out


def op_map_info(_a):
    core = get_core()
    info = {}
    if core is not None:
        # 地图基础信息（名称/描述/标签）
        try:
            from common_cs.ugc_edit_core.parts.map_setting.iUGCEditMapSettingPart import (
                iUGCEditMapSettingPart)
            p = core.get_part(iUGCEditMapSettingPart)
            if p is not None and hasattr(p, "get_map_setting_data"):
                info["map_setting"] = _safe(p.get_map_setting_data())
            else:
                info["map_setting"] = None
        except Exception as e:
            info["map_setting_err"] = "%s: %s" % (type(e).__name__, e)
        # 地图编辑数据
        try:
            from common_cs.ugc_edit_core.parts.map_edit_data.iUGCEditMapEditDataPart import (
                iUGCEditMapEditDataPart)
            p = core.get_part(iUGCEditMapEditDataPart)
            if p is not None and hasattr(p, "get_map_edit_data"):
                info["map_edit_data"] = _safe(p.get_map_edit_data())
            else:
                info["map_edit_data"] = None
        except Exception as e:
            info["map_edit_data_err"] = "%s: %s" % (type(e).__name__, e)
    cm = get_component_manager()
    if cm is not None:
        try:
            info["anchor_info"] = _safe(cm.get_anchor_info())
        except Exception as e:
            info["anchor_err"] = "%s: %s" % (type(e).__name__, e)
        try:
            t2ids = getattr(cm, "_component_type2ugc_ids", None) or {}
            t2str = get_type_names(cm)
            info["type_distribution"] = {
                "%s(%s)" % (t2str.get(str(t), t), t): len(v) for t, v in t2ids.items()
            }
            info["total_components"] = sum(len(v) for v in t2ids.values())
        except Exception as e:
            info["dist_err"] = "%s: %s" % (type(e).__name__, e)
    return info


def op_list_types(a):
    """组件类型清单。

    现在由 fuxiang_catalog 提供权威数据：登记表 ∪ 实现类 ∪ 物件目录
    component_id 的全集，并给出能否创建的判据。保留旧字段以便兼容。

    目录上下文不可用时（例如数据表尚未就绪）自动降级为「只用登记表 +
    工厂缓存」的旧行为，保证本接口在任何时刻都可用。
    """
    cm = get_component_manager()
    factory = getattr(cm, "_component_factory", None) or {}
    t2ids = getattr(cm, "_component_type2ugc_ids", None) or {}
    cat = None
    if cm is not None:
        try:
            cat = _catalog_call("op_list_component_types",
                                dict(a, limit=int(a.get("limit") or 200)))
            if isinstance(cat, dict) and cat.get("error"):
                cat = None
        except Exception:
            cat = None

    out = []
    if cat is not None:
        for c in (cat.get("component_types") or []):
            t = c["component_type"]
            out.append({
                "type": t,
                "name": c.get("name"),
                "name_cn": c.get("name_cn"),
                "count": _count_of(t2ids, t),
                "factory_cached": bool(c.get("factory_warm")),
                "component_class": c.get("class"),
                "registered": bool(c.get("registered")),
                "creatable": bool(c.get("creatable")),
                "catalog_item_count": c.get("catalog_item_count"),
                "place_support": c.get("place_support"),
                "property_schema": c.get("property_schema"),
                "not_creatable_kind": c.get("not_creatable_kind"),
                # 旧字段：仅表示「本地预览路径是否已验证」。已不再等同于可摆放性。
                "local_place_supported": c.get("place_support") == "verified",
            })
        total, returned = cat.get("total"), cat.get("returned")
        source = "fuxiang_catalog"
    else:
        t2str = get_type_names(cm) if cm is not None else {}
        try:
            scene = importlib.reload(importlib.import_module("fuxiang_scene"))
            verified = set(getattr(scene, "VERIFIED_TYPES", ()) or ())
        except Exception:
            verified = set()
        for t in sorted(t2str, key=lambda x: int(x) if x.isdigit() else 0):
            ti = int(t) if t.isdigit() else t
            out.append({
                "type": ti, "name": t2str[t], "name_cn": None,
                "count": _count_of(t2ids, ti),
                "factory_cached": ti in factory,
                "component_class": None,
                "registered": True,
                # 降级路径拿不到实现类扫描结果，不做无根据的判定。
                "creatable": None,
                "catalog_item_count": None,
                "place_support": "verified" if ti in verified else "unknown",
                "property_schema": None,
                "not_creatable_kind": None,
                "local_place_supported": ti in verified,
            })
        total = returned = len(out)
        source = "registry_only_fallback"

    return {"types": out,
            "total": total,
            "returned": returned,
            "source": source,
            "factory_size": len(factory),
            "registered_factory": {str(k): getattr(v, "__name__", repr(v)[:80])
                                   for k, v in factory.items()},
            "note": "types 为全集（含无实现类的目录孤儿）。判断可创建请看 creatable。"}


def _count_of(container, key):
    if container is None:
        return 0
    v = None
    for k in (key, str(key)):
        try:
            if k in container:
                v = container[k]
                break
        except Exception:
            continue
    if v is None:
        return 0
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    try:
        return len(v)
    except Exception:
        return 0



def op_list_components(a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    t2str = get_type_names(cm)
    id2c = getattr(cm, "_ugc_id2component", None) or {}

    type_filter = a.get("type")
    limit = int(a.get("limit") or 200)
    offset = int(a.get("offset") or 0)

    items = []
    for uid, comp in id2c.items():
        ct = getattr(comp, "COMPONENT_TYPE", None)
        if type_filter is not None:
            try:
                if int(ct) != int(type_filter):
                    continue
            except Exception:
                continue
        d = describe_component(comp)
        if d.get("ugc_id") is None:
            d["ugc_id"] = _safe(uid)
        d["type_name"] = t2str.get(str(ct), None)
        items.append(d)
    items.sort(key=lambda x: (str(x.get("type_name") or ""), str(x.get("ugc_id"))))
    return {
        "total": len(items),
        "offset": offset,
        "returned": len(items[offset:offset + limit]),
        "components": items[offset:offset + limit],
    }


def _ci_json(comp):
    """component_info → JSON 字符串。严格 dumps 优先，失败才降级 _jdump。

    ⚠️ 顺序不能反：先过 `_safe` 会把嵌套 5 层以上的值压成 "<deep>"（颜色 [["FFFFFF"]] 就在这层）。
    """
    ci = getattr(comp, "component_info", None)
    if ci is None:
        return None
    try:
        return json.dumps(ci, ensure_ascii=False)
    except Exception:
        try:
            return _jdump(ci)
        except Exception:
            return None


def op_global_singletons(a):
    """只读：列出**全局单例**组件（天空盒/雾/LUT/游戏时间/倒计时/安装器…）。

    跨图复刻观感时用：目标图**已经各自有一套**单例，正确做法是「按类型配对后改写属性」，
    而不是再摆一套。本接口就是给还原侧做配对用的。

    args:
      types   可选，只返回这些 component_type（默认全部 >= GLOBAL_TYPE_FLOOR=10000000）
      floor   可选，单例编号下界，默认 10000000
      with_ci true 时附带 component_info（**压成 JSON 字符串**避免 _safe 深度截断）
    """
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    floor = int(a.get("floor") or 10000000)
    want = {int(t) for t in (a.get("types") or [])}
    with_ci = bool(a.get("with_ci"))
    t2str = get_type_names(cm)
    id2c = getattr(cm, "_ugc_id2component", None) or {}

    items = []
    for uid, comp in id2c.items():
        ct = getattr(comp, "COMPONENT_TYPE", None)
        try:
            ct_i = int(ct)
        except (TypeError, ValueError):
            continue
        if ct_i < floor or (want and ct_i not in want):
            continue
        it = {"ugc_id": _safe(getattr(comp, "_ugc_id", uid)),
              "component_type": ct_i,
              "type_name": t2str.get(str(ct_i), None),
              "owner_uid": _safe(getattr(comp, "_owner_uid", None))}
        if with_ci:
            # ⚠️ 先严格 dumps 再降级：先过 _safe 会把 info 里的嵌套值压成 "<deep>"
            it["ci"] = _ci_json(comp)
        items.append(it)
    items.sort(key=lambda x: (x.get("component_type") or 0, str(x.get("ugc_id"))))
    return {"total": len(items), "floor": floor, "components": items}


def op_material_info(a):
    """只读：实体运行时材质 vs 组件属性袋材质，逐件对比。args: ugc_ids"""
    room = get_room()
    if room is None:
        return {"error": "拿不到 room（确认在「随笔浮想」编辑器内）"}
    ids = a.get("ugc_ids") or []
    if not isinstance(ids, list) or not ids:
        return {"error": "需要 ugc_ids 数组（单批上限 50）"}
    if len(ids) > 50:
        return {"error": "单批最多 50 件"}
    cm = get_component_manager()
    out, missing = [], []
    for gid in ids:
        comp = cm._ugc_id2component.get(int(gid))
        if comp is None:
            missing.append(int(gid))
            continue
        bag = {}
        for e in (comp.component_info or {}).get("events", []) or []:
            if e.get("type") in (10000110005, 10000110006, 10000110007):
                bag[e["type"]] = e.get("info")
        rec = {"ugc_id": int(gid),
               "item_id": getattr(getattr(comp, "_owner", None), "ugc_type", None),
               "prop_mtg": bag.get(10000110005), "prop_color": bag.get(10000110006),
               "prop_alpha": bag.get(10000110007)}
        model = getattr(getattr(comp, "_owner", None), "model", None)
        if model is None:
            rec["runtime"] = None
            rec["note"] = "实体无 model（可能是逻辑组件或尚未实例化）"
            out.append(rec)
            continue
        # material_group 不是序列：既没有 __iter__ 也没有 __len__/__getitem__，
        # list(model.all_materials) 会抛 TypeError: not iterable。
        # 正确取法是按 count 下标 get_at(i)。
        mg = model.all_materials
        try:
            n = int(getattr(mg, "count", 0) or 0)
            slots = []
            for i in range(n):
                s = mg.get_at(i)
                try:
                    tech = s.get_technique_name()
                except Exception as exc:
                    tech = "<err %s>" % type(exc).__name__
                slots.append({"technique": tech,
                              "filename": getattr(s, "filename", ""),
                              "shader": getattr(s, "shader", ""),
                              "transparent_mode": getattr(s, "transparent_mode", None),
                              "alpha": getattr(s, "alpha", None)})
            rec["runtime"] = {"group_data": model.get_material_group_data(),
                              "count": n, "slots": slots}
        except Exception:
            rec["runtime"] = {"error": traceback.format_exc()[-400:]}
        want = rec.get("prop_mtg")
        if isinstance(want, list) and want and isinstance(rec.get("runtime"), dict):
            base = str(want[0]).replace("\\", "/").rsplit("/", 1)[-1]
            got = str(rec["runtime"].get("group_data") or "")
            if base and base.split(".")[0] != got.split(".")[0]:
                rec["mismatch"] = {"assigned_mtg": base, "runtime_group": got}
        out.append(rec)
    return {"ok": True, "materials": out, "missing_ugc_ids": missing,
            "note": "material_group 只能按 count + get_at(i) 取；"
                    "slot 的 filename/shader 常为空串，有信息量的是 technique "
                    "与 group_data（原生 mtg 名）。mismatch 只说明属性袋与运行时不一致，"
                    "不等于渲染失败，须截图确认。"}


def op_get_component(a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    uid = a.get("ugc_id")
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    comp = None
    try:
        comp = cm.get_ugc_component(uid)
    except Exception:
        comp = None
    if comp is None:
        comp = id2c.get(uid) or id2c.get(str(uid)) or id2c.get(int(uid) if str(uid).isdigit() else uid)
    if comp is None:
        for k, v in id2c.items():
            if str(k) == str(uid):
                comp = v
                break
    if comp is None:
        return {"error": "未找到组件 ugc_id=%r（当前共 %d 个）" % (uid, len(id2c))}
    d = describe_component(comp, full=bool(a.get("full")))
    # 参数/事件
    try:
        child_key = a.get("child_key")
        if child_key:
            d["params"] = _safe(cm.get_ugc_params_events_info(child_key, str(uid)))
    except Exception as e:
        d["params_err"] = "%s: %s" % (type(e).__name__, e)
    return d


def op_create_component(a):
    """
    低层附加组件接口：检查类型和空闲 owner，分配无冲突 ID。
    普通场景物件应使用 op_place_item，以完成实体创建和组件初始化。
    """
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    info = a.get("info")
    if not isinstance(info, dict):
        return {"error": "需要 info 字典（至少含 component_type 与 owner_uid）"}

    info = dict(info)
    ct = int(info.get("component_type"))
    info["component_type"] = ct
    # 游戏会按需加载组件类，factory 是缓存，不是可创建类型白名单。
    if str(ct) not in get_type_names(cm):
        return {"error": "未知 component_type=%r" % ct}

    # 护栏 2：必须能解析出 owner
    if "owner_uid" not in info:
        uid_sample = []
        for uid, c in list((getattr(cm, "_ugc_id2component", None) or {}).items())[:5]:
            u = getattr(c, "_owner_uid", None)
            if u is not None and u not in uid_sample:
                uid_sample.append(u)
        return {"error": "info 缺少 owner_uid（游戏会静默返回 None）。"
                         "当前地图中可用的 owner_uid 示例: %s" % uid_sample}

    id2c = getattr(cm, "_ugc_id2component", None)
    snapshot = dict(id2c) if isinstance(id2c, dict) else None

    scene = importlib.import_module('fuxiang_scene')
    uid = info.get('ugc_id')
    if uid is not None:
        if isinstance(uid, bool) or not str(uid).isdigit() or int(uid) <= 0:
            return {"error": "ugc_id 必须为正整数"}
        uid = int(uid)
        if any(str(k) == str(uid) for k in id2c) or any(
                any(str(k) == str(uid) for k in ids) for ids in cm._component_type2ugc_ids.values()):
            return {"error": "ugc_id 已被占用，未执行创建"}
    else:
        uid = scene.next_component_id(cm)
    info['ugc_id'] = uid
    owner = cm.create_component_client.__globals__['ugc_utils'].get_unit(cm._room, info['owner_uid'])
    if owner is None:
        return {"error": "owner_uid 无法解析为有效 unit"}
    if getattr(owner, 'ugc_component', None) is not None:
        return {"error": "owner 已有组件，未执行覆盖；新增场景物件请用 fuxiang_place_item"}

    try:
        comp = cm.create_component_client(info)
    except Exception:
        return {"error": traceback.format_exc()[-1500:]}

    if comp is None:
        return {"error": "游戏返回 None（未创建）。通常是 owner_uid 无法解析为有效 unit。"}

    cm._ugc_id_counter = max(cm._ugc_id_counter, uid)

    # 护栏 3：ID 冲突检测与回滚
    uid = getattr(comp, "_ugc_id", None)
    if snapshot is not None and uid in snapshot and snapshot[uid] is not comp:
        try:
            id2c.clear()
            id2c.update(snapshot)
        except Exception:
            pass
        return {"error": "检测到 ugc_id 冲突：新组件 id=%s 与已有组件重复，已回滚索引。"
                         "这是游戏 ID 生成器的已知问题；请改用 fuxiang_eval 手工创建，"
                         "或先调用 fuxiang_repair_index。"
                         % (uid,),
                "rolled_back": True,
                "component": describe_component(comp, full=True)}

    # 当前版本游戏会自动登记；保留对缺失登记的兼容处理。
    registered = False
    if isinstance(id2c, dict) and uid is not None and uid not in id2c:
        try:
            id2c[uid] = comp
            t2ids = getattr(cm, "_component_type2ugc_ids", None)
            if isinstance(t2ids, dict):
                t = getattr(comp, "COMPONENT_TYPE", None)
                if t is not None:
                    t2ids.setdefault(t, set()).add(uid)
            registered = True
        except Exception:
            pass

    return {"created": describe_component(comp, full=True),
            "registered_into_index": registered}


def op_remove_component(a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    uid = a.get("ugc_id")
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    comp = None
    for k, v in id2c.items():
        if str(k) == str(uid):
            comp = v
            break
    if comp is None:
        return {"error": "未找到组件 ugc_id=%r" % uid}

    # 校验：索引里的对象必须与找到的一致，避免删错
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    indexed = id2c.get(uid) if isinstance(id2c, dict) else None
    conflict = (indexed is not None and indexed is not comp)

    before = describe_component(comp)
    if getattr(_unit_of(comp), '_fuxiang_local_item', None):
        return op_remove_item(a)
    try:
        cm.remove_component(comp)
    except Exception:
        return {"error": traceback.format_exc()[-1500:], "component": before}

    out = {"removed": before}
    if conflict:
        out["warning"] = ("ugc_id=%r 在索引中指向另一个对象，可能发生 ID 冲突。"
                          "建议调用 fuxiang_repair_index 校验一致性。" % (uid,))
    return out


def op_list_items(a):
    """列出可放置物件目录（ugc_item_data），支持关键字/类型/分类过滤。

    每一行都带上中文名、编辑器分类路径与所属组件类型，便于直接定位到接口参数。
    """
    data = get_item_data()
    if not data:
        return {"error": "物件目录不可用（需处于随笔浮想编辑器中）"}
    kw = (a.get("query") or "").lower()
    item_type = a.get("item_type")
    category_id = a.get("category_id")
    limit = int(a.get("limit") or 50)
    offset = int(a.get("offset") or 0)
    include_hidden = a.get("include_hidden", False)
    if not isinstance(include_hidden, bool):
        return {"error": "include_hidden 必须为布尔值"}
    scene = importlib.reload(importlib.import_module('fuxiang_scene'))
    visible_models = scene.visible_model_index(data)
    try:
        catalog = _catalog_call("op_build", {"refresh": False})
        index_items = catalog.get("items") or {}
        index_comps = catalog.get("components") or {}
    except Exception:
        index_items, index_comps = {}, {}

    out = []
    hidden_matches = 0
    for k, v in data.items():
        if item_type is not None and str(v.get("item_type")) != str(item_type):
            continue
        if category_id is not None and str(v.get("tap_order_sec")) != str(category_id):
            continue
        if kw:
            extra = index_items.get(int(k) if str(k).isdigit() else k, {})
            blob = "%s %s %s %s %s" % (k, v.get("item_tid", ""), v.get("model_path", ""),
                                       extra.get("name_cn") or "",
                                       extra.get("category_cn") or "")
            if kw not in blob.lower():
                continue
        if v.get('is_visible') and not include_hidden:
            hidden_matches += 1
            continue
        idx = index_items.get(int(k) if str(k).isdigit() else k, {})
        ctype = v.get("component_id")
        try:
            ctype = int(ctype)
        except Exception:
            pass
        comp = index_comps.get(ctype) or {}
        out.append({
            "item_id": _safe(k),
            "name_cn": idx.get("name_cn"),
            "component_id": _safe(ctype),
            "component_name": comp.get("name"),
            "component_class": comp.get("class"),
            "component_creatable": comp.get("creatable"),
            "category_id": idx.get("category_id"),
            "category_cn": idx.get("category_cn"),
            "category_group_cn": idx.get("category_group_cn"),
            "item_type": _safe(v.get("item_type")),
            "item_tid": _safe(v.get("item_tid")),
            "model_path": _safe(v.get("model_path")),
            "img_path": _safe(v.get("img_path")),
            "occupancy_value": _safe(v.get("occupancy_value")),
            "zoom_limit": _safe(v.get("zoom_limit")),
            "rotate_limit": _safe(v.get("rotate_limit")),
            "disable_edit": _safe(v.get("disable_edit")),
            **scene.catalog_identity(data, k, v, visible_models),
        })
    out.sort(key=lambda x: str(x.get("item_id")))
    return {"total": len(out), "offset": offset,
            "excluded_hidden_matches": hidden_matches,
            "note": "模型相同不代表物件功能相同；请核对 item_id 与 component_id。"
                    "隐藏记录需 include_hidden=true。component_creatable=false 表示该物件"
                    "没有实现类，无法通过接口创建。",
            "returned": len(out[offset:offset + limit]),
            "items": out[offset:offset + limit]}


def _scene_call(name, args):
    scene = smart_reload("fuxiang_scene")
    return getattr(scene, name)(sys.modules[__name__], args)


def _catalog_call(name, args):
    catalog = smart_reload("fuxiang_catalog")
    return getattr(catalog, name)(sys.modules[__name__], args)


def op_catalog_summary(a):
    """全量注册表概览：组件类型数、可创建数、物件数、摆放支持矩阵。"""
    return _catalog_call("op_catalog_summary", a)


def op_catalog_types(a):
    """列出全部组件类型（含无实现类的孤儿），可按 only/query 过滤。"""
    return _catalog_call("op_list_component_types", a)


def op_catalog_type(a):
    """单个组件类型的完整档案：实现类、目录物件、属性 schema 可得性。"""
    return _catalog_call("op_get_component_type", a)


def op_catalog_find(a):
    """统一寻址：item_id / component_type / query / enum 任一定位组件或物件。"""
    return _catalog_call("op_find", a)


def op_catalog_schema(a):
    """取组件类型的属性 schema（ugc_item_property / ugc_control_panel）。"""
    return _catalog_call("op_component_schema", a)


def op_catalog_enums(a):
    """列出组件属性取值枚举（common_cs.ugc.UGCEnums）。"""
    return _catalog_call("op_list_enums", a)


def op_catalog_categories(a):
    """列出编辑器的物件分类树（ugc_item_tab）。"""
    return _catalog_call("op_list_categories", a)


def op_catalog_export(a):
    """把注册表导出到 tools/fuxiang/registry/。"""
    return _catalog_call("op_registry_export", a)


def op_catalog_items(a):
    """逐个枚举物件目录（含隐藏项）。limit=0 取回全部，是「定位所有物体」的枚举入口。"""
    return _catalog_call("op_catalog_items", a)


def op_catalog_verify(a):
    """全量可达性自检：一次调用走通全部物件与全部组件类型。"""
    return _catalog_call("op_catalog_verify", a)


# --------------------------------------------------------------------------
# 资源大厅（组合库）—— 只读定位
# --------------------------------------------------------------------------
def _hall_call(name, args):
    hall = importlib.reload(importlib.import_module("fuxiang_resource_hall"))
    return getattr(hall, name)(sys.modules[__name__], args)


def op_hall_info(a):
    """资源大厅（组合库）总览：结构常量、库状态、标签表、记录字段表。"""
    return _hall_call("op_hall_info", a)


def op_hall_items(a):
    """枚举资源大厅里的组合记录（官方 PGC + 玩家自制）。"""
    return _hall_call("op_hall_items", a)


def op_hall_item(a):
    """按 combo_id / share_code 取单条组合，含 combo_data 拆解。"""
    return _hall_call("op_hall_item", a)


def op_hall_verify(a):
    """资源大厅记录自检：字段完整性、combo_data 可解析性。"""
    return _hall_call("op_hall_verify", a)


def op_hall_ui(a):
    """资源大厅列表页实况：页签/排序/标签状态、分类树、已渲染卡片数。"""
    return _hall_call("op_hall_ui", a)


def op_hall_cards(a):
    """枚举资源大厅当前页签下的卡片（资源清单）。"""
    return _hall_call("op_hall_cards", a)


def op_hall_export(a):
    """把资源大厅当前页签的卡片清单导出为 JSON 快照。"""
    return _hall_call("op_hall_export", a)


# --------------------------------------------------------------------------
# 可编辑物体的数值修改（变换 / 属性）—— 独立于摆放逻辑
# --------------------------------------------------------------------------
def _edit_call(name, args):
    ops = importlib.reload(importlib.import_module("fuxiang_edit_ops"))
    return getattr(ops, name)(sys.modules[__name__], args)


def op_edit_targets(a):
    """列出可编辑物件（含可编辑性判定）。"""
    return _edit_call("op_edit_targets", a)


def op_edit_target(a):
    """定位单个可编辑物件的变换与属性袋（可联表属性 schema）。"""
    return _edit_call("op_edit_target", a)


def op_edit_transform(a):
    """修改已存在物件的位置/旋转/缩放，并登记保存变更集。"""
    return _edit_call("op_edit_transform", a)


def op_edit_props(a):
    """修改已存在物件的组件属性（property_id -> 值）。"""
    return _edit_call("op_edit_props", a)


def op_set_component_info(a):
    """整段回写 component_info['events']（快照复原用；带回读校验与失败回滚）。"""
    return _edit_call("op_set_component_info", a)


def op_set_component_info_batch(a):
    """批量整袋回写 N 件（快照 props 阶段提速；逐件同一护栏 + 逐件升级重试）。"""
    return _edit_call("op_set_component_info_batch", a)


def op_set_map_setting(a):
    """写地图设置（key/value；跨图还原场景框架用，key "14"=框架 4001/4002/4003）。"""
    return _edit_call("op_set_map_setting", a)


def op_get_map_bgm(a):
    """读地图默认背景音乐（`ugc_props_info['ugc_bgm_conf']`，快照的 map.props 那一层）。"""
    return _edit_call("op_get_map_bgm", a)


def op_set_map_bgm(a):
    """写地图默认背景音乐（走「更多→网易云音乐→背景音乐设置」面板的同一条通道）。"""
    return _edit_call("op_set_map_bgm", a)


def op_edit_history(a):
    """查看本会话经接口做过的数值改动。"""
    return _edit_call("op_edit_history", a)


def op_delete_preview(a):
    """删除预览：哪些对象会被删、组合级联预估（只读）。"""
    return _del_call("op_del_preview", a)


def op_delete_objects(a):
    """删除场景中的对象（走游戏原生命令层，支持 dry_run）。"""
    return _del_call("op_del_objects", a)


def _del_call(name, args):
    dl = smart_reload("fuxiang_edit_del")
    return getattr(dl, name)(sys.modules[__name__], args)


# --------------------------------------------------------------------------
# 组合下载（资源大厅 → 玩家账号库；写操作，单条 + 轮询）
# --------------------------------------------------------------------------
def _dl_call(name, args):
    dl = smart_reload("fuxiang_combo_dl")
    return getattr(dl, name)(sys.modules[__name__], args)


def op_combo_download(a):
    """发送一条组合下载请求（服务端写入）。非阻塞，随后轮询。"""
    return _dl_call("op_combo_download", a)


def op_combo_download_poll(a):
    """查询下载结果：组合库中是否新增条目。"""
    return _dl_call("op_combo_download_poll", a)


def op_combo_download_status(a):
    """查看下载能力、库容量与历史（不发起下载）。"""
    return _dl_call("op_combo_download_status", a)


# --------------------------------------------------------------------------
# 预览渲染（复用引擎帧循环出图）—— 不建相机 / 不毁相机 / 不碰 RenderTarget
# --------------------------------------------------------------------------
def _preview_call(name, args):
    preview = importlib.reload(importlib.import_module("fuxiang_preview"))
    return getattr(preview, name)(sys.modules[__name__], args)


def op_preview_plan(a):
    """只算不拍：由包围盒推导视角列表（正俯视 + 四角斜视）。"""
    return _preview_call("op_preview_plan", a)


def op_preview_pose(a):
    """把编辑器相机摆到指定视角，首次调用会备份原姿态。"""
    return _preview_call("op_preview_pose", a)


def op_preview_fire(a):
    """让引擎把当前帧写成图片文件（异步落盘）。"""
    return _preview_call("op_preview_fire", a)


def op_preview_restore(a):
    """把相机还原到备份姿态。"""
    return _preview_call("op_preview_restore", a)


def op_preview_state(a):
    """诊断：相机姿态、备份状态、出图接口可用性。"""
    return _preview_call("op_preview_state", a)


def op_preview_layout(a):
    """导出俯视平面图数据（绘制在主机侧完成）。"""
    return _preview_call("op_preview_layout", a)


def op_plan_item(a):
    return _scene_call("plan", a)


def op_place_item(a):
    return _scene_call("place", a)


def op_place_poll(a):
    return _scene_call("place_poll", a)


def op_place_batch(a):
    """批量摆放：一条命令登记 N 件（快照还原提速；逐件仍走同一套 plan 护栏）。"""
    return _scene_call("place_batch", a)


def op_place_poll_batch(a):
    """批量 poll：一条命令推进整批摆放请求（逐 token 回执 + 退避建议）。"""
    return _scene_call("place_poll_batch", a)


def op_recover_pending(a):
    """恢复"服务端已创建、原生确认未完成"的物件登记（room 被重建后的待确认态）。

    args: ugc_ids（可省=全量待确认）, dry_run（默认 true）, limit（默认 50）。
    只走客户端命令层，不向服务端重发创建；恢复后须立即 save_map。
    """
    return _scene_call("recover_pending", a)


def op_remove_item(a):
    return _scene_call("remove", a)


def op_set_transform(a):
    return _scene_call("set_transform", a)


def op_pending_saves(a):
    """查看编辑器的待保存队列（save_component_info）。

    这是本地事件重放队列，不是服务器保存收据。摆放接口不向其插入事件。
    """
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    lst = getattr(cm, "save_component_info", None)
    if not isinstance(lst, list):
        return {"error": "保存队列结构异常", "type": type(lst).__name__}
    limit = int(a.get("limit") or 50)
    items = []
    for e in lst[:limit]:
        if not isinstance(e, dict):
            items.append({"repr": repr(e)[:200]})
            continue
        entry = {"key": _safe(e.get("key"))}
        cps = e.get("creat_params")
        if isinstance(cps, list):
            entry["unit_count"] = len(cps)
            uids = []
            for u in cps[:20]:
                if isinstance(u, dict):
                    ci = (u.get("ugc_recover_info") or {}).get("create_info") or {}
                    uids.append({"uid": _safe(u.get("uid")),
                                 "item_id": _safe(u.get("item_id")),
                                 "ugc_id": _safe(ci.get("ugc_id"))})
            entry["units"] = uids
        for k in ("del_uid", "del_key"):
            if k in e:
                entry[k] = _safe(e[k])
        items.append(entry)
    return {
        "pending_count": len(lst),
        "returned": len(items),
        "items": items,
        "is_save_component_info": bool(getattr(cm, "is_save_component_info", False)),
        "note": "这是本地事件重放队列，不代表服务器保存结果；摆放接口不写此队列。",
    }


def op_repair_index(a):
    """
    自愈：把仍存活但脱离索引的组件重新登记回管理器，
    并报告 ghost（索引有、对象无）与 orphan（对象有、索引无）。

    默认不做全堆扫描：ghost/orphan 由组件管理器自己的两个索引差派生出
    （index 有而 id2c 无 / id2c 有而 index 无），绝不调用 `gc.get_objects()`。
    项目纪律明确：进程内 `gc.get_objects()` 曾触发 MemoryError/长时间卡顿。
    只有显式传 `allow_heap_scan=true` 才会做全堆扫描，以找回"存活但完全脱离索引"的组件。
    """
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    id2c = getattr(cm, "_ugc_id2component", None)
    t2ids = getattr(cm, "_component_type2ugc_ids", None)
    if not isinstance(id2c, dict):
        return {"error": "索引结构异常"}
    before = len(id2c)

    restored = []
    if a.get("allow_heap_scan"):
        # ⚠️ 全堆扫描：文档明确该操作在本进程内会 MemoryError / 长时间卡顿。
        #    仅在用户显式 opt-in 时执行。
        found = {}
        try:
            for o in gc.get_objects():
                tn = type(o).__name__
                if not (tn.startswith("UGC") and tn.endswith("Component")):
                    continue
                owner = getattr(o, '_owner', None)
                if getattr(o, '_room', None) is not cm._room or owner is None:
                    continue
                if cm._room.unit_mgr.get_unit(getattr(owner, 'uid', None)) is not owner:
                    continue
                u = getattr(o, "_ugc_id", None)
                if u is None:
                    continue
                found.setdefault(u, []).append(o)
        except MemoryError:
            return {"ok": False,
                    "error": "allow_heap_scan=true 的全堆扫描触发 MemoryError，"
                             "已中止（项目纪律：进程内别扫 gc 堆）",
                    "before": before}
        for uid, objs in found.items():
            if uid in id2c:
                continue
            if len(objs) != 1:
                continue
            comp = objs[0]
            try:
                id2c[uid] = comp
                if isinstance(t2ids, dict):
                    t = getattr(comp, "COMPONENT_TYPE", None)
                    if t is not None:
                        t2ids.setdefault(t, set())
                        if uid not in t2ids[t]:
                            t2ids[t].add(uid)
                restored.append({"ugc_id": _safe(uid), "class": type(comp).__name__})
            except Exception as e:
                restored.append({"ugc_id": _safe(uid), "error": str(e)})

    all_ids = set()
    if isinstance(t2ids, dict):
        for ids in t2ids.values():
            try:
                all_ids.update(str(i) for i in ids)
            except Exception:
                pass
    cur = set(str(k) for k in id2c)

    def _k(x):
        return int(x) if x.isdigit() else 0

    heap = bool(a.get("allow_heap_scan"))
    return {
        "before": before,
        "after": len(id2c),
        "restored": restored,
        "ghost_ids": sorted(all_ids - cur, key=_k),
        "orphan_ids": sorted(cur - all_ids, key=_k),
        "heap_scanned": heap,
        "note": ("本调用未做全堆扫描；ghost/orphan 来自索引差。"
                 if not heap else "本次显式 allow_heap_scan=true，已做全堆扫描。") +
                "如需找回完全脱离索引的组件，请显式 allow_heap_scan=true（有 MemoryError 风险）。",
    }


def op_get_params(a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    child_key = a.get("child_key")
    key = a.get("key")
    try:
        if key is None:
            return {"result": _safe(getattr(cm, "_ugc_params_events_key", None))}
        return {"result": _safe(cm.get_ugc_params_events_info(child_key, key))}
    except Exception:
        return {"error": traceback.format_exc()[-1200:]}


def op_set_params(a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    child_key = a.get("child_key")
    key = a.get("key")
    info = a.get("info")
    if child_key is None or key is None or info is None:
        return {"error": "需要 child_key / key / info"}
    try:
        r = cm.modify_ugc_params_events_key(child_key, key, info)
        return {"ok": True, "result": _safe(r)}
    except Exception:
        return {"error": traceback.format_exc()[-1500:]}


def op_list_events(_a):
    cm = get_component_manager()
    if cm is None:
        return {"error": "UGCComponentManager 不可用"}
    try:
        return {"events_key": _safe(getattr(cm, "_ugc_params_events_key", None))}
    except Exception:
        return {"error": traceback.format_exc()[-800:]}


def op_create_event(a):
    em = get_event_manager()
    if em is None:
        return {"error": "UGCEventManager 不可用"}
    et = a.get("event_type")
    args = a.get("args")
    try:
        ev = em.create_event(et, args)
        return {"event": _safe(ev)}
    except Exception:
        return {"error": traceback.format_exc()[-1500:]}


def op_save(a):
    """旧保存标记/改名接口，尚未验证完整地图持久化。"""
    core = get_core()
    cm = get_component_manager()
    out = {}
    if cm is not None:
        try:
            cm.set_save_component_info(True)
            out["save_flag"] = True
        except Exception as e:
            out["save_flag_err"] = "%s: %s" % (type(e).__name__, e)
        try:
            out["event_info"] = _safe(cm.get_save_event_info()) if a.get("with_dump") else "<skipped>"
        except Exception as e:
            out["event_info_err"] = "%s: %s" % (type(e).__name__, e)
    if core is not None:
        try:
            from common_cs.ugc_edit_core.parts.map_setting.iUGCEditMapSettingPart import (
                iUGCEditMapSettingPart)
            p = core.get_part(iUGCEditMapSettingPart)
            if p is not None and a.get("name"):
                out["save_basic_info"] = _safe(p.save_map_basic_info(name=a.get("name")))
        except Exception as e:
            out["basic_info_err"] = "%s: %s" % (type(e).__name__, e)
    out["note"] = "地图保存/发布为服务端权威，请同时在游戏中确认保存结果。"
    return out


# ----------------------------------------------------------------------
# 主动保存地图（方案 A）：复刻 UIBattleUGCEdit._save_change 的
#   send_battle_event(UGC, info) 原生链路，并监听 room.ugc_save_finish 回执。
# 观察器挂在 room 实例上（vars(room)），与 fuxiang_edit 热重载解耦。
# 主线程只做「打包 + 发事件 + 挂观察器」，绝不等待；poll 在主机侧做。
# 参考原型：builds/modern_villa/save_native.py / reload_native.py
# ----------------------------------------------------------------------
_SAVE_SAFE_MISSING = ("<missing>")


def _save_obs(room):
    return vars(room).setdefault("_fuxiang_save_obs", {
        "installed": False, "original": None, "active": None,
        "acks": [], "pending_before": None, "requested_at": 0.0})


def _ensure_save_obs(room):
    """把 room.ugc_save_finish 包一层记录器（幂等），返回 (obs, err)。"""
    st = _save_obs(room)
    if st["installed"]:
        return st, None
    original = getattr(room, "ugc_save_finish", None)
    if not callable(original):
        return None, "room 上不存在可调用的 ugc_save_finish（保存回执监听不可用）"
    st["original"] = original

    def observed(self, event_data):
        st0 = vars(self).get("_fuxiang_save_obs")
        if st0 is not None:
            try:
                st0["acks"].append({"t": time.time(), "data": _safe(event_data)})
            except Exception:
                st0["acks"].append({"t": time.time(), "data": "<unsafe>"})
        orig = st0["original"] if st0 else None
        try:
            return orig(event_data) if callable(orig) else None
        except Exception:
            return None

    room.ugc_save_finish = types.MethodType(observed, room)
    st["installed"] = True
    return st, None


def _get_edit_ui(room):
    """取 UIBattleUGCEdit 实例（保存变更集 ui.ugc_params['change_units'] 所在）。"""
    try:
        G = room.create_unit_with_info.__globals__["GK"]
    except Exception:
        return None
    ui = getattr(getattr(G, "input", None), "ugc_edit_ui", None)
    if ui is not None and isinstance(getattr(ui, "ugc_params", None), dict):
        return ui
    gui = getattr(G, "g_ui_mgr", None)
    if gui is None:
        return None
    try:
        ui = gui.get_ui("UIBattleUGCEdit")
    except Exception:
        return None
    return ui if isinstance(getattr(ui, "ugc_params", None), dict) else None


def reg_change(ui, unit, comp=None):
    """登记保存变更集；顺带处理「一个单元挂多个同类组件」的漏登记。

    原生 ``ui._add_change_unit(unit.uid)`` 内部走 ``get_single_unit_key(unit)``：
    当 ``room.single_unit_mgr.check_unit_single(unit)`` 为真时，它返回的是该单元
    **当前那一个**组件的 ugc_id（实测运动器 6 个共用 unit、剧情/对话/选项同样共用），
    于是本次真正改动的组件登记不进去、保存时不上送 —— 属性袋写得再对也白写。

    所以在原生登记之后，把目标组件自己补进 ``change_units['component']``
    （与引擎自身的分桶口径一致；``_get_change_uid`` 对 component 桶是按组件取 params 的，
    实测同一单元下 172 与 176 各自一份、内容不同）。

    返回本组件是否已在登记集合里，供调用方回读上报。
    """
    ui._add_change_unit(unit.uid)
    if comp is None:
        return False
    cu = (getattr(ui, "ugc_params", None) or {}).get("change_units")
    if not isinstance(cu, dict):
        return False
    try:
        if not ui.check_unit_single(unit):
            return False
    except Exception:
        return False
    cid = getattr(comp, "_ugc_id", None)
    if cid is None:
        return False
    bucket = cu.setdefault("component", set())
    if isinstance(bucket, dict):
        # 引擎自身的 _add_change_unit 用的是 set（UGCBaseComponent/`setdefault(key, set())`
        # + `.add(uid)`）；这里兼容 dict[component_id] = unit_uid 的写法，
        # 别把它静默丢掉 —— 静丢掉就等于本组件不上送。
        bucket[int(cid)] = getattr(unit, "uid", None)
    elif isinstance(bucket, set):
        bucket.add(cid)
    elif isinstance(bucket, list):
        if not any(int(x) == int(cid) for x in bucket):
            bucket.append(cid)
    else:
        return False
    return any(int(x) == int(cid) for x in bucket)


def op_save_map(a):
    """发起一次服务端保存（async）。与点编辑器保存按钮同链路：
    ui._save_change() → send_battle_event(UGC, {key:'save_change',...})。
    主线程只发事件、丢回执观察器，不等待。用 op_save_map_poll 轮询回执。
    """
    room = get_room()
    if room is None:
        return {"error": "拿不到 room（确认在「随笔浮想」编辑器内）"}
    ui = _get_edit_ui(room)
    if ui is None:
        return {"error": "拿不到编辑器 UI（UIBattleUGCEdit），无法发起保存"}

    st, err = _ensure_save_obs(room)
    if err:
        return {"error": err}
    if st["active"] is not None:
        # 单笔在途护栏；若上一个请求早已超时未回执，自动释放，避免永久阻塞
        if float(st.get("requested_at") or 0) and \
                (time.time() - float(st["requested_at"])) > float(a.get("stale_after") or 120.0):
            st["active"] = None
        else:
            return {"ok": False, "error": "已有保存请求在途（request_id=%s），先轮询它再继续"
                                          "（单笔在途护栏，上限 %.0fs）"
                                          % (st["active"], float(a.get("stale_after") or 120.0))}

    request_id = a.get("request_id") or ("save_%d" % int(time.time() * 1000))
    try:
        change_units = getattr(ui, "ugc_params", {}).get("change_units")
    except Exception:
        change_units = None
    st["active"] = request_id
    st["acks"] = []
    st["pending_before"] = copy.deepcopy(change_units) if change_units is not None else None
    st["requested_at"] = time.time()
    # 保存前快照编辑器登记集合，供 save_map_poll 比对「保存后被摘除登记」的对象。
    # 2026-09-19：save_map 完成后，**未进入该次提交**的已落位对象会被服务端摘除登记集合
    # （实体仍在组件表里，下次保存还会被再次提交 → 同位置重复件）。此前完全靠文档纪律，
    # 现在做成回执里的可观测字段。
    try:
        st["registry_before"] = sorted(
            int(u) for u in (getattr(ui, "all_ugc_uid_set", set()) or set()))
    except Exception:
        st["registry_before"] = None

    try:
        ui._save_change()          # 与保存按钮完全相同的最终一步（含 send_battle_event）
    except Exception as e:
        st["active"] = None
        return {"ok": False, "error": "_save_change 抛错: %s" % traceback.format_exc()[-1500:], "request_id": request_id}

    pending_count = None
    try:
        if isinstance(change_units, dict):
            pending_count = {k: len(v) for k, v in change_units.items()}
    except Exception:
        pass
    return {"ok": True, "request_id": request_id, "submitted": True,
            "pending_before_count": pending_count,
            "note": "已向服务端提交保存。请用 fuxiang_save_map_poll(request_id=...) 轮询 ugc_save_finish 回执。"}


def op_save_map_poll(a):
    """轮询保存回执：room.ugc_save_finish 一旦被引擎回调即视为服务端已回执。"""
    room = get_room()
    if room is None:
        return {"error": "拿不到 room"}
    st = vars(room).get("_fuxiang_save_obs")
    request_id = a.get("request_id")
    timeout = float(a.get("timeout") or 30.0)
    if st is None or not st.get("installed") or st.get("active") is None:
        return {"ok": False, "status": "no_active_request",
                "error": "本会话当前没有在途的保存请求（先调 fuxiang_save_map）"}
    if request_id and st["active"] != request_id:
        return {"ok": False, "status": "no_such_request", "active": st["active"]}
    n = len(st["acks"])
    elapsed = round(time.time() - float(st.get("requested_at") or time.time()), 2)
    rid = st["active"]
    if n > 0:
        st["active"] = None
        out = {"ok": True, "status": "saved", "request_id": rid,
               "receipt_count": n, "receipts": st["acks"], "elapsed": elapsed,
               "persistence": "server_acknowledged",
               "note": "服务端已回执保存完成；真实持久化仍建议保存后退出重进核对。"}
        # 保存基线比对：找出「保存前在编辑器登记集合、保存后消失」的对象。
        # 这类对象多是**未进入本次提交而被服务端回滚**的已落位件 —— 它们仍留在组件表里，
        # 下一次保存会被再次提交，从而在同一个位置产出重复件。也可能含本会话主动删除的
        # 对象，需结合现场判断（自动化摆放场景下通常没有手动删除）。
        before = st.get("registry_before")
        try:
            ui = _get_edit_ui(room)
            now = set() if ui is None else set(
                int(u) for u in (getattr(ui, "all_ugc_uid_set", set()) or set()))
        except Exception:
            now = None
        if before is not None and now is not None:
            dropped = sorted(set(before) - now)
            out["registry_before"] = len(before)
            out["registry_after"] = len(now)
            out["dropped_count"] = len(dropped)
            out["dropped_uids"] = dropped[:200]
            if dropped:
                out["note"] += (
                    "｜⚠️ 有 %d 个对象在保存后离开了编辑器登记集合：多为此前已落位但"
                    "**未进入本次提交**（被服务端回滚）。它们仍留在组件表里、下次保存会被"
                    "再次提交 → 同位置重复件。请核对 dropped_uids，必要时重新登记或重摆。"
                    % len(dropped))
        return out
    if elapsed >= timeout:
        st["active"] = None
        return {"ok": True, "status": "timeout", "request_id": rid,
                "elapsed": elapsed, "persistence": "unknown",
                "note": "超时未收到回执（服务端可能较慢或未被接受）。可稍后再调或重试。"}
    return {"ok": True, "status": "pending", "request_id": rid,
            "elapsed": elapsed, "persistence": "unknown",
            "note": "保存仍在途，请稍后再 poll。"}


def op_save_dirty(a):
    """诊断：读编辑器当前的『未保存改动缓冲』。

    change_units（ui）与 save_component_info（cm）只在改动集被服务端受理并处理
    后才会被清空。因此：移动/编辑后它们应非空 === 脏；fuxiang_save_map 提交后
    若它们被清空 === 改动已上送服务端（持久化信号，不依赖退出重进）。
    """
    room = get_room()
    if room is None:
        return {"error": "拿不到 room"}
    ui = _get_edit_ui(room)
    cm = get_component_manager()
    out = {"ok": True}
    try:
        cu = getattr(ui, "ugc_params", {}).get("change_units") if ui is not None else None
        out["change_units_is_dict"] = isinstance(cu, dict)
        if isinstance(cu, dict):
            out["change_units_length"] = sum(len(v) for v in cu.values())
            out["change_units_per_key"] = {k: len(v) for k, v in cu.items()}
        else:
            out["change_units_length"] = len(cu) if cu is not None else None
        if a.get("detail") and isinstance(cu, dict):
            out["change_units_detail"] = _safe({k: v[:6] for k, v in cu.items()})
    except Exception as e:
        out["change_units_err"] = "%s: %s" % (type(e).__name__, e)
    try:
        if cm is not None:
            sc = getattr(cm, "save_component_info", None)
            out["save_events_length"] = len(sc) if sc is not None else None
            out["is_save_component_info"] = bool(getattr(cm, "is_save_component_info", False))
    except Exception as e:
        out["save_err"] = "%s: %s" % (type(e).__name__, e)

    # 保存基线比对（2026-09-19）：save_map_poll 的 ack 常收不到（实测 60s 无回执），
    # 而 save_dirty 是「会话内确认已上送」的推荐手段、调用方每次保存后必然调用它 ——
    # 故把「上次保存前的登记集合 vs 当前登记集合」的差异挂在这里，比只挂在 ack 分支可靠。
    # 差异里的对象多为此前已落位但**未进入该次提交**（被服务端回滚），它们仍留在组件表里、
    # 下次保存会被再次提交 → 同位置重复件；也可能含本会话主动删除的对象，需结合现场判断。
    st = vars(room).get("_fuxiang_save_obs") or {}
    before = st.get("registry_before")
    if before is not None and ui is not None:
        try:
            now = set(int(u) for u in (getattr(ui, "all_ugc_uid_set", set()) or set()))
            dropped = sorted(set(before) - now)
            out["registry_before"] = len(before)
            out["registry_after"] = len(now)
            out["dropped_count"] = len(dropped)
            out["dropped_uids"] = dropped[:200]
            if dropped:
                out["warning"] = (
                    "有 %d 个对象在上次保存后离开了编辑器登记集合：多为此前已落位但"
                    "**未进入该次提交**（被服务端回滚）。它们仍留在组件表里、下次保存会被"
                    "再次提交 → 同位置重复件。请核对 dropped_uids 后重摆或重新登记。"
                    % len(dropped))
        except Exception as e:
            out["registry_cmp_err"] = "%s: %s" % (type(e).__name__, e)
    return out


# 主线程黑名单：调用即冻结整个游戏进程（引擎在运行期不安全或会持 GIL 死等）。
# 2026-09-16 实测：get_vertex_bounding() 在主线程调用后 20+ 分钟不返回，
# 心跳线程随之饿死，dwrg.exe 整体卡死（引擎文档自述"效率比较低，
# 不适合游戏进行时使用"）。恢复需重启游戏。
# 2026-09-22：名单抽到 bridge/fuxiang_eval_guard.py，与桥接层 op_eval 共用同一真源
# （此前桥层只有 3 条，直连 eval 可绕过 get_submesh_bounding_*）。
from fuxiang_eval_guard import EVAL_BLOCK_PATTERNS, find_blocked  # noqa: E402


def _get_GK():
    """取游戏全局门面 GK（`GK.DM.*` 是注册表/色板的唯一入口）。

    推导式很偏门（借 `create_unit_with_info` 的函数全局），三条建模师会话都在这上面
    绊过（2026-09-19 实机 `NameError: name 'GK' is not defined`），因此与 core/cm/em
    一起预置。不在编辑器上下文时返回 None（此时应先查 fuxiang_status）。
    """
    try:
        room = get_core().room
        return room.create_unit_with_info.__globals__["GK"]
    except Exception:
        return None


def eval_presets():
    """eval 通道的预置名，两条通道（edit 层 op_eval 与桥层 raw eval）共用一份。

    写在这里而不是各通道自己挑：2026-09-19~20 连续三条建模师会话都在"以为有预置、
    实际没有"上中断过（taggeddict / cm / GK / FE）。`FS`/`FE` 尤其必要——原生三步法
    与白名单扩展的范例代码都写 `FS.get_edit_ui(FE)`，缺了它照抄范例必炸。
    """
    ns = {"core": get_core(), "room": get_room(), "cm": get_component_manager(),
          "em": get_event_manager(),
          "GK": _get_GK(), "jdump": _jdump, "plain": _safe,
          "FE": sys.modules[__name__]}
    try:
        import fuxiang_scene as _fs
        ns["FS"] = _fs
    except Exception:
        pass
    return ns


def op_eval(a):
    """逃生舱：在编辑器上下文里执行任意 Python。"""
    code = a.get("code", "")
    for pattern, reason in EVAL_BLOCK_PATTERNS:
        if pattern in code:
            return {"error": "已拦截危险调用 %r：%s。"
                             "如需包围盒请用 model.bounding_size / bounding_center，"
                             "或摆好后用截图目测。" % (pattern, reason)}
    ns = {"__name__": "fuxiang_edit_eval", "__result__": None}
    ns.update(eval_presets())
    try:
        exec(compile(code, "<fuxiang-edit>", "exec"), ns, ns)
    except Exception:
        tb = traceback.format_exc()
        if "not JSON serializable" in tb:
            return {"error":
                "序列化失败：引擎注册表/组件数据里的值不是 JSON 原生类型"
                "（常见为 taggeddict，它不继承 dict，json.dumps 接不住）。"
                "改用本 op 预置的 jdump(obj) 或 plain(obj)："
                "__result__ = jdump({...})。"
                "不要用 json.dumps(..., default=str)——它不报错但会把嵌套结构压成 "
                "repr 字符串，读回来的值是坏的。原始栈:\n" + tb[-2000:]}
        return {"error": tb[-2500:]}
    return {"result": _safe(ns.get("__result__"))}


def op_event_list(a):
    """列出场景全部事件（注册表+引用解析+保存口径）。"""
    return _edit_call("op_event_list", a)


def op_event_create(a):
    """新建事件（原生命令层）。args: name"""
    return _edit_call("op_event_create", a)


def op_event_delete(a):
    """删除事件（清引用+注册表移除）。args: event_key"""
    return _edit_call("op_event_delete", a)


def op_event_bind(a):
    """把事件绑定到物体的事件类参数槽。args: ugc_id, event_key, property_id|index, params_idx"""
    return _edit_call("op_event_bind", a)


def op_event_unbind(a):
    """解除事件绑定。args: ugc_id, event_key"""
    return _edit_call("op_event_unbind", a)


def op_motion_preview(a):
    """把对象绑定到运动器(203911)并开启/停止运动驱动（可同时持久化绑定）。"""
    return _edit_call("op_motion_preview", a)


# --------------------------------------------------------------------------
# 剧情编辑（STORY 10007001 / DIALOGUE 10007002 / CHOICE 10007003）
# --------------------------------------------------------------------------
def _story_call(name, args):
    story = smart_reload("fuxiang_story")
    return getattr(story, name)(sys.modules[__name__], args)


def op_story_fields(a):
    """剧情字段字典：语义名/property_id/允许值（写文件前先查它）。"""
    return _story_call("op_story_fields", a)


def op_story_tree(a):
    """当前地图的剧情树（剧情→对话→选项，只读）。compact=true 只看概要。"""
    return _story_call("op_story_tree", a)


def op_story_create(a):
    """发起创建一个剧情节点（异步）。args: kind=story|dialogue|choice, fields{...}"""
    return _story_call("op_story_create", a)


def op_story_create_poll(a):
    """查询创建请求；placed 后自动落 fields。args: request_id"""
    return _story_call("op_story_create_poll", a)


def op_story_set(a):
    """改一个剧情节点的字段。args: ugc_id, fields{...}, dry_run"""
    return _story_call("op_story_set", a)


def op_story_delete(a):
    """删除剧情节点。args: ugc_id|ugc_ids, cascade, dry_run"""
    return _story_call("op_story_delete", a)


def op_story_export(a):
    """导出剧情到文件。args: path（可省）, format=json|md"""
    return _story_call("op_story_export", a)


def op_story_export_doc(a):
    """导出剧情但**不落盘**：内容以 JSON 字符串回传（给快照用）。

    ⛔ 不能直接回传 doc 字典：回传层 `_safe` 深度 >4 会变 `<deep>`，而
    doc 是 stories→dialogues→choices→fields 四层嵌套，最里层必被截断。
    故在游戏侧先 `json.dumps` 压平成字符串再回传（与组件表同一技巧）。
    """
    return _story_call("op_story_export_doc", a)


def op_story_import(a):
    """剧情文件 → 变更计划（dry_run 默认 true）/ 应用。args: path|data|text, prune"""
    return _story_call("op_story_import", a)


def op_story_import_next(a):
    """推进导入计划里的下一个创建（begin→placed→落字段→下一个）。"""
    return _story_call("op_story_import_next", a)


# --------------------------------------------------------------------------
# 界面编辑器（UI 编辑）：控件的增删改与事件绑定
# 模型与原生链路见 bridge/fuxiang_ui_edit.py 头注释，用法见 UI_EDIT.md
# --------------------------------------------------------------------------
def _ui_edit_call(name, args):
    ui_edit = smart_reload("fuxiang_ui_edit")
    return getattr(ui_edit, name)(sys.modules[__name__], args)


def op_ui_state(a):
    """界面编辑器定位与概览（视图是否打开 / 控件计数 / 画布尺寸 / 事件表）。"""
    return _ui_edit_call("op_ui_state", a)


def op_ui_parts(a):
    """界面编辑器各子 part 的定位（19 个面板的类名 / 分工 / 方法）。"""
    return _ui_edit_call("op_ui_parts", a)


def op_ui_catalog(a):
    """界面编辑器可放置的控件目录（组件类型 200000 的全部物件）。"""
    return _ui_edit_call("op_ui_catalog", a)


def op_ui_schema(a):
    """控件字段字典：别名 / property_id / 事件类型 / 应用方法 / 面板文案 / 值形态。"""
    return _ui_edit_call("op_ui_schema", a)


def op_ui_list(a):
    """地图上已有控件清单（include_native 附带 UI_NATIVE 概要）。"""
    return _ui_edit_call("op_ui_list", a)


def op_ui_get(a):
    """单个控件的属性袋 / 事件绑定 / 登记状态 / 保存报文形态。"""
    return _ui_edit_call("op_ui_get", a)


def op_ui_add(a):
    """新增控件（服务端建件；确认走原生 on_check_ui_component_limit）。args: item_id|item|name, pos[x,y]"""
    return _ui_edit_call("op_ui_add", a)


def op_ui_add_poll(a):
    """查询 ui_add 的结果，直到 status=placed。args: request_id"""
    return _ui_edit_call("op_ui_add_poll", a)


def op_ui_set(a):
    """改控件属性（pos/width/height/alpha/visible/name/content/字体/颜色…）。args: ugc_id, props"""
    return _ui_edit_call("op_ui_set", a)


def op_ui_move(a):
    """把控件挪到画布 2D 坐标。args: ugc_id, pos [x, y]"""
    return _ui_edit_call("op_ui_move", a)


def op_ui_bind(a):
    """给控件绑事件（on_click/on_release/on_long_press/on_show/on_hide）。args: ugc_id, slot, event_key"""
    return _ui_edit_call("op_ui_bind", a)


def op_ui_unbind(a):
    """解绑控件上的事件。args: ugc_id, event_key"""
    return _ui_edit_call("op_ui_unbind", a)


def op_ui_delete(a):
    """删除控件（原生删除命令；默认 dry_run）。args: ugc_id|ugc_ids, dry_run=false 才真删"""
    return _ui_edit_call("op_ui_delete", a)


def op_ui_verify(a):
    """落库前自检：是否进报文 / 事件引用双向一致 / 待删除标记。"""
    return _ui_edit_call("op_ui_verify", a)


# ---------------------------------------------------------------------------
# 地图封面（cover）：原生链路见 bridge/fuxiang_cover.py 头注释，用法见 COVER.md
# ---------------------------------------------------------------------------
def _cover_call(name, args):
    cover = smart_reload("fuxiang_cover")
    return getattr(cover, name)(sys.modules[__name__], args)


def op_cover_state(a):
    """封面实况：槽位（相对 doc_dir 的图片路径 + md5）/ 服务器回读封面 / 发布面板状态。"""
    return _cover_call("op_cover_state", a)


def op_cover_set_slot(a):
    """把 doc_dir 下的一张图写成封面槽（等价原生「确认」）。args: path, index=0, dry_run"""
    return _cover_call("op_cover_set_slot", a)


def op_cover_upload(a):
    """按发布流程的原生写法上传封面图（不发布地图）。args: index=0, dry_run"""
    return _cover_call("op_cover_upload", a)


def op_cover_upload_poll(a):
    """查封面上传回执（share_url）。args: index, since"""
    return _cover_call("op_cover_upload_poll", a)


def op_cover_apply(a):
    """落库：set_ugc_map_cover_photo(ugc_key, [url…])。args: urls（省略则用上传回执）, dry_run"""
    return _cover_call("op_cover_apply", a)


OPS = {
    "status": op_status,
    "map_info": op_map_info,
    "list_types": op_list_types,
    "list_components": op_list_components,
    "get_component": op_get_component,
    "material_info": op_material_info,
    "create_component": op_create_component,
    "remove_component": op_remove_component,
    "repair_index": op_repair_index,
    "list_items": op_list_items,
    "plan_item": op_plan_item,
    "place_item": op_place_item,
    "place_poll": op_place_poll,
    "place_batch": op_place_batch,
    "place_poll_batch": op_place_poll_batch,
    "recover_pending": op_recover_pending,
    "remove_item": op_remove_item,
    "set_transform": op_set_transform,
    "pending_saves": op_pending_saves,
    "get_params": op_get_params,
    "set_params": op_set_params,
    "list_events": op_list_events,
    "create_event": op_create_event,
    "save": op_save,
    "save_map": op_save_map,
    "save_map_poll": op_save_map_poll,
    "save_dirty": op_save_dirty,
    "eval": op_eval,
    # ---- 全量注册表（组件 + 物体 的统一寻址）----
    "catalog_summary": op_catalog_summary,
    "catalog_types": op_catalog_types,
    "catalog_type": op_catalog_type,
    "catalog_find": op_catalog_find,
    "catalog_schema": op_catalog_schema,
    "catalog_enums": op_catalog_enums,
    "catalog_categories": op_catalog_categories,
    "catalog_export": op_catalog_export,
    "catalog_items": op_catalog_items,
    "catalog_verify": op_catalog_verify,
    # ---- 资源大厅（组合库，只读）----
    "hall_info": op_hall_info,
    "hall_items": op_hall_items,
    "hall_item": op_hall_item,
    "hall_verify": op_hall_verify,
    "hall_ui": op_hall_ui,
    "hall_cards": op_hall_cards,
    "hall_export": op_hall_export,
    # ---- 可编辑物体的数值修改（变换 / 属性）----
    "edit_targets": op_edit_targets,
    "edit_target": op_edit_target,
    "edit_transform": op_edit_transform,
    "edit_props": op_edit_props,
    "set_component_info": op_set_component_info,
    "set_component_info_batch": op_set_component_info_batch,
    "set_map_setting": op_set_map_setting,
    "get_map_bgm": op_get_map_bgm,
    "set_map_bgm": op_set_map_bgm,
    "global_singletons": op_global_singletons,
    "edit_history": op_edit_history,
    "motion_preview": op_motion_preview,
    "event_list": op_event_list,
    "event_create": op_event_create,
    "event_delete": op_event_delete,
    "event_bind": op_event_bind,
    "event_unbind": op_event_unbind,
    # ---- 预览渲染（复用引擎帧循环出图）----
    "preview_plan": op_preview_plan,
    "preview_pose": op_preview_pose,
    "preview_fire": op_preview_fire,
    "preview_restore": op_preview_restore,
    "preview_state": op_preview_state,
    "preview_layout": op_preview_layout,
    # ---- 组合下载（资源大厅 → 账号库；写操作，单条 + 轮询）----
    "combo_download": op_combo_download,
    "combo_download_poll": op_combo_download_poll,
    "combo_download_status": op_combo_download_status,
    # ---- 从场景删除对象（原生命令层）----
    "delete_preview": op_delete_preview,
    "delete_objects": op_delete_objects,

    "story_fields": op_story_fields,
    "story_tree": op_story_tree,
    "story_create": op_story_create,
    "story_create_poll": op_story_create_poll,
    "story_set": op_story_set,
    "story_delete": op_story_delete,
    "story_export": op_story_export,
    "story_export_doc": op_story_export_doc,
    "story_import": op_story_import,
    "story_import_next": op_story_import_next,

    # ---- 界面编辑器（UI 编辑）：控件的增删改与事件绑定，详见 UI_EDIT.md ----
    "ui_state": op_ui_state,
    "ui_parts": op_ui_parts,
    "ui_catalog": op_ui_catalog,
    "ui_schema": op_ui_schema,
    "ui_list": op_ui_list,
    "ui_get": op_ui_get,
    "ui_add": op_ui_add,
    "ui_add_poll": op_ui_add_poll,
    "ui_set": op_ui_set,
    "ui_move": op_ui_move,
    "ui_bind": op_ui_bind,
    "ui_unbind": op_ui_unbind,
    "ui_delete": op_ui_delete,
    "ui_verify": op_ui_verify,

    # ---- 地图封面（cover）：槽位/上传/落库，详见 COVER.md ----
    "cover_state": op_cover_state,
    "cover_set_slot": op_cover_set_slot,
    "cover_upload": op_cover_upload,
    "cover_upload_poll": op_cover_upload_poll,
    "cover_apply": op_cover_apply,
}


_BUSY_OP = {"op": None, "since": 0.0}


def dispatch(payload):
    """payload: JSON 字符串，形如 {"op": "...", "args": {...}}"""
    try:
        req = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except Exception as e:
        return {"ok": False, "error": "payload 不是合法 JSON: %s" % e}
    if not isinstance(req, dict) or not isinstance(req.get("args", {}), dict):
        return {"ok": False, "error": "payload 和 args 必须为对象"}
    op = req.get("op")
    args = req.get("args", {})
    if op not in OPS:
        return {"ok": False, "error": "unknown op %r, 可用: %s" % (op, sorted(OPS))}
    _BUSY_OP["op"] = op
    _BUSY_OP["since"] = time.time()
    try:
        value = OPS[op](args)
        if isinstance(value, dict) and "error" in value:
            return {"ok": False, "error": value["error"], "details": value}
        return {"ok": True, "value": value}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-3000:]}
    finally:
        _BUSY_OP["op"] = None
