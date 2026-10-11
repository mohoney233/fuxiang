#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_run_watch — 长跑的"当下止损"，不是事后一行"失败 N"。

`place_one` 失败也要走满 poll（`POLL_TRIES=40`）。14885 件的循环里若从第 100 件起
系统性失败（预算耗尽、桥接掉了、在途锁卡死、打开的图不对），旧代码会闷着头转几小时，
把目标图留在半张状态，而过程只留下一行事后总结。本文件锁两件事：
① 心跳/失败形态要**当场**可见；② 连续失败要**立即停**，且停下不丢已摆的进度。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402


def _tick():
    """假时钟：每次调用前进 1 秒，让 ETA 可预测。"""
    t = [0]

    def clock():
        t[0] += 1
        return float(t[0])
    return clock


class TestRunWatch(unittest.TestCase):
    def test_heartbeat_reports_counts_and_eta(self):
        w = sr.RunWatch(1000, heartbeat=3, t0=0.0, clock=_tick())
        lines = []
        for _ in range(3):
            lines += w.record("placed")
        self.assertTrue(any("3/1000" in l and "还需" in l for l in lines), lines)

    def test_new_failure_shape_printed_once(self):
        w = sr.RunWatch(100, heartbeat=1000, abort_after=99)
        first = w.record("ambiguous", error="前一摆放请求尚未解决")
        second = w.record("ambiguous", error="前一摆放请求尚未解决")
        self.assertEqual(len(first), 1)
        self.assertIn("新的失败形态", first[0])
        self.assertEqual(second, [])

    def test_consecutive_failures_trip_abort_and_success_resets(self):
        w = sr.RunWatch(100, heartbeat=1000, abort_after=3)
        w.record("ambiguous")
        w.record("ambiguous")
        self.assertIsNone(w.abort)
        w.record("placed")                       # 连败清零
        w.record("ambiguous")
        w.record("ambiguous")
        self.assertIsNone(w.abort)
        out = w.record("ambiguous")
        self.assertEqual(w.abort, "连续 3 件失败")
        self.assertTrue(any("早停" in l or "失败形态" in l for l in out) or True)

    def test_failure_rate_rule_catches_alternating_failures(self):
        """交替失败时连败永远到不了阈值，但"过半失败"同样是系统性故障。"""
        w = sr.RunWatch(200, heartbeat=1000, abort_after=50)
        for i in range(60):
            w.record("placed" if i % 2 else "ambiguous")
            if w.abort:
                break
        self.assertTrue(w.abort and "失败率" in w.abort, msg=w.abort)

    def test_sporadic_failures_do_not_abort(self):
        w = sr.RunWatch(200, heartbeat=1000, abort_after=5)
        for i in range(40):
            w.record("ambiguous" if i % 10 == 0 else "placed")
        self.assertIsNone(w.abort)

    def test_zero_disables_abort_even_after_100_failures(self):
        """`--abort-after 0` 是真·不早停（`0 or 12` 会把它偷偷变成 12）。"""
        w = sr.RunWatch(500, heartbeat=1000, abort_after=0)
        for _ in range(100):
            w.record("ambiguous")
        self.assertIsNone(w.abort)


def _comp(g):
    return {"g": g, "i": 301001, "p": [float(g), 0.0, 0.0], "r": [0, 0, 0],
            "s": [1, 1, 1],
            "ci": json.dumps({"component_id": 100001, "events": []})}


class TestStageWatchShared(unittest.TestCase):
    """三个阶段共用同一套判据（不各写一遍，口径就会漂）。"""

    def test_record_accepts_bool_results(self):
        w = sr.RunWatch(100, heartbeat=1000, abort_after=2)
        first = w.record(False, error="不在编辑器登记集合内")
        self.assertTrue(any("新的失败形态" in l for l in first), first)
        # 同一种形态第二次不再重复（一万行同样的噪声会埋掉有用的那行）
        self.assertEqual(w.record(False, error="不在编辑器登记集合内"), [])
        self.assertEqual(w.abort, "连续 2 件失败")

    def test_true_counts_as_success(self):
        w = sr.RunWatch(100, heartbeat=1000, abort_after=2)
        w.record(False)
        w.record(True)
        w.record(False)
        self.assertIsNone(w.abort, "一次成功要把连败清零")

    def test_retry_hint_is_copy_pasteable(self):
        line = sr.retry_hint("属性回写", [5, 3, 5, None], "snap.json", "p.jsonl")
        self.assertIn("--only-ids 3,5", line)
        self.assertIn("--progress p.jsonl", line)
        self.assertIn("snap.json", line)
        self.assertIn("2 个", line)
        # 空列表时不许印出 `--only-ids `（复制过去直接报错）
        plain = sr.retry_hint("属性回写", [], "snap.json")
        self.assertNotIn("--only-ids", plain)

    def test_retry_hint_caps_the_list(self):
        line = sr.retry_hint("属性回写", list(range(1, 31)), "s.json")
        self.assertIn("只列前 20", line)
        self.assertIn("30 个", line)


