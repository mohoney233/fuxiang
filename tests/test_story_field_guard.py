#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_story_field_guard — 剧情字段写入 / 事件引用登记的护栏（无需游戏）。

背景（2026-09-23 跨图还原实机踩出来的四个坑，一条链）
------------------------------------------------------
源图 story 的 `edit_time` 槽值是 `[""]`（面板没填时原生写空串），导出成文档是 `""`；
导入时 `encode_value(("int", …), "")` → `int("")` 抛 ValueError，把**整个节点**的字段
写入连累回滚 ⇒ 节点补不齐模板槽（STORY 停在 2 槽，正常 13 槽）⇒ 之后指向它的事件引用
补绑时报「装不下下标」⇒ 事件「剧情被触发」**静默丢失**（本地一切"成功"）。

同一轮还暴露：
  ① `_register_event_refs` 登记的是**组件本地下标**，而注册表约定是**单元级**
     （own_index + PARAMS_HEADER）⇒ 4 条幽灵引用（解析回本地下标 0 =「所属对话ugc_id」），
     event_list 的 `bad_refs` 就是这么露出来的；
  ② 还原侧把快照里**源节点**的 `index` 一并下发给 event_bind ⇒ 目标节点模板槽顺序不同，
     同一个 own_index 指到**别的字段**（事件「测试」落到 enable(…40008) 而非
     「设为可用」(…40009)），而槽里恰好放着同名键 ⇒ `key_ok` 仍是 true，
     只有比 property_id 才发现；
  ③ 上面三条都是"看不出错"的失败，所以还原侧必须有**逐条回对**（verify_event_refs）。
这里把四条都锁住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "cli"))

import fuxiang_edit_ops as ops  # noqa: E402
import fuxiang_story as fs  # noqa: E402
import snapshot_restore as sr  # noqa: E402

INT_SPEC = ("p", "int", "_init", "某数值", None)
TEXT_SPEC = ("p", "text", "_init", "某文本", None)
COLOR_SPEC = ("p", "color", "_init", "某颜色", None)

PID_ON_TOUCH = 1000700340003   # 选项被点击（事件发送槽）
PID_ENABLE = 1000700340008     # 是否启用（**不是**事件槽）
PID_ENABLE_EVT = 1000700340009  # 设为可用（事件槽）
PID_STORY_START = 1000700114704  # 使剧情开始播放（事件接收槽）


class TestDecodeValueGuard(unittest.TestCase):
    """数值字段的空串必须归成 None —— 不然它会在导入侧炸掉整个节点。"""

    def test_int_empty_string_becomes_none(self):
        self.assertIsNone(fs.decode_value(INT_SPEC, [""]))
        self.assertIsNone(fs.decode_value(INT_SPEC, ["   "]))

    def test_int_normal(self):
        self.assertEqual(fs.decode_value(INT_SPEC, [7]), 7)
        self.assertEqual(fs.decode_value(INT_SPEC, ["7"]), "7")

    def test_text_empty_string_kept(self):
        # text 字段的空串是合法值，不该被吃掉
        self.assertEqual(fs.decode_value(TEXT_SPEC, [""]), "")

    def test_empty_list_is_none(self):
        self.assertIsNone(fs.decode_value(INT_SPEC, []))

    def test_color_list_of_one_string(self):
        self.assertEqual(fs.decode_value(COLOR_SPEC, [["A0AED6"]]), "A0AED6")

    def test_doc_round_trip_no_crash(self):
        """文档里 `edit_time: ""` → 跳过（而不是 int("") 抛错）。"""
        decoded = fs.decode_value(INT_SPEC, [""])
        self.assertIsNone(decoded)
        # 空值必须能被判定为"跳过"，不能进 encode_value（encode_value(None) 会抛）
        self.assertTrue(decoded is None or decoded == "")


class TestParamsHeaderSingleSourceOfTruth(unittest.TestCase):
    """单元级下标的偏移量只能有一处定义。"""

    def test_matches_edit_ops_constant(self):
        self.assertEqual(fs._params_header(), ops.PARAMS_HEADER)
        self.assertEqual(ops.PARAMS_HEADER, 3)

    def test_event_ref_own_index_is_reversible(self):
        for own in (0, 3, 5, 12):
            unit_level = own + ops.PARAMS_HEADER
            self.assertEqual(ops._event_ref_own_index(unit_level), own)


class FakeCM(object):
    def __init__(self, keys=()):
        self._ugc_params_events_key = {
            "event": {k: {"info": []} for k in keys}}
        self.calls = []

    def change_ugc_params_events_key(self, child, key, refs):
        self.calls.append((child, key, list(refs)))
        self._ugc_params_events_key["event"][key]["info"] = list(refs)


class FakeComp(object):
    def __init__(self, ugc_id):
        self._ugc_id = ugc_id


