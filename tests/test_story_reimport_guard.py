#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_story_reimport_guard — 剧情层重跑不翻倍。

`story_import` **不查重**：重跑一次 `restore --with-story`（同会话重跑，或跨会话用
`--resume-from` 续做）就把整批剧情节点再建一遍。而剧情三件套**不占** `component_nums`
⇒ 件数对得上、事后从读数上看不出来。所以判"要不要导"必须发生在导之前。
匹配沿用 `story_map_from_tree`（结构位置 + 内容校验），与 `--verify-only` 同一个判据。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402

DOC = {"stories": [{"ugc_id": 11, "name": "开场", "dialogues": [
    {"ugc_id": 12, "sort_id": 1, "content": "甲", "choices": [
        {"ugc_id": 13, "sort_id": 1, "content": "选A"}]}]}]}


def _snap():
    return {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t",
            "map": {"name": "src", "framework_id": "4002",
                    "map_setting": {"name": "src"}, "anchor_info": {}},
            "components": [{"g": 1, "i": 301001, "p": [1.0, 0.0, 0.0],
                            "r": [0, 0, 0], "s": [1, 1, 1],
                            "ci": json.dumps({"component_id": 100001, "events": []})}],
            "events": {"count": 0, "events": [], "saved_keys": []},
            "story": json.loads(json.dumps(DOC)),
            "budget": {"component_nums": 1, "registered": 1,
                       "max_component_nums": 15000, "occupancy": 1,
                       "max_occupancy": 30000}}


class TestStoryNodeCount(unittest.TestCase):
    def test_counts_all_three_layers(self):
        self.assertEqual(sr.story_node_count(DOC), 3)     # 1 剧情 + 1 对话 + 1 选项

    def test_empty_doc_is_zero(self):
        self.assertEqual(sr.story_node_count({}), 0)
        self.assertEqual(sr.story_node_count(None), 0)


class TestReimportGuard(unittest.TestCase):
    def _run(self, mapped, expect_apply):
        """mapped = 目标图上能配上的节点数（模拟 story_map_from_tree 的结果）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_snap(), f, ensure_ascii=False)
            applied = []
            smap = {11: 101, 12: 102, 13: 103} if mapped >= 3 else (
                {11: 101, 12: 102} if mapped else {})

            def fake_apply(doc, *a, **k):
                applied.append(doc)
                return {"ok": True, "created_count": 3, "story_id_map": {}}

            argv = [path, "--apply", "--yes", "--with-story",
                    "--no-props", "--no-events",
                    "--progress", os.path.join(tmp, "p.jsonl")]
            if expect_apply == "force":
                argv.append("--force-story-import")
            with contextlib.redirect_stdout(io.StringIO()) as buf, \
                    mock.patch.object(sr, "fetch_story_tree", lambda *a, **k: {}), \
                    mock.patch.object(sr, "story_map_from_tree",
                                      lambda doc, tree: (dict(smap), [])), \
                    mock.patch.object(sr, "apply_story", fake_apply), \
                    mock.patch.object(sr, "call_edit",
                                      lambda *a, **k: {"ok": True, "components": [],
                                                       "value": {}, "index": {},
                                                       "status": "placed",
                                                       "ugc_id": 555}), \
                    mock.patch.object(sr, "save_and_check",
                                      lambda *a, **k: {"submitted": True}), \
                    mock.patch.object(sr, "fetch_target_info",
                                      lambda *a, **k: {"framework": "4002",
                                                      "map_setting": {"name": "B 图"},
                                                      "bounds": None, "budget": None}), \
                    mock.patch.object(sr, "fetch_placeable_types",
                                      lambda *a, **k: {100001}):
                rc = sr.main(argv)
            return bool(applied), buf.getvalue(), rc

    def test_complete_layer_skips_the_import(self):
        applied, out, _rc = self._run(3, expect_apply=False)
        self.assertFalse(applied, msg="目标图已有全套节点，却还是调了 story_import")
        self.assertIn("跳过导入", out)

    def test_missing_layer_still_imports(self):
        applied, out, _rc = self._run(0, expect_apply=True)
        self.assertTrue(applied, msg="目标图没有剧情节点，应该照常导入")

    def test_partial_layer_refuses_and_points_at_resume(self):
        """半截状态再导必然翻倍；出路是原生续跑，不是重导。"""
        applied, out, _rc = self._run(2, expect_apply=False)
        self.assertFalse(applied)
        self.assertIn("story.py resume", out)

    def test_force_flag_is_the_named_escape(self):
        applied, out, _rc = self._run(2, expect_apply="force")
        self.assertTrue(applied, msg="--force-story-import 没起作用")

    def test_skip_path_still_binds_ids(self):
        """跳过导入不等于放弃补绑：已存在节点的 id 映射要并进总映射。"""
        _applied, out, _rc = self._run(3, expect_apply=False)
        self.assertIn("并入", out)

    def test_guard_is_wired_before_the_import(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("story_node_count(doc)", src)
        self.assertIn('"--force-story-import"', src)
        self.assertLess(src.index("story_node_count(doc)"), src.index("apply_story(doc)"))




if __name__ == "__main__":
    unittest.main()
