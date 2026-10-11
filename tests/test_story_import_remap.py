#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_story_import_remap — 剧情**跨图导入**的引用重映射锁（无需游戏）。

背景（2026-09-23 实测定位）
--------------------------
剧情文档里存着**源图**的 ugc_id，而节点间的 `story_id` / `dialogue_id` /
`jump_dialogue` 写的就是源图 id。导入到**新图**时这些节点全是 create，id 会变；
若原样写回旧 id，新节点就会指向不存在的节点（= 悬空）。

修复靠纯结构化的 path：
    `_flatten` 生成 `story#N` / `story#N/dialogue#N` / `story#N/dialogue#N/choice#N`
    （只依赖文档自身数组顺序，与 ugc_id 无关）
    ⇒ 旧 id → 文档 path → created 里的 new id

这里锁住四件事：
1. `_doc_id_to_path` 覆盖每个节点，且 path 与 `_flatten` 的编号规则一致；
2. 跨图（created 有对应 path）时父/跳转引用必须被换成新 id；
3. 同图导入（对应 path 不在 created 里）时必须**保留原值**——否则会把本来正确的
   引用改坏（这是最危险的回归方向）；
4. `_story_id_map` 不返回 old == new 的条目（否则重绑会空转）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import fuxiang_story as fs  # noqa: E402


def sample_doc():
    """两张剧情：第一张 1 对白 2 选项，第二张 3 对白（第 1 个对白有 2 个跳转选项）。"""
    return {
        "format": fs.FORMAT_NAME, "version": fs.FORMAT_VERSION,
        "map": {"name": "src"},
        "stories": [
            {"ugc_id": 349, "owner_uid": 1000304, "name": "剧情1",
             "edit_time": "", "start_play_event": "剧情被触发",
             "dialogues": [
                 {"ugc_id": 350, "owner_uid": 1000305, "story_id": 349, "sort_id": 0,
                  "content": "你好呀",
                  "choices": [
                      {"ugc_id": 351, "owner_uid": 1000306, "dialogue_id": 350,
                       "sort_id": 0, "content": "选项1", "on_touched_event": "选项1"},
                      {"ugc_id": 352, "owner_uid": 1000306, "dialogue_id": 350,
                       "sort_id": 1, "content": "选项2", "enable": 0,
                       "on_touched_event": "选项2"}]}]},
            {"ugc_id": 353, "owner_uid": 1000304,
             "dialogues": [
                 {"ugc_id": 354, "owner_uid": 1000305, "story_id": 353, "sort_id": 0,
                  "content": "门缝里塞着一封信。",
                  "choices": [
                      {"ugc_id": 357, "owner_uid": 1000306, "dialogue_id": 354,
                       "sort_id": 0, "jump_type": 1227, "jump_dialogue": 355,
                       "content": "拆开信件", "enable": 1,
                       "on_touched_event": "拆开信件"},
                      {"ugc_id": 358, "owner_uid": 1000306, "dialogue_id": 354,
                       "sort_id": 1, "jump_type": 1227, "jump_dialogue": 356,
                       "content": "塞回门缝", "enable": 1,
                       "on_touched_event": "塞回门缝"}]},
                 {"ugc_id": 355, "owner_uid": 1000305, "story_id": 353, "sort_id": 1,
                  "content": "信上只有一行小字…", "choices": []},
                 {"ugc_id": 356, "owner_uid": 1000305, "story_id": 353, "sort_id": 2,
                  "content": "你把信塞了回去…", "choices": []}]}],
        "orphan_dialogues": [], "orphan_choices": [],
    }


def node_of(doc, path):
    for n in fs._flatten(doc):
        if n["path"] == path:
            return n
    raise AssertionError("没有 path=%s" % path)


class TestDocIdToPath(unittest.TestCase):
    def test_covers_every_node(self):
        doc = sample_doc()
        m = fs._doc_id_to_path(doc)
        self.assertEqual(len(m), 10)
        for uid in (349, 350, 351, 352, 353, 354, 355, 356, 357, 358):
            self.assertIn(uid, m)

    def test_path_shape_is_structural(self):
        m = fs._doc_id_to_path(sample_doc())
        self.assertEqual(m[349], "story#1")
        self.assertEqual(m[350], "story#1/dialogue#1")
        self.assertEqual(m[351], "story#1/dialogue#1/choice#1")
        self.assertEqual(m[352], "story#1/dialogue#1/choice#2")
        self.assertEqual(m[353], "story#2")
        self.assertEqual(m[354], "story#2/dialogue#1")
        self.assertEqual(m[355], "story#2/dialogue#2")
        self.assertEqual(m[357], "story#2/dialogue#1/choice#1")

    def test_stable_when_ugc_ids_change(self):
        """path 只由数组顺序决定 —— 与 ugc_id 无关（跨图复刻的前提）。"""
        doc = sample_doc()
        m1 = fs._doc_id_to_path(doc)
        # 把源图 id 整体挪到另一个号段，path 必须一模一样
        shifted = sample_doc()
        for s in shifted["stories"]:
            s["ugc_id"] += 1000
            for d in s["dialogues"]:
                d["ugc_id"] += 1000
                d["story_id"] += 1000
                for c in d["choices"]:
                    c["ugc_id"] += 1000
                    c["dialogue_id"] += 1000
        m2 = fs._doc_id_to_path(shifted)
        self.assertEqual(sorted(m1.values()), sorted(m2.values()))


