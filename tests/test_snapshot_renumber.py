#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_renumber — 「落盘 ugc_id 只在本会话有效」这件事在比对层怎么收口。

2026-10-01 实机：春岚复原完再跑 `accept --live`，仍报「属性或变换变更 4」，其中 3 条
只是组合件成员表换了个号（[[14982]]→[[14998]]），内容一件没动。看图的人分不清
"重编号"和"真差异"，就会把工具报的坏消息当成图还没修好（或反过来，把真差异
混在一堆噪声里放过）。本文件锁两件事：

  1) **换号后逐字段相等** ⇒ 记进 `renumbered`，不算差异、不影响 `identical`；
  2) 归一只在 `ID_REF_PROPERTIES` 这几张成员表字段上做，且换号后仍不等 ⇒ 照旧报差异
     （错接、少一项、顺序变了都不许被洗白）。
"""
import copy
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "chunlan_cross_session_case.json")


def comp(g, i, x, z, events):
    ci = {"component_id": i, "events": [{"type": t, "info": v}
                                        for t, v in events.items()]}
    return {"g": g, "i": i, "p": [x, 0.0, z], "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps(ci, ensure_ascii=False)}


def snap(comps, props=None):
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": {"name": "m", "framework_id": "4002",
                    "map_setting": {"14": "4002", "1": "0"},
                    # 地图属性层（默认背景音乐等）：新导出必带；不带就是"没采到"
                    "props": props if props is not None else _props_layer(),
                    "anchor_info": {"gamearea0": [[1, [0, 0, 0]], [2, [100, 0, 100]]]}},
            "components": comps,
            "events": {"count": 0, "events": [], "saved_keys": []},
            "story": {"stories": []},
            "budget": {"component_nums": len(comps), "registered": len(comps),
                       "max_component_nums": 15000, "occupancy": len(comps)}}


def _props_layer(**over):
    """一张"什么都没设"的地图属性层 —— 采到了但值为 null，和压根没采是两回事。"""
    keys = list(sn.PROPS_KEYS)
    out = {k: {"present": False, "value": None} for k in keys}
    for k, v in over.items():
        out[k] = {"present": True, "value": v}
    return out



# 交付侧（旧会话）与实机侧（新会话）：几何件 g 1/2 → 7/8，组合件 g 3 → 9
A = snap([comp(1, 100001, 1.0, 2.0, {}),
          comp(2, 100001, 3.0, 4.0, {}),
          comp(3, 203900, 5.0, 6.0, {"20390018701": [[1]],
                                     "20390003001": [[1, 2]]})])
B = snap([comp(7, 100001, 1.0, 2.0, {}),
          comp(8, 100001, 3.0, 4.0, {}),
          comp(9, 203900, 5.0, 6.0, {"20390018701": [[7]],
                                     "20390003001": [[7, 8]]})])


class TestRenumberClassification(unittest.TestCase):
    def test_member_list_renumber_is_not_reported_as_change(self):
        d = sn.diff_snapshots(A, B)
        self.assertEqual(d["components"]["changed"], [])
        self.assertEqual(len(d["components"]["renumbered"]), 1)
        self.assertEqual(d["components"]["renumbered"][0]["ugc_id"], [3, 9])
        self.assertEqual(sorted(d["components"]["renumbered"][0]["properties"]),
                         ["20390003001", "20390018701"])

    def test_renumber_only_is_still_identical(self):
        self.assertTrue(sn.diff_snapshots(A, B)["identical"])

    def test_missing_member_after_remap_is_a_real_change(self):
        """换号后**少了一项**：是真损伤，不许被归一洗掉。"""
        b = copy.deepcopy(B)
        for c in b["components"]:
            if c["g"] == 9:
                info = json.loads(c["ci"])
                info["events"] = [e for e in info["events"]
                                  if str(e["type"]) != "20390018701"]
                info["events"].append({"type": "20390018701", "info": [[]]})
                c["ci"] = json.dumps(info, ensure_ascii=False)
        d = sn.diff_snapshots(A, b)
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertEqual(d["components"]["renumbered"], [])
        self.assertFalse(d["identical"])

    def test_wrong_target_after_remap_is_a_real_change(self):
        """成员表指到了**别人的**新 id（错接）⇒ 换号后不等，照旧报差异。"""
        b = copy.deepcopy(B)
        for c in b["components"]:
            if c["g"] == 9:
                info = json.loads(c["ci"])
                for e in info["events"]:
                    if str(e["type"]) == "20390018701":
                        e["info"] = [[8]]
                c["ci"] = json.dumps(info, ensure_ascii=False)
        d = sn.diff_snapshots(A, b)
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertEqual(d["components"]["renumbered"], [])

    def test_reorder_of_member_list_is_a_real_change(self):
        """顺序也是数据（还原时按袋里的顺序整段写回）⇒ 换序不许算成重编号。"""
        b = copy.deepcopy(B)
        for c in b["components"]:
            if c["g"] == 9:
                info = json.loads(c["ci"])
                for e in info["events"]:
                    if str(e["type"]) == "20390003001":
                        e["info"] = [[8, 7]]
                c["ci"] = json.dumps(info, ensure_ascii=False)
        d = sn.diff_snapshots(A, b)
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertEqual(d["components"]["renumbered"], [])

    def test_non_whitelist_field_is_never_rewritten(self):
        """白名单之外的整数（模型号/材质号）即使与某个 gid 撞号也不参与换号，
        否则真差异会被洗成"一致"。"""
        a = snap([comp(1, 100001, 1.0, 2.0, {}),
                  comp(3, 203911, 5.0, 6.0, {"20391102002": [1]})])
        b = snap([comp(7, 100001, 1.0, 2.0, {}),
                  comp(9, 203911, 5.0, 6.0, {"20391102002": [7]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertEqual(d["components"]["renumbered"], [])

    def test_str_form_ids_keep_their_form(self):
        """袋里存 "1" 这种字符串形态时，换号后仍是字符串形态，别造出假差异。"""
        a = snap([comp(1, 100001, 1.0, 2.0, {}),
                  comp(3, 203900, 5.0, 6.0, {"20390018701": [["1"]]})])
        b = snap([comp(7, 100001, 1.0, 2.0, {}),
                  comp(9, 203900, 5.0, 6.0, {"20390018701": [["7"]]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"], [])
        self.assertEqual(len(d["components"]["renumbered"]), 1)

    def test_unpaired_id_inside_member_list_keeps_its_value(self):
        """配对不到的 id 保持原值：两边都保持 ⇒ 仍算重编号，不能凭空报差异。
        （真的错接由 test_wrong_target_after_remap 那条管。）
        """
        a = snap([comp(1, 100001, 1.0, 2.0, {}),
                  comp(3, 203900, 5.0, 6.0, {"20390018701": [[1, 999]]})])
        b = snap([comp(7, 100001, 1.0, 2.0, {}),
                  comp(9, 203900, 5.0, 6.0, {"20390018701": [[7, 999]]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"], [])
        self.assertEqual(len(d["components"]["renumbered"]), 1)


class TestDiffKinds(unittest.TestCase):
    """换号洗不掉的差异，要**说清是什么性质**：跨会话还原时引擎会把浮点末位规范化、
    也会把同一批元素换个顺序落盘。这两种若不点名，1:1 就永远只能报"不是 1:1"，
    而人无法判断到底是图坏了还是表示法变了。
    """

    def test_precision_only_is_soft(self):
        a = snap([comp(1, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201332092285, -282.048370]}]}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201000213623, -282.048004]}]}]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"][0]["kinds"]["1000400113101"],
                         "precision")

    def test_nested_reorder_is_resorted_not_value(self):
        """换序发生在**嵌套内层**（春岚 1000400113101 的真实形状）。"""
        pts = [{"p": [919.0, 62.2, -282.0], "t": 101001},
               {"p": [925.0, 60.1, -222.5], "t": 101001},
               {"p": [946.6, 63.3, -273.6], "t": 101001}]
        a = snap([comp(1, 10004001, 0.0, 0.0, {"1000400113101": [{"bron": pts}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": list(reversed(pts))}]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"][0]["kinds"]["1000400113101"],
                         "resorted")

    def test_one_point_really_moved_stays_value(self):
        pts = [{"p": [919.0, 62.2, -282.0], "t": 101001},
               {"p": [925.0, 60.1, -222.5], "t": 101001}]
        moved = [{"p": [919.0, 62.2, -282.0], "t": 101001},
                 {"p": [999.0, 60.1, -222.5], "t": 101001}]
        a = snap([comp(1, 10004001, 0.0, 0.0, {"1000400113101": [{"bron": pts}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0, {"1000400113101": [{"bron": moved}]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"][0]["kinds"]["1000400113101"],
                         "value")

    def test_oversized_list_is_not_fuzzy_matched(self):
        """超过匹配上限就从严报 value：判据宁可吵，也不能把 accept 跑成几分钟的停顿。"""
        big = [[i, 0.0, 0.0] for i in range(sn._SET_MATCH_MAX + 10)]
        a = snap([comp(1, 203911, 5.0, 6.0, {"20391119001": [big]})])
        b = snap([comp(9, 203911, 5.0, 6.0, {"20391119001": [list(reversed(big))]})])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"][0]["kinds"]["20391119001"], "value")

    def test_soft_only_makes_component_layer_warn_not_fail(self):
        pts = [{"p": [919.0, 62.2, -282.0]}, {"p": [925.0, 60.1, -222.5]}]
        a = snap([comp(1, 10004001, 0.0, 0.0, {"1000400113101": [{"bron": pts}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0, {"1000400113101": [{"bron": list(reversed(pts))}]})])
        rep = sn.acceptance_report(a, b)
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "warn", msg=l["msg"])
        self.assertEqual(l["counts"]["changed_value"], 0)
        self.assertEqual(l["counts"]["changed_soft"], 1)
        self.assertIn("顺序", l["msg"])
        self.assertIn("组件", rep["warn"])
        self.assertFalse(rep["ok"], msg="warn 层不算通过：1:1 的结论要人签字，不由工具代签")

    def test_transform_only_row_is_hard(self):
        """没有 kinds 的行（纯变换差异/旧结果）一律从严，不许被当成表示法差异。"""
        self.assertTrue(sn._is_hard_change({"properties": ["<transform>"]}))


    def test_precision_only_does_not_block_the_verdict(self):
        """只差浮点末位 ⇒ 组件层 **ok**：`--tol` 就是本项目声明的坐标尺度，
        而且引擎每次落盘都会重新规范化末位——把它压成 warn 等于宣布跨账号还原
        永远拿不到 exit 0，那条收口命令就废了。"""
        a = snap([comp(1, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201332092285, -282.048370]}]}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201000213623, -282.048004]}]}]})])
        rep = sn.acceptance_report(a, b)
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "ok", msg=l["msg"])
        self.assertEqual(l["counts"]["changed_precision"], 1)
        self.assertEqual(l["counts"]["changed_value"], 0)
        self.assertIn("浮点末位", l["msg"])
        self.assertTrue(rep["ok"], msg=rep["layers"])

    def test_order_difference_still_needs_a_human(self):
        """同一件里既有末位差又有**换序** ⇒ 换序那一维仍然要人确认（warn）。"""
        far = {"p": [1200.0, 60.1, -222.5]}
        near = {"p": [919.0, 62.201332, -282.048370]}
        a = snap([comp(1, 10004001, 0.0, 0.0,
                      {"1000400113101": [{"bron": [near, far]}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                      {"1000400113101": [{"bron": [dict(near, p=[919.0, 62.201, -282.048]),
                                                 far]}]})])
        # 顺序变了（换成 far 在前也算），先把"只是末位差"那条路排掉：这里两边元素集相同
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"][0]["kinds"]["1000400113101"],
                         "precision", msg="先确认这条夹具走的是末位差")
        rep = sn.acceptance_report(a, b)
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "ok", msg=l["msg"])
        # 真正的换序另造一件：两点互换（距离远超 tol，末位也一样）
        c = snap([comp(1, 10004001, 5.0, 5.0,
                      {"1000400113101": [{"bron": [near, far]}]})])
        e = snap([comp(8, 10004001, 5.0, 5.0,
                      {"1000400113101": [{"bron": [far, near]}]})])
        rep2 = sn.acceptance_report(c, e)
        l2 = [x for x in rep2["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l2["level"], "warn", msg=l2["msg"])
        self.assertEqual(l2["counts"]["changed_soft"], 1)
        self.assertFalse(rep2["ok"])

    def test_tighter_tol_upgrades_last_digit_difference(self):
        """想更严就自己加：`--tol 0.01` 下同一批浮点差升级成 value ⇒ fail。"""
        a = snap([comp(1, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201332092285, -282.048370]}]}]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                       {"1000400113101": [{"bron": [{"p": [919.0, 62.201000213623, -282.048004]}]}]})])
        rep = sn.acceptance_report(a, b, tol=0.0001)
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "fail", msg=l["msg"])
        self.assertEqual(l["counts"]["changed_value"], 1)


    def test_mixed_row_takes_the_worse_tier(self):
        """一件里既有换序又有末位差 ⇒ 取最坏那一档（warn），轻的那维不许盖掉重的。"""
        far = {"p": [1200.0, 60.1, -222.5]}
        near = {"p": [919.0, 62.201332, -282.04837]}
        other = {"p": [925.0, 60.1, -222.5]}
        a = snap([comp(1, 10004001, 0.0, 0.0,
                      {"1000400113101": [{"bron": [near, far]}],
                       "1000400113102": [other]})])
        b = snap([comp(7, 10004001, 0.0, 0.0,
                      {"1000400113101": [{"bron": [far, near]}],
                       "1000400113102": [{"p": [925.0, 60.1001, -222.5]}]})])
        d = sn.diff_snapshots(a, b)
        kinds = d["components"]["changed"][0]["kinds"]
        self.assertEqual(sorted(kinds.values()), ["precision", "resorted"], msg=kinds)
        rep = sn.acceptance_report(a, b)
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "warn", msg=l["msg"])
        self.assertEqual(l["counts"]["changed_soft"], 1)
        self.assertEqual(l["counts"]["changed_precision"], 0)


class TestAcceptanceWithRenumber(unittest.TestCase):

    def _layer(self, rep):
        return [l for l in rep["layers"] if l["key"] == "组件"][0]

    def test_component_layer_passes_with_renumber_only(self):
        rep = sn.acceptance_report(A, B)
        l = self._layer(rep)
        self.assertEqual(l["level"], "ok", msg=l["msg"])
        self.assertEqual(l["counts"]["renumbered"], 1)
        self.assertEqual(l["counts"]["changed"], 0)
        self.assertIn("重编号", l["msg"])
        self.assertTrue(rep["ok"], msg=rep["layers"])

    def test_component_layer_still_fails_on_real_change(self):
        b = copy.deepcopy(B)
        for c in b["components"]:
            if c["g"] == 9:
                info = json.loads(c["ci"])
                for e in info["events"]:
                    if str(e["type"]) == "20390018701":
                        e["info"] = [[]]
                c["ci"] = json.dumps(info, ensure_ascii=False)
        rep = sn.acceptance_report(A, b)
        l = self._layer(rep)
        self.assertEqual(l["level"], "fail", msg=l["msg"])
        self.assertEqual(l["counts"]["changed"], 1)
        self.assertEqual(l["counts"]["renumbered"], 0)

    def test_print_diff_counts_renumber_rows(self):
        """打印层也要说清楚：不报的话，人只会看到"属性变更 0"却不知道少了 3 条。"""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(sn.diff_snapshots(A, B))
        out = buf.getvalue()
        self.assertIn("仅 id 重编号", out)
        self.assertIn("20390018701", out)


@unittest.skipUnless(os.path.exists(FIXTURE),
                     "缺春岚实机夹具 %s ⇒ 真实形状未执行（不是通过）" % FIXTURE)
class TestRealChunlanCase(unittest.TestCase):
    """同一套判据要能在**真实数据**上跑出同样的分类。

    合成夹具锁的是逻辑分支，这里锁的是"春岚上这些字段确实长这样"：成员表是
    `[[gid]]` 套一层、出生点列表的换序发生在 `[{...,"bron":[点,…]}]` 的**内层**。
    顺带把 2026-10-01 那次试点损伤的复原结果钉住（复原没做对，这里就会红）。
    """

    @classmethod
    def setUpClass(cls):
        with io.open(FIXTURE, encoding="utf-8") as f:
            cls.fx = json.load(f)

    def _bag(self, side, gid):
        for c in self.fx[side]["components"]:
            if str(c["g"]) == str(gid):
                return {str(e["type"]): e["info"] for e in json.loads(c["ci"])["events"]}
        raise AssertionError("%s 侧没有 gid=%s" % (side, gid))

    def test_real_shapes_classify_the_same_way(self):
        d = sn.diff_snapshots(self.fx["a"], self.fx["b"])
        c = d["components"]
        self.assertEqual((len(c["added"]), len(c["removed"])), (0, 0))
        self.assertEqual(len(c["renumbered"]), 3, msg=c["renumbered"])
        self.assertEqual(len(c["changed"]), 1, msg=c["changed"])
        self.assertEqual(c["changed"][0]["kinds"], {"1000400113101": "resorted"})
        # 换号那三件里有一件是"跨号"的（a 的 14983 ↔ b 的 14982）——
        # 说明配对给的就是新号，不是"同gid 自己映射自己"这种没验证到的路径
        self.assertIn([14983, 14982], [x["ugc_id"] for x in c["renumbered"]])

    def test_acceptance_on_real_case_is_warn_not_fail(self):
        rep = sn.acceptance_report(self.fx["a"], self.fx["b"])
        l = [x for x in rep["layers"] if x["key"] == "组件"][0]
        self.assertEqual(l["level"], "warn", msg=l["msg"])
        self.assertEqual(l["counts"]["changed_value"], 0)
        self.assertEqual(l["counts"]["renumbered"], 3)

    def test_pilot_damage_is_repaired_in_the_real_case(self):
        """运动器 14998（a 侧的 14982）已被复原成交付内容，且组合件成员表非空。"""
        mot = self._bag("b", 14998)
        self.assertEqual(mot["20391102001"], [u"春嵐_上映_chorus-split-15"])
        self.assertEqual(mot["20391102004"], [u"春嵐_38秒区間"])
        self.assertEqual(len(mot["20391119001"][0]), 19)
        self.assertEqual(self._bag("b", 14981)["20390018701"], [[14998]])


class TestStoryRefsAreNormalized(unittest.TestCase):
    """对话件的「所属剧情」存的是剧情节点 ugc_id：跨会话换号后要算「同一指向」。

    2026-10-01 实机：春岚那件对话写 `[118]`（它自己那件剧情的号），搬到新图后剧情件是
    133、而 `[117]` 指着的是一件时钟件 ⇒ 这一族若既不换号又不归一，"接错线"在两层报告里
    都看不见（比对只说"值不同"，还原只说"写成功"）。
    """

    def test_dialogue_story_ref_is_normalized(self):
        ea = {"1000700240001": [118]}
        eb = {"1000700240001": [133]}
        lut = sn._id_lut([({"g": 118}, {"g": 133})])
        self.assertTrue(sn._renumber_only(ea, eb, lut),
                        msg="换了号就算重新编号，不该报成内容差异")
        self.assertEqual(sn._norm_ids(ea, lut), eb)

    def test_choice_jump_refs_are_in_the_same_set(self):
        from snapshot import STORY_REF_PROPERTIES as SR
        for pid in ("1000700340001", "1000700340005", "1000700340006"):
            self.assertIn(pid, SR, msg="选项→对话 / 跳转目标同族，漏一个就漏一类接错线")


class TestStackedSameBagItemsPairByHeight(unittest.TestCase):
    """同一格子里同袋的两件（春岚那两块薄平台 x/z 相同、只差 y）不许交叉配对。

    实机：两块 98x0.05x62、颜色同为 151515 的平台，源图 y=60 / y=496.124，
    目标图同样是这两层。第一遍配对只用 x/z 格子 + 内容签名（两件签名完全相同）
    ⇒ 会配成交叉，判据于是报出两处「落点差异」——假阳性，还会把真问题挤出去。
    """

    def _slab(self, g, y):
        return {"g": g, "i": 301001, "p": [923.738, y, -401.555], "r": [0, 0, 0],
                "s": [98.0, 0.05, 62.27],
                "ci": json.dumps({"component_id": 100001, "events": [
                    {"type": "10000110006", "info": [["151515"]]}]})}

    def test_equal_multisets_are_identical(self):
        a = [self._slab(1, 60.0), self._slab(2, 496.124)]
        b = [self._slab(9, 496.124), self._slab(10, 60.0)]   # 顺序也反过来
        pairs, rest = sn._pair_components(a, b, 0.5)
        self.assertEqual(rest, [])
        self.assertTrue(all(x is not None and abs(x["p"][1] - y["p"][1]) < 0.5
                            for x, y in pairs),
                        msg="真实 y 要各归各：配对不能只看 x/z 格子")
        d = sn.diff_snapshots({"components": a}, {"components": b})
        self.assertTrue(d["identical"], msg=d["components"])


class TestPositionIsPartOfTheVerdict(unittest.TestCase):
    """落点要进判据：配对用**可信坐标**，变换比对含 `p`。

    两条都是 2026-10-01 实机逼出来的：
    * 交互区 203022 的 `get_position3()` 是哨兵、真值在 `pp` ⇒ 旧口径按 `p` 配对，
      源图落在"哨兵格"、目标图落在真坐标格，同一件被报成一加一删。
    * 一块薄平台被引擎摆到 248 单位之外、还低 58 ⇒ 而 `p` 根本不在比对字段里，
      "作者内容 0 差异"是假的（摆放侧也从不回读落点）。
    """

    def test_pairing_uses_the_real_position(self):
        a = {"g": 1, "i": 203022, "p": [0.0, -65536.0, 0.0],
             "pp": [928.0, -67.923, -147.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 203022, "events": []})}
        b = {"g": 2, "i": 203022, "p": [928.0, -67.923, -147.0], "r": [0, 0, 0],
             "s": [1, 1, 1], "ci": json.dumps({"component_id": 203022, "events": []})}
        pairs, rest = sn._pair_components([a], [b], 0.5)
        self.assertEqual(rest, [], msg="第二遍（可信坐标）必须把这件配上，不许报成一加一删")
        self.assertIsNotNone(pairs[0][0])
        # 第一遍仍按 `p` 格子配：这两件配不上，正是靠第二遍（可信坐标）补上的
        self.assertNotEqual(sn._cell(a, 0.5), sn._cell(b, 0.5))
        self.assertEqual(sn._cell(a, 0.5, True), sn._cell(b, 0.5, True))
        self.assertEqual(sn._transform_diff(a, b, 0.5), ([], {}),
                         msg="单边 pp 是字段版本差，不是挪动")

    def test_y_only_shift_is_reported(self):
        a = {"g": 1, "i": 301001, "p": [923.74, 60.0, -401.55], "r": [0, 0, 0],
             "s": [98.0, 0.05, 62.27],
             "ci": json.dumps({"component_id": 100001, "events": []})}
        b = {"g": 2, "i": 301001, "p": [923.74, 2.0, -401.55], "r": [0, 0, 0],
             "s": [98.0, 0.05, 62.27],
             "ci": json.dumps({"component_id": 100001, "events": []})}
        fields, kinds = sn._transform_diff(a, b, 0.5)
        self.assertEqual(fields, [u"落点"], msg="只挪 y 也要报（旧口径看不见）")
        self.assertEqual(kinds["<transform>"], "value")

    def test_sentinel_side_is_not_compared_as_moved(self):
        a = {"g": 1, "i": 203912, "p": [0.0, -65536.0, 0.0], "r": [0, 0, 0],
             "s": [1, 1, 1], "ci": json.dumps({"component_id": 203912, "events": []})}
        b = {"g": 2, "i": 203912, "p": [993.7, -644.6, -100.1], "r": [0, 0, 0],
             "s": [1, 1, 1], "ci": json.dumps({"component_id": 203912, "events": []})}
        fields, _kinds = sn._transform_diff(a, b, 0.5)
        self.assertNotIn(u"落点", fields,
                         msg="哨兵位没有可比性（运动点这类每图自带），比了全是假差异")


class TestTransformIsNotSwallowedByRenumber(unittest.TestCase):
    """归一越好，"只看一边"的分支越危险。

    旧代码是 `if 袋不同: … elif 变换不同: …`：一件**同时**只差落盘 id 重编号、旋转又
    真变了 90° 时，整件被归进 `renumbered`，旋转差异一条都不印 ⇒ 图上摆歪了的件在
    "1:1 验收"里隐身。现在袋与变换各算一遍，任一边有差异都不许进 renumbered。
    """

    @staticmethod
    def _pair(r_a, r_b, s_a=None, s_b=None, pp_a=None, pp_b=None):
        a = snap([comp(1, 100001, 1.0, 2.0, {}),
                  dict(comp(3, 203900, 5.0, 6.0, {"20390018701": [[1]]}), r=r_a,
                       s=s_a or [1, 1, 1], **({"pp": pp_a} if pp_a else {}))])
        b = snap([comp(7, 100001, 1.0, 2.0, {}),
                  dict(comp(9, 203900, 5.0, 6.0, {"20390018701": [[7]]}), r=r_b,
                       s=s_b or [1, 1, 1], **({"pp": pp_b} if pp_b else {}))])
        return a, b

    def test_renumbered_bag_plus_real_rotation_is_still_a_change(self):
        a, b = self._pair([0, 0, 0], [0, 90, 0])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["renumbered"], [],
                         msg="袋只差换号就把旋转吞掉 ⇒ 归一分支吃掉了变换差异")
        row = d["components"]["changed"][0]
        self.assertIn("<transform>", row["properties"])
        self.assertEqual(row["kinds"]["<transform>"], "value")
        self.assertEqual(row["kinds"]["20390018701"], "renumber")
        self.assertEqual(row["transform_fields"], [u"旋转"])
        self.assertTrue(sn._is_hard_change(row))
        rep = sn.acceptance_report(a, b)
        self.assertEqual([l["level"] for l in rep["layers"] if l["key"] == "组件"][0],
                         "fail")

    def test_renumbered_bag_plus_within_tol_rotation_is_not_a_block(self):
        """袋只差换号 + 旋转在 tol 内 ⇒ 归到"浮点末位"那一档，不拦结论（见上面的理由）。"""
        a, b = self._pair([0, 0, 0], [0, 0.2, 0])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["renumbered"], [])
        row = d["components"]["changed"][0]
        self.assertEqual(row["kinds"]["<transform>"], "precision")
        self.assertFalse(sn._is_hard_change(row))
        self.assertFalse(d["identical"], msg="比出来仍要说「有几件不完全相同」")
        rep = sn.acceptance_report(a, b)
        self.assertEqual([l["level"] for l in rep["layers"] if l["key"] == "组件"][0],
                         "ok")

    def test_pp_move_alone_is_reported(self):
        """逻辑件的 p 恒为哨兵，挪动只体现在 pp ⇒ 不比 pp 就是漏报。"""
        a, b = self._pair([0, 0, 0], [0, 0, 0], pp_a=[1.0, 0.0, 2.0],
                         pp_b=[40.0, 0.0, 2.0])
        d = sn.diff_snapshots(a, b)
        row = d["components"]["changed"][0]
        self.assertEqual(row["transform_fields"], [u"真实位置"])
        self.assertEqual(row["kinds"]["<transform>"], "value")

    def test_scale_beyond_tol_is_hard(self):
        a, b = self._pair([0, 0, 0], [0, 0, 0], s_a=[1, 1, 1], s_b=[2, 2, 2])
        d = sn.diff_snapshots(a, b)
        row = d["components"]["changed"][0]
        self.assertEqual(row["transform_fields"], [u"缩放"])
        self.assertTrue(sn._is_hard_change(row))

    def test_identical_still_passes_with_both_sides_equal(self):
        a, b = self._pair([0, 37, 0], [0, 37, 0])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["changed"], [])
        self.assertEqual(len(d["components"]["renumbered"]), 1)
        self.assertTrue(d["identical"])


class TestMapPropsLayerIsCompared(unittest.TestCase):
    """地图属性层（默认背景音乐）—— 2026-10-01 用户报「还原出来的图没有地图默认背景音乐」。

    BGM 既不是组件（203920 音乐事件两张图逐字段相同），也不是 map_setting 的玩法键
    （MUSIC=7/SOUND=6 两张图都没写过），它只在 `room.ugc_props_info['ugc_bgm_conf']`。
    这一层以前根本不采，于是七层全绿的验收能在一张开局静音的图上印绿灯。
    """

    def _pair(self, props_a, props_b):
        c = [comp(1, 100001, 1.0, 2.0, {})]
        return snap(c, props=props_a), snap(c, props=props_b)

    def test_bgm_difference_fails_the_map_props_layer(self):
        a, b = self._pair(_props_layer(ugc_bgm_conf={"mode": 3, "param": {"songId": "X"}}),
                          _props_layer(ugc_bgm_conf=None))
        d = sn.diff_snapshots(a, b)
        self.assertEqual([x["key"] for x in d["map_props"]["removed"]], ["ugc_bgm_conf"])
        self.assertFalse(d["identical"])
        rep = sn.acceptance_report(a, b)
        row = [l for l in rep["layers"] if l["key"] == "地图属性"][0]
        self.assertEqual(row["level"], "fail")
        self.assertFalse(rep["ok"])

    def test_both_unset_is_a_real_pass(self):
        """两边都"什么都没设"是真一致（采到了、值为 null）—— 与"没采到"待遇不同。"""
        a, b = self._pair(_props_layer(), _props_layer())
        rep = sn.acceptance_report(a, b)
        row = [l for l in rep["layers"] if l["key"] == "地图属性"][0]
        self.assertEqual(row["level"], "ok")

    def test_old_export_without_the_layer_is_unknown_not_ok(self):
        old = snap([comp(1, 100001, 1.0, 2.0, {})])
        del old["map"]["props"]                    # 2026-10-01 之前的导出就是这样
        new = snap([comp(1, 100001, 1.0, 2.0, {})])
        rep = sn.acceptance_report(old, new)
        row = [l for l in rep["layers"] if l["key"] == "地图属性"][0]
        self.assertEqual(row["level"], "unknown")
        self.assertIn("地图属性", rep["unknown"])
        self.assertFalse(rep["ok"], msg="「没采到」永远不能算通过")

    def test_precheck_names_the_missing_layer(self):
        old = snap([comp(1, 100001, 1.0, 2.0, {})])
        del old["map"]["props"]
        rows = {r["key"]: r for r in sn.validate_snapshot(old)["items"]}
        self.assertEqual(rows["map_props"]["level"], "warn")
        self.assertIn("背景音乐", rows["map_props"]["msg"])

    def test_precheck_reports_captured_bgm_value(self):
        s = snap([comp(1, 100001, 1.0, 2.0, {})],
                 props=_props_layer(ugc_bgm_conf={"mode": 3, "param": {"songId": "X"}}))
        rows = {r["key"]: r for r in sn.validate_snapshot(s)["items"]}
        self.assertEqual(rows["map_props"]["level"], "ok")
        self.assertIn("mode", rows["map_props"]["msg"])


if __name__ == "__main__":
    unittest.main()
