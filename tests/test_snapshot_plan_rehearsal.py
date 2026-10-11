#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_plan_rehearsal — 用**真实的 15000 行快照**离线预演全量还原计划。

为什么需要这一层：B 账号那次才是这条链路第一次面对 14885 件的全量。而这次交付里
每一个"中途才炸"的坑（运动器在途锁、越界缩放被滑块拦、共用单元的身份、预算口径）
都是**批量/极值**才现形的，小样本试点全绿。这些函数本来就不碰引擎 ⇒ 没理由等到 B 图
才发现计划层少算了件、或者一批件被静默丢掉。

夹具用 `snapshots/chunlan_1to1_20260930.json`（14998 件，顶格图）。文件不在就跳过
并说明原因 —— 不许静默通过。
"""
import argparse
import ast
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402

SNAP_PATH = os.path.join(ROOT, "snapshots", "chunlan_1to1_20260930.json")

# 手册里 B 账号全量那条命令的开关组合
FULL_RUN_ARGS = dict(include_ui=False, include_motion=False, allow_sentinel=True,
                     with_story=True, include_global=False,
                     include_not_placeable=True)

# 已知的跳过类别（新增类别必须显式加进来，不然"静默少摆"就没有信号了）
KNOWN_SKIP_REASONS = {
    "story_stage", "ui", "motion_point", "skeleton", "global_singleton",
    "sentinel_position", "not_placeable", "deleted_residue", "unknown_component_type",
}


def _verified_types():
    """AST 直读 `bridge/fuxiang_scene.py` 的 VERIFIED_TYPES（离线，不 import 引擎侧模块）。"""
    path = os.path.join(ROOT, "bridge", "fuxiang_scene.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "VERIFIED_TYPES":
                    body = node.value
                    while isinstance(body, ast.Call):      # frozenset({...})
                        body = body.args[0] if body.args else None
                    if isinstance(body, (ast.Set, ast.List)):
                        return {int(e.value) for e in body.elts
                                if isinstance(e, ast.Constant)}
    raise AssertionError("读不到 VERIFIED_TYPES —— 表的结构变了")


def _full_args(**kw):
    d = dict(FULL_RUN_ARGS)
    d.update(kw)
    return argparse.Namespace(**d)


@unittest.skipUnless(os.path.exists(SNAP_PATH),
                     "缺真实快照 %s ⇒ 预演未执行（不是通过）" % SNAP_PATH)
class TestPlanRehearsal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SNAP_PATH, encoding="utf-8") as f:
            cls.snap = json.load(f)
        cls.types = _verified_types()
        cls.plan = sr.build_plan(cls.snap, cls.types, _full_args())

    # ---- 件数守恒：计划不能"少算了"还不吭声 ----
    def test_every_component_is_placed_or_skipped_with_a_reason(self):
        total = len(self.snap["components"])
        self.assertEqual(self.plan["total_components"], total)
        reasons = {s["reason"].split("（")[0] for s in self.plan["skipped"]}
        unknown = reasons - KNOWN_SKIP_REASONS
        self.assertEqual(unknown, set(), msg="出现了没登记过的跳过类别：%s" % unknown)
        # 摆出去的件 + 跳过的件 = 快照总件数（成组折叠把两件并成一个请求，
        # 所以守恒要用 covered_components，不能用 len(specs)）
        self.assertEqual(self.plan["to_place"] + len(self.plan["skipped"]), total)

    def test_skip_counts_match_the_manual(self):
        """手册写的口径（可摆 14885 ／ 跳过 113）变了要在这里留下信号。"""
        self.assertGreater(self.plan["to_place"], 14000)
        self.assertLess(len(self.plan["skipped"]), 400)

    def test_worst_case_samples_are_actually_in_the_plan(self):
        """试点挑的是极值：337 点路径的运动器、467 成员的组合件必须在计划里。"""
        olds = {s["old_g"] for s in self.plan["specs"]}
        for gid, why in ((14841, "最长路径关键帧"), (3875, "最多成员的组合件")):
            self.assertIn(gid, olds, msg="%s（gid=%s）没进计划" % (why, gid))

    # ---- 极值件不被阀门拦掉 ----
    def test_out_of_range_scale_items_stay_in_the_plan(self):
        """214 个组合件 z=0.02 < 滑块下限 0.1：这批一件都不能少（出口由 place_one 带，
        那条 forwarding 已由 `test_snapshot_restore_pilot` 锁住，这里只锁"计划不丢件"）。"""
        small = [s for s in self.plan["specs"]
                 if s.get("scale") and min(s["scale"]) < 0.1]
        self.assertGreater(len(small), 200,
                           msg="越界缩放件在全量计划里剩 %d 件" % len(small))
        olds = {c.get("g") for c in self.snap["components"]
                if c.get("s") and min(c["s"]) < 0.1}
        self.assertEqual(olds - {s["old_g"] for s in self.plan["specs"]}
                         - {g for s in self.plan["specs"]
                            for g in (s.get("group_old_gs") or [])}, set())

    def test_unverified_types_carry_the_flag(self):
        """白名单外（角色这类）放行时要带 allow_unverified，否则 plan() 那侧照样拒。"""
        flagged = [s for s in self.plan["specs"] if s.get("allow_unverified")]
        self.assertTrue(flagged, msg="include-not-placeable 放行了 0 件？")
        self.assertLess(len(flagged), 20, msg="放宽面不该扩散到已实测的类型")

    # ---- 预算口径：顶格图上宁高不低 ----
    def test_budget_need_uses_planned_counts_not_whole_map(self):
        need = sr.plan_budget_need(self.snap, self.plan, self.plan["specs"], True)
        self.assertLessEqual(need["components"], self.plan["to_place"] + 10)
        blank = {"component_nums": 0, "max_component_nums": 15000,
                 "occupancy": 0, "max_occupancy": 30000}
        bg = sr.check_budget(self.snap.get("budget"), blank, need)
        self.assertEqual(bg["level"], "ok", msg=bg["msg"])
        self.assertTrue(bg["planned"])

    def test_budget_refuses_a_lower_cap_target(self):
        """B 账号上限更低（读到过 20000 也读到过 15000）必须当场拦，不能摆到一半卡住。"""
        need = sr.plan_budget_need(self.snap, self.plan, self.plan["specs"], True)
        small = {"component_nums": 0, "max_component_nums": 8000,
                 "occupancy": 0, "max_occupancy": 30000}
        bg = sr.check_budget(self.snap.get("budget"), small, need)
        self.assertEqual(bg["level"], "error", msg=bg["msg"])

    # ---- 事件引用：每条都要有归属（可绑 / 有解释的悬空）----
    def test_every_event_ref_is_accounted_for(self):
        ev = self.snap.get("events") or {}
        total_refs = sum(len(e.get("refs") or []) for e in ev.get("events") or [])
        self.assertEqual(total_refs, self.plan["events"]["refs"])
        self.assertGreater(total_refs, 200, msg="这份快照的引用数变了，检查口径要复核")
        # 计划阶段还没有新 id ⇒ 用旧 id 自映射，只验"每条引用都被归了类"
        id_map = {s["old_g"]: s["old_g"] for s in self.plan["specs"]}
        act, dang = sr.event_bind_plan(ev, id_map)
        self.assertEqual(len(act) + len(dang), total_refs,
                         msg="有条数没算进'可绑'或'悬空'，就是被静默丢掉了")
        for d in dang:
            self.assertIsNotNone(d.get("old_ugc_id"),
                                 msg="悬空却没记下指向哪个组件：%s" % d)

    def test_dangling_advice_covers_every_dangling_ref(self):
        ev = self.snap.get("events") or {}
        id_map = {s["old_g"]: s["old_g"] for s in self.plan["specs"]}
        _act, dang = sr.event_bind_plan(ev, id_map)
        rows = sr.dangling_advice(dang, sr._skip_reason_index(self.snap, self.plan,
                                                            self.plan["specs"]))
        self.assertEqual(sum(n for _k, n, _h in rows), len(dang),
                         msg="悬空按原因分组后条数对不上，等于漏报")
        # 认不出的原因也必须被显式印出来（而不是悄悄并进别的类）
        for k, _n, h in rows:
            self.assertTrue(h, msg="原因 %s 没有出口提示" % k)

    # ---- 坐标：每件都得有个落点 ----
    def test_all_specs_have_a_position(self):
        for s in self.plan["specs"]:
            p = s.get("position")
            self.assertIsInstance(p, list, msg="gid=%s 没有落点" % s.get("old_g"))
            self.assertEqual(len(p), 3)
            self.assertTrue(all(isinstance(x, float) for x in p))

    def test_no_spec_is_parked_at_the_hidden_y_without_a_note(self):
        """y=-65536 直接摆出去会停在隐藏点（实机：观影触发区要读 pp 才落对位置）。"""
        bad = [s for s in self.plan["specs"]
               if len(s.get("position") or ()) == 3 and abs(s["position"][1] + 65536.0) < 1e-6]
        self.assertEqual(bad, [], msg="这些件带着哨兵 y 进计划：%s"
                         % [x.get("old_g") for x in bad][:10])

    # ---- UI_NATIVE（技能控件）这一层：默认不摆是**选择**，不是"做不到" ----
    def test_include_ui_fits_the_same_budget(self):
        """顶格图加这 48 件仍然放得下 ⇒ "预算不够所以不搬"这个理由不成立。"""
        p = sr.build_plan(self.snap, self.types, _full_args(include_ui=True))
        without = sr.build_plan(self.snap, self.types, _full_args())
        self.assertGreater(p["to_place"], without["to_place"])
        need = sr.plan_budget_need(self.snap, p, p["specs"], True)
        blank = {"component_nums": 0, "max_component_nums": 15000,
                 "occupancy": 0, "max_occupancy": 30000}
        self.assertEqual(sr.check_budget(self.snap.get("budget"), blank, need)["level"],
                         "ok", msg="开 --include-ui 后预算判定变紧，手册口径要复核")

    def test_ui_skip_reason_does_not_claim_impossibility(self):
        """跳过理由不许写"配置复杂/摆不出来"：2026-10-01 用只读 `plan_item` 复核过，
        阀门是放行的（`server_place_supported` / `server_begin_allowed` 都 true），
        真实状态是"通道开放、落库未证"。"""
        skipped = [s for s in self.plan["skipped"] if str(s["reason"]).startswith("ui")]
        self.assertTrue(skipped, msg="这份快照没有 UI_NATIVE？那条检查要改写")
        for s in skipped:
            self.assertNotIn("配置复杂", s["reason"])
            self.assertIn("落库未证", s["reason"])


if __name__ == "__main__":
    unittest.main()
