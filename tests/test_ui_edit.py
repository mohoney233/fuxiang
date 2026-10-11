# -*- coding: utf-8 -*-
"""界面编辑器（UI 编辑）层的离线回归 —— 不接触游戏进程。

锁住的是**会静默写错东西**的那部分知识：
  * PROP_ALIAS 的别名 → property_id → 应用方法三者自洽（客户端改版后 _alias_map
    会在运行时报错，这里先把静态一致性锁住）；
  * 控件坐标是画布 2D，护栏必须拒掉场景习惯的 [x,y,z] 与非数值；
  * 摆放状态机只在「组件类型 200000 且界面编辑器在前视图」时才分流到
    on_check_ui_component_limit（视图关着时原生命令返回空，走通用登记路径）；
  * 文档里写的 op 数量等于 OPS 里真实的 ui_* 数量（同 test_doc_counts 的动机）。
"""
import ast
import math
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import fuxiang_ui_edit as UE        # noqa: E402
import fuxiang_placement as UP      # noqa: E402


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _aliases_from(table):
    """绕过游戏上下文跑 _alias_map：给它一份假的运行时属性表。"""
    real = UE._prop_table
    UE._prop_table = lambda edit, comp=None: table
    try:
        return UE._alias_map(None)
    finally:
        UE._prop_table = real


def _table(setters=None, drop=()):
    out = {}
    for alias, (pid, setter) in UE.PROP_ALIAS.items():
        if str(pid) in drop:
            continue
        out[str(pid)] = {"property_id": pid, "event_type": None, "event_name": None,
                         "setter": (setters or {}).get(alias, setter),
                         "property_type": "80001" if alias in UE.EVENT_SLOTS else "20004",
                         "property_params": None, "tab_id": None,
                         "label": None, "direction": "sender", "key_index": 0,
                         "default_info": None}
    return out


class TestAliasTable(unittest.TestCase):
    def test_alias_targets_unique_and_complete(self):
        pids = [pid for pid, _ in UE.PROP_ALIAS.values()]
        self.assertEqual(len(pids), len(set(pids)), "两个别名指向同一个 property_id")
        for alias, (_, setter) in UE.PROP_ALIAS.items():
            self.assertTrue(setter.startswith("_"), "%s 的应用方法名不像方法：%s" % (alias, setter))

    def test_event_slots_are_declared_once(self):
        self.assertTrue(set(UE.EVENT_SLOTS) <= set(UE.PROP_ALIAS))
        # 事件类槽位（面板文案「点击/松手/长按/使控件显示/使控件隐藏」）不能漏
        for slot in ("on_click", "on_release", "on_long_press", "on_show", "on_hide"):
            self.assertIn(slot, UE.EVENT_SLOTS)

    def test_alias_map_validated_against_runtime_table(self):
        aliases = _aliases_from(_table())
        self.assertEqual(set(aliases), set(UE.PROP_ALIAS))
        self.assertEqual(int(aliases["on_click"]["property_id"]), 20000016605)
        self.assertEqual(aliases["on_click"]["setter"], "_down_send")

    def test_alias_map_rejects_drifted_setter(self):
        with self.assertRaises(ValueError) as ctx:
            _aliases_from(_table(setters={"on_click": "_set_something_else"}))
        self.assertIn("语义变了", str(ctx.exception))

    def test_alias_map_rejects_missing_property(self):
        with self.assertRaises(ValueError) as ctx:
            _aliases_from(_table(drop=[str(UE.PROP_ALIAS["pos"][0])]))
        self.assertIn("不在 UI_CUSTOM 属性表里", str(ctx.exception))

    def test_resolve_prop_accepts_alias_and_both_id_shapes(self):
        aliases = _aliases_from(_table())
        self.assertEqual(UE._resolve_prop(aliases, "pos"), 20000010001)
        self.assertEqual(UE._resolve_prop(aliases, 20000016605), 20000016605)
        self.assertEqual(UE._resolve_prop(aliases, "20000016605"), 20000016605)
        with self.assertRaises(ValueError):
            UE._resolve_prop(aliases, "not_a_field")
        with self.assertRaises(ValueError):
            UE._resolve_prop(aliases, None)

    def test_by_pid_index_round_trips(self):
        aliases = _aliases_from(_table())
        by_pid = UE._by_pid(aliases)
        self.assertEqual(len(by_pid), len(aliases))
        self.assertEqual(by_pid["20000016605"]["alias"], "on_click")


