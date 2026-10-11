#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_restore — 快照还原的纯逻辑锁（无需游戏）。

还原是**写操作**，计划算错就会往地图里摆错东西或多摆骨架件，所以这里锁住：
1. 全局单例 / 地图骨架 / 剧情 / UI / 运动点 / 哨兵位 默认都要跳过；
2. `component_id` 必须取自 component_info，**不能**用 item_id（两套命名空间！）；
3. 事件引用必须按新 ugc_id 重映射，映射不到的要报 dangling 而不是乱绑。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402


class _A:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, k):
        return None


def _c(g, item, cid=None, p=(0.0, 0.0, 0.0), ci=None):
    return {"g": g, "i": item, "p": list(p), "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps(ci if ci is not None
                             else {"component_id": cid or item,
                                   "events": []}, ensure_ascii=False)}


class TestComponentId(unittest.TestCase):
    def test_prefers_component_info_over_item_id(self):
        """item_id ≠ component_type（301001 是目录项，真实类型是 100001）。

        拿错就会把属性写到类型不符的组件上（护栏会拒，但计划阶段就该对）。
        """
        c = {"g": 1, "i": 301001,
             "ci": json.dumps({"component_id": 100001, "events": []})}
        self.assertEqual(sr.component_id_of(c), 100001)

    def test_falls_back_to_item_id(self):
        self.assertEqual(sr.component_id_of({"g": 1, "i": 203003}), 203003)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.types = {100001, 203003, 203004, 203005, 203006}

    def test_global_singletons_skipped(self):
        comps = [_c(1, 10009001, 10009001),       # SKYBOX
                 _c(2, 10003002, 10003002),       # LUT
                 _c(3, 10001001, 10001001)]       # GLOBAL
        to_place, skipped = sr.classify_components(comps, self.types)
        self.assertEqual(to_place, [])
        self.assertEqual(len(skipped), 3)
        self.assertTrue(all("global_singleton" in s["reason"] for s in skipped))

    def test_skeleton_and_story_and_ui_and_motion_skipped(self):
        comps = [_c(1, 201009, 201009),      # ANCHOR 骨架
                 _c(2, 10007001, 10007001),  # STORY
                 _c(3, 200001, 200001),      # UI_NATIVE
                 _c(4, 203912, 203912),      # MOTION_POINT
                 _c(5, 100001, 100001)]      # 正常实体
        to_place, skipped = sr.classify_components(comps, self.types)
        self.assertEqual([c["g"] for c in to_place], [5])
        reasons = {s["reason"].split("（")[0] for s in skipped}
        self.assertEqual(reasons, {"skeleton", "story_stage", "ui", "motion_point"})

    def test_sentinel_position_skipped(self):
        comps = [_c(1, 203911, 203911, p=(0.0, -65536.0, 0.0))]
        # 用 None 关掉白名单检查：这里要验的是哨兵位规则本身
        to_place, skipped = sr.classify_components(comps, None)
        self.assertEqual(to_place, [])
        self.assertIn("sentinel_position", skipped[0]["reason"])

    def test_not_placeable_skipped(self):
        comps = [_c(1, 101001, 101001)]      # 角色类，不在 verified 白名单
        to_place, skipped = sr.classify_components(comps, self.types)
        self.assertEqual(to_place, [])
        self.assertIn("not_placeable", skipped[0]["reason"])

    def test_include_flags_open_the_gates(self):
        comps = [_c(1, 10009001, 10009001), _c(2, 200001, 200001),
                 _c(3, 203912, 203912), _c(4, 201009, 201009)]
        to_place, _ = sr.classify_components(
            comps, None, include_ui=True, include_motion=True,
            include_global=True)
        self.assertEqual(sorted(c["g"] for c in to_place), [1, 2, 3, 4])

    def test_placeable_types_none_disables_check(self):
        comps = [_c(1, 999999, 999999)]
        to_place, _ = sr.classify_components(comps, None)
        self.assertEqual(len(to_place), 1)


class TestPlaceSpec(unittest.TestCase):
    def test_sentinel_y_falls_back_to_zero(self):
        spec = sr.place_spec(_c(1, 203911, 203911, p=(10.0, -65536.0, 20.0)))
        self.assertEqual(spec["position"][1], 0.0)
        self.assertEqual(spec["position"][0], 10.0)
        self.assertEqual(spec["position"][2], 20.0)

    def test_keeps_item_id_and_transform(self):
        c = _c(7, 203003, 203003, p=(1.0, 2.0, 3.0))
        c["r"] = [0, 90, 0]
        c["s"] = [2.0, 1.0, 3.0]
        spec = sr.place_spec(c)
        self.assertEqual(spec["item_id"], 203003)
        self.assertEqual(spec["rotation"], [0.0, 90.0, 0.0])
        self.assertEqual(spec["scale"], [2.0, 1.0, 3.0])
        self.assertEqual(spec["old_g"], 7)


