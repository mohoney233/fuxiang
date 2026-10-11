#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_plan_integrity — 把"这份交付件的全量计划不会静默接错线"钉成回归。

2026-10-01 修完袋引用闸门之后，剩下的问题是**这个结论本身不能被机器复查**：
"春岚默认全量计划悬空 0 处"当时只是我手工跑出来的一句话，谁改了白名单、
改了 `classify_components` 的跳过口径、或换了一张图，它就会悄悄不成立 ——
而不成立的表现是"旧号原样写进目标图"，也就是最坏的那一类：本地回读全过，
重进之后才发现接错线。

这里锁三件事，全部**离线**（不连游戏，跑在交付件本身 ``snapshots/chunlan_1to1_20260930.json`` 上）：

1. 全量计划下，属性袋引用与事件引用都**没有一处**指向本次不重建的组件；
2. 这条判据不是永真：`--only-types 203900` 这种试点筛法会造出成千上万处悬空
   （所以闸门与 `--allow-dangling-refs` 不是摆设）；
3. **换号白名单不许"顺手扩"**：拿真实数据算一遍，把某个非白名单字段一起换号会
   改掉什么 —— 春岚袋里有 28 个非白名单字段的整数恰好等于某个组件 gid（模型号/枚举/
   路径坐标/阵营序号），跟着换号就是把好数据改成另一个组件的号。
