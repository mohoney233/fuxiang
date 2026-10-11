#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_check — 交付前"这份快照能不能拿去还原"的离线判据。

这些结论全部来自 2026-09-30/10-01 跨账号交付里真踩到的失效面：
`<deep>` 截断、ugc_id 重复、共用单元的身份、控件坐标只在袋里、删除残骸被当内容、
组合件成员指向文件外、缺 framework_id 对不上地基。
体检必须跑在**任何写入之前** —— 摆到一半才发现等于把目标图弄脏了。
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402


def _comp(g, i, cid, p=(1.0, 2.0, 3.0), ci_extra=None, events=None):
    ci = {"component_id": cid, "events": events or []}
    ci.update(ci_extra or {})
    return {"g": g, "i": i, "p": list(p), "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps(ci, ensure_ascii=False)}


def _snap(comps, props=True):
    mp = {"name": "m", "framework_id": "4002",
          "map_setting": {"14": "4002"}, "anchor_info": {"gamearea0": []}}
    if props:
        # 地图属性层（默认背景音乐等）：采到了但什么都没设 ⇒ 干净；**没这一层**要报 warn
        mp["props"] = {k: {"present": True, "value": None} for k in sn.PROPS_KEYS}
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": mp,
            "components": comps,
            "events": {"count": 0, "events": [], "saved_keys": []},
            "story": {"stories": []},
            "budget": {"component_nums": len(comps), "max_component_nums": 15000,
                       "occupancy": 100}}


def _levels(v):
    return {i["key"]: i["level"] for i in v["items"]}


class TestValidateSnapshot(unittest.TestCase):
    def test_clean_file_is_deliverable(self):
        v = sn.validate_snapshot(_snap([_comp(1, 301001, 100001)]))
        self.assertTrue(v["ok"], v["blockers"])
        self.assertEqual(v["warnings"], [])

    def test_missing_map_props_layer_warns(self):
        """没采地图属性层（旧导出）必须**在交付前**就报出来。

        2026-10-01：春岚2 交付时七层全绿，用户随后发现试玩里"开局没有默认背景音乐"——
        BGM 住在 `ugc_props_info`，当时那层根本没采，所以没人能提前一天说出这件事。
        """
        v = sn.validate_snapshot(_snap([_comp(1, 301001, 100001)], props=False))
        self.assertEqual(_levels(v)["map_props"], "warn")
        msgs = [w["msg"] for w in v["warnings"]]
        self.assertTrue(any("背景音乐" in m for m in msgs), msgs)

    def test_duplicate_ugc_id_blocks(self):
        """两张同 id 的件会让 id 映射张冠李戴（摆错对象还看不出）。"""
        s = _snap([_comp(7, 301001, 100001, p=(1, 2, 3)),
                   _comp(7, 301001, 100001, p=(9, 9, 9))])
        v = sn.validate_snapshot(s)
        self.assertFalse(v["ok"])
        self.assertEqual(_levels(v)["identity"], "blocker")

    def test_deep_truncation_blocks(self):
        c = _comp(1, 301001, 100001)
        c["ci"] = json.dumps({"component_id": 100001,
                              "events": [{"type": 10000110006, "info": ["<deep>"]}]})
        v = sn.validate_snapshot(_snap([c]))
        self.assertEqual(_levels(v)["ci_depth"], "blocker")

    def test_unparseable_ci_blocks(self):
        c = _comp(1, 301001, 100001)
        c["ci"] = "{不是 JSON"
        v = sn.validate_snapshot(_snap([c]))
        self.assertEqual(_levels(v)["ci_parse"], "blocker")

    def test_widget_without_canvas_position_warns(self):
        """控件（200000）的坐标只在属性袋 POS 里；没有它就会被摆到 (0,0)。"""
        w = _comp(362, 5201002, 200000, p=(0.0, -65536.0, 0.0))
        self.assertEqual(_levels(sn.validate_snapshot(_snap([w])))["widget_pos"], "warn")
        w2 = _comp(363, 5201002, 200000, p=(0.0, -65536.0, 0.0),
                   events=[{"type": 20000010001, "info": [451.6, 625.2]}])
        v = sn.validate_snapshot(_snap([w2]))
        self.assertEqual(_levels(v)["widget_pos"], "ok")
        self.assertNotIn("sentinel", [i["key"] for i in v["items"] if i["level"] == "info"])

    def test_residue_rows_are_warned_not_silent(self):
        c = _comp(1, 301001, 100001)
        c["del"] = 1
        v = sn.validate_snapshot(_snap([c]))
        self.assertEqual(_levels(v)["residue"], "warn")

    def test_references_outside_the_file_are_warned(self):
        """组合件成员/成组互指指向文件外 ⇒ 还原后是悬空引用或半残的组。"""
        comp = _comp(5, 203900, 203900,
                     events=[{"type": 20390003001, "info": [[11, 12, 999]]}])
        pair = _comp(6, 204041, 204041, ci_extra={"group_components": [77]})
        v = sn.validate_snapshot(_snap([comp, pair]))
        lv = _levels(v)
        self.assertEqual(lv["member_refs"], "warn")
        self.assertEqual(lv["group_refs"], "warn")

    def test_missing_framework_warns(self):
        s = _snap([_comp(1, 301001, 100001)])
        s["map"].pop("framework_id")
        s["map"]["map_setting"] = {}
        s["map"].pop("anchor_info")
        lv = _levels(sn.validate_snapshot(s))
        self.assertEqual(lv["framework"], "warn")
        self.assertEqual(lv["anchor"], "warn")

    def test_scale_out_of_slider_range_is_info(self):
        c = _comp(1, 203900, 203900)
        c["s"] = [0.05, 0.2, 0.02]
        self.assertEqual(_levels(sn.validate_snapshot(_snap([c])))["scale_range"], "info")


