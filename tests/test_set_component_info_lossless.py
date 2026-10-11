#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_set_component_info_lossless — 整袋回写的两处数据安全锁。

`op_set_component_info` 是快照还原的属性阶段唯一出口，一次跑上万件，两个失效面都是
"回执看起来正常、图里其实坏了"：
  1. 回滚载荷若走 `_plain`（显示态：list 只留 40 项、深度 >4 变 `<deep>`），
     一台 3700 点路径的运动器在校验失败被"回滚"后，会**带着被截断的路径**留在图上；
  2. `remapped` 判定也走 `_plain` 时，组合件成员表（春岚实测单件 40~数百个 ugc_id）
     第 40 项之后的重映射会被当成"没变"，于是既没换号也不报错。
"""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import fuxiang_edit_ops as ops  # noqa: E402

PATH = 20391119001          # 运动器路径关键帧
MEMBERS = 20390003001       # COMPOSITE 成员表（整袋都是 ugc_id）
MOTION_REF = 20390018701    # COMPOSITE → 运动器


def _mover_bag(points):
    return {'component_id': 203911,
            'events': [{'type': PATH, 'info': [points]}]}


class TestRollbackIsLossless(unittest.TestCase):
    def setUp(self):
        self.points = [[0.083, [i, 28, -100], [0, 0, i], [0.1, 2, 0.1]]
                       for i in range(120)]
        self.original = _mover_bag(self.points)
        self.comp = SimpleNamespace(
            COMPONENT_TYPE=203911, _ugc_id=118,
            component_info=copy.deepcopy(self.original))
        self.comp.update_component_info = lambda events: \
            self.comp.component_info.update(
                events=copy.deepcopy([dict(e) for e in events]))
        self.unit = SimpleNamespace(uid=1000074)
        self.edit = SimpleNamespace(reg_change=lambda *args: True)

    def _run(self, events):
        with patch.object(ops, 'find_target',
                          return_value=(self.comp, self.unit, None)), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            return ops.op_set_component_info(
                self.edit, {'ugc_id': 118, 'events': events,
                            'expect_component_id': 203911})

    def test_write_keeps_every_keyframe(self):
        r = self._run(copy.deepcopy(self.original)['events'])
        self.assertTrue(r['ok'], r)
        self.assertEqual(len(self.comp.component_info['events'][0]['info'][0]), 120)

    def test_rollback_restores_full_path_not_40_points(self):
        """回读缺字段 → 回滚；回滚载荷必须是原袋全量。"""
        calls = []

        def update(events):
            data = copy.deepcopy([dict(e) for e in events])
            if not calls:
                data.pop(0)                      # 第一次写入"丢字段"，触发校验失败
            calls.append(True)
            self.comp.component_info['events'] = data

        self.comp.update_component_info = update
        r = self._run(copy.deepcopy(self.original)['events'])
        self.assertFalse(r['ok'])
        self.assertTrue(r['rollback']['complete'])
        self.assertEqual(self.comp.component_info, self.original)   # 120 点全在
        self.assertEqual(len(self.comp.component_info['events'][0]['info'][0]), 120)


class TestCompositeRemap(unittest.TestCase):
    """成员表换号必须覆盖 40 项以后，且运动器的枚举字段不许被当成 id 改。"""

    def setUp(self):
        self.members = [[228 + k for k in range(300)]]
        self.comp = SimpleNamespace(
            COMPONENT_TYPE=203900, _ugc_id=552,
            component_info={'component_id': 203900, 'events': [
                {'type': MEMBERS, 'info': copy.deepcopy(self.members)},
                {'type': MOTION_REF, 'info': [[553]]}]})
        self.comp.update_component_info = lambda events: \
            self.comp.component_info.update(
                events=copy.deepcopy([dict(e) for e in events]))
        self.unit = SimpleNamespace(uid=1000057)
        self.edit = SimpleNamespace(reg_change=lambda *args: True)

    def _run(self, remap):
        with patch.object(ops, 'find_target',
                          return_value=(self.comp, self.unit, None)), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            return ops.op_set_component_info(
                self.edit, {'ugc_id': 552, 'expect_component_id': 203900,
                            'remap': remap,
                            'events': copy.deepcopy(
                                self.comp.component_info['events'])})

    def test_composite_fields_are_in_the_remap_whitelist(self):
        self.assertIn(str(MEMBERS), ops.UGCID_REF_PROPERTIES)
        self.assertIn(str(MOTION_REF), ops.UGCID_REF_PROPERTIES)

    def test_tail_of_member_list_is_remapped_and_reported(self):
        r = self._run({self.members[0][200]: 99999, self.members[0][-1]: 88888,
                       553: 7001})
        self.assertTrue(r['ok'], r)
        got = {e['type']: e['info'] for e in self.comp.component_info['events']}
        self.assertIn(99999, got[MEMBERS][0])       # 第 201 项：_plain 看不见的那一段
        self.assertIn(88888, got[MEMBERS][0])       # 末项
        self.assertEqual(got[MOTION_REF], [[7001]])
        self.assertIn(str(MEMBERS), r['remapped_properties'])
        self.assertIn(str(MOTION_REF), r['remapped_properties'])

    def test_motion_enum_fields_are_never_touched(self):
        """20391102002/02007/18801 存的是 PATH/REPEAT/计量枚举（1641/1653/1711）。

        它们数值上会与某些 ugc_id 撞号，列进白名单就会把枚举换成随机 id。
        """
        for enum_pid in ("20391102002", "20391102007", "20391118801",
                         str(PATH)):
            self.assertNotIn(enum_pid, ops.UGCID_REF_PROPERTIES)

    def test_unmapped_member_ids_stay_untouched(self):
        r = self._run({999999: 1})                  # 与成员表完全无关的映射
        self.assertTrue(r['ok'], r)
        got = {e['type']: e['info']
               for e in self.comp.component_info['events']}
        self.assertEqual(got[MEMBERS], self.members)
        self.assertEqual(r['remapped_properties'], [])


class TestReadbackDetectsTruncatedTail(unittest.TestCase):
    """回读校验必须能看出"尾巴被吃掉了"，否则截断写入会一路绿灯。"""

    def test_deep_difference_is_reported_not_hidden(self):
        points = [[0.083, [i, 28, -100], [0, 0, i], [0.1, 2, 0.1]] for i in range(120)]
        comp = SimpleNamespace(
            COMPONENT_TYPE=203911, _ugc_id=118,
            component_info={'component_id': 203911,
                            'events': [{'type': PATH, 'info': [points]}]})

        def update(events):
            # 引擎侧"接受"了写入，但只回前 40 点 —— 典型静默截断
            data = copy.deepcopy([dict(e) for e in events])
            data[0]['info'][0] = data[0]['info'][0][:40]
            comp.component_info['events'] = data

        comp.update_component_info = update
        with patch.object(ops, 'find_target', return_value=(comp, SimpleNamespace(uid=1), None)), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            r = ops.op_set_component_info(
                SimpleNamespace(reg_change=lambda *a: True),
                {'ugc_id': 118, 'expect_component_id': 203911,
                 'events': [{'type': PATH, 'info': [points]}]})
        self.assertTrue(r['ok'], r)                 # 字段没缺，所以不判失败
        self.assertIn(str(PATH), r['value_diff_properties'])   # 但尾巴差异必须报出来


if __name__ == '__main__':
    unittest.main()
