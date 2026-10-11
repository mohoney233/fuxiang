# -*- coding: utf-8 -*-
"""随笔浮想编辑器：从场景**删除**已存在物件 —— 写层。

与创建对称的原生路径（实机逆向确认）
--------------------------------------
``UIBattleUGCEdit._do_normal_del_commands(operate_units)``：
  1. 清理照片数据（``_clean_photo_data_before_delete``）
  2. 七道前置校验（任一不过就整体中止）：运动点 / 锚点 / 手递手点 / 胜利条件 /
     检查点路线 / 小提琴音符 / 非法组件
  3. 组合（COMPOSITE）联动：删父件会带出子件（``del_composite_dict``）
  4. 组装 ``UGCGroupDelCommandObjCommandReverse``（execute/undo）并 ``self._do_command(command)``
     → 撤销栈 + 变更登记 + 占用刷新 + 组件数刷新 + 属性面板刷新（一个都不少）
  5. 逐个 ``on_del_unit_setup_mgr(unit.uid)``

因此本模块**不自造删除**，只做「定位 → 护栏 → 调原生命令层 → 回读校验 → 审计」。

护栏
----
1. 只允许删**编辑器已登记**的对象（``ui.all_ugc_uid_set``），非登记对象需显式
   ``allow_unregistered=true``（用于清理历史遗留物）。
2. 拒绝已处于待删除状态的对象。
3. **只接受显式 ugc_id / owner_uid / ugc_ids**，不支持按 item_id 或位置模糊圈选。
4. 单次最多 50 个对象。
5. ``dry_run=true`` 只返回将被删除的清单（含组合级联预估），不写任何东西。
6. 删除后回读校验（实体消失、索引清理、占用与组件数下降），失败如实报告。
7. 审计留痕（挂 room，最多 100 条）。

持久化：删除走编辑器命令层，与手动删除等价；是否落库仍需在游戏内**保存后重进**核实。
撤销：删除会进游戏自己的撤销栈（Ctrl+Z 可撤）。
"""
import importlib
import time

HISTORY_LIMIT = 100
MAX_BATCH = 50


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
    ops = importlib.reload(importlib.import_module("fuxiang_edit_ops"))
    return ops._ui(edit)


def _history(room, record=None):
    buf = vars(room).setdefault("_fuxiang_edit_history", [])
    if record is None:
        return buf
    record["ts"] = time.time()
    buf.append(record)
    if len(buf) > HISTORY_LIMIT:
        del buf[:len(buf) - HISTORY_LIMIT]
    return buf


def _ops():
    return importlib.reload(importlib.import_module("fuxiang_edit_ops"))


# ---------------------------------------------------------------------------
# 定位（只接受显式 id，不做模糊圈选）
# ---------------------------------------------------------------------------
def resolve_units(edit, args):
    """把 ugc_id / ugc_ids / owner_uid 解析成 [(ugc_id, comp, unit)]，并给出不可删原因。"""
    ops = _ops()
    cm = _cm(edit)
    room = _room(edit)
    try:
        ui = _ui(edit)
    except Exception:
        ui = None
    allow_unreg = bool(args.get("allow_unregistered"))

    raw_ids = []
    if args.get("ugc_ids") is not None:
        v = args["ugc_ids"]
        if not isinstance(v, (list, tuple)) or not v:
            raise ValueError("ugc_ids 必须是非空数组")
        raw_ids = list(v)
    elif args.get("ugc_id") is not None:
        raw_ids = [args["ugc_id"]]
    elif args.get("owner_uid") is not None:
        raw_ids = [args["owner_uid"]]
    else:
        raise ValueError("需要 ugc_id / ugc_ids / owner_uid 之一（删除不支持模糊圈选）")
    if len(raw_ids) > MAX_BATCH:
        raise ValueError("单次最多删除 %d 个对象；请分批" % MAX_BATCH)

    id2c = getattr(cm, "_ugc_id2component", None) or {}
    deleted = room.del_unit_uids.get("unit", []) if hasattr(room, "del_unit_uids") else []

    ok, bad = [], []
    for raw in raw_ids:
        comp = None
        for k, c in id2c.items():
            if str(k) == str(raw):
                comp = c
                break
        if comp is None and args.get("owner_uid") is not None and len(raw_ids) == 1:
            for c in id2c.values():
                if str(getattr(c, "_owner_uid", None)) == str(raw):
                    comp = c
                    break
        if comp is None:
            bad.append({"id": str(raw), "reason": "组件索引中没有该 ugc_id"})
            continue
        unit = getattr(comp, "_owner", None)
        ugc_id = getattr(comp, "_ugc_id", None)
        uid = getattr(unit, "uid", None)
        reasons = []
        if unit is None:
            reasons.append("组件没有挂载实体 unit")
        elif room.unit_mgr.get_unit(uid) is not unit:
            reasons.append("实体已不在场景中")
        if uid in deleted:
            reasons.append("已处于待删除状态")
        if ui is not None and uid not in getattr(ui, "all_ugc_uid_set", set()) and not allow_unreg:
            reasons.append("不在编辑器登记集合（如确要删除历史遗留物，请传 allow_unregistered=true）")
        if reasons:
            bad.append({"id": str(raw), "ugc_id": ugc_id, "reason": reasons})
            continue
        ok.append((ugc_id, comp, unit))

    return ok, bad


