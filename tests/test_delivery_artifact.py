#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_delivery_artifact — 交付物**本身**的回归锁（用真实的春岚快照，不是合成夹具）。

为什么单独立一个文件：这轮所有判据（`check` / `accept` / 重复件 / 事件锚点 / 残骸身份）
都在合成小夹具上验过，但"能交付"这句话的真正证据只能来自**要交给别人的那份文件**。
这里锁四件事：

1. 手册里记的指纹必须等于文件当下内容（操作者第 0 步就是比它；漂了一眼就能看出来）；
2. 那份快照自己过得了交付体检，且不含删除残骸行；
3. 逐件 `item_id` 取值正确（共用单元上不是"单元第一件的 id"）—— 这是原始需求里
   "1:1"最容易悄悄坏掉的一处；
4. 已知的真实差额被如实报出（多一份同内容重复的运动器 + 一件待提交删除的运动器），
   即判据在真实数据上既不漏报也不虚报。
"""
import hashlib
import io
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn            # noqa: E402
import snapshot_restore as sr    # noqa: E402

DELIVERABLE = os.path.join(ROOT, "snapshots", "chunlan_1to1_20260930.json")
NOTES = DELIVERABLE[:-len(".json")] + ".restore_notes.md"
RECHECK = os.path.join(ROOT, "snapshots", "chunlan_recheck_20261001.json")
UI_NATIVE = sn.UI_NATIVE_TYPE


def _load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


@unittest.skipUnless(os.path.exists(DELIVERABLE),
                     "缺交付快照 %s ⇒ 交付物回归未执行（不是通过）" % DELIVERABLE)
class TestDeliverableArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = _load(DELIVERABLE)

    def test_fingerprint_matches_the_manual(self):
        """手册第 0 步让操作者比 sha256；这个数字漂了就等于第一步就骗人。"""
        with open(DELIVERABLE, "rb") as fh:
            real = hashlib.sha256(fh.read()).hexdigest()[:16]
        self.assertTrue(os.path.exists(NOTES), "交付手册不在，指纹没处对")
        with io.open(NOTES, encoding="utf-8") as f:
            notes = f.read()
        claimed = set(re.findall(r"`([0-9a-f]{16})`", notes))
        self.assertTrue(claimed, "手册里找不到 16 位指纹的写法，那条步骤已失效")
        self.assertIn(real, claimed,
                      msg="文件指纹 %s 与手册记的 %s 不一致" % (real, sorted(claimed)))
        size = int(re.search(r"([\d,]+) bytes", notes).group(1).replace(",", ""))
        self.assertEqual(os.path.getsize(DELIVERABLE), size,
                         msg="手册记的字节数与文件不符")

    def test_deliverable_passes_its_own_delivery_gate(self):
        v = sn.validate_snapshot(self.snap)
        blockers = [i for i in v["items"] if i["level"] == "blocker"]
        self.assertEqual(blockers, [], msg=blockers)
        self.assertTrue(v["ok"], msg=[i["msg"] for i in v["items"]])

    def test_no_deleted_residue_rows_in_the_handoff(self):
        """带残骸的快照交给别人 = 让对方凭空多摆幽灵件；交付这份必须干净。"""
        rows = [c for c in self.snap["components"] if c.get("del")]
        self.assertEqual(rows, [], msg="交付快照里有 %d 件删除残骸" % len(rows))

    def test_every_row_carries_a_parseable_bag(self):
        bad = 0
        for c in self.snap["components"]:
            try:
                ci = json.loads(c["ci"]) if isinstance(c.get("ci"), str) else c.get("ci")
                self.assertIsInstance(ci.get("component_id"), int)
            except Exception:
                bad += 1
        self.assertEqual(bad, 0, msg="%d 行的 component_info 解析失败/缺 component_id" % bad)
        # 严格 dumps 的产物里不许有 `_safe()` 的截断占位（有的话就是静默丢数据）
        raw = "".join(str(c.get("ci")) for c in self.snap["components"])
        self.assertNotIn("<deep>", raw[:400000])
        self.assertNotIn("<deep>", raw[400000:])

    def test_shared_unit_rows_keep_their_own_item_id(self):
        """共用单元上的件必须带**自己的** item_id（原始需求里"1:1"最容易悄悄坏的一处）。

        春岚 48 件 UI_NATIVE 全挂在一个 `UgcGlobalUnit` 上；取 `unit.ugc_type` 会把它们
        全印成同一个 item，还原时整批摆成同一种控件，而本地回读还一致。
        """
        rows = [c for c in self.snap["components"]
                if (json.loads(c["ci"]).get("component_id") if isinstance(c.get("ci"), str)
                    else None) == UI_NATIVE]
        self.assertTrue(rows, "这份快照里没有 UI_NATIVE？那条历史缺陷要重新表述")
        items = {c["i"] for c in rows}
        self.assertTrue(all(isinstance(i, int) and i > 0 for i in items), items)
        # 关键判据：同类型多件时，item 集合不是"整个文件只剩一个值"那种退化——
        # 而是**逐件问出来的**：与重导那份在同 gid 上一一相等（见下一条）
        self.assertGreaterEqual(len(rows), 2)

    @unittest.skipUnless(os.path.exists(RECHECK),
                         "缺重导快照（本轮实况）⇒ 交叉核对未执行")
    def test_item_ids_agree_with_a_fresh_export_per_component(self):
        """与当前导出器重导的那份逐件比 `i`：交付文件的身份取值不是旧口径。"""
        recheck = _load(RECHECK)
        b = {c["g"]: c for c in recheck["components"]}
        drift = []
        for c in self.snap["components"]:
            other = b.get(c["g"])
            if other is None or other.get("del"):
                continue
            if c.get("i") != other.get("i"):
                drift.append((c["g"], c.get("i"), other.get("i")))
        self.assertEqual(drift, [],
                         msg="%d 件的 item_id 与重导不一致（旧口径取 unit.ugc_type）：首若干 %s"
                              % (len(drift), drift[:5]))

    @unittest.skipUnless(os.path.exists(RECHECK),
                         "缺重导快照 ⇒ 已知真实差额的判据未执行")
    def test_acceptance_reports_the_known_real_delta(self):
        """把本轮查明的差额**钉在判据上**：多一件同内容重复、11 件待提交删除。

        这既是"判据在真实数据上不漏报"的证据，也是反向的"不虚报"证据：
        事件/剧情/设置三层必须说一致（它们确实没变）。
        """
        rep = sn.acceptance_report(self.snap, _load(RECHECK))
        by = {l["key"]: l for l in rep["layers"]}
        self.assertFalse(rep["ok"], msg="这份读数带着待提交删除与重复件，不该说 1:1")
        self.assertEqual(by["组件"]["level"], "fail")
        dups = by["组件"]["counts"]["duplicate_extra"]
        self.assertEqual(dups, 1, msg="同内容重复的运动器没被点名：%s" % by["组件"]["msg"])
        self.assertEqual(by["残骸"]["level"], "warn")
        self.assertEqual(by["残骸"]["counts"]["to"], 11)
        self.assertNotEqual(by["事件"]["level"], "fail", msg=by["事件"]["msg"])
        self.assertEqual(by["剧情"]["level"], "ok", msg=by["剧情"]["msg"])
        self.assertEqual(by["地图设置"]["level"], "ok", msg=by["地图设置"]["msg"])

    @unittest.skipUnless(os.path.exists(RECHECK), "缺重导快照 ⇒ 未执行")
    def test_pending_deletion_identities_are_actionable(self):
        """待删除袋的警告必须能认出**是哪一件**，否则发现丢了东西要靠人回忆。"""
        items = sn.residue_identities(_load(RECHECK), limit=11)
        named = [x for x in items if x["name"]]
        self.assertTrue(named, msg="残骸警告里一个名字都没有：%s" % items)
        # 春岚作者自己命名的那件运动器就在里面（本轮就是这样认出来的）
        self.assertTrue(any("春嵐" in (x["name"] or "") for x in named), named)


class TestManualIsCurrent(unittest.TestCase):
    """手册里的计划数字要等于源码算出来的（漂了就是操作者照抄错）。"""

    @unittest.skipUnless(os.path.exists(DELIVERABLE) and os.path.exists(NOTES), "缺交付件")
    def test_plan_numbers_in_the_manual(self):
        with io.open(NOTES, encoding="utf-8") as f:
            notes = f.read()
        m = re.search(r"组件 (\d+) → 可摆 (\d+) ｜ 跳过 (\d+)", notes)
        self.assertTrue(m, msg="手册里的计划口径写法变了，本测试要同步改写")
        total, placeable, skipped = (int(x) for x in m.groups())
        snap = _load(DELIVERABLE)
        self.assertEqual(total, len(snap["components"]))
        types = set()   # 离线拿不到 live 白名单：用快照里出现过的 component_id 全集
        for c in snap["components"]:
            try:
                types.add(json.loads(c["ci"]).get("component_id"))
            except Exception:
                pass
        import argparse
        plan = sr.build_plan(snap, types, argparse.Namespace(
            include_ui=False, include_motion=False, allow_sentinel=True,
            with_story=True, include_global=False, include_not_placeable=True))
        self.assertEqual(plan["to_place"] + len(plan["skipped"]), total,
                         msg="计划不守恒，手册口径不可信")
        self.assertEqual(placeable, plan["to_place"],
                         msg="手册写可摆 %d，源码算 %d（%s）" % (placeable, plan["to_place"],
                                                    plan["skipped_by_reason"]))
        self.assertEqual(skipped, len(plan["skipped"]))


if __name__ == "__main__":
    unittest.main()
