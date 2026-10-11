#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_restore_batch — 批量摆放/批量属性回写的护栏锁（HANDOVER_RESTORE_SPEED_20261002 §3）。

提速不能丢护栏。这里把 §3 的八条逐条锁住（每条至少一个测试），外加
批量路径自己的语义：FIFO 池化认领、幂等续做、没试≠试了失败、
op 回执不带顶层 error 键（dispatch 信封契约）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
sys.path.insert(0, str(ROOT / 'cli'))
import fuxiang_placement as placement          # noqa: E402
import fuxiang_scene as scene                   # noqa: E402
import fuxiang_edit_ops as ops                  # noqa: E402
import snapshot_restore as sr                   # noqa: E402


def _spec(item=202009, ct=100000, pos=(626.0, 0.0, -100.0)):
    return {'item_id': item, 'component_type': ct,
            'position': list(pos), 'rotation': [0, 0, 0], 'scale': [1, 1, 1]}


class UgcLogicUnit(object):
    """类名必须**精确**是 'UgcLogicUnit'：`_batch_pool`/`_poll_pending` 都按类名分流。

    实机事实（2026-10-02）：春岚的 14318 件方块（item 301001 / 组件 100001）落地后
    owner 就是它，而且同样在 del_unit_uids + create_units 里。
    """
    def __init__(self, uid, ugc_type):
        self.uid = uid
        self.ugc_type = ugc_type


class UgcGlobalUnit(object):
    """运动器 203911 / 音乐事件 203920 等的宿主：**不进** del_unit_uids，可多件共享。"""
    def __init__(self, uid, ugc_type):
        self.uid = uid
        self.ugc_type = ugc_type