class TestRealPositionField(unittest.TestCase):
    """`pp` 是单元上的真实坐标（快照只在它与 `p` 不符时记）。

    实机证据：春岚 203022 交互区（触发「点击播放」的那件）`get_position3()`
    返回哨兵 `[0,-65536,0]`，而 `unit.position` = `[928, -67.923, -147]`。
    漏了它，还原出来的图会把观影触发区摆到原点 —— 地图看着一样，点不动。
    """

    def _area(self, pp=(928.0, -67.923, -147.0)):
        c = _c(14994, 203022, 203022, p=(0.0, -65536.0, 0.0))
        if pp is not None:
            c["pp"] = list(pp)
        return c

    def test_sentinel_p_with_pp_is_placed_not_skipped(self):
        to_place, skipped = sr.classify_components([self._area()], None)
        self.assertEqual([c["g"] for c in to_place], [14994])
        self.assertEqual(skipped, [])

    def test_sentinel_p_without_pp_still_skipped(self):
        comps = [_c(1, 203911, 203911, p=(0.0, -65536.0, 0.0))]
        to_place, skipped = sr.classify_components(comps, None)
        self.assertEqual(to_place, [])
        self.assertIn("sentinel_position", skipped[0]["reason"])

    def test_place_spec_prefers_pp(self):
        self.assertEqual(sr.place_spec(self._area())["position"],
                         [928.0, -67.923, -147.0])

    def test_pp_holding_sentinel_y_is_not_a_real_position(self):
        c = self._area(pp=(0.0, -65536.0, 0.0))
        self.assertFalse(sr.has_real_position(c))
        # 仍走 --allow-sentinel 的 y=0 兜底，不能把哨兵 y 当真坐标摆出去
        self.assertEqual(sr.place_spec(c)["position"][1], 0.0)