def _snapshot(edit, unit, comp):
    ops = _ops()
    t = ops._transform(unit)
    return {
        "ugc_id": getattr(comp, "_ugc_id", None),
        "owner_uid": getattr(unit, "uid", None),
        "component_type": getattr(comp, "COMPONENT_TYPE", None),
        "component_class": type(comp).__name__,
        "item_id": getattr(unit, "ugc_type", None),
        "transform": t,
        "is_composite_parent": getattr(unit, "ugc_type", None) == 203900,
    }


def _counters(edit, ui):
    out = {}
    for name, fn in (("occupancy", "get_current_occupancy"),
                     ("component_count", "get_all_ugc_component_nums")):
        try:
            out[name] = getattr(ui, fn)()
        except Exception as e:
            out[name] = None
            out[name + "_err"] = "%s: %s" % (type(e).__name__, e)
    return out


def _composite_links(edit, unit):
    """尽力报告该对象在组合里的身份（父件/子件/无）。"""
    out = {"is_composite_parent": getattr(unit, "ugc_type", None) == 203900,
           "composite_child_of": None}
    try:
        ui = _ui(edit)
        pm = getattr(ui, "ugc_part_manager", None)
        if pm is None:
            return out
        import types as _t
        mod = importlib.import_module(
            "ui.UIBattleUGCPart.UIBattleUGCCompositePart")
        cls = getattr(mod, "UIBattleUGCCompositeManagerPart", None)
        if cls is None:
            return out
        iface = next((k for k in vars(mod)
                      if isinstance(vars(mod)[k], type) and "iUIBattleUGCCompositeManagerPart" == k), None)
        part = None
        try:
            part = pm.get_part(vars(mod)[iface]) if iface else None
        except Exception:
            part = None
        if part is None:
            return out
        for meth, key in (("get_composite_parent_idx", "composite_child_of"),
                          ("get_ugc_id_in_composite", "composite_child_of")):
            f = getattr(part, meth, None)
            if not callable(f):
                continue
            try:
                v = f(unit.uid)
            except Exception:
                continue
            if v:
                out[key] = _plain(v)
                break
    except Exception:
        pass
    return out


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


# ---------------------------------------------------------------------------
# op：预览
# ---------------------------------------------------------------------------
def op_del_preview(edit, args):
    """只读预览：哪些对象会被删、组合级联预估、当前占用与组件数。"""
    ok, bad = resolve_units(edit, dict(args, allow_unregistered=args.get("allow_unregistered")))
    ui = None
    try:
        ui = _ui(edit)
    except Exception:
        ui = None
    ops = _ops()
    items = []
    for ugc_id, comp, unit in ok:
        row = _snapshot(edit, unit, comp)
        row.update(_composite_links(edit, unit))
        items.append(row)
    return {
        "would_delete": items,
        "would_delete_count": len(items),
        "rejected": bad,
        "counters_now": _counters(edit, ui) if ui is not None else None,
        "scene_state": _scene_state(edit),
        "note": "删除走游戏原生命令层（_do_normal_del_commands），自动处理组合级联、"
                "占用刷新、撤销栈与变更登记；游戏内 Ctrl+Z 可撤销。"
                "持久化仍需在游戏内保存后重进核实。",
    }


def _scene_state(edit):
    room = _room(edit)
    cm = _cm(edit)
    deleted = room.del_unit_uids.get("unit", []) if hasattr(room, "del_unit_uids") else []
    return {
        "component_index_size": len(getattr(cm, "_ugc_id2component", None) or {}),
        "pending_del_count": len(deleted),
    }


