#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_audit_fixes — 2026-10-03 快照功能审计后的六项修补的离线判据。

审计里真复现过的失效面（都在 2 万件的「快照测试」图上）：
* component_nums=20133 > max_component_nums=15000，体检当正常印出来；
* 摆放操作留下空桶 {'unit': []}，之后每次导出都喊「不可跨会话比较」假告警；
* diff 恒 exit 0，脚本化「改前/改后比对」没法用退出码；
* 采集层异常（批次中止/事件表读取失败）只留在 stdout，文件自己不带印章；
* 地图身份键（名/简介/标签）永不参与比对且无开关。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402


def _comp(g, i, p=(1.0, 2.0, 3.0)):
    return {"g": g, "i": i, "p": list(p), "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps({"component_id": i, "events": []}, ensure_ascii=False)}


def _snap(comps=None, budget=None, counts=None, ms_name="m"):
    mp = {"name": ms_name, "framework_id": "4002",
          "map_setting": {"14": "4002", "1": "0", "name": ms_name,
                          "desc": "旧简介", "tags": ["a"]},
          "anchor_info": {"gamearea0": []},
          "props": {k: {"present": True, "value": None} for k in sn.PROPS_KEYS}}
    snap = {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": mp,
            "components": comps if comps is not None else [_comp(1, 100001)],
            "events": {"count": 0, "events": [], "saved_keys": []},
            "story": {"stories": []},
            "budget": budget if budget is not None
            else {"component_nums": 1, "max_component_nums": 15000,
                  "occupancy": 100, "del_unit_uids": {}}}
    if counts is not None:
        snap["counts"] = counts
    return snap


def _levels(v):
    return {i["key"]: i["level"] for i in v["items"]}


class TestBudgetCapAnomaly(unittest.TestCase):
    """计数器与上限自相矛盾必须点名，不能当正常印出来。"""

    def test_over_cap_warns(self):
        snap = _snap(budget={"component_nums": 20133, "max_component_nums": 15000})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["budget"], "warn")

    def test_under_cap_ok(self):
        snap = _snap(budget={"component_nums": 14933, "max_component_nums": 15000})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["budget"], "ok")

    def test_missing_cap_warns(self):
        snap = _snap(budget={"component_nums": 100})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["budget"], "warn")


class TestFetchIntegrityStamp(unittest.TestCase):
    """采集异常必须随文件落盘，check 按严重度分报。"""

    def test_aborted_batch_warns(self):
        snap = _snap(counts={"components": 10200, "fetch_anomalies": [
            {"layer": "components", "kind": "aborted_batch",
             "msg": "组件批次 start=10200 无返回，中止：实收 10200 / 声明 20478"}]})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["fetch_integrity"], "warn")

    def test_fetch_failed_warns(self):
        snap = _snap(counts={"components": 1, "fetch_anomalies": [
            {"layer": "events", "kind": "fetch_failed",
             "msg": "事件表读取失败（桥接无返回）"}]})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["fetch_integrity"], "warn")

    def test_count_mismatch_is_info(self):
        # 没有批次中止的总数差，多半是「无 _owner 组件被合法跳过」——盖章供人核，不当异常喊
        snap = _snap(counts={"components": 1, "fetch_anomalies": [
            {"layer": "components", "kind": "count_mismatch",
             "msg": "组件声明 20478、实收 20477"}]})
        self.assertEqual(_levels(sn.validate_snapshot(snap))["fetch_integrity"], "info")

    def test_clean_stamp_is_silent(self):
        snap = _snap(counts={"components": 1, "fetch_anomalies": []})
        self.assertNotIn("fetch_integrity", _levels(sn.validate_snapshot(snap)))

    def test_old_file_without_stamp_is_silent(self):
        self.assertNotIn("fetch_integrity", _levels(sn.validate_snapshot(_snap())))

    def test_build_snapshot_collects_into_counts(self):
        # fetch 失败路径的印章要落进 counts（这里只测纯逻辑：印章函数与结构）
        out = []
        sn._stamp(out, "events", "fetch_failed", "x")
        sn._stamp(None, "events", "fetch_failed", "y")     # 不收集时是 no-op
        self.assertEqual(out, [{"layer": "events", "kind": "fetch_failed", "msg": "x"}])


