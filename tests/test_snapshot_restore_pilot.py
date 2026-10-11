#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_snapshot_restore_pilot — 试点选择 / 计划预算 / 断点续做 三处纯逻辑锁。

跨账号复刻的实际流程是「先在目标空图上摆几件试路，再跑全量」，这三块都是那条
流程上新出现的失效面：
  * 只有 --limit（取快照前 N 件，春岚前 14318 件几乎全是方块）根本没法试点；
  * 预算按源图整图读数算，在"跳过一批 + 放开一批"的顶格图上两头错；
  * 进度按 index 判定续做，试点与全量的清单顺序不同 ⇒ 会把没摆的当成摆过。
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot_restore as sr  # noqa: E402


def _fake_unit(pos3, pos=None, cls_name="UgcLogicUnit"):
    """类名可指定的假单元：回读代码按 `type(unit).__name__` 分流全局组件。"""
    unit = type(cls_name, (object,), {})()
    unit.uid = 1
    unit.get_position3 = lambda: NS(x=pos3[0], y=pos3[1], z=pos3[2])
    p = pos if pos is not None else pos3
    unit.position = NS(x=p[0], y=p[1], z=p[2])
    return unit


def _run_generated(code, comps):
    """把 landing_audit 真正下发的 eval 代码在本地跑一遍（不碰游戏进程）。"""
    ns = {"cm": NS(_ugc_id2component=comps), "__result__": None}
    exec(compile(code, "<landing_audit>", "exec"), ns, ns)
    return ns["__result__"]


def _depth(node):
    """容器嵌套深度：回传层 `_safe(depth>4)` 把 >4 层的值压成 "<deep>"。"""
    if isinstance(node, dict):
        return 1 + max([_depth(v) for v in node.values()] or [0])
    if isinstance(node, (list, tuple)):
        return 1 + max([_depth(v) for v in node] or [0])
    return 0


def _census_comp(gid, cid, uid, p=(0.0, 0.0, 0.0)):
    unit = NS(uid=uid, ugc_type=cid)
    unit.get_position3 = lambda: NS(x=p[0], y=p[1], z=p[2])
    return NS(_ugc_id=gid, _owner=unit, _owner_uid=uid,
              component_info={'component_id': cid, 'events': []})


def _run_type_census(cm, room, types):
    """把 `CODE_TYPE_CENSUS` 真正下发的代码在本地跑一遍（不碰游戏进程）。"""
    from snapshot import CODE_TYPE_CENSUS, _bridge_dir
    code = (CODE_TYPE_CENSUS.replace('%BRIDGE%', repr(_bridge_dir()))
            .replace('%TYPES%', repr(list(types))))
    ns = {'cm': cm, 'room': room, '__result__': None}
    exec(compile(code, '<type_census>', 'exec'), ns, ns)
    return ns['__result__']


class TestTypeCensusSkipsPendingDeletions(unittest.TestCase):
    """待保存提交的删除**不许**被当成"目标图已经有这一件"。

    2026-10-02 实机：清理现场后 127 件实验件还挂在待删除袋里（本地索引要等重进
    才清），而这份普查喂的正是"已有同内容同位置就扣掉、不再摆"的判据。不排掉的话，
    计划里那几件会被扣掉 ⇒ 交付的图**少内容**，而且引用会绑到那个待删幽灵的号上。
    （与 `CODE_COMPONENTS` 的 `del` 标记、`classify_components` 的 deleted_residue 同一条口径。）
    """

    def test_deleted_components_and_deleted_units_are_excluded(self):
        comps = {1: _census_comp(1, 101001, 1001),
                 2: _census_comp(2, 101001, 1002),   # 宿主单元已待删
                 3: _census_comp(3, 101001, 1003)}   # 组件本身已待删
        cm = NS(_ugc_id2component=comps)
        room = NS(del_unit_uids={'unit': [1002], 'component': [3]})
        res = _run_type_census(cm, room, [101001])
        self.assertEqual(res['n'], 1)
        self.assertEqual([r['g'] for r in res['rows']], [1])

    def test_live_rows_keep_content_and_transform(self):
        cm = NS(_ugc_id2component={7: _census_comp(7, 203912, 900,
                                                   p=(1.0, 2.0, 3.0))})
        res = _run_type_census(cm, NS(del_unit_uids={}), [203912])
        self.assertEqual(res['n'], 1)
        self.assertEqual(res['rows'][0]['p'], [1.0, 2.0, 3.0])
        self.assertEqual(res['rows'][0]['i'], 203912)
        self.assertIn('ci', res['rows'][0])

    def test_types_filter_still_applies(self):
        cm = NS(_ugc_id2component={1: _census_comp(1, 101001, 1001),
                                   2: _census_comp(2, 200001, 1002)})
        res = _run_type_census(cm, NS(del_unit_uids={}), [101001])
        self.assertEqual([r['g'] for r in res['rows']], [1])


class TestBudgetExcludesPendingDeletions(unittest.TestCase):
    """预算余量必须扣掉"已登记但待保存删除"的残影。

    2026-10-02 实机：清理现场后本地 `component_nums` 是 5585（5685 件挂着待删标记、
    要等重进才从索引消失），服务端真值只剩出厂 103 ⇒ 续做的"预算复核"以
    5585+9286>15000 拒写（这条闸门不受 --yes 放行）。待删的不是地图内容，
    与 `CODE_COMPONENTS` 的 `del`、`classify_components` 的 deleted_residue 同口径。
    """

    def test_budget_code_reports_a_live_counter(self):
        from snapshot import CODE_BUDGET
        self.assertIn('component_nums_live', CODE_BUDGET)
        self.assertIn('pending_deletion_registered', CODE_BUDGET)
        self.assertIn('_del_comp_norm', CODE_BUDGET,
                      '桶里的值可能是 int 也可能是数字字符串，两边都要收进判据')

    def test_target_info_prefers_the_live_counter(self):
        import inspect
        src = inspect.getsource(sr.fetch_target_info)
        self.assertIn('component_nums_live', src)
        self.assertIn('component_nums_raw', src, '原值要留着给人看，不许静默替换')


def _comp(g, item, cid, p=(0.0, 0.0, 0.0), events=None):
    return {"g": g, "i": item, "p": list(p), "r": [0, 0, 0], "s": [1, 1, 1],
            "ci": json.dumps({"component_id": cid, "events": events or []})}


