#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_diff — 快照比对的纯逻辑锁（无需游戏）。

锁住这几件事，因为 diff 的结论会被直接拿来判定"保存有没有丢东西"：
1. ugc_id 每次重载都变，**不能**让它产生假差异（否则 diff 全是噪声）；
2. 坐标抖动小于容差不能算差异；
3. 属性 / 事件 / 剧情的变更必须报出来（漏报 = 丢了东西还说没事）。
"""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn  # noqa: E402


def _snap(comps, events=None, story=None, name="m", ms=None):
    mp = {"name": name}
    if ms is not None:
        mp["map_setting"] = ms
    return {"format": sn.FORMAT, "version": 1, "captured_at": "t0",
            "map": mp, "components": comps,
            "events": events or {"count": 0, "events": [], "saved_keys": []},
            "story": story or {"stories": []}, "budget": {}}


def _comp(g, i, x, z, y=0.0, ci=None):
    return {"g": g, "i": i, "p": [x, y, z], "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps(ci if ci is not None
                             else {"component_id": i, "events": []}, ensure_ascii=False)}


class TestAnchor(unittest.TestCase):
    def test_anchor_ignores_ugc_id(self):
        a = _comp(1, 100001, 10.0, 20.0)
        b = _comp(999, 100001, 10.0, 20.0)
        self.assertEqual(sn.anchor_of(a, 0.5), sn.anchor_of(b, 0.5))

    def test_tolerance_matching_across_cell_boundary(self):
        """10.0 与 10.4 落在不同的 0.5 量化格里，但距离 < 容差，必须配对成功。

        硬按格子配对会把这种抖动误报成「删一件 + 新增一件」，是假阳性。
        """
        a = [_comp(1, 100001, 10.0, 20.0)]
        b = [_comp(2, 100001, 10.4, 20.4)]
        pairs, unpaired = sn._pair_components(a, b, 0.5)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]["g"], 1)
        self.assertEqual(unpaired, [])

    def test_beyond_tolerance_is_not_paired(self):
        a = [_comp(1, 100001, 10.0, 20.0)]
        b = [_comp(2, 100001, 12.0, 20.0)]
        pairs, unpaired = sn._pair_components(a, b, 0.5)
        self.assertEqual(pairs[0][0], None)
        self.assertEqual(len(unpaired), 1)

    def test_anchor_distinguishes_item_id(self):
        a = _comp(1, 100001, 10.0, 20.0)
        b = _comp(2, 203003, 10.0, 20.0)
        self.assertNotEqual(sn.anchor_of(a, 0.5), sn.anchor_of(b, 0.5))


class TestDiffComponents(unittest.TestCase):
    def test_identical_after_ugc_id_renumber(self):
        """核心场景：重载后 id 全变，diff 必须仍然报「一致」。"""
        base = [_comp(1, 100001, 0.0, 0.0), _comp(2, 203003, 5.0, 5.0)]
        after = [_comp(501, 100001, 0.0, 0.0), _comp(502, 203003, 5.0, 5.0)]
        d = sn.diff_snapshots(_snap(base), _snap(after))
        self.assertTrue(d["identical"], msg="id 重编号不应产生差异")
        self.assertEqual(d["components"]["added"], [])
        self.assertEqual(d["components"]["removed"], [])
        self.assertEqual(d["components"]["changed"], [])

    def test_added_and_removed(self):
        a = [_comp(1, 100001, 0.0, 0.0)]
        b = [_comp(1, 100001, 0.0, 0.0), _comp(2, 203003, 9.0, 9.0)]
        d = sn.diff_snapshots(_snap(a), _snap(b))
        self.assertEqual(len(d["components"]["added"]), 1)
        self.assertEqual(d["components"]["removed"], [])
        d2 = sn.diff_snapshots(_snap(b), _snap(a))
        self.assertEqual(len(d2["components"]["removed"]), 1)

    def test_pp_change_reported_as_transform(self):
        """逻辑件的 `p` 恒为哨兵，挪动只体现在 `pp` —— 不比就是「改了还说没事」。"""
        a = [_comp(1, 203022, 0.0, 0.0, y=-65536.0)]
        a[0]["pp"] = [928.0, -67.923, -147.0]
        b = copy.deepcopy(a)
        b[0]["pp"] = [700.0, -67.923, -147.0]
        d = sn.diff_snapshots(_snap(a), _snap(b))
        self.assertFalse(d["identical"])
        self.assertEqual(d["components"]["changed"][0]["properties"], ["<transform>"])

    def test_pp_absent_both_sides_is_not_a_change(self):
        """老快照没有 pp 字段时不能凭空报出差异（None vs None）。"""
        a = [_comp(1, 203022, 0.0, 0.0, y=-65536.0)]
        b = copy.deepcopy(a)
        self.assertTrue(sn.diff_snapshots(_snap(a), _snap(b))["identical"])

    def test_one_sided_pp_is_schema_diff_not_movement(self):
        """只有新快照带 pp（字段是 2026-09-30 才加的）不能报成「挪动了」。

        否则新旧文件一比就满是假差异，真正的问题反而被噪声盖掉。
        """
        a = [_comp(1, 203022, 0.0, 0.0, y=-65536.0)]                    # 老快照
        b = copy.deepcopy(a)
        b[0]["pp"] = [928.0, -67.923, -147.0]
        self.assertTrue(sn.diff_snapshots(_snap(a), _snap(b))["identical"])

    def test_property_change_detected(self):
        a = [_comp(1, 100001, 0.0, 0.0, ci={"component_id": 100001,
                                            "events": [{"type": 10000110006,
                                                        "info": [["FFFFFF"]]}]})]
        b = copy.deepcopy(a)
        b[0]["ci"] = json.dumps({"component_id": 100001,
                                 "events": [{"type": 10000110006,
                                             "info": [["000000"]]}]})
        d = sn.diff_snapshots(_snap(a), _snap(b))
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertIn("10000110006", d["components"]["changed"][0]["properties"])
        self.assertFalse(d["identical"])

    def test_duplicate_at_same_anchor(self):
        """同位置多一件（重复件）必须报出来 —— 保存回滚的典型症状。"""
        a = [_comp(1, 100001, 3.0, 4.0)]
        b = [_comp(1, 100001, 3.0, 4.0), _comp(2, 100001, 3.05, 4.05)]
        d = sn.diff_snapshots(_snap(a), _snap(b))
        self.assertEqual(len(d["components"]["added"]), 1)

    def test_transform_change_detected(self):
        a = [_comp(1, 100001, 0.0, 0.0)]
        b = copy.deepcopy(a)
        b[0]["r"] = [0, 90, 0]
        d = sn.diff_snapshots(_snap(a), _snap(b))
        self.assertEqual(len(d["components"]["changed"]), 1)
        self.assertIn("<transform>", d["components"]["changed"][0]["properties"])


class TestDiffEvents(unittest.TestCase):
    def _ev(self, key, ugc_id, prop="20300311106", val="['k']"):
        return {"count": 1, "saved_keys": [key],
                "events": [{"event_key": key, "sort_id": 0,
                            "refs": [{"ugc_id": ugc_id, "index": 5, "params_idx": 0,
                                      "component_type": 203003, "property_id": prop,
                                      "current_value": val}],
                            "in_save": True}]}

    def test_event_refs_survive_renumber(self):
        comps_a = [_comp(1, 203003, 1.0, 2.0)]
        comps_b = [_comp(777, 203003, 1.0, 2.0)]
        a = _snap(comps_a, self._ev("踏入灯下", 1))
        b = _snap(comps_b, self._ev("踏入灯下", 777))
        d = sn.diff_snapshots(a, b)
        self.assertTrue(d["identical"], msg="事件引用靠锚点，不应受 ugc_id 变化影响")

    def test_event_added_removed_changed(self):
        comps = [_comp(1, 203003, 1.0, 2.0)]
        a = _snap(comps, self._ev("A", 1))
        b = _snap(comps, self._ev("B", 1))
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["added"], ["B"])
        self.assertEqual(d["events"]["removed"], ["A"])

        c = _snap(comps, self._ev("A", 1, val="['别的']"))
        d2 = sn.diff_snapshots(a, c)
        self.assertEqual(d2["events"]["changed"], ["A"])

    def test_dangling_ref_reported_as_change(self):
        """引用了一个快照里不存在的物件 → 退回 uid 标记，必须显出差异。"""
        comps_a = [_comp(1, 203003, 1.0, 2.0)]
        comps_b = []          # 物件没了
        a = _snap(comps_a, self._ev("A", 1))
        b = _snap(comps_b, self._ev("A", 1))
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], ["A"])
        self.assertEqual(len(d["components"]["removed"]), 1)


class TestDiffStory(unittest.TestCase):
    def _story(self, content):
        return {"stories": [{"ugc_id": 119, "name": "序章", "dialogues": [
            {"ugc_id": 121, "sort_id": 1, "content": content, "choices": []}]}]}

    def test_story_same_is_identical(self):
        a = _snap([], None, self._story("你好"))
        b = _snap([], None, self._story("你好"))
        self.assertTrue(sn.diff_snapshots(a, b)["identical"])

    def test_story_content_change(self):
        a = _snap([], None, self._story("你好"))
        b = _snap([], None, self._story("再见"))
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["story"]["changed"], ["序章"])
        self.assertFalse(d["identical"])

    def test_story_added(self):
        a = _snap([], None, {"stories": []})
        b = _snap([], None, self._story("你好"))
        self.assertEqual(sn.diff_snapshots(a, b)["story"]["added"], ["序章"])


class TestDiffMapSetting(unittest.TestCase):
    """地图级**玩法设置**也要比：漏了它，"两份快照一致"会在玩法不同的图上报一致。

    这是 2026-10-01 交付春岚时发现的口径洞：还原手册第 3 步就用 diff 判"重进后有没有丢"，
    而当时 diff 只看组件/事件/剧情，地图设置整层不在判据里。
    """

    def test_value_change_is_reported_and_breaks_identical(self):
        a = _snap([], ms={"1": "0", "2": "1"})
        b = _snap([], ms={"1": "1", "2": "1"})
        d = sn.diff_snapshots(a, b)
        self.assertEqual([r["key"] for r in d["map_setting"]["changed"]], ["1"])
        self.assertEqual(d["map_setting"]["changed"][0]["to"], "1")
        self.assertFalse(d["identical"])

    def test_added_and_removed_keys(self):
        d = sn.diff_snapshots(_snap([], ms={"1": "0"}), _snap([], ms={"1": "0", "9": "1"}))
        self.assertEqual([r["key"] for r in d["map_setting"]["added"]], ["9"])
        self.assertEqual(d["map_setting"]["removed"], [])
        self.assertFalse(d["identical"])
        d2 = sn.diff_snapshots(_snap([], ms={"1": "0", "9": "1"}), _snap([], ms={"1": "0"}))
        self.assertEqual([r["key"] for r in d2["map_setting"]["removed"]], ["9"])

    def test_map_identity_keys_are_not_compared(self):
        """地图名/简介/标签跨账号本就不同 —— 报成差异只会教人忽略整份报告。"""
        a = _snap([], name="春岚", ms={"1": "0", "name": "春岚", "desc": "甲", "tags": ["x"]})
        b = _snap([], name="B 的图", ms={"1": "0", "name": "B 的图", "desc": "", "tags": []})
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["map_setting"]["changed"], [])
        self.assertTrue(d["identical"])

    def test_framework_key_is_part_of_the_layer(self):
        d = sn.diff_snapshots(_snap([], ms={"14": "4002"}), _snap([], ms={"14": "4001"}))
        self.assertEqual([r["key"] for r in d["map_setting"]["changed"]], ["14"])

    def test_int_and_str_values_match(self):
        d = sn.diff_snapshots(_snap([], ms={"1": 0}), _snap([], ms={"1": "0"}))
        self.assertEqual(d["map_setting"]["changed"], [])
        self.assertFalse(d["map_setting"]["skipped"])

    def test_missing_layer_is_flagged_not_silently_ok(self):
        """一方没采到（旧导出）⇒ 标 skipped，结论里必须写明"未比对"，不能说一致。"""
        d = sn.diff_snapshots(_snap([], ms={"1": "0"}), _snap([]))
        self.assertTrue(d["map_setting"]["skipped"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(d)
        out = buf.getvalue()
        self.assertIn("未比对", out)
        self.assertIn("别把", out)

    def test_print_reports_the_compared_count(self):
        import contextlib
        import io
        d = sn.diff_snapshots(_snap([], ms={"1": "0", "2": "1"}),
                             _snap([], ms={"1": "0", "2": "1"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(d)
        self.assertIn("2 个数字键一致", buf.getvalue())


class TestDiffResidue(unittest.TestCase):
    """删除残骸（`del:1`）不能报成"新增 N 件"：那是假阳性，会教人忽略整份报告。

    2026-10-01 实测：春岚试点删完 11 件后导出，`component_nums` 一件没变，diff 却报
    「新增 11」—— 因为删除后组件仍留在 `_ugc_id2component`（原生 Ctrl+Z 依赖），
    重进才消失。
    """

    def _del(self, g, i, x=0.0, z=0.0):
        c = _comp(g, i, x, z)
        c["del"] = 1
        return c

    def test_residue_is_not_a_component_difference(self):
        a = _snap([_comp(1, 100001, 1.0, 2.0)])
        b = _snap([_comp(1, 100001, 1.0, 2.0), self._del(2, 100001)])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["added"], [])
        self.assertEqual(d["components"]["residue"], {"from": 0, "to": 1})
        self.assertTrue(d["identical"])

    def test_residue_rows_are_not_paired_or_counted_as_content(self):
        """残骸也不许当成"内容件"参与配对 —— 否则会把真件配到幽灵上。"""
        a = _snap([_comp(1, 100001, 1.0, 2.0)])
        b = _snap([self._del(9, 100001, 1.0, 2.0)])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["from"]["components"], 1)
        self.assertEqual(d["to"]["components"], 0)
        self.assertEqual([x["g"] for x in d["components"]["removed"]], [1])

    def test_print_explains_the_residue_count(self):
        import contextlib
        import io
        b = _snap([self._del(2, 100001), self._del(3, 100001)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(sn.diff_snapshots(_snap([]), b))
        self.assertIn("删除残骸", buf.getvalue())
        self.assertIn("重进", buf.getvalue())

    def test_sentinel_added_is_flagged_as_unconfirmed(self):
        """没打 del 标记但停在哨兵位的新增，像未确认态：仍算差异，但要说明怎么判。"""
        parked = _comp(5, 203911, 0.0, 0.0, y=-65536.0)
        d = sn.diff_snapshots(_snap([]), _snap([parked]))
        self.assertEqual(len(d["components"]["added_sentinel"]), 1)
        self.assertFalse(d["identical"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(d)
        self.assertIn("哨兵位", buf.getvalue())

    def test_placed_row_is_not_called_sentinel(self):
        d = sn.diff_snapshots(_snap([]), _snap([_comp(5, 100001, 3.0, 4.0)]))
        self.assertEqual(d["components"]["added_sentinel"], [])


class TestPairingTieBreak(unittest.TestCase):
    """哨兵位逻辑件的配对：坐标全是 (0,-65536,0)，按顺序配 = **任意配**。

    333 个运动器共用一格，重进后 ugc_id 重编号、遍历顺序一变，旧口径就会把
    "只是顺序变了"报成一堆属性变更；真截断反而埋在噪声里说不清是哪一件。
    ⇒ 同距离时优先配内容相同的，剩下的才是真差异。
    """

    def _motion(self, g, pts, extra=None):
        evs = [{"type": 20391119001, "info": [pts]}]
        if extra:
            evs.append({"type": 20391102004, "info": extra})
        return _comp(g, 203911, 0.0, 0.0, y=-65536.0,
                     ci={"component_id": 203911, "events": evs})

    def _sig(self, c):
        return sn._content_sig(c)

    def test_reordered_logical_components_are_not_changes(self):
        x = self._motion(1, [[1.0, 2.0, 3.0, 0.0], [4.0, 5.0, 6.0, 0.0]])
        y = self._motion(2, [[7.0, 8.0, 9.0, 0.0]], extra=[1])
        a = _snap([x, y])
        b_snap = _snap([self._motion(50, [[7.0, 8.0, 9.0, 0.0]], extra=[1]),
                        self._motion(51, [[1.0, 2.0, 3.0, 0.0], [4.0, 5.0, 6.0, 0.0]])])
        d = sn.diff_snapshots(a, b_snap)
        self.assertEqual(d["components"]["changed"], [])
        self.assertEqual(d["components"]["added"], [])
        self.assertTrue(d["identical"])

    def test_truncated_path_is_still_detected(self):
        """放行"顺序不算差异"之后，真丢数据必须照样报出来（宁少报噪声，不能漏真损）。"""
        full = self._motion(1, [[float(i), 2.0, 3.0, 0.0] for i in range(300)])
        trunc = self._motion(1, [[float(i), 2.0, 3.0, 0.0] for i in range(200)])
        other = self._motion(2, [[9.0, 9.0, 9.0, 0.0]])
        d = sn.diff_snapshots(_snap([full, other]), _snap([trunc, other]))
        ch = d["components"]["changed"]
        self.assertEqual(len(ch), 1)
        self.assertIn("20391119001", ch[0]["properties"])

    def test_signature_ignores_property_order(self):
        c1 = self._motion(1, [[1.0, 0.0, 0.0, 0.0]], extra=[1])
        c2 = _comp(2, 203911, 0.0, 0.0, y=-65536.0, ci={
            "component_id": 203911,
            "events": [{"type": 20391102004, "info": [1]},
                       {"type": 20391119001, "info": [[[1.0, 0.0, 0.0, 0.0]]]}]})
        self.assertEqual(self._sig(c1), self._sig(c2))


class TestEventAnchorUnambiguity(unittest.TestCase):
    """事件引用靠"锚点"跨会话比较：锚点撞车 = **漏报**（绑定挪走了还说没事）。

    旧实现只用 `(item_id, x, z)`，而逻辑件的 `p` 恒为哨兵 `[0,-65536,0]` ⇒
    春岚 332 个运动器全折叠成 `203911@0,0`。同一次 diff 里"某个触发绑到了另一件运动器"
    的两个状态会产生**完全相同**的事件表。
    """

    def _ev(self, comps, key="K1", gid=None, prop="10000010001", val="1"):
        return _snap(comps, events={"count": 1, "saved_keys": [],
                                    "events": [{"event_key": key,
                                                "refs": [{"ugc_id": gid if gid is not None
                                                         else comps[0]["g"],
                                                         "property_id": prop,
                                                         "current_value": val}]}]})

    def _motion(self, g, pts):
        return _comp(g, 203911, 0.0, 0.0, y=-65536.0,
                     ci={"component_id": 203911, "events": [
                         {"type": 20391119001, "info": [pts]}]})

    def test_same_cell_different_content_gets_different_anchor(self):
        a, b = self._motion(1, [[1.0, 2.0, 3.0, 0.0]]), self._motion(2, [[9.0, 8.0, 7.0, 0.0]])
        idx = sn._anchor_index([a, b], 0.5)
        self.assertNotEqual(idx[1], idx[2])
        self.assertTrue(idx[1].startswith("203911@0,0#"))

    def test_identical_twins_share_a_label_on_purpose(self):
        """同 item + 同坐标 + 同属性袋的两件**本来就不可区分** ⇒ 共用标签是语义正确。

        给它们编号会在图里多/少一件双胞胎时让别的件标签漂移（2026-10-01 在春岚上
        实测假阳性：7 条完全相同的引用被报成"绑定变更"）。
        """
        a, b = self._motion(1, [[1.0, 2.0, 3.0, 0.0]]), self._motion(2, [[1.0, 2.0, 3.0, 0.0]])
        idx = sn._anchor_index([a, b], 0.5)
        self.assertEqual(idx[1], idx[2])

    def test_label_does_not_depend_on_group_membership(self):
        """锚点只能由组件自身决定：别处多/少一件，不许让已有件的标签变化。"""
        solo = self._motion(14841, [[1.0, 2.0, 3.0, 0.0]])
        twin = self._motion(15003, [[1.0, 2.0, 3.0, 0.0]])
        other = self._motion(14833, [[5.0, 5.0, 5.0, 0.0]])
        self.assertEqual(sn._anchor_index([solo], 0.5)[14841],
                         sn._anchor_index([solo, twin, other], 0.5)[14841])

    def test_every_anchor_carries_the_signature(self):
        idx = sn._anchor_index([self._motion(1, [[1.0, 2.0, 3.0, 0.0]])], 0.5)
        self.assertIn("#", idx[1])
        self.assertTrue(idx[1].startswith("203911@0,0#"))

    def test_binding_moved_between_logical_components_is_reported(self):
        """这是这条检查存在的理由：重绑到另一件运动器必须报"事件变更"。"""
        m1 = self._motion(1, [[1.0, 2.0, 3.0, 0.0]])
        m2 = self._motion(2, [[9.0, 8.0, 7.0, 0.0]])
        before = self._ev([m1, m2], gid=1)
        after = self._ev([self._motion(50, [[1.0, 2.0, 3.0, 0.0]]),
                          self._motion(51, [[9.0, 8.0, 7.0, 0.0]])], gid=51)
        d = sn.diff_snapshots(before, after)
        self.assertEqual(d["components"]["changed"], [])   # 内容都在，只是引用挪了
        self.assertEqual(d["events"]["changed"], ["K1"])
        self.assertFalse(d["identical"])

    def test_same_binding_after_renumbering_is_not_a_change(self):
        a = self._ev([self._motion(1, [[1.0, 2.0, 3.0, 0.0]]),
                      self._motion(2, [[9.0, 8.0, 7.0, 0.0]])], gid=2)
        b = self._ev([self._motion(77, [[9.0, 8.0, 7.0, 0.0]]),
                      self._motion(78, [[1.0, 2.0, 3.0, 0.0]])], gid=77)
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["events"]["changed"], [])
        self.assertTrue(d["identical"])


class TestDuplicateDetection(unittest.TestCase):
    """**同内容重复件**要单独点名：它常常不表现为「新增」。

    2026-10-01 实机：试点"删了重建"在春岚里留下一件与 14841 逐字段相同的多余运动器
    （15003）。配对时它被同内容的件吸走，于是只混在几条属性变更里，看不出来。
    """

    def _m(self, g, pts):
        return _comp(g, 203911, 0.0, 0.0, y=-65536.0,
                     ci={"component_id": 203911, "events": [
                         {"type": 20391119001, "info": [pts]}]})

    def test_stray_twin_is_reported_with_counts(self):
        x = self._m(1, [[1.0, 2.0, 3.0, 0.0]])
        d = sn.diff_snapshots(_snap([x]), _snap([self._m(50, [[1.0, 2.0, 3.0, 0.0]]),
                                                self._m(51, [[1.0, 2.0, 3.0, 0.0]])]))
        extra = d["components"]["duplicate_extra"]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["count"], 2)
        self.assertEqual(extra[0]["before_count"], 1)
        self.assertFalse(d["identical"])

    def test_matching_duplicates_are_not_reported(self):
        """两边都有同样的双胞胎 ⇒ 不是这次还原造成的，不算差异（比的是**件数**）。"""
        pair = [self._m(1, [[1.0, 0.0, 0.0, 0.0]]), self._m(2, [[1.0, 0.0, 0.0, 0.0]])]
        d = sn.diff_snapshots(_snap(pair), _snap(pair))
        self.assertEqual(d["components"]["duplicate_extra"], [])

    def test_residue_is_not_counted_as_a_duplicate(self):
        x = self._m(1, [[1.0, 0.0, 0.0, 0.0]])
        ghost = self._m(2, [[1.0, 0.0, 0.0, 0.0]])
        ghost["del"] = 1
        d = sn.diff_snapshots(_snap([x]), _snap([self._m(50, [[1.0, 0.0, 0.0, 0.0]]), ghost]))
        self.assertEqual(d["components"]["duplicate_extra"], [])

    def test_empty_bag_components_are_not_duplicates(self):
        """空袋的"逐字段相同"只是因为什么都没记 ⇒ 伪信号（春岚实测 52 件这种）。"""
        empty = [_comp(g, 203912, 0.0, 0.0, y=-65536.0,
                       ci={"component_id": 203912, "events": []}) for g in (5, 6, 7)]
        self.assertEqual(sn.duplicate_groups(empty), [])
        self.assertEqual(sn.content_sig_counts(empty), {})

    def test_positioned_twins_are_not_duplicates(self):
        """方块共用同一个属性袋是常态（春岚 14318 件同一个袋）—— 它们靠坐标区分。

        把它们算进"重复"会让报告印出成万条噪声，反而埋掉真信号（多出来的运动器）。
        """
        bag = {"component_id": 100001, "events": [{"type": 10000110001, "info": ["FFFFFF"]}]}
        a = _snap([_comp(1, 301001, 1.0, 2.0, ci=bag), _comp(2, 301001, 30.0, 40.0, ci=bag)])
        b = _snap([_comp(1, 301001, 1.0, 2.0, ci=bag), _comp(2, 301001, 30.0, 40.0, ci=bag),
                   _comp(3, 301001, 60.0, 70.0, ci=bag)])
        d = sn.diff_snapshots(a, b)
        self.assertEqual(d["components"]["duplicate_extra"], [])
        self.assertEqual(len(d["components"]["added"]), 1)   # 第三件仍按新增报，没丢信号

    def test_print_names_the_group(self):
        import contextlib
        import io
        x = self._m(1, [[1.0, 2.0, 3.0, 0.0]])
        d = sn.diff_snapshots(_snap([x]), _snap([self._m(50, [[1.0, 2.0, 3.0, 0.0]]),
                                                 self._m(51, [[1.0, 2.0, 3.0, 0.0]])]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sn.print_diff(d)
        out = buf.getvalue()
        self.assertIn("同内容重复", out)
        self.assertIn("50,51", out)


class TestSummarize(unittest.TestCase):
    def test_warns_on_pending_delete_bag(self):
        s = _snap([_comp(1, 100001, 0, 0)])
        s["budget"] = {"occupancy": 553, "del_unit_uids": {"component": [167, 168]}}
        out = sn.summarize(s)
        self.assertTrue(out["warnings"], msg="有待删除袋时必须告警")
        self.assertIn("待删除", out["warnings"][0])

    def test_no_warning_when_clean(self):
        s = _snap([_comp(1, 100001, 0, 0)])
        s["budget"] = {"occupancy": 562, "del_unit_uids": {}}
        self.assertEqual(sn.summarize(s)["warnings"], [])


if __name__ == "__main__":
    unittest.main()