class TestEmptyDelBag(unittest.TestCase):
    """空桶 {'unit': []} 不是待删除 —— summarize/共享判据/restoore 闸门口径一致。"""

    def test_summarize_ignores_empty_bucket(self):
        snap = _snap(budget={"component_nums": 1, "max_component_nums": 15000,
                             "del_unit_uids": {"unit": []}})
        self.assertEqual(sn.summarize(snap)["warnings"], [])

    def test_summarize_still_warns_on_real_bag(self):
        snap = _snap(budget={"component_nums": 1, "max_component_nums": 15000,
                             "del_unit_uids": {"unit": [42]}})
        self.assertTrue(sn.summarize(snap)["warnings"])

    def test_pending_deletions_present(self):
        self.assertFalse(sn.pending_deletions_present(None))
        self.assertFalse(sn.pending_deletions_present({"del_unit_uids": {}}))
        self.assertFalse(sn.pending_deletions_present({"del_unit_uids": {"unit": []}}))
        self.assertTrue(sn.pending_deletions_present({"del_unit_uids": {"unit": [1]}}))


class TestDiffExitCodeAndIdentity(unittest.TestCase):
    """--exit-code 与 --include-identity：结论编进退出码，身份键可选参与比对。"""

    def test_identical_maps_exit_zero(self):
        a, b = _snap(), _snap()
        d = sn.diff_snapshots(a, b)
        self.assertTrue(d["identical"])
        self.assertEqual(d["map_identity"]["skipped"], True)   # 默认不比身份

    def test_renamed_map_stays_identical_by_default(self):
        b = _snap(ms_name="改名后")
        self.assertTrue(sn.diff_snapshots(_snap(), b)["identical"])

    def test_include_identity_catches_rename(self):
        d = sn.diff_snapshots(_snap(), _snap(ms_name="改名后"), include_identity=True)
        self.assertFalse(d["identical"])
        self.assertEqual(d["map_identity"]["changed"][0]["key"], "name")
        self.assertFalse(d["map_identity"]["skipped"])

    def test_include_identity_catches_desc_and_tags(self):
        b = _snap()
        b["map"]["map_setting"]["desc"] = "新简介"
        d = sn.diff_snapshots(_snap(), b, include_identity=True)
        self.assertFalse(d["identical"])
        self.assertIn("desc", [x["key"] for x in d["map_identity"]["changed"]])

    def test_identity_skipped_when_map_setting_missing(self):
        b = _snap(ms_name="改名后")
        del b["map"]["map_setting"]
        d = sn.diff_snapshots(_snap(), b, include_identity=True)
        self.assertTrue(d["map_identity"]["skipped"])
        self.assertTrue(d["identical"])       # 身份层与玩法层同样「没采到就没比成」

    def test_cmd_diff_exit_code_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            pa = os.path.join(td, "a.json")
            pb = os.path.join(td, "b.json")
            pc = os.path.join(td, "c.json")
            with open(pa, "w", encoding="utf-8") as f:
                json.dump(_snap(), f)
            with open(pb, "w", encoding="utf-8") as f:
                json.dump(_snap(), f)
            with open(pc, "w", encoding="utf-8") as f:
                json.dump(_snap(comps=[_comp(1, 100001), _comp(2, 100001, p=(9, 2, 3))]), f)
            self.assertEqual(sn.main(["diff", pa, pb, "--exit-code"]), 0)
            self.assertEqual(sn.main(["diff", pa, pc, "--exit-code"]), 1)
            # 默认（无 --exit-code）恒 0 —— 旧脚本行为不变
            self.assertEqual(sn.main(["diff", pa, pc]), 0)

    def test_cmd_diff_include_identity_flag_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            pa = os.path.join(td, "a.json")
            pb = os.path.join(td, "b.json")
            for path, name in ((pa, "旧名"), (pb, "新名")):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(_snap(ms_name=name), f)
            # 身份键改名：默认不算差异（exit 0），带 --include-identity 才 exit 1
            self.assertEqual(sn.main(["diff", pa, pb, "--exit-code"]), 0)
            self.assertEqual(
                sn.main(["diff", pa, pb, "--exit-code", "--include-identity"]), 1)


if __name__ == "__main__":
    unittest.main()