class TestValueGuards(unittest.TestCase):
    def test_pos2_is_canvas_2d_only(self):
        self.assertEqual(UE._pos2([700, 300]), [700.0, 300.0])
        self.assertEqual(UE._pos2((1.5, -2.5)), [1.5, -2.5])
        for bad in ([0, 0, 0], [1], 5, "700,300", None, [1, "x"], [float("nan"), 1]):
            with self.assertRaises(ValueError, msg="应拒收 %r" % (bad,)):
                UE._pos2(bad)

    def test_num_rejects_bool_and_nonfinite(self):
        self.assertEqual(UE._num(12, "width"), 12.0)
        for bad in (True, False, "12", None, float("inf"), float("nan")):
            with self.assertRaises(ValueError, msg="应拒收 %r" % (bad,)):
                UE._num(bad, "width")

    def test_center_pos_inside_canvas(self):
        size = UE.CANVAS_FALLBACK_SIZE
        self.assertTrue(all(0.0 < r < 1.0 for r in UE.DEFAULT_POS_RATIO))
        self.assertTrue(0 < size[0] * UE.DEFAULT_POS_RATIO[0] < size[0])
        self.assertTrue(math.isfinite(size[0]) and math.isfinite(size[1]))


class TestEnumeration(unittest.TestCase):
    class _FakeCM:
        def __init__(self, ids):
            self._component_type2ugc_ids = ids

    def test_type_ids_normalizes_and_sorts(self):
        cm = self._FakeCM({UE.UI_CUSTOM_TYPE: {"3", 1, 2, "x", None}})
        self.assertEqual(UE._type_ids(cm, UE.UI_CUSTOM_TYPE), [1, 2, 3])

    def test_type_ids_accepts_string_key(self):
        cm = self._FakeCM({str(UE.UI_CUSTOM_TYPE): [7, 6]})
        self.assertEqual(UE._type_ids(cm, UE.UI_CUSTOM_TYPE), [6, 7])

    def test_type_ids_missing_type_is_empty(self):
        self.assertEqual(UE._type_ids(self._FakeCM({}), UE.UI_CUSTOM_TYPE), [])

    def test_plain_flattens_mapping_like_and_caps_depth(self):
        class Tagged(object):
            def keys(self):
                return ["a"]

            def __getitem__(self, k):
                return [1, 2]
        self.assertEqual(UE._plain(Tagged()), {"a": [1, 2]})
        deep = []
        node = deep
        for _ in range(8):
            child = []
            node.append(child)
            node = child
        self.assertIn("<deep>", repr(UE._plain(deep)))

    def test_bag_reads_events_by_property_id(self):
        class Comp(object):
            component_info = {"component_id": UE.UI_CUSTOM_TYPE, "events": [
                {"type": 20000010001, "info": [1.0, 2.0]},
                {"type": 20000016605, "info": ["按钮触发1", 1286]},
                {"type": 20000016502, "info": None},
                "junk",
            ]}
        bag = UE._bag(Comp())
        self.assertEqual(bag["20000010001"], [1.0, 2.0])
        self.assertEqual(bag["20000016605"], ["按钮触发1", 1286])
        self.assertIsNone(bag["20000016502"])

    def test_ui_parts_table_covers_the_19_sub_parts(self):
        self.assertEqual(len(UE.UI_PARTS_CN), 19)
        for name in UE.UI_PARTS_CN:
            self.assertTrue(name.startswith("UIBattleUGCCustomUI"), name)
            self.assertTrue(UE.UI_PARTS_CN[name], "%s 没有中文说明" % name)


