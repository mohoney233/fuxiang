"""Same mesh does not mean the same editor component or behavior."""
import unittest
from unittest.mock import patch
from test_scene import fixture, scene, edit, server


class CatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            202009: {'component_id':100000,'is_visible':1,'model_path':'cover.gim','item_type':1},
            202014: {'component_id':202014,'model_path':'cover.gim','item_type':1},
            10148: {'component_id':100000,'model_path':'table.gim','item_type':1}}

    def test_default_search_excludes_hidden_same_mesh_record(self):
        with patch.object(edit,'get_item_data',return_value=self.data):
            result=edit.op_list_items({'query':'cover.gim'})
        self.assertEqual([x['item_id'] for x in result['items']], [202014])
        self.assertEqual(result['excluded_hidden_matches'],1)
        self.assertEqual(result['items'][0]['component_id'],202014)

    def test_hidden_records_are_explicit_and_keep_identity(self):
        with patch.object(edit,'get_item_data',return_value=self.data):
            result=edit.op_list_items({'query':'cover.gim','include_hidden':True})
        hidden, visible=result['items']
        self.assertEqual(hidden['item_id'],202009)
        self.assertEqual(hidden['component_id'],100000)
        self.assertEqual(hidden['same_model_visible_item_ids'],[202014])
        self.assertFalse(hidden['object_list_eligible'])
        self.assertTrue(visible['object_list_eligible'])
        self.assertNotIn('object_list_visible',visible)

    def test_pagination_happens_after_visibility_filter(self):
        with patch.object(edit,'get_item_data',return_value=self.data):
            result=edit.op_list_items({'offset':1,'limit':1})
        self.assertEqual(result['total'],2)
        self.assertEqual([x['item_id'] for x in result['items']],[202014])

    def test_plan_does_not_substitute_items_that_share_a_mesh(self):
        api,_,_=fixture()
        api.get_item_data=lambda:self.data
        hidden=scene.plan(api,{'item_id':202009})
        visible=scene.plan(api,{'item_id':202014})
        self.assertEqual(hidden['component_type'],100000)
        self.assertEqual(visible['component_type'],202014)
        self.assertTrue(visible['server_place_supported'])
        self.assertTrue(hidden['catalog_hidden'])
        self.assertEqual(hidden['same_model_visible_item_ids'],[202014])

    def test_include_hidden_requires_boolean(self):
        with patch.object(edit,'get_item_data',return_value=self.data):
            self.assertIn('error',edit.op_list_items({'include_hidden':'false'}))

    def test_mcp_forwards_include_hidden(self):
        with patch.object(server,'call_edit',return_value={'ok':True}) as call:
            server.handle_tool_call('fuxiang_list_items',{'query':'cover','include_hidden':True})
        self.assertEqual(call.call_args.args[1]['include_hidden'],True)


if __name__=='__main__':
    unittest.main()
