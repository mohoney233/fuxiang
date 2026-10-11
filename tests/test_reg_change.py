# -*- coding: utf-8 -*-
"""reg_change 的离线回归（不接触游戏进程）。

背景（2026-09-22 实机定位，详见 EDIT_OPS.md §9.5）：
  原生 ``ui._add_change_unit(unit.uid)`` 内部走 ``get_single_unit_key(unit)``；
  当 ``check_unit_single(unit)`` 为真（运动器/剧情/对话/选项这类"一个单元挂多个同类组件"）
  时它只返回该单元**当前那一个**组件的 ugc_id —— 实测 6 个运动器共用 unit 1000094，
  往 169 写完后登记到的是 176。于是属性袋写得再对，保存也不带上本组件 → 重进即失。
  修法是 ``fuxiang_edit.reg_change`` 在原生登记后把本组件补进 ``change_units['component']``。
  这段是"看起来成功了但其实没落库"的典型，所以锁住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import fuxiang_edit as edit  # noqa: E402


class FakeUnit(object):
    def __init__(self, uid):
        self.uid = uid


class FakeComp(object):
    def __init__(self, ugc_id):
        self._ugc_id = ugc_id


class FakeUI(object):
    """只实现 reg_change 用到的三件事，并记录原生调用。"""

    def __init__(self, change_units, single=True, single_raises=False):
        self.ugc_params = {"change_units": change_units}
        self.added = []
        self._single = single
        self._single_raises = single_raises

    def _add_change_unit(self, uid):
        self.added.append(uid)

    def check_unit_single(self, unit):
        if self._single_raises:
            raise RuntimeError("single_unit_mgr 不可用")
        return self._single


class TestRegChange(unittest.TestCase):
    def test_native_call_always_happens_first(self):
        cu = {"unit": set()}
        ui = FakeUI(cu, single=False)
        self.assertFalse(edit.reg_change(ui, FakeUnit(1000002), FakeComp(7)))
        self.assertEqual(ui.added, [1000002])
        self.assertEqual(cu, {"unit": set()})          # 非共用单元不掺和

    def test_single_unit_adds_this_component(self):
        # 实机场景：改的是 169，原生却登记了同单元的 176
        cu = {"component": {176}}
        ui = FakeUI(cu, single=True)
        self.assertTrue(edit.reg_change(ui, FakeUnit(1000094), FakeComp(169)))
        self.assertEqual(ui.added, [1000094])
        self.assertEqual(cu["component"], {169, 176})

    def test_list_bucket_supported_and_idempotent(self):
        cu = {"component": [176]}
        ui = FakeUI(cu, single=True)
        comp = FakeComp(172)
        self.assertTrue(edit.reg_change(ui, FakeUnit(1000094), comp))
        self.assertTrue(edit.reg_change(ui, FakeUnit(1000094), comp))
        self.assertEqual(cu["component"], [176, 172])   # 不重复追加

    def test_creates_missing_component_bucket(self):
        cu = {}
        ui = FakeUI(cu, single=True)
        self.assertTrue(edit.reg_change(ui, FakeUnit(1000094), FakeComp(169)))
        self.assertEqual(cu, {"component": {169}})

    def test_single_check_failure_falls_back_to_native_only(self):
        cu = {"component": set()}
        ui = FakeUI(cu, single_raises=True)
        self.assertFalse(edit.reg_change(ui, FakeUnit(1000094), FakeComp(169)))
        self.assertEqual(ui.added, [1000094])
        self.assertEqual(cu["component"], set())

    def test_dict_bucket_also_supported(self):
        # 引擎原生用的是 set；万一别处把它建成 dict[component_id]=unit_uid，
        # 也不能静默丢掉本组件（丢掉 == 不上送 == 重进即失）
        cu = {"component": {137: 1000073}}
        ui = FakeUI(cu, single=True)
        self.assertTrue(edit.reg_change(ui, FakeUnit(1000073), FakeComp(119)))
        self.assertEqual(cu["component"], {137: 1000073, 119: 1000073})

    def test_unknown_bucket_type_reports_not_registered(self):
        cu = {"component": "unexpected"}
        ui = FakeUI(cu, single=True)
        self.assertFalse(edit.reg_change(ui, FakeUnit(1000094), FakeComp(169)))
        self.assertEqual(ui.added, [1000094])          # 原生登记仍然做了，只是本组件没补上

    def test_no_component_means_native_only(self):
        cu = {"component": set()}
        ui = FakeUI(cu, single=True)
        self.assertFalse(edit.reg_change(ui, FakeUnit(1000070), None))
        self.assertEqual(cu["component"], set())

    def test_ugc_id_int_coerces_against_str_keys(self):
        # 引擎桶里可能是 str/float 混排，别因为类型差异重复塞
        cu = {"component": ["169"]}
        ui = FakeUI(cu, single=True)
        edit.reg_change(ui, FakeUnit(1000094), FakeComp(169))
        self.assertEqual(len(cu["component"]), 1)


if __name__ == "__main__":
    unittest.main()
