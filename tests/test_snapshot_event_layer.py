#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_event_layer — 事件层「逐条一致」这句话现在比了什么。

以前每个事件只比 `(锚点, property_id, 当前值)` 的排序，扔掉了两样要紧的：

* **`in_save`**：事件在不在保存集合里。`get_save_event_info()` 只收集 info 非空的键
  并按 sort_id 组成保存载荷 ⇒ 不在里面就等于"重进之后没有这条事件"。这正好是本项目
  栽过的「保存后引用丢失」那一类，不进判据就是白说。
* **`params_idx`**：事件键在槽位 info 里的下标。同一件、同一字段、值也一样，但注册表
  指的是另一个下标 ⇒ 是条陈旧/错位引用（2026-10-01 复原运动器时实机撞到的就是这种形态，
  当时靠 `bad_refs` 才发现）。

另外两档分开处理，不再混成"一致/不一致"：只差**面板顺序** ⇒ `warn`（框架自带事件会让
目标图的序号整体偏移，那不代表玩法不同）；引用里出现 `key_ok=false` ⇒ `unknown`
（这条读数本身不能信，按本项目口径 unknown 永远不算通过）。
"""
import copy
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402

DELIVERABLE = os.path.join(ROOT, "snapshots", "chunlan_1to1_20260930.json")
LIVE_AFTER_REPAIR = os.path.join(ROOT, "probe", "_live_final.json")


def comp(g, i, x, z):
    return {"g": g, "i": i, "p": [x, 0.0, z], "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps({"component_id": i, "events": []})}


def ref(gid, prop="20300311106", val="['开工']", params_idx="0", key_ok=True):
    return {"ugc_id": gid, "property_id": prop, "current_value": val,
            "params_idx": params_idx, "key_ok": key_ok, "alive": True}


def event(key=u"开工", sort_id=0, refs=None, in_save=True):
    return {"event_key": key, "sort_id": sort_id, "in_save": in_save,
            "refs": refs if refs is not None else [ref(11)]}


def snap(comps=None, events=None):
    comps = comps if comps is not None else [comp(11, 100001, 1.0, 2.0)]
    evs = events if events is not None else [event()]
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": {"name": "m", "framework_id": "4002",
                    "map_setting": {"14": "4002", "1": "0"},
                    "anchor_info": {"gamearea0": [[1, [0, 0, 0]], [2, [100, 0, 100]]]}},
            "components": comps,
            "events": {"count": len(evs), "events": evs,
                       "saved_keys": [e["event_key"] for e in evs if e.get("in_save")]},
            "story": {"stories": []},
            "budget": {"component_nums": len(comps), "registered": len(comps),
                       "max_component_nums": 15000, "occupancy": len(comps)}}


def _layer(rep, key):
    return [l for l in rep["layers"] if l["key"] == key][0]


class TestAnchorsUseTheRealPosition(unittest.TestCase):
    """事件锚点要用可信坐标（`pp` 兜底）：逻辑件的 `p` 是哨兵，两边会算成不同格子。

    实机：交互区 203022 源图 `p=(0,-65536,0)` / `pp=(928,-67.923,-147)`，目标图那件
    `p=(928,-67.923,-147)` ⇒ 旧口径一边算 `203022@0,0`、一边算 `203022@928,-147`，
    一模一样的引用被报成「绑定变更」，真该看的顺序/成员反而被淹在里面。
    """

    def test_sentinel_p_does_not_move_the_anchor(self):
        a = {"g": 1, "i": 203022, "p": [0.0, -65536.0, 0.0],
             "pp": [928.0, -67.923, -147.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 203022, "events": []})}
        b = {"g": 2, "i": 203022, "p": [928.0, -67.923, -147.0], "r": [0, 0, 0],
             "s": [1, 1, 1], "ci": json.dumps({"component_id": 203022, "events": []})}
        self.assertEqual(sn.anchor_of(a, 0.5), sn.anchor_of(b, 0.5))

    def test_no_pp_still_uses_p(self):
        c = {"g": 3, "i": 100001, "p": [10.0, 2.0, 3.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 100001, "events": []})}
        self.assertEqual(sn.anchor_of(c, 0.5)[:2], (100001, 20))


class TestEventLayerScope(unittest.TestCase):
    def test_identical_events_pass(self):
        rep = sn.acceptance_report(snap(), snap())
        self.assertEqual(_layer(rep, "事件")["level"], "ok")
        self.assertIn("保存集合", _layer(rep, "事件")["msg"])

    def test_event_not_in_save_set_is_a_change(self):
        """不在保存集合 ⇒ 重进就没有这条事件。以前这一维根本不比。"""
        a, b = snap(), snap(events=[event(in_save=False)])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [u"开工"])
        self.assertFalse(d["identical"])
        self.assertEqual(_layer(sn.acceptance_report(a, b), "事件")["level"], "fail")

    def test_params_idx_difference_is_a_change(self):
        a = snap(events=[event(refs=[ref(11, params_idx="0")])])
        b = snap(events=[event(refs=[ref(11, params_idx="1")])])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [u"开工"])

    def test_untrusted_ref_still_prints_when_nothing_else_differs(self):
        b = snap(events=[event(refs=[ref(11, key_ok=False)])])
        d = sn.diff_snapshots(snap(), b)
        self.assertTrue(d["events"]["unknown"], msg=d["events"]["untrusted"])
        rep = sn.acceptance_report(snap(), b)
        self.assertEqual(_layer(rep, "事件")["level"], "unknown")
        self.assertFalse(rep["ok"])

    def test_hard_diff_is_not_masked_by_an_untrusted_ref(self):
        """只说"没比成"会把真正的坏消息盖掉 ⇒ 有硬差异时报 fail，但仍要跟着列出没比成的部分。"""
        b = snap(events=[event(in_save=False, refs=[ref(11, key_ok=False)])])
        rep = sn.acceptance_report(snap(), b)
        l = _layer(rep, "事件")
        self.assertEqual(l["level"], "fail", msg=l["msg"])
        self.assertIn("没比成", l["msg"])
        self.assertIn("绑定变更 1", l["msg"])

    def test_old_export_without_in_save_dim_is_unknown(self):
        """老导出没采 `in_save`（实机 `_verify_a.json` 那批是有这维的，但 `key_ok` 就缺）
        ⇒ 那一维不能叫比过了。"""
        def stripped(events):
            for e in events:
                e.pop("in_save", None)
                for r in e.get("refs") or []:
                    r.pop("key_ok", None)
            return events
        a = snap(events=stripped(copy.deepcopy([event()])))
        b = snap(events=stripped(copy.deepcopy([event()])))
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [])
        self.assertIn("in_save", d["events"]["missing_dims"])
        self.assertIn("key_ok", d["events"]["missing_dims"])
        self.assertEqual(_layer(sn.acceptance_report(a, b), "事件")["level"], "unknown")

    def test_only_panel_order_is_warn_not_fail(self):
        a = snap(events=[event(sort_id=0)])
        b = snap(events=[event(sort_id=3)])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["sort_only"], [u"开工"])
        self.assertEqual(d["events"]["changed"], [])
        self.assertFalse(d["identical"], msg="面板顺序不同仍要说'不是逐字相同'")
        self.assertEqual(_layer(sn.acceptance_report(a, b), "事件")["level"], "warn")

    def test_renumbered_ids_with_same_anchor_are_equal(self):
        """落盘 id 换号但锚点相同 ⇒ 引用一致（锚点归一是这一层的老做法，别退化）。"""
        a = snap(comps=[comp(11, 100001, 1.0, 2.0)],
                 events=[event(refs=[ref(11)])])
        b = snap(comps=[comp(901, 100001, 1.0, 2.0)],
                 events=[event(refs=[ref(901)])])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [])
        self.assertEqual(d["events"]["sort_only"], [])

    def test_print_diff_shows_the_two_new_rows(self):
        import contextlib
        buf = io.StringIO()
        a = snap(events=[event(sort_id=0)])
        b = snap(events=[event(sort_id=3, in_save=False)])
        with contextlib.redirect_stdout(buf):
            sn.print_diff(sn.diff_snapshots(a, b))
        out = buf.getvalue()
        self.assertIn("仅面板顺序", out)
        self.assertIn("绑定变更       1", out)

    def test_wired_into_acceptance_and_diff(self):
        """实现但没接进结论 = 交付时照样漏。"""
        import inspect
        src = inspect.getsource(sn.acceptance_report)
        self.assertIn('_norm_event_table', inspect.getsource(sn.diff_snapshots))
        self.assertIn("ev_notes", src)
        self.assertIn("st_notes", src)


@unittest.skipUnless(os.path.exists(DELIVERABLE) and os.path.exists(LIVE_AFTER_REPAIR),
                     "缺春岚两份导出 ⇒ 真实事件数据未执行")
class TestOnRealChunlanEvents(unittest.TestCase):
    def test_real_event_layer_is_clean_after_repair(self):
        with io.open(DELIVERABLE, encoding="utf-8") as f:
            a = json.load(f)
        with io.open(LIVE_AFTER_REPAIR, encoding="utf-8") as f:
            b = json.load(f)
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [])
        self.assertEqual(d["events"]["untrusted"], [],
                         msg="复原前这里是 2 处 key_ok=false（运动器绑错事件）")
        self.assertTrue(all(e["in_save"] for e in
                            ((b.get("events") or {}).get("events") or [])))


if __name__ == "__main__":
    unittest.main()
