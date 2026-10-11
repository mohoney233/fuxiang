import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import fuxiang_scene as scene
import fuxiang_edit as edit
spec = importlib.util.spec_from_file_location('fxserver', ROOT / 'mcp/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class WorldBattleUGCEdit:
    def create_unit_with_info(self, info, unused):
        pass


WorldBattleUGCEdit.create_unit_with_info.__globals__['UNIT_TYPE_CONF'] = NS(UGC_COM=10541)


def fixture():
    room = WorldBattleUGCEdit()
    cm = NS(_ugc_id2component={113: NS(_owner_uid=1000001)},
            _component_type2ugc_ids={100000: {113}}, _ugc_id_counter=0)
    room.ugc_component_manager = cm
    room.unit_mgr = NS(get_unit=Mock(return_value=None))
    api = NS(get_room=lambda: room, get_item_data=lambda: {
        202009: {'component_id': 100000, 'model_path': 'scene/test.gim', 'zoom_limit': (.1, 1000)},
        201001: {'component_id': 201001, 'model_path': 'scene/generator.gim'},
        # 角色类：服务端受理，但要 allow_unverified，且创建时必须携带 pos
        101001: {'component_id': 101001, 'model_path': 'chr/civilian.gim'},
        # 仍在 UNSUPPORTED_TYPES 里的类型（用于验证"硬拒、allow_unverified 也救不回"）
        203008: {'component_id': 203008, 'model_path': 'ui/panel.gim'},
        # 未出现在任何列表中的假类型，用于验证 allow_unverified 兜底
        299999: {'component_id': 299999, 'model_path': 'scene/unknown.gim'}})
    return api, room, cm


class SceneTests(unittest.TestCase):
    def test_plan_differentiates_item_from_component(self):
        api, room, cm = fixture()
        result = scene.plan(api, {'item_id': '202009', 'scale': 2})
        self.assertEqual((result['item_id'], result['component_type'], result['unit_type']),
                         (202009, 100000, 10541))
        self.assertEqual(result['scale'], [2, 2, 2])
        self.assertEqual(cm._ugc_id_counter, 0)
        self.assertEqual(len(cm._ugc_id2component), 1)

    def test_invalid_transforms_and_unsupported_items(self):
        api, _, _ = fixture()
        cases = [{'position': [1, 2]}, {'position': [1, math.nan, 3]},
                 {'rotation': [False, 0, 0]}, {'scale': 0}, {'scale': .01},
                 {'item_id': 101001},          # 实测不可摆放的角色类
                 {'item_id': 299999},          # 白名单外的未知类型
                 {'unit_type': 10546}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                scene.plan(api, dict({'item_id': 202009}, **args))

    def test_allocator_checks_unindexed_units_and_secondary_component_index(self):
        _, room, cm = fixture()
        room.unit_mgr.get_unit.side_effect = [object(), None]
        cm._component_type2ugc_ids[100000].add(120)
        self.assertEqual(scene.next_unit_id(room, cm), 1000003)
        self.assertEqual(scene.next_component_id(cm), 121)

    def test_removal_rejects_preexisting_components(self):
        api, room, _ = fixture()
        with self.assertRaises(ValueError):
            scene.remove(api, {'ugc_id': 113})

    def test_creation_failure_cleans_up_new_unit(self):
        api, room, cm = fixture()
        units = {}
        def create(info, _):
            units[info['uid']] = NS(uid=info['uid'])
            return units[info['uid']]
        # Keep verified UNIT_TYPE_CONF available on the fake function as well.
        create.__globals__['UNIT_TYPE_CONF'] = NS(UGC_COM=10541)
        room.create_unit_with_info = create
        room.unit_mgr.get_unit = units.get
        room.unit_mgr.remove_unit = units.pop
        cm.create_component_client = Mock(side_effect=RuntimeError('constructor failed'))
        result = scene.place(api, {'mode': 'local_preview', 'item_id': 202009})
        self.assertIn('error', result)
        self.assertTrue(result['cleanup']['complete'])
        self.assertEqual(units, {})
        self.assertEqual(set(cm._ugc_id2component), {113})

    def test_cleanup_reports_failure_without_claiming_rollback(self):
        _, room, cm = fixture()
        room.unit_mgr.get_unit.return_value = object()
        room.unit_mgr.remove_unit = Mock(side_effect=RuntimeError('failed'))
        self.assertFalse(scene.cleanup(room, cm, 1000002, 114)['complete'])


def scene_fixture_with_ui():
    """带 UI mock 的完整夹具：模拟玩家路径下的 ugc_params 与创建接口。

    返回 (api, room, cm, ui)。
    """
    # 离线环境没有游戏内建的 math3d；命令模式需要它
    if 'math3d' not in sys.modules:
        _m = types.ModuleType('math3d')
        def _vec(*a):
            if len(a) == 1:
                o = a[0]
                try:
                    return [o.x, o.y, o.z]
                except Exception:
                    return list(o)
            return list(a)
        _m.vector = _vec
        _m.euler_to_matrix = lambda v: v
        sys.modules['math3d'] = _m

    api, room, cm = fixture()
    cm.save_component_info = []
    cm.is_save_component_info = False
    cm.set_save_component_info = lambda v: setattr(cm, 'is_save_component_info', v)

    def check_and_save(ev):
        if cm.save_component_info or cm.is_save_component_info:
            cm.save_component_info.append(ev)
            return True
        return False
    cm.check_and_save_component_info = check_and_save

    ui = NS(ugc_params={'change_units': {}}, added=[], commands=[])

    def add_change_unit(uid):
        ui.added.append(uid)
        ui.ugc_params['change_units'].setdefault('unit', set()).add(uid)
    ui._add_change_unit = add_change_unit

    # 玩家路径的核心：命令模式创建（_normal_create_units + math3d.vector）
    def normal_create_units(units, end_pos=None, last_update_params=None):
        ui.commands.append({'units': list(units), 'end_pos': end_pos})
        for u in units:
            ui.ugc_params.setdefault('create_units', []).append(u)
            comp = getattr(u, '_fuxiang_mock_comp', None)
            if comp is not None:
                ui.ugc_params.setdefault('create_components', []).append(comp)
            ui.ugc_params['has_create'] = True
            add_change_unit(u.uid)
    ui._normal_create_units = normal_create_units

    # 便于 mock 挂上组件对象引用

    units = {}

    def create(info, _):
        def vec(v):
            return NS(x=float(v[0]), y=float(v[1]), z=float(v[2]))
        units[info['uid']] = NS(
            uid=info['uid'], model=NS(valid=True),
            ugc_dir=vec(info['ugc_dir']), ugc_size=vec(info['ugc_size']),
            get_position3=lambda p=info['pos']: vec(p),
            _fuxiang_local_item=None)
        return units[info['uid']]
    create.__globals__['UNIT_TYPE_CONF'] = NS(UGC_COM=10541)
    create.__globals__['g_ui_mgr'] = NS(get_ui=lambda n: ui)
    room.create_unit_with_info = create
    room.unit_mgr.get_unit = units.get
    room.unit_mgr.remove_unit = units.pop

    def create_comp(info):
        comp = NS(_ugc_id=info['ugc_id'], _owner_uid=info['owner_uid'],
                  _owner=units.get(info['owner_uid']),
                  init_ugc_info=Mock(), init_ugc_info_finished=Mock(),
                  destroy=Mock())
        cm._ugc_id2component[info['ugc_id']] = comp
        cm._component_type2ugc_ids.setdefault(100000, set()).add(info['ugc_id'])
        units[info['owner_uid']]._fuxiang_local_item = {
            'ugc_id': info['ugc_id'], 'item_id': 202009}
        units[info['owner_uid']]._fuxiang_mock_comp = comp
        return comp
    cm.create_component_client = create_comp

    def remove_component(c):
        cm._ugc_id2component.pop(c._ugc_id, None)
        for ids in cm._component_type2ugc_ids.values():
            ids.discard(c._ugc_id)
    cm.remove_component = remove_component
    return api, room, cm, ui


class SaveRegistrationTests(unittest.TestCase):
    """place/remove 必须同步维护 ugc_params 的新建记录与变更集。"""

    def _saveable(self):
        return scene_fixture_with_ui()

    def test_save_event_shape_targets_new_ugc_id(self):
        _, _, _ = fixture()
        spec = {'item_id': 202009, 'component_type': 100000, 'unit_type': 10541,
                'model_path': 'scene/test.gim', 'position': [1, 0, 2],
                'rotation': [0, 0, 0], 'scale': [1, 1, 1]}
        ev = scene.save_event(spec, 1000002, 114)
        self.assertEqual(ev['key'], 'add_unit')
        info = ev['creat_params'][0]
        self.assertEqual(info['unit_type'], 10541)
        self.assertEqual(info['ugc_type'], 202009)
        rec = info['ugc_recover_info']
        self.assertEqual(rec['create_info']['ugc_id'], 114)
        self.assertEqual(rec['create_info']['component_type'], 100000)
        self.assertTrue(scene._event_targets_ugc_id(ev, 114))
        self.assertFalse(scene._event_targets_ugc_id(ev, 999))

    def test_save_event_is_replayable_by_native_handler(self):
        """do_save_component_info 会重放队列事件；结构不全将导致队列永久卡住。

        必须同时提供：
          create_info    -> create_component_client
          component_info -> recover_ugc_info/init_ugc_info
        """
        _, _, _ = fixture()
        spec = {'item_id': 202009, 'component_type': 100000, 'unit_type': 10541,
                'model_path': 'scene/test.gim', 'position': [1, 0, 2],
                'rotation': [0, 0, 0], 'scale': [1, 1, 1]}
        rec = scene.save_event(spec, 1000002, 114)['creat_params'][0]['ugc_recover_info']
        self.assertIn('create_info', rec)
        self.assertIn('component_info', rec,
                      '缺少 component_info 会导致 recover_ugc_info 抛异常、保存队列卡死')
        self.assertEqual(rec['component_info']['component_id'], 202009)
        self.assertIn('events', rec['component_info'])
        # create_info 不应混入 component_info 的字段
        self.assertNotIn('events', rec['create_info'])

    def test_place_then_remove_roundtrip(self):
        """本地降级路径：place/remove 应完整创建并清理实体与组件索引。"""
        api, room, cm, ui = self._saveable()
        result = scene.place(api, {'mode': 'local_preview', 'item_id': 202009, 'position': [10, 0, 10]})
        self.assertNotIn('error', result)
        self.assertEqual(result['scope'], 'client_local')
        self.assertFalse(result['persistence_verified'])
        # 玩家路径不写 save_component_info
        self.assertEqual(cm.save_component_info, [])

        removed = scene.remove(api, {'ugc_id': result['ugc_id']})
        self.assertTrue(removed['removed'])
        self.assertNotIn(result['ugc_id'], cm._ugc_id2component)

    def test_save_event_still_replayable(self):
        """本地队列虽不再由 place 使用，但结构必须保持可重放（防御性）。"""
        _, _, cm, _ui = self._saveable()
        cm.save_component_info = []
        cm.set_save_component_info = lambda v: setattr(cm, 'is_save_component_info', v)
        cm.is_save_component_info = True
        ok, was = scene.register_save(cm, {'key': 'add_unit', 'creat_params': []})
        self.assertTrue(ok)

    def test_register_save_restores_flag(self):
        _, _, cm, _ui = self._saveable()
        cm.is_save_component_info = True
        ok, was = scene.register_save(cm, {'key': 'add_unit', 'creat_params': []})
        self.assertTrue(ok)
        self.assertTrue(was)
        self.assertTrue(cm.is_save_component_info, '原标志必须被恢复')

    def test_allow_unverified_opens_other_component_types(self):
        api, _, _ = fixture()
        # 白名单外的类型默认拒绝
        with self.assertRaises(ValueError):
            scene.plan(api, {'item_id': 299999})
        # 显式开关后放行，并标记未验证
        spec = scene.plan(api, {'item_id': 299999, 'allow_unverified': True})
        self.assertEqual(spec['component_type'], 299999)
        self.assertFalse(spec['component_type_verified'])
        self.assertEqual(spec['unit_type'], 10541)

    def test_whitelist_types_pass_without_switch(self):
        """实测白名单内的类型应默认放行。"""
        api, _, _ = fixture()
        for item_id, ct in ((202009, 100000), (201001, 201001)):
            with self.subTest(item_id=item_id):
                spec = scene.plan(api, {'item_id': item_id})
                self.assertEqual(spec['component_type'], ct)
                self.assertTrue(spec['component_type_verified'])

    def test_unsupported_types_rejected_with_reason(self):
        """实测不可摆放的类型应给出明确原因，且 allow_unverified 也救不回来。"""
        api, _, _ = fixture()
        for extra in ({}, {'allow_unverified': True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError) as ctx:
                scene.plan(api, dict({'item_id': 203008}, **extra))
            self.assertIn('实测无法', str(ctx.exception))

    def test_role_types_need_allow_unverified_but_are_server_supported(self):
        """角色类不再硬拒（2026-09-30 复审）：要显式 allow_unverified，且服务端阀门放行。

        锁两件事：① 别把它当"无解只能手摆"（§11 已实证能落库）；
        ② 也别把它当已实测类型（专用确认分支还没实机跑过，VERIFIED_TYPES 里没有）。
        """
        api, _, _ = fixture()
        with self.assertRaises(ValueError):
            scene.plan(api, {'item_id': 101001})
        spec = scene.plan(api, {'item_id': 101001, 'allow_unverified': True,
                                'position': [919.0, 62.201, -282.048]})
        self.assertEqual(spec['component_type'], 101001)
        self.assertTrue(spec['server_begin_allowed'])
        self.assertIn(101001, scene.ROLE_COMPONENT_TYPES)

    def test_zoom_limit_blocks_by_default_but_restore_can_pass(self):
        """滑块范围只拦"新输入"，不该拦"抄自源图的既有值"。

        2026-10-01 实机：春岚 214 个组合件 z=0.02，目录 zoom_limit=(0.1,1000)
        ⇒ 全量还原时组合层一件都摆不出来。引擎本身收这些值（它们就在源图里），
        所以给还原一条显式出口，同时把越界值记进 spec 供回执透出。
        """
        api, _, _ = fixture()
        tight = NS(get_room=lambda: api.get_room(),
                   get_item_data=lambda: {777: {'component_id': 100000,
                                                'model_path': 'scene/test.gim',
                                                'zoom_limit': (0.1, 1000)}})
        with self.assertRaises(ValueError) as ctx:
            scene.plan(tight, {'item_id': 777, 'scale': [1, 1, 0.02]})
        self.assertIn('缩放范围', str(ctx.exception))
        spec = scene.plan(tight, {'item_id': 777, 'scale': [1, 1, 0.02],
                                  'allow_out_of_range_scale': True})
        self.assertEqual(spec['scale_out_of_catalog_range'], [0.02])
        ok = scene.plan(tight, {'item_id': 777, 'scale': [1, 1, 1]})
        self.assertEqual(ok['scale_out_of_catalog_range'], [])

    def test_verified_flag_true_for_obstacle(self):
        api, _, _ = fixture()
        spec = scene.plan(api, {'item_id': 202009})
        self.assertTrue(spec['component_type_verified'])
        self.assertTrue(spec['visually_verified'])


class ChangeSetTests(unittest.TestCase):
    """保存变更集（change_units）是物件能否落库的关键，必须有覆盖。"""

    def _fixture_with_ui(self):
        return scene_fixture_with_ui()

    def test_explicit_local_preview(self):
        api, room, cm, ui = self._fixture_with_ui()
        r = scene.place(api, {'mode': 'local_preview', 'item_id': 202009, 'position': [1, 0, 2]})
        self.assertNotIn('error', r)
        # 显式本地预览不发送服务器请求
        self.assertEqual(r['scope'], 'client_local')

    def test_remove_cleans_up_local_unit(self):
        api, room, cm, ui = self._fixture_with_ui()
        r = scene.place(api, {'mode': 'local_preview', 'item_id': 202009})
        self.assertEqual(r['scope'], 'client_local')
        out = scene.remove(api, {'ugc_id': r['ugc_id']})
        self.assertTrue(out['removed'])
        self.assertNotIn(r['ugc_id'], cm._ugc_id2component)

    def test_explicit_local_preview_without_ui(self):
        """UI 实例不可用时仍可显式创建本地预览。"""
        api, room, cm, ui = self._fixture_with_ui()
        room.create_unit_with_info.__globals__['g_ui_mgr'] = NS(
            get_ui=Mock(side_effect=RuntimeError('ui gone')))
        r = scene.place(api, {'mode': 'local_preview', 'item_id': 202009})
        self.assertNotIn('error', r)
        self.assertEqual(r['scope'], 'client_local')
        self.assertFalse(r['persistence_verified'])

    def test_change_units_snapshot(self):
        _, _, _, ui = self._fixture_with_ui()
        ui.ugc_params['change_units']['unit'] = {1000068, 1000072}
        snap = scene.change_units_snapshot(ui)
        self.assertEqual(snap['unit'], [1000068, 1000072])

    def test_dispatch_propagates_operation_errors(self):
        edit.OPS['test_failure'] = lambda _: {'error': 'failed', 'cleanup': {'complete': False}}
        try:
            result = edit.dispatch({'op': 'test_failure'})
            self.assertFalse(result['ok'])
            self.assertFalse(result['details']['cleanup']['complete'])
        finally:
            del edit.OPS['test_failure']

    def test_list_types_is_read_only(self):
        _, _, cm = fixture()
        cm._component_type2str = {100000: 'OBSTACLE'}
        cm._component_factory = {}
        original = edit.get_component_manager
        edit.get_component_manager = lambda: cm
        try:
            result = edit.op_list_types({})
            self.assertEqual(cm._component_factory, {})
            self.assertTrue(result['types'][0]['local_place_supported'])
            self.assertFalse(result['types'][0]['factory_cached'])
        finally:
            edit.get_component_manager = original

    def test_legacy_create_rejects_id_collision_before_game_call(self):
        _, _, cm = fixture()
        cm._component_type2str = {100000: 'OBSTACLE'}
        cm.create_component_client = Mock()
        original = edit.get_component_manager
        edit.get_component_manager = lambda: cm
        try:
            result = edit.op_create_component({'info': {
                'component_type': 100000, 'owner_uid': 1000001, 'ugc_id': '113'}})
            self.assertIn('error', result)
            cm.create_component_client.assert_not_called()
        finally:
            edit.get_component_manager = original

    def test_mcp_failure_flag_and_argument_checks(self):
        original = server.call_edit
        server.call_edit = Mock(side_effect=server.BridgeError('failed'))
        try:
            result = server.process({'id': 1, 'method': 'tools/call', 'params': {
                'name': 'fuxiang_place_item', 'arguments': {'item_id': 202009}}})
            self.assertTrue(result['result']['isError'])
            with self.assertRaises(server.BridgeError):
                server.handle_tool_call('fuxiang_place_item', {'item_id': 202009, 'unit_type': 10546})
        finally:
            server.call_edit = original


if __name__ == '__main__':
    unittest.main()
