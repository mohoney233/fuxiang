# -*- coding: utf-8 -*-
"""fuxiang_catalog 的离线回归测试。

用假 room / 假数据表复现「组件 + 物体」注册表的判定逻辑，不接触游戏进程。
覆盖：类型↔实现类映射、孤儿定性、命名不臆测、分类挂载、place_support 来源、
统一寻址的输入校验与缓存失效。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import fuxiang_catalog as catalog  # noqa: E402


class WorldBattleUGCEdit:
    def create_unit_with_info(self, info, unused):
        pass


class FakeDM:
    """只提供注册表真正读取的那几张表。"""

    def __init__(self):
        self.all_tid_data = {
            "TID_UGC_ITEM_3028": "桌子1",
            "TID_UGC_ITEM_202014": "地下室盖板",
            "TID_UGC_ITEM_202020": "三连蹦床",
            "TID_UGC_ITEM_201005": "胜利条件",
            "TID_UGC_CLASSIFY_3": "物件",
            "TID_UGC_CLASSIFY_2": "功能",
            "TID_UGC_CLASSIFY_304": "桌子",
            "TID_UGC_CLASSIFY_210": "噩梦逐影",
        }
        # item_id -> 记录；202109 的 component_id 指向不存在的实现类
        self.ugc_item_data = {
            3028: {"item_tid": "TID_UGC_ITEM_3028", "item_type": 1,
                   "component_id": 100000, "model_path": "scene/table.gim",
                   "occupancy_value": "2.44", "zoom_limit": (0.1, 1000),
                   "is_visible": 0, "tap_order_sec": 304, "simple_hide": 0},
            202014: {"item_tid": "TID_UGC_ITEM_202014", "item_type": 1,
                     "component_id": 202014, "model_path": "scene/cover.gim",
                     "occupancy_value": "3.37", "is_visible": 0,
                     "tap_order_sec": 210, "simple_hide": 0},
            202020: {"item_tid": "TID_UGC_ITEM_202020", "item_type": 1,
                     "component_id": 202020, "model_path": "scene/trampoline.gim",
                     "occupancy_value": "38.28", "is_visible": 1,
                     "tap_order_sec": 210, "simple_hide": 1},
            201005: {"item_tid": "TID_UGC_ITEM_201005", "item_type": 1,
                     "component_id": 201005, "model_path": "chr/prop/x.gim",
                     "occupancy_value": "1", "is_visible": 1,
                     "tap_order_sec": 210, "simple_hide": 1},
            10012001: {"item_tid": "TID_UGC_ITEM_10012001", "item_type": 2,
                       "component_id": 10012001, "model_path": "fx/model/fx_empty.gim",
                       "occupancy_value": "1", "is_visible": 1,
                       "tap_order_sec": 210, "simple_hide": 1},
        }
        # 属性 schema：100000 与 202020 有；201005/10012001 没有
        self.ugc_item_property = {100000: {"1": {"20": {"1": {"id": 100000}}}},
                                  202020: {"1": {"100": {"1": {"id": 202020}}}}}
        # 操控面板 schema：只有 10012001
        self.ugc_control_panel = {10012001: {"25001": {"12": {"1": {"item_id": 1}}}}}
        # 分类树：group 3 -> child 304；group 2 -> child 210
        self.ugc_item_tab = {
            3: {0: {"tab_tid": "TID_UGC_CLASSIFY_3", "child_id": 0},
                304: {"tab_tid": "TID_UGC_CLASSIFY_304", "child_id": 304,
                      "sort_id": 14, "simple_hide": 0}},
            2: {0: {"tab_tid": "TID_UGC_CLASSIFY_2", "child_id": 0},
                210: {"tab_tid": "TID_UGC_CLASSIFY_210", "child_id": 210,
                      "sort_id": 1, "simple_hide": 0}},
        }


def fixture():
    room = WorldBattleUGCEdit()
    cm = NS(_ugc_id2component={1: NS(_owner_uid=1000001)},
            _component_type2ugc_ids={100000: {1}},
            _component_type2str={100000: "OBSTACLE", 202014: "BASEMENT"},
            _component_factory={100000: object()},
            _ugc_id_counter=0)
    room.ugc_component_manager = cm
    room.unit_mgr = NS(get_unit=lambda uid: None)
    room.create_unit_with_info.__globals__["GK"] = NS(DM=FakeDM())
    api = NS(get_room=lambda: room, get_component_manager=lambda: cm)
    return api, room, cm


_ENUMS_STUB = types.ModuleType("stub_ugc_enums")


class _MotionType:
    PATH = 1641


_MotionType.__module__ = "common_cs.ugc.UGCEnums"
_ENUMS_STUB.MotionType = _MotionType


class _FakeComponentClass:
    COMPONENT_TYPE = 202020


_FAKE_MOD = types.ModuleType("common_cs.combat_core.components.unit_ugc.UGCFakeComponent")
_FAKE_MOD.UGCFakeComponent = _FakeComponentClass


class CatalogTests(unittest.TestCase):
    def setUp(self):
        catalog.invalidate()

    def tearDown(self):
        catalog.invalidate()

    # ---- 纯函数 ----
    def test_count_handles_set_int_and_missing(self):
        self.assertEqual(catalog._count({100000: {1, 2, 3}}, 100000), 3)
        self.assertEqual(catalog._count({100000: 7}, 100000), 7)
        self.assertEqual(catalog._count({}, 100000), 0)
        self.assertEqual(catalog._count(None, 100000), 0)
        self.assertEqual(catalog._count({"100000": [9]}, 100000), 1)

    def test_plain_normalises_non_json_values(self):
        class Vec:
            x, y, z = 1.0, 2.0, 3.0
        self.assertEqual(catalog._plain(Vec()), [1.0, 2.0, 3.0])
        self.assertEqual(catalog._plain((0.1, 1000)), [0.1, 1000])
        self.assertIsNone(catalog._plain(None))

    def test_orphan_kinds_are_evidence_based(self):
        self.assertEqual(catalog._classify_orphan(1, None, True, False)[0],
                         "mode_control_panel")
        self.assertEqual(catalog._classify_orphan(1, None, False, True)[0],
                         "schema_without_implementation")
        hidden = {"catalog_hidden": True}
        self.assertEqual(catalog._classify_orphan(1, hidden, False, False)[0],
                         "hidden_catalog_record")
        self.assertEqual(catalog._classify_orphan(1, {}, False, False)[0],
                         "no_implementation")

    # ---- 索引构建 ----
    def test_item_links_to_category_and_component(self):
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        it = d["items"][3028]
        self.assertEqual(it["name_cn"], "桌子1")
        self.assertEqual(it["category_cn"], "桌子")
        self.assertEqual(it["category_group_cn"], "物件")
        self.assertEqual(it["component_type"], 100000)
        self.assertFalse(it["catalog_hidden"])
        self.assertTrue(it["object_list_eligible"])

    def test_component_name_is_not_guessed_from_tid(self):
        """100000 没有自己的目录行，不允许用 TID_UGC_ITEM_100000 猜名字。"""
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        c = d["components"][100000]
        self.assertIsNone(c["name_cn"])
        self.assertIsNone(c["name_cn_source"])
        # 但仍给出代表性物件，明确标为「代表性」而非类型名
        self.assertEqual(c["representative_item_name_cn"], "桌子1")
        self.assertTrue(c["creatable"])

    def test_component_with_own_catalog_row_uses_it_as_name(self):
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        c = d["components"][202014]
        self.assertEqual(c["name_cn"], "地下室盖板")
        self.assertEqual(c["name_cn_source"], "own_catalog_item")
        self.assertTrue(c["creatable"])

    def test_registered_type_is_creatable_without_loaded_class(self):
        """creatable 取决于登记表，不取决于「类当前是否已加载」。

        游戏是按登记表里的名字动态 import 实现类的；若把类扫描结果当判据，
        尚未被 import 的合法类型会被误判为不可创建。
        """
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        self.assertNotIn("ugc", sys.modules.get("nosuch", ""))
        c = d["components"][100000]
        self.assertTrue(c["registered"])
        self.assertFalse(c["class_resolvable"])      # 测试环境扫不到类
        self.assertTrue(c["creatable"])              # 但仍可创建
        self.assertIn(100000, d["counts"]["component_types_class_scan_unconfirmed"])

    def test_types_without_implementation_are_not_creatable(self):
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        c = d["components"]
        self.assertFalse(c[202020]["creatable"])
        self.assertEqual(c[202020]["not_creatable_kind"],
                         "schema_without_implementation")
        self.assertEqual(c[10012001]["not_creatable_kind"], "mode_control_panel")
        self.assertEqual(c[201005]["not_creatable_kind"], "hidden_catalog_record")
        self.assertTrue(c[10012001]["control_panel_schema"])
        self.assertFalse(c[10012001]["property_schema"])

    def test_class_scan_drives_creatable(self):
        """把带 COMPONENT_TYPE 的类放进 sys.modules，孤儿应变为可创建。"""
        api, _, cm = fixture()
        cm._component_type2str[202020] = "FAKE"
        sys.modules[_FAKE_MOD.__name__] = _FAKE_MOD
        try:
            d = catalog.build(api, force=True)
            self.assertEqual(d["components"][202020]["class"], "UGCFakeComponent")
            self.assertTrue(d["components"][202020]["class_resolvable"])
            self.assertTrue(d["components"][202020]["creatable"])
            self.assertNotIn(202020, d["counts"]["component_types_without_class"])
        finally:
            sys.modules.pop(_FAKE_MOD.__name__, None)

    def test_place_support_comes_from_scene_tables(self):
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        # 100000 在 fuxiang_scene.VERIFIED_TYPES 里
        self.assertEqual(d["components"][100000]["place_support"], "verified")
        # 202014 也是实测已验证的类型
        self.assertEqual(d["components"][202014]["place_support"], "verified")
        # 未实测的类型不应被说成 verified / unsupported
        self.assertEqual(d["components"][202020]["place_support"], "unsupported")
        # 101001/102001 角色类：2026-09-30 复审移出 UNSUPPORTED（服务端受理，
        # EDIT_OPS.md §11 已实证「创建时带 pos」能落库）。但摆放状态机那条专用
        # 分支还没有实测记录 ⇒ 不是 verified，也不是 unsupported。
        verified, unsupported, server = catalog._place_support()
        for role in (101001, 102001):
            self.assertNotIn(role, unsupported)
            self.assertIn(role, server)
            self.assertNotIn(role, verified)
        # 204041 当年的 "failed to create model!" 出在本地构造路径
        # （目录 model_path 指向 .pse 特效），服务端路径从未试过
        self.assertNotIn(204041, unsupported)
        self.assertIn(204041, server)
        # 2026-10-01 实机：摆 → 保存 → **重进**逐位回读一致（成对创建、两件各落各位）
        # ⇒ 才算 verified。角色 101001 只到 placed+本地回读，所以仍在上面保持非 verified。
        self.assertIn(204041, verified)

    def test_counts_are_internally_consistent(self):
        api, _, _ = fixture()
        d = catalog.build(api, force=True)
        cnt = d["counts"]
        self.assertEqual(cnt["items_total"], len(d["items"]))
        self.assertEqual(
            cnt["component_types_total"],
            cnt["component_types_creatable"] + len(cnt["component_types_without_class"]))
        self.assertEqual(cnt["items_without_category"], 0)

    def test_cache_reuses_and_refreshes(self):
        api, room, cm = fixture()
        first = catalog.build(api)
        self.assertIs(catalog.build(api), first)       # 命中缓存，返回同一对象
        cm._ugc_id2component[2] = NS(_owner_uid=1000002)
        self.assertIsNot(catalog.build(api), first)    # 索引规模变了 → 缓存失效
        # force=True 每次都重建，因此只保证内容一致，不保证是同一对象
        self.assertEqual(catalog.build(api, force=True),
                         catalog.build(api, force=True))

    # ---- op 层 ----
    def test_find_requires_a_selector(self):
        api, _, _ = fixture()
        self.assertIn("error", catalog.op_find(api, {}))

    def test_find_by_item_id_reports_component(self):
        api, _, _ = fixture()
        r = catalog.op_find(api, {"item_id": 3028})
        m = r["matches"][0]
        self.assertEqual(m["kind"], "item")
        self.assertEqual(m["item"]["name_cn"], "桌子1")
        self.assertEqual(m["component_type"]["name"], "OBSTACLE")

    def test_find_unknown_item_id_is_an_error_not_a_silent_miss(self):
        api, _, _ = fixture()
        r = catalog.op_find(api, {"item_id": 999999})
        self.assertIn("error", r)

    def test_find_by_query_matches_chinese_name(self):
        api, _, _ = fixture()
        r = catalog.op_find(api, {"query": "桌子"})
        kinds = [m["kind"] for m in r["matches"]]
        self.assertIn("item", kinds)
        self.assertTrue(any(m.get("item", {}).get("item_id") == 3028
                            for m in r["matches"]))

    def test_find_enum_uses_injected_enum_module(self):
        api, _, _ = fixture()
        catalog._CACHE["enums"] = {"MotionType": {"PATH": 1641}}
        r = catalog.op_find(api, {"enum": "MotionType"})
        self.assertEqual(r["matches"][0]["members"]["PATH"], 1641)

    def test_list_categories_reports_item_counts(self):
        api, _, _ = fixture()
        r = catalog.op_list_categories(api, {})
        by_id = {c["category_id"]: c for c in r["categories"]}
        self.assertEqual(by_id[304]["name_cn"], "桌子")
        self.assertEqual(by_id[304]["item_count"], 1)
        self.assertEqual(by_id[210]["item_count"], 4)

    def test_component_schema_reports_absence_honestly(self):
        api, _, _ = fixture()
        r = catalog.op_component_schema(api, {"component_type": 201005})
        self.assertFalse(r["available"])
        self.assertIn("note", r)
        r2 = catalog.op_component_schema(api, {"component_type": 100000})
        self.assertTrue(r2["available"])
        self.assertIsNotNone(r2["sources"]["ugc_item_property"])

    def test_invalid_component_type_argument(self):
        api, _, _ = fixture()
        self.assertIn("error", catalog.op_get_component_type(api, {"component_type": "x"}))
        self.assertIn("error", catalog.op_find(api, {"component_type": "x"}))


if __name__ == "__main__":
    unittest.main()