class TestGroupCreateCollapse(unittest.TestCase):
    """一次创建出多件的类型（204041 推荐检查点）必须把快照里互指的两件并成一个请求。

    实机 2026-10-01：`add_unit(item 204041)` 一次产出 2 个实体，且两件
    `component_info['group_components']` 由引擎自动写成互指 ⇒ 那一对本就是一次产物。
    按件各发一次请求会摆出 4 件（翻倍），按请求算预算又会少报。
    """

    def _pair(self):
        a = _c(14992, 204041, 204041, p=(936.099, 61.745, -232.759),
               ci={"component_id": 204041, "events": [], "group_components": [14993]})
        b = _c(14993, 204041, 204041, p=(927.464, 162.066, -210.032),
               ci={"component_id": 204041, "events": [], "group_components": [14992]})
        return [a, b]

    def test_pair_collapses_into_one_request(self):
        comps = self._pair()
        specs = sr.collapse_group_creates([sr.place_spec(c) for c in comps],
                                          {c["g"]: c for c in comps})
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["old_g"], 14992)
        self.assertEqual(specs[0]["group_old_gs"], [14993])
        # 成员各自该落的变换要带上，否则两件叠在同一处
        self.assertEqual(specs[0]["group_transforms"][0]["position"],
                         [927.464, 162.066, -210.032])
        self.assertEqual(sr.covered_components(specs), 2)

    def test_to_place_and_pilot_filter_use_component_count(self):
        comps = self._pair()
        plan = sr.build_plan({"components": comps}, None,
                             sr.argparse.Namespace(only_ids="14993"))
        self.assertEqual(len(plan["specs"]), 1)      # 一个请求
        self.assertEqual(plan["to_place"], 2)        # 但覆盖两件（预算口径不能少算）
        plan_all = sr.build_plan({"components": comps}, None, None)
        self.assertEqual(plan_all["to_place"], 2)

    def test_unpaired_component_is_left_alone(self):
        """没有互指伙伴的单件不许被凭空成组（会等一个永远不来的成员）。"""
        one = [_c(7, 204041, 204041)]
        specs = sr.collapse_group_creates([sr.place_spec(one[0])], {7: one[0]})
        self.assertEqual(len(specs), 1)
        self.assertNotIn("group_old_gs", specs[0])

    def test_types_table_matches_the_bridge_source(self):
        """还原侧的 GROUP_CREATE_TYPES 必须与桥接真源一致（AST 读，不 import 引擎模块）。"""
        import ast
        path = os.path.join(ROOT, "bridge", "fuxiang_scene.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        got = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "GROUP_CREATE_TYPES" for t in node.targets):
                got = ast.literal_eval(node.value)
        self.assertIsNotNone(got, "桥接里 GROUP_CREATE_TYPES 不见了")
        self.assertEqual(got, sr.GROUP_CREATE_TYPES)


class TestPropsRegistryEscalation(unittest.TestCase):
    """整袋回写被"不在编辑器登记集合"拦下时，必须带 `allow_unregistered` 重试一次。

    2026-10-01 实机：春岚的运动器 `placed` 之后属性回写被这条护栏拒
    （「物件不在编辑器登记集合内」）⇒ 运动层会还原成**空壳**，而 CLI 只报"失败 1"。
    运动器/音乐事件/剧情三件套这类挂在全局单元上的组件天生不在登记集合里
    （同 2026-09-23 单例同步那次），所以这是必要出口，不是绕过校验。
    """

    def _motion(self):
        return _c(14841, 203911, 203911, p=(0.0, -65536.0, 0.0),
                  ci={"component_id": 203911,
                      "events": [{"type": 20391119001,
                                  "info": [[[1.0, [0, 0, 0], [0, 0, 0], [0, 0, 0]]]]}]})

    def _run(self, errors):
        """errors: 每次 set_component_info 的返回（按调用顺序）。返回 (结果, 收到的参数)。"""
        from unittest.mock import Mock
        seen = []

        def fake(op, args, timeout=None):
            seen.append(args)
            return errors[len(seen) - 1]

        m = Mock(side_effect=fake)
        old = sr.call_edit
        sr.call_edit = m
        try:
            r = sr.apply_props(15003, self._motion(), {})
        finally:
            sr.call_edit = old
        return r, seen

    def test_escalates_once_on_registry_guard(self):
        r, seen = self._run([
            {"ok": False, "error": "物件不在编辑器登记集合内（可能尚未完成放置确认）"},
            {"ok": True}])
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(seen), 2)
        self.assertNotIn("allow_unregistered", seen[0])   # 第一次先按严格护栏走
        self.assertTrue(seen[1]["allow_unregistered"])
        self.assertEqual(r.get("escalated"), "allow_unregistered")

    def test_other_errors_are_not_masked_by_a_retry(self):
        """别的错误（类型不符等）不许被重试掩盖 —— 那正是护栏该拦的方向。"""
        r, seen = self._run([{"ok": False, "error": "组件类型不符：期望 203911，实际 100001"}])
        self.assertEqual(len(seen), 1)
        self.assertFalse(r.get("ok"))

    def test_still_failing_after_retry_is_reported(self):
        r, seen = self._run([
            {"ok": False, "error": "物件不在编辑器登记集合内"},
            {"ok": False, "error": "物件不在编辑器登记集合内"}])
        self.assertEqual(len(seen), 2)
        self.assertFalse(r.get("ok"))
        self.assertNotIn("escalated", r)


class TestEventPlan(unittest.TestCase):
    def _events(self):
        return {"count": 1, "events": [
            {"event_key": "踏入灯下", "sort_id": 0, "refs": [
                {"ugc_id": 152, "index": 5, "params_idx": 0,
                 "property_id": 20300311106, "current_value": "['踏入灯下', 1286]"}],
             "in_save": True}]}

    def test_refs_remapped_to_new_ids(self):
        actions, dangling = sr.event_bind_plan(self._events(), {152: 900152})
        self.assertEqual(dangling, [])
        self.assertEqual(actions[0]["ugc_id"], 900152)
        self.assertEqual(actions[0]["info"], ["踏入灯下", 1286])
        self.assertEqual(actions[0]["property_id"], 20300311106)

    def test_missing_mapping_reported_as_dangling(self):
        actions, dangling = sr.event_bind_plan(self._events(), {})
        self.assertEqual(actions, [])
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0]["old_ugc_id"], 152)

    def test_parse_current_value(self):
        self.assertEqual(sr.parse_current_value("['踏入灯下', 1286]"),
                         ["踏入灯下", 1286])
        self.assertEqual(sr.parse_current_value("[1]"), [1])
        self.assertIsNone(sr.parse_current_value("<deep>"))
        self.assertIsNone(sr.parse_current_value(None))