class TestLandingIsReadBack(unittest.TestCase):
    """摆放后必须回读落点：`placed` 回执只证明这件存在，不证明它落在请求坐标上。

    2026-10-01 实机：14869 件里一件被引擎放到 (1163.4, 2.0, -342.3)，请求的是
    (923.74, 60.0, -401.56) —— 差 248 单位、还低 58；回执正常、件数对得上、
    当时比对也不看 `p`，最后是人在试玩里看出「落脚平台更低」才发现。
    """

    def _specs(self):
        return [{"old_g": 11, "position": [923.7, 60.0, -401.6]},
                {"old_g": 12, "position": [1.0, 2.0, 3.0]}]

    def test_reports_only_the_off_items(self):
        seen = []

        def fake(op, args, timeout=90):
            seen.append(args["code"])
            return {"ok": True, "value": {
                "bad": [{"ugc_id": 7512, "want": [923.7, 60.0, -401.6],
                         "is": [[1163.4, 2.0, -342.3]]}], "miss": 1}}
        old = sr.call_edit
        try:
            sr.call_edit = fake
            r = sr.landing_audit(self._specs(), {11: 7512, 12: 7513})
        finally:
            sr.call_edit = old
        self.assertEqual(r["checked"], 2)
        self.assertEqual([x["ugc_id"] for x in r["off"]], [7512])
        self.assertEqual(r["unreadable"], 1, msg="读不到的件要单独计数，不许混进「都对」")
        self.assertIn("_tol", seen[0])
        self.assertIn("923.7", seen[0],
                      msg="请求坐标要发给桥逐号点验（回传被截到 200 条，拉整表回来自己比会得到假的「全都不对」）")

    def test_is_wired_after_placing_and_before_props(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("landing_audit(", src)
        self.assertLess(src.index("摆放完成：成功"), src.index("landing_audit("),
                        msg="落点回读要等 id 映射齐了才能做")
        self.assertLess(src.index("landing_audit("), src.index("阶段 2：属性回写"),
                        msg="摆错地方的件要在回写属性前说出来，别在半张错图上继续写")

    def test_unreadable_batch_is_not_silently_green(self):
        import inspect
        src = inspect.getsource(sr.landing_audit)
        self.assertIn("不许当成「都对」", src,
                      msg="这一批没做成要说出来；不能因为没收到差异就报「都对」")

    # ---- 2026-10-02：这道审计自己坏过，先修它才好拿它核位 ----
    def _captured_code(self, extra=None):
        seen = []

        def fake(op, args, timeout=90):
            seen.append(args["code"])
            res = {"bad": [], "miss": 0}
            res.update(extra or {})
            return {"ok": True, "value": res}
        old = sr.call_edit
        try:
            sr.call_edit = fake
            r = sr.landing_audit(self._specs(), {11: 7512, 12: 7513})
        finally:
            sr.call_edit = old
        return seen[0], r

    def test_audit_payload_survives_the_response_depth_limit(self):
        """实机：回的 `is` 写成 `[[x,y,z],[x,y,z]]` ⇒ 套进 `{"bad":[{...}]}` 正好 5 层，
        回传层的 `_safe(depth>4)` 把它**整片压成 `<deep>`** —— 审计报了"11/15 件错位"
        却一件实际坐标都读不出来。现在只回差距最小的那一个扁平三元组。
        """
        code, _r = self._captured_code()
        res = _run_generated(code, {
            7512: NS(_owner=_fake_unit((1163.4, 2.0, -342.3),
                                       (0.0, -65536.0, 0.0)))})
        self.assertEqual(res["miss"], 1, "7513 查不到组件 ⇒ 读不到，单独计数")
        item = res["bad"][0]
        self.assertEqual(item["ugc_id"], 7512)
        self.assertEqual(len(item["is"]), 3, "必须回扁平三元组")
        self.assertTrue(all(isinstance(v, float) for v in item["is"]))
        self.assertGreater(item["delta"], 0)
        self.assertIn("alt", item)
        # 深度算术：res(1)+bad 列表(2)+条目 dict(3)+is 列表(4)+float(0) = 4
        # 旧形态多一层内列表 ⇒ 5 ⇒ 被压成 <deep>
        self.assertLessEqual(_depth(res), 4,
                             "回传层只到 4 层，超了这一步的结论就是 <deep>")

    def test_global_unit_owners_are_exempt_and_counted(self):
        """`UgcGlobalUnit`（运动器/音乐事件…）的位置真值在属性袋、不在单元上：
        拿请求坐标比单元的隐藏哨兵是假警报（2026-10-01 那次 333 件里 332 件就是它）。
        这类**单独计数并显式报出来**，不当"摆错地方"、也不静默吞掉。
        """
        code, r = self._captured_code(extra={"exempt_global": 2})
        self.assertEqual(r["exempt_global"], 2)
        res = _run_generated(code, {
            7512: NS(_owner=_fake_unit((0.0, -65536.0, 0.0),
                                       cls_name="UgcGlobalUnit")),
            7513: NS(_owner=_fake_unit((1.0, 2.0, 3.0)))})
        self.assertEqual(res["bad"], [], "全局单元上的组件不许报成错位")
        self.assertEqual(res["exempt_global"], 1)


class TestStoryRefsAreRemappedOnWrite(unittest.TestCase):
    """写袋时的换号集合必须与比对归一**同一张表**（含剧情节点引用那一族）。"""

    def test_apply_props_sends_the_story_slots_for_remap(self):
        seen = {}
        old = sr.call_edit

        def fake(op, args, timeout=90):
            seen["op"] = op
            seen.update(args)
            return {"ok": True}
        try:
            sr.call_edit = fake
            comp = {"g": 119, "i": 10007002, "ci": json.dumps(
                {"component_id": 10007002,
                 "events": [{"type": 1000700240001, "info": [118]}]})}
            r = sr.apply_props(500, comp, {118: 900})
        finally:
            sr.call_edit = old
        self.assertTrue(r.get("ok"))
        self.assertEqual(seen["op"], "set_component_info")
        props = [str(x) for x in seen["remap_properties"]]
        self.assertIn("1000700240001", props,
                      msg="剧情节点引用没交给桥接换号 ⇒ 旧号原样写下去，接到别的件上")
        self.assertIn("20390003001", props, msg="成员表那一族不许掉")
        self.assertEqual(seen["remap"], {"118": 900})


class TestPendingDeletionGuard(unittest.TestCase):
    """写前必须把**别人/上一批未提交的删除**摆到 yes 之前：保存会把它们一起提交。

    2026-10-01 实机：春岚的待删除袋里躺着一件有名字的运动器
    （`14982 cid=203911「春嵐_上映_chorus-split-15」`），而旧预检只印
    `{'unit': 7, 'component': 4}` —— 数得出几件、认不出是哪件，等发现时已经晚了
    （退出编辑器也会自动保存）。
    """

    def _budget(self, **kw):
        b = {"del_unit_uids": {"unit": [1014623], "component": [14982]}}
        b.update(kw)
        return b

    def test_note_names_the_items_when_identities_available(self):
        note = sr.pending_deletion_note(self._budget(
            pending_deletions=[{"bucket": "component", "id": 14982, "gid": 14982,
                                "cid": 203911, "name": "春嵐_上映_chorus-split-15"}],
            pending_deletions_total=1))
        self.assertIn("14982", note)
        self.assertIn("春嵐_上映_chorus-split-15", note)
        self.assertIn("自动保存", note)

    def test_note_still_warns_without_identities(self):
        """旧桥接没这个字段时不能变成"没提示"：退回桶计数 + 同样的后果说明。"""
        note = sr.pending_deletion_note(self._budget())
        self.assertIn("component", note)
        self.assertIn("待删除袋", note)
        self.assertIn("自动保存", note)

    def test_wired_before_the_yes_prompt(self):
        """只实现函数不接到写前确认 = 照样能一路 yes 把删除提交掉。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("pending_deletion_note(", src)
        self.assertLess(src.index("pending_deletion_note(tgt[\"budget\"])"),
                        src.index('input("继续？输入 yes'))


class TestMapSettingSync(unittest.TestCase):
    """地图级**玩法设置**：几何 1:1 但设置不同 ⇒ 长得一样、玩起来不一样。

    春岚实测有 4 个键与注册表默认值不同（key 1=0/默认1、10=1/默认0、
    22=0/默认1、20=`mode,1;type,0;error,0;camp_key,-1,`/默认0），
    而旧的跨图预检只对齐 key 14（框架）⇒ 空白目标图即使件件对上，
    玩法行为仍与源图不同。这里锁三层：身份键不碰、差异要报、写入要能落。
    """

    SNAP = {"1": "0", "2": "1", "14": "4002", "20": "mode,1;type,0;error,0;camp_key,-1,",
            "22": "0", "name": "春岚", "desc": "别抄我简介", "tags": ["悬疑"],
            "sub_tags": [], "topics": ["x"]}
    BLANK = {"1": "1", "2": "1", "14": "4002", "20": "0", "22": "1",
             "name": "新地图", "desc": "", "tags": [], "sub_tags": [], "topics": []}

    def test_identity_keys_never_in_sync_set(self):
        self.assertEqual(sr.setting_numeric_keys(self.SNAP),
                         ["1", "2", "14", "20", "22"])
        for k in ("name", "desc", "tags", "sub_tags", "topics"):
            self.assertNotIn(k, sr.setting_numeric_keys(self.SNAP))

    def test_diff_reports_the_four_but_not_framework(self):
        d = sr.map_setting_diff(self.SNAP, self.BLANK)
        self.assertEqual([r["key"] for r in d["diffs"]], ["1", "20", "22"])
        self.assertEqual(d["level"], "warn")
        # key 14 由 --set-framework 负责（要重进才生效），不在这里重复写
        self.assertNotIn("14", [r["key"] for r in d["rows"]])

    def test_diff_ignores_map_identity_values(self):
        """名字/简介不同**不是**差异 —— 跨图复刻本就不该覆盖目标图的身份。"""
        d = sr.map_setting_diff({"name": "春岚"}, {"name": "新地图", "1": "0"})
        self.assertEqual(d["diffs"], [])

    def test_diff_normalizes_int_vs_str(self):
        d = sr.map_setting_diff({"1": 0}, {"1": "0"})
        self.assertEqual(d["diffs"], [])
        self.assertEqual(d["level"], "ok")

    def test_missing_target_is_unknown_not_ok(self):
        """读不到目标设置时不能说"一致"，也不能盲写。"""
        for bad in (None, {}, "x"):
            d = sr.map_setting_diff(self.SNAP, bad)
            self.assertEqual(d["level"], "unknown")
            self.assertTrue(d["unknown"])
        r = sr.sync_map_settings(self.SNAP, None)
        self.assertTrue(r["skipped"])

    def test_camp_ref_value_is_the_next_token(self):
        """`camp_key,-1,` 里键和值是**逗号分开的相邻两段**，解析成 `camp_key=-1` 会读不到。"""
        self.assertEqual(sr._camp_ref_value("mode,1;type,0;error,0;camp_key,-1,"), "-1")
        self.assertEqual(sr._camp_ref_value("mode,1;camp_key,2,"), "2")
        self.assertIsNone(sr._camp_ref_value("mode,1;type,0"))

    def test_camp_ref_to_specific_team_is_refused(self):
        """指向具体队伍（camp_key≠-1）时宁可不写：目标图队名命名空间不同会绑错队。"""
        snap = {"20": "mode,1;camp_key,2,"}
        tgt = {"20": "0"}
        with patch.object(sr, "call_edit") as m:
            r = sr.sync_map_settings(snap, tgt)
        self.assertEqual([x["key"] for x in r["refused"]], ["20"])
        self.assertEqual(r["applied"], [])
        m.assert_not_called()

    def test_dry_run_writes_nothing(self):
        with patch.object(sr, "call_edit") as m:
            r = sr.sync_map_settings(self.SNAP, self.BLANK, dry=True)
        self.assertEqual([x["key"] for x in r["applied"]], ["1", "20", "22"])
        m.assert_not_called()

    def test_apply_sends_one_write_per_differing_key(self):
        with patch.object(sr, "call_edit",
                          return_value={"ok": True, "changed": True}) as m:
            r = sr.sync_map_settings(self.SNAP, self.BLANK)
        self.assertEqual(m.call_count, 3)
        sent = [(c[0][1]["key"], c[0][1]["value"]) for c in m.call_args_list]
        self.assertIn(("1", "0"), sent)
        self.assertIn(("20", "mode,1;type,0;error,0;camp_key,-1,"), sent)
        self.assertEqual(len(r["applied"]), 3)
        self.assertEqual(r["failed"], [])

    def test_failure_carries_the_native_readback(self):
        """op 的 ok 已含"写完回读要相等"⇒ 失败必须带出回读值，否则两种故障分不开。"""
        with patch.object(sr, "call_edit",
                          return_value={"value": {"ok": False, "after": "1",
                                                  "error": "原生返回 False"}}) as m:
            r = sr.sync_map_settings({"1": "0"}, {"1": "1"})
        self.assertEqual(r["applied"], [])
        f = r["failed"][0]
        self.assertEqual(f["native_after"], "1")
        self.assertIn("原生返回 False", f["error"])

    def test_wired_into_the_cli(self):
        """实现了但没接上开关 = 交付时照样漏（预检/写入/回读三条路都要在）。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn('"--sync-settings"', src)
        self.assertIn("map_setting_diff(", src)        # 预检要报差异
        self.assertIn("sync_map_settings(", src)       # --apply 要真写
        # 重进后要能判落没落库，而且结论要进 exit code（只印一行不算接上）
        self.assertIn("settings_verify(", src)
        self.assertIn('sv["level"] != "ok"', src)

    def test_verify_lines_flag_what_did_not_persist(self):
        """写进去 ≠ 存得下：重进回读后差异又回来了，就是没落住。"""
        lines = sr.settings_verify_lines({"1": "0", "2": "1"}, {"1": "1", "2": "1"})
        self.assertIn("warn", lines[0])
        self.assertTrue(any("key 1" in l for l in lines))
        self.assertIn("重进后回读", lines[0])

    def test_verify_lines_ok_when_identical(self):
        lines = sr.settings_verify_lines({"1": "0"}, {"1": "0"})
        self.assertEqual(len(lines), 1)
        self.assertIn("ok", lines[0])

    def test_verify_lines_unknown_without_target(self):
        self.assertIn("unknown", sr.settings_verify_lines({"1": "0"}, None)[0])

    def test_settings_verify_carries_a_level(self):
        """光有输出行不够：`--verify-only` 的结论要能拿到这一层的判定。"""
        self.assertEqual(sr.settings_verify({"1": "0", "2": "1"},
                                            {"1": "1", "2": "1"})["level"], "warn")
        self.assertEqual(sr.settings_verify({"1": "0"}, {"1": "0"})["level"], "ok")
        self.assertEqual(sr.settings_verify({"1": "0"}, None)["level"], "unknown")


class TestVerifyOnlyExitCode(unittest.TestCase):
    """`--verify-only` 的 exit code 一直是"事件引用"的结论 ⇒ 设置层没落住也 exit 0，
    而文档写的是"重进后 `--verify-only` 的 `设置` 行必须是 [ok]"、"exit code 即结论"。
    两条说法对不上时，收口的人会把"没查成"当"查过了"。
    """

    def _run(self, snap_ms, live_ms, events_ok=True, props="same"):
        import contextlib
        import io
        # 地图属性层（默认背景音乐等）：--verify-only 现在也判这一层，
        # 所以桩里要给出"源/目标两侧"的读数；props=None 模拟旧导出没采这一层。
        layer = {"ugc_bgm_conf": {"present": True, "value": None},
                 "rank_setting": {"present": True, "value": ["10", "1000", ""]},
                 "player_num": {"present": True, "value": 4},
                 "map_edit_data": {"present": True, "value": {}},
                 "other_photos": {"present": True, "value": []},
                 "ugc_top_view_photo": {"present": True, "value": None}}
        snap_props = None if props is None else layer
        if props == "diff":
            target_layer = dict(layer, ugc_bgm_conf={"present": True,
                                                     "value": {"mode": 3,
                                                                "param": {"id": "X"}}})
        else:
            target_layer = layer

        def _fake_fetch_props(log=print):
            return target_layer

        mp = {"name": "m", "framework_id": "4002", "map_setting": snap_ms}
        if snap_props is not None:
            mp["props"] = snap_props

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"format": "fuxiang-snapshot", "version": 1,
                           "map": mp,
                           "components": [], "events": {"events": []},
                           "story": {"stories": []}, "budget": {}}, f)
            with patch.object(sr, "idmap_path", lambda *x, **k: os.path.join(d, "none.json")), \
                    patch.object(sr, "fetch_story_tree", lambda *x, **k: {}), \
                    patch.object(sr, "story_map_from_tree", lambda *x, **k: ({}, [])), \
                    patch.object(sr, "verify_event_refs",
                                 lambda *x, **k: {"ok": events_ok, "total": 0,
                                                  "matched": 0, "ghost": [],
                                                  "missing": [], "rows": []}), \
                    patch.object(sr, "_print_verify", lambda *x, **k: None), \
                    patch.object(sr, "fetch_live_index", lambda *x, **k: ({}, 0)), \
                    patch("snapshot.fetch_props", _fake_fetch_props), \
                    patch.object(sr, "fetch_target_info",
                                 lambda *x, **k: {"map_setting": live_ms}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sr.main([path, "--verify-only"])
            return rc, buf.getvalue()

    def test_settings_not_persisted_fails_the_command(self):
        rc, out = self._run({"1": "0", "2": "1"}, {"1": "1", "2": "1"})
        self.assertEqual(rc, 1, msg=out)
        self.assertIn("设置层", out)

    def test_settings_persisted_passes(self):
        rc, out = self._run({"1": "0", "2": "1"}, {"1": "0", "2": "1"})
        self.assertEqual(rc, 0, msg=out)
        self.assertIn("设置", out)

    def test_map_props_difference_fails_the_command(self):
        """默认背景音乐这类地图级设置没对上 ⇒ 压住 exit code。

        2026-10-01：春岚2 七层全绿却在试玩里"开局没有音乐"——这一层当时根本不采。
        """
        rc, out = self._run({"1": "0", "2": "1"}, {"1": "0", "2": "1"}, props="diff")
        self.assertEqual(rc, 1, msg=out)
        self.assertIn("ugc_bgm_conf", out)

    def test_old_export_without_props_layer_is_unknown(self):
        rc, out = self._run({"1": "0", "2": "1"}, {"1": "0", "2": "1"}, props=None)
        self.assertEqual(rc, 1, msg=out)
        self.assertIn("没采", out)

    def test_missing_target_reading_is_not_a_pass(self):
        """读不到目标图设置 ⇒ 这一层**没比成**，不许 exit 0（与 accept 的 unknown 同口径）。"""
        rc, out = self._run({"1": "0"}, None)
        self.assertEqual(rc, 1, msg=out)

    def test_event_verdict_still_decides_when_settings_agree(self):
        rc, out = self._run({"1": "0"}, {"1": "0"}, events_ok=False)
        self.assertEqual(rc, 1, msg=out)


class TestPropsLayerSaveNeedsAChangeSet(unittest.TestCase):
    """地图属性层的落库要靠**非空变更集**（2026-10-01 小图 2026.9.16 实机）。

    同一份 ugc_bgm_conf 写进 props 后连着两次空变更集保存都没落库（重进后仍 null）；
    第三次先做一次同值 `edit_transform` 登记一条变更再保存 ⇒ 重进后读回标记值。
    这条判据不能写反：没有它，--set-bgm 会"回执一切正常"而图里什么都没变。
    """

    def _run(self, dirty_len, targets, transform_ok=True):
        calls = []

        def fake(op, args, timeout=90):
            calls.append((op, args))
            if op == "save_dirty":
                return {"change_units_length": dirty_len, "save_events_length": 0}
            if op == "edit_targets":
                return {"targets": targets}
            if op == "edit_transform":
                return {"ok": transform_ok}
            return {"ok": True, "change_units_length": 0, "save_events_length": 0,
                    "submitted": True}
        import snapshot_restore as sr
        old = sr.call_edit
        sr.call_edit = fake
        try:
            return sr.save_props_layer("tok", log=lambda *a: None), calls
        finally:
            sr.call_edit = old

    def _t(self, gid=500, ct=100001, pos=(1.0, 2.0, 3.0)):
        return {"ugc_id": gid, "component_type": ct, "editable": True,
                "transform": {"position": list(pos), "rotation": [0, 0, 0], "scale": [1, 1, 1]}}

    def test_empty_change_set_gets_a_same_value_transform(self):
        res, calls = self._run(0, [self._t()])
        tr = [c for c in calls if c[0] == "edit_transform"]
        self.assertEqual(len(tr), 1, calls)
        self.assertEqual(tr[0][1]["position"], [1.0, 2.0, 3.0])   # 原值回写，不动地图
        self.assertEqual(res["dirtied_via"]["ugc_id"], 500)

    def test_nonempty_change_set_is_left_alone(self):
        res, calls = self._run(3, [self._t()])
        self.assertEqual([c for c in calls if c[0] == "edit_transform"], [])
        self.assertIsNone(res["dirtied_via"])

    def test_unreadable_dirty_reading_counts_as_empty(self):
        """save_dirty 读不到（None）不能当"有变更集"—— 那样又会静默空保存一次。"""
        res, calls = self._run(None, [self._t()])
        self.assertEqual(len([c for c in calls if c[0] == "edit_transform"]), 1)

    def test_widget_and_framework_types_are_not_the_first_pick(self):
        """登记变更要挑普通件；控件/运动点/锚点不是首选（同值回写也别碰框架件）。"""
        res, calls = self._run(0, [self._t(gid=100, ct=200001), self._t(gid=10, ct=203912),
                                   self._t(gid=777, ct=100001)])
        tr = [c for c in calls if c[0] == "edit_transform"]
        self.assertEqual(tr[0][1]["ugc_id"], 777, calls)

    def test_no_pickable_component_is_reported_not_faked(self):
        res, calls = self._run(0, [])
        self.assertEqual([c for c in calls if c[0] == "edit_transform"], [])
        self.assertIsNone(res["dirtied_via"])


class TestBagRefDangling(unittest.TestCase):
    """属性袋里的组件 id 引用，指向"本次不重建"的件时必须**写之前就报**。

    写袋时只换 id_map 里的旧号；引用落在跳过名单里 ⇒ 那个数字原样落到目标图，
    而目标图上同号的是**另一个组件** ⇒ 静默接错线。事件层早就有悬空预测，袋层以前没有。
    春岚实机：默认全量计划悬空 0 处；但 `--only-types 203900` 这种试点筛法会造出
    14505 处（组合件成员表 `20390003001` 指着一整片没摆的几何方块）。
    """

    def _snap(self, with_member=True, skipped_geo=True):
        geo = {"g": 5, "i": 100001, "p": [1.0, 0.0, 2.0], "r": [0, 0, 0], "s": [1, 1, 1],
               "ci": json.dumps({"component_id": 100001, "events": []})}
        ev = []
        if with_member:
            ev.append({"type": "20390018701", "info": [[5]]})
            ev.append({"type": "20390003001", "info": [[5, 6]]})
        comp = {"g": 9, "i": 203900, "p": [3.0, 0.0, 4.0], "r": [0, 0, 0], "s": [1, 1, 1],
                "ci": json.dumps({"component_id": 203900, "events": ev})}
        comps = [comp] + ([geo] if skipped_geo else [])
        return {"components": comps}

    def test_reference_to_unplaced_component_is_dangling(self):
        snap = self._snap()
        specs = [{"old_g": 9, "component_type": 203900}]     # 几何件 5 不在计划里
        d = sr.bag_ref_dangling(snap, specs)
        self.assertEqual(sorted(x["ref"] for x in d), [5, 5, 6], msg=d)
        self.assertTrue(all(x["owner"] == 9 for x in d))
        self.assertIn("20390018701", {x["property_id"] for x in d})

    def test_group_members_count_as_placed(self):
        snap = self._snap()
        specs = [{"old_g": 9, "group_old_gs": [5]}, {"old_g": 6}]
        self.assertEqual(sr.bag_ref_dangling(snap, specs), [])

    def test_deleted_residue_rows_are_not_owners(self):
        snap = self._snap()
        snap["components"].append(dict(snap["components"][0], g=70, **{"del": 1}))
        d = sr.bag_ref_dangling(snap, [{"old_g": 9}])
        self.assertEqual(sorted({x["owner"] for x in d}), [9])

    def test_note_groups_by_skip_reason_and_names_remedies(self):
        snap = self._snap()
        plan = {"skipped": [{"g": 5, "reason": "ui（UI_NATIVE 通道开放但落库未证）"},
                            {"g": 6, "reason": "界面"}]}
        specs = [{"old_g": 9}]
        note = sr.bag_ref_dangling_note(sr.bag_ref_dangling(snap, specs),
                                        sr._skip_reason_index(snap, plan, specs))
        self.assertIn("静默接错线", note)
        self.assertIn("--include-ui", note)
        self.assertIn("--allow-dangling-refs", note)
        self.assertIn("--only-*", note, msg="试点筛法造的悬空要说清是哪一类原因")

    def test_mid_run_partial_idmap_is_the_same_class_of_bug(self):
        """计划里有、但**实际没摆上**（摆放失败/早停）⇒ id_map 里没它，
        依赖它的袋值会换不到号。属性回写前必须再问一次，不能只在写之前问。"""
        snap = self._snap()
        full = [{"old_g": 9}, {"old_g": 5}, {"old_g": 6}]
        self.assertEqual(sr.bag_ref_dangling(snap, full), [],
                         msg="三件都在计划里时不该报悬空")
        partial = [{"old_g": 9}]          # 几何件 5/6 这次没摆上（id_map 里没它们）
        d = sr.bag_ref_dangling(snap, partial)
        self.assertEqual(sorted(x["ref"] for x in d), [5, 5, 6], msg=d)

    def test_stage2_asks_before_writing(self):
        """属性回写前也要问一遍（计划里有 ≠ 真摆上了），且要排在第一次 apply_props 之前。"""
        import inspect
        src = inspect.getsource(sr.main)
        pre = src.index("阶段 2：属性回写")
        post = src.index("本次没摆上", pre)
        self.assertLess(post, src.index("apply_props(new_gid", pre),
                        msg="报在写之后就没有「有机会停在写之前」的意义了")

    def test_wired_before_the_write_and_flagged(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("bag_ref_dangling(", src)
        self.assertIn("a.allow_dangling_refs", src)
        self.assertIn('"--allow-dangling-refs"', src)
        # 闸门要排在"交互式 yes 提示"之前（`a.yes` 在预算判断里也会出现，不能拿它当锚点）
        prompt = src.index("即将向**当前打开的地图**写入")
        self.assertLess(src.index("a.allow_dangling_refs"), prompt,
                        msg="先拒写再问 yes：反过来等于让人先签字")


class TestSingletonRefsAreDeclared(unittest.TestCase):
    """全局单例同步**没有 old→new 映射可用**：袋里若存着组件 id，就是原样写下去。

    春岚实测 8 处配对 0 处带这种字段，所以只报不拦。换一张图就不一定了 ⇒
    这条必须出现在预检输出里，而不是等人重进后去猜。
    """

    def _snap(self, with_ref=True):
        events = [{"type": "1000400113101", "info": [{"bron": []}]}]
        if with_ref:
            events.append({"type": "1000400118701", "info": [[5]]})
        return {"components": [
            _comp(5, 100001, 100001, p=(1.0, 0.0, 2.0)),
            {"g": 9, "i": 10004001, "p": [0.0, 0.0, 0.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 10004001, "events": events})}]}

    def _run(self, snap):
        with patch.object(sr, "fetch_target_singletons", lambda: [
                {"component_type": 10004001, "ugc_id": 77}]), \
                patch.object(sr, "call_edit", lambda *a, **k: {"ok": True}):
            return sr.sync_global_singletons(snap, dry=True)

    def test_ref_bearing_singleton_is_reported(self):
        r = self._run(self._snap())
        hits = r["refs_needing_remap"]
        self.assertEqual(len(hits), 1, msg=r)
        self.assertEqual(hits[0]["property_id"], "1000400118701")
        self.assertEqual(hits[0]["component_type"], 10004001)

    def test_ordinary_singleton_bag_is_not_flagged(self):
        self.assertEqual(self._run(self._snap(with_ref=False))["refs_needing_remap"], [])

    def test_both_print_paths_show_the_warning(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("原样写到目标图", src)       # 预检（dry-run）那条
        self.assertIn("按原样落库", src)           # --apply 那条
        self.assertLess(src.index("原样写到目标图"), src.index("即将向**当前打开的地图**写入"),
                        msg="警告要在人签字之前出现")


class TestStageRequirementsGate(unittest.TestCase):
    """少带一个开关 ⇒ 跑完几十分钟才发现"少一整层"。这件事要在**动手之前**被说出来。

    手册里那串 B 账号命令是带齐开关的，但手打一漏（最常见是忘了 `--with-story`
    或 `--sync-settings`）以前没有任何东西会拦 —— 预检只报预算与框架。
    """

    def _snap(self):
        return {"components": [
            {"g": 1, "i": 301001, "p": [1.0, 0.0, 2.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 100001, "events": []})},
            {"g": 2, "i": 6000001, "p": [0.0, -65536.0, 0.0], "r": [0, 0, 0],
             "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 200001,
                               "events": [{"type": "20000010001", "info": [3, 4]}]})},
            {"g": 3, "i": 10004001, "p": [0.0, 0.0, 0.0], "r": [0, 0, 0], "s": [1, 1, 1],
             "ci": json.dumps({"component_id": 10004001, "events": []})}],
            "map": {"name": "m", "framework_id": "4002",
                    "map_setting": {"14": "4002", "1": "0", "10": "1"}},
            "story": {"stories": [{"name": u"S", "dialogues": []}]}}

    def _plan(self, **flags):
        import argparse
        ns = argparse.Namespace(include_ui=False, include_motion=False,
                                allow_sentinel=True, with_story=False,
                                include_global=False, include_not_placeable=False,
                                only_types=None, only_ids=None)
        for k, v in flags.items():
            setattr(ns, k, v)
        return sr.build_plan(self._snap(), {100001, 200001, 10004001}, ns), ns

    def test_gaps_name_the_missing_flags(self):
        plan, ns = self._plan()
        reqs = sr.stage_requirements(self._snap(), plan, ns)
        by_flag = {r["flag"]: r for r in reqs}
        self.assertIn("--include-ui", by_flag)
        self.assertIn("--with-story", by_flag)
        self.assertIn("--sync-settings", by_flag)
        self.assertIn("--sync-global", by_flag)
        self.assertTrue(all(not r["satisfied"] for r in reqs), msg=reqs)
        note = sr.missing_stage_note(reqs)
        self.assertIn("少一整层", note)
        self.assertIn("--include-ui", note)

    def test_all_flags_on_means_no_gap(self):
        plan, ns = self._plan(include_ui=True, with_story=True, include_global=True,
                              include_not_placeable=True)
        ns.sync_settings = True
        ns.sync_global = True
        ns.with_story = True
        reqs = sr.stage_requirements(self._snap(), plan, ns)
        self.assertEqual(sr.missing_stage_note(reqs), None, msg=reqs)

    def test_every_skip_reason_is_classified(self):
        """`classify_components` 新加一种跳过原因时，必须在这里表态（要开关 or 不算缺口）。

        否则"少一层"会以"没人认识这个原因"的形式静默溜掉 —— 正是这条判据要防的事。
        """
        import inspect
        import re
        src = inspect.getsource(sr.classify_components)
        emitted = {m.split("（", 1)[0] for m in
                   re.findall(r'reason = "([^"]+)', src)}
        self.assertTrue(emitted, msg="reason 的写法变了，本测试要同步改写")
        unknown = emitted - set(sr.STAGE_NEEDS) - set(sr.STAGE_NEEDS_NONE)
        self.assertEqual(unknown, set(),
                         msg="这些跳过原因没归进 STAGE_NEEDS / STAGE_NEEDS_NONE：%s" % unknown)

    def test_motion_points_are_not_demanded(self):
        """运动点**不是**缺口：催用户 `--include-motion` 反而会在目标图上多摆 50 件。

        实机证据（三张不同的图）：`gpt6和风街区`（0 台运动器 / 361 件）、
        `樱花残羽艾玛的地图`（6 台运动器）、春岚（332 台运动器）里 203912
        **都正好 50 个** ⇒ 每图自带的框架件，不是作者内容。
        """
        snap = self._snap()
        snap["components"].append(
            {"g": 4, "i": 203912, "p": [0.0, -65536.0, 0.0], "r": [0, 0, 0],
             "s": [1, 1, 1], "ci": json.dumps({"component_id": 203912, "events": []})})
        ns = sr.argparse.Namespace(include_ui=True, include_motion=False,
                                allow_sentinel=True, with_story=True,
                                include_global=True, include_not_placeable=True,
                                only_types=None, only_ids=None,
                                sync_settings=True, sync_global=True)
        plan = sr.build_plan(snap, {100001, 200001, 10004001, 203912}, ns)
        self.assertIn("motion_point", str(plan["skipped_by_reason"]))
        reqs = sr.stage_requirements(snap, plan, ns)
        self.assertFalse([r for r in reqs if "motion" in r["stage"].lower()
                          or "运动点" in r["stage"]],
                         msg="运动点被当成缺口 ⇒ 有人会去加 --include-motion，目标图多摆 50 件：%s"
                             % reqs)
        self.assertIsNone(sr.missing_stage_note(reqs),
                          msg="该开的都开了就不该报缺口：%s" % reqs)
        self.assertIn("motion_point", sr.STAGE_NEEDS_NONE)
        self.assertNotIn("motion_point", sr.STAGE_NEEDS)

    def test_gate_runs_after_progress_resume_but_before_placing(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertLess(src.index("resume_from"), src.index("allow_missing_stages"),
                        msg="缺口要在 resume 之后判（已建好的层不该再被要求）")
        self.assertLess(src.index("allow_missing_stages"), src.index("阶段 1：摆放"),
                        msg="但必须拦在真正动手之前")

    def test_pilot_run_is_exempt(self):
        """试点（--only-types / --only-ids）本来就只摆几件，拿"少一层"判它就是每次误报。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("and not pilot_mode", src,
                      msg="筛法声明过「这不是全量」，闸门要让路")
        self.assertLess(src.index("pilot_mode = "), src.index("and not pilot_mode"))


class TestStockLayerIsNotAGap(unittest.TestCase):
    """出厂自带层要对着**目标图现状**判，不能照快照全量催开关。

    2026-10-01 实机：新开的一张图 = 113 件，逐件与春岚比下来全是出厂默认件
    （运动点 50、界面控件 48、锚点 4、天空盒/雾/LUT…各 1），而闸门当时无条件
    催 `--include-ui`「48 处缺口」。照它带上开关 ⇒ 目标图再插 48 件重复控件
    （和 `--include-motion` 那次把出厂运动点当缺口是同一类自伤，只是这次会真写下去）。
    """

    def setUp(self):
        self._real_rows = sr.fetch_target_type_rows

    def tearDown(self):
        sr.fetch_target_type_rows = self._real_rows

    def _ui(self, g, extra=None, p=(0.0, 0.0, 0.0)):
        ci = {"component_id": 200001,
              "events": [{"type": "20000120098", "info": [1]}]}
        ci.update(extra or {})
        return {"g": g, "i": 6000001, "p": list(p), "r": [0.0, 0.0, 0.0],
                "s": [1.0, 1.0, 1.0], "ci": json.dumps(ci)}

    def _row(self, g, cid=200001, extra=None, p=(0.0, 0.0, 0.0)):
        """目标图侧的一行（`CODE_TYPE_CENSUS` 的回包形状：i 是 component_id）。"""
        ci = {"component_id": cid,
              "events": ([{"type": "20000120098", "info": [1]}]
                         if cid == 200001 else [])}
        ci.update(extra or {})
        return {"g": g, "i": cid, "ci": json.dumps(ci), "p": list(p),
                "r": [0.0, 0.0, 0.0], "s": [1.0, 1.0, 1.0]}

    def _snap(self, *comps):
        return {"components": list(comps),
                "map": {"name": "m", "framework_id": "4002",
                        "map_setting": {"14": "4002"}},
                "story": {}}

    def _ns(self, include_ui=False):
        import argparse
        return argparse.Namespace(include_ui=include_ui, include_motion=False,
                                  allow_sentinel=True, with_story=False,
                                  include_global=False, include_not_placeable=False,
                                  only_types=None, only_ids=None,
                                  sync_settings=False, sync_global=False)

    def _stock(self, snap, rows, target_n=None, types=(100001, 200001)):
        sr.fetch_target_type_rows = lambda t, timeout=120: rows
        tgt = {"type_distribution": {"UI_NATIVE(200001)": (target_n
                                                           if target_n is not None
                                                           else len(rows))}}
        return sr.stock_for_stages(snap, None, set(types), tgt)

    def test_identical_stock_is_not_reported_as_gap(self):
        snap = self._snap(self._ui(1), self._ui(2))
        stock, excl = self._stock(snap, [self._row(9), self._row(10)])
        self.assertEqual(stock["ui"], {"snap_n": 2, "on_target": 2, "net": 0,
                                       "types": [200001]}, msg=stock)
        self.assertEqual(sorted(excl), [1, 2])
        plan = sr.build_plan(snap, {100001, 200001}, self._ns())
        reqs = sr.stage_requirements(snap, plan, self._ns(), stock=stock)
        ui = [r for r in reqs if r["flag"] == "--include-ui"][0]
        self.assertEqual(ui["count"], 0, msg="缺口要按净数报")
        self.assertTrue(ui["satisfied"], msg="出厂层已齐 ⇒ 不该催开关")
        self.assertIn("出厂自带", ui["note"])

    def test_partial_overlap_reports_the_net(self):
        snap = self._snap(self._ui(1), self._ui(2), self._ui(3, {"x": 1}))
        stock, excl = self._stock(snap, [self._row(9)])
        self.assertEqual((stock["ui"]["on_target"], stock["ui"]["net"]), (1, 2))
        self.assertEqual(len(excl), 1)

    def test_multiset_not_set_keeps_authored_duplicates(self):
        """目标图有 1 件同内容只抵掉 1 件：作者自己摆的重复件不能被吃掉。"""
        snap = self._snap(self._ui(1), self._ui(2))     # 快照里两件内容相同
        stock, excl = self._stock(snap, [self._row(9)])
        self.assertEqual(stock["ui"]["net"], 1, msg="该摆的还剩一件")
        self.assertEqual(len(excl), 1)

    def test_moved_position_is_not_stock(self):
        """位置不同就不算"已经有了"：只比属性袋会把作者挪过位置的那件判成已有而漏摆，
        目标图最后留下的是摆错地方的那件 —— 比多一件更糟（实机：新图默认角色在
        (620.89, 3.11, -114.73)，春岚那 4 件角色全被挪过）。"""
        snap = self._snap(self._ui(1, p=(919.0, 62.201, -282.048)))
        stock, excl = self._stock(snap, [self._row(9)])
        self.assertEqual(stock["ui"]["on_target"], 0, msg="挪过位置不算出厂件")
        self.assertEqual(stock["ui"]["net"], 1)
        self.assertEqual(excl, {})

    def test_stock_editor_object_is_deducted_even_without_a_stage(self):
        """103002 / 203930 这类编辑器件：阀门不给摆（`server_place_supported=false`）、
        每图又自带一件 ⇒ 留在计划里只会每件失败一次（2026-10-01 试点实机第 4 件）。
        扣件范围不限于 ui/not_placeable 两层，按"目标图已有同内容同位置"判。"""
        comp = self._row(55, cid=103002, p=(0.0, -65536.0, 0.0))
        snap = self._snap(comp)
        sr.fetch_target_type_rows = lambda t, timeout=120: [
            self._row(52, cid=103002, p=(0.0, -65536.0, 0.0))]
        stock, excl = sr.stock_for_stages(
            snap, None, {103002},
            {"type_distribution": {"NPC_ACTION_EDITOR(103002)": 1}})
        self.assertEqual(excl, {55: (103002, 52)},
                         msg="扣掉的同时要带上目标图那个已有号（引用要接得上）")

    def test_renumbered_id_refs_still_match(self):
        """比对口径与验收同源：袋里存落盘 ugc_id 的字段不参与内容判等。"""
        from snapshot import ID_REF_PROPERTIES as REF
        f = REF[0]
        snap = self._snap(self._ui(1, {f: [[14982]]}))
        stock, excl = self._stock(snap, [self._row(9, extra={f: [[77777]]})])
        self.assertEqual(stock["ui"]["on_target"], 1, msg="换了号也算同一件")
        self.assertEqual(list(excl), [1])

    def test_target_without_that_type_skips_the_bridge_call(self):
        snap = self._snap(self._ui(1))

        def _boom(t, timeout=120):
            raise AssertionError("目标图一件这类都没有，不该发起采集")
        sr.fetch_target_type_rows = _boom
        stock, excl = sr.stock_for_stages(
            snap, None, {100001, 200001},
            {"type_distribution": {"GEOMETRY(100001)": 5}})
        self.assertEqual(stock["ui"]["net"], 1, msg="整层都是真缺口")
        self.assertEqual(excl, {})

    def test_unreadable_target_falls_back_and_says_so(self):
        snap = self._snap(self._ui(1))
        sr.fetch_target_type_rows = lambda t, timeout=120: None
        stock, excl = sr.stock_for_stages(
            snap, None, {100001, 200001},
            {"type_distribution": {"UI_NATIVE(200001)": 1}})
        self.assertEqual(stock, {})
        self.assertEqual(excl, {})
        plan = sr.build_plan(snap, {100001, 200001}, self._ns())
        reqs = sr.stage_requirements(snap, plan, self._ns(), stock=stock)
        ui = [r for r in reqs if r["flag"] == "--include-ui"][0]
        self.assertIn("没对着目标图判", ui.get("note", ""))
        self.assertFalse(ui["satisfied"], msg="读不到就退回老口径：仍然催开关")

    def test_deduction_is_wired_before_the_write(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("stock_for_stages", src)
        self.assertIn("s.get(\"old_g\") not in overlapped", src)
        self.assertLess(src.index("stock_for_stages"), src.index("阶段 1：摆放"),
                        msg="扣件要发生在真正动手之前")
        self.assertLess(src.index('plan["specs"] = specs'), src.index("阶段 1：摆放"))

    def test_budget_is_rejudged_on_what_will_actually_be_placed(self):
        """跑到 11421 件时中途保存把 progress 的 id 洗成过期态，只能走 --resume-from 续跑，
        而预检按整份计划报「component_nums 需 14887 / 余 3692 ⇒ 预算不足」—— 已摆过的件
        被重复计了一次。实机数据：整份计划 error，按这次真会摆的 3448 件算 ok。
        所以复核必须排在 resume/续做**之后**，而且不受 --yes 放行。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("remaining = [s for s in specs", src)
        self.assertIn("bg2", src)
        self.assertLess(src.index("resume_from"), src.index("remaining = [s for s in specs"),
                        msg="要等 resume 把「还要建什么」定下来才算")
        self.assertLess(src.index("remaining = [s for s in specs"), src.index("阶段 1：摆放"))
        seg = src[src.index("remaining = [s for s in specs"):src.index("阶段 1：摆放")]
        self.assertIn("return 1", seg, msg="复核不够要真拦，不许只印一行")

    def test_resume_from_export_is_checked_for_freshness_before_writing(self):
        """`save_map` 会重洗落盘 id（实机：一次长跑里 11349/11435 个号自己就不认识了）。
        拿旧导出走 `--resume-from` 时配对**照样按内容配得上**，但那批号已不作数
        ⇒ 属性回写到别的组件上，而回读仍报 ok。所以动手前必须抽号问一次当前图。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("新鲜度", src)
        self.assertLess(src.index("list(id_map.values())"), src.index("阶段 1：摆放"))
        seg = src[src.index("list(id_map.values())"):src.index("阶段 1：摆放")]
        self.assertIn("return 1", seg, msg="抽样对不上要真拒写，不许只印一行")

    def test_deducted_items_are_seeded_into_the_id_map(self):
        """扣掉的件在目标图上**有对应对象**：只扣不接 = 引用悬空被闸门拦下，
        强行放行 = 旧号原样写下去接到别的组件。两种都比不扣更糟。"""
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn("specs + stock_specs", src, msg="悬空判据要把已有件算进本次范围")
        self.assertIn("id_map[_old] = _tg", src)
        self.assertLess(src.index("id_map[_old]"), src.index("阶段 1：摆放"))


class TestIncludeNotPlaceable(unittest.TestCase):
    """`--include-not-placeable`：服务端受理、但阀门里没有实测记录的件（角色 / 推荐检查点）。

    春岚 1:1 交付时那 6 件被旧口径判成"无解，只能手摆"，复审发现拦截来自
    2026-09-16 的**客户端本地构造**结论（`fuxiang_scene` 里有 2026-09-30 复审注释），
    不是服务端拒绝。放行只回答"能不能进计划"，落不落库仍要重进回对 ⇒
    计划必须同时把 `allow_unverified` 标出来，好让 place_item 越过 `plan()` 那道阀门。
    """

    def test_skipped_by_default(self):
        comps = [_comp(1, 101001, 101001), _comp(2, 301001, 100001)]
        to_place, skipped = sr.classify_components(comps, {100001})
        self.assertEqual([c["g"] for c in to_place], [2])
        self.assertEqual(skipped[0]["reason"].split("（")[0], "not_placeable")

    def test_flag_admits_and_marks_only_unverified(self):
        args = sr.argparse.Namespace(include_not_placeable=True)
        snap = {"components": [_comp(1, 101001, 101001), _comp(2, 301001, 100001)]}
        plan = sr.build_plan(snap, {100001}, args)
        self.assertEqual(sorted(s["old_g"] for s in plan["specs"]), [1, 2])
        by = {s["old_g"]: s for s in plan["specs"]}
        self.assertTrue(by[1].get("allow_unverified"))
        # 已实测类型不该被顺手放宽（allow_unverified 会同时绕过 disable_edit 等护栏）
        self.assertNotIn("allow_unverified", by[2])

    def test_place_one_forwards_the_flag(self):
        """标记要真的传到 place_item，否则计划放行、实机照样被拒。"""
        from unittest.mock import patch
        spec = sr.place_spec(_comp(1, 101001, 101001))
        spec["allow_unverified"] = True
        with patch.object(sr, "call_edit",
                          return_value={"status": "placed", "ugc_id": 9}) as m:
            _r, status, _tries = sr.place_one(spec, "t1")
        self.assertEqual(status, "placed")
        sent = m.call_args[0][1]
        self.assertTrue(sent["allow_unverified"])
        self.assertEqual(sent["item_id"], 101001)

    def test_place_one_always_allows_recorded_scale(self):
        """还原侧摆放一律带 allow_out_of_range_scale：抄的是源图既有缩放。

        不带这条出口时，春岚 214 个组合件（z=0.02 < 目录下限 0.1）会在 plan() 就被拒，
        整层组合件摆不出来 —— 而它们占 14291 处成员引用。
        """
        from unittest.mock import patch
        spec = sr.place_spec(_comp(552, 203900, 203900))
        with patch.object(sr, "call_edit",
                          return_value={"status": "placed", "ugc_id": 9}) as m:
            sr.place_one(spec, "t1")
        self.assertTrue(m.call_args[0][1]["allow_out_of_range_scale"])

    def test_hint_points_at_the_flag(self):
        """出口提示别再写"无解只能手摆"——那会让人跳过本可试的路。"""
        self.assertIn("include-not-placeable", sr.DANGLING_FIX_HINTS["not_placeable"])


class TestUiCustomWidgets(unittest.TestCase):
    """界面控件（UI_CUSTOM 200000）：落点在画布 2D，不在单元上；视图没开要报 warn。

    全图控件共用一个 UgcGlobalUnit（UI_EDIT.md §0）⇒ 快照的 p/r/s 对所有控件是同一个值，
    照它摆会把整批控件叠在同一个点，之后才靠属性回写挪回去 —— 中间那次原生确认
    （占用/越界弹窗判据）看到的却是重叠堆，判据失真。
    """

    def _widget(self, gid, pos_xy, unit_p=(0.0, -65536.0, 0.0)):
        return {"g": gid, "i": 5201002, "p": list(unit_p), "r": [0, 0, 0],
                "s": [1, 1, 1],
                "ci": json.dumps({"component_id": 200000, "events": [
                    {"type": 20000010001, "info": [pos_xy[0], pos_xy[1]]},
                    {"type": 20000016605, "info": ["我的事件", 1286]}]})}

    def test_place_spec_uses_canvas_pos(self):
        spec = sr.place_spec(self._widget(362, [451.6, 625.2]))
        self.assertEqual(spec["position"], [451.6, 625.2, 0.0])
        self.assertEqual(spec["component_type"], 200000)

    def test_widget_without_pos_falls_back_to_unit(self):
        """袋里没有 POS 才退回单元坐标；哨兵 y 仍按既有规则兜底落 y=0。"""
        c = self._widget(362, [1.0, 2.0])
        c["ci"] = json.dumps({"component_id": 200000, "events": []})
        self.assertEqual(sr.place_spec(c)["position"], [0.0, 0.0, 0.0])

    def test_non_widget_ignores_the_canvas_slot(self):
        """POS 槽号只在 UI_CUSTOM 上有画布语义：非控件件不许被它带跑。"""
        c = self._widget(7, [1.0, 2.0])
        c["ci"] = json.dumps({"component_id": 100001, "events": [
            {"type": 20000010001, "info": [1.0, 2.0]}]})
        self.assertEqual(sr.place_spec(c)["position"], [0.0, 0.0, 0.0])

    def test_preflight_warns_when_view_closed(self):
        specs = [sr.place_spec(self._widget(362, [451.6, 625.2]))]
        r = sr.check_ui_view(specs, {"view_open": False})
        self.assertEqual(r["level"], "warn")
        self.assertEqual(r["widgets"], 1)
        self.assertIn("界面编辑器", r["msg"])
        self.assertNotIn("失败", r["msg"])          # 是提示不是判死
        self.assertEqual(sr.check_ui_view(specs, {"view_open": True})["level"], "ok")
        self.assertEqual(sr.check_ui_view(specs, None)["level"], "warn")

    def test_no_widgets_means_no_ui_line(self):
        r = sr.check_ui_view([sr.place_spec(_comp(1, 301001, 100001))], {"view_open": False})
        self.assertEqual((r["level"], r["widgets"]), ("ok", 0))


    def test_widget_with_canvas_pos_is_not_skipped_as_positionless(self):
        """控件的 p 恒是哨兵（全图控件共用一个单元），坐标在袋里 ⇒ 不能按 sentinel 跳掉。

        2026-10-01 实机：还原一件控件时 `可摆 0`，全被 sentinel_position 吃了。
        """
        w = self._widget(362, [451.6, 625.2])
        to_place, skipped = sr.classify_components([w], None)
        self.assertEqual([c["g"] for c in to_place], [362])
        self.assertEqual(skipped, [])
        self.assertTrue(sr.has_known_position(w))

    def test_widget_without_canvas_pos_is_still_skipped(self):
        """袋里也没有 POS 才是真没坐标（别把兜底 y=0 当成有坐标）。"""
        w = self._widget(363, [0, 0])
        w["ci"] = json.dumps({"component_id": 200000, "events": []})
        to_place, skipped = sr.classify_components([w], None)
        self.assertEqual(to_place, [])
        self.assertIn("sentinel_position", skipped[0]["reason"])

    def test_deleted_residue_is_skipped_not_placed(self):
        """导出后仍在组件表里的撤销栈残骸（del:1）不是地图内容，摆出去就是幽灵件。"""
        c = _comp(900, 301001, 100001, p=(10.0, 1.0, 20.0))
        c["del"] = 1
        to_place, skipped = sr.classify_components([c], None)
        self.assertEqual(to_place, [])
        self.assertIn("deleted_residue", skipped[0]["reason"])

    def test_source_marks_residue_rows(self):
        """导出侧要真的打上 `del` 标（那是字符串代码，普通用例发现不了被改回去）。"""
        import snapshot as sn
        self.assertIn('it["del"] = 1', sn.CODE_COMPONENTS)
        self.assertIn("room.del_unit_uids", sn.CODE_COMPONENTS)


class TestPilotSelection(unittest.TestCase):
    def _specs(self):
        return [sr.place_spec(_comp(1, 301001, 100001)),
                sr.place_spec(_comp(120, 203911, 203911, p=(0, -65536, 0))),
                sr.place_spec(_comp(552, 203900, 203900))]

    def test_parse_int_list(self):
        self.assertIsNone(sr.parse_int_list(None))
        self.assertIsNone(sr.parse_int_list(""))
        self.assertEqual(sr.parse_int_list("203911, 203900"), {203911, 203900})
        with self.assertRaises(ValueError):
            sr.parse_int_list("203911,运动器")     # 静默忽略会摆出一张缺件的图

    def test_select_by_component_type(self):
        kept, dropped = sr.select_specs(self._specs(), only_types={203911})
        self.assertEqual([s["old_g"] for s in kept], [120])
        self.assertEqual(dropped, 2)

    def test_select_by_ids_and_intersection(self):
        kept, _ = sr.select_specs(self._specs(), only_ids={1, 552})
        self.assertEqual(sorted(s["old_g"] for s in kept), [1, 552])
        kept, _ = sr.select_specs(self._specs(), only_types={203900}, only_ids={1, 552})
        self.assertEqual([s["old_g"] for s in kept], [552])   # 同时给取交集

    def test_build_plan_counts_reflect_selection(self):
        """计划里报出的 to_place 必须是筛后的数字，否则预算与写前确认都会误报。"""
        snap = {"components": [_comp(1, 301001, 100001),
                               _comp(120, 203911, 203911, p=(0, -65536, 0))]}
        args = sr.argparse.Namespace(allow_sentinel=True, only_types="203911")
        plan = sr.build_plan(snap, None, args)
        self.assertEqual(plan["to_place"], 1)
        self.assertEqual(plan["only"]["types"], [203911])
        self.assertEqual(plan["only"]["dropped"], 1)


class TestPlannedBudget(unittest.TestCase):
    """need 必须是「这次真会占多少」，不是「源图整图占多少」。"""

    SNAP = {"components": ([_comp(i, 301001, 100001) for i in range(100)]
                           + [_comp(900 + i, 203911, 203911, p=(0, -65536, 0))
                              for i in range(10)]
                           + [_comp(9999, 10007001, 10007001, p=(0, -65536, 0))]),
            "budget": {"occupancy": 222, "component_nums": 111,
                       "max_occupancy": 30000, "max_component_nums": 15000}}

    def test_skipped_items_are_not_counted(self):
        """不摆的（逻辑件默认整批跳过）不该进 need：顶格图会被整图口径误拦。"""
        plan = sr.build_plan(self.SNAP, None)
        need = sr.plan_budget_need(self.SNAP, plan, plan["specs"])
        self.assertEqual(need["components"], 100)          # 不含 10 台运动器与剧情件
        self.assertLess(need["components"], self.SNAP["budget"]["component_nums"])

    def test_with_story_adds_story_nodes(self):
        plan = sr.build_plan(self.SNAP, None)
        with_story = sr.plan_budget_need(self.SNAP, plan, plan["specs"],
                                         with_story=True)
        self.assertEqual(with_story["components"], 101)    # +1 个剧情节点

    def test_occupancy_converts_from_source_average(self):
        """单件占用不线性（组合件/运动器更贵），按源图均值折算且宁高不低。"""
        plan = sr.build_plan(self.SNAP, None)
        need = sr.plan_budget_need(self.SNAP, plan, plan["specs"])
        self.assertEqual(need["occupancy_per_component"], 2.0)   # 222 / 111
        self.assertGreaterEqual(need["occupancy"], need["components"])
        self.assertEqual(need["occupancy"], 200)

    def test_plan_counts_drive_the_verdict(self):
        tgt = {"occupancy": 0, "max_occupancy": 30000,
               "component_nums": 14895, "max_component_nums": 15000}   # 余 105
        plan = sr.build_plan(self.SNAP, None)
        whole = sr.check_budget(self.SNAP["budget"], tgt)          # 整图 111 > 105 → 误拦
        planned = sr.check_budget(self.SNAP["budget"], tgt,
                                  sr.plan_budget_need(self.SNAP, plan, plan["specs"]))
        self.assertEqual(whole["level"], "error")
        self.assertEqual(planned["level"], "ok")
        self.assertTrue(all(r["basis"] == "计划" for r in planned["rows"]))

    def test_legacy_two_arg_call_keeps_old_behaviour(self):
        tgt = {"occupancy": 100, "max_occupancy": 30000,
               "component_nums": 20, "max_component_nums": 15000}
        r = sr.check_budget({"occupancy": 29999, "component_nums": 1}, tgt)
        self.assertEqual(r["level"], "error")
        self.assertEqual(r["rows"][0]["basis"], "快照整图")


class TestLoadProgress(unittest.TestCase):
    def _write(self, rows):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_resume_is_keyed_by_component_not_index(self):
        """试点记录（i=0 是运动器）不该让全量跑把「第 0 条方块」当成已摆。"""
        path = self._write([{"i": 0, "status": "placed", "old_g": 120, "ugc_id": 7,
                             "run": "rA", "map": "靶场"}])
        id_map, done, foreign = sr.load_progress(path, rid="rB", cur_map="靶场")
        self.assertEqual(done, {120})
        self.assertEqual(id_map, {120: 7})
        self.assertEqual(foreign, 0)

    def test_records_from_another_map_are_ignored(self):
        """同一份快照在别的图上跑过：old_g→ugc_id 映射指向另一张图，绝不能沿用。"""
        path = self._write([{"status": "placed", "old_g": 1, "ugc_id": 5,
                             "run": "rA", "map": "和风的旧靶场"}])
        id_map, done, foreign = sr.load_progress(path, rid="rB", cur_map="春岚二号")
        self.assertEqual((id_map, done, foreign), ({}, set(), 1))

    def test_new_run_ignores_other_runs(self):
        path = self._write([{"status": "placed", "old_g": 1, "ugc_id": 5,
                             "run": "rA", "map": "靶场"}])
        self.assertEqual(sr.load_progress(path, rid="rB", cur_map="靶场",
                                          new_run=True)[2], 1)
        self.assertEqual(len(sr.load_progress(path, rid="rB", cur_map="靶场")[0]), 1)

    def test_legacy_records_without_run_or_map_still_resume(self):
        path = self._write([{"i": 3, "status": "placed", "old_g": 42, "ugc_id": 9}])
        id_map, done, foreign = sr.load_progress(path, rid="rB", cur_map="靶场")
        self.assertEqual(done, {42})
        self.assertEqual(foreign, 0)

    def test_failed_records_never_count_as_done(self):
        path = self._write([{"status": "ambiguous", "old_g": 42, "ugc_id": None,
                             "run": "rA", "map": "靶场"}])
        self.assertEqual(sr.load_progress(path, rid="rA", cur_map="靶场")[1], set())


class TestDanglingAdvice(unittest.TestCase):
    """悬空引用必须按「为什么没摆」分报。

    旧版一律提示"加 --with-story 会补绑"，而春岚 234 条悬空里 232 条指向运动器
    （sentinel_position）—— 那条路对它们无效，提示会把人误导成"已经救回来了"。
    """

    def _d(self, g):
        return {"old_ugc_id": g, "event_key": "e", "property_id": 1}

    def test_movers_point_to_allow_sentinel_not_story(self):
        reason = {120: "sentinel_position（逻辑组件无真实坐标，需 --allow-sentinel）"}
        out = sr.dangling_advice([self._d(120)] * 3, reason)
        self.assertEqual(out[0][0], "sentinel_position")
        self.assertEqual(out[0][1], 3)
        self.assertIn("--allow-sentinel", out[0][2])
        self.assertNotIn("with-story", out[0][2])

    def test_story_nodes_still_point_to_with_story(self):
        reason = {9999: "story_stage（由 --with-story 阶段重建）"}
        out = sr.dangling_advice([self._d(9999)], reason)
        self.assertEqual(out[0][0], "story_stage")
        self.assertIn("--with-story", out[0][2])

    def test_unknown_component_is_reported_not_silently_story(self):
        out = sr.dangling_advice([self._d(7)], {})
        self.assertIn("不在本次计划里", out[0][0])

    def test_pilot_filtered_items_are_not_reported_as_unknown(self):
        """试点只摆 2 件，其余悬空引用必须说明"是筛掉的，不是没法还原"。"""
        snap = {"components": [_comp(i, 301001, 100001) for i in range(5)]
                + [_comp(120, 203911, 203911, p=(0, -65536, 0))]}
        args = sr.argparse.Namespace(allow_sentinel=True, only_ids="120")
        plan = sr.build_plan(snap, None, args)
        reason = sr._skip_reason_index(snap, plan, plan["specs"],
                                       only=args.only_ids)
        self.assertIn("未被试点过滤选中", str(reason[3]))
        out = sr.dangling_advice([{"old_ugc_id": 3}], reason)
        self.assertIn("全量", out[0][2])
        self.assertNotIn("with-story", out[0][2])

    def test_limit_truncation_is_not_blamed_on_only_flags(self):
        """`--limit` 截断被印成「去掉 --only-* 跑全量」= 让人去动一个根本没带的开关。
        2026-10-01 试点那次日志就是这么写的（那次只带了 --limit 20）。"""
        snap = {"components": [_comp(i, 301001, 100001) for i in range(5)]}
        plan = sr.build_plan(snap, None, sr.argparse.Namespace(allow_sentinel=True))
        reason = sr._skip_reason_index(snap, plan, plan["specs"][:2], limit=2)
        self.assertIn("被 --limit 截断", str(reason[4]), msg=reason)
        self.assertNotIn("only", str(reason[4]))
        out = sr.dangling_advice([{"old_ugc_id": 4}], reason)
        self.assertIn("--limit", out[0][2])

    def test_deducted_stock_item_is_reported_as_already_present(self):
        """扣掉的出厂件不是"没摆"，是**已经在目标图上**：话术要说清，否则每次预检
        都会让人以为少了一件。"""
        snap = {"components": [_comp(i, 301001, 100001) for i in range(3)]}
        plan = sr.build_plan(snap, None, sr.argparse.Namespace(allow_sentinel=True))
        reason = sr._skip_reason_index(snap, plan, plan["specs"][:1], overlapped={2})
        self.assertIn("目标图已有同内容", str(reason[2]), msg=reason)
        out = sr.dangling_advice([{"old_ugc_id": 2}], reason)
        self.assertIn("已经在", out[0][2])

    def test_motion_point_hint_no_longer_asks_to_place_framework(self):
        """运动点每图自带 50 个：出口话术里写 `--include-motion` 等于教人去多摆一层。"""
        self.assertIn("不该摆", sr.DANGLING_FIX_HINTS["motion_point"])
        self.assertIn("--allow-sentinel", sr.DANGLING_FIX_HINTS["motion_point"])
        self.assertNotIn("加 --include-motion", sr.DANGLING_FIX_HINTS["motion_point"],
                         msg="不能再把摆运动点当出口")

    def test_audit_and_restore_share_one_definition(self):
        """出口表只许一处定义（snapshot_audit 必须 import，不得复制一份）。"""
        import snapshot_audit
        self.assertIs(snapshot_audit.dangling_advice, sr.dangling_advice)


class TestVerifyScoping(unittest.TestCase):
    """`--verify-only` 的"幽灵引用"只能在**本次纳入判据的对象**里判。

    2026-10-01 实机：试点只摆 2 件后跑回对，报出 6 条"多余引用（幽灵）"——
    其实那些是春岚自带的运动器/时钟引用，它们根本不在本次 id_map 里。
    误报的害处不是难看，而是让人以为还原写出了脏数据。
    """

    def _run(self, id_map, actual_refs, scope=None):
        from unittest.mock import patch
        events = {"count": 1, "events": [
            {"event_key": "E", "refs": [
                {"ugc_id": 120, "property_id": 20391102004, "params_idx": 0}]}]}
        got = {"count": 1, "events": [
            {"event_key": "E", "refs": [dict(r, index=3) for r in actual_refs]}]}

        def fake(op, args, timeout=None):
            return {"events": got["events"]} if op == "event_list" else {}

        with patch.object(sr, "call_edit", side_effect=fake):
            return sr.verify_event_refs(events, id_map, scope_new_ids=scope)

    def test_untouched_object_is_not_called_a_ghost(self):
        v = self._run({120: 900120},                       # 本次只重建了 900120
                      [{"ugc_id": 900120, "property_id": 20391102004, "params_idx": 0},
                       {"ugc_id": 14975, "property_id": 20391102004, "params_idx": 0}])
        self.assertEqual(v["extra"], [])
        self.assertEqual(len(v["out_of_scope"]), 1)
        self.assertTrue(v["ok"])                            # 未纳入判据不许拉红

    def test_real_ghost_still_reported(self):
        v = self._run({120: 900120},
                      [{"ugc_id": 900120, "property_id": 20391102004, "params_idx": 0},
                       {"ugc_id": 900120, "property_id": 20391102001, "params_idx": 0}])
        self.assertEqual(len(v["extra"]), 1)
        self.assertEqual(v["out_of_scope"], [])
        self.assertFalse(v["ok"])


class TestAuditDeliveryGate(unittest.TestCase):
    """`snapshot_audit`（手册第 1.5 步）必须带上交付闸门，且**沿用同一份判据**。

    audit 原本只算"能摆多少"，看不见待提交删除的身份 / UI_NATIVE 整层缺口 / 玩法设置没采到
    ⇒ 印出来一切正常，收口时才发现不对。而"两处各算一遍"在这个仓库已经漂移过两次。
    """

    def _write(self, comps, tmp):
        snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t0",
                "map": {"name": "m", "framework_id": "4002",
                        "map_setting": {"14": "4002"}, "anchor_info": {}},
                "components": comps,
                "events": {"count": 0, "events": [], "saved_keys": []},
                "story": {"stories": []},
                "budget": {"component_nums": len(comps), "max_component_nums": 15000,
                           "occupancy": 1}}
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        return path

    def _run(self, path):
        import contextlib
        import io
        import snapshot_audit as sa
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sa.main(path)
        return buf.getvalue()

    def test_gate_section_names_the_hidden_layers(self):
        with tempfile.TemporaryDirectory() as td:
            row = {"g": 3, "i": 301001, "p": [1.0, 2.0, 3.0], "r": [0, 0, 0],
                   "s": [1, 1, 1], "del": 1,
                   "ci": json.dumps({"component_id": 203911, "events": [
                       {"type": 20391102001, "info": ["春嵐_上映_chorus-split-15"]}]},
                       ensure_ascii=False)}
            path = self._write([_comp(1, 301001, 100001),
                                _comp(2, 6000001, 200001), row], td)
            out = self._run(path)
        self.assertIn("交付闸门", out)
        self.assertIn("UI_NATIVE", out)
        self.assertIn("会在下次保存时被提交", out)
        self.assertIn("春嵐_上映_chorus-split-15", out)   # 身份，不只是条数

    def test_gate_verdict_follows_the_single_source_of_truth(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write([_comp(1, 301001, 100001),
                                _comp(1, 301002, 100001)], td)   # ugc_id 重复
            self.assertIn("有阻塞项", self._run(path))

    def test_clean_file_reports_all_passed(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write([_comp(1, 301001, 100001)], td)
            out = self._run(path)
            self.assertIn("可以交付还原", out)


class TestSameMapGuard(unittest.TestCase):
    """`--apply` 回源图自己 = 把整张图在身上再复制一遍，只能靠批量删除收拾。

    以前只有手册里一句"建议空白图"，没有任何强制；这一条把"建议"变成写前闸门。
    """

    SNAP = {"map": {"name": "春岚", "map_setting": {"name": "春岚"}},
            "budget": {"component_nums": 14653, "registered": 14605}}

    def _tgt(self, name, nums=0, reg=0):
        return {"map_setting": {"name": name},
                "budget": {"component_nums": nums, "registered": reg}}

    def test_same_name_blocks_even_on_an_empty_target(self):
        g = sr.same_map_guard(self.SNAP, self._tgt("春岚", 0, 0))
        self.assertEqual(g["level"], "block")
        self.assertIn("--allow-same-map", g["msg"])

    def test_matching_counters_block_even_if_renamed(self):
        """改了名也可能把快照摆回原图：两个计数器都对上就足以起疑。"""
        g = sr.same_map_guard(self.SNAP, self._tgt("我的新图", 14653, 14605))
        self.assertEqual(g["level"], "block")
        self.assertIn("计数器", "；".join(g["reasons"]))

    def test_blank_target_passes(self):
        g = sr.same_map_guard(self.SNAP, self._tgt("B 的空白图", 3, 2))
        self.assertEqual(g["level"], "ok")

    def test_single_counter_match_is_only_a_note(self):
        g = sr.same_map_guard(self.SNAP, self._tgt("B 的图", 14653, 2))
        self.assertEqual(g["level"], "warn")

    def test_missing_target_info_is_unknown_not_ok(self):
        """读不到目标图 ⇒ 这一项**没查成**，不许印成"确认不是源图"。"""
        g = sr.same_map_guard(self.SNAP, None)
        self.assertEqual(g["level"], "unknown")
        self.assertIn("没查成", g["msg"])

    def test_wired_before_the_write(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn('"--allow-same-map"', src)
        self.assertIn('same_map_guard(snap, tgt)', src)
        # 闸门必须在"输入 yes"之前，否则人已经按下去了
        self.assertLess(src.index('same_map_guard(snap, tgt)'),
                        src.index('input("继续？输入 yes'))


class TestLimitZeroSemantics(unittest.TestCase):
    """`--limit 0` 必须是"一件都不摆"，不许被当成"不设限"。

    2026-10-01 实机踩到：`if a.limit:` 让 0 走成 falsy ⇒ `--apply --yes --limit 0`
    实际按 14881 件去摆，只因为同图闸门与预算闸门双双拦住才没走到。
    """

    def _snap_file(self, tmp):
        comps = []
        for g in (1, 2, 3):
            comps.append(_comp(g, 301001, 100001, p=(float(g), 0.0, 0.0)))
        snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t0",
                "map": {"name": "src", "map_setting": {"name": "src"},
                        "framework_id": "4002", "anchor_info": {}},
                "components": comps,
                "events": {"count": 0, "events": [], "saved_keys": []},
                "story": {"stories": []},
                "budget": {"component_nums": 3, "max_component_nums": 15000,
                           "occupancy": 3, "max_occupancy": 30000}}
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        return path

    def _run(self, argv, tmp):
        calls = []
        old_call, old_save = sr.call_edit, sr.save_and_check
        import snapshot as _sn
        old_sn_call = _sn.call_edit

        def _stub(op, args=None, **kw):
            calls.append((op, args or {}))
            return {"ok": True, "status": "placed", "ugc_id": 777,
                    "components": [], "index": {}, "value": {}}

        sr.call_edit = _stub
        _sn.call_edit = _stub          # fetch_target_info 里的预算走的是 snapshot 那份
        sr.save_and_check = lambda *a, **k: {"submitted": True}
        try:
            rc = sr.main(argv + ["--progress", os.path.join(tmp, "p.jsonl")])
        finally:
            sr.call_edit, sr.save_and_check = old_call, old_save
            _sn.call_edit = old_sn_call
        return rc, [c[0] for c in calls]

    def test_limit_zero_places_nothing(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc, ops = self._run([path, "--apply", "--yes", "--limit", "0"], tmp)
            self.assertNotIn("place_item", ops,
                             msg="--limit 0 却发出了摆放请求：%s" % ops)
            self.assertIn("一件都不摆", buf.getvalue())

    def test_no_limit_flag_keeps_full_plan(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                # 桩里 list_types 回空集合 ⇒ 全图按 not_placeable 筛掉，
                # 这里要放行才看得见"没给 --limit 就是全量"
                _rc, ops = self._run([path, "--apply", "--yes",
                                      "--include-not-placeable"], tmp)
            self.assertEqual(ops.count("place_item"), 3)

    def test_negative_limit_refuses(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc, ops = self._run([path, "--apply", "--yes", "--limit", "-1"], tmp)
            self.assertEqual(rc, 1)
            self.assertNotIn("place_item", ops)


class TestAuditStoryCounts(unittest.TestCase):
    """`snapshot_audit` 的剧情计数不能拿 `len(dict)` 当件数。"""

    def test_story_counts_reads_the_document_not_its_keys(self):
        import snapshot_audit as sa
        snap = {"story": {"format": "fuxiang-story", "version": 1, "exported_at": "t",
                          "map": {"name": "m"}, "orphan_dialogues": [], "orphan_choices": [],
                          "stories": [{"ugc_id": 1, "dialogues": [
                              {"ugc_id": 2, "choices": [{"ugc_id": 3}, {"ugc_id": 4}]}]}]}}
        self.assertEqual(sa.story_counts(snap), (1, 1, 2))
        self.assertNotEqual(len(snap["story"]), 1)      # 正是旧代码印成"剧情 7"的原因


if __name__ == "__main__":
    unittest.main()


class TestEventCreationOrder(unittest.TestCase):
    """新建事件的**发送顺序**按源图 sort_id：面板顺序也是 1:1 的一部分。

    实机（春岚→新图）：13 条事件的引用与保存状态全一致，唯独两条的 sort_id 差 1
    （注册表按创建先后分配 sort_id，而我们原先按引用出现的先后发创建请求）
    ⇒ 收口时那一层只能等人盖章。修在创建顺序上，不动任何绑定结果。
    """

    def setUp(self):
        self.old = sr.call_edit
        self.calls = []

        def fake(op, args, timeout=90):
            self.calls.append((op, args.get("name")))
            return {"ok": True}
        sr.call_edit = fake

    def tearDown(self):
        sr.call_edit = self.old

    def _ref(self, key):
        return {"ugc_id": 1, "index": 4, "params_idx": 0,
                "property_id": 20300311106, "current_value": "['%s', 1]" % key}

    def test_created_in_source_sort_order(self):
        events = {"events": [
            {"event_key": "后建的", "sort_id": 5, "refs": [self._ref("后建的")]},
            {"event_key": "先建的", "sort_id": 1, "refs": [self._ref("先建的")]}]}
        sr.apply_events(events, {1: 11}, comp_index={})
        names = [n for op, n in self.calls if op == "event_create"]
        self.assertEqual(names, ["先建的", "后建的"],
                         msg="注册表的 sort_id 按创建先后分配 ⇒ 要按源图 sort_id 发")
        self.assertEqual(sum(1 for op, _n in self.calls if op == "event_bind"), 2,
                         msg="只许改发送顺序，绑定一条不许少")
