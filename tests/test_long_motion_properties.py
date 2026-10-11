"""Regression: editing a scalar must not truncate persisted animation paths."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import fuxiang_edit_ops as ops

PATH = 20391119001
NAME = 20391102001


class LongMotionProperties(unittest.TestCase):
    def setUp(self):
        self.points = [[0.083, [i, 28, -100], [0, 0, i], [0.1, 2, 0.1]]
                       for i in range(120)]
        self.events = [{'type': PATH, 'info': [self.points]},
                       {'type': NAME, 'info': ['before']}]
        self.comp = SimpleNamespace(COMPONENT_TYPE=203911, _ugc_id=118,
                                    component_info={'component_id': 203911,
                                                    'events': copy.deepcopy(self.events)})
        self.comp.update_component_info = lambda events: self.comp.component_info.update(
            events=copy.deepcopy(events))
        self.unit = SimpleNamespace(uid=1000074)
        self.edit = SimpleNamespace(reg_change=lambda *args: True)

    def run_edit(self, props):
        with patch.object(ops, 'find_target', return_value=(self.comp, self.unit, None)), \
             patch.object(ops, 'property_schema', return_value={str(PATH): {}, str(NAME): {}}), \
             patch.object(ops, '_room', return_value=object()), \
             patch.object(ops, '_ui', return_value=object()), \
             patch.object(ops, '_history'):
            return ops.op_edit_props(self.edit, {'ugc_id': 118, 'props': props})

    def test_edit_name_preserves_all_nested_path_points(self):
        result = self.run_edit({str(NAME): ['after']})
        self.assertTrue(result['ok'])
        self.assertEqual(self.comp.component_info['events'][0]['info'], [self.points])
        self.assertEqual(result['before_events'], self.events)

    def test_readback_checks_tail_and_rolls_back_without_truncation(self):
        calls = []
        def update(events):
            data = copy.deepcopy(events)
            if not calls:
                data[0]['info'][0][100][1][0] = -999
            calls.append(True)
            self.comp.component_info['events'] = data
        self.comp.update_component_info = update
        requested = copy.deepcopy(self.points)
        requested[110][1][0] = 999
        result = self.run_edit({str(PATH): [requested]})
        self.assertFalse(result['ok'])
        self.assertTrue(result['rollback']['complete'])
        self.assertEqual(self.comp.component_info['events'], self.events)


if __name__ == '__main__':
    unittest.main()