class TestRegisterEventRefsUnitLevel(unittest.TestCase):
    """直写属性袋后补登记引用时，下标必须是**单元级**（own + PARAMS_HEADER）。"""

    def setUp(self):
        self.cm = FakeCM(keys=["选项1"])
        self._orig = fs._cm
        fs._cm = lambda edit: self.cm
        self.addCleanup(lambda: setattr(fs, "_cm", self._orig))

    def test_on_touched_registered_with_unit_level_index(self):
        events = [
            {"type": 1000700340001, "info": [353]},       # 所属对话 —— 非事件槽
            {"type": 1000700340002, "info": [0]},
            {"type": 1000700340007, "info": ["选项1"]},
            {"type": PID_ON_TOUCH, "info": ["选项1"]},    # 本地下标 3
        ]
        reg = fs._register_event_refs(None, FakeComp(358), events)
        self.assertEqual(reg, [{"event": "选项1", "ref": (358, 6, 0)}],
                         "本地下标 3 必须登记成单元级 6 —— 少加 PARAMS_HEADER "
                         "会造出指向「所属对话ugc_id」的幽灵引用")

    def test_non_event_pids_not_registered(self):
        events = [{"type": PID_ENABLE, "info": ["not-an-event-slot"]}]
        self.assertEqual(fs._register_event_refs(None, FakeComp(1), events), [])

    def test_empty_slot_not_registered(self):
        events = [{"type": PID_ON_TOUCH, "info": None}]
        self.assertEqual(fs._register_event_refs(None, FakeComp(1), events), [])

    def test_unknown_event_not_registered(self):
        # 注册表里没这个事件（info=None 也视为不存在）⇒ 不登记
        events = [{"type": PID_ON_TOUCH, "info": ["不存在的事件"]}]
        self.assertEqual(fs._register_event_refs(None, FakeComp(1), events), [])

    def test_idempotent(self):
        events = [{"type": PID_ON_TOUCH, "info": ["选项1"]}]
        fs._register_event_refs(None, FakeComp(358), events)
        fs._register_event_refs(None, FakeComp(358), events)
        refs = self.cm._ugc_params_events_key["event"]["选项1"]["info"]
        self.assertEqual(refs, [(358, 0 + ops.PARAMS_HEADER, 0)])


class ChangeOnlyCM(object):
    """只有 change_（追加语义）的假组件管理器。"""

    def __init__(self):
        self.calls = []

    def change_ugc_params_events_key(self, child, key, refs):
        self.calls.append((child, key, list(refs)))


class BothCM(ChangeOnlyCM):
    """同时有 change_ 与 modify_（替换语义）的假管理器。"""

    def __init__(self):
        ChangeOnlyCM.__init__(self)
        self.mods = []

    def modify_ugc_params_events_key(self, child, key, refs):
        self.mods.append((child, key, list(refs)))


class TestSetEventRefsReplaceSemantics(unittest.TestCase):
    """摘引用必须走 `modify_`：`change_` 只追加，给短列表不会删掉任何东西。

    2026-09-23 实机：源图幽灵引用清不掉，就是因为 `op_event_unbind` 用了 change_ ——
    回读长度一点没变。这条锁住优先级与去重。
    """

    def test_prefers_modify_when_available(self):
        cm = BothCM()
        r = ops._set_event_refs(cm, "选项1", [[358, 3, 0], [358, 6, 0], [358, 6, 0]])
        self.assertEqual(r["by"], "modify")
        self.assertEqual(cm.mods, [("event", "选项1", [[358, 3, 0], [358, 6, 0]])])
        self.assertEqual(cm.calls, [], "有 modify_ 时不该再碰 change_")

    def test_falls_back_to_change(self):
        cm = ChangeOnlyCM()
        r = ops._set_event_refs(cm, "k", [[1, 2, 3]])
        self.assertEqual(r["by"], "change")
        self.assertEqual(len(cm.calls), 1)

    def test_dedupe_keeps_first_occurrence_order(self):
        self.assertEqual(ops._dedupe_refs([[1, 2, 3], [1, 2, 3], [4, 5, 6], (1, 2, 3)]),
                         [[1, 2, 3], [4, 5, 6]])

    def test_dedupe_tolerates_non_iterable(self):
        self.assertEqual(ops._dedupe_refs(["x", "x", None]), ["x", None])