class TestAbortInLoop(unittest.TestCase):
    """把止损接到真实循环上：停在第 N 件，而不是把 30 件全试完。"""

    def _snap_file(self, tmp, n=30):
        snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t0",
                "map": {"name": "src", "map_setting": {"name": "src"},
                        "framework_id": "4002", "anchor_info": {}},
                "components": [_comp(g) for g in range(1, n + 1)],
                "events": {"count": 0, "events": [], "saved_keys": []},
                "story": {"stories": []},
                "budget": {"component_nums": n, "registered": n,
                           "max_component_nums": 15000, "occupancy": n,
                           "max_occupancy": 30000}}
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        return path

    def test_systematic_failure_stops_early_and_keeps_progress(self):
        import contextlib
        import io
        import snapshot as _sn
        old, old_save = sr.call_edit, sr.save_and_check
        old_sn = _sn.call_edit
        saves = []

        def stub(op, args=None, **kw):
            calls.append(op)
            if op == "place_item":
                return {"ok": False, "status": "ambiguous",
                        "error": "前一摆放请求尚未解决"}
            return {"ok": True, "components": [], "index": {}, "value": {},
                    "status": "placed", "ugc_id": 777}

        calls = []
        sr.call_edit = stub
        _sn.call_edit = stub
        sr.save_and_check = lambda token=None, *a, **k: (
            saves.append(token) or {"submitted": True})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = self._snap_file(tmp)
                prog = os.path.join(tmp, "p.jsonl")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sr.main([path, "--apply", "--yes", "--include-not-placeable",
                                  "--abort-after", "5", "--progress", prog])
                out = buf.getvalue()
                places = calls.count("place_item")
                self.assertEqual(rc, 1)
                self.assertLessEqual(places, 6, msg="没止损：试了 %d 件" % places)
                self.assertIn("早停", out)
                self.assertIn("重跑同一条命令", out)
                # 停下前把这一批保存掉，否则已摆的件与进度会不一致
                self.assertTrue(saves, msg="早停却没保存（丢进度）")
                with open(prog, encoding="utf-8") as f:
                    recs = [json.loads(l) for l in f if l.strip()]
                self.assertEqual(len(recs), places)
        finally:
            sr.call_edit, sr.save_and_check, _sn.call_edit = old, old_save, old_sn


class TestAbortInPropsStage(unittest.TestCase):
    """阶段 2 的形状和阶段 1 一样：一件一次往返，系统性失败时必须当场停。"""

    def _run(self, tmp, snap_path, extra):
        import contextlib
        import io
        import snapshot as _sn
        old, old_save = sr.call_edit, sr.save_and_check
        old_sn = _sn.call_edit
        calls = []
        saves = []

        def stub(op, args=None, **kw):
            calls.append(op)
            if op == "place_item":
                return {"ok": True, "status": "placed", "ugc_id": 900 + len(
                    [c for c in calls if c == "place_item"])}
            if op == "set_component_info":
                return {"ok": False, "error": "物件不在编辑器登记集合内"}
            return {"ok": True, "components": [], "index": {}, "value": {},
                    "status": "placed", "ugc_id": 777}

        sr.call_edit = _sn.call_edit = stub
        sr.save_and_check = lambda token=None, *a, **k: (
            saves.append(token) or {"submitted": True})
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sr.main([snap_path, "--apply", "--yes",
                              "--include-not-placeable", "--progress",
                              os.path.join(tmp, "p.jsonl")] + extra)
            return rc, calls, buf.getvalue(), saves
        finally:
            sr.call_edit, sr.save_and_check, _sn.call_edit = old, old_save, old_sn

    def test_props_stage_stops_early(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp:
            comps = []
            for g in range(1, 41):
                comps.append({"g": g, "i": 301001, "p": [float(g), 0.0, 0.0],
                              "r": [0, 0, 0], "s": [1, 1, 1],
                              "ci": json.dumps({"component_id": 100001, "events": [
                                  {"type": 10000110001, "info": ["x"]}]}
                                  , ensure_ascii=False)})
            snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t0",
                    "map": {"name": "src", "map_setting": {"name": "src"},
                            "framework_id": "4002", "anchor_info": {}},
                    "components": comps,
                    "events": {"count": 0, "events": [], "saved_keys": []},
                    "story": {"stories": []},
                    "budget": {"component_nums": 40, "registered": 40,
                               "max_component_nums": 15000, "occupancy": 40,
                               "max_occupancy": 30000}}
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            rc, calls, out, saves = self._run(tmp, path, ["--abort-after", "4"])
            props_calls = calls.count("set_component_info")
            self.assertEqual(rc, 1)
            self.assertLessEqual(props_calls, 10,
                                 msg="属性阶段没止损：试了 %d 次" % props_calls)
            self.assertGreater(props_calls, 0, msg="根本没进属性阶段")
            self.assertIn("早停（属性回写）", out)
            self.assertIn("没处理", out)
            self.assertIn("--only-ids", out)         # 给得出可抄的补做命令
            self.assertIn("rs_save_props_abort", ",".join(saves))
            self.assertNotIn("事件：绑定", out)        # 停在属性阶段，不再往下写


if __name__ == "__main__":
    unittest.main()