class BatchPlacementTests(unittest.TestCase):
    """placement.begin_batch / poll_batch 的批量语义（护栏 1/2/3/4）。"""

    def setUp(self):
        self.cm = NS(_ugc_id2component={})
        self.room = NS(del_unit_uids={'unit': []})
        self.ui = NS(ugc_params={'create_units': [], 'create_components': []},
                     all_ugc_uid_set=set(), _add_unit_server=Mock(),
                     _add_change_unit=Mock(),
                     get_all_ugc_component_nums=lambda: len(self.ui.all_ugc_uid_set),
                     get_current_occupancy=lambda: len(self.ui.all_ugc_uid_set) * 4)
        self.units = []
        self.native = Mock(side_effect=self.confirm)
        self.scene = NS(SERVER_COMPONENT_TYPES=scene.SERVER_COMPONENT_TYPES,
                        ROLE_COMPONENT_TYPES=scene.ROLE_COMPONENT_TYPES,
                        GROUP_CREATE_TYPES=scene.GROUP_CREATE_TYPES,
                        context=lambda _: (self.room, self.cm),
                        get_edit_ui=lambda _: self.ui,
                        _apply_full_transform=Mock(
                            side_effect=self.record_transform),
                        apply_role_transform=Mock(),
                        role_position=lambda u: [1.0, 2.0, 3.0],
                        command_create=self.native,
                        transform=lambda u: getattr(
                            u, '_applied',
                            {k: list(_spec()[k])
                             for k in ('position', 'rotation', 'scale')}),
                        register_change=scene.register_change,
                        unregister_create=scene.unregister_create)

    def record_transform(self, unit, tr):
        unit._applied = {k: list(tr[k]) for k in ('position', 'rotation', 'scale')}

    def confirm(self, ui, unit, pos):
        ui.all_ugc_uid_set.add(unit.uid)
        # 共享宿主（UgcGlobalUnit）不在待删除袋里 —— 原生确认命令对它照样会被调用
        # （`_confirm_global_component` 的 uid_is_new + create_units 分支），
        # 所以这里只有"在袋里才摘"。
        if unit.uid in self.room.del_unit_uids['unit']:
            self.room.del_unit_uids['unit'].remove(unit.uid)
        return True, None

    def begin_batch(self, items, planned):
        return placement.begin_batch(self.scene, None, {'items': items},
                                     planned, self.room, self.cm, self.ui)

    def poll_batch(self, tokens):
        return placement.poll_batch(self.scene, None, {'request_ids': tokens})

    def spawn(self, uid, item=202009):
        """服务端建件落地的实机形态：新实体进待确认袋 + create_units。"""
        unit = NS(uid=uid, ugc_type=item)
        comp = NS(_ugc_id=uid, _owner_uid=uid, _owner=unit)
        self.cm._ugc_id2component[uid] = comp
        self.room.del_unit_uids['unit'].append(uid)
        self.ui.ugc_params['create_units'].append(unit)
        self.ui.ugc_params['create_components'].append(comp)
        self.units.append(unit)
        return unit

    def spawn_global_comp(self, gid, host, item=203911):
        comp = NS(_ugc_id=gid, _owner_uid=host.uid, _owner=host)
        self.cm._ugc_id2component[gid] = comp
        return comp

    def _row(self, out, i):
        return out['results'][i]

    # ---- 护栏 1：MAX_REQUESTS=128，批量大小 + 在途未定论数 ≤ 128 ----
    def test_guardrail_1_quota_counts_batch_plus_unsettled(self):
        retries = ['b%03d' % i for i in range(120)]
        state = placement._state(self.room)
        for t in retries:
            state['requests'][t] = {
                'id': t, 'status': 'pending', 'started': time.monotonic(),
                'spec': _spec(), 'ui': self.ui, 'cm': self.cm,
                'before_uids': set(), 'before_ids': set()}
        planned = [_spec()] * 129
        items = [{'request_id': t} for t in retries] + \
            [{'request_id': 'n%d' % i} for i in range(9)]
        with self.assertRaises(ValueError) as cm:
            self.begin_batch(items, planned)      # 120 未定论 + 9 新 = 129 > 128
        self.assertIn('128', str(cm.exception))
        self.assertEqual(self.ui._add_unit_server.call_count, 0)
        # 120 + 8 = 128 恰好放行；重试 token 幂等返回，不重复发送
        out = self.begin_batch(items[:128], planned[:128])
        self.assertEqual(out['registered'], 8)
        self.assertEqual(self.ui._add_unit_server.call_count, 8)

    # ---- 护栏 2：同规格去重（token 未给时按 spec 等值查未定论请求）----
    def test_guardrail_2_same_spec_without_token_is_deduped(self):
        spec = _spec()
        out = self.begin_batch([{}, {}, {}], [dict(spec)] * 3)
        self.assertEqual(out['registered'], 1, '同规格三件只许登记一件')
        ids = {r['request_id'] for r in out['results']}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.ui._add_unit_server.call_count, 1)
        # 单笔路径同样命中这份去重（spec 哈希查未定论，不再只看 active 单指针）
        reply = placement.begin(self.scene, None, {}, dict(spec),
                                self.room, self.cm, self.ui)
        self.assertEqual(reply['request_id'], next(iter(ids)))
        self.assertEqual(self.ui._add_unit_server.call_count, 1)

    def test_dedup_does_not_swallow_different_specs(self):
        out = self.begin_batch([{}], [_spec()])
        with self.assertRaises(ValueError):
            placement.begin(self.scene, None, {}, _spec(item=203003, ct=203003),
                            self.room, self.cm, self.ui)

    # ---- 护栏 3：ambiguous/conflict/finalize_failed 逐件定性，只有 placed 释放额度 ----
    def test_guardrail_3_failed_request_keeps_occupying_quota(self):
        spec = _spec()
        items = [{'request_id': 't%d' % i} for i in range(3)]
        self.begin_batch(items, [dict(spec)] * 3)
        for uid in (101, 102, 103):
            self.spawn(uid)

        def side(ui, unit, pos):
            if unit.uid == 102:
                raise RuntimeError('partial execution')
            return self.confirm(ui, unit, pos)

        self.native.side_effect = side
        out = self.poll_batch(['t0', 't1', 't2'])
        statuses = {r['request_id']: r['status'] for r in out['results']}
        self.assertEqual(statuses['t0'], 'placed')
        self.assertEqual(statuses['t1'], 'finalize_failed')
        self.assertEqual(statuses['t2'], 'placed')
        state = placement._state(self.room)
        # 未定论失败在账 ⇒ 不属于新批的 token ⇒ 新批被拒（先检查现场）。
        # begin_batch 开头的 _prune 是惰性回收：placed 的进留痕表释放额度，
        # finalize_failed 的留在 requests 里继续占额（护栏 3 的字面语义）。
        with self.assertRaises(ValueError) as cm:
            self.begin_batch([{'request_id': 'fresh'}], [dict(spec)])
        self.assertIn('t1', str(cm.exception))
        self.assertIn('t0', state['settled'])
        self.assertIn('t1', state['requests'])
        self.assertNotIn('t1', state['settled'])
        self.assertEqual(self.ui._add_unit_server.call_count, 3)
        # 用同一 token 重试：幂等返回**同一条失败定性**（finalize_failed 是终态，
        # 与单笔路径一致：绝不重发引擎、绝不建第二件；出路是检查现场/重进地图）
        out2 = self.begin_batch([{'request_id': 't1'}], [dict(spec)])
        self.assertEqual(out2['results'][0]['request_id'], 't1')
        self.assertEqual(out2['results'][0]['status'], 'finalize_failed')
        self.assertEqual(self.ui._add_unit_server.call_count, 3)
        out3 = self.poll_batch(['t1'])
        self.assertEqual(out3['results'][0]['status'], 'finalize_failed')
        self.assertEqual(len(self.cm._ugc_id2component), 3,
                         '重试不许在账上再建一件')

    # ---- 护栏 4：成组创建（204041）仍是一个请求 ----
    def test_guardrail_4_group_create_stays_one_request(self):
        spec = _spec(item=204041, ct=204041)
        spec['group_transforms'] = [{'position': [9.0, 8.0, 7.0],
                                     'rotation': [0, 0, 0], 'scale': [1, 1, 1]}]
        out = self.begin_batch([{'request_id': 'g1'}], [dict(spec)])
        self.assertEqual(out['registered'], 1, '成组创建不许拆成两个请求')
        self.assertEqual(self.ui._add_unit_server.call_count, 1)
        self.spawn(101, item=204041)
        self.spawn(102, item=204041)
        out2 = self.poll_batch(['g1'])
        row = out2['results'][0]
        self.assertEqual(row['status'], 'placed')
        self.assertEqual(len(row['group_ugc_ids']), 2, '一次 add_unit 出两件都认领')
        self.assertEqual(self.units[0]._applied['position'], [626.0, 0.0, -100.0])
        self.assertEqual(self.units[1]._applied['position'], [9.0, 8.0, 7.0])

    # ---- 批量认领：FIFO 配对 + 跨 item 不错位 + 不重复认领 ----
    def test_fifo_claim_matches_send_order_across_items(self):
        a, b = _spec(item=6012), _spec(item=6013)
        items = [{'request_id': 'f%d' % i} for i in range(3)]
        self.begin_batch(items, [a, b, a])
        self.spawn(101, item=6012)
        self.spawn(102, item=6013)
        self.spawn(103, item=6012)
        out = self.poll_batch(['f0', 'f1', 'f2'])
        got = [(r['request_id'], r['status'], r.get('ugc_id'))
               for r in out['results']]
        self.assertEqual([g[1] for g in got], ['placed'] * 3)
        self.assertEqual([g[2] for g in got], [101, 102, 103],
                         'uid 序=发送序（bench B 轮实测 FIFO），按序认领')

    def test_claim_never_shares_one_unit(self):
        spec = _spec()
        items = [{'request_id': 's%d' % i} for i in range(2)]
        self.begin_batch(items, [dict(spec)] * 2)
        self.spawn(101)
        out = self.poll_batch(['s0', 's1'])
        self.assertEqual(out['results'][0]['status'], 'placed')
        self.assertEqual(out['results'][1]['status'], 'pending',
                         '只有一个候选时第二个请求必须等下一轮，不许共用')

    def test_manual_placed_unit_is_never_claimed(self):
        spec = _spec()
        self.begin_batch([{'request_id': 'm1'}], [dict(spec)])
        unit = self.spawn(101)
        # 玩家手动放置的形态：已进编辑器登记集合
        self.ui.all_ugc_uid_set.add(101)
        out = self.poll_batch(['m1'])
        self.assertEqual(out['results'][0]['status'], 'pending')
        self.assertEqual(self.native.call_count, 0)
        self.ui.all_ugc_uid_set.discard(101)

    def test_global_component_claim_confirms_without_entity(self):
        host = NS(uid=1000057, ugc_type=203911)
        self.cm._ugc_id2component[10] = NS(_ugc_id=10, _owner_uid=host.uid,
                                           _owner=host)
        spec = _spec(item=203911, ct=203911)
        self.begin_batch([{'request_id': 'gl'}], [dict(spec)])
        self.spawn_global_comp(11, host)
        out = self.poll_batch(['gl'])
        row = out['results'][0]
        self.assertEqual(row['status'], 'placed')
        self.assertEqual(row.get('ugc_id'), 11)

    def test_pending_after_poll_suggests_backoff_and_overdue_is_named(self):
        spec = _spec()
        self.begin_batch([{'request_id': 'w1'}], [dict(spec)])
        out = self.poll_batch(['w1'])
        self.assertEqual(out['results'][0]['status'], 'pending')
        self.assertEqual(out['poll_after_ms'], 150)
        state = placement._state(self.room)
        state['requests']['w1']['started'] = time.monotonic() - 200
        out2 = self.poll_batch(['w1'])
        self.assertEqual(out2['results'][0]['status'], 'overdue')

    def test_idempotent_poll_does_not_refinalize(self):
        spec = _spec()
        self.begin_batch([{'request_id': 'p1'}], [dict(spec)])
        self.spawn(101)
        first = self.poll_batch(['p1'])
        calls = self.native.call_count
        second = self.poll_batch(['p1'])
        self.assertEqual(second['results'][0], first['results'][0])
        self.assertEqual(self.native.call_count, calls)

    def test_plan_error_is_per_item_not_whole_batch(self):
        spec = _spec()
        planned = [dict(spec), {'plan_error': 'ValueError: 未找到物件'},
                   dict(spec)]
        items = [{'request_id': 'e%d' % i} for i in range(3)]
        out = self.begin_batch(items, planned)
        self.assertEqual(out['results'][0]['status'], 'pending')
        self.assertEqual(out['results'][1]['status'], 'error')
        self.assertIn('未找到物件', out['results'][1]['error'])
        self.assertEqual(out['results'][2]['status'], 'pending')
        self.assertEqual(out['registered'], 2)

    def test_role_items_carry_their_own_position(self):
        s1 = _spec(item=101001, ct=101001, pos=(1.0, 2.0, 3.0))
        s2 = _spec(item=101001, ct=101001, pos=(4.0, 5.0, 6.0))
        self.begin_batch([{'request_id': 'r0'}, {'request_id': 'r1'}], [s1, s2])
        self.assertEqual(self.ui._add_unit_server.call_args_list[0],
                         (([101001],), {'pos': (1.0, 2.0, 3.0)}))
        self.assertEqual(self.ui._add_unit_server.call_args_list[1],
                         (([101001],), {'pos': (4.0, 5.0, 6.0)}))

    def test_blockers_refuse_before_any_registration(self):
        spec = _spec()
        placement.begin(self.scene, None, {'request_id': 'old'}, dict(spec),
                        self.room, self.cm, self.ui)
        with self.assertRaises(ValueError) as cm:
            self.begin_batch([{'request_id': 'new'}], [dict(spec)])
        self.assertIn('old', str(cm.exception))
        self.assertEqual(self.ui._add_unit_server.call_count, 1)

    # ------------------------------------------------------------------
    # 2026-10-02 首次实机全量（1.5 万件）暴露的两个池化缺陷（HANDOVER_BATCH_DEFECTS §3）
    # ------------------------------------------------------------------
    def _spawn_logic(self, uid, item=301001, gid=123, pending=True,
                     create_units=True):
        """逻辑单元落地形态：`UgcLogicUnit`。

        pending=True = 在 `del_unit_uids['unit']` 里（服务端新建未确认）。
        create_units=False 复现实机批量形态：批量登记时 `ui.ugc_params['create_units']`
        **只留最后一个新建单元**（实机连拍：7 件里 6 件 cu=False）—— 判据不许用它。
        """
        unit = UgcLogicUnit(uid, item)
        comp = NS(_ugc_id=gid, _owner_uid=unit.uid, _owner=unit)
        self.cm._ugc_id2component[gid] = comp
        if pending:
            self.room.del_unit_uids['unit'].append(unit.uid)
        if create_units:
            self.ui.ugc_params['create_units'].append(unit)
            self.ui.ugc_params['create_components'].append(comp)
        self.units.append(unit)
        return unit, comp

    def test_pending_logic_unit_is_finalized_like_the_single_path(self):
        """缺陷 B（核心实锤）：逻辑单元上的实体必须走 `_finalize_unit` 施加变换。

        run-1 的 7 个方块在 progress 里是 placed、实况却全是 `[0,-65536,0]`：
        owner 是 `UgcLogicUnit` ⇒ 旧实现按类名一律判 'global' ⇒ 走
        `_confirm_global_component`（**从不施加变换**）。

        ⚠️ 这里刻意用**批量真实形态**：单元在 `del_unit_uids['unit']` 里、
        **不在** `create_units` 里（实机 7 件批量里 6 件如此）。照抄单笔路径
        「del_bag 且 create_units」的判据会让这 6 件继续走 global —— 那正是
        第一版修复没修好、靠实机连拍才抓到的原因（probe/live/probe_window.py）。
        """
        spec = _spec(item=301001, ct=100001, pos=(928.0, -800.0, -99.98))
        self.begin_batch([{'request_id': 'geo'}], [dict(spec)])
        unit, _comp = self._spawn_logic(1000078, item=301001, gid=123,
                                        create_units=False)
        out = self.poll_batch(['geo'])
        row = out['results'][0]
        self.assertEqual(row['status'], 'placed')
        self.assertEqual(row['ugc_id'], 123)
        self.assertEqual(self.scene._apply_full_transform.call_count, 1,
                         '逻辑单元上的实体必须走 _finalize_unit（施加变换）；'
                         '只在 del_unit_uids 里、不在 create_units 里也算')
        self.assertEqual(unit._applied['position'], [928.0, -800.0, -99.98],
                         '回执 placed 就必须真的落在请求坐标上')

    def test_plain_new_entity_in_del_bag_but_not_create_units(self):
        """普通实体（如角色）同样只认 `del_unit_uids['unit']`。

        批量登记时 `create_units` 只留最后一个 ⇒ 若把 create_units 加进判据，
        同一批里除最后一件外的角色会全部落到"不在待确认态"而不被认领。
        """
        spec = _spec(item=101001, ct=101001, pos=(1.0, 2.0, 3.0))
        self.begin_batch([{'request_id': 'role'}], [dict(spec)])
        unit = NS(uid=101, ugc_type=101001,
                  hide_collision_pos=lambda v: None)   # 角色确认要取消半隐藏态
        comp = NS(_ugc_id=88, _owner_uid=101, _owner=unit)
        self.cm._ugc_id2component[88] = comp
        self.room.del_unit_uids['unit'].append(101)      # 只有待确认袋
        self.units.append(unit)
        out = self.poll_batch(['role'])
        self.assertEqual(out['results'][0]['status'], 'placed')
        self.assertEqual(self.scene.apply_role_transform.call_count, 1)

    def test_new_logic_unit_outside_pending_state_stays_global(self):
        """判据的边界：不在待确认袋的新逻辑单元仍走全局组件路径，不许误判成实体。

        （位置真值在 component_info，`_apply_full_transform` 对它没有意义。）
        """
        spec = _spec(item=203912, ct=203912)
        self.begin_batch([{'request_id': 'mp'}], [dict(spec)])
        self._spawn_logic(1000099, item=203912, gid=55, pending=False,
                          create_units=False)
        out = self.poll_batch(['mp'])
        self.assertEqual(out['results'][0]['status'], 'placed')
        self.assertEqual(out['results'][0]['ugc_id'], 55)
        self.assertEqual(self.scene._apply_full_transform.call_count, 0)

    def test_stale_settled_receipt_is_not_an_idempotent_hit(self):
        """缺陷 D（2026-10-02 实机，静默丢件）：旧 run 的 placed 留痕被当幂等命中。

        账本/留痕表是 room 级的，而 token = 快照内容派生的 run id + 位次 ⇒ 同一个
        room 会话里**换一份 progress 重跑**时，位次相同的请求会命中旧 run 的回执：
        回执说 placed、图上却一件没有（实机 i=0..99 全部如此，100 件从交付里消失，
        进度里只剩 13 个过期的 ugc_id）。幂等命中必须回读组件表。
        """
        spec = _spec()
        self.begin_batch([{'request_id': 'd0'}], [dict(spec)])
        self.spawn(101)
        self.poll_batch(['d0'])
        st = placement._state(self.room)
        self.assertTrue('d0' in st['settled'] or 'd0' in st['requests'],
                        'placed 之后回执要在账上（留痕或账本）')
        self.assertEqual(self.ui._add_unit_server.call_count, 1)
        # 旧 run 的组件在当前图上已经不存在（被清理/被删）
        self.cm._ugc_id2component.pop(101, None)
        out = self.begin_batch([{'request_id': 'd0'}], [dict(spec)])
        self.assertEqual(out['registered'], 1,
                         '回执指向的组件不在了 ⇒ 必须重新登记，不许假装建过')
        self.assertEqual(self.ui._add_unit_server.call_count, 2)

    def test_live_settled_receipt_is_still_idempotent(self):
        """边界：组件还在图上时，同一 token 仍然是幂等命中（绝不重发引擎）。"""
        spec = _spec()
        self.begin_batch([{'request_id': 'd1'}], [dict(spec)])
        self.spawn(102)
        self.poll_batch(['d1'])
        out = self.begin_batch([{'request_id': 'd1'}], [dict(spec)])
        self.assertEqual(out['registered'], 0)
        self.assertEqual(out['results'][0]['status'], 'placed')
        self.assertEqual(out['results'][0]['ugc_id'], 102)
        self.assertEqual(self.ui._add_unit_server.call_count, 1)

    def test_pending_request_is_never_dropped_as_stale(self):
        """边界：还没定论（没有 result）的请求**不许**被当成过期回执重发。"""
        spec = _spec()
        self.begin_batch([{'request_id': 'd2'}], [dict(spec)])
        out = self.begin_batch([{'request_id': 'd2'}], [dict(spec)])
        self.assertEqual(out['registered'], 0, '在途请求绝不重发')
        self.assertEqual(self.ui._add_unit_server.call_count, 1)

    def test_shared_new_unit_hosts_many_components_each_claimable(self):
        """缺陷 A（实机 54 placed / 46 pending）：同一新宿主上的多件必须一轮全认领。

        49 件 203911 运动器全挂在**同一个**新建 UgcGlobalUnit 上；旧实现把"新单元"
        的认领键写成 unit.uid ⇒ 49 条池记录同键 ⇒ `_claim` 每轮只放行 1 件、
        其余件在单次批量 poll 里永远认领不到（每轮 1 件 + 150ms 退避 = run-1 的 9.6s）。
        认领键改成组件自己的 ugc_id 后，按创建序 FIFO 一次配对完。
        """
        spec = _spec(item=203911, ct=203911)
        self.begin_batch([{'request_id': 'sh%d' % i} for i in range(3)],
                         [dict(spec)] * 3)
        host = UgcGlobalUnit(1000077, 203911)
        # 宿主是服务端本次建的：在 begin 的 before 快照之后出现，且在 create_units 里
        self.ui.ugc_params['create_units'].append(host)
        for gid in (119, 120, 121):
            self.spawn_global_comp(gid, host)
        out = self.poll_batch(['sh0', 'sh1', 'sh2'])
        rows = out['results']
        self.assertEqual([r['status'] for r in rows], ['placed'] * 3,
                         '同一宿主单元上的 3 件必须一轮全认领（旧实现只认领 1 件）')
        self.assertEqual([r['ugc_id'] for r in rows], [119, 120, 121],
                         '认领键 = 组件 ugc_id ⇒ 按创建序 FIFO 配对')
        self.assertEqual(out['unsettled'], 0)