class TestCrossMapPreflight(unittest.TestCase):
    """跨图预检：把 A 图搭的场景搬到 B 图，先算清楚能不能落得下。"""

    def test_bbox_of_specs(self):
        specs = [{"position": [0.0, 1.0, -10.0]}, {"position": [10.0, 5.0, 20.0]}]
        b = sr.bbox_of(specs)
        self.assertEqual(b["x"], [0.0, 10.0])
        self.assertEqual(b["y"], [1.0, 5.0])
        self.assertEqual(b["z"], [-10.0, 20.0])

    def test_bbox_empty(self):
        self.assertIsNone(sr.bbox_of([]))

    def test_anchor_bounds_gamearea(self):
        a = {"gamearea0": [[1, [1120, 0, 1120]], [2, [1120, 0, -1440]],
                           [3, [-1440, 0, -1440]], [4, [-1440, 0, 1120]]]}
        b = sr.anchor_bounds(a)
        self.assertEqual(b["x"], [-1440.0, 1120.0])
        self.assertEqual(b["z"], [-1440.0, 1120.0])

    def test_anchor_bounds_garbage(self):
        self.assertIsNone(sr.anchor_bounds(None))
        self.assertIsNone(sr.anchor_bounds({"x": 1}))
        self.assertIsNone(sr.anchor_bounds({"gamearea0": [[1, [0, 0, 0]]]}))

    def test_framework_same_and_diff(self):
        self.assertTrue(sr.check_framework("4002", "4002")["same"])
        self.assertEqual(sr.check_framework("4002", "4002")["level"], "ok")
        d = sr.check_framework("4002", "4001")
        self.assertFalse(d["same"])
        self.assertEqual(d["level"], "warn")

    def test_framework_unknown_is_not_fatal(self):
        # 任一侧取不到 → 不报 warn，避免噪音拦住还原
        self.assertTrue(sr.check_framework(None, "4002")["same"])
        self.assertTrue(sr.check_framework("4002", None)["same"])

    def test_bounds_inside_and_outside(self):
        bbox = {"x": [0.0, 800.0], "y": [0.0, 75.0], "z": [-700.0, 200.0]}
        bounds = {"x": [-1440.0, 1120.0], "z": [-1440.0, 1120.0]}
        self.assertEqual(sr.check_bounds(bbox, bounds)["level"], "ok")
        out = sr.check_bounds({"x": [0.0, 2000.0], "z": [0.0, 1.0]}, bounds)
        self.assertEqual(out["level"], "warn")
        self.assertIn("x", out["msg"])

    def test_bounds_missing_is_unknown(self):
        self.assertEqual(sr.check_bounds(None, {"x": [0, 1], "z": [0, 1]})["level"],
                         "unknown")

    def test_budget_fits_and_not(self):
        snap = {"occupancy": 1390, "component_nums": 341}
        tgt = {"occupancy": 100, "max_occupancy": 30000,
               "component_nums": 20, "max_component_nums": 15000}
        r = sr.check_budget(snap, tgt)
        self.assertEqual(r["level"], "ok")
        r2 = sr.check_budget({"occupancy": 29999, "component_nums": 1}, tgt)
        self.assertEqual(r2["level"], "error")

    def test_budget_missing_is_unknown(self):
        self.assertEqual(sr.check_budget(None, {})["level"], "unknown")


class TestGlobalSingletonPairing(unittest.TestCase):
    """全局单例：按类型**就地覆写**，不是新建。配对错了会把天空盒写进胜负条件。"""

    def _singleton(self, g, ct):
        return {"g": g, "i": ct, "p": [0, -65536, 0],
                "ci": json.dumps({"component_id": ct, "events": []})}

    def test_pairs_by_type(self):
        snap = [self._singleton(1, 10009001), self._singleton(2, 10003001)]
        tgt = [{"ugc_id": 54, "component_type": 10009001},
               {"ugc_id": 111, "component_type": 10003001}]
        pairs, missing = sr.pair_global_singletons(snap, tgt)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(missing, [])
        by = {p["component_type"]: p["target_ugc_id"] for p in pairs}
        self.assertEqual(by, {10009001: 54, 10003001: 111})

    def test_story_types_excluded(self):
        """⛔ 剧情三类编号也 ≥1e7，但不是单例——算进去会拿剧情去覆写单例。"""
        snap = [self._singleton(1, 10009001),
                self._singleton(2, 10007001), self._singleton(3, 10007002),
                self._singleton(4, 10007003)]
        tgt = [{"ugc_id": 54, "component_type": 10009001}]
        pairs, missing = sr.pair_global_singletons(snap, tgt)
        self.assertEqual([p["component_type"] for p in pairs], [10009001])

    def test_missing_in_target_reported(self):
        snap = [self._singleton(1, 10009001)]
        pairs, missing = sr.pair_global_singletons(snap, [])
        self.assertEqual(pairs, [])
        self.assertEqual([m["component_type"] for m in missing], [10009001])

    def test_entity_types_never_paired(self):
        """6 位编号的实体/道具绝不能进单例配对。"""
        snap = [{"g": 9, "i": 10065,
                 "ci": json.dumps({"component_id": 100000, "events": []})}]
        tgt = [{"ugc_id": 54, "component_type": 10009001}]
        pairs, _ = sr.pair_global_singletons(snap, tgt)
        self.assertEqual(pairs, [])


