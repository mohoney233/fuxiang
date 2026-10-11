#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_progress_audit — 「续做跳过的那件，其实根本没在图上」必须看得见。

长跑靠 progress 续做，`load_progress` 把 `done` 里的 `old_g` 一律跳过。某件如果当时
回执 `placed`、后来却被摘除或被别的操作删掉，续做就永远跳过它 ⇒ 最终图少件，而过程日志
全程"成功"。这条审计拿**实况身份**回对，专门抓这种静默丢失；同时它必须承认自己的适用
边界：重进地图后 ugc_id 全部重编号，此时大面积"找不到"是**审计失效**，不是还原坏了。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402


def _rec(old_g, ugc_id, item=100001, status="placed", run="r1", map_="m"):
    return {"i": old_g, "old_g": old_g, "ugc_id": ugc_id, "item_id": item,
            "status": status, "run": run, "map": map_}


class TestProgressAudit(unittest.TestCase):
    def test_all_claims_present_is_trusted(self):
        recs = [_rec(1, 101), _rec(2, 102), _rec(3, 103)]
        live = {"101": 100001, "102": 100001, "103": 100001}
        a = sr.progress_audit(recs, live)
        self.assertTrue(a["ok"], msg=a)
        self.assertEqual(a["claimed"], 3)
        self.assertFalse(a["stale_session"])

    def test_missing_claim_is_a_failure_not_a_note(self):
        recs = [_rec(1, 101), _rec(2, 102), _rec(3, 103), _rec(4, 104)]
        live = {"101": 100001, "102": 100001, "103": 100001}   # 104 没了
        a = sr.progress_audit(recs, live)
        self.assertEqual([m["ugc_id"] for m in a["missing"]], [104])
        self.assertFalse(a["ok"])
        self.assertFalse(a["stale_session"])   # 少量缺失 = 真丢件，不是会话过期

    def test_mass_missing_means_stale_session_not_broken_restore(self):
        """重进过地图 ⇒ id 全变。这时候印一屏"缺失"会把人带偏。

        ⚠️ 判"会话过期"必须**又有比例又有样本量**：只有几条记录时"全找不到"是真丢件，
        不是重进导致的重编号。
        """
        recs = [_rec(g, 100 + g) for g in range(1, 41)]
        a = sr.progress_audit(recs, {"999": 100001})
        self.assertTrue(a["stale_session"])
        self.assertFalse(a["ok"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.print_audit(a)
        self.assertIn("accept --live", buf.getvalue())
        self.assertIn("重进", buf.getvalue())

    def test_small_sample_with_nothing_found_is_not_called_stale(self):
        """试点只有 1~2 件时不许用"会话过期"糊过去 —— 那是唯一能发现丢件的机会。"""
        recs = [_rec(1, 14999)]
        a = sr.progress_audit(recs, {"101": 100001})
        self.assertFalse(a["stale_session"], msg="1/1 缺失被判成会话过期了")
        self.assertEqual(len(a["missing"]), 1)
        self.assertTrue(a["inconclusive_small"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.print_audit(a)
        self.assertIn("无法", buf.getvalue())

    def test_id_hijacked_by_another_item(self):
        recs = [_rec(1, 101, item=100001)]
        a = sr.progress_audit(recs, {"101": 203900})
        self.assertEqual(a["wrong_item"][0]["actual_item"], 203900)
        self.assertFalse(a["ok"])

    def test_unknown_identity_is_not_counted_as_pass(self):
        """getter 给不出 item_id 时**没比成**，不能说可信。"""
        recs = [_rec(1, 101)]
        a = sr.progress_audit(recs, {"101": None})
        self.assertEqual(a["identity_unknown"], [101])
        self.assertFalse(a["ok"])

    def test_not_placed_records_are_not_losses(self):
        recs = [_rec(1, 101), _rec(2, None, status="ambiguous"),
                _rec(3, 103, status="send_unknown")]
        a = sr.progress_audit(recs, {"101": 100001})
        self.assertEqual({r["old_g"] for r in a["not_placed"]}, {2, 3})
        self.assertEqual(a["missing"], [])
        self.assertTrue(a["ok"])   # 这些本来就要重做，不算"跳过了没摆上的"

    def test_read_progress_records_survives_junk(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.progress.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"old_g": 1, "ugc_id": 101, "status": "placed"}\n')
                f.write("not json\n")
                f.write('[1, 2]\n')          # 不是 dict
            recs = sr.read_progress_records(p)
            self.assertEqual(len(recs), 1)
            self.assertEqual(sr.read_progress_records(os.path.join(td, "nope.jsonl")),
                             [])

    def test_wired_into_the_cli(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn('"--check-progress"', src)
        self.assertIn("progress_audit(", src)
        self.assertIn("fetch_live_index(", src)
        # 别的图的记录要按图名筛掉，不能拿另一张图的 id 来判
        self.assertIn("read_progress_records(", src)


class TestRebuildIdMap(unittest.TestCase):
    """跨会话续做：重进后 id 全变，必须能从当前图实况**重建**映射，而不是拿旧的写。"""

    def _c(self, g, x, z, prop=None):
        return {"g": g, "i": 301001, "p": [x, 0.0, z], "r": [0, 0, 0], "s": [1, 1, 1],
                "ci": json.dumps({"component_id": 100001,
                                  "events": ([] if prop is None else
                                             [{"type": 10000110001, "info": [prop]}])},
                                 ensure_ascii=False)}

    def _snap(self, comps, name="m"):
        return {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t",
                "map": {"name": name, "framework_id": "4002", "map_setting": {}},
                "components": comps, "events": {"count": 0, "events": []},
                "story": {"stories": []}, "budget": {}}

    def test_renumbered_same_content_maps_completely(self):
        a = self._snap([self._c(1, 1.0, 2.0, "甲"), self._c(2, 5.0, 6.0, "乙")])
        b = self._snap([self._c(901, 1.0, 2.0, "甲"), self._c(902, 5.0, 6.0, "乙")])
        rb = sr.rebuild_id_map(a, b)
        self.assertEqual(rb["id_map"], {1: 901, 2: 902})
        self.assertEqual(rb["todo"], [])
        self.assertEqual(rb["extra"], [])

    def test_items_not_yet_placed_land_in_todo(self):
        a = self._snap([self._c(1, 1.0, 2.0, "甲"), self._c(2, 5.0, 6.0, "乙")])
        b = self._snap([self._c(901, 1.0, 2.0, "甲")])
        rb = sr.rebuild_id_map(a, b)
        self.assertEqual(rb["todo"], [2])
        self.assertEqual(rb["mapped"], 1)

    def test_extra_objects_are_reported_not_deleted(self):
        a = self._snap([self._c(1, 1.0, 2.0, "甲")])
        b = self._snap([self._c(901, 1.0, 2.0, "甲"), self._c(999, 30.0, 30.0, "丙")])
        rb = sr.rebuild_id_map(a, b)
        self.assertEqual(rb["extra"], [999])
        self.assertNotIn(999, rb["id_map"].values())

    def test_residue_rows_are_outside_the_universe(self):
        ghost = self._c(777, 1.0, 2.0, "甲")
        ghost["del"] = 1
        a = self._snap([ghost])
        b = self._snap([self._c(901, 1.0, 2.0, "甲")])
        self.assertEqual(sr.rebuild_id_map(a, b)["mapped"], 0)
        self.assertEqual(sr.rebuild_id_map(b, a)["mapped"], 0)


class TestStaleProgressGuard(unittest.TestCase):
    """重进后再跑 `restore --apply`：旧 progress 会跳过摆放却按旧 id 写属性。"""

    def _write_progress(self, tmp, n=25):
        path = os.path.join(tmp, "p.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for g in range(1, n + 1):
                f.write(json.dumps({"i": g, "old_g": g, "ugc_id": 1000 + g,
                                    "item_id": 301001, "status": "placed",
                                    "run": "h1", "map": "m"}) + "\n")
        return path

    def _snap_file(self, tmp, n=25):
        comps = [{"g": g, "i": 301001, "p": [float(g), 0.0, 0.0], "r": [0, 0, 0],
                  "s": [1, 1, 1],
                  "ci": json.dumps({"component_id": 100001, "events": []})}
                 for g in range(1, n + 1)]
        snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t",
                "map": {"name": "m", "framework_id": "4002",
                        "map_setting": {"name": "别的图"}, "anchor_info": {}},
                "components": comps, "events": {"count": 0, "events": [],
                                                "saved_keys": []},
                "story": {"stories": []},
                "budget": {"component_nums": n, "registered": n,
                           "max_component_nums": 15000, "occupancy": n,
                           "max_occupancy": 30000}}
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        return path

    def test_apply_refuses_when_ids_are_renumbered(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            prog = self._write_progress(tmp)
            path = self._snap_file(tmp)
            old_fetch, old_call, old_save = (sr.fetch_live_index, sr.call_edit,
                                             sr.save_and_check)
            old_tgt = sr.fetch_target_info
            sr.fetch_live_index = lambda: ({"1": 301001}, 1)   # 记录的 id 一个都不在
            sr.call_edit = lambda *a, **k: {"ok": True, "components": [],
                                            "value": {}, "status": "placed",
                                            "ugc_id": 777}
            sr.fetch_target_info = lambda: {"framework": "4002",
                                            "map_setting": {"name": "m"},
                                            "bounds": None, "budget": None}
            sr.save_and_check = lambda *a, **k: {"submitted": True}
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sr.main([path, "--apply", "--yes", "--progress", prog])
            finally:
                sr.fetch_live_index, sr.call_edit, sr.save_and_check = (
                    old_fetch, old_call, old_save)
                sr.fetch_target_info = old_tgt
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("过期", out)
            self.assertIn("写到别的组件上", out)
            self.assertIn("--resume-from", out)
            self.assertNotIn("属性回写：成功", out)   # 根本没走到会写错对象的那步

    def test_allow_stale_progress_is_the_named_escape(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn('"--allow-stale-progress"', src)
        self.assertIn("a.allow_stale_progress", src)
        self.assertIn('"--resume-from"', src)
        self.assertIn("rebuild_id_map(snap, other)", src)


    def test_resume_from_file_errors_are_explained_not_tracebacks(self):
        """给错路径要印人能看懂的话 + 返回码，不许甩 JSONDecodeError 栈。"""
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp, n=2)
            buf = io.StringIO()
            old_call = sr.call_edit
            sr.call_edit = lambda *a, **k: {"ok": True, "components": [],
                                            "value": {}, "index": {}}
            try:
                with contextlib.redirect_stdout(buf):
                    rc = sr.main([path, "--apply", "--yes", "--resume-from",
                                  os.path.join(tmp, "nope.json")])
            finally:
                sr.call_edit = old_call
            self.assertEqual(rc, 2)
            self.assertIn("读不到", buf.getvalue())
            self.assertIn("export --out", buf.getvalue())


class TestLiveIndexIsPaged(unittest.TestCase):
    """`fetch_live_index` 必须**分页取全**：回传层 `_safe` 把 dict 截到 200 条。

    2026-10-02 实机（目标图 6031 件）：这条通道只回 200 条 ⇒ 5700 件"已摆放"里
    5613 件被判成"图上找不到 ⇒ 会话过期"，续做被 `--allow-stale-progress` 闸门拦下。
    真因是回传截断，不是重进重编号 —— 拿被截断的表去判"还在不在"必然冤假。
    """

    def test_pages_cover_every_gid_and_never_exceed_the_cap(self):
        import re
        calls = []

        def fake(op, args, timeout=90):
            m = re.search(r"_lo, _hi = (\d+), (\d+)", args["code"])
            lo, hi = int(m.group(1)), int(m.group(2))
            calls.append((lo, hi))
            idx = {str(g): (301001 if g % 3 else 203911)
                   for g in range(0, 6032) if lo <= g <= hi}
            return {"ok": True, "value": {"index": idx, "n": len(idx),
                                          "total": 6031, "max_gid": 6031}}

        old = sr.call_edit
        sr.call_edit = fake
        try:
            idx, total = sr.fetch_live_index()
        finally:
            sr.call_edit = old
        self.assertEqual(total, 6031)
        self.assertEqual(set(idx), set(str(g) for g in range(0, 6032)),
                         "分页要把 0..max_gid 全部覆盖，一件不漏")
        self.assertGreater(len(calls), 1, "6000 件必须分多页取")
        self.assertTrue(all(hi - lo + 1 <= 200 for lo, hi in calls),
                        "一页不许超过回传层的 200 条上限")
        self.assertTrue(all(calls[i][0] > calls[i - 1][0] for i in range(1, len(calls))),
                        "按 gid 递增分页，不许原地打转")

    def test_unreadable_index_is_not_pretended(self):
        old = sr.call_edit
        sr.call_edit = lambda *a, **k: {"ok": True, "value": {"nope": 1}}
        try:
            self.assertEqual(sr.fetch_live_index(), ({}, None),
                             "读不到就报读不到，不许拿半张表当真表")
        finally:
            sr.call_edit = old

    def test_big_map_progress_is_not_called_a_stale_session(self):
        records = [{"i": g, "old_g": g, "ugc_id": g, "item_id": 301001,
                    "status": "placed"} for g in range(1, 5701)]
        live = {str(g): 301001 for g in range(1, 5701)}
        aud = sr.progress_audit(records, live)
        self.assertEqual(aud["missing"], [])
        self.assertFalse(aud["stale_session"])
        self.assertTrue(aud["ok"])


if __name__ == "__main__":
    unittest.main()