class TestRestoreRefusesBlockedFile(unittest.TestCase):
    """体检有阻塞项时，`--apply` 必须**在碰游戏之前**就拒掉。"""

    def test_apply_stops_before_any_write(self):
        import snapshot_restore as sr
        s = _snap([_comp(1, 301001, 100001, p=(1, 2, 3)),
                   _comp(1, 301001, 100001, p=(5, 6, 7))])       # 重复 gid
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False)
            calls = []
            old = sr.call_edit
            sr.call_edit = lambda *a, **k: calls.append(a) or {}
            try:
                rc = sr.main([path, "--apply", "--yes"])
            finally:
                sr.call_edit = old
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])          # 一次桥接调用都没发出 ⇒ 没有写入


class TestResidueIdentities(unittest.TestCase):
    """残骸警告要给**身份**，不能只给条数：数得出几件、认不出是哪件，等发现时已存进去了。

    2026-10-01 实机：导出警告印 `{'unit': 7, 'component': 4}`，而"退出编辑器会自动保存"
    意味着这批删除会被提交 —— 里面就躺着一个有名字的运动器。
    """

    def _del(self, g, cid, name=None):
        evs = [] if name is None else [{"type": 20391102001, "info": [name]}]
        c = _comp(g, 301001, cid, events=evs)
        c["del"] = 1
        return c

    def test_names_are_extracted_without_a_slot_table(self):
        items = sn.residue_identities(_snap([self._del(14982, 203911, "春嵐_上映_chorus-split-15"),
                                             self._del(15007, 200000)]))
        self.assertEqual(items[0]["g"], 14982)
        self.assertEqual(items[0]["name"], "春嵐_上映_chorus-split-15")
        self.assertIsNone(items[1]["name"])

    def test_pure_numeric_strings_are_not_names(self):
        """属性袋里全是数字串（id/坐标），把它们当"名字"会让警告变成噪声。"""
        self.assertIsNone(sn._display_name_of(
            {"events": [{"type": 1, "info": ["1234"]}, {"type": 2, "info": ["1.5"]}]}))
        self.assertEqual(sn._display_name_of(
            {"events": [{"type": 1, "info": ["1234"]},
                        {"type": 2, "info": ["观赏MV"]}]}), "观赏MV")

    def test_residue_message_names_the_pending_deletion(self):
        v = sn.validate_snapshot(_snap([self._del(14982, 203911, "春嵐_上映_chorus-split-15")]))
        msg = [i["msg"] for i in v["items"] if i["key"] == "residue"][0]
        self.assertIn("14982", msg)
        self.assertIn("春嵐_上映_chorus-split-15", msg)
        self.assertIn("自动保存", msg)      # 必须说清"会被提交"，不能只说"重进后消失"

    def test_summarize_warning_lists_identities(self):
        s = _snap([self._del(1, 203911, "甲"), self._del(2, 203911, "乙")])
        w = sn.summarize(s)["warnings"]
        joined = " ".join(w)
        self.assertIn("甲", joined)
        self.assertIn("乙", joined)

    def test_limit_is_capped_with_a_remainder_count(self):
        s = _snap([self._del(g, 203911, "n%d" % g) for g in range(1, 7)])
        line = sn.format_residue_identities(sn.residue_identities(s), 6)
        self.assertIn("另有 2 件未列出", line)


    def test_ui_native_layer_is_named_as_unverified_not_impossible(self):
        """UI_NATIVE 默认不摆要**报出来**，且措辞不许说成"做不到"（通道实测放行）。"""
        s = _snap([_comp(1, 6000001, sn.UI_NATIVE_TYPE), _comp(2, 301001, 100001)])
        v = sn.validate_snapshot(s)
        rows = [i for i in v["items"] if i["key"] == "ui_native"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "info")
        self.assertIn("落库未证", rows[0]["msg"])
        self.assertIn("--include-ui", rows[0]["msg"])
        self.assertTrue(v["ok"], msg="这一层是提示，不该把交付判死")

    def test_no_ui_native_line_when_absent(self):
        v = sn.validate_snapshot(_snap([_comp(1, 301001, 100001)]))
        self.assertEqual([i for i in v["items"] if i["key"] == "ui_native"], [])