class TestPlacementUiBranch(unittest.TestCase):
    """摆放状态机里的界面控件分流：判据、坐标回读、以及「两条分支都要有」。"""

    def test_is_ui_widget_needs_matching_spec_and_component(self):
        class Real(object):
            COMPONENT_TYPE = UE.UI_CUSTOM_TYPE

        class Other(object):
            COMPONENT_TYPE = UE.UI_NATIVE_TYPE

        self.assertTrue(UP._is_ui_widget({"spec": {"component_type": 200000}}, Real()))
        self.assertFalse(UP._is_ui_widget({"spec": {"component_type": 200000}}, Other()))
        self.assertFalse(UP._is_ui_widget({"spec": {"component_type": 100000}}, Real()))
        self.assertTrue(UP._is_ui_widget({"spec": {"component_type": 200000}}))

    def test_widget_pos_reads_the_pos_slot(self):
        comp = type("C", (), {"component_info": {"events": [
            {"type": 20000016501, "info": ["按钮"]},
            {"type": "20000010001", "info": [451.6, 625.2]},
        ]}})()
        self.assertEqual(UP._ui_widget_pos(comp), [451.6, 625.2])

    def test_widget_pos_none_when_slot_missing_or_malformed(self):
        for events in ([], [{"type": 20000010001, "info": None}],
                       [{"type": 20000010001, "info": [1]}]):
            comp = type("C", (), {"component_info": {"events": events}})()
            self.assertIsNone(UP._ui_widget_pos(comp))

    def test_poll_has_one_dedicated_ui_route(self):
        """界面控件只能走"请求后新出现的 200000 组件"这条专路（2026-09-23 实机）。

        全图控件共用一个 UgcGlobalUnit，所以 poll 里另外两种口径（新实体 /
        owner.ugc_type==item_id）都认不到第 2..N 件；专路必须在 candidates 之前，
        否则会先被当成"没有新物件"而一直 pending。

        2026-10-02 批量摆放重构后该机制有**两个入口**（同一认领机制，不是两套）：
        单件路径在 `_poll_pending`（带 `assigned is None` 守卫——批量池化认领时
        控件由 poll_batch 直接分流，不经这里，防重复认领）；批量路径在
        `poll_batch` 的 `kind == 'widget'` 分支。两处都必须最终落到
        `_confirm_ui_component`（原生接口收列表，多出的件一起处理）。
        """
        src = _read(os.path.join(ROOT, "bridge", "fuxiang_placement.py"))
        route = "if assigned is None and _is_ui_widget(req) and _ui_view_open(ui):"
        self.assertEqual(src.count(route), 1,
                         "单件路径的界面控件分流应当只有一处专路")
        self.assertIn("_confirm_ui_component(scene, req, state, ui, ui_new)", src,
                      "专路要按列表确认（原生接口收列表，多出的件一起处理）")
        pending = src[src.index("def _poll_pending("):]
        self.assertLess(pending.index(route),
                        pending.index("candidates = []"),
                        "UI 专路要在 candidates/_new_global_components 之前，否则永远 pending")
        self.assertGreaterEqual(src.count("and _ui_view_open(ui)"), 1,
                                "分流要要求界面编辑器在前视图（视图关着时原生确认返回空命令）")
        batch = src[src.index("def poll_batch(scene, edit, args):"):]
        self.assertIn("kind == 'widget'", batch,
                      "批量路径要有控件分流（池化认领的 widget 类）")
        self.assertIn("_confirm_ui_component(", batch,
                      "批量路径的控件认领也必须落到同一个确认函数，不得另造一套")

    def test_ui_confirm_builds_native_update_params(self):
        src = _read(os.path.join(ROOT, "bridge", "fuxiang_placement.py"))
        for key in ("refresh_occupancy", "refresh_component_nums", "refresh_ui_custom",
                    "select_ugc_id"):
            self.assertIn(key, src, "update_params 少键 %s" % key)
        self.assertIn("on_check_ui_component_limit", src)


class TestOpRegistration(unittest.TestCase):
    """OPS 注册与 UI_EDIT.md 里的数量必须一致（同 test_doc_counts 的动机）。"""

    def _ops_keys(self):
        src = _read(os.path.join(ROOT, "bridge", "fuxiang_edit.py"))
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "OPS":
                        return [k.value for k in node.value.keys]
        self.fail("fuxiang_edit.py 里找不到 OPS 字典")

    def test_ui_ops_registered(self):
        keys = [k for k in self._ops_keys() if k.startswith("ui_")]
        self.assertEqual(len(keys), len(set(keys)), "OPS 里 ui_ 键重复")
        for needed in ("ui_state", "ui_list", "ui_add", "ui_add_poll", "ui_set",
                       "ui_bind", "ui_unbind", "ui_delete", "ui_verify"):
            self.assertIn(needed, keys)

    def test_doc_matches_registered_count(self):
        keys = [k for k in self._ops_keys() if k.startswith("ui_")]
        doc = _read(os.path.join(ROOT, "UI_EDIT.md"))
        got = [int(m.group(1)) for m in re.finditer(r"(\d+) 个 op", doc)]
        self.assertTrue(got, "UI_EDIT.md 里没有「N 个 op」的数量口径")
        for v in got:
            self.assertEqual(v, len(keys),
                             "UI_EDIT.md 写 %d 个 op，OPS 实际 %d 个" % (v, len(keys)))

    def test_cli_exposes_every_ui_op(self):
        src = _read(os.path.join(ROOT, "cli", "fxui.py"))
        for op in [k for k in self._ops_keys() if k.startswith("ui_")]:
            if op in ("ui_add_poll",):        # add 内部自动轮询，不单独出命令
                continue
            self.assertIn('"%s"' % op, src, "cli/fxui.py 没有暴露 op %s" % op)


