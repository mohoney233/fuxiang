# -*- coding: utf-8 -*-
"""剧情写测试：建 1 个测试剧情节点 -> save_map -> 删除 -> save_map -> 核对回基线。

用途：裁决 tools/fuxiang/STORY_EDIT.md 顶部的内部矛盾 1 / 2：
  矛盾 1「节点删除是否可信」——story_delete 之后重进地图是否真的消失。
  矛盾 2「重进终验是否通过」——保存基线回滚是否留下同位置重复件。

用法：
  python cli/story_write_test.py baseline   # 采基线快照，写 ipc/story_test_snapshot.json
  python cli/story_write_test.py create [名]  # 建测试剧情节点（原生面板路径，默认 ZZ_TEST_DELETE_ME）
  python cli/story_write_test.py save       # save_map + 轮询 + 读 dropped_count
  python cli/story_write_test.py delete [名]  # 删测试节点（先 dry_run；打 del_unit_uids 前后差值）
  python cli/story_write_test.py verify     # 当前态 vs 基线 差异
  python cli/story_write_test.py pendingdel # 只读 room.del_unit_uids 两个桶
  python cli/story_write_test.py all        # create -> save -> delete -> save -> verify

`delete` 会额外打印 `[del_uids_before/after]` 与 `[MISATTRIBUTION] leaked=...`：
待删除袋里若出现**非本次目标**的 ugc_id，即 STORY_EDIT §8.6 所述的「删除被记到别的
活节点头上」危险行为（会把正常剧情连带删掉），必须立即按该节急救。

约定：本脚本不实现新的 unwrap（统一走 cli/fxapi.py）；只读记账一律用
`FS.get_edit_ui(FE)` 的 ui 记账（组件数 / 占用 / 登记集合），**不**用
`status.component_count`（那是 cm._ugc_id2component 索引，含残骸且滞后）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fxapi  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "ipc", "story_test_snapshot.json")

# 明确可识别的测试命名，便于残留时人工清除
TEST_NAME = "ZZ_TEST_DELETE_ME"

# 可覆盖：python cli/story_write_test.py create ZZ_TEST_A
if len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
    TEST_NAME = sys.argv[2]

# ui 记账探针（只读）。用预置 FS/FE 拿 ui，不碰受保护 API。
_PROBE = ("ui = FS.get_edit_ui(FE)\n"
          "__result__ = jdump({\n"
          "  'ui_component_nums': ui.get_all_ugc_component_nums(),\n"
          "  'occupancy': ui.get_current_occupancy(),\n"
          "  'registered': len(set(getattr(ui, 'all_ugc_uid_set', None) or set())),\n"
          "})\n")

# 直读「待删除」标记袋 —— 用于验证删除不会把标记记到别的（活）节点头上
_DEL_PROBE = ("du = getattr(room, 'del_unit_uids', None) or {}\n"
              "__result__ = jdump({k: sorted(int(x) for x in (v or []))\n"
              "                    for k, v in du.items()})\n")


def pending_del():
    """room.del_unit_uids 的当前内容（component / unit 两个桶）。"""
    val = fxapi.call("eval", {"code": _DEL_PROBE})
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {"raw": val}
    return val


# 校验「清理待删除标记」这条路径本身是否有效：
# fuxiang_story._clear_pending_delete 用 room.check_del_unit_uids(uid, bucket) 判存在，
# 若该接口语义与假设不符（例如只认 unit 桶、或需要 uid 为 str），清理会静默失效，
# 结果就是「服务端创建的节点在下次保存被当删除上送」——即 STORY_EDIT §8.6 的危险。
_CHECK_PROBE = ("du = getattr(room, 'del_unit_uids', None) or {}\n"
                "out = {'type': type(du).__name__,\n"
                "       'has_check': hasattr(room, 'check_del_unit_uids'),\n"
                "       'has_change': hasattr(room, 'change_del_unit_uids'),\n"
                "       'raw': {k: sorted(int(x) for x in (v or [])) for k, v in du.items()}}\n"
                "uids = sorted({int(x) for v in du.values() for x in (v or [])})\n"
                "per = {}\n"
                "for u in uids:\n"
                "    row = {}\n"
                "    for b in ('component', 'unit'):\n"
                "        try:\n"
                "            row[b] = bool(room.check_del_unit_uids(int(u), b))\n"
                "        except Exception as e:\n"
                "            row[b] = 'ERR:' + type(e).__name__\n"
                "    per[str(u)] = row\n"
                "out['per_uid'] = per\n"
                "__result__ = jdump(out)\n")


def del_flags():
    """待删除袋 + 每个 uid 在两个桶里的 check 结果。"""
    val = fxapi.call("eval", {"code": _CHECK_PROBE})
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {"raw": val}
    return val


def ui_counters():
    """编辑器权威记账：组件数 / 占用值 / 登记集合大小。"""
    val = fxapi.call("eval", {"code": _PROBE})
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {"raw": val}
    return val if isinstance(val, dict) else {"raw": val}


def snapshot():
    st = fxapi.call("status") or {}
    dirty = fxapi.call("save_dirty") or {}
    tree = fxapi.call("story_tree", {"compact": True}) or {}
    mi = fxapi.call("map_info") or {}
    return {
        "ui": ui_counters(),
        "index_component_count": st.get("component_count"),
        "change_units_length": dirty.get("change_units_length"),
        "save_events_length": dirty.get("save_events_length"),
        "is_save_component_info": dirty.get("is_save_component_info"),
        "type_distribution": (mi.get("type_distribution") or {}),
        "story_count": tree.get("story_count"),
        "dialogue_count": tree.get("dialogue_count"),
        "choice_count": tree.get("choice_count"),
        "orphan_dialogues": tree.get("orphan_dialogues"),
        "orphan_choices": tree.get("orphan_choices"),
        "story_names": [s.get("name") for s in (tree.get("stories") or [])],
        "story_ids": [s.get("ugc_id") for s in (tree.get("stories") or [])],
        # 本次测试留下的节点（按命名前缀识别），用于"删一个、留一个"的定向核对
        "test_nodes": [(s.get("ugc_id"), s.get("name"))
                       for s in (tree.get("stories") or [])
                       if (s.get("name") or "").startswith("ZZ_TEST")],
    }


def show(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=1,
                                default=str) + "\n")


def _save():
    """save_map -> 轮询到定论 -> 读 dropped_count。

    ⚠️ 轮询必须在**首个定论态**退出：save_map_poll 一旦读到回执就会把在途标志清空并
    返回 status=saved，此后所有轮询都变成 no_active_request。死等某个不存在的键会把
    首次回执（含 dropped_count）覆盖掉。
    """
    res = fxapi.call("save_map", {})
    sys.stdout.write("[save_map] " + json.dumps(res, ensure_ascii=False,
                                                default=str) + "\n")
    rid = (res or {}).get("request_id") if isinstance(res, dict) else None
    poll = None
    if rid:
        deadline = time.time() + 90
        while time.time() < deadline:
            poll = fxapi.call("save_map_poll", {"request_id": rid,
                                               "timeout": 0.01})
            status = poll.get("status") if isinstance(poll, dict) else None
            if status in ("saved", "timeout", "no_active_request",
                          "no_such_request"):
                break
            time.sleep(0.5)
    sys.stdout.write("[save_map_poll] " + json.dumps(poll, ensure_ascii=False,
                                                     default=str) + "\n")
    dirty = fxapi.call("save_dirty") or {}
    sys.stdout.write("[save_dirty] " + json.dumps(dirty, ensure_ascii=False,
                                                  default=str) + "\n")
    return poll, dirty


def cmd_baseline():
    snap = snapshot()
    with open(SNAP, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1)
    sys.stdout.write("baseline -> %s\n" % SNAP)
    show(snap)
    return 0


def _load():
    with open(SNAP, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_create():
    # story 的合法字段见 story_fields(kind=story)；只有 name 是必须的，
    # edit_time 由 op_story_create 自动补。
    res = fxapi.call("story_create", {"kind": "story",
                                      "fields": {"name": TEST_NAME}})
    sys.stdout.write("[create] " + json.dumps(res, ensure_ascii=False,
                                              default=str) + "\n")
    rid = (res or {}).get("request_id") if isinstance(res, dict) else None
    if not rid:
        sys.stdout.write("no request_id -> abort\n")
        return 1
    poll = None
    deadline = time.time() + 40
    while time.time() < deadline:
        poll = fxapi.call("story_create_poll", {"request_id": rid})
        if isinstance(poll, dict) and poll.get("status") in (
                "placed", "timeout", "unknown"):
            break
        time.sleep(0.3)
    sys.stdout.write("[poll] " + json.dumps(poll, ensure_ascii=False,
                                            default=str) + "\n")
    if isinstance(poll, dict):
        sys.stdout.write(">>> test story ugc_id = %r, auto_dialogue = %r\n"
                         % (poll.get("ugc_id"), poll.get("auto_dialogue_ugc_id")))
    show({"now": snapshot()})
    return 0 if (isinstance(poll, dict) and poll.get("status") == "placed") else 1


def cmd_save():
    _save()
    return 0


def cmd_delete():
    tree = fxapi.call("story_tree", {"compact": False}) or {}
    hit = [s for s in (tree.get("stories") or []) if s.get("name") == TEST_NAME]
    # 连带它的对白（含原生自动建的首对白）与选项
    ids = []
    for s in hit:
        ids.append(s.get("ugc_id"))
        for d in s.get("dialogues") or []:
            ids.append(d.get("ugc_id"))
            ids += [c.get("ugc_id") for c in d.get("choices") or []]
    sys.stdout.write("[matched] " + json.dumps(
        {"stories": [s.get("ugc_id") for s in hit], "targets": ids},
        ensure_ascii=False) + "\n")
    if not ids:
        sys.stdout.write("no test node found -> nothing to delete\n")
        return 1
    sys.stdout.write("[del_uids_before] " + json.dumps(pending_del(), ensure_ascii=False) + "\n")
    dry = fxapi.call("story_delete", {"ugc_ids": ids, "dry_run": True,
                                      "allow_unregistered": True, "cascade": True})
    sys.stdout.write("[dry_run] " + json.dumps(dry, ensure_ascii=False,
                                               default=str) + "\n")
    res = fxapi.call("story_delete", {"ugc_ids": ids, "allow_unregistered": True,
                                      "cascade": True})
    sys.stdout.write("[delete] " + json.dumps(res, ensure_ascii=False,
                                              default=str) + "\n")
    # 关键证据：删除只应把**本次目标**记进待删除袋；若出现别的（活）节点 ugc_id，
    # 就是 STORY_EDIT §8.6 描述的「删除记到别的节点头上」危险行为。
    after = pending_del()
    sys.stdout.write("[del_uids_after] " + json.dumps(after, ensure_ascii=False) + "\n")
    leaked = set()
    for bucket, uids in (after or {}).items():
        leaked |= set(uids) - set(ids)
    sys.stdout.write("[MISATTRIBUTION] leaked=%s\n" % sorted(leaked))
    show({"now": snapshot()})
    return 0


def _diff(base, cur):
    keys = ["index_component_count", "change_units_length", "save_events_length",
            "is_save_component_info", "story_count", "dialogue_count",
            "choice_count", "orphan_dialogues", "orphan_choices"]
    d = {k: [base.get(k), cur.get(k)] for k in keys}
    bu, cu = base.get("ui") or {}, cur.get("ui") or {}
    for k in ("ui_component_nums", "occupancy", "registered"):
        d["ui." + k] = [bu.get(k), cu.get(k)]
    d["type_distribution_diff"] = {
        k: [base["type_distribution"].get(k, 0), cur["type_distribution"].get(k, 0)]
        for k in set(base["type_distribution"]) | set(cur["type_distribution"])
        if base["type_distribution"].get(k, 0) != cur["type_distribution"].get(k, 0)}
    d["new_story_names"] = [n for n in cur.get("story_names", [])
                            if n not in base.get("story_names", [])]
    d["new_story_ids"] = [i for i in cur.get("story_ids", [])
                          if i not in base.get("story_ids", [])]
    d["test_nodes_present"] = cur.get("test_nodes")
    try:
        d["story_count_delta"] = (cur.get("story_count") or 0) - (base.get("story_count") or 0)
    except TypeError:
        d["story_count_delta"] = None
    # 判定：UI 记账（组件数/占用/登记）全部回基线 且 剧情数回基线
    reg_ok = (bu.get("ui_component_nums") == cu.get("ui_component_nums")
              and bu.get("occupancy") == cu.get("occupancy")
              and bu.get("registered") == cu.get("registered"))
    story_ok = base.get("story_count") == cur.get("story_count")
    d["VERDICT_back_to_baseline"] = bool(reg_ok and story_ok)
    return d


def cmd_verify():
    base = _load()
    cur = snapshot()
    show({"baseline_vs_now": _diff(base, cur), "test_name": TEST_NAME})
    return 0


def cmd_all():
    rc = cmd_baseline()
    if rc:
        return rc
    rc = cmd_create()
    if rc:
        sys.stdout.write("!! create 未 placed，停止（不要保存）\n")
        return rc
    cmd_save()
    # 保存后立刻看重复件迹象（矛盾 2）
    mid = snapshot()
    sys.stdout.write("[after-save snapshot] " + json.dumps(mid, ensure_ascii=False,
                                                           default=str) + "\n")
    cmd_delete()
    cmd_save()
    sys.stdout.write("=== FINAL VERIFY ===\n")
    return cmd_verify()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    table = {"baseline": cmd_baseline, "create": cmd_create, "save": cmd_save,
             "delete": cmd_delete, "verify": cmd_verify, "all": cmd_all,
             "pendingdel": lambda: (show(pending_del()), 0)[1],
             "delcheck": lambda: (show(del_flags()), 0)[1]}
    fn = table.get(cmd)
    if fn is None:
        sys.stdout.write(__doc__)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
