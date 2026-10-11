#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_story_layer — 剧情层「逐字一致」这句话到底比了什么。

交付快照里每条剧情带 13 个可比字段（生效类型、可触发次数、隐藏玩家单位、仅自己触发、
仅房主生效、允许退出、触发播放/结束/立刻结束的事件名…），对话带 14 个（说话人类型、
演出类型、文本类型/字号/颜色/对齐、显示退出、起止事件…），选项带 5 个。
而 `diff/accept` 的剧情层以前只比 **对话序 + 正文 + 两个跳转 id**，却印
「剧情/对话/选项逐字一致」——春岚那段剧情正是"开场观影"，`hide_player_units`
（观影时藏不藏人）改了都看不出来。

本文件锁四件事：
  1) 这些字段现在**逐个参与比对**，并点名到 `剧情《X》·hide_player_units 1→0`；
  2) 易变的 `ugc_id/owner_uid/edit_time` 不参与（否则每次重进都报差异）；
  3) 靠 id 相连的 `story_id/dialogue_id/jump_*` **解析成名字**再比 —— 跨会话一比全是假差异
     会让人学会忽略整份报告；解析不到就报 `unknown`，不许当一致；
  4) 字段清单与桥接真源 `bridge/fuxiang_story.py::FIELDS` 逐字对齐（AST 比对，
     与 `GROUP_CREATE_TYPES` 同一手法），防止比对侧悄悄缩水。