"""
import argparse
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn            # noqa: E402
import snapshot_restore as sr    # noqa: E402

DELIVERABLE = os.path.join(ROOT, "snapshots", "chunlan_1to1_20260930.json")


def _ns(**kw):
    ns = argparse.Namespace(include_ui=False, include_motion=False, allow_sentinel=True,
                            with_story=True, include_global=False,
                            include_not_placeable=False, only_types=None, only_ids=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@unittest.skipUnless(os.path.exists(DELIVERABLE),
                     "缺交付快照 %s ⇒ 计划完整性未执行（不是通过）" % DELIVERABLE)
class TestPlanIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with io.open(DELIVERABLE, encoding="utf-8") as f:
            cls.snap = json.load(f)
        cls.types = {json.loads(c["ci"]).get("component_id")
                     for c in cls.snap["components"]}

    def _olds(self, **flags):
        plan = sr.build_plan(self.snap, self.types, _ns(**flags))
        olds = set()
        for s in plan["specs"]:
            olds.add(s["old_g"])
            olds.update(s.get("group_old_gs") or [])
        return olds

    def test_full_plan_leaves_no_dangling_bag_refs(self):
        olds = self._olds()
        d = sr.bag_ref_dangling(self.snap, [{"old_g": g} for g in olds])
        self.assertEqual(d, [], msg="全量计划里 %d 处袋引用指向本次不重建的组件：%s"
                                  % (len(d), d[:3]))

    def test_full_plan_leaves_no_dangling_event_refs(self):
        olds = self._olds()
        _acts, dang = sr.event_bind_plan(self.snap.get("events") or {},
                                         {g: g for g in olds},
                                         comp_index=sr._info_index(self.snap))
        self.assertEqual(dang, [], msg="%d 处事件引用绑不上：%s" % (len(dang), dang[:3]))

    def test_pilot_subset_does_dangle_so_the_gate_is_not_decoration(self):
        """反向：只摆组合件时必须有大量悬空 —— 否则上面两条是永真断言。"""
        olds = self._olds(only_types="203900", only_ids=None)
        subset = {"specs": [{"old_g": g} for g in olds]}
        d = sr.bag_ref_dangling(self.snap, subset["specs"])
        self.assertGreater(len(d), 1000,
                           msg="试点筛法只造出 %d 处悬空 ⇒ 判据可能已经失效（%s）"
                               % (len(d), olds and sorted(olds)[:3]))
        # 而且报出来的都是"成员表/组合件字段"这一类，不是随便什么数
        self.assertTrue(all(x["property_id"].endswith(("18701", "03001")) for x in d))

    def test_bag_refs_flagged_owner_is_the_placed_side(self):
        """被拦的是**要写下去的那件**引用了没摆的件；反过来（没摆的件）不该出现在报告里。"""
        olds = self._olds(only_types="203900")
        d = sr.bag_ref_dangling(self.snap, [{"old_g": g} for g in olds])
        owners = {x["owner"] for x in d}
        self.assertTrue(owners <= olds, msg="报告里混进了本次不摆的件")


@unittest.skipUnless(os.path.exists(DELIVERABLE), "缺交付快照 ⇒ 未执行")
class TestWhitelistIsLoadBearing(unittest.TestCase):
    """非白名单字段里那些"等于某个组件 gid"的整数是**撞号**，不能跟着换号。"""

    @classmethod
    def setUpClass(cls):
        with io.open(DELIVERABLE, encoding="utf-8") as f:
            cls.snap = json.load(f)
        gids = {int(c["g"]) for c in cls.snap["components"]}
        cls.collisions = []
        for row in cls.snap["components"]:
            ci = json.loads(row["ci"])
            for e in ci.get("events") or []:
                pid = str(e.get("type"))
                if pid.endswith(sn.GROUP_REF_FIELDS):
                    continue
                for v in sn._iter_numbers(e.get("info")):
                    if v in gids and v != row["g"]:
                        cls.collisions.append((int(row["g"]), pid, v))
                        break

    def test_real_map_has_such_collisions(self):
        self.assertGreater(len(self.collisions), 100,
                           msg="春岚里没找到撞号样本 ⇒ 这条回归失去对象（%d）"
                               % len(self.collisions))

    def test_widening_the_whitelist_would_rewrite_real_data(self):
        """把某个非白名单字段也拿去换号 ⇒ 它会被改成**另一个组件**的新号。

        这就是为什么 `accept` 的归一与还原的换号都只在 `ID_REF_PROPERTIES` 上做：
        跟着换号不是"更保险"，而是把好数据改掉。
        """
        owner, pid, ref = self.collisions[0]
        fake_lut = {str(ref): 999001}          # 假设这个组件在目标图上换成了 999001
        row = next(c for c in self.snap["components"] if c["g"] == owner)
        ci = json.loads(row["ci"])
        info = next(e["info"] for e in ci["events"] if str(e["type"]) == pid)
        before = json.dumps(info, sort_keys=True)
        after = sn._remap_ids(info, fake_lut)
        self.assertNotEqual(json.dumps(after, sort_keys=True, default=str), before,
                         msg="%s 的值恰好不含组件号 ⇒ 这条测试要换样本，别删" % pid)

    def test_the_actual_whitelist_is_the_bridge_list(self):
        """比对/还原两处用的表要一字不差：桥接 `_remap_info` 的表是权威。

        两张表都要查：物件/成员引用（UGCID_REF）与**剧情节点引用**（STORY_REF）。
        后者是 2026-10-01 实机补上的一族——春岚搬到新图后对话件的「所属剧情」写着 117，
        而 117 在那张图上是一件时钟件（真正的剧情件是 133）：还原不换号、比对不归一，
        于是"接错线"这件事在两层报告里都看不见。
        """
        import ast
        path = os.path.join(ROOT, "bridge", "fuxiang_edit_ops.py")
        with io.open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        want = {"UGCID_REF_PROPERTIES": None, "STORY_REF_PROPERTIES": None}
        for node in tree.body:
            if isinstance(node, ast.Assign) and \
                    getattr(node.targets[0], "id", "") in want:
                want[node.targets[0].id] = tuple(ast.literal_eval(node.value))
        bridge = want["UGCID_REF_PROPERTIES"]
        story = want["STORY_REF_PROPERTIES"]
        self.assertIsNotNone(bridge, "桥接的 UGCID_REF_PROPERTIES 找不到了")
        self.assertIsNotNone(story, "桥接的 STORY_REF_PROPERTIES 找不到了")
        self.assertEqual(set(bridge), set(sn.ID_REF_PROPERTIES),
                         msg="两张表漂了：还原按桥接换号、比对按这张表归一，"
                             "不一致就会一边写了另一边说不一致")
        self.assertEqual(set(story), set(sn.STORY_REF_PROPERTIES),
                         msg="剧情节点引用这一族两边漂了：写的时候不换号、比的时候当值差异")
        self.assertEqual(set(sn.ALL_REF_PROPERTIES), set(bridge) | set(story),
                         msg="并集没跟上：还原传的 remap_properties 与比对的归一集合必须同源")


class TestStockDeductionSparesTheAuthoredBulk(unittest.TestCase):
    """扣掉"目标图已经有"的那批出厂件，不许顺手把作者的内容也扣掉。

    新判据拿「属性袋 + 位置」的内容签名对目标图现状做**多重集**相减。而春岚有
    14318 件几何**共用同一个属性袋**（靠坐标区分），另有几十件逻辑件袋是空的
    ⇒ 签名一旦漏掉坐标、或相减写成集合，作者没摆完的件就会被判成"已经有了"而静默不摆 ——
    那比多摆一件难查得多（事后 diff 只会说"少 N 件"，看不出是工具扣的）。
    这三条跑在**真实交付件 + 真实空白图基线**（`tests/fixtures/blank_map_baseline_20261001.json`，
    同账号新开的图，113 件全是出厂件）上，不靠合成数据。
    """

    FIXTURE = os.path.join(ROOT, "tests", "fixtures",
                           "blank_map_baseline_20261001.json")
    AUTHORED = {100001, 203005, 203022, 203900, 203911, 203920, 204041, 301001}

    def setUp(self):
        self._real = sr.fetch_target_type_rows
        with open(self.FIXTURE, encoding="utf-8") as f:
            blank = json.load(f)
        self.rows = [{"g": c["g"], "i": sr._cid_int(c), "ci": c["ci"],
                      "p": c.get("p"), "r": c.get("r"), "s": c.get("s")}
                     for c in blank["components"]]
        self.td = blank["map"]["type_distribution"]
        self.blank_n = len(self.rows)
        sr.fetch_target_type_rows = lambda types, timeout=120: [
            r for r in self.rows if r["i"] in set(types)]

    def tearDown(self):
        sr.fetch_target_type_rows = self._real

    def _stock(self):
        with open(DELIVERABLE, encoding="utf-8") as f:
            snap = json.load(f)
        stock, excl = sr.stock_for_stages(snap, None, None,
                                          {"type_distribution": self.td})
        return snap, stock, excl

    def test_blank_map_leaves_no_ui_gap(self):
        """今天这条结论的最强形式：真实交付件 + 真实新图，控件层净缺 0。"""
        _snap, stock, _excl = self._stock()
        self.assertEqual(stock["ui"]["on_target"], 48, msg=stock)
        self.assertEqual(stock["ui"]["net"], 0, msg="新图自带同一批控件 ⇒ 不该催 --include-ui")

    def test_only_stock_types_get_deducted(self):
        snap, _stock, excl = self._stock()
        cids = set(cid for cid, _tg in excl.values())
        self.assertTrue(cids, msg="空白图有出厂件，该扣的必须扣下来")
        self.assertFalse(cids & self.AUTHORED,
                         msg="作者内容被当成「已有」扣掉了：%s" % sorted(cids & self.AUTHORED))
        self.assertLessEqual(len(excl), self.blank_n,
                             msg="扣掉的件数不可能超过目标图现有件数")
        rows_by_g = {c.get("g"): c for c in (snap.get("components") or [])}
        target_gs = set(r["g"] for r in self.rows)
        for old, (cid, tg) in excl.items():
            self.assertEqual(sr._cid_int(rows_by_g[old]), cid,
                             msg="扣件记录里的类型要与快照一致，否则报表会指错件")
            self.assertIn(tg, target_gs, msg="要接进 id 映射的是目标图上真实存在的号")

    def test_geometry_with_a_shared_bag_is_never_deducted(self):
        """14318 件几何共用同一个属性袋：签名漏掉坐标就会整批被判成重复。"""
        _snap, _stock, excl = self._stock()
        geo = [g for g, (cid, _tg) in excl.items() if cid in (100001, 301001)]
        self.assertEqual(geo, [], msg="几何件一件都不许被扣")


if __name__ == "__main__":
    unittest.main()
