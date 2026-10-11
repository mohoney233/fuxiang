#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_identity — 快照的 item_id 必须**逐件**取，不能取单元的第一件。

共用单元是这套编辑器反复咬人的结构（`UI_EDIT.md` §0：全图 UI_CUSTOM 控件共用**一个**
`UgcGlobalUnit`，实机 30 件跨 27 个分类全挂在 uid 1000313 上，而 `unit.ugc_type` 只是
第一件的 item_id）。导出若用 `ugc_type` 当身份，一张有 30 种控件的图会被还原成
"同一个控件摆 30 遍"，而且**本地回读还一致** —— 属于"看起来成功、其实是错图"的方向。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import fuxiang_edit as fe  # noqa: E402
import snapshot as sn  # noqa: E402


class _Swallow(object):
    """本客户端对"不存在的方法"返回的哨兵：能调用、不抛错，但绝不是数据。"""

    def __call__(self, *a, **k):
        return self

    def __int__(self):
        raise TypeError("Swallow is not a number")


class _Room(object):
    def __init__(self, table=None, raises=False):
        self._table = table or {}
        self._raises = raises

    def get_component_item_id(self, gid):
        if self._raises:
            raise RuntimeError("服务端接口未就绪")
        return self._table.get(gid)


class TestComponentItemId(unittest.TestCase):
    def test_per_component_id_wins_over_unit_first_item(self):
        room = _Room({362: 5201002, 363: 5301002})
        self.assertEqual(fe.component_item_id(room, 362, 5201002), 5201002)
        # 同单元第二件：单元上记的还是第一件的，必须问出它自己的
        self.assertEqual(fe.component_item_id(room, 363, 5201002), 5301002)

    def test_digit_string_is_normalised_to_int(self):
        self.assertEqual(fe.component_item_id(_Room({1: "301001"}), 1, 0), 301001)

    def test_swallow_sentinel_falls_back(self):
        """getter 存在但返回哨兵（能调用、不抛错）⇒ 必须校验成正整数才采用。"""
        room = _Room()
        room.get_component_item_id = _Swallow()
        self.assertEqual(fe.component_item_id(room, 7, 301001), 301001)

    def test_missing_getter_falls_back(self):
        room = type("NoApi", (), {})()          # 客户端改版后没这个方法
        self.assertEqual(fe.component_item_id(room, 7, 6000001), 6000001)

    def test_error_none_and_non_positive_fall_back(self):
        self.assertEqual(fe.component_item_id(_Room(raises=True), 7, 5), 5)
        self.assertEqual(fe.component_item_id(_Room({7: None}), 7, 5), 5)
        self.assertEqual(fe.component_item_id(_Room({7: 0}), 7, 5), 5)
        self.assertEqual(fe.component_item_id(_Room({7: -1}), 7, 5), 5)

    def test_no_fallback_means_none(self):
        self.assertIsNone(fe.component_item_id(_Room(raises=True), 7))


class TestExportUsesTheHelper(unittest.TestCase):
    """导出代码是**字符串**、在游戏侧执行，本文件其它用例发现不了它被改回去 ⇒ 锁源码。"""

    def test_components_code_calls_the_helper(self):
        self.assertIn("_fe.component_item_id(room, gid", sn.CODE_COMPONENTS)

    def test_components_code_no_longer_uses_unit_ugc_type_as_identity(self):
        self.assertNotIn('"i": getattr(u, "ugc_type"', sn.CODE_COMPONENTS)


if __name__ == "__main__":
    unittest.main()