class TestApplyEventsUsesPortableLocator(unittest.TestCase):
    """还原侧只下发 property_id —— 源节点的本地下标跨节点/跨图会指错字段。"""

    def setUp(self):
        self.calls = []
        self._orig = sr.call_edit

        def fake(op, args=None, timeout=None):
            self.calls.append((op, args))
            if op == "event_bind":
                return {"ok": True, "property_id": args.get("property_id")}
            if op == "event_create":
                return {"ok": True}
            return {}

        sr.call_edit = fake
        self.addCleanup(lambda: setattr(sr, "call_edit", self._orig))

    def binds(self):
        return [a for o, a in self.calls if o == "event_bind"]

    def test_no_local_index_forwarded(self):
        events = {"events": [{"event_key": "测试", "refs": [
            {"ugc_id": 352, "index": 8, "params_idx": 0,
             "property_id": PID_ENABLE_EVT, "current_value": "['测试']"}]}]}
        r = sr.apply_events(events, {352: 359})
        self.assertEqual(len(self.binds()), 1)
        self.assertNotIn("index", self.binds()[0],
                         "源节点 index=8 → 目标节点本地下标 5 是 enable 字段，"
                         "下发它会写到 enable 上")
        self.assertEqual(self.binds()[0]["property_id"], PID_ENABLE_EVT)
        self.assertEqual(r["applied"][0]["src_index"], 8)  # 只留作报告

    def test_index_fallback_only_without_pid(self):
        events = {"events": [{"event_key": "x", "refs": [
            {"ugc_id": 1, "index": 6, "params_idx": 0, "property_id": None}]}]}
        sr.apply_events(events, {1: 11})
        self.assertEqual(self.binds()[0]["index"], 6)

    def test_info_written_through(self):
        events = {"events": [{"event_key": "e", "refs": [
            {"ugc_id": 1, "index": 4, "params_idx": 0, "property_id": 20300311106,
             "current_value": "['山庄来信触发', 1286]"}]}]}
        sr.apply_events(events, {1: 11}, comp_index={})
        self.assertEqual(self.binds()[0]["info"], ["山庄来信触发", 1286])


class TestVerifyEventRefs(unittest.TestCase):
    """回对自检：key_ok 为真也可能落在**别的字段**上，必须比 property_id。"""

    def setUp(self):
        self.resp = {}
        self._orig = sr.call_edit

        def fake(op, args=None, timeout=None):
            if op == "event_list":
                return {"ok": True, "value": self.resp}
            return {}

        sr.call_edit = fake
        self.addCleanup(lambda: setattr(sr, "call_edit", self._orig))

    def _snap_event(self, pid, old=352, params_idx=0):
        return {"event_key": "测试", "refs": [
            {"ugc_id": old, "index": 8, "params_idx": params_idx, "property_id": pid}]}

    def _target(self, pid, ugc=359, params_idx=0, key="测试"):
        return {"count": 1, "events": [{"event_key": key, "refs": [
            {"ugc_id": ugc, "index": 8, "params_idx": params_idx,
             "property_id": pid, "key_ok": True}]}]}

    def test_all_match(self):
        self.resp = self._target(PID_ENABLE_EVT)
        v = sr.verify_event_refs({"events": [self._snap_event(PID_ENABLE_EVT)]},
                                 {352: 359})
        self.assertTrue(v["ok"])
        self.assertEqual(v["matched"], 1)

    def test_pid_mismatch_caught_even_when_key_ok_true(self):
        # 槽里恰好放着同名键 ⇒ key_ok 为真，但字段不是「设为可用」而是 enable
        self.resp = self._target(PID_ENABLE)
        v = sr.verify_event_refs({"events": [self._snap_event(PID_ENABLE_EVT)]},
                                 {352: 359})
        self.assertFalse(v["ok"])
        self.assertEqual(v["pid_mismatch"][0]["expected_property_id"],
                         [str(PID_ENABLE_EVT)])
        self.assertEqual(v["pid_mismatch"][0]["got_property_id"], [str(PID_ENABLE)])

    def test_missing_caught(self):
        self.resp = {"count": 0, "events": []}
        v = sr.verify_event_refs(
            {"events": [self._snap_event(PID_STORY_START, old=349)]}, {349: 352})
        self.assertFalse(v["ok"])
        self.assertEqual(v["missing"][0]["ugc_id"], "352")

    def test_extra_phantom_caught(self):
        # 幽灵引用与正确引用**共用同一个 params_idx**（都写在 0 位）：
        # 按 (键, ugc, params_idx) 单点比会被盖掉，按集合比才露出来
        self.resp = self._target(PID_ENABLE_EVT)
        self.resp["events"][0]["refs"].append(
            {"ugc_id": 359, "index": 3, "params_idx": 0,
             "property_id": 1000700340001, "key_ok": True})
        v = sr.verify_event_refs({"events": [self._snap_event(PID_ENABLE_EVT)]},
                                 {352: 359})
        self.assertFalse(v["ok"])
        self.assertEqual(len(v["extra"]), 1)
        self.assertEqual(v["extra"][0]["property_id"], "1000700340001")

    def test_unmapped_reported(self):
        self.resp = {"count": 0, "events": []}
        v = sr.verify_event_refs({"events": [self._snap_event(PID_STORY_START)]}, {})
        self.assertFalse(v["ok"])
        self.assertEqual(v["unmapped"][0]["ugc_id"], 352)


if __name__ == "__main__":
    unittest.main()