class TestSyncGlobalSingletonsArgs(unittest.TestCase):
    """同步单例必须带 `allow_unregistered=True`。

    ⛔ 骨架自带的全局单例（天空盒/雾/LUT/胜负条件）**不在** `ui.all_ugc_uid_set`
    （登记集合只收"编辑器摆放出来"的物件）⇒ 不带这个开关，写属性会被 `find_target`
    挡下。2026-09-23 跨图还原实测：8 处配对里 6 处全失败于这条护栏。
    """

    def setUp(self):
        self.sent = []
        self._orig_call = sr.call_edit
        self._orig_fetch = sr.fetch_target_singletons

        def fake_call(op, args, timeout=None):
            self.sent.append((op, args))
            return {"ok": True}

        sr.call_edit = fake_call
        sr.fetch_target_singletons = lambda: [
            {"ugc_id": 110, "component_type": 10001001},
            {"ugc_id": 111, "component_type": 10003001}]

    def tearDown(self):
        sr.call_edit = self._orig_call
        sr.fetch_target_singletons = self._orig_fetch

    def _snap(self):
        def one(g, ct):
            return {"g": g, "i": ct, "p": [0, -65536, 0],
                    "ci": json.dumps({"component_id": ct,
                                      "events": [{"type": 1, "info": ["x"]}]})}
        return {"components": [one(1, 10001001), one(2, 10003001)]}

    def test_forwards_allow_unregistered(self):
        r = sr.sync_global_singletons(self._snap())
        self.assertEqual(r["pairs"], 2)
        self.assertEqual(len(r["failed"]), 0)
        self.assertEqual(len(r["applied"]), 2)
        for op, args in self.sent:
            self.assertEqual(op, "set_component_info")
            self.assertIs(args.get("allow_unregistered"), True,
                          "单例同步必须显式放行未登记对象，否则被 find_target 拦下")

    def test_no_events_means_no_call(self):
        """空事件袋的单例不必写（省一次往返）。"""
        sr.fetch_target_singletons = lambda: [
            {"ugc_id": 110, "component_type": 10001001}]
        snap = {"components": [{"g": 1, "i": 10001001,
                                "ci": json.dumps({"component_id": 10001001,
                                                  "events": []})}]}
        r = sr.sync_global_singletons(snap)
        self.assertEqual(self.sent, [])
        self.assertEqual(r["applied"], [])


class TestOwnSlotInfo(unittest.TestCase):
    """槽位值必须取自**组件自身**事件袋，而不是引用里的 current_value。

    ⛔ 为什么必须这样：引用里的 `current_value` 由 `event_list` 解析而来，旧版本是从
    **单元级** params 读的 —— 多个组件共用同一 unit 时（实测 4 个 CHOICE 共用
    unit 1000306）读到的是**主组件**的值。旧快照因此存了错值。
    实测和风街区 9 条引用里 5 条错（「选项1」被记成 `['拆开信件']`、「测试」被记成 `[1]`、
    「剧情被触发」被记成 None）。照抄回写 = 把槽位写坏。
    """

    def _comp(self, g, events):
        return {"g": g, "i": 10007003, "p": [0, 0, 0],
                "ci": json.dumps({"component_id": 10007003,
                                  "events": events}, ensure_ascii=False)}

    def _snap(self):
        # 组件的**自身**袋：351 的 40003 里是「选项1」
        c351 = self._comp(351, [
            {"type": 1000700340001, "info": [350]},
            {"type": 1000700340002, "info": [0]},
            {"type": 1000700340007, "info": ["选项1"]},
            {"type": 1000700340003, "info": ["选项1"]}])
        return {"components": [c351]}

    def test_reads_from_component_bag(self):
        idx = sr._info_index(self._snap())
        info, ok = sr.own_slot_info(idx, 351, 6)   # 6-3=3 → 40003
        self.assertTrue(ok)
        self.assertEqual(info, ["选项1"])

    def test_out_of_range(self):
        idx = sr._info_index(self._snap())
        info, ok = sr.own_slot_info(idx, 351, 99)
        self.assertFalse(ok)
        self.assertIsNone(info)

    def test_index_below_header(self):
        """index < 3 落在变换三元组上，不是事件槽。"""
        idx = sr._info_index(self._snap())
        self.assertFalse(sr.own_slot_info(idx, 351, 2)[1])

    def test_missing_component(self):
        self.assertFalse(sr.own_slot_info({}, 351, 6)[1])

    def test_none_index(self):
        self.assertFalse(sr.own_slot_info(sr._info_index(self._snap()), 351, None)[1])

    def test_plan_ignores_polluted_current_value(self):
        """核心断言：current_value 是错的，也要用组件袋里的正确值。"""
        ev = {"count": 1, "events": [
            {"event_key": "选项1", "sort_id": 0, "refs": [
                {"ugc_id": 351, "index": 6, "params_idx": 0,
                 "property_id": 1000700340003,
                 "current_value": "['拆开信件']"}]}]}   # ← 旧快照里的污染值
        actions, _ = sr.event_bind_plan(ev, {351: 900351},
                                        comp_index=sr._info_index(self._snap()))
        self.assertEqual(actions[0]["info"], ["选项1"])
        self.assertEqual(actions[0]["info_source"], "component_bag")

    def test_plan_without_index_still_uses_current_value(self):
        """不给组件索引时退回旧行为（便于对老调用方保持兼容）。"""
        ev = {"count": 1, "events": [
            {"event_key": "选项1", "sort_id": 0, "refs": [
                {"ugc_id": 351, "index": 6, "params_idx": 0,
                 "property_id": 1000700340003, "current_value": "[1]"}]}]}
        actions, _ = sr.event_bind_plan(ev, {351: 900351})
        self.assertEqual(actions[0]["info"], [1])
        self.assertEqual(actions[0]["info_source"], "current_value")

    def test_plan_marks_field_default_when_nothing_available(self):
        """两者都取不到 → 交给 event_bind 按字段权威形态初始化，别硬塞错值。"""
        ev = {"count": 1, "events": [
            {"event_key": "剧情被触发", "sort_id": 0, "refs": [
                {"ugc_id": 349, "index": 5, "params_idx": 0,
                 "property_id": None, "current_value": None}]}]}
        actions, _ = sr.event_bind_plan(ev, {349: 900349},
                                        comp_index=sr._info_index(self._snap()))
        self.assertIsNone(actions[0]["info"])
        self.assertEqual(actions[0]["info_source"], "field_default")


