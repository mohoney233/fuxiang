# -*- coding: utf-8 -*-
"""随笔浮想编辑器：可编辑物体的**数值修改**层（位置 / 旋转 / 缩放 / 属性）。

定位（实机逆向 + 复核）
------------------------
放置流程动的是「新建物件」；本模块动的是**已存在物件**的数值，路径完全不同：

变换（位置/旋转/缩放）
  unit.set_ugc_size(...) / set_ugc_rot(...) / set_position(...) / set_ugc_position(...)
  → 渲染、碰撞、逻辑位置三处同时更新；再 ``edit.reg_change(ui, unit, comp)`` 进保存变更集。

属性（组件属性袋 ``component_info['events']``）
  ``UGCBaseComponent.component_info`` = ``{'component_id': ..., 'events': [{'type': <property_id>, 'info': [...]}, ...]}``
  - ``update_component_info(events)``            **写属性袋要用这个**：一次完成
    ``self.component_info['events'] = events`` → ``_owner.apply_ugc_info()`` → ``apply_components_client(events)``
  - ``init_ugc_info(ugc_info, only_save=True)``  把整个 component_info **换成新容器**后立即 return
    （UGCBaseComponent.py:452），不跑 apply_ugc_info、也不跑 apply_components_client
  - ⚠️ 两者的差别**不是**「能不能落库」，判据见 ``_get_ugc_info`` 的实现：单元级 params 里的
    param 对象**就是**主组件袋里的那批 dict（同一对象）。⇒ 组件**独占**一个 unit 时两种写法
    都会进报文；组件与兄弟**共享** unit 时（剧情 1000073 / 对话 1000074 / 选项 1000075 /
    UI_NATIVE 1000061 这类一个单元挂 N 个组件）镜像只有主组件那份，``init_ugc_info`` 改了
    自己的袋而单元参数表不动 —— 保存上送的就是旧值，重进即失（2026-09-21 剧情那次即此因）。
    另外 init 会把原生容器换成普通 dict，所以统一用 update_component_info。
  - **落库真正缺一不可的是** ``edit.reg_change(ui, unit, comp)``（= 原生
    ``ui._add_change_unit(unit.uid)`` + 把**本组件**补进 ``change_units['component']``）。
    共用单元（运动器/剧情/对话/选项）时只调原生那句会登记到"当前"那个组件上，改的组件白改。
  - ``apply_components_client(events)``          把属性袋应用到引擎（原生应用器）
  - ``set_property(name, value)``                写内部属性并触发 ``client_property_change_cb_map`` 回调
  （``do_last_update(info)`` 就是 ``apply_components_client(info['events'])`` +
  对 ``info['keys']`` 逐个 ``set_property``；服务器增量下发走的是同一套。）

属性 id 的语义
  ``events[i]['type']`` 是 **property_id**（如 10100101001）。
  组件类的 ``ITEM_PARAM_2_EVENT_TYPE`` 把它映射成 event_type（如 13），
  ``EVENT_TYPE_2_METHOD_NAME`` 再把 event_type 映射成应用方法（如 ``_init_camp``）。
  属性定义在 ``GK.DM.ugc_item_property[component_id]``，层次为
  ``mode_id -> class_id -> {item_id: property_def}``，
  ``property_def`` 含 ``property_id / property_type / property_params / tab_id`` 等。

安全护栏
--------
1. 只允许改**编辑器已登记**的物件（在 ``cm._ugc_id2component`` 且 unit 存活，
   且在 ``ui.all_ugc_uid_set`` 中）。
2. 拒绝待删除物件（``room.del_unit_uids['unit']``）。
3. 属性写入前校验 ``property_id`` 确实属于该组件类型的 schema，未知名直接拒绝。
4. 写入后**回读校验**；不一致则**自动回滚**并把结果如实返回。
5. ``dry_run=true`` 只算不写。
6. 每次改动登记 ``edit.reg_change(ui, unit, comp)``，让游戏自身的保存流程落库
   （共用单元时它会把本组件补进 change_units，只调原生那句会登记错组件；
   地图保存仍是服务端权威，本模块不绕过）。
7. 审计：最多 100 条改动记录挂在 room 上，可用 ``fuxiang_edit_history`` 回看。

本模块**不碰**摆放/删除逻辑（``fuxiang_scene`` / ``fuxiang_placement``）。
"""
import importlib
import math
import re
import time

HISTORY_LIMIT = 100


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def _room(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != "WorldBattleUGCEdit":
        raise ValueError("当前不在随笔浮想编辑器")
    return room


def _cm(edit):
    cm = getattr(_room(edit), "ugc_component_manager", None)
    if cm is None:
        raise ValueError("组件管理器不可用")
    return cm


def _ui(edit):
    try:
        scene = importlib.reload(importlib.import_module("fuxiang_scene"))
        ui = scene.get_edit_ui(edit)
    except Exception:
        ui = None
    if ui is None:
        raise ValueError("编辑器 UI 不可用，无法登记保存变更集")
    return ui


def _dm(edit):
    room = _room(edit)
    return room.create_unit_with_info.__globals__["GK"].DM


def _vec(value, name, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s 必须是三个有限数值 [x,y,z]" % name)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) or (positive and v <= 0) for v in value):
        raise ValueError("%s 包含无效数值" % name)
    return [float(v) for v in value]


def _unit_of(comp):
    return getattr(comp, "_owner", None)


def supports_transform(unit):
    """该实体是否支持 UGC 变换接口。

    只有普通 UGC 物件实体（如 ``ClientUnits.*.UgcComUnit``）带
    ``ugc_dir`` / ``ugc_size`` / ``set_ugc_position``；角色类（``MyCivilianUnit`` 等）
    走的是另一套变换，不能按本接口改，需明确拒绝而不是报错。
    """
    return all(hasattr(unit, a) for a in
               ("ugc_dir", "ugc_size", "set_ugc_rot", "set_ugc_position", "set_ugc_size"))


def _try(fn, *a):
    try:
        v = fn(*a) if callable(fn) else fn
    except Exception:
        return None
    return v


def _xyz(v):
    if v is None:
        return None
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except Exception:
        return None


def _transform(unit):
    """尽力读取变换；实体不支持的部分返回 None（不抛错）。"""
    pos = _xyz(_try(getattr(unit, "get_position3", None)) or _try(getattr(unit, "position", None)))
    rot = _xyz(_try(getattr(unit, "ugc_dir", None)))
    scl = _xyz(_try(getattr(unit, "ugc_size", None)))
    return {"position": pos, "rotation": rot, "scale": scl}


def _history(room, record=None):
    buf = vars(room).setdefault("_fuxiang_edit_history", [])
    if record is None:
        return buf
    record["ts"] = time.time()
    buf.append(record)
    if len(buf) > HISTORY_LIMIT:
        del buf[:len(buf) - HISTORY_LIMIT]
    return buf


# ---------------------------------------------------------------------------
# 定位：找出可编辑物件
# ---------------------------------------------------------------------------
def find_target(edit, args):
    """把 ugc_id / owner_uid / item_id / position 解析成 (comp, unit, why_not)。"""
    cm = _cm(edit)
    room = _room(edit)
    ui = None
    try:
        ui = _ui(edit)
    except Exception:
        ui = None
    id2c = getattr(cm, "_ugc_id2component", None) or {}

    ugc_id = args.get("ugc_id")
    owner_uid = args.get("owner_uid")
    item_id = args.get("item_id")
    if ugc_id is not None:
        key = None
        for k in id2c:
            if str(k) == str(ugc_id):
                key = k
                break
        if key is None:
            return None, None, "组件索引中没有 ugc_id=%s" % ugc_id
        comp = id2c[key]
    elif owner_uid is not None:
        comp = None
        for c in id2c.values():
            if str(getattr(c, "_owner_uid", None)) == str(owner_uid):
                comp = c
                break
        if comp is None:
            return None, None, "组件索引中没有 owner_uid=%s" % owner_uid
    else:
        # 按 item_id + 位置取最近的一个
        target_pos = args.get("near_position")
        best, best_d = None, None
        for c in id2c.values():
            u = _unit_of(c)
            if u is None:
                continue
            if item_id is not None and str(getattr(u, "ugc_type", None)) != str(item_id):
                continue
            if target_pos is None:
                best = c
                break
            try:
                d = sum((float(a) - float(b)) ** 2
                        for a, b in zip(_transform(u)["position"], target_pos))
            except Exception:
                continue
            if best_d is None or d < best_d:
                best, best_d = c, d
        if best is None:
            return None, None, "没有匹配 item_id=%s 的物件" % item_id
        comp = best
        args["_auto_pick"] = True

    unit = _unit_of(comp)
    if unit is None:
        return None, None, "组件没有挂载实体 unit"
    if room.unit_mgr.get_unit(getattr(unit, "uid", None)) is not unit:
        return None, None, "实体已不在场景中（unit_mgr 查不到）"
    deleted = room.del_unit_uids.get("unit", []) if hasattr(room, "del_unit_uids") else []
    if ui is not None:
        # ⚠️ 登记集合（ui.all_ugc_uid_set）只覆盖"编辑器摆放出来"的物件；**骨架自带**的
        # 全局单例（天空盒/雾/LUT/胜负条件…）不在里面，于是对它们写属性会被挡下 ——
        # 2026-09-23 跨图还原的 --sync-global 就是这样 6 处全失败（报"不在编辑器登记集合内"）。
        # 这类对象仍是**存活**的（上面 unit_mgr 已查得到），写属性走
        # update_component_info + edit.reg_change 会把它补进 change_units ⇒ 安全。
        # 因此给显式调用方留 allow_unregistered=true 的出口（默认仍然拦）。
        if not args.get("allow_unregistered"):
            if getattr(unit, "uid", None) not in getattr(ui, "all_ugc_uid_set", set()):
                return None, None, ("物件不在编辑器登记集合内（可能尚未完成放置确认）；"
                                    "骨架自带的全局单例请显式传 allow_unregistered=true")
        if getattr(unit, "uid", None) in deleted:
            return None, None, "物件处于待删除状态，请先在游戏内处理"
    return comp, unit, None


def describe_target(edit, comp, unit):
    cm = _cm(edit)
    t2str = {str(k): v for k, v in (getattr(cm, "_component_type2str", None) or {}).items()}
    ct = getattr(comp, "COMPONENT_TYPE", None)
    ci = getattr(comp, "component_info", None)
    events = []
    cid = None
    if hasattr(ci, "get"):
        cid = ci.get("component_id")
        for e in (ci.get("events") or []):
            if hasattr(e, "get"):
                events.append({"property_id": e.get("type"), "info": _plain(e.get("info"))})
    return {
        "ugc_id": getattr(comp, "_ugc_id", None),
        "owner_uid": getattr(unit, "uid", None),
        "component_type": ct,
        "component_name": t2str.get(str(ct)),
        "component_class": type(comp).__name__,
        "item_id": getattr(unit, "ugc_type", None),
        "model": getattr(getattr(unit, "model", None), "path", None),
        "transform": _transform(unit),
        "component_id": cid,
        "property_count": len(events),
        "properties": events,
    }


def _plain(v, depth=0):
    if depth > 4:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x, depth + 1) for x in list(v)[:40]]
    if hasattr(v, "keys"):
        return {str(k): _plain(v[k], depth + 1) for k in list(v.keys())[:40]}
    return repr(v)[:80]