class TestBindDryRunIsReallyReadOnly(unittest.TestCase):
    """`event_bind`/`ui_bind` 的 dry_run 必须**一个字节都不写**。

    2026-09-23 实机事故：`op_ui_bind --dry-run` 把事件真写进了玩家手放按钮的松手槽
    —— 因为 `op_event_bind` 收下 dry_run 却从不早退，下游照写、回读还"通过"。
    这类"预演即真写"是静默改数据，比报错危险，故用假对象把行为钉住。
    """

    def setUp(self):
        import fuxiang_edit_ops as ops
        self.ops = ops
        self.saved = {n: getattr(ops, n) for n in ("_cm", "_ui", "_room", "event_field_shape")}

        class Comp(object):
            COMPONENT_TYPE = UE.UI_CUSTOM_TYPE

            def __init__(self):
                self._ugc_id = 362
                self._owner_uid = 1000313
                self._owner = None          # setUp 里接到假 Unit（_unit_of 读这个）
                self.writes = []
                self.component_info = {"component_id": UE.UI_CUSTOM_TYPE, "events": [
                    {"type": 20000010001, "info": [451.6, 625.2]},
                    {"type": 20000016605, "info": ["按钮触发1", 1286]},
                    {"type": 20000016606, "info": None, "disable_delete": True},
                ]}

            def update_component_info(self, events):
                self.writes.append([dict(e) for e in events])
                self.component_info["events"] = events

        class Unit(object):
            uid = 1000313

        class CM(object):
            def __init__(self, comp):
                self._ugc_id2component = {362: comp}
                self._ugc_params_events_key = {"event": {"按钮触发1": {"sort_id": 7, "info": []}}}
                self.written_refs = None

            def modify_ugc_params_events_key(self, child, key, refs):
                self.written_refs = refs
                self._ugc_params_events_key[child][key]["info"] = refs

        self.comp, self.unit = Comp(), Unit()
        self.comp._owner = self.unit
        self.cm = CM(self.comp)
        self.ui = type("UI", (), {"ugc_params": {"change_units": {}},
                                  "all_ugc_uid_set": {self.unit.uid}})()
        self.edited = []
        # reg_change(ui, unit, comp) 是普通函数挂到假类上 ⇒ 首参会吃掉假 UI 实例，取末两位
        edit = type("Edit", (), {"reg_change": lambda *a: self.edited.append(a[-2:])})()
        self.edit = edit
        ops._cm = lambda e: self.cm
        ops._ui = lambda e: self.ui
        ops._room = lambda e: self.cm
        ops.event_field_shape = lambda comp, pid: {"key_index": 0,
                                                   "default_info": ["", 1286]}

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(self.ops, name, fn)

    def _bind(self, **args):
        return self.ops.op_event_bind(self.edit, dict({"ugc_id": 362,
                                                       "event_key": "按钮触发1",
                                                       "property_id": 20000016606}, **args))

    def test_dry_run_writes_nothing(self):
        res = self._bind(dry_run=True)
        self.assertTrue(res.get("ok") and res.get("dry_run"))
        self.assertEqual(self.comp.writes, [], "dry_run 仍调了 update_component_info")
        self.assertEqual(self.cm.written_refs, None, "dry_run 仍改了事件注册表")
        self.assertEqual(self.edited, [], "dry_run 仍登记了保存变更集")
        self.assertEqual([e["info"] for e in self.comp.component_info["events"]][2], None)
        self.assertEqual(res["would_write"], ["按钮触发1", 1286])

    def test_real_bind_writes_slot_shape_and_registry(self):
        res = self._bind()
        self.assertTrue(res.get("ok"), res)
        slot = self.comp.component_info["events"][2]
        # 字段形态 ['', 1286] 先落地，再把事件名写进 key_index 指的那个位置
        self.assertEqual(slot["info"], ["按钮触发1", 1286])
        self.assertEqual(self.cm.written_refs, [(362, 5, 0)])
        self.assertIn((self.unit, self.comp), self.edited)

    def test_dry_run_plan_matches_real_write(self):
        plan = self._bind(dry_run=True)
        self._bind()
        self.assertEqual(plan["would_write"], self.comp.component_info["events"][2]["info"],
                         "预演和实际写入不一致 —— 预演就没有参考价值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
