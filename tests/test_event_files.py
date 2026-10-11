# -*- coding: utf-8 -*-
"""事件文件层（event_export / event_dsl / event_import）的离线回归测试。

锁住 2026-09-18 P3-1 解出的字段语法，重点是**事件键不一定在 info[0]**：
  * property_type 的 ';' 段与 property_params 的 '{}' 段一一对齐；
  * 80001 段 = 事件键槽，其段内第二元素 1=发送 / 2=接收；
  * info 形态 = get_default_value_info(定义)[0] 各段顺序拼接。
所以 20204111101（人数检测·玩家角色数量）的键在第 3 位、20200303005（道具箱·手持物）在第 2 位。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import event_export  # noqa: E402
import event_dsl  # noqa: E402
import event_import  # noqa: E402

TMP = os.path.join(ROOT, "tests", "_tmp_event_files")


def _machine_doc():
    """一条键在 0 位的普通机关 + 一条键在第 3 位的人数检测机关。"""
    return {
        "meta": {"schema_version": 1},
        "objects": {
            "120": {"label": "进入区域", "item_id": 203003, "component_type": 203003,
                    "position": [-430.0, 1.543, 200.0], "rotation": None,
                    "ugc_id_at_export": 120},
            "121": {"label": "成功提示", "item_id": 203004, "component_type": 203004,
                    "position": [-430.0, 53.0, 205.0], "rotation": None,
                    "ugc_id_at_export": 121},
            "125": {"label": "人数检测区", "item_id": 202041, "component_type": 202041,
                    "position": [-950.0, 1.543, 400.0], "rotation": None,
                    "ugc_id_at_export": 125},
        },
        "events": [
            {"key": "进入提示", "sort_id": 2, "in_save": True, "bindings": [
                {"role": "sender", "semantic": "区域.进入区域时", "object": "进入区域",
                 "object_ugc_id_at_export": 120, "property_id": "20300311106",
                 "raw_value": ["进入提示", 1286]},
                {"role": "receiver", "semantic": "信息提示.使文字出现", "object": "成功提示",
                 "object_ugc_id_at_export": 121, "property_id": "20300411205",
                 "raw_value": ["进入提示"]}]},
            {"key": "人数达到", "sort_id": 4, "in_save": True, "bindings": [
                {"role": "sender", "semantic": "人数检测.玩家角色数量达标", "object": "人数检测区",
                 "object_ugc_id_at_export": 125, "property_id": "20204111101",
                 "raw_value": [1, 1.0, "人数达到"]}]},
        ],
    }


def _live_objs():
    return [{"ugc_id": 120, "item_id": 203003, "position": [-430.0, 1.543, 200.0]},
            {"ugc_id": 121, "item_id": 203004, "position": [-430.0, 53.0, 205.0]},
            {"ugc_id": 125, "item_id": 202041, "position": [-950.0, 1.543, 400.0]}]


def _live_events():
    """地图现状：与 _machine_doc 完全一致（含非 0 位事件键）。"""
    return {
        "进入提示": {"refs": [
            {"ugc_id": 120, "property_id": "20300311106", "current_value": "['进入提示', 1286]"},
            {"ugc_id": 121, "property_id": "20300411205", "current_value": "['进入提示']"}]},
        "人数达到": {"refs": [
            {"ugc_id": 125, "property_id": "20204111101",
             "current_value": "[1, 1.0, '人数达到']"}]},
    }


class FieldContractTest(unittest.TestCase):
    def test_param_names_align_with_info(self):
        """每个登记字段的参数名列表都要与 info 逐位对齐（键的位置由名字标出）。"""
        for pid, (_role, semantic, names) in event_export.FIELD_SEMANTICS.items():
            self.assertTrue(names, pid)
            self.assertEqual(names.count("事件键"), 1,
                             "%s(%s) 应恰好有一个「事件键」位" % (semantic, pid))

    def test_event_key_index_matches_native_shape(self):
        """事件键下标 = 80001 段展平后的位置（与游戏侧 event_field_shape 同判据）。"""
        expect = {"20300311106": 0, "20300411205": 0, "10000103004": 0,
                  "20200303005": 1, "20204111101": 2}
        for pid, idx in expect.items():
            names = event_export.FIELD_SEMANTICS[pid][2]
            self.assertEqual(names.index("事件键"), idx, pid)

    def test_decode_value_labels_non_zero_key_slot(self):
        got = event_export.decode_value("20204111101", [1, 1.0, "人数达到"])
        self.assertEqual(got, {"玩家角色数量": 1, "持续时间": 1.0, "事件键": "人数达到"})
        got = event_export.decode_value("20200303001", ["你好", 1286])
        self.assertEqual(got["事件接收者"], "全部组件(1286)")


class DslRoundTripTest(unittest.TestCase):
    def setUp(self):
        if not event_dsl.yaml:
            self.skipTest("需要 PyYAML")
        os.makedirs(TMP, exist_ok=True)
        self.machine = os.path.join(TMP, "machine.json")
        self.dsl = os.path.join(TMP, "events.yaml")
        self.back = os.path.join(TMP, "machine2.json")
        json.dump(_machine_doc(), open(self.machine, "w", encoding="utf-8"), ensure_ascii=False)

    def tearDown(self):
        for p in (self.machine, self.dsl, self.back):
            if os.path.exists(p):
                os.remove(p)

    def test_dsl_is_chinese_and_keeps_key_slot(self):
        event_dsl.to_dsl(self.machine, self.dsl)
        text = open(self.dsl, encoding="utf-8").read()
        self.assertIn("玩家角色数量达标", text)
        self.assertIn("时机: 进入区域时", text)
        self.assertNotIn("未解码字段", text)
        doc = event_dsl.yaml.safe_load(text)
        when = [e for e in doc["事件"] if e["事件"] == "人数达到"][0]["当"]
        self.assertEqual(when["玩家角色数量"], 1)
        self.assertEqual(when["持续时间"], 1.0)

    def test_to_dsl_then_from_dsl_is_lossless(self):
        event_dsl.to_dsl(self.machine, self.dsl)
        event_dsl.from_dsl(self.dsl, self.back, source_machine=self.machine)
        back = {e["key"]: {str(b["property_id"]): b["raw_value"]
                           for b in e["bindings"]}
                for e in json.load(open(self.back, encoding="utf-8"))["events"]}
        want = {e["key"]: {str(b["property_id"]): b["raw_value"]
                           for b in e["bindings"]}
                for e in _machine_doc()["events"]}
        self.assertEqual(back, want)     # 含 [1, 1.0, '人数达到'] 的键位不漂移


class ImportPlanTest(unittest.TestCase):
    def test_identical_file_produces_no_changes(self):
        plan = event_import.build_plan(_machine_doc(), _live_events(), _live_objs(), 2.0, False)
        self.assertEqual(plan["create_events"], [])
        self.assertEqual(plan["bind"], [])
        self.assertEqual(plan["update"], [])
        self.assertEqual(plan["issues"], [])

    def test_unlabeled_object_falls_back_to_coordinates_without_crash(self):
        """对象没有用户标签时走坐标匹配（曾因 match_anchor 返回值解包错误直接崩溃）。"""
        doc = _machine_doc()
        doc["events"][1]["bindings"][0]["object"] = "DETECT_REGION_125"   # 不在 labels.json
        plan = event_import.build_plan(doc, _live_events(), _live_objs(), 2.0, False)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["bind"], [])

    def test_changed_threshold_is_reported_as_update(self):
        doc = _machine_doc()
        doc["events"][1]["bindings"][0]["raw_value"] = [2, 1.0, "人数达到"]
        plan = event_import.build_plan(doc, _live_events(), _live_objs(), 2.0, False)
        self.assertEqual([(u["ugc_id"], u["property_id"], u["value"]) for u in plan["update"]],
                         [(125, "20204111101", [2, 1.0, "人数达到"])])


if __name__ == "__main__":
    unittest.main()