"""
import ast
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


def story_doc(stories=None, orphan_dialogues=None, orphan_choices=None):
    return {"format": "fuxiang-story", "version": 1,
            "stories": stories or [], "orphan_dialogues": orphan_dialogues or [],
            "orphan_choices": orphan_choices or []}


def snap(story, name="m"):
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": {"name": name, "framework_id": "4002",
                    "map_setting": {"14": "4002", "1": "0"},
                    "anchor_info": {"gamearea0": [[1, [0, 0, 0]], [2, [100, 0, 100]]]}},
            "components": [],
            "events": {"count": 0, "events": [], "saved_keys": []},
            "story": story,
            "budget": {"component_nums": 0, "registered": 0,
                       "max_component_nums": 15000, "occupancy": 0}}


BASE_STORY = {
    "ugc_id": 118, "owner_uid": 1000073, "name": u"开场观影", "edit_time": 111,
    "enable_quit": 1, "effect_type": 1051, "effect_count": 1,
    "only_trigger_self": 0, "only_enable_host": 0, "hide_player_units": 1,
    "start_play_event": "", "end_play_event": "",
    "dialogues": [
        {"ugc_id": 119, "owner_uid": 1000074, "story_id": 118, "sort_id": 0,
         "talker_type": 1541, "content": u"字幕一", "perform_type": 1231,
         "show_quit": 1, "text_color": "FFFFFF",
         "choices": [
             {"ugc_id": 120, "dialogue_id": 119, "sort_id": 0, "content": u"好",
              "enable": 1, "jump_type": 1226, "jump_story": 118,
              "jump_dialogue": 119, "on_touched_event": u"好"}]},
    ],
}


def _layer(rep, key):
    return [l for l in rep["layers"] if l["key"] == key][0]


class TestStoryFieldsParticipate(unittest.TestCase):
    def test_hide_player_units_change_is_reported(self):
        a = snap(story_doc([copy.deepcopy(BASE_STORY)]))
        b = snap(story_doc([dict(copy.deepcopy(BASE_STORY), hide_player_units=0)]))
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["story"]["changed"], [u"开场观影"])
        self.assertTrue(any("hide_player_units" in p for p in d["story"]["changed_paths"]),
                        msg=d["story"]["changed_paths"])
        self.assertFalse(d["identical"])

    def test_dialogue_perform_and_text_color_are_compared(self):
        def mk(perf, color):
            s = copy.deepcopy(BASE_STORY)
            s["dialogues"][0]["perform_type"] = perf
            s["dialogues"][0]["text_color"] = color
            return s
        d = sn.diff_snapshots(snap(story_doc([mk(1231, "FFFFFF")])),
                              snap(story_doc([mk(1230, "FF0000")])))
        paths = " ".join(d["story"]["changed_paths"])
        self.assertIn("perform_type", paths)
        self.assertIn("text_color", paths)

    def test_choice_enable_is_compared(self):
        def mk(enable):
            s = copy.deepcopy(BASE_STORY)
            s["dialogues"][0]["choices"][0]["enable"] = enable
            return s
        d = sn.diff_snapshots(snap(story_doc([mk(1)])), snap(story_doc([mk(0)])))
        self.assertEqual(d["story"]["changed"], [u"开场观影"])

    def test_volatile_ids_do_not_create_diffs(self):
        """重进/换账号会重编号：ugc_id/owner_uid/edit_time 参与比对就永远是差异。"""
        s = copy.deepcopy(BASE_STORY)
        s["ugc_id"], s["owner_uid"], s["edit_time"] = 900, 901, 999
        s["dialogues"][0]["ugc_id"] = 902
        s["dialogues"][0]["owner_uid"] = 903
        s["dialogues"][0]["story_id"] = 900
        s["dialogues"][0]["choices"][0]["ugc_id"] = 904
        s["dialogues"][0]["choices"][0]["dialogue_id"] = 902
        # 重进是**整份文档一起**重编号：跳转也跟着换，不然它们就解析不到了（那是另一回事，
        # 由 test_unresolvable_jump_is_unknown_not_ok 管）
        s["dialogues"][0]["choices"][0]["jump_story"] = 900
        s["dialogues"][0]["choices"][0]["jump_dialogue"] = 902
        d = sn.diff_snapshots(snap(story_doc([copy.deepcopy(BASE_STORY)])),
                              snap(story_doc([s])))
        self.assertEqual(d["story"]["changed"], [], msg=d["story"]["changed_paths"])
        self.assertTrue(d["identical"])

    def test_jump_ids_resolve_to_names(self):
        """选项跳到"剧情 118/对话 119"，新会话变成 900/902 —— 解析成名字后是一回事。"""
        s = copy.deepcopy(BASE_STORY)
        s["ugc_id"] = 900
        s["dialogues"][0]["ugc_id"] = 902
        s["dialogues"][0]["story_id"] = 900
        c = s["dialogues"][0]["choices"][0]
        c["dialogue_id"] = 902
        c["jump_story"] = 900
        c["jump_dialogue"] = 902
        d = sn.diff_snapshots(snap(story_doc([copy.deepcopy(BASE_STORY)])),
                              snap(story_doc([s])))
        self.assertEqual(d["story"]["changed"], [], msg=d["story"]["changed_paths"])

    def test_unresolvable_jump_is_reported_and_flagged_not_compared(self):
        """跳转指向文件外的节点：既是**内容坏了**（引用断），也是**没比成** ⇒ 两层都要说。"""
        s = copy.deepcopy(BASE_STORY)
        s["dialogues"][0]["choices"][0]["jump_story"] = 777777
        st = sn._story_unresolved(snap(story_doc([s])))
        self.assertTrue(st, msg="解析不到的 id 应该被记下来")
        rep = sn.acceptance_report(snap(story_doc([copy.deepcopy(BASE_STORY)])),
                                  snap(story_doc([s])))
        l = _layer(rep, "剧情")
        self.assertEqual(l["level"], "fail", msg=l["msg"])
        self.assertIn("解析不到", l["msg"], msg="只报差异不报「这一部分没比成」是不诚实的")
        self.assertFalse(rep["ok"])

    def test_old_export_without_story_fields_is_unknown_not_ok(self):
        """老导出（实机：`_verify_a.json` 的剧情节点只有 ugc_id/owner_uid/dialogues）
        跟新导出比，13 个字段全是 `None vs 1` —— 那不是"没采到"，那是不知。"""
        old = {"format": "fuxiang-story", "version": 1,
               "stories": [{"ugc_id": 118, "owner_uid": 1000073,
                            "name": u"开场观影",       # 同名，只差在"字段有没有采到"
                            "dialogues": [{"ugc_id": 119, "owner_uid": 1000074,
                                           "story_id": 118,
                                           "choices": [{"ugc_id": 120,
                                                        "dialogue_id": 119}]}]}],
               "orphan_dialogues": [], "orphan_choices": []}
        new = story_doc([copy.deepcopy(BASE_STORY)])
        d = sn.diff_snapshots(snap(old), snap(new))
        self.assertEqual(d["story"]["changed"], [],
                         msg="一边整层没采到 ⇒ 该比「没比成」，不是比出 13 条 None≠1 的假差异")
        self.assertEqual(d["story"]["added"], [])
        self.assertEqual(d["story"]["removed"], [])
        self.assertTrue(any(x.startswith("story.") or x == "story 层整层未采"
                            for x in d["story"]["uncaptured_levels"]),
                        msg=d["story"]["uncaptured_levels"])
        rep = sn.acceptance_report(snap(old), snap(new))
        self.assertEqual(_layer(rep, "剧情")["level"], "unknown")
        self.assertFalse(rep["ok"])

    def test_orphan_dialogues_participate(self):
        """孤儿对话/选项也是要重建的内容：不比对就会在"整批丢掉"时照样绿灯。"""
        a = snap(story_doc([copy.deepcopy(BASE_STORY)],
                           orphan_dialogues=[{"ugc_id": 500, "sort_id": 3,
                                               "content": u"没人挂的台词",
                                               "talker_type": 1223}]))
        b = snap(story_doc([copy.deepcopy(BASE_STORY)]))
        d = sn.diff_snapshots(a, b)
        self.assertIn(u"@孤儿对话", d["story"]["removed"])

    def test_print_diff_names_the_field(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        a = snap(story_doc([copy.deepcopy(BASE_STORY)]))
        b = snap(story_doc([dict(copy.deepcopy(BASE_STORY), hide_player_units=0)]))
        with contextlib.redirect_stdout(buf):
            sn.print_diff(sn.diff_snapshots(a, b))
        out = buf.getvalue()
        self.assertIn("hide_player_units", out)
        self.assertNotIn("    ~ 开场观影\n    ~ 开场观影", out)


class TestFieldListMatchesBridge(unittest.TestCase):
    """比对侧的字段清单不能自己缩水：与桥接 `FIELDS` 表逐字对齐（AST 读，不 import 游戏侧模块）。"""

    def _bridge_fields(self):
        path = os.path.join(ROOT, "bridge", "fuxiang_story.py")
        with io.open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        out = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "FIELDS":
                for level, body in zip(node.value.keys, node.value.values):
                    out[level.value] = {k.value for k in body.keys}
        self.assertTrue(out, "桥接 FIELDS 表结构变了，本测试要同步改写")
        return out

    def test_every_field_is_classified(self):
        """桥接认识的每个字段，比对侧必须**要么比、要么点名说是易变/连接用 id/子节点**，
        不许默默漏 —— 漏掉一个就是"这层没比成却印一致"。
        """
        bridge = self._bridge_fields()
        volatile = {"ugc_id", "owner_uid", "edit_time", "name"}
        children = {"dialogues", "choices"}
        for level in ("story", "dialogue", "choice"):
            got = (set(sn.STORY_COMPARE_FIELDS[level]) | set(sn.STORY_ID_FIELDS)
                   | volatile | children)
            missing = bridge[level] - got
            self.assertEqual(missing, set(),
                             msg="%s 层这些字段没进比对也没被点名：%s" % (level, sorted(missing)))
        self.assertIn("hide_player_units", sn.STORY_COMPARE_FIELDS["story"])
        self.assertIn("perform_type", sn.STORY_COMPARE_FIELDS["dialogue"])

    def test_compare_field_count_is_not_shrunk(self):
        n = sum(len(v) for v in sn.STORY_COMPARE_FIELDS.values())
        self.assertGreaterEqual(n, 30,
                                msg="参与比对的剧情字段缩到 %d，以前是 3+4 个 ⇒ 这层又变回摆设" % n)


@unittest.skipUnless(os.path.exists(DELIVERABLE) and os.path.exists(LIVE_AFTER_REPAIR),
                     "缺春岚交付/复原后导出 ⇒ 真实剧情数据未执行")
class TestOnRealChunlanStory(unittest.TestCase):
    def test_real_story_layer_is_consistent_and_fully_compared(self):
        with io.open(DELIVERABLE, encoding="utf-8") as f:
            a = json.load(f)
        with io.open(LIVE_AFTER_REPAIR, encoding="utf-8") as f:
            b = json.load(f)
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["story"]["changed"], [], msg=d["story"]["changed_paths"])
        self.assertFalse(d["story"]["unknown"], msg=d["story"]["unresolved"])
        # 真实那份确实逐字段比过：把藏人翻掉就必须报差异
        s = copy.deepcopy(a["story"])
        s["stories"][0]["hide_player_units"] = 0
        d2 = sn.diff_snapshots(a, dict(b, story=s))
        self.assertEqual(d2["story"]["changed"], [u"春嵐_全片观影"])


if __name__ == "__main__":
    unittest.main()