class TestPendingDeletionIdentities(unittest.TestCase):
    """采集侧（游戏内字符串）也要能认出待删除袋里**是哪几件**，不只是条数。"""

    def test_formats_gid_cid_and_name(self):
        line = sn.format_pending_deletions(
            [{"bucket": "component", "id": 14982, "gid": 14982, "cid": 203911,
              "name": "春嵐_上映_chorus-split-15"}], 1)
        self.assertIn("14982", line)
        self.assertIn("203911", line)
        self.assertIn("春嵐_上映_chorus-split-15", line)

    def test_unit_bucket_row_without_component_is_explained(self):
        """'unit' 桶存的是单元 uid：组件表里已经找不到它时，不能印成 `gid=None`。"""
        line = sn.format_pending_deletions(
            [{"bucket": "unit", "id": 1014999, "gid": None, "cid": None, "name": None}], 1)
        self.assertIn("unit桶 id=1014999", line)
        self.assertIn("不在组件表里", line)

    def test_remainder_is_counted(self):
        items = [{"bucket": "component", "id": g, "gid": g, "cid": 1, "name": None}
                 for g in range(1, 5)]
        line = sn.format_pending_deletions(items, 11)
        self.assertIn("另有 7 件未列出", line)

    def test_falls_back_when_identities_unavailable(self):
        self.assertIn("桶计数", sn.format_pending_deletions([], 3))

    def test_budget_code_string_collects_them(self):
        """采集写在游戏侧执行的字符串里，普通用例发现不了它被改回去 ⇒ 直接锁源码。"""
        for key in ("pending_deletions", "pending_deletions_total", "del_unit_uids"):
            self.assertIn(key, sn.CODE_BUDGET)
        # 'unit' 桶要落到"该单元上的组件"，不能只报 uid
        self.assertIn("_by_uid", sn.CODE_BUDGET)

    def test_summarize_warning_names_pending_deletions(self):
        s = _snap([_comp(1, 100001, 100001)])
        s["budget"] = {"del_unit_uids": {"component": [14982]},
                       "pending_deletions": [
                           {"bucket": "component", "id": 14982, "gid": 14982,
                            "cid": 203911, "name": "甲"}],
                       "pending_deletions_total": 1}
        w = " ".join(sn.summarize(s)["warnings"])
        self.assertIn("待删除", w)          # 旧口径的"不可跨会话比较"语义不许丢
        self.assertIn("14982", w)
        self.assertIn("甲", w)


    def test_ref_candidates_are_declared_not_remapped(self):
        """白名单外的字段里出现"等于某个组件 gid"的整数 ⇒ 报成候选，但**不参与换号**。

        春岚实测 27 个这种字段，几乎全是撞号（模型号/枚举/路径坐标/阵营序号）：
        跟着换号就会把真差异洗白（`accept` 的归一只在 `ID_REF_PROPERTIES` 上做）。
        但换一张图时某个字段可能是真引用 ⇒ 必须说出来，并给出分辨办法。
        """
        a = _snap([_comp(5, 100001, 100001),
                   _comp(9, 10004001, 10004001,
                         events=[{"type": "1000400199999", "info": [[5]]}])])
        res = sn.validate_snapshot(a)
        row = [i for i in res["items"] if i["key"] == "ref_candidates"]
        self.assertTrue(row, msg=res["items"])
        self.assertEqual(row[0]["level"], "info")
        self.assertIn("1000400199999", row[0]["msg"])
        self.assertIn("不对这些字段换号", row[0]["msg"].replace("**", ""))
        self.assertIn("两次不同会话", row[0]["msg"], msg="要给出分辨撞号与真引用的办法")

    def test_member_ref_fields_are_not_listed_as_candidates(self):
        a = _snap([_comp(5, 100001, 100001),
                   _comp(9, 203900, 203900,
                         events=[{"type": "20390018701", "info": [[5]]}])])
        res = sn.validate_snapshot(a)
        self.assertNotIn("ref_candidates", {i["key"] for i in res["items"]})


    def test_check_prints_a_copy_paste_command(self):
        """开关是分几行抄的就会漏 —— 体检直接把整条命令拼出来。"""
        import argparse
        import contextlib
        v = _snap([_comp(1, 6000001, sn.UI_NATIVE_TYPE if hasattr(sn, "UI_NATIVE_TYPE") else 200001,
                         p=(0.0, -65536.0, 0.0)),
                   _comp(2, 10007001, 10007001)])
        v["story"] = {"stories": [{"name": u"S", "dialogues": []}],
                      "orphan_dialogues": [], "orphan_choices": []}
        v["map"]["map_setting"] = {"14": "4002", "1": "0"}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(v, f, ensure_ascii=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sn.cmd_check(argparse.Namespace(file=path))
        out = buf.getvalue()
        self.assertIn("python cli/snapshot_restore.py", out)
        self.assertIn("--include-ui", out)
        self.assertIn("--with-story", out)
        line = [l for l in out.splitlines() if l.strip().startswith("python cli/snapshot_restore.py")]
        self.assertEqual(len(line), 1, msg=out)


    def test_orphan_story_nodes_are_declared_as_not_rebuilt(self):
        """孤儿对话/选项：**导出里有、导入不重建**（`story_import` 只遍历 stories）。

        春岚这份是 0，所以今天不影响交付；但换一张有孤儿的图就会"跑完全程、目标图少几件"，
        而且只有重进回对才看得见 ⇒ 必须在体检就说清"没有开关能救它"。
        """
        v = _snap([_comp(1, 100001, 100001)])
        v["story"] = {"stories": [],
                      "orphan_dialogues": [{"ugc_id": 301, "sort_id": 0, "content": u"没人挂"}],
                      "orphan_choices": [{"ugc_id": 302, "sort_id": 0, "content": u"没父"}]}
        res = sn.validate_snapshot(v)
        row = [i for i in res["items"] if i["key"] == "story_orphans"]
        self.assertEqual(row[0]["level"], "warn")
        self.assertIn("不会重建", row[0]["msg"])
        self.assertIn("对话 1", row[0]["msg"])
        self.assertIn("选项 1", row[0]["msg"])

    def test_no_orphans_says_so(self):
        res = sn.validate_snapshot(_snap([_comp(1, 100001, 100001)]))
        row = [i for i in res["items"] if i["key"] == "story_orphans"]
        self.assertEqual(row[0]["level"], "ok")

    def test_restore_also_prints_the_orphan_gap(self):
        """体检说了不算：真正跑 `restore` 的人可能只看预检输出，那一侧也要印。"""
        import inspect
        import snapshot_restore as sr
        src = inspect.getsource(sr.main)
        self.assertIn("orphan_dialogues", src)
        self.assertIn("任何开关都不会重建", src)


class TestSelfConsistency(unittest.TestCase):
    """截断/手拼过的 JSON 是最阴的交付物：其余每条检查都能全过，然后安静少摆几百件。"""

    def _full(self, n=3):
        v = _snap([_comp(i, 100001, 100001, p=(float(i), 0.0, 0.0)) for i in range(1, n + 1)])
        v["map"]["total_components"] = n
        v["map"]["type_distribution"] = {"100001": n}
        v["counts"] = {"components": n, "events": 0, "stories": 0}
        return v

    def test_consistent_file_passes(self):
        res = sn.validate_snapshot(self._full())
        row = [i for i in res["items"] if i["key"] == "self_consistency"]
        self.assertEqual(row[0]["level"], "ok")

    def test_truncated_component_list_is_a_blocker(self):
        v = self._full(3)
        v["components"] = v["components"][:2]          # 掉了最后一行，但计数没改
        res = sn.validate_snapshot(v)
        row = [i for i in res["items"] if i["key"] == "self_consistency"]
        self.assertEqual(row[0]["level"], "blocker", msg=row)
        self.assertFalse(res["ok"])
        self.assertIn("安静", row[0]["msg"])

    def test_only_type_distribution_mismatch_is_caught(self):
        """三个数各自独立：只漂一个也要抓到（截断常只改到其中一处）。"""
        v = self._full(3)
        v["map"]["type_distribution"] = {"100001": 99}
        row = [i for i in sn.validate_snapshot(v)["items"]
               if i["key"] == "self_consistency"]
        self.assertEqual(row[0]["level"], "blocker")
        self.assertIn("type_distribution", row[0]["msg"])

    def test_old_export_without_counters_is_info_not_failure(self):
        v = self._full(2)
        del v["counts"]
        v["map"].pop("total_components")
        v["map"].pop("type_distribution")
        row = [i for i in sn.validate_snapshot(v)["items"]
               if i["key"] == "self_consistency"]
        self.assertEqual(row[0]["level"], "info")

    def test_real_deliverables_are_self_consistent(self):
        for name in ("chunlan_1to1_20260930.json", "chunlan_recheck_20261001.json"):
            path = os.path.join(ROOT, "snapshots", name)
            if not os.path.exists(path):
                continue
            with io.open(path, encoding="utf-8") as f:
                d = json.load(f)
            row = [i for i in sn.validate_snapshot(d)["items"]
                   if i["key"] == "self_consistency"]
            self.assertEqual(row[0]["level"], "ok", msg="%s：%s" % (name, row))


class TestCaptureScopeIsDeclared(unittest.TestCase):
    """体检要说清「这份文件能验收到什么程度」，不然对方拿着老文件跑 accept 只会得到 unknown。

    实机：`snapshots/_verify_a.json` 的剧情节点只有 `ugc_id/owner_uid/dialogues` 三个键，
    引用里也没有 `key_ok`。拿去和新导出比，比对层只能报「这些列没比成」——那不如在交付
    之前就说明白。
    """

    def test_old_story_capture_is_flagged(self):
        v = _snap([_comp(1, 100001, 100001)])
        v["story"] = {"format": "fuxiang-story", "version": 1,
                      "stories": [{"ugc_id": 1, "owner_uid": 2,
                                   "dialogues": [{"ugc_id": 3, "owner_uid": 4}]}],
                      "orphan_dialogues": [], "orphan_choices": []}
        res = sn.validate_snapshot(v)
        self.assertEqual(_levels(res).get("story_capture"), "warn")
        self.assertTrue(any("没采到" in i["msg"] for i in res["items"]
                            if i["key"] == "story_capture"))

    def test_full_story_capture_is_only_informational(self):
        v = _snap([_comp(1, 100001, 100001)])
        v["story"] = {"format": "fuxiang-story", "version": 1,
                      "stories": [{"ugc_id": 1, "owner_uid": 2, "name": u"S",
                                   "hide_player_units": 1, "effect_type": 1051,
                                   "dialogues": []}],
                      "orphan_dialogues": [], "orphan_choices": []}
        res = sn.validate_snapshot(v)
        self.assertEqual(_levels(res).get("story_capture"), "info")

    def test_missing_event_dims_block_honest_acceptance(self):
        v = _snap([_comp(1, 100001, 100001)])
        v["events"] = {"count": 1, "saved_keys": [],
                       "events": [{"event_key": u"E", "refs": [
                           {"ugc_id": 1, "property_id": "10000110006",
                            "current_value": "['x']", "index": 3}]}]}
        res = sn.validate_snapshot(v)
        self.assertEqual(_levels(res).get("event_dims"), "warn")
        v["events"]["events"][0]["in_save"] = True
        v["events"]["events"][0]["refs"][0]["key_ok"] = True
        self.assertEqual(_levels(sn.validate_snapshot(v)).get("event_dims"), "ok")

    def test_no_events_no_row(self):
        res = sn.validate_snapshot(_snap([_comp(1, 100001, 100001)]))
        self.assertNotIn("event_dims", _levels(res))


if __name__ == "__main__":
    unittest.main()