class BatchPropsOpTests(unittest.TestCase):
    """op_set_component_info_batch：逐件护栏 + 逐件升级重试（护栏 8）。"""

    def setUp(self):
        self.comp = NS(COMPONENT_TYPE=100001, _ugc_id=118,
                       component_info={'events': []})
        self.comp.update_component_info = lambda events: \
            self.comp.component_info.update(events=[dict(e) for e in events])
        self.unit = NS(uid=1000074)
        self.edit = NS(reg_change=Mock(return_value=True))
        self.events = [{'type': 10000140001, 'info': 1}]

    def _core(self, item):
        with patch.object(ops, 'find_target',
                          return_value=(self.comp, self.unit, None)), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            return ops._set_component_info_core(self.edit, item)

    def test_guardrail_8_escalation_is_per_item(self):
        """「不在编辑器登记集合」只放行命中那一件（补 allow_unregistered 重试），
        别件不带 allow_unregistered；其它错误一律原样逐件返回。"""
        calls = []

        def fake_find(edit, args):
            calls.append(dict(args))
            if args.get('ugc_id') == 1 and not args.get('allow_unregistered'):
                return None, None, ('物件不在编辑器登记集合内（可能尚未完成放置确认）；'
                                    '骨架自带的全局单例请显式传 allow_unregistered=true')
            if args.get('ugc_id') == 3:
                return None, None, '组件类型不符：期望 9，实际 8'
            return self.comp, self.unit, None

        with patch.object(ops, 'find_target', side_effect=fake_find), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            r = ops.op_set_component_info_batch(self.edit, {'items': [
                {'ugc_id': 1, 'events': self.events},
                {'ugc_id': 2, 'events': self.events},
                {'ugc_id': 3, 'events': self.events,
                 'expect_component_id': 9}]})
        self.assertTrue(r['ok'])
        rows = r['results']
        self.assertTrue(rows[0]['ok'])
        self.assertEqual(rows[0].get('escalated'), 'allow_unregistered',
                         '命中护栏文案的那一件要升级重试成功')
        self.assertTrue(rows[1]['ok'])
        self.assertNotIn('escalated', rows[1], '别件不许被连坐放行')
        self.assertFalse(rows[2]['ok'])
        self.assertIn('组件类型不符', rows[2]['error'])
        self.assertNotIn('escalated', rows[2])
        # 第 1 件恰好调了两次（第二次才带 allow_unregistered），第 2 件一次
        self.assertEqual(len([c for c in calls if c['ugc_id'] == 1]), 2)
        self.assertTrue(calls[1]['allow_unregistered'])
        self.assertEqual(len([c for c in calls if c['ugc_id'] == 2]), 1)

    def test_batch_op_never_returns_top_level_error_key(self):
        """dispatch 契约：返回字典带 "error" 键会被整批当失败（BGM 链路同款坑）。
        逐件失败必须留在 results[i]['error']。"""
        with patch.object(ops, 'find_target',
                          return_value=(None, None, '组件索引中没有 ugc_id=9')), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()):
            r = ops.op_set_component_info_batch(self.edit, {'items': [
                {'ugc_id': 9, 'events': self.events}]})
        self.assertNotIn('error', r)
        self.assertTrue(r['ok'])
        self.assertFalse(r['results'][0]['ok'])

    def test_batch_op_counts_and_payload_guard(self):
        with self.assertRaises(ValueError):
            ops.op_set_component_info_batch(self.edit, {'items': []})
        with self.assertRaises(ValueError):
            ops.op_set_component_info_batch(
                self.edit, {'items': [{'ugc_id': 1, 'events': []}] * 201})