def _property_copy(v):
    """Lossless copy for persisted values; _plain is display-only and truncates."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_property_copy(x) for x in v]
    if hasattr(v, "keys"):
        return {k: _property_copy(v[k]) for k in v.keys()}
    raise TypeError("Unsupported property value: %s" % type(v).__name__)


# ---------------------------------------------------------------------------
# 属性 schema
# ---------------------------------------------------------------------------
def property_schema(edit, component_type):
    """把 ugc_item_property[component_type] 展平成 property_id -> 定义。"""
    dm = _dm(edit)
    root = None
    for k in (component_type, str(component_type)):
        try:
            if k in dm.ugc_item_property:
                root = dm.ugc_item_property[k]
                break
        except Exception:
            continue
    out = {}
    if root is None:
        return out
    stack = [(root, None, None)]
    seen = 0
    while stack and seen < 20000:
        cur, mode, cls = stack.pop()
        seen += 1
        if not hasattr(cur, "keys"):
            continue
        pid = None
        try:
            pid = cur.get("property_id")
        except Exception:
            pid = None
        if pid is not None:
            out[str(pid)] = {
                "property_id": str(pid),
                "property_type": _plain(cur.get("property_type")),
                "property_params": _plain(cur.get("property_params")),
                "class_id": cur.get("class_id"),
                "tab_id": cur.get("tab_id"),
                "mode": cur.get("id"),
                "sort": cur.get("property_sort"),
                "default_show": cur.get("show_default"),
                "simple_hide": cur.get("simple_hide"),
                "extra_params_1": cur.get("extra_params_1"),
            }
        for k in cur.keys():
            child = cur.get(k)
            if hasattr(child, "keys"):
                stack.append((child, cur.get("id"), cur.get("class_id")))
    return out


def event_type_maps(comp):
    """属性 id -> event_type -> 应用方法名。

    注意：映射值可能是标量，也可能是 list（不同组件类定义不一致），
    因此查表前必须先确认可哈希，不能直接拿来当 key。
    """
    p2e = {}
    e2m = {}
    for cls in type(comp).__mro__:
        for a, tgt in (("ITEM_PARAM_2_EVENT_TYPE", p2e), ("EVENT_TYPE_2_METHOD_NAME", e2m)):
            d = getattr(cls, a, None)
            if isinstance(d, dict):
                for k, v in d.items():
                    tgt.setdefault(str(k), v)
    return p2e, e2m


def _hashable(v):
    return v if isinstance(v, (int, float, str, bool, type(None))) else None


def _map_get(mapping, key):
    """安全查表：key 不可哈希时返回 None，而不是抛异常。"""
    k = _hashable(key)
    if k is None:
        return None
    try:
        return mapping.get(k)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# op：查询
# ---------------------------------------------------------------------------
def op_edit_targets(edit, args):
    """列出可编辑物件（可按 component_type / item_id 过滤）。"""
    cm = _cm(edit)
    room = _room(edit)
    ui = None
    try:
        ui = _ui(edit)
    except Exception:
        ui = None
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    ct_filter = args.get("component_type")
    ct_filter = int(ct_filter) if ct_filter is not None else None
    item_filter = args.get("item_id")
    item_filter = int(item_filter) if item_filter is not None else None
    limit = args.get("limit")
    limit = 200 if limit is None else int(limit)
    offset = int(args.get("offset") or 0)

    rows, blocked = [], []
    for k, comp in sorted(id2c.items(), key=lambda x: str(x[0])):
        ct = getattr(comp, "COMPONENT_TYPE", None)
        unit = _unit_of(comp)
        it = getattr(unit, "ugc_type", None) if unit is not None else None
        if ct_filter is not None and ct != ct_filter:
            continue
        if item_filter is not None and str(it) != str(item_filter):
            continue
        if unit is None:
            blocked.append({"ugc_id": getattr(comp, "_ugc_id", None), "reason": "无实体"})
            continue
        reasons = []
        if room.unit_mgr.get_unit(getattr(unit, "uid", None)) is not unit:
            reasons.append("实体已不在场景")
        deleted = room.del_unit_uids.get("unit", []) if hasattr(room, "del_unit_uids") else []
        if getattr(unit, "uid", None) in deleted:
            reasons.append("待删除")
        if ui is not None and getattr(unit, "uid", None) not in getattr(ui, "all_ugc_uid_set", set()):
            reasons.append("未在编辑器登记集合")
        if not supports_transform(unit):
            reasons.append("实体类型 %s 不支持 UGC 变换接口（如角色类）" % type(unit).__name__)
        row = {"ugc_id": getattr(comp, "_ugc_id", None),
               "owner_uid": getattr(unit, "uid", None),
               "component_type": ct,
               "component_class": type(comp).__name__,
               "item_id": it,
               "editable": not reasons,
               "blocked_by": reasons or None,
               "transform": _transform(unit)}
        if reasons:
            blocked.append({"ugc_id": row["ugc_id"], "reasons": reasons})
        rows.append(row)
    total = len(rows)
    page = rows if limit == 0 else rows[offset:offset + limit]
    return {"total": total, "editable": sum(1 for r in rows if r["editable"]),
            "blocked": len(blocked), "blocked_sample": blocked[:10],
            "offset": offset, "returned": len(page), "targets": page,
            "note": "editable=false 的物件不能改；用 fuxiang_edit_target 看具体原因。"}


def op_edit_target(edit, args):
    """定位单个可编辑物件：变换 + 属性袋 + 属性 schema 联表。"""
    comp, unit, why = find_target(edit, dict(args))
    if comp is None:
        return {"found": False, "reason": why}
    out = {"found": True, "target": describe_target(edit, comp, unit)}
    if args.get("with_schema"):
        ct = getattr(comp, "COMPONENT_TYPE", None)
        schema = property_schema(edit, ct)
        p2e, e2m = event_type_maps(comp)
        cur = {str(p["property_id"]): p["info"] for p in out["target"]["properties"]}
        rows = []
        for pid, sc in sorted(schema.items()):
            et_raw = _map_get(p2e, pid)
            et = _hashable(et_raw)
            rows.append({
                "property_id": pid,
                "current_value": cur.get(pid),
                "event_type": et,
                "event_type_raw": _plain(et_raw) if et is None else None,
                "apply_method": _plain(_map_get(e2m, et)),
                "property_type": sc.get("property_type"),
                "property_params": sc.get("property_params"),
                "class_id": sc.get("class_id"),
                "tab_id": sc.get("tab_id"),
                "extra_params_1": sc.get("extra_params_1"),
                "is_set": pid in cur,
            })
        out["schema"] = {"component_type": ct, "property_count": len(rows),
                         "set_count": sum(1 for r in rows if r["is_set"]),
                         "properties": rows,
                         "property_types": _property_type_table()}
    return out


def _property_type_table():
    """property_type 编码的含义。

    依据：schema 里的 property_params / extra_params_1 与属性面板的
    UGCPropertyItem* 类型类对应关系归纳而来，**不是官方文档**。
    属性面板同族类：UGCPropertyItemSwitch / Checkbox / Coordinate(Pos/Revolve/Scale/Size)
    / Camp / Class / Event / Material / Color / Filter / HandheldSelect …
    """
    return {
        "30001": "开关/枚举（property_params 形如 {TID,默认值}）",
        "50001": "枚举选择（property_params 给出选项集合，extra_params_1 带引擎联动提示）",
        "20011": "数值/档位（property_params 形如 {TID,min,max,步进}）",
        "20001": "数值（property_params 形如 {TID,个数,min,max,上限}）",
        "70001": "引用选择（property_params 形如 {序号,上下文组件id}，如阵营/角色/道具）",
        "210001": "数值输入（见 ugc_control_panel）",
        "10003": "引用类（指向其它组件或物件）",
        "-1/-2/-3/-4": "内建变换类属性（POS / REVOLVE / SCALE / SIZE，见 ComuKeys.UGC_BASE_EVENT_TYPE）",
    }


# ---------------------------------------------------------------------------
# op：改变换
# ---------------------------------------------------------------------------
def op_edit_transform(edit, args):
    """修改**已存在**物件的 position / rotation / scale，并登记保存变更集。"""
    comp, unit, why = find_target(edit, dict(args))
    if comp is None:
        return {"ok": False, "error": why}
    if not supports_transform(unit):
        return {"ok": False, "error": "实体类型 %s 不支持 UGC 变换接口（如角色类）；"
                                      "只能改属性，不能改变换" % type(unit).__name__,
                "ugc_id": getattr(comp, "_ugc_id", None)}
    keys = ("position", "rotation", "scale")
    if not any(k in args for k in keys):
        raise ValueError("至少提供 position / rotation / scale 中一项")

    before = _transform(unit)
    wanted = dict(before)
    for k in keys:
        if k not in args:
            continue
        wanted[k] = _vec(args[k], k, positive=(k == "scale"))
    dry = bool(args.get("dry_run"))
    if dry:
        return {"dry_run": True, "ugc_id": getattr(comp, "_ugc_id", None),
                "owner_uid": getattr(unit, "uid", None),
                "before": before, "would_set": wanted}

    # persist_via_recreate：就地 edit_transform 不会持久化（服务端读实时位置同步，
    # 而 API 只改本地 → 重载被旧值拉回）。改用「删除原对象 + 按目标变换重摆」的
    # add_unit 管线（已验证可持久化位置）。代价：新对象 ugc_id 会变化。
    if args.get("persist_via_recreate"):
        if getattr(unit, "_fuxiang_local_item", None):
            return {"ok": False, "error": "persist_via_recreate 只用于服务端对象；"
                                          "本地预览请先 fuxiang_place_item(mode=local_preview) 或改就地模式",
                    "ugc_id": getattr(comp, "_ugc_id", None)}
        item_id = getattr(unit, "ugc_type", None) or getattr(comp, "component_id", None)
        if item_id is None:
            return {"ok": False, "error": "取不到 item_id，无法重摆"}
        old_ugc_id = getattr(comp, "_ugc_id", None)
        req_id = "recreate_%d" % int(time.time() * 1000)
        del_res = edit._del_call("op_del_objects", {"ugc_id": old_ugc_id})
        place_res = edit._scene_call("place", {
            "item_id": item_id, "position": wanted["position"],
            "rotation": wanted["rotation"], "scale": wanted["scale"],
            "mode": "server", "request_id": req_id})
        return {"ok": True, "mode": "recreate",
                "old_ugc_id": old_ugc_id, "item_id": item_id,
                "target_transform": wanted,
                "delete_result": del_res, "place_result": place_res,
                "request_id": req_id,
                "note": "已删除原对象并发出重摆请求（add_unit 管线，可持久化）。"
                        "用 fuxiang_place_poll(request_id=%s) 轮询新对象就位并确认位置。" % req_id}

    scene = importlib.reload(importlib.import_module("fuxiang_scene"))
    ui = _ui(edit)
    room = _room(edit)

    def apply(values):
        scene._apply_full_transform(unit, {"position": values["position"],
                                          "rotation": values["rotation"],
                                          "scale": values["scale"]})

    try:
        apply(wanted)
        after = _transform(unit)
        for k in keys:
            for got, want in zip(after[k], wanted[k]):
                delta = (got - want + 180) % 360 - 180 if k == "rotation" else got - want
                if abs(delta) > 0.001:
                    raise RuntimeError("变换回读不一致: %s" % k)
        registered = False
        try:
            edit.reg_change(ui, unit, comp)
            registered = True
        except Exception as exc:
            raise RuntimeError("已改值但登记保存变更集失败: %s" % exc)
        _history(room, {"kind": "transform", "ugc_id": getattr(comp, "_ugc_id", None),
                        "owner_uid": unit.uid, "before": before, "after": after})
        return {"ok": True, "ugc_id": getattr(comp, "_ugc_id", None),
                "owner_uid": unit.uid, "before": before, "after": after,
                "registered_in_change_set": registered,
                "scope": "server" if getattr(unit, "_fuxiang_local_item", None) else "existing_object",
                "persistence_verified": False,
                "note": "已进入编辑器保存变更集；请按正常流程在游戏内保存，"
                        "持久化需保存后重进核实。"}
    except Exception as exc:
        rollback = {}
        try:
            apply(before)
            restored = _transform(unit)
            rollback = {"complete": all(
                abs((g - b + 180) % 360 - 180) < 0.001 if k == "rotation" else abs(g - b) < 0.001
                for k in keys for g, b in zip(restored[k], before[k])),
                "restored": restored}
        except Exception as rexc:
            rollback = {"complete": False, "error": str(rexc)}
        return {"ok": False, "error": str(exc), "rollback": rollback,
                "ugc_id": getattr(comp, "_ugc_id", None)}


# ---------------------------------------------------------------------------
# op：改属性
# ---------------------------------------------------------------------------
def op_edit_props(edit, args):
    """修改物件的组件属性袋（component_info['events']）。

    ``props`` 形如 ``{"10100101001": ["TID_BATTLE_UGC_TEAM_9001"]}`` —— 键为
    property_id，值为要写入的值（标量会按原值结构自动包成 list）。
    """
    comp, unit, why = find_target(edit, dict(args))
    if comp is None:
        return {"ok": False, "error": why}
    props = args.get("props") or {}
    remove = set(str(x) for x in (args.get("remove") or []))
    if not props and not remove:
        raise ValueError("需要 props（要写入的属性）或 remove（要清除的属性 id）至少其一")
    if props and not isinstance(props, dict):
        raise ValueError("props 必须是对象 {property_id: value}")

    ct = getattr(comp, "COMPONENT_TYPE", None)
    schema = property_schema(edit, ct)
    unknown = [p for p in list(props) + list(remove) if str(p) not in schema]
    if unknown and not args.get("allow_unknown_property"):
        return {"ok": False, "error": "属性不在该组件类型的 schema 中: %s；"
                                      "确要写入请传 allow_unknown_property=true" % unknown,
                "component_type": ct, "schema_property_count": len(schema)}
    if not schema:
        return {"ok": False, "error": "该组件类型没有属性 schema（component_type=%s），"
                                      "只能改变换" % ct}

    ci = getattr(comp, "component_info", None)
    if not hasattr(ci, "get"):
        return {"ok": False, "error": "组件没有 component_info，无法改属性"}
    old_info = {"component_id": ci.get("component_id"),
                "events": [_property_copy(e) for e in (ci.get("events") or [])]}

    # 组装新 events：按 property_id 覆盖/追加，保持原有顺序
    new_events = []
    touched = set()
    for e in (ci.get("events") or []):
        if not hasattr(e, "get"):
            continue
        pid = str(e.get("type"))
        if pid in remove:
            touched.add(pid)
            continue
        if pid in props:
            new_events.append({"type": e.get("type"), "info": _as_info(props[pid], e.get("info"))})
            touched.add(pid)
        else:
            new_events.append({"type": e.get("type"), "info": _property_copy(e.get("info"))})
    existing = {str(e.get("type")) for e in (ci.get("events") or []) if hasattr(e, "get")}
    for pid, value in props.items():
        if str(pid) in existing:
            continue
        new_events.append({"type": _as_type(pid), "info": _as_info(value, None)})
        touched.add(str(pid))

    dry = bool(args.get("dry_run"))
    if dry:
        return {"dry_run": True, "ugc_id": getattr(comp, "_ugc_id", None),
                "before_events": old_info["events"], "would_write": new_events,
                "touched": sorted(touched), "component_type": ct}

    new_info = {"component_id": ci.get("component_id"), "events": new_events}
    room = _room(edit)
    ui = _ui(edit)
    logs = []
    try:
        # 1) 原生更新器：组件 events 赋值 + _owner.apply_ugc_info()（单元级同步，
        #    保存通道读单元级 ugc_info）+ 引擎实时应用。
        #    ⚠️ 2026-09-21 修正：原先 init_ugc_info(only_save=True) 只改组件内存、
        #    不同步 owner 单元，保存落库的一直是服务端旧数据（剧情内容重进即失的根因）。
        comp.update_component_info(new_events)
        logs.append("update_component_info 已写入属性袋并同步 owner 单元")
        # 2) update_component_info 内部已调 apply_components_client，无需重复应用
        synced = []
        for key in (args.get("sync_keys") or []):
            try:
                comp.set_property(key, getattr(comp, key))
                synced.append(key)
            except Exception as exc:
                logs.append("set_property(%s) 失败: %s" % (key, exc))
        # 4) 回读校验
        ci2 = getattr(comp, "component_info", None) or {}
        got = {}
        for e in (ci2.get("events") or []):
            if hasattr(e, "get"):
                got[str(e.get("type"))] = _property_copy(e.get("info"))
        mismatched = []
        for pid in touched:
            if pid in remove:
                if pid in got:
                    mismatched.append({"property_id": pid, "expected": None,
                                       "got": got.get(pid)})
                continue
            expect = props.get(pid)
            if str(pid) in props and _property_copy(_as_info(expect, None)) != got.get(pid):
                mismatched.append({"property_id": pid,
                                   "expected": _plain(_as_info(expect, None)),
                                   "got": got.get(pid)})
        if mismatched:
            raise RuntimeError("属性回读不一致: %s" % mismatched[:5])
        # 5) 登记本组件进保存变更集（共用单元时原生只登记"当前"那个组件，见 reg_change）
        edit.reg_change(ui, unit, comp)
        logs.append("已登记 reg_change(本组件)")
        _history(room, {"kind": "props", "ugc_id": getattr(comp, "_ugc_id", None),
                        "owner_uid": unit.uid, "touched": sorted(touched),
                        "before_events": old_info["events"], "after_events": new_events})
        return {"ok": True, "ugc_id": getattr(comp, "_ugc_id", None),
                "owner_uid": unit.uid, "component_type": ct,
                "touched": sorted(touched), "synced_keys": synced,
                "before_events": old_info["events"], "after_events": new_events,
                "steps": logs, "persistence_verified": False,
                "note": "属性袋已写入并应用到引擎；请按正常流程在游戏内保存。"}
    except Exception as exc:
        rollback = {"complete": False}
        try:
            comp.update_component_info(old_info["events"])
            restored = getattr(comp, "component_info", None) or {}
            rollback = {"complete": True,
                        "restored_events": [_plain(e) for e in (restored.get("events") or [])]}
        except Exception as rexc:
            rollback = {"complete": False, "error": "%s: %s" % (type(rexc).__name__, rexc)}
        return {"ok": False, "error": str(exc), "steps": logs, "rollback": rollback,
                "ugc_id": getattr(comp, "_ugc_id", None)}


# 组件属性里**存放 ugc_id 引用**的字段。重建地图后 ugc_id 全部重新分配，
# 这些字段必须跟着重映射，否则物件会绑到一个不存在（或绑错）的运动器上。
#   *18701  —— 物件 / 组合件 → 运动器
#   20390003001 —— COMPOSITE 的成员表（整袋都是 ugc_id，春岚实测 214 件共 14291 处）
# ⚠️ 只列「整袋值都是 ugc_id」的字段。运动器的 20391102002/02007/18801 存的是
#    PATH/REPEAT/计量**枚举**（1641/1653/1711，见 EDIT_OPS.md §9.1），数值上会与某些
#    ugc_id 撞号，列进来会把枚举值改写成随机 id；20391119001 的路径关键帧同理。
UGCID_REF_PROPERTIES = ("10000118701", "10000018701",
                        "20390018701", "20390003001")

# 存**剧情节点 ugc_id** 的槽（2026-10-01 实机：春岚→新图后，对话件的「所属剧情」
# 写着 117，而 117 是当时图上一件时钟件 —— 真正的剧情件是 133 ⇒ 对话接错线）。
# 这几槽的值就是 ugc_id（空/0 表示未设置，不会在 remap 里命中），所以要跟着换号：
#   1000700240001 对话 → 所属剧情        1000700340001 选项 → 所属对话
#   1000700340005 选项 → 跳转剧情        1000700340006 选项 → 跳转对话
# 定义见 bridge/fuxiang_story.py 的 STORY_FIELDS（`_init_story_ugc_id` 等）。
STORY_REF_PROPERTIES = ("1000700240001", "1000700340001",
                       "1000700340005", "1000700340006")


def _remap_info(info, remap):
    """把 info 里的旧 ugc_id 换成新 ugc_id（int / str 两种形态都认）。"""
    if not remap:
        return info
    lut = {}
    for k, v in remap.items():
        lut[str(k)] = v
        try:
            lut[int(k)] = v
        except (TypeError, ValueError):
            pass

    def _one(x):
        # 递归：info 可以是 [gid]，也可以是 [[gid]]（引擎里两种形态都出现过）。
        # ⚠️ 元素可能是 list —— 不可哈希，直接 `x in lut` 会抛 TypeError（实机踩到）。
        if isinstance(x, (list, tuple)):
            return [_one(i) for i in x]
        if isinstance(x, str) and x.isdigit() and x in lut:
            return lut[x]
        try:
            if x in lut:
                return lut[x]
        except TypeError:
            return x
        return x

    return _one(info)


def _set_component_info_core(edit, args):
    """整段回写组件的 `component_info['events']` 的**单件核心**（快照复原用）。

    与 `op_edit_props` 的差别：后者按 property_id 逐字段写并做 schema 校验；
    本接口**整袋替换**，因为快照里的 events 本来就是从同类型组件导出的，
    再过一遍 schema 只会误拒（schema 并不完整覆盖所有字段）。

    args:
      ugc_id                目标组件
      events                [{type, info}, ...]（整袋）
      expect_component_id   可选，与目标的 COMPONENT_TYPE 不符时拒绝（防误写）
      remap                 可选 {旧ugc_id: 新ugc_id}，用于运动器绑定等引用字段
      remap_properties      可选，默认 UGCID_REF_PROPERTIES
      dry_run               true 时只给 before/after，不写
    """
    comp, unit, why = find_target(edit, dict(args))
    if comp is None:
        return {"ok": False, "error": why}
    events = args.get("events")
    if not isinstance(events, list):
        raise ValueError("events 必须是 [{type, info}, ...]")
    ct = getattr(comp, "COMPONENT_TYPE", None)
    expect = args.get("expect_component_id")
    if expect is not None and str(expect) != str(ct):
        return {"ok": False, "error": "组件类型不符：期望 %s，实际 %s（ugc_id=%s）"
                                      % (expect, ct, args.get("ugc_id")),
                "ugc_id": args.get("ugc_id")}
    ci = getattr(comp, "component_info", None)
    if not hasattr(ci, "get"):
        return {"ok": False, "error": "组件没有 component_info，无法回写"}

    old_events = [_property_copy(e) for e in (ci.get("events") or [])]
    remap = args.get("remap") or {}
    remap_props = set(str(x) for x in (args.get("remap_properties")
                                       or UGCID_REF_PROPERTIES))
    remapped = []
    new_events = []
    for e in events:
        if not isinstance(e, dict):
            continue
        pid = str(e.get("type"))
        info = e.get("info")
        if remap and pid in remap_props:
            info2 = _remap_info(info, remap)
            # ⚠️ 判定必须无损：_plain 只留 list 前 40 项，组合件成员表（数百个 id）
            #    在 40 项之后发生的重映射会被当成"没变"，remapped 就漏报了。
            if _property_copy(info2) != _property_copy(info):
                remapped.append(pid)
            info = info2
        new_events.append({"type": _as_type(e.get("type")), "info": info})

    dry = bool(args.get("dry_run"))
    if dry:
        return {"dry_run": True, "ugc_id": args.get("ugc_id"), "component_type": ct,
                "before_events": [_plain(e) for e in old_events],
                "would_write": _plain(new_events),
                "remapped_properties": remapped,
                "counts": {"before": len(old_events), "after": len(new_events)}}

    room = _room(edit)
    ui = _ui(edit)
    logs = []
    try:
        # 1) 原生更新器（含 _owner.apply_ugc_info() 单元级同步 —— 保存通道读单元级数据）
        comp.update_component_info(new_events)
        logs.append("update_component_info 已整袋写入并同步 owner 单元")
        # 2) 回读校验：type 集合必须齐全（info 的数值形态可能被引擎规范化，
        #    只对缺失/多余做硬校验，值差异仅报告，避免误回滚）
        ci2 = getattr(comp, "component_info", None) or {}
        got = {}
        for e in (ci2.get("events") or []):
            if hasattr(e, "get"):
                # ⚠️ 必须无损回读：_plain 截到 40 项 / 深度 4，运动器 3700 点路径
                #    写成 40 点也会"校验通过"（值差异看不出来 = 静默丢数据）。
                got[str(e.get("type"))] = _property_copy(e.get("info"))
        want = {str(e.get("type")) for e in new_events}
        missing = sorted(want - set(got))
        extra = sorted(set(got) - want)
        if missing:
            raise RuntimeError("回读缺失字段: %s" % missing[:5])
        written = {str(e.get("type")): e.get("info") for e in new_events}
        value_diff = sorted(p for p in want & set(got)
                            if got.get(p) != _property_copy(written.get(p)))
        # 3) 登记进保存变更集
        edit.reg_change(ui, unit, comp)
        logs.append("已登记 reg_change(本组件)")
        _history(room, {"kind": "set_component_info",
                        "ugc_id": getattr(comp, "_ugc_id", None),
                        "owner_uid": unit.uid,
                        # 历史挂在 room 上（HISTORY_LIMIT 条、每条不限长）：整袋长路径
                        # 存进来，一批还原下来会把游戏进程内存顶上去，这里只存显示态。
                        "before_events": [_plain(e) for e in old_events],
                        "after_events": _plain(new_events)})
        return {"ok": True, "ugc_id": getattr(comp, "_ugc_id", None),
                "owner_uid": unit.uid, "component_type": ct,
                "counts": {"before": len(old_events), "after": len(new_events)},
                "remapped_properties": remapped,
                "extra_properties": extra, "value_diff_properties": value_diff[:10],
                "steps": logs, "persistence_verified": False,
                "note": "属性袋已整袋写入并应用到引擎；请按正常流程在游戏内保存。"}
    except Exception as exc:
        rollback = {"complete": False}
        try:
            comp.update_component_info(old_events)
            rollback = {"complete": True}
        except Exception as rexc:
            rollback = {"complete": False, "error": "%s: %s" % (type(rexc).__name__, rexc)}
        return {"ok": False, "error": str(exc), "steps": logs, "rollback": rollback,
                "ugc_id": getattr(comp, "_ugc_id", None)}


def op_set_component_info(edit, args):
    """单件整袋回写（快照复原用）；核心在 _set_component_info_core。"""
    return _set_component_info_core(edit, args)


# 「物件不在编辑器登记集合内」是 allow_unregistered 升级重试的唯一触发串
# （find_target 的护栏文案；全局单元类组件必须走这条出口，2026-10-01 实机）。
_ESCALATE_MARKER = "不在编辑器登记集合"


def op_set_component_info_batch(edit, args):
    """批量整袋回写（快照复原 props 阶段提速）：一条命令循环写 N 袋。

    args: {'items': [{ugc_id, events, expect_component_id, remap,
                      remap_properties, dry_run, allow_unregistered}, ...]}
    每件复用 _set_component_info_core 的**同一条**路径与护栏（find_target 寻址、
    expect 类型校验、回读校验、失败回滚、reg_change、历史）。
    护栏 8：`allow_unregistered` 升级重试**逐件**保留——只对错误串命中
    「不在编辑器登记集合」且调用方没显式放行的那一件，静默补一次
    allow_unregistered=true；其余错误原样逐件返回，绝不整批连坐。
    返回字典**不带**顶层 "error" 键（dispatch 契约：带了会被当整批失败）；
    逐件失败在 results[i]['error'] 里。
    """
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items 必须是非空数组")
    if len(items) > 200:
        raise ValueError("单批最多 200 件（回传截断上限）；请调小 --props-batch")
    results = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": i, "ok": False, "error": "item 必须是对象"})
            continue
        r = _set_component_info_core(edit, item)
        err = str(r.get("error") or "")
        if not r.get("ok") and not r.get("dry_run") \
                and _ESCALATE_MARKER in err and not item.get("allow_unregistered"):
            r2 = _set_component_info_core(edit, dict(item, allow_unregistered=True))
            if r2.get("ok") or r2.get("dry_run"):
                r2["escalated"] = "allow_unregistered"   # 让调用方看得见这次放宽
            r = r2
        row = {"index": i, "ugc_id": item.get("ugc_id"),
               "ok": bool(r.get("ok")), "dry_run": bool(r.get("dry_run"))}
        if r.get("ok") or r.get("dry_run"):
            for k in ("component_type", "remapped_properties", "escalated"):
                if r.get(k):
                    row[k] = r.get(k)
        else:
            row["error"] = str(r.get("error") or "")[:200]
        results.append(row)
    n_ok = sum(1 for r in results if r.get("ok") or r.get("dry_run"))
    return {"ok": True, "results": results,
            "counts": {"total": len(items), "ok": n_ok,
                       "failed": len(items) - n_ok},
            "persistence_verified": False,
            "note": "批量整袋回写完成，逐件回执在 results；持久化仍须 save_map。"}


def _as_info(value, sample):
    """把用户给的值规整成 info：原值是 list 就包成 list，否则按标量。"""
    if isinstance(sample, (list, tuple)) or (sample is None and isinstance(value, (list, tuple))):
        return list(value) if isinstance(value, (list, tuple)) else [value]
    return value


# ---------------------------------------------------------------------------
# 地图级设置（跨图复刻用）
# ---------------------------------------------------------------------------
def _map_setting_part(edit):
    core = getattr(edit, "core", None)
    if core is None:
        core = getattr(edit, "get_core", None)
        core = core() if callable(core) else core
    if core is None:
        return None, "拿不到 UGCEditCore"
    try:
        from common_cs.ugc_edit_core.parts.map_setting.iUGCEditMapSettingPart import (
            iUGCEditMapSettingPart)
    except Exception as exc:
        return None, "导入失败: %s: %s" % (type(exc).__name__, exc)
    p = core.get_part(iUGCEditMapSettingPart)
    if p is None:
        return None, "地图设置 part 不存在"
    return p, None


def op_set_map_setting(edit, args):
    """写地图设置（跨图还原场景框架用）。

    实测（2026-09-23）：`set_map_setting_data(key, value, source='') -> bool`，
    写入返回 True、幂等、不产生脏缓冲。key "14" = 场景框架（下拉 4001/4002/4003）。

    args:
      key      设置键（字符串，如 "14"）
      value    目标值（字符串）
      source   可选，默认 ''（透传给原生）
      dry_run  true 时只报 before/after 与是否相同，不写
    """
    p, why = _map_setting_part(edit)
    if p is None:
        return {"ok": False, "error": why}
    key = args.get("key")
    if key is None:
        return {"ok": False, "error": "缺少 key"}
    key = str(key)
    before = {}
    try:
        d = p.get_map_setting_data() or {}
        before = {k: _plain(v) for k, v in d.items()} if hasattr(d, "items") else {}
    except Exception as exc:
        return {"ok": False, "error": "读设置失败: %s: %s" % (type(exc).__name__, exc)}

    allowed = None
    try:
        cfg = _dm(edit).ugc_setting_config or {}
        c = cfg.get(int(key)) if str(key).lstrip("-").isdigit() else None
        if c and c.get("item_params"):
            allowed = [x for x in str(c["item_params"]).split(",") if x]
    except Exception:
        allowed = None

    value = args.get("value")
    value = "" if value is None else str(value)
    if allowed and value not in allowed:
        return {"ok": False, "error": "值 %r 不在候选 %s 内（key=%s）" % (value, allowed, key),
                "before": before.get(key), "allowed": allowed}

    if before.get(key) == value:
        return {"ok": True, "changed": False, "key": key, "value": value,
                "before": before.get(key), "allowed": allowed,
                "note": "目标值与当前一致，无需写入（幂等）"}

    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "changed": True, "key": key,
                "before": before.get(key), "after": value, "allowed": allowed}

    src = args.get("source") or ""
    try:
        r = p.set_map_setting_data(key, value, src)
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "key": key, "before": before.get(key)}
    after = None
    try:
        after = (p.get_map_setting_data() or {}).get(key)
    except Exception:
        pass
    return {"ok": bool(r) and str(after) == value, "changed": True, "key": key,
            "before": before.get(key), "after": after, "native_return": bool(r),
            "allowed": allowed,
            "note": "框架类改动可能需要重进地图才完全生效；改动后请正常保存。"}


def _bgm_props_key():
    from common_cs import ComuKeys                # ComuKeys 是**模块**（不是类）；与引擎同一张表
    return ComuKeys.UGC_PROPS.UGC_BGM_CONF, ComuKeys.BgmConf


def op_get_map_bgm(edit, args):
    """读地图默认背景音乐（`ugc_props_info['ugc_bgm_conf']`）。

    2026-10-01：这一层既不在组件袋也不在 map_setting 里，快照以前根本不采，
    于是"七层全绿"能在一张开局静音的图上印绿灯。写入见 `op_set_map_bgm`。
    """
    room = _room(edit)
    if room is None:
        return {"ok": False, "error": "拿不到 room（编辑器没打开地图？）"}
    props = getattr(room, "ugc_props_info", None) or {}
    try:
        key, _BgmConf = _bgm_props_key()
    except Exception as exc:
        return {"ok": False, "error": "导入 ComuKeys 失败: %s: %s" % (type(exc).__name__, exc)}
    conf = props.get(key)
    return {"ok": True, "key": str(key), "conf": _plain(conf),
            "present": key in props,
            "note": "conf=null ⇒ 这张图没设过默认背景音乐（与「这层没采到」是两回事）"}


def op_set_map_bgm(edit, args):
    """写地图默认背景音乐 —— 走编辑器面板自己的那条通道，不发明新载荷。

    2026-10-01 从**开着的面板**（更多 → 网易云音乐 → 背景音乐设置，类 `UGCUICloudMusic`）
    反解出 `apply_ugc_bgm_conf(mode, param)` 做两件事：
      1) `room.ugc_props_info[ComuKeys.UGC_PROPS.UGC_BGM_CONF] = {'mode','param'}`
      2) `GK.g_unit.send_battle_event(ComuKeys.GAME_BATTLE_EVENT_TYPE.UGC,
                                      {'key': 'bgm_conf', 'bgm_conf': 同一个 dict})`
    所以这里**直接调面板实例的那个方法**（厂商路径，将来它改了我们也跟着对）；
    面板没实例化时先 `show_ui` 把它实例化再调。
    ⚠️ 2026-10-01 bisect（小图 2026.9.16，全部重进后判）：只发 battle event 的回退分支
    连着**非空变更集**保存也不落库（重进读回旧值），真面板方法 + 非空保存两次都落库
    ⇒ 面板方法是必要的。回退分支只留给显式 `allow_unpersisted_fallback` 的调用方；
    默认拿不到真面板就拒写 —— 宁可不写也不假成功。两条路都不碰 `save_ugc_map_data` ——
    那条是"整条地图记录"级别的写入，载荷语义（增量还是整体覆盖）没证清之前不动它。

    args:
      mode        1=默认 2=静音 3=网易云（也接受 "default"/"mute"/"cloud"）
      param       云音乐时 {'id': <songId>, 'name': <曲名>}；mode=1/2 可省
      dry_run     true 只报 before/after 不写
      panel_wait  面板未实例化时 show_ui 后的等待秒数（默认 1.5，上限 5；等待在
                  主线程上，游戏会冻结这一小段——实测首次创建 ≤1s，1.5 足够）
    """
    room = _room(edit)
    if room is None:
        return {"ok": False, "error": "拿不到 room（编辑器没打开地图？）"}
    try:
        key, BgmConf = _bgm_props_key()
    except Exception as exc:
        return {"ok": False, "error": "导入 ComuKeys 失败: %s: %s" % (type(exc).__name__, exc)}
    modes = {"default": BgmConf.Default, "mute": BgmConf.Mute,
             "cloud": BgmConf.CloudMusic, "cloudmusic": BgmConf.CloudMusic}
    mode = args.get("mode")
    if isinstance(mode, str) and mode.strip().lower() in modes:
        mode = modes[mode.strip().lower()]
    valid = {int(BgmConf.Default), int(BgmConf.Mute), int(BgmConf.CloudMusic)}
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return {"ok": False, "error": "mode 要 1/2/3（或 default/mute/cloud），收到 %r" % (mode,)}
    if mode not in valid:
        return {"ok": False, "error": "mode=%s 不在 %s 内" % (mode, sorted(valid))}
    param = args.get("param")
    param = {} if param is None else dict(param)
    if mode == int(BgmConf.CloudMusic) and not param.get("id"):
        return {"ok": False, "error": "云音乐模式必须给 param.id（网易云 songId）",
                "hint": "快照里 map.props.ugc_bgm_conf.value.param 就是现成的值"}

    props = getattr(room, "ugc_props_info", None)
    if props is None:
        return {"ok": False, "error": "room.ugc_props_info 不存在"}
    before = _plain(props.get(key))
    conf = {"mode": mode, "param": param}
    if before == conf:
        return {"ok": True, "changed": False, "conf": conf,
                "note": "目标值与当前一致，无需写入（幂等）"}
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "changed": True, "before": before, "conf": conf}

    via, err = None, None
    from common_cs import ComuKeys
    GK = None
    try:
        GK = room.create_unit_with_info.__globals__["GK"]      # 引擎自己的引用路子（见 _dm）
    except Exception:
        GK = None
    panel = None
    um = getattr(GK, "g_ui_mgr", None) if GK is not None else None
    if um is not None and callable(getattr(um, "get_ui", None)):
        try:
            panel = um.get_ui("UGCUICloudMusic")
        except Exception:
            panel = None
    # ⚠️ 切图/面板从没开过时 get_ui 回的是 **Swallow 哨兵**，而它：callable 为真、
    # `__self__ is panel` 也为真（照这两条判"面板可用"会调出一个静默空操作 ——
    # 2026-10-01 实机就这么"写成功"过一次，回读却还是 null）。判据要看类型名。
    pname = "%s.%s" % (type(panel).__module__, type(panel).__name__) if panel is not None else ""
    if panel is not None and "swallow" in pname.lower():
        # 2026-10-01 bisect（小图 2026.9.16，重进后判）：回退分支 send_battle_event +
        # **非空变更集**保存也不落库（重进读回旧值），真面板方法 + 非空保存两次都落库
        # ⇒ 面板方法是必要的，这里必须先 show_ui 把它实例化，不能再静默退化成回退分支。
        # 2026-10-02 实机（2026.9.16）：show_ui 是**异步**建 UI，发完立即 get_ui 必是
        # 哨兵（昨天两张还原图上 --set-bgm 就是这么失败）；首次创建实测 ≤1s。
        # 这里轮询等待 panel_wait 秒（默认 1.5s）。⚠️ 本 op 在主线程上执行，等待期间
        # 游戏会冻结——所以上限压到秒级，且只此一次；仍拿不到就拒写（宁可不写不假成功）。
        panel_wait = 1.5
        try:
            panel_wait = min(5.0, max(0.0, float(args.get("panel_wait", 1.5) or 1.5)))
        except (TypeError, ValueError):
            panel_wait = 1.5
        try:
            import time as _time
            um.show_ui("UGCUICloudMusic")
            deadline = _time.time() + panel_wait
            while True:
                panel = um.get_ui("UGCUICloudMusic")
                pname = "%s.%s" % (type(panel).__module__, type(panel).__name__) \
                    if panel is not None else ""
                if panel is None or "swallow" not in pname.lower():
                    break
                if _time.time() >= deadline:
                    err = ("show_ui 后等了 %.1fs 仍是哨兵（%s）⇒ 面板没起来，拒写"
                           % (panel_wait, pname))
                    break
                _time.sleep(0.15)
        except Exception as exc:
            err = "show_ui(UGCUICloudMusic): %s: %s" % (type(exc).__name__, exc)
    if panel is not None and "swallow" not in pname.lower():
        try:
            panel.apply_ugc_bgm_conf(mode, param)
            via = "UGCUICloudMusic.apply_ugc_bgm_conf（面板原方法）"
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
    elif via is None and args.get("allow_unpersisted_fallback"):
        # 仅限明确要求「只管本会话生效、不落库」的调用方；默认拒绝 ——
        # 回退分支写出来的 conf 实测不进保存载荷（见上面 bisect 记录）。
        try:
            g_unit = getattr(GK, "g_unit", None) if GK is not None else None
            if g_unit is None:
                raise RuntimeError("面板没开过、又拿不到 g_unit ⇒ 发不出 bgm_conf 事件")
            g_unit.send_battle_event(ComuKeys.GAME_BATTLE_EVENT_TYPE.UGC,
                                      {"key": "bgm_conf", "bgm_conf": conf})
            props[key] = conf
            via = "send_battle_event(UGC,{'key':'bgm_conf'})（回退分支；实测不落库）"
        except Exception as exc:
            err = (err + "; " if err else "") + "%s: %s" % (type(exc).__name__, exc)

    after = _plain(props.get(key)) if props is not None else None
    if via is None:
        return {"ok": False, "changed": False, "via": None,
                "before": before, "conf": after, "error": err or (
                    "拿不到真面板（UGCUICloudMusic）⇒ 拒写：回退分支实测不落库，"
                    "请人工开一次「更多 → 网易云音乐」面板后重试，或检查 show_ui"),
                "note": "2026-10-01 bisect：落库必须走真面板方法，宁可不写也不假成功"}
    # ⚠️ dispatch 的信封规则：结果字典里只要**有** "error" 键（哪怕 None）就判整体失败
    # （2026-10-02 实机踩到：写入成功却因 "error": None 被包装成 ok:false，restore 的
    # --set-bgm 会把成功误读成失败而跳过落库保存）。所以成功路径绝不携带该键。
    out = {"ok": after == conf, "changed": True, "via": via,
           "before": before, "conf": after,
           "note": "本地已写并回读一致；**落库**要等重进后由 accept --live 的「地图属性」层给结论"}
    if err:
        out["error"] = err
    return out


def _as_type(pid):
    try:
        return int(pid)
    except (TypeError, ValueError):
        return pid


def op_edit_history(edit, args):
    """查看本会话经接口做过的数值改动。"""
    room = _room(edit)
    buf = _history(room)
    limit = int(args.get("limit") or 20)
    return {"count": len(buf), "limit": limit, "entries": buf[-limit:],
            "note": "最多保留 %d 条，便于回看与复盘。" % HISTORY_LIMIT}


def op_motion_preview(edit, args):
    """把对象绑定到运动器（MOTION, 203911）并开启/关闭运动驱动。

    编码语义（2026-09-18 反汇编+实机验证，详见 EDIT_OPS.md「运动器」一节）：
      - 运动器本体是组件 203911（挂在 UgcGlobalUnit 全局逻辑单元上，无实体模型）；
      - 路径点在运动器属性 20391119001（_set_point_info）：
        条目 = [到达时刻(累计秒), [x,y,z], [dir_x,dir_y,dir_z], [sx,sy,sz]]；
        绑定时引擎会把对象当前位置作为 0 时刻首点自动追加；
      - move_time（20391102006）= 全程总时长；_type 1641=PATH / 1642=ROTATION；
        _path_type 1651=SCROLL；_move_type 1711=TIME / 1712=COUNT；
      - 对象侧属性 10000118701（et14）= [运动器 ugc_id 列表]，是持久化载体；
      - 编辑模式下由 preview_motion_component(True, info) 注册 unit tick；
        runtime begin_time 需显式 begin()。

    args:
      ugc_id:         被驱动对象
      motion_ugc_ids: [运动器组件 ugc_id...]（component_id 必须=203911）
      flag:           true=绑定+注册 tick+开始；false=停止并清空（默认 true）
      motion_chilren: 随动子对象 ugc_id 列表（可选，默认 []）
      move_time:      可选，覆盖每个运动器总时长（秒）
      point_info:     可选，覆盖路径点（格式见上）
      persist:        可选，true 时把绑定写入对象属性袋 10000118701（走 op_edit_props 原生管线）
    """
    import traceback

    room = _room(edit)
    cm = _cm(edit)
    ugc_id = args.get("ugc_id")
    motion_ids = args.get("motion_ugc_ids") or []
    flag = bool(args.get("flag", True))

    comp = cm._ugc_id2component.get(ugc_id)
    if not comp:
        return {"ok": False, "error": "对象 %r 不在组件索引中" % (ugc_id,)}
    unit = _unit_of(comp)
    if not unit:
        return {"ok": False, "error": "对象 %r 没有存活实体" % (ugc_id,)}

    mcomps = []
    for mid in motion_ids:
        mc = cm._ugc_id2component.get(mid)
        if not mc:
            return {"ok": False, "error": "运动器 %r 不在组件索引中" % (mid,)}
        if (getattr(mc, "component_info", {}) or {}).get("component_id") != 203911:
            return {"ok": False,
                    "error": "组件 %r 不是运动器(203911)" % (mid,)}
        mcomps.append(mc)

    # 可选：覆盖运动器配置（短操作，仅写内存，持久化需走属性袋+保存）
    for mc in mcomps:
        if args.get("move_time") is not None:
            mc._set_move_time(float(args["move_time"]))
        if args.get("point_info") is not None:
            mc._set_point_info([list(p) for p in args["point_info"]])

    result = {"ok": True, "ugc_id": ugc_id, "motion_ugc_ids": list(motion_ids),
              "persistence_verified": False}

    if not flag:
        try:
            unit.preview_motion_component(False, {"motions": [], "motion_chilren": []})
            result["stopped"] = True
        except Exception as exc:
            result.update(ok=False, error="停止失败: %s" % exc,
                          trace=traceback.format_exc()[-400:])
        return result

    try:
        unit.preview_motion_component(
            True, {"motions": list(motion_ids),
                   "motion_chilren": list(args.get("motion_chilren") or [])})
    except Exception as exc:
        return {"ok": False, "error": "绑定失败: %s" % exc,
                "trace": traceback.format_exc()[-500:], "ugc_id": ugc_id}

    began = []
    for m in (getattr(unit, "motions", None) or []):
        try:
            m.begin()
            began.append(round(room.game_time(), 2))
        except Exception:
            pass
    result["motions_running"] = len(getattr(unit, "motions", None) or [])
    result["begun_at"] = began[0] if began else None
    result["note"] = ("运动由引擎 tick 驱动；u.position 是逻辑位缓存，实机位置以渲染/截图为准。"
                      "要持久化绑定请传 persist=true 并正常保存地图。")

    if args.get("persist"):
        props = {"10000118701": [list(motion_ids)]}  # info[0] = 运动器 ugc_id 列表
        pr = op_edit_props(edit, {"ugc_id": ugc_id, "props": props})
        result["persist"] = {"ok": pr.get("ok"), "error": pr.get("error")}
        # 运动器侧配置（203911 挂在全局逻辑单元上，不在编辑器登记集合）：
        # 原生 update_component_info + 把全局 unit uid 登记进变更集。
        # 运动器独占一个 UgcGlobalUnit，所以旧的 init_ugc_info(info, True) 其实也能进报文
        # （2026-09-22 A/B 实测）；改用 update 是为了 ① 保留原生 component_info 容器
        # ② 顺带 apply_components_client 让配置实时生效 ③ 与共享单元组件的写法统一。
        import copy
        try:
            ui = _ui(edit)
            gsaved = []
            for mc in mcomps:
                mu = _unit_of(mc)
                if not mu:
                    continue
                info = copy.deepcopy(mc.component_info or {})
                events = info.setdefault("events", [])

                def _put(events, pid, val):
                    for e in events:
                        if e.get("type") == pid:
                            e["info"] = val
                            return
                    events.append({"type": pid, "info": val})

                if args.get("point_info") is not None:
                    # 属性袋里的 20391119001 要点列表**再包一层**：info = [[点, 点, ...]]，
                    # 每个点 = [bt, [x,y,z], [dx,dy,dz], [sx,sy,sz]]（4 元素）。
                    # 少这层外层 list 会被 UGCMotionComponent.check_value_type_valid 判非法、
                    # 换回默认值 [[]]（保存后重进路径全丢），并且 _edit_refresh_motion_point
                    # 会抛 TypeError: object of type 'float' has no len()。
                    # 运行时 comp._point_info 用的是不包层的那一层，见上面 _set_point_info。
                    _put(events, 20391119001, [[list(p) for p in args["point_info"]]])
                if args.get("move_time") is not None:
                    _put(events, 20391102006, [float(args["move_time"])])
                mc.update_component_info(events)
                # 运动器是「单元多件」：原生 _add_change_unit 只会登记该单元"当前"那个组件，
                # 走 edit.reg_change 把本组件补进 change_units['component']。
                reg_ok = edit.reg_change(ui, mu, mc)
                bag = {e.get("type"): e.get("info")
                       for e in ((mc.component_info or {}).get("events") or [])
                       if e.get("type") in (20391119001, 20391102006)}
                gsaved.append({"ugc_id": mc._ugc_id, "readback": bag,
                               "registered_in_change_units": reg_ok})
            result["persist"]["motion_config_saved"] = gsaved
        except Exception as exc:
            result["persist"]["motion_config_error"] = str(exc)[:200]
    return result


# ==================================================================
# 事件系统（2026-09-18 实证：用户手动建"你好"事件 + 前后快照 diff）
#
# 数据架构：
#   * 事件注册表 cm._ugc_params_events_key['event'][事件键] =
#       {'sort_id': n, 'info': [(ugc_id, params_index, params_idx), ...]}
#     info = 引用三元组（哪些组件参数槽位持有该事件键）；info=None 表示事件已删除
#     （get_save_event_info() 只收集 info 非空的键、按 sort_id 排序 → 保存载荷）。
#   * 绑定 = 把事件键字符串写进组件参数槽的 info[params_idx]
#     （原生入口 ui._set_event_key(事件键, [(ugc_id, index, params_idx)...])，
#      内部 _add_change_unit + update_component_info）。
#   * 槽位空值形态：info = None；绑定后如 ['你好', 0.0, 0.0, 0.0]。
#   * 已知事件类字段（实测）：10000103004 = GEOMETRY 接收事件（info[0]=键，1..3 附加参数）、
#     20200303001 = 道具箱 202003 发送事件（info[0]=键，info[1]=动作参数）。
#   * 创建 = 命令层 UGCEventAddCommand(owner, type=True, is_shared=True,
#       type_info={'event_key': 名字, 'child_key': 'event'}) → UGCGroupCommandReverse →
#       ui._do_command（进撤销栈，与 UI"新建事件"同链路）。
# ==================================================================

_EVENT_CHILD_KEY = "event"

# 单元参数表的结构不变量（2026-09-23 实机验证）：
#   params[0..2] = 变换三元组（位置/旋转/缩放；逻辑组件填 -999 占位）
#   params[3:]   = **组件自己的**事件袋（= component_info['events'] 的那批 dict）
# ⇒ 事件引用三元组 (ugc_id, index, params_idx) 里的 index 是「单元级」下标，
#   对应的**组件本地**下标 = index - PARAMS_HEADER。
# ⛔ 千万别用 `ui._get_ugc_info(unit)['params']` 去解析/写槽位：多个组件共用同一
#    unit 时（实测 4 个 CHOICE 全挂 unit 1000306、2 个 STORY 挂 unit 1000304），
#    它返回的是**主组件**的那一份袋 —— 读会读到兄弟组件的值，写会把槽位写到
#    兄弟组件身上。这是 event_list 曾把「选项1」的槽位读成「拆开信件」的根因。
PARAMS_HEADER = 3


def _event_ref_own_index(index):
    """引用里的单元级 index → 组件本地事件袋下标。"""
    return int(index) - PARAMS_HEADER


def _event_registry(edit):
    cm = _cm(edit)
    return cm._ugc_params_events_key.setdefault(_EVENT_CHILD_KEY, {})


def _dedupe_refs(refs):
    """引用去重（保持首次出现的顺序）。元素可能是 tuple 也可能是 list。"""
    seen, out = set(), []
    for r in refs or []:
        try:
            k = tuple(r)
        except TypeError:
            out.append(r)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _set_event_refs(cm, key, refs):
    """**替换**某个事件的全部引用（不是追加）。

    ⛔ `change_ugc_params_events_key(child, key, infos)` 是**追加**语义 —— 2026-09-23 实机
    实测：给它一个过滤过的短列表**不会删掉任何东西**（回读长度不变），传 `[]` 对非空列表
    也无效；只有 info 曾是 `None`（删除态）时 `[]` 才能"复活为空引用"。
    所以「解绑 / 清幽灵引用」用它等于**静默空操作**（本文件此前的 op_event_unbind 就是）。
    真正的替换接口是 `modify_ugc_params_events_key(child, key, infos)`（实测把
    `[[358,3,0],[358,6,0]]` 写成 `[[358,6,0]]`，回读即 `[[358,6,0]]`）。
    """
    refs = _dedupe_refs(refs)
    fn = getattr(cm, "modify_ugc_params_events_key", None)
    if callable(fn):
        fn(_EVENT_CHILD_KEY, key, refs)
        return {"by": "modify", "refs": refs}
    cm.change_ugc_params_events_key(_EVENT_CHILD_KEY, key, refs)
    return {"by": "change", "refs": refs}


def _event_resolve_ref(edit, ref, key=None):
    """(ugc_id, index, params_idx) → 可读引用信息。

    `property_id` / `current_value` 取自 **目标组件自身** 的事件袋；`key` 给定时
    附带 `key_ok`（该槽位里是否真的放着这个事件键）—— 这是判断引用是否陈旧/错位的
    唯一可信自检，快照导出会用它。
    """
    cm = _cm(edit)
    try:
        uid, index, params_idx = ref
    except Exception:
        return {"raw": repr(ref)[:60]}
    comp = cm._ugc_id2component.get(uid)
    entry = {"ugc_id": uid, "index": index, "params_idx": params_idx,
             "alive": bool(comp)}
    if comp:
        ci = getattr(comp, "component_info", None) or {}
        if hasattr(ci, "get"):
            entry["component_type"] = ci.get("component_id")
            own = list(ci.get("events") or [])
            oi = _event_ref_own_index(index)
            if 0 <= oi < len(own):
                e = own[oi]
                entry["own_index"] = oi
                entry["property_id"] = e.get("type") if hasattr(e, "get") else None
                info = e.get("info") if hasattr(e, "get") else None
                entry["current_value"] = repr(info)[:120]
                entry["resolved_from"] = "component_bag"
                if key is not None:
                    entry["key_ok"] = (isinstance(info, list) and key in info)
            else:
                entry["resolved_from"] = "unresolved"
                if key is not None:
                    entry["key_ok"] = False
    return entry


def _native_dm(comp):
    """取组件实现类所引用的游戏 DM（与 apply_components_client 同源，最可靠）。"""
    fn = getattr(type(comp), "apply_components_client", None)
    g = getattr(fn, "__globals__", None) or {}
    return g.get("ugc_utils"), (g.get("Netease") or g.get("GK")).DM


def event_field_shape(comp, property_id):
    """事件类字段的**权威形态**（不靠猜）。

    依据 2026-09-18 反汇编 ``UGCBaseComponent.apply_components_client``(line 1332)：
      * ``info`` 的形态 = ``ugc_utils.get_default_value_info(def)[0]`` 各段**顺序拼接**
        （源码里是 ``default_value += default_value_info``）；
      * ``property_type`` 的 ';' 段与 ``property_params`` 的 '{}' 段**一一对齐**
        （全库 383 个 8000x 字段实测 0 例外）；
      * 其中 ``80001`` 段 = 事件键槽，该段第二元素 **1=发送 / 2=接收**；
        ``80002`` 阵营、``80003`` 职业等 8000x 族成员**不是**事件键槽。

    返回 ``{property_id, label, direction, key_index, default_info, event_type, method}``。
    ``key_index`` = 事件键在 ``info`` 里的下标，**不一定是 0**（如人数检测
    ``20204111101`` 的形态是 [数量, 秒数, 事件键]）。非事件字段各字段均为 None。
    """
    try:
        pid = int(str(property_id))
    except Exception:
        pid = property_id
    out = {"property_id": pid, "label": None, "direction": None, "key_index": None,
           "default_info": None, "event_type": None, "method": None}
    try:
        one = comp._get_property_by_id(pid)
    except Exception:
        one = None
    if not one:
        return out
    ptypes = [p for p in str(one.get("property_type") or "").split(";") if p]
    groups = [g.split(",") for g in re.findall(r"\{([^}]*)\}",
                                               str(one.get("property_params") or ""))]
    key_group = None
    for i, pt in enumerate(ptypes):
        d = str(groups[i][1]).strip() if i < len(groups) and len(groups[i]) > 1 else None
        if pt == "80001" and d in ("1", "2"):
            key_group = i
            out["direction"] = "sender" if d == "1" else "receiver"
            break
    if key_group is None:
        return out
    ug, DM = _native_dm(comp)
    try:
        out["label"] = DM.all_tid_data.get(
            "TID_BATTLE_UGC_PROSETTING_TEXT_%d" % int(groups[key_group][0]))
    except Exception:
        pass
    try:
        segs = ug.get_default_value_info(one, DM.ugc_dropdown_index,
                                         DM.ugc_role_filter_table,
                                         DM.ugc_tool_filter_table)[0] or []
        flat = []
        for seg in segs:
            flat.extend(list(seg) if isinstance(seg, (list, tuple)) else [seg])
        out["default_info"] = flat
        n = 0
        for seg in segs[:key_group]:
            n += len(seg) if isinstance(seg, (list, tuple)) else 1
        out["key_index"] = n
    except Exception:
        pass
    p2e, e2m = event_type_maps(comp)
    et = _plain(p2e.get(str(pid)))
    out["event_type"] = et
    meth = e2m.get(str(et)) if et is not None else None
    out["method"] = meth if isinstance(meth, str) else (repr(meth)[:60] if meth else None)
    return out


def op_event_list(edit, args):
    """列出场景全部事件（注册表 + 引用解析 + 保存口径）。"""
    reg = _event_registry(edit)
    saved = set(_cm(edit).get_save_event_info() or [])
    events = []
    for key, entry in reg.items():
        refs = [_event_resolve_ref(edit, r, key=key) for r in (entry.get("info") or [])]
        events.append({"event_key": key, "sort_id": entry.get("sort_id"),
                       "refs": refs, "in_save": key in saved})
    events.sort(key=lambda e: (e.get("sort_id") or 0))
    bad = [{"event_key": e["event_key"], "ugc_id": r.get("ugc_id"),
            "index": r.get("index"), "why": r.get("resolved_from")}
           for e in events for r in e["refs"] if r.get("key_ok") is False]
    return {"ok": True, "count": len(events), "events": events,
            "saved_keys": sorted(saved), "bad_refs": bad,
            "note": "info=None 的事件已删除不显示；引用三元组=(ugc_id, params下标, info下标)。"
                    "property_id/current_value 取自**组件自身**事件袋（params[3+]）；"
                    "key_ok=false 表示该槽位里没有这个事件键（陈旧/错位引用，见 bad_refs）。"}


def op_event_create(edit, args):
    """新建事件（原生命令层，与 UI 新建事件同链路，进撤销栈）。"""
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "error": "事件名必须为非空字符串"}
    name = name.strip()
    reg = _event_registry(edit)
    if name in reg and reg[name].get("info") is not None:
        return {"ok": False, "error": "事件已存在: %s" % name}
    ui = _ui(edit)
    try:
        from ui.UIBattleUGCPart.UIBattleUGCEditCommand import (
            UGCEventAddCommand, UGCGroupCommandReverse)
        info = {"owner": ui, "type": True, "is_shared": True,
                "type_info": {"event_key": name, "child_key": _EVENT_CHILD_KEY}}
        cmd = UGCEventAddCommand(info)
        group = UGCGroupCommandReverse({"owner": ui, "commands": [cmd],
                                        "last_update_params": {"refresh_property_view": True}})
        ui._do_command(group)
    except Exception as exc:
        return {"ok": False, "error": "命令层创建失败: %s" % exc, "event_key": name}
    entry = reg.get(name)
    if entry is None:
        # 命令层未落到注册表时退回直写（保持可用性，注明非撤销栈路径）
        _cm(edit).change_ugc_params_events_key(_EVENT_CHILD_KEY, name, [])
        entry = reg.get(name)
    return {"ok": entry is not None, "event_key": name,
            "sort_id": entry.get("sort_id") if entry else None,
            "persistence_verified": False,
            "note": "已创建；保存地图后持久化。引用绑定用 event_bind。"}


def op_event_delete(edit, args):
    """删除事件：清空所有引用槽位（恢复 info=None）+ 注册表移除。"""
    key = args.get("event_key")
    cm = _cm(edit)
    reg = _event_registry(edit)
    entry = reg.get(key)
    if entry is None:
        return {"ok": False, "error": "事件不存在: %s" % (key,)}
    ui = _ui(edit)
    cleared = []
    for ref in (entry.get("info") or []):
        try:
            uid, index, params_idx = ref
            comp = cm._ugc_id2component.get(uid)
            unit = _unit_of(comp) if comp else None
            if not unit:
                continue
            params = ui._get_ugc_info(unit).get("params") or []
            if index < len(params):
                slot = params[index]
                # ⚠️ 只清"仍持有本事件键"的槽位；槽位若已被其它事件接管（陈旧引用），
                # 绝不能动它，否则会误删别的事件的绑定（2026-09-18 往返测试实测踩到）
                if (isinstance(slot.get("info"), list) and params_idx < len(slot["info"])
                        and slot["info"][params_idx] == key):
                    slot["info"][params_idx] = None
                    unit.ugc_component.update_component_info(params[3:])
                    ui._add_change_unit(unit.uid)
                    cleared.append(uid)
        except Exception:
            continue
    cm.change_ugc_params_events_key(_EVENT_CHILD_KEY, key, None)  # info=None → 保存口径剔除
    return {"ok": True, "event_key": key, "cleared_refs": cleared,
            "removed": reg.get(key, {}).get("info") is None,
            "persistence_verified": False,
            "note": "注册表已置删除态；保存后生效。撤销栈不含本操作（直写路径）。"}


def op_event_bind(edit, args):
    """把事件绑定到物体的事件类参数槽位（原生 _set_event_key + 注册表登记）。

    args: ugc_id, event_key, property_id(可选, 如 10000103004),
          index/own_index(可选, 直接指定槽位), extra(可选, 键在 0 位时的其余参数),
          info(可选, 整段无损写回; 事件键下标仍由字段权威形态决定)

    ⭐ 槽位定位优先级：`property_id` > `own_index` > `index`。
    property_id 是**可移植**标识；本地下标（own_index / index）是**节点相关**的：
    同一 own_index 在不同节点/不同图里可能是**完全不同的字段**（源图选项 own[5]=
    「设为可用」(…40009)，目标图 own[5]=「是否启用」(…40008) ⇒ 2026-09-23 实机把
    事件「测试」写到了 enable 上，且 slot 里恰好放着同名键，key_ok 还是 true，只有比
    property_id 才发现）。跨图还原一律给 property_id。
    """
    ugc_id = args.get("ugc_id")
    key = args.get("event_key")
    cm = _cm(edit)
    reg = _event_registry(edit)
    comp = cm._ugc_id2component.get(ugc_id)
    if not comp:
        return {"ok": False, "error": "对象 %r 不在组件索引中" % (ugc_id,)}
    if key not in reg:
        return {"ok": False, "error": "事件不存在: %r" % (key,)}
    if reg[key].get("info") is None:
        # 曾被删除(info=None)：复活为空引用事件
        cm.change_ugc_params_events_key(_EVENT_CHILD_KEY, key, [])
    unit = _unit_of(comp)
    if not unit:
        return {"ok": False, "error": "对象 %r 没有存活实体" % (ugc_id,)}
    ui = _ui(edit)

    # ⛔ 槽位一律在**目标组件自身**的事件袋里定位与写入。
    # 曾经这里读 `ui._get_ugc_info(unit)['params']`：共用单元时那是**主组件**的袋，
    # 于是把事件键写到了兄弟组件身上（4 个 CHOICE 共用 unit 1000306 即此情形）。
    ci = getattr(comp, "component_info", None)
    if not hasattr(ci, "get"):
        return {"ok": False, "error": "组件 %r 没有 component_info，无法写槽位" % (ugc_id,)}
    own = list(ci.get("events") or [])

    def _find_by_pid(pid):
        return next((i for i, p in enumerate(own)
                     if hasattr(p, "get") and str(p.get("type")) == str(pid)), None)

    own_index = None
    located_by = None
    pid = args.get("property_id")
    if pid is not None:
        # ⭐ property_id 优先（见 docstring：本地下标跨节点不可移植）
        located_by = "property_id"
        own_index = _find_by_pid(pid)
        if own_index is None:
            # 槽位不存在（新物件或曾被重置）：按事件字段形态在**本组件袋**里创建
            own.append({"type": int(pid) if str(pid).isdigit() else pid,
                        "info": None, "disable_delete": True})
            own_index = len(own) - 1
            located_by = "property_id_created"
    if own_index is None:
        own_index = args.get("own_index")
        if own_index is not None:
            located_by = "own_index"
    if own_index is None:
        index_arg = args.get("index")
        if index_arg is not None:
            own_index = _event_ref_own_index(index_arg)
            located_by = "index(unit_level)"
    if own_index is None:
        return {"ok": False,
                "error": "需要 property_id（事件类字段，如 GEOMETRY 的 10000103004）"
                         "或 own_index / index"}
    own_index = int(own_index)
    if own_index < 0:
        return {"ok": False, "ugc_id": ugc_id,
                "error": "index 小于 %d：unit params 的前 %d 项是变换三元组，"
                         "组件事段袋下标 = index - %d" % (PARAMS_HEADER, PARAMS_HEADER,
                                                          PARAMS_HEADER)}
    if own_index >= len(own):
        return {"ok": False, "ugc_id": ugc_id, "own_index": own_index,
                "error": "组件 %r 事件袋只有 %d 项，装不下下标 %d（引用陈旧："
                         "可能指向已被删除/改版过的字段）"
                         % (ugc_id, len(own), own_index)}
    index = own_index + PARAMS_HEADER
    prop = args.get("property_id") or own[own_index].get("type")
    params_idx = int(args.get("params_idx") or 0)
    # 事件键在 info 里的真实下标（权威形态来自字段定义，不再假设是 0）
    shape = event_field_shape(comp, prop)
    if args.get("params_idx") is None and shape.get("key_index") is not None:
        params_idx = int(shape["key_index"])
    ref = (ugc_id, index, params_idx)
    # 写参数槽（等价 ui._set_event_key 内部逻辑，但落在**本组件**的袋上，
    # 并处理槽位 info 为 None 的初始化——UI 流程由属性控件先建好列表）
    slot = own[own_index]
    given = args.get("info")
    if isinstance(given, list):
        # 无损写回：文件层给整段 info（事件键下标由 shape 决定，不在 0 位也成立）
        planned_info = list(given)
    elif not isinstance(slot.get("info"), list):
        extra = args.get("extra")
        if extra is not None:
            planned_info = [key] + list(extra)         # 兼容既有调用方（键在 0 位）
        elif shape.get("default_info") is not None:
            planned_info = list(shape["default_info"])  # 原生权威形态（含非 0 位键）
        else:
            planned_info = [key] + [0.0, 0.0, 0.0]      # 兜底：GEOMETRY 接收字段形态
    else:
        planned_info = list(slot["info"])
    if params_idx >= len(planned_info):
        planned_info = planned_info + [None] * (params_idx + 1 - len(planned_info))
    planned_info[params_idx] = key
    if args.get("dry_run"):
        # ⚠️ dry_run 必须**在任何写入之前**返回：这里曾直接把参数传给下游却照写不误
        # （2026-09-23 实机：界面控件的 ui_bind --dry-run 真写进了 362 的松手槽）。
        return {"ok": True, "dry_run": True, "ugc_id": ugc_id, "event_key": key,
                "property_id": prop, "index": index, "own_index": own_index,
                "params_idx": params_idx, "located_by": located_by,
                "field_shape": _plain(shape.get("default_info")),
                "would_write": planned_info,
                "would_register": list(_plain(x) for x in
                                        (reg[key].get("info") or [])) + [list(ref)],
                "note": "只算不写：would_write 是槽位 info 的未来值，would_register 是注册表引用。"}
    # 写回**本组件**的事件袋；reg_change 是落库真正缺一不可的那步（见模块头注释：
    # 共用单元时只调原生 _add_change_unit 会登记到"当前"那个组件上，改的组件白改）
    slot["info"] = planned_info
    comp.update_component_info(own)
    try:
        edit.reg_change(ui, unit, comp)
    except Exception:
        ui._add_change_unit(unit.uid)
    # 校验参数槽落地（同样从本组件袋回读，不看单元镜像）
    back = list((getattr(comp, "component_info", None) or {}).get("events") or [])
    slot = back[own_index] if own_index < len(back) else {}
    landed = (isinstance(slot.get("info"), list)
              and params_idx < len(slot["info"])
              and slot["info"][params_idx] == key)
    # 注册表登记（若 _set_event_key 未自动同步）
    refs = reg[key].get("info") or []
    if ref not in refs:
        # 用**替换**语义写（change_ 是追加，虽然这里恰好需要追加，但统一走一条路更好排错）
        _set_event_refs(cm, key, list(refs) + [ref])
        synced_by = "op"
    else:
        synced_by = "native"
    return {"ok": landed, "ugc_id": ugc_id, "event_key": key,
            "index": index, "own_index": own_index, "params_idx": params_idx,
            "located_by": located_by,
            "property_id": slot.get("type", prop),
            "field_label": _plain(shape.get("label")),
            "direction": shape.get("direction"),
            "field_shape": _plain(shape.get("default_info")),
            "slot_value": repr(slot.get("info"))[:140],
            "registry_synced_by": synced_by,
            "persistence_verified": False,
            "note": "绑定已写参数槽+注册表；保存地图后持久化。"}


def op_event_unbind(edit, args):
    """解除绑定：槽位 info 恢复 None + 注册表移除该引用。"""
    ugc_id = args.get("ugc_id")
    key = args.get("event_key")
    cm = _cm(edit)
    reg = _event_registry(edit)
    comp = cm._ugc_id2component.get(ugc_id)
    if not comp:
        return {"ok": False, "error": "对象 %r 不在组件索引中" % (ugc_id,)}
    unit = _unit_of(comp)
    ui = _ui(edit)
    # ⛔ 与 event_bind 同理：槽位在**本组件**的事件袋里（params[3:]），
    # 不能用 ui._get_ugc_info(unit)['params']——共用单元时那是主组件的袋。
    own = list((getattr(comp, "component_info", None) or {}).get("events") or [])
    removed = []
    entry = reg.get(key)
    refs = list(entry.get("info") or []) if entry else []
    keep = []
    for ref in refs:
        try:
            uid, index, params_idx = ref
        except Exception:
            continue
        if uid != ugc_id:
            keep.append(ref)
            continue
        oi = _event_ref_own_index(index)
        if unit and 0 <= oi < len(own):
            slot = own[oi]
            if isinstance(slot.get("info"), list) and params_idx < len(slot["info"]) \
                    and slot["info"][params_idx] == key:
                slot["info"][params_idx] = None
                comp.update_component_info(own)
                try:
                    edit.reg_change(ui, unit, comp)
                except Exception:
                    ui._add_change_unit(unit.uid)
                removed.append(ref)
                continue
        # 槽位已改持其它事件键（陈旧引用）：只摘注册表引用，不动槽位
        removed.append({"stale_ref_removed": ref})
    if entry is not None:
        # ⛔ 注意两种"清空"语义完全不同：
        #   `change_` + []   = 只把 **删除态(info=None)** 复活成空引用（对非空列表无效）
        #   `modify_` + keep = 真正替换（摘引用靠它；见 _set_event_refs 的实测记录）
        sync = _set_event_refs(cm, key, keep)
    else:
        sync = {"by": "none", "refs": keep}
    return {"ok": True, "ugc_id": ugc_id, "event_key": key,
            "removed_refs": removed, "remaining_refs": keep,
            "registry_written_by": sync.get("by"),
            "persistence_verified": False}
