"""Regressions for delayed responses, retries and native finalization."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import fuxiang_placement as placement
import fuxiang_scene as scene


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.cm = NS(_ugc_id2component={})
        self.room = NS(del_unit_uids={'unit': []})
        self.ui = NS(ugc_params={'create_units': [], 'create_components': []},
                     all_ugc_uid_set=set(), _add_unit_server=Mock(),
                     get_all_ugc_component_nums=lambda: len(self.ui.all_ugc_uid_set),
                     get_current_occupancy=lambda: len(self.ui.all_ugc_uid_set)*4)
        self.spec = {'item_id': 202009, 'component_type': 100000,
                     'position': [626, 0, -100], 'rotation': [0, 0, 0], 'scale': [1, 1, 1]}
        self.units = []
        self.native = Mock(side_effect=self.confirm)
        self.scene = NS(SERVER_COMPONENT_TYPES=scene.SERVER_COMPONENT_TYPES,
                        ROLE_COMPONENT_TYPES=scene.ROLE_COMPONENT_TYPES,
                        GROUP_CREATE_TYPES=scene.GROUP_CREATE_TYPES,
                        context=lambda _: (self.room, self.cm), get_edit_ui=lambda _: self.ui,
                        _apply_full_transform=Mock(side_effect=self.record_transform),
                        apply_role_transform=Mock(),
                        role_position=lambda u: list(self.spec['position']),
                        command_create=self.native,
                        transform=lambda u: getattr(
                            u, '_applied',
                            {k: list(self.spec[k]) for k in ('position', 'rotation', 'scale')}),
                        unregister_create=scene.unregister_create)

    def begin(self, token='a'):
        return placement.begin(self.scene, None, {'request_id': token}, self.spec,
                               self.room, self.cm, self.ui)

    def poll(self, token='a'):
        return placement.poll(self.scene, None, {'request_id': token})

    def spawn(self, uid=101):
        unit = NS(uid=uid, ugc_type=self.spec['item_id'])
        comp = NS(_ugc_id=uid, _owner_uid=uid, _owner=unit)
        self.cm._ugc_id2component[uid] = comp
        self.room.del_unit_uids['unit'].append(uid)
        self.ui.ugc_params['create_units'].append(unit)
        self.ui.ugc_params['create_components'].append(comp)
        self.units.append(unit)
        return unit

    def record_transform(self, unit, tr):
        """记住套到该实体上的变换，供 transform 回读。

        成组创建要**逐件**校验落点，回读必须跟着各件的变换走，
        不能永远返回主件的 spec（那会把"两件叠在同一处"看成成功）。
        """
        unit._applied = {k: list(tr[k]) for k in ('position', 'rotation', 'scale')}

    def confirm(self, ui, unit, pos):
        ui.all_ugc_uid_set.add(unit.uid)
        self.room.del_unit_uids['unit'].remove(unit.uid)
        return True, None

    def test_role_create_carries_position_at_send(self):
        """角色（101001/102001）只有「创建那一步」带坐标才会落库（EDIT_OPS.md §11.1）。

        常规调用不带 pos，服务端建出来的角色会停在哨兵位 [0,-65536,0] ——
        摆放算「成功」但图是错的，所以这条要锁死。
        """
        self.spec.update({'item_id': 101001, 'component_type': 101001,
                          'position': [919.0, 62.201, -282.048]})
        self.assertEqual(self.begin()['status'], 'pending')
        self.ui._add_unit_server.assert_called_once_with(
            [101001], pos=(919.0, 62.201, -282.048))

    def test_non_role_create_still_sends_without_position(self):
        """普通件不变：不带 pos。改造半径只限角色类，别碰 90 类的既有实测路径。"""
        self.begin()
        self.ui._add_unit_server.assert_called_once_with([202009])

    def test_group_create_adopts_every_new_unit(self):
        """成组创建（204041 推荐检查点）：一次 add_unit 出 2 个实体 ⇒ 整组认领。

        实机 2026-10-01：候选数≠1 时旧代码一律判 ambiguous，那件永远摆不出来。
        锁两件事：① 两件都确认、回执给出 group_ugc_ids；② 各件拿自己的落点，
        第二件不许跟着主件叠在同一处（那会还原出一个"重叠检查点"）。
        """
        self.spec.update({'item_id': 204041, 'component_type': 204041,
                          'position': [936.099, 61.745, -232.759],
                          'rotation': [0.0, 37.39, 0.0],
                          'group_transforms': [{'position': [927.464, 162.066, -210.032],
                                                'rotation': [0.0, 0.0, 0.0],
                                                'scale': [1.0, 1.0, 1.0]}]})
        self.begin()
        for uid in (1014629, 1014630):
            self.spawn(uid=uid)
        out = self.poll()
        self.assertEqual(out['status'], 'placed')
        self.assertEqual(out['group_ugc_ids'], [1014629, 1014630])
        self.assertEqual(out['transform']['position'], [936.099, 61.745, -232.759])
        self.assertEqual([u._applied['position'] for u in self.units],
                         [[936.099, 61.745, -232.759], [927.464, 162.066, -210.032]])

    def test_group_create_falls_back_to_single_when_short(self):
        """只冒出 1 个实体时按单件确认，别编造 group_ugc_ids。

        回执里没有成员 id ⇒ 还原侧那个成员进不了 id_map，对账时会如实报缺件；
        比"假装成组成功、把两件叠一处"更可查。
        """
        self.spec.update({'item_id': 204041, 'component_type': 204041})
        self.begin()
        self.spawn(uid=1014629)
        out = self.poll()
        self.assertEqual(out['status'], 'placed')
        self.assertNotIn('group_ugc_ids', out)

    def test_role_confirm_uses_position_only_flow(self):
        """角色确认走「只改位置 + 取消半隐藏」，不套 _apply_full_transform。"""
        self.spec.update({'item_id': 101001, 'component_type': 101001,
                          'position': [919.0, 62.201, -282.048]})
        self.begin()
        unit = self.spawn(uid=245)
        unit.hide_collision_pos = Mock()
        result = self.poll()
        self.assertEqual(result['status'], 'placed')
        self.scene._apply_full_transform.assert_not_called()
        self.scene.apply_role_transform.assert_called_once()
        unit.hide_collision_pos.assert_called_once_with(False)

    def test_delayed_creation_and_idempotent_poll(self):
        with patch('time.sleep', side_effect=AssertionError('Main thread slept')):
            self.assertEqual(self.begin()['status'], 'pending')
            self.assertEqual(self.poll()['status'], 'pending')
            unit = self.spawn()
            result = self.poll()
            self.assertEqual(result['status'], 'placed')
            self.assertFalse(result['persistence_verified'])
            self.assertTrue(result['editor_registered'])
            self.assertEqual(self.poll(), result)
            self.assertEqual(self.begin(), result)
            self.ui._add_unit_server.assert_called_once()
            self.native.assert_called_once()
            self.assertFalse(self.ui.ugc_params['create_units'])
            self.assertTrue(unit._fuxiang_local_item['server_created'])

    def test_retries_and_reload_do_not_resend(self):
        self.begin()
        import importlib
        importlib.reload(placement)
        self.begin()
        with self.assertRaises(ValueError): self.begin('b')
        self.ui._add_unit_server.assert_called_once()

    def test_overdue_request_can_complete_later(self):
        self.begin()
        self.room._fuxiang_placements['requests']['a']['started'] -= 181
        self.assertEqual(self.poll()['status'], 'overdue')
        with self.assertRaises(ValueError): self.begin('b')
        self.spawn()
        self.assertEqual(self.poll()['status'], 'placed')
        self.ui._add_unit_server.assert_called_once()

    def test_ambiguous_and_manual_units_remain_untouched(self):
        self.begin()
        first = self.spawn()
        self.spawn(102)
        self.assertEqual(self.poll()['status'], 'ambiguous')
        self.native.assert_not_called()
        self.scene._apply_full_transform.assert_not_called()
        del self.cm._ugc_id2component[102]
        self.assertEqual(self.poll()['status'], 'ambiguous')
        self.native.assert_not_called()

    def test_preexisting_orphan_is_not_adopted(self):
        self.spawn()
        self.begin()
        self.assertEqual(self.poll()['status'], 'pending')
        self.native.assert_not_called()

    def test_already_confirmed_unit_is_not_adopted(self):
        self.begin()
        unit = self.spawn()
        self.ui.all_ugc_uid_set.add(unit.uid)
        self.assertEqual(self.poll()['status'], 'conflict')
        self.native.assert_not_called()

    def test_partial_native_failure_is_not_reexecuted(self):
        self.begin()
        self.spawn()
        self.native.side_effect = RuntimeError('partial execution')
        self.assertEqual(self.poll()['status'], 'finalize_failed')
        self.assertEqual(self.poll()['status'], 'finalize_failed')
        self.native.assert_called_once()
        with self.assertRaises(ValueError): self.begin('b')

    def test_changed_context_and_unknown_request_fail_closed(self):
        with self.assertRaises(ValueError): self.poll()
        self.begin()
        self.scene.get_edit_ui = lambda _: NS()
        with self.assertRaises(ValueError): self.poll()

    def test_same_token_different_transform_rejected(self):
        self.begin()
        self.spec = dict(self.spec, position=[1, 2, 3])
        with self.assertRaises(ValueError): self.begin()
        self.ui._add_unit_server.assert_called_once()

    def test_send_exception_does_not_allow_duplicate_request(self):
        self.ui._add_unit_server.side_effect = RuntimeError('network outcome unknown')
        self.assertEqual(self.begin()['status'], 'send_unknown')
        with self.assertRaises(ValueError): self.begin('b')
        self.spawn()
        self.assertEqual(self.poll()['status'], 'placed')

    def test_complex_server_type_rejected_before_sending(self):
        # 2026-09-19：201001(大门) 已随白名单扩容至 76 类而被受理，本用例改用
        # 203013(图片墙)——实机唯一确认不受理的类型，且当前仍在白名单之外。
        # 若日后 203013 被列入白名单，此处会失败，提示改用下一个未受理类型。
        self.spec = dict(self.spec, component_type=203013)
        with self.assertRaises(ValueError): self.begin()
        self.ui._add_unit_server.assert_not_called()

    def test_settled_requests_are_recycled_and_stay_idempotent(self):
        # 根治回归：反复摆放不再撞 MAX_REQUESTS。回收是惰性的——下一次 begin
        # 开头才打扫，所以循环结束后 requests 里最多剩最后一条刚定论的记录。
        total = placement.MAX_REQUESTS + 12
        first = None
        for i in range(total):
            token = 't%03d' % i
            self.assertEqual(self.begin(token)['status'], 'pending')
            self.spawn(uid=200 + i)
            result = self.poll(token)
            self.assertEqual(result['status'], 'placed')
            if i == 0:
                first = result
        state = self.room._fuxiang_placements
        # 惰性回收：下一次 begin 开头才打扫，因此 requests 至多残留 1 条刚定论、
        # 待回收的记录；它不随总调用数增长，MAX_REQUESTS 在实际使用中已不可达。
        self.assertLessEqual(len(state['requests']), 1,
                             'requests 只应残留至多一条待回收记录')
        self.assertTrue('t000' in state['settled'], 't000 应已被回收进留痕表')
        self.assertFalse('t000' in state['requests'])
        self.assertEqual(self.ui._add_unit_server.call_count, total)
        transforms = self.scene._apply_full_transform.call_count
        # 已被回收的编号仍可幂等回查：返回原定论，且绝不重发、绝不重建
        self.assertEqual(self.begin('t000'), first)
        self.assertEqual(self.poll('t000'), first)
        self.assertEqual(self.ui._add_unit_server.call_count, total)
        self.assertEqual(self.scene._apply_full_transform.call_count, transforms)

    def test_settled_table_is_fifo_bounded(self):
        total = placement.MAX_SETTLED + 20
        for i in range(total):
            token = 's%05d' % i
            self.begin(token)
            self.spawn(uid=5000 + i)
            self.poll(token)
        state = self.room._fuxiang_placements
        settled = state['settled']
        self.assertLessEqual(len(settled), placement.MAX_SETTLED)
        # 最新一条尚留在 requests（未触发下一次回收），故倒数第二条才是表中最新
        self.assertTrue('s%05d' % (total - 2) in settled)
        self.assertFalse('s00000' in settled, '最旧的记录应被 FIFO 淘汰')

    def test_unresolved_request_is_never_recycled(self):
        # 未定论请求（含锁定 active 的 finalize_failed）不得被回收，否则会
        # 意外释放「单笔在途」锁、丢掉迟到结果。
        self.begin()
        self.spawn()
        self.native.side_effect = RuntimeError('partial execution')
        self.assertEqual(self.poll()['status'], 'finalize_failed')
        state = self.room._fuxiang_placements
        for _ in range(3):
            with self.assertRaises(ValueError): self.begin('b')
        self.assertIn('a', state['requests'])
        self.assertNotIn('a', state['settled'])
        self.assertEqual(state['active'], 'a')

    def test_correct_cover_preserves_native_item_identity(self):
        self.spec = dict(self.spec, item_id=202014, component_type=202014,
                         catalog_hidden=False, object_list_eligible=True)
        self.begin()
        self.ui._add_unit_server.assert_called_once_with([202014])
        self.spawn()
        result = self.poll()
        self.assertEqual(result['item_id'], 202014)
        self.assertTrue(result['object_list_eligible'])
        self.assertNotIn('object_list_visible', result)
        self.assertFalse(result['persistence_verified'])

    def test_server_unit_cannot_be_locally_destroyed(self):
        unit = NS(uid=101, _fuxiang_local_item={'server_created': True})
        with patch.object(scene, 'owned_item', return_value=(self.room,self.cm,NS(_ugc_id=101),unit)):
            with self.assertRaises(ValueError): scene.remove(None, {'ugc_id':101})

    def test_no_implicit_local_fallback_when_ui_missing(self):
        with patch.object(scene,'plan',return_value=self.spec), \
             patch.object(scene,'context',return_value=(self.room,self.cm)), \
             patch.object(scene,'get_edit_ui',return_value=None), \
             patch.object(scene,'_place_local') as local:
            with self.assertRaises(ValueError): scene.place(None, {'item_id':202009})
            local.assert_not_called()


class GlobalComponentSessionTests(unittest.TestCase):
    """挂在全局单元上的组件（运动器 203911 等）摆完之后，会话必须还能继续摆下一件。

    2026-10-01 春岚实机：restore 一批里运动器 placed 之后，同批下一件（组合件）被
    「前一摆放请求尚未解决」顶掉；根因是 `_confirm_global_component` 只写了 result、
    没同步 req['status']，`_prune` 又"为 in-flight 而保留 active"，于是这把锁永不释放。
    春岚有 332 个运动器，这条会在 MAX_REQUESTS=128 之前就把全量还原拦腰截断。
    """

    def setUp(self):
        self.cm = NS(_ugc_id2component={})
        self.room = NS(del_unit_uids={'unit': []})
        self.ui = NS(ugc_params={'create_units': [], 'create_components': []},
                     all_ugc_uid_set=set(), _add_unit_server=Mock(),
                     _add_change_unit=Mock(),
                     get_all_ugc_component_nums=lambda: len(self.cm._ugc_id2component),
                     get_current_occupancy=lambda: 15007)
        self.scene = NS(SERVER_COMPONENT_TYPES=scene.SERVER_COMPONENT_TYPES,
                        ROLE_COMPONENT_TYPES=scene.ROLE_COMPONENT_TYPES,
                        GROUP_CREATE_TYPES=scene.GROUP_CREATE_TYPES,
                        context=lambda _: (self.room, self.cm),
                        get_edit_ui=lambda _: self.ui,
                        _apply_full_transform=Mock(),
                        command_create=Mock(return_value=(True, None)),
                        transform=lambda u: {'position': [0, 0, 0],
                                             'rotation': None, 'scale': None},
                        register_change=scene.register_change,
                        unregister_create=scene.unregister_create)
        # 既有全局单元（运动器们共用一个），它上面挂一件"旧组件"，让 before_uids 认得它
        self.host = NS(uid=1000057, ugc_type=203911)
        self.cm._ugc_id2component[10] = NS(_ugc_id=10, _owner_uid=1000057, _owner=self.host)
        self.spec = {'item_id': 203911, 'component_type': 203911,
                     'position': [0, 0, 0], 'rotation': [0, 0, 0], 'scale': [1, 1, 1]}

    def begin(self, token):
        return placement.begin(self.scene, None, {'request_id': token}, dict(self.spec),
                               self.room, self.cm, self.ui)

    def poll(self, token):
        return placement.poll(self.scene, None, {'request_id': token})

    def _server_created_new_component(self, gid):
        """服务端只建组件、不建新实体：新 gid 挂在既有全局单元上。"""
        self.cm._ugc_id2component[gid] = NS(_ugc_id=gid, _owner_uid=self.host.uid,
                                            _owner=self.host)

    def test_global_component_releases_the_inflight_lock(self):
        self.assertEqual(self.begin('g1')['status'], 'pending')
        self._server_created_new_component(500)
        out = self.poll('g1')
        self.assertEqual(out['status'], 'placed')
        self.assertEqual(out['entity_mode'], 'global_component')
        # 关键：下一件不许被"前一摆放请求尚未解决"顶掉
        self.assertEqual(self.begin('g2')['status'], 'pending')

    def test_placed_request_is_recycled_and_status_aligned(self):
        self.begin('g1')
        self._server_created_new_component(500)
        self.poll('g1')
        state = placement._state(self.room)
        self.assertIsNone(state['active'])             # 成功回执即释放单笔在途锁
        self.begin('g2')                               # _prune 在 begin 开头跑
        self.assertNotIn('g1', state['requests'])      # 未定论集合里不该再留着它
        self.assertEqual(state['settled']['g1']['status'], 'placed')
        self.assertEqual(state['active'], 'g2')        # 锁换给了新一件
        # 迟到查询走 settled，回执一模一样
        self.assertEqual(self.poll('g1')['ugc_id'], 500)

    def test_unresolved_request_still_blocks_a_second_send(self):
        """护栏本身不许被放宽：真未定论（没有回执）时仍然只许单笔在途。"""
        self.begin('g1')
        with self.assertRaises(ValueError):
            self.begin('g2')


if __name__ == '__main__':
    unittest.main()