class TestOpsAreRegistered(unittest.TestCase):
    def test_batch_ops_are_in_the_dispatch_table(self):
        import fuxiang_edit as fe
        for name in ('place_batch', 'place_poll_batch',
                     'set_component_info_batch'):
            self.assertIn(name, fe.OPS, 'op 必须注册进 dispatch 表')


class RestoreBatchPathTests(unittest.TestCase):
    """CLI 批量路径（--placement-batch / --props-batch，默认 0=关闭）。"""

    OLD_REC_KEYS = {"i", "token", "status", "old_g", "ugc_id", "item_id",
                    "run", "map", "poll_tries", "t"}

    def _snap_file(self, tmp, n=5, with_events=False):
        events = [{'type': 10000040001, 'info': 1}] if with_events else []
        comps = [{"g": g, "i": 202009, "p": [float(g), 0.0, 0.0],
                  "r": [0, 0, 0], "s": [1, 1, 1],
                  "ci": json.dumps({"component_id": 100000, "events": events})}
                 for g in range(1, n + 1)]
        snap = {"format": "fuxiang-snapshot", "version": 1, "captured_at": "t",
                "map": {"name": "m", "framework_id": "4002",
                        "map_setting": {"name": "别的图"}, "anchor_info": {}},
                "components": comps, "events": {"count": 0, "events": [],
                                                "saved_keys": []},
                "story": {"stories": []},
                "budget": {"component_nums": n, "registered": n,
                           "max_component_nums": 15000, "occupancy": n,
                           "max_occupancy": 30000}}
        path = os.path.join(tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        return path

    def _fake_call(self, calls, fail_batch_at=None, fail_index=None,
                   raw_batch=None):
        def fake(op, args=None, timeout=90):
            calls.append((op, args))
            if op == "place_batch":
                if raw_batch is not None:
                    # 模拟 2026-10-02 run-2 的实机形态：op 抛异常 / 失败信封
                    # （调用方只看到一个"缺 results 的普通 dict"）
                    return dict(raw_batch)
                if fail_batch_at is not None and \
                        len([c for c in calls if c[0] == "place_batch"]) \
                        >= fail_batch_at:
                    raise RuntimeError("批量摆放请求失败（测试注入）")
                rows = []
                for idx, it in enumerate(args["items"]):
                    rows.append({"index": idx,
                                 "request_id": it["request_id"],
                                 "status": "pending"})
                return {"results": rows, "registered": len(rows),
                        "poll_after_ms": 0}
            if op == "place_poll_batch":
                rows = []
                for token in args["request_ids"]:
                    i = int(token.rsplit("_", 1)[1])
                    if fail_index is not None and i == fail_index:
                        rows.append({"request_id": token,
                                     "status": "finalize_failed",
                                     "error": "注入失败"})
                    else:
                        rows.append({"request_id": token, "status": "placed",
                                     "ugc_id": 5000 + i})
                return {"results": rows,
                        "placed": sum(1 for r in rows
                                      if r["status"] == "placed"),
                        "unsettled": sum(1 for r in rows
                                         if r["status"] != "placed"),
                        "poll_after_ms": 0}
            if op == "set_component_info_batch":
                rows = [{"index": i, "ugc_id": it.get("ugc_id"), "ok": True}
                        for i, it in enumerate(args["items"])]
                return {"ok": True, "results": rows,
                        "counts": {"total": len(rows), "ok": len(rows),
                                   "failed": 0}}
            if op == "set_component_info":
                return {"ok": True, "ugc_id": (args or {}).get("ugc_id")}
            if op == "place_item":
                i = int(str(args["request_id"]).rsplit("_", 1)[1])
                if fail_index is not None and i == fail_index:
                    return {"status": "finalize_failed", "error": "注入失败"}
                return {"status": "placed", "ugc_id": 5000 + i}
            if op == "eval":
                return {"result": {}}
            return {"ok": True}
        return fake

    def _run(self, n, placement_batch=0, props_batch=0, with_events=False,
             fail_batch_at=None, fail_index=None, a_batch=50, raw_batch=None):
        calls = []
        saves = []
        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp, n=n, with_events=with_events)
            prog = os.path.join(tmp, "p.jsonl")
            old = (sr.fetch_live_index, sr.call_edit, sr.save_and_check,
                   sr.fetch_target_info, sr.landing_audit,
                   sr.fetch_placeable_types)
            sr.fetch_live_index = lambda: ({}, 0)
            sr.call_edit = self._fake_call(calls, fail_batch_at=fail_batch_at,
                                           fail_index=fail_index,
                                           raw_batch=raw_batch)
            sr.save_and_check = lambda token, **k: (
                saves.append(token), {"submitted": True})[1]
            sr.fetch_target_info = lambda: {"framework": "4002",
                                            "map_setting": {"name": "m2"},
                                            "bounds": None, "budget": None}
            sr.landing_audit = lambda *a, **k: {"checked": 0, "off": [],
                                                "unreadable": 0}
            sr.fetch_placeable_types = lambda: {100000}
            argv = [path, "--apply", "--yes", "--progress", prog,
                    "--no-events", "--batch", str(a_batch)]
            if placement_batch:
                argv += ["--placement-batch", str(placement_batch)]
            if props_batch:
                argv += ["--props-batch", str(props_batch)]
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sr.main(argv)
            finally:
                (sr.fetch_live_index, sr.call_edit, sr.save_and_check,
                 sr.fetch_target_info, sr.landing_audit,
                 sr.fetch_placeable_types) = old
            recs = []
            if os.path.exists(prog):
                with open(prog, encoding="utf-8") as f:
                    recs = [json.loads(x) for x in f.read().splitlines() if x]
        return rc, recs, calls, saves, buf.getvalue()

    # ---- 护栏 5：逐件 progress/idmap 记录格式不变 ----
    def test_guardrail_5_batch_records_match_old_format(self):
        rc_new, recs_new, calls_new, _s, out = self._run(5, placement_batch=2)
        self.assertEqual(rc_new, 0, msg=out)
        rc_old, recs_old, calls_old, _s2, out2 = self._run(5, placement_batch=0)
        self.assertEqual(rc_old, 0, msg=out2)
        self.assertEqual({r["i"] for r in recs_new}, {r["i"] for r in recs_old})
        for r in recs_new:
            self.assertEqual(set(r), self.OLD_REC_KEYS)
        for r in recs_old:
            self.assertEqual(set(r), self.OLD_REC_KEYS)
        # 批量路径的往返按批摊薄：5 件、批 2 ⇒ place_batch 3 次（旧路径 place_item 5 次）
        self.assertEqual(len([c for c in calls_new if c[0] == "place_batch"]), 3)
        self.assertEqual(len([c for c in calls_new if c[0] == "place_item"]), 0)
        self.assertEqual(len([c for c in calls_old if c[0] == "place_item"]), 5)
        # id_map 一致（同 token/同序）
        self.assertEqual({r["old_g"]: r["ugc_id"] for r in recs_new if r["ugc_id"]},
                         {r["old_g"]: r["ugc_id"] for r in recs_old
                          if r["ugc_id"]})

    # ---- 护栏 6：没试与试了失败仍要分开 ----
    def test_guardrail_6_untried_batch_writes_no_progress(self):
        rc, recs, calls, saves, out = self._run(
            6, placement_batch=2, fail_batch_at=2)
        self.assertEqual(rc, 1)
        self.assertIn("没试", out)
        # 只有第 1 批（2 件）试过并落了进度；第 2 批起的 4 件没有任何记录
        self.assertEqual({r["i"] for r in recs}, {0, 1})
        self.assertEqual(len([c for c in calls if c[0] == "place_batch"]), 2,
                         '失败后就停，不许再开新批')

    def test_guardrail_6_tried_failure_is_recorded_per_item(self):
        rc, recs, _c, _s, out = self._run(
            3, placement_batch=3, fail_index=1)
        self.assertEqual(rc, 0, msg=out)
        by_i = {r["i"]: r["status"] for r in recs}
        self.assertEqual(by_i[0], "placed")
        self.assertEqual(by_i[1], "finalize_failed", '试了失败要逐件留痕')
        self.assertEqual(by_i[2], "placed")

    # ---- 缺陷 C：失败行必须带上原因（否则现场只剩"失败了 N 件"这个数字）----
    def test_failed_progress_row_carries_the_reason(self):
        """2026-10-02 run-2：100 条 `status:"exception"` 的进度行没有任何错误文本，
        为什么失败全丢了。非 placed 行现在带 `error`；placed 行**一个键都不加**
        （格式被断点续做/--check-progress/护栏 5 锁着）。"""
        _rc, recs, _c, _s, _out = self._run(
            3, placement_batch=3, fail_index=1)
        by_i = {r["i"]: r for r in recs}
        self.assertEqual(by_i[1]["status"], "finalize_failed")
        self.assertEqual(by_i[1]["error"], "注入失败")
        self.assertNotIn("error", by_i[0], "placed 行的键集合不许变（护栏 5）")
        self.assertNotIn("error", by_i[2])
        self.assertEqual(set(by_i[0]), self.OLD_REC_KEYS)

    def test_single_path_failure_row_carries_the_reason_too(self):
        _rc, recs, _c, _s, _out = self._run(3, placement_batch=0, fail_index=1)
        by_i = {r["i"]: r for r in recs}
        self.assertEqual(by_i[1]["status"], "finalize_failed")
        self.assertEqual(by_i[1]["error"], "注入失败")

    def test_missing_reason_is_stated_not_silent(self):
        self.assertIsNone(sr.failure_reason("placed", {}),
                          "placed 行不加 error 键")
        self.assertEqual(sr.failure_reason("exception", {"error": "boom"}), "boom")
        self.assertEqual(sr.failure_reason("pending", {"note": "池中暂无候选"}),
                         "池中暂无候选")
        self.assertIn("没有原因字段",
                      sr.failure_reason("exception", {}))

    def test_batch_without_results_is_untried_not_exceptions(self):
        """缺陷 C 的实机形态：护栏拒绝整批时 op 抛异常／回失败信封，调用方只看到
        一个"缺 results 的普通 dict"，于是 100 件被逐件记成 exception 且没有原因
        （还把口径污染成"试了失败"）。整批登记失败 = **没试**：不写进度、停下、报原因。
        """
        rc, recs, _c, _s, out = self._run(
            3, placement_batch=3,
            raw_batch={"ok": False,
                       "error": "存在 46 条不属于本批的未定论摆放请求"})
        self.assertEqual(rc, 1)
        self.assertEqual(recs, [], "整批没试：一行进度都不许写（护栏 6）")
        self.assertIn("没试", out)
        self.assertIn("46 条", out)

    # ---- 护栏 7：保存节奏（每批一次，末批照旧）----
    def test_guardrail_7_save_cadence_follows_the_batch(self):
        _rc, _recs, _calls, saves, _out = self._run(
            5, placement_batch=2, a_batch=2)
        # 5 件、批 2：批 1 后存、批 2 后存、末批 1 件后存（final）⇒ 摆放侧 3 次
        # （属性阶段的收尾保存另计）
        self.assertEqual(len([s for s in saves if "props" not in s]), 3)
        self.assertTrue(all(s.startswith("rs_save_") for s in saves))

    def test_default_batch_flags_are_off(self):
        import inspect
        src = inspect.getsource(sr.main)
        self.assertIn('"--placement-batch"', src)
        self.assertIn('"--props-batch"', src)
        self.assertIn('default=0', src)

    def test_out_of_range_flags_are_refused_before_touching_the_game(self):
        with patch.object(sr, "call_edit") as m:
            rc, *_ = self._run(1, placement_batch=129)
            self.assertEqual(rc, 2)
            rc, *_ = self._run(1, props_batch=201)
            self.assertEqual(rc, 2)
            self.assertEqual(m.call_count, 0)

    # ---- 批量属性回写路径 ----
    def test_props_batch_sends_chunks_and_keeps_payload_shape(self):
        rc, recs, calls, _s, out = self._run(
            5, placement_batch=3, props_batch=2, with_events=True)
        self.assertEqual(rc, 0, msg=out)
        batch_calls = [c for c in calls if c[0] == "set_component_info_batch"]
        self.assertEqual(len(batch_calls), 3, '5 袋、批 2 ⇒ 3 条命令')
        for _op, args in batch_calls:
            for item in args["items"]:
                self.assertIn("remap", item)
                self.assertIn("remap_properties", item)
                self.assertIn("events", item)
        self.assertIn("属性回写：成功 5 / 失败 0", out)

    def test_props_batch_failure_is_not_silently_green(self):
        def fake(op, args=None, timeout=90):
            if op == "set_component_info_batch":
                rows = [{"index": i, "ugc_id": it.get("ugc_id"),
                         "ok": i != 0,
                         **({} if i == 0 else {}),
                         **({"error": "注入失败"} if i == 0 else {})}
                        for i, it in enumerate(args["items"])]
                return {"ok": True, "results": rows,
                        "counts": {"total": len(rows), "ok": len(rows) - 1,
                                   "failed": 1}}
            if op == "place_batch":
                rows = [{"index": i, "request_id": it["request_id"],
                         "status": "pending"}
                        for i, it in enumerate(args["items"])]
                return {"results": rows, "registered": len(rows),
                        "poll_after_ms": 0}
            if op == "place_poll_batch":
                rows = [{"request_id": t, "status": "placed",
                         "ugc_id": 5000 + int(t.rsplit("_", 1)[1])}
                        for t in args["request_ids"]]
                return {"results": rows, "placed": len(rows), "unsettled": 0,
                        "poll_after_ms": 0}
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            path = self._snap_file(tmp, n=2, with_events=True)
            prog = os.path.join(tmp, "p.jsonl")
            old = (sr.fetch_live_index, sr.call_edit, sr.save_and_check,
                   sr.fetch_target_info, sr.landing_audit,
                   sr.fetch_placeable_types)
            sr.fetch_live_index = lambda: ({}, 0)
            sr.call_edit = fake
            sr.save_and_check = lambda token, **k: {"submitted": True}
            sr.fetch_target_info = lambda: {"framework": "4002",
                                            "map_setting": {"name": "m2"},
                                            "bounds": None, "budget": None}
            sr.landing_audit = lambda *a, **k: {"checked": 0, "off": [],
                                                "unreadable": 0}
            sr.fetch_placeable_types = lambda: {100000}
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sr.main([path, "--apply", "--yes", "--progress", prog,
                                  "--no-events", "--placement-batch", "2",
                                  "--props-batch", "2"])
            finally:
                (sr.fetch_live_index, sr.call_edit, sr.save_and_check,
                 sr.fetch_target_info, sr.landing_audit,
                 sr.fetch_placeable_types) = old
        out = buf.getvalue()
        self.assertEqual(rc, 0, msg=out)
        self.assertIn("属性回写：成功 1 / 失败 1", out)
        self.assertIn("补做", out)


if __name__ == "__main__":
    unittest.main()
