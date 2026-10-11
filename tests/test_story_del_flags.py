# -*- coding: utf-8 -*-
"""剧情删除的「待删除标记校正」离线回归（不接触游戏进程）。

背景（2026-09-22 实机定位，详见 STORY_EDIT.md §8.6）：
  保存时真正上送删除的通道是 ``room.del_unit_uids['component']``（报文 ``del_uid.component``），
  **不是** ``ui._del_unit()``。而 ``_del_unit()`` 对全局组件会把标记写到**存活的兄弟节点**上：
  袋子清空 → 删 A(targets=[167,168]) → 袋子变成 [169,170]（另一条存活剧情）。
  结果：重进后「要删的 A 复活、没要删的 B 消失」。

  修复 = 删除后把袋子校正成「恰好只有 targets」。这段决策风险最高（算错会删掉正文剧情），
  故把纯逻辑抽成 ``fuxiang_story.plan_del_flags`` 并在此锁住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import fuxiang_story  # noqa: E402


class TestPlanDelFlags(unittest.TestCase):
    def test_empty_bag_marks_all_targets(self):
        # 袋子空 ⇒ 必须补标记：否则保存时 targets 根本不会被上送为删除
        p = fuxiang_story.plan_del_flags([], [], [167, 168])
        self.assertEqual(p["to_clear"], [])
        self.assertEqual(p["to_mark"], [167, 168])

    def test_misattributed_survivor_is_cleared_and_target_marked(self):
        # 实机复现场景：删 A(167,168) 却把存活剧情 D(169,170) 标进了袋子
        p = fuxiang_story.plan_del_flags(bag=[169, 170], live=[169, 170, 118, 120],
                                         targets=[167, 168])
        self.assertEqual(p["to_clear"], [169, 170], "误标必须摘掉，否则 D 会被删")
        self.assertEqual(p["to_mark"], [167, 168], "目标必须补标记，否则 A 不会被删")

    def test_already_correct_bag_is_noop(self):
        p = fuxiang_story.plan_del_flags([167, 168], live=[118, 120], targets=[167, 168])
        self.assertEqual(p, {"to_clear": [], "to_mark": []})

    def test_bag_entries_not_live_are_left_alone(self):
        # 袋里「已不存在」的 id 是正当的待删除成员，不必摘（摘了反而可能复活它）
        p = fuxiang_story.plan_del_flags(bag=[999], live=[118], targets=[167])
        self.assertEqual(p["to_clear"], [])
        self.assertEqual(p["to_mark"], [167])

    def test_target_already_locally_deleted_is_still_marked(self):
        # 目标已被本地删除（不在 live 里）→ 仍要补标记，这正是「重进复活」的修复路径
        p = fuxiang_story.plan_del_flags(bag=[], live=[118, 120], targets=[167, 168])
        self.assertEqual(p["to_mark"], [167, 168])

    def test_never_touches_non_story_ids(self):
        # 存活集合只含剧情族；非剧情族的袋子成员一律不动
        p = fuxiang_story.plan_del_flags(bag=[1, 2, 3], live=[118], targets=[167])
        self.assertEqual(p["to_clear"], [])
        self.assertEqual(p["to_mark"], [167])

    def test_accepts_strings_and_sets(self):
        p = fuxiang_story.plan_del_flags(bag={"169"}, live={"169", 118}, targets=["169"])
        self.assertEqual(p, {"to_clear": [], "to_mark": []})

    def test_none_inputs(self):
        p = fuxiang_story.plan_del_flags(None, None, None)
        self.assertEqual(p, {"to_clear": [], "to_mark": []})


class TestOpStoryDeleteWiring(unittest.TestCase):
    def test_op_story_delete_exists_and_is_documented(self):
        self.assertTrue(callable(getattr(fuxiang_story, "op_story_delete", None)))
        doc = fuxiang_story.op_story_delete.__doc__ or ""
        for needle in ("del_unit_uids", "两段式", "_reconcile_del_flags"):
            self.assertIn(needle, doc,
                          "op_story_delete 的文档必须说明两段式删除与标记校正（当前缺 %r）" % needle)

    def test_reconcile_helper_exists(self):
        self.assertTrue(callable(getattr(fuxiang_story, "_reconcile_del_flags", None)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