class TestDanglingStoryRefs(unittest.TestCase):
    """事件 → 剧情节点的引用：place 阶段必悬空，靠 --with-story 重建后补绑。

    实测（和风街区）：9 条引用里 3 条直接可绑、6 条指向 STORY/CHOICE
    （#349 剧情1、#351 选项1、#352 选项2、#357 拆开信件、#358 塞回门缝）。
    """

    def _events(self):
        return {"count": 2, "events": [
            {"event_key": "踏入灯下", "sort_id": 0, "refs": [
                {"ugc_id": 152, "index": 5, "params_idx": 0,
                 "property_id": 20300311106, "current_value": "['踏入灯下', 1286]"}],
             "in_save": True},
            {"event_key": "选项1", "sort_id": 1, "refs": [
                {"ugc_id": 351, "index": 6, "params_idx": 0,
                 "property_id": 1000700340003, "current_value": "['选项1']"}],
             "in_save": True}]}

    def test_only_old_filters_to_dangling_batch(self):
        """第二遍补绑只处理这批旧 id —— 别把已绑好的重绑一遍。"""
        actions, dangling = sr.event_bind_plan(self._events(), {351: 900351},
                                               only_old={351})
        self.assertEqual(dangling, [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["old_ugc_id"], 351)
        self.assertEqual(actions[0]["ugc_id"], 900351)

    def test_only_old_empty_means_nothing(self):
        actions, dangling = sr.event_bind_plan(self._events(), {152: 900152},
                                               only_old=set())
        self.assertEqual(actions, [])
        self.assertEqual(dangling, [])

    def test_only_old_none_keeps_all(self):
        actions, _ = sr.event_bind_plan(self._events(), {152: 900152, 351: 900351})
        self.assertEqual(len(actions), 2)

    def test_action_carries_old_ugc_id(self):
        """actions 要带 old_ugc_id，才能回报"这条是补绑的第几条"。"""
        actions, _ = sr.event_bind_plan(self._events(), {152: 900152})
        self.assertEqual(actions[0]["old_ugc_id"], 152)

    def test_dangling_old_ids(self):
        olds = sr.dangling_old_ids(self._events(), {152: 900152})
        self.assertEqual(olds, {351})

    def test_dangling_old_ids_all_bound(self):
        self.assertEqual(sr.dangling_old_ids(self._events(),
                                             {152: 1, 351: 2}), set())


class TestMergeStoryIdMap(unittest.TestCase):
    """剧情 id 映射并入总映射：新 id 进 map，冲突必须报出来而不是静默覆盖。"""

    def test_merges_new_entries(self):
        id_map = {152: 900152}
        m = sr.merge_story_id_map(id_map, {"351": 900351, "349": 900349})
        self.assertEqual(m["merged"], 2)
        self.assertEqual(id_map[351], 900351)
        self.assertEqual(id_map[349], 900349)
        self.assertEqual(m["conflicts"], {})

    def test_keeps_existing_on_same_value(self):
        id_map = {351: 900351}
        m = sr.merge_story_id_map(id_map, {"351": 900351})
        self.assertEqual(m["merged"], 0)
        self.assertEqual(m["conflicts"], {})

    def test_reports_conflict(self):
        """同一个旧 id 被两套机制各自重建 → 必须人工看，不能默默取其一。"""
        id_map = {351: 111}
        m = sr.merge_story_id_map(id_map, {"351": 222})
        self.assertEqual(m["merged"], 0)
        self.assertEqual(m["conflicts"][351], {"existing": 111, "story": 222})
        self.assertEqual(id_map[351], 111)   # 不覆盖既有

    def test_empty(self):
        self.assertEqual(sr.merge_story_id_map({}, {})["merged"], 0)


class TestStoryIdMapFallback(unittest.TestCase):
    """兜底求交：老版本 op 不回 id_map 时，用快照剧情文档的 path 自己算。

    ⚠️ path 规则必须与 bridge/fuxiang_story._flatten 逐字一致。
    """

    def _doc(self):
        return {"stories": [
            {"ugc_id": 349, "dialogues": [
                {"ugc_id": 350, "choices": [{"ugc_id": 351}, {"ugc_id": 352}]}]},
            {"ugc_id": 353, "dialogues": [
                {"ugc_id": 354, "choices": [{"ugc_id": 357}, {"ugc_id": 358}]},
                {"ugc_id": 355, "choices": []},
                {"ugc_id": 356, "choices": []}]}]}

    def test_maps_by_path(self):
        created = [
            {"path": "story#1", "kind": "story", "ugc_id": 901},
            {"path": "story#1/dialogue#1", "kind": "dialogue", "ugc_id": 902},
            {"path": "story#1/dialogue#1/choice#1", "kind": "choice", "ugc_id": 903},
            {"path": "story#1/dialogue#1/choice#2", "kind": "choice", "ugc_id": 904},
            {"path": "story#2", "kind": "story", "ugc_id": 905},
            {"path": "story#2/dialogue#1", "kind": "dialogue", "ugc_id": 906},
            {"path": "story#2/dialogue#1/choice#1", "kind": "choice", "ugc_id": 907},
            {"path": "story#2/dialogue#1/choice#2", "kind": "choice", "ugc_id": 908},
            {"path": "story#2/dialogue#2", "kind": "dialogue", "ugc_id": 909},
            {"path": "story#2/dialogue#3", "kind": "dialogue", "ugc_id": 910},
        ]
        m = sr._story_id_map_from_created(self._doc(), created)
        self.assertEqual(m["349"], 901)
        self.assertEqual(m["351"], 903)
        self.assertEqual(m["352"], 904)
        self.assertEqual(m["357"], 907)
        self.assertEqual(m["358"], 908)
        self.assertEqual(m["356"], 910)

    def test_skips_missing_paths(self):
        """只建了一部分（断点续做）时不能瞎猜。"""
        m = sr._story_id_map_from_created(
            self._doc(), [{"path": "story#1", "kind": "story", "ugc_id": 901}])
        self.assertEqual(m, {"349": 901})

    def test_empty_created(self):
        self.assertEqual(sr._story_id_map_from_created(self._doc(), []), {})


class TestStoryMapFromTree(unittest.TestCase):
    """`--verify-only` 用目标图**当前剧情树**反推旧 id → 新 id。

    ⚠️ 为什么必须默认反推：落盘的 idmap 只含「摆放出来的物件」，剧情节点的旧 id 不在
    里面。不反推的话，指向剧情节点的事件引用会同时被报成「未映射」**和**「幽灵」——
    2026-09-23 实机把 9/9 全对误报成 3/9 + 6 条幽灵，就是这个条件写错了。
    ⚠️ 但也不能随便映射：内容对不上就宁可不映射（映射错 = 把事件绑到别的节点上）。
    """

    def _doc(self):
        return {"stories": [
            {"ugc_id": 349, "name": "剧情1", "dialogues": [
                {"ugc_id": 350, "content": "你好呀",
                 "choices": [{"ugc_id": 351, "content": "选项1"}]}]},
            {"ugc_id": 353, "name": "剧情2", "dialogues": [
                {"ugc_id": 354, "content": "信上只有一行小字", "choices": []}]}]}

    def _tree(self):
        return {"stories": [
            {"ugc_id": 352, "name": "剧情1", "dialogues": [
                {"ugc_id": 353, "content": "你好呀",
                 "choices": [{"ugc_id": 358, "content": "选项1"}]}]},
            {"ugc_id": 354, "name": "剧情2", "dialogues": [
                {"ugc_id": 355, "content": "信上只有一行小字", "choices": []}]}]}

    def test_maps_by_path_with_content_check(self):
        m, mism = sr.story_map_from_tree(self._doc(), self._tree())
        self.assertEqual(mism, [])
        self.assertEqual(m, {"349": 352, "350": 353, "351": 358,
                             "353": 354, "354": 355})

    def test_content_mismatch_refuses_to_map(self):
        """内容不一致 ⇒ 宁可 unmapped 也不能绑错节点。"""
        tree = self._tree()
        tree["stories"][0]["dialogues"][0]["content"] = "完全不同的台词"
        m, mism = sr.story_map_from_tree(self._doc(), tree)
        self.assertNotIn("350", m)
        self.assertEqual([x["path"] for x in mism],
                         ["story#1/dialogue#1"])
        # 兄弟节点不受影响
        self.assertEqual(m["351"], 358)

    def test_empty_doc_is_safe(self):
        self.assertEqual(sr.story_map_from_tree({}, {"stories": []}), ({}, []))
        self.assertEqual(sr.story_map_from_tree(None, None), ({}, []))

    def test_缺名字时仍按位置映射(self):
        """源节点 name 为空（原生 panel 会自动补「剧情N」）不该挡住映射。"""
        doc = self._doc()
        doc["stories"][0]["name"] = None
        tree = self._tree()
        tree["stories"][0]["name"] = "剧情1"
        m, mism = sr.story_map_from_tree(doc, tree)
        self.assertEqual(m["349"], 352)
        self.assertEqual(mism, [])


class TestDanglingOwners(unittest.TestCase):
    """悬空引用要能被读出"指向谁"，否则计划里的 `6 条悬空` 等于没说。"""

    def _snap(self):
        def comp(g, cid):
            return {"g": g, "i": cid, "p": [0, 0, 0],
                    "ci": json.dumps({"component_id": cid, "events": []})}
        return {
            "components": [comp(349, 10007001), comp(351, 10007003),
                           comp(152, 203003)],
            "story": {"stories": [
                {"ugc_id": 349, "name": "剧情1", "dialogues": [
                    {"ugc_id": 350, "content": "你好呀", "choices": [
                        {"ugc_id": 351, "content": "选项1"}]}]}]},
        }

    def test_names_story_and_choice(self):
        s = sr._dangling_owners(self._snap(), {349, 351})
        self.assertIn("#349 STORY「剧情1」", s)
        self.assertIn("#351 CHOICE「选项1」", s)

    def test_non_story_type_shows_type(self):
        self.assertIn("type=203003", sr._dangling_owners(self._snap(), {152}))

    def test_unknown_id(self):
        self.assertIn("不在快照", sr._dangling_owners(self._snap(), {999}))

    def test_long_list_is_capped_with_a_type_tally(self):
        """交付体检实机印过 233 条逐条列出（`#125 type=203911、#127 …` 刷满一屏）。
        那种输出的下场是整段被跳过，而它要传达的恰恰是"这些全是没摆的运动器、出口只有一个"
        ⇒ 截断可以，**总条数与按类型的总账不许丢**。"""
        def comp(g, cid):
            return {"g": g, "i": cid, "p": [0, 0, 0],
                    "ci": json.dumps({"component_id": cid, "events": []})}
        snap = {"components": [comp(g, 203911) for g in range(125, 155)]
                + [comp(152, 203003)], "story": {}}
        s = sr._dangling_owners(snap, set(range(125, 155)) | {152}, limit=12)
        self.assertIn("共 30 条", s)
        self.assertIn("type 203911×29", s)
        self.assertLess(len(s), 500, msg="截断后必须还能读完")
        short = sr._dangling_owners(snap, {152})
        self.assertNotIn("共 ", short, msg="没超过 limit 不该出现截断话术")

    def test_label_helpers(self):
        snap = self._snap()
        self.assertEqual(sr._story_label(snap, 349), "剧情1")
        self.assertEqual(sr._story_label(snap, 351), "选项1")
        self.assertEqual(sr._story_label(snap, 999), "")


class TestRunId(unittest.TestCase):
    def test_stable_across_calls(self):
        items = [{"a": 1}, {"a": 2}]
        self.assertEqual(sr.run_id_for("s.json", items),
                         sr.run_id_for("s.json", items))

    def test_new_run_differs(self):
        items = [{"a": 1}]
        self.assertNotEqual(sr.run_id_for("s.json", items),
                            sr.run_id_for("s.json", items, new_run=True))


if __name__ == "__main__":
    unittest.main()
