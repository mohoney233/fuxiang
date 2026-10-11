#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_accept — 「这张图和快照 1:1 吗」这条命令的判据。

交付流程原来是六个出口（check / restore 预检 / apply / export / diff / verify-only），
每处各说一段话，收口时要人把六份输出拼起来。拼的时候最容易犯的错是
**把"没比对上"当成"一致"** —— 本文件锁的就是这件事：
每一层要么是 ok，要么就把结论压住，不许默默放行。
"""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402


def _comp(g, i, x, z, y=0.0, events=None, cid=None):
    ci = {"component_id": cid or i, "events": events or []}
    return {"g": g, "i": i, "p": [x, y, z], "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps(ci, ensure_ascii=False)}


def _props(**over):
    """地图属性层（默认背景音乐等）的空值形态：采到了、什么都没设。

    2026-10-01 加这一层：春岚2 七层全绿，用户却在试玩里发现"开局没有音乐"——
    因为 BGM 住在 `ugc_props_info`，而这一层从来没被采。缺它 ⇒ `unknown` 压住结论。
    """
    out = {k: {"present": True, "value": None} for k in sn.PROPS_KEYS}
    for k, v in over.items():
        out[k] = {"present": True, "value": v}
    return out


def _snap(comps=None, ms=None, budget=None, events=None, props="same"):
    comps = comps if comps is not None else [_comp(1, 100001, 1.0, 2.0),
                                            _comp(2, 100001, 5.0, 6.0)]
    mp = {"name": "m", "framework_id": "4002",
          "map_setting": ms if ms is not None else {"14": "4002", "1": "0"},
          "anchor_info": {"gamearea0": [[1, [0, 0, 0]], [2, [100, 0, 100]]]}}
    if props != "absent":
        mp["props"] = _props()
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": mp,
            "components": comps,
            "events": events or {"count": 0, "events": [], "saved_keys": []},
            "story": {"stories": []},
            "budget": budget if budget is not None else
            {"component_nums": len(comps), "registered": len(comps),
             "max_component_nums": 15000, "occupancy": 100}}


def _level(rep, key):
    for l in rep["layers"]:
        if l["key"] == key:
            return l["level"], l["msg"]
    raise AssertionError("没有这一层：%s（现有 %s）" % (key, [l["key"] for l in rep["layers"]]))


class TestAcceptance(unittest.TestCase):
    def test_identical_files_pass(self):
        a = _snap()
        rep = sn.acceptance_report(a, copy.deepcopy(a))
        self.assertTrue(rep["ok"], msg=rep["layers"])
        self.assertEqual(rep["fail"], [])
        self.assertEqual(rep["unknown"], [])

    def test_old_export_without_props_layer_is_not_a_pass(self):
        """没采到地图属性层 ⇒ `unknown`，压住 1:1 —— 不能再有"七层全绿但没音乐"。"""
        a = _snap()
        b = _snap(props="absent")
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "地图属性")[0], "unknown")
        self.assertIn("地图属性", rep["unknown"])
        self.assertFalse(rep["ok"])

    def test_bgm_conf_difference_fails_the_map_props_layer(self):
        a = _snap()
        a["map"]["props"]["ugc_bgm_conf"] = {"present": True,
                                            "value": {"mode": 3,
                                                      "param": {"id": "X"}}}
        rep = sn.acceptance_report(a, _snap())
        self.assertEqual(_level(rep, "地图属性")[0], "fail")
        self.assertFalse(rep["ok"])

    def test_missing_component_fails_the_component_layer(self):
        a = _snap()
        b = _snap(comps=[_comp(1, 100001, 1.0, 2.0)])
        rep = sn.acceptance_report(a, b)
        lvl, msg = _level(rep, "组件")
        self.assertEqual(lvl, "fail")
        self.assertIn("删除 1", msg)
        self.assertFalse(rep["ok"])

    def test_stray_same_content_duplicate_fails(self):
        """多出来的逐字段相同件（实机 15003）不许被"逐件一致"放过去。"""
        twin = _comp(9, 203911, 0.0, 0.0, y=-65536.0, cid=203911,
                     events=[{"type": 20391119001, "info": [[[1.0, 2.0, 3.0, 0.0]]]}])
        base = _comp(8, 203911, 0.0, 0.0, y=-65536.0, cid=203911,
                     events=[{"type": 20391119001, "info": [[[1.0, 2.0, 3.0, 0.0]]]}])
        a = _snap(comps=[base])
        b = _snap(comps=[copy.deepcopy(base), copy.deepcopy(twin)])
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "组件")[0], "fail")
        self.assertFalse(rep["ok"])

    def test_pending_deletions_contaminate_the_reading(self):
        """目标图导出时带着待提交的删除 ⇒ 这份读数本身不干净，不能算通过。"""
        a = _snap()
        b = _snap()
        b["components"].append(_comp(3, 100001, 9.0, 9.0))
        b["components"][-1]["del"] = 1
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "残骸")[0], "warn")
        self.assertFalse(rep["ok"])

    def test_uncompared_layer_is_not_a_pass(self):
        a = _snap()
        b = _snap()
        b["map"].pop("map_setting")
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "地图设置")[0], "unknown")
        self.assertIn("地图设置", rep["unknown"])
        self.assertFalse(rep["ok"])
        self.assertNotIn("地图设置", rep["fail"])

    def test_setting_value_difference_fails(self):
        a = _snap(ms={"14": "4002", "1": "0"})
        b = _snap(ms={"14": "4002", "1": "1"})
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "地图设置")[0], "fail")
        self.assertFalse(rep["ok"])

    def test_event_rebinding_fails(self):
        evs = {"count": 1, "saved_keys": ["K1"],
               "events": [{"event_key": "K1",
                           "refs": [{"ugc_id": 1, "property_id": "10000010001",
                                     "current_value": "1"}]}]}
        a = _snap(events=evs)
        b = _snap(events=copy.deepcopy(evs))
        b["components"][0] = _comp(1, 100001, 1.0, 2.0,
                                   events=[{"type": 10000010001, "info": ["2"]}])
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "事件")[0], "fail")

    def test_blocked_file_is_separate_from_difference(self):
        a = _snap()
        b = _snap()
        b["components"].append(copy.deepcopy(b["components"][0]))   # ugc_id 重复
        rep = sn.acceptance_report(a, b)
        self.assertTrue(rep["blocked"])
        self.assertEqual(_level(rep, "文件")[0], "blocker")
        self.assertFalse(rep["ok"])

    def test_counter_mismatch_is_warn_not_silence(self):
        a = _snap()
        b = _snap(budget={"component_nums": 99, "registered": 2,
                          "max_component_nums": 15000, "occupancy": 100})
        rep = sn.acceptance_report(a, b)
        self.assertEqual(_level(rep, "件数")[0], "warn")
        self.assertFalse(rep["ok"])

    def test_print_states_uncompared_layers(self):
        import contextlib
        import io
        a = _snap()
        b = _snap()
        b["map"].pop("map_setting")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_acceptance(sn.acceptance_report(a, b))
        out = buf.getvalue()
        self.assertIn("没比成的层", out)
        self.assertIn("不是 1:1", out)

    def test_accept_needs_a_second_state(self):
        import argparse
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(_snap(), f)
            args = argparse.Namespace(snapshot=p, other=None, live=False,
                                      tol=0.5, json=False)
            self.assertEqual(sn.cmd_accept(args), 2)   # 不猜、不静默比空气


if __name__ == "__main__":
    unittest.main()