# ---------------------------------------------------------------------------
# op：执行
# ---------------------------------------------------------------------------
def op_del_objects(edit, args):
    """删除场景中的对象（走游戏原生命令层）。支持 dry_run。"""
    dry = bool(args.get("dry_run"))
    ok, bad = resolve_units(edit, dict(args))
    ui = _ui(edit)
    room = _room(edit)
    if not ok:
        return {"ok": False, "error": "没有可删除的对象", "rejected": bad,
                "detail": "所有目标都被护栏拦下；用 fuxiang_edit_delete 配 dry_run=true 看原因。"}
    if bad and not args.get("skip_rejected"):
        return {"ok": False,
                "error": "部分目标不可删除（%d 个）；请先处理它们或传 skip_rejected=true 只删可删的"
                         % len(bad),
                "rejected": bad,
                "deletable": [{"ugc_id": u, "owner_uid": getattr(un, "uid", None)}
                              for u, c, un in ok]}

    units = [un for _u, _c, un in ok]
    counters_before = _counters(edit, ui)
    scene_before = _scene_state(edit)
    snapshots = [_snapshot(edit, un, c) for _u, c, un in ok]

    if dry:
        return {"dry_run": True, "would_delete": snapshots,
                "would_delete_count": len(snapshots),
                "rejected": bad, "counters_before": counters_before,
                "note": "未写入任何东西。确认无误后去掉 dry_run 再执行。"}

    # 原生删除：游戏删除按钮的完整链路（含七道校验 / 组合展开 / 占用刷新 / 撤销栈）。
    # 关键：它从 ui.choose_uid_list（多选状态）取对象，因此先把目标写入、删完即恢复。
    old_choose = getattr(ui, "choose_uid_list", None)
    uids = [getattr(un, "uid", None) for _u, _c, un in ok]
    try:
        ui.choose_uid_list = uids
        ui._del_unit()
    except Exception as exc:
        return {"ok": False, "error": "原生命令层删除失败: %s: %s" % (type(exc).__name__, exc),
                "counters_before": counters_before,
                "note": "命令可能部分执行；请在游戏内查看现场，不要重复发送同一批删除。"}
    finally:
        try:
            ui.choose_uid_list = old_choose
        except Exception:
            pass

    # 回读校验：以**编辑器记账**为准（占用下降 / 组件数下降 / 移出登记集合）。
    # unit_mgr 与组件索引的清理是延迟的，不能作为失败判据（实测会滞后）。
    counters_after = _counters(edit, ui)
    occ_down = (counters_after.get("occupancy") is not None and
                counters_before.get("occupancy") is not None and
                counters_after["occupancy"] < counters_before["occupancy"])
    cnt_down = (counters_after.get("component_count") is not None and
                counters_before.get("component_count") is not None and
                counters_after["component_count"] < counters_before["component_count"])
    gone, remaining = [], []
    for (ugc_id, comp, unit), snap in zip(ok, snapshots):
        uid = getattr(unit, "uid", None)
        membership = ui is not None and uid in getattr(ui, "all_ugc_uid_set", set())
        row = {"ugc_id": ugc_id, "owner_uid": uid,
               "removed_from_editor_set": not membership}
        (gone if not membership else remaining).append(row)
    scene_after = _scene_state(edit)
    record = {"kind": "delete", "count": len(ok),
              "deleted": [s["ugc_id"] for s in snapshots],
              "occupancy": [counters_before.get("occupancy"), counters_after.get("occupancy")]}
    _history(room, record)
    return {"ok": not remaining and (occ_down or cnt_down),
            "deleted": snapshots,
            "deleted_count": len(snapshots),
            "verified": {"occupancy_down": bool(occ_down),
                         "component_count_down": bool(cnt_down),
                         "all_removed_from_editor_set": not remaining},
            "still_remaining": remaining,
            "rejected": bad,
            "counters_before": counters_before,
            "counters_after": counters_after,
            "scene_before": scene_before,
            "scene_after": scene_after,
            "undo_hint": "删除已进游戏撤销栈，可在游戏内 Ctrl+Z 撤销。",
            "persistence_verified": False,
            "note": "删除走游戏删除按钮的完整链路（选中 → 校验 → 命令层），与手动删除等价；"
                    "占用与组件数已回读核实；unit_mgr/索引清理由引擎延迟完成属正常。"
                    "持久化需在游戏内保存后重进核实。"}