class TestCrossMapRemap(unittest.TestCase):
    """created 用 path → new id 表示"本次新建"，模拟导入到空白图。"""

    def _created(self, doc, base=900000):
        out = []
        for i, n in enumerate(fs._flatten(doc), 1):
            out.append({"path": n["path"], "kind": n["kind"], "ugc_id": base + i})
        return out

    def test_parent_refs_remapped(self):
        doc = sample_doc()
        created = self._created(doc)
        id_path = fs._doc_id_to_path(doc)
        by_path = {c["path"]: c["ugc_id"] for c in created}

        d1 = node_of(doc, "story#1/dialogue#1")
        nf = fs._node_fields(None, d1, created, id_path)
        self.assertEqual(nf["story_id"], by_path["story#1"])
        self.assertNotEqual(nf["story_id"], 349)

        c1 = node_of(doc, "story#1/dialogue#1/choice#1")
        nf = fs._node_fields(None, c1, created, id_path)
        self.assertEqual(nf["dialogue_id"], by_path["story#1/dialogue#1"])

    def test_jump_dialogue_remapped(self):
        """⛔ 最容易漏的一条：jump_dialogue 是源图的对话 id。"""
        doc = sample_doc()
        created = self._created(doc)
        id_path = fs._doc_id_to_path(doc)
        by_path = {c["path"]: c["ugc_id"] for c in created}

        c = node_of(doc, "story#2/dialogue#1/choice#1")
        nf = fs._node_fields(None, c, created, id_path)
        # 源图 jump_dialogue=355 → 新图 story#2/dialogue#2 的新 id
        self.assertEqual(nf["jump_dialogue"], by_path["story#2/dialogue#2"])
        self.assertNotEqual(nf["jump_dialogue"], 355)

    def test_same_map_import_keeps_original_ids(self):
        """⚠️ 回归保护：同图导入时 created 为空/不含该 path ⇒ 原值必须原样保留。

        改错的后果比不改更严重：本来正确的引用会被改成不存在的 id。
        """
        doc = sample_doc()
        d1 = node_of(doc, "story#1/dialogue#1")
        nf = fs._node_fields(None, d1, [], fs._doc_id_to_path(doc))
        self.assertEqual(nf["story_id"], 349)

        c = node_of(doc, "story#2/dialogue#1/choice#1")
        nf = fs._node_fields(None, c, [], fs._doc_id_to_path(doc))
        self.assertEqual(nf["dialogue_id"], 354)
        self.assertEqual(nf["jump_dialogue"], 355)

    def test_partial_create_only_remaps_created(self):
        """父节点是既有的（不在 created 里）、子节点新建：父引用保持原值。"""
        doc = sample_doc()
        id_path = fs._doc_id_to_path(doc)
        created = [{"path": "story#1/dialogue#1", "kind": "dialogue", "ugc_id": 777}]
        c1 = node_of(doc, "story#1/dialogue#1/choice#1")
        nf = fs._node_fields(None, c1, created, id_path)
        # 选项的父对白是本次新建的 → 换成 777
        self.assertEqual(nf["dialogue_id"], 777)
        # 而它的 story 没被新建 → 不参与，选项本身不写 story_id
        self.assertNotIn("story_id", nf)

    def test_without_id_path_behaves_like_before(self):
        doc = sample_doc()
        d1 = node_of(doc, "story#1/dialogue#1")
        nf = fs._node_fields(None, d1, [], None)
        self.assertEqual(nf["story_id"], 349)


class TestStoryIdMap(unittest.TestCase):
    def test_maps_old_to_new_by_path(self):
        doc = sample_doc()
        created = [{"path": n["path"], "kind": n["kind"], "ugc_id": 900000 + i}
                   for i, n in enumerate(fs._flatten(doc), 1)]
        m = fs._story_id_map({"id_path": fs._doc_id_to_path(doc),
                              "created": created})
        by_path = {c["path"]: c["ugc_id"] for c in created}
        self.assertEqual(m[351], by_path["story#1/dialogue#1/choice#1"])
        self.assertEqual(m[357], by_path["story#2/dialogue#1/choice#1"])
        self.assertEqual(len(m), 10)

    def test_skips_identical_ids(self):
        """old == new 的不进映射（否则重绑白跑一遍）。"""
        doc = sample_doc()
        created = [{"path": n["path"], "kind": n["kind"], "ugc_id": n["ugc_id"]}
                   for n in fs._flatten(doc)]
        self.assertEqual(fs._story_id_map({"id_path": fs._doc_id_to_path(doc),
                                           "created": created}), {})

    def test_empty_state(self):
        self.assertEqual(fs._story_id_map({}), {})
        self.assertEqual(fs._story_id_map({"id_path": {}, "created": []}), {})


if __name__ == "__main__":
    unittest.main()
