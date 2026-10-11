# -*- coding: utf-8 -*-
"""fuxiang_edit 序列化出口的离线回归。

背景（2026-09-19 实机）：建模师在 fuxiang_eval 里读 GK.DM.ugc_dropdown_index 时抛
    TypeError: Object of type taggeddict is not JSON serializable
根因：引擎的 taggeddict 是 C 扩展类型，MRO 只有 (taggeddict, object)，既不继承 dict
也不注册 Mapping，json 的两条 isinstance 快速分支都接不住它。

覆盖：_safe 拆 mapping 型、_jdump 保结构（不退化成 repr 字符串）、显式 default 不被改写、
向量优先于 mapping、op_eval 撞序列化错误时给出可执行指引而不是裸栈。
"""
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import fuxiang_edit as edit  # noqa: E402


class TaggedDict:
    """仿引擎 taggeddict：只实现 mapping 协议，故意不继承 dict。"""

    def __init__(self, data):
        self._data = data

    def keys(self):
        return list(self._data)

    def __getitem__(self, k):
        return self._data[k]


class Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class SafeTest(unittest.TestCase):
    def test_unwraps_mapping_like(self):
        v = TaggedDict({'TID': 'TID_X', 'type': 100, 'value': '10000', 'id': 1})
        self.assertEqual(edit._safe(v),
                         {'TID': 'TID_X', 'type': 100, 'value': '10000', 'id': 1})

    def test_nested_mapping_like(self):
        v = {'palette': {7001: TaggedDict({'value': 'f8b4fe'})}}
        self.assertEqual(json.loads(edit._jdump(v))['palette']['7001']['value'], 'f8b4fe')

    def test_vector_still_wins_over_mapping(self):
        v = Vec(1.0, 2.0, 3.0)
        self.assertEqual(edit._safe(v), [1.0, 2.0, 3.0])

    def test_unserializable_object_still_reprs(self):
        self.assertEqual(edit._safe(Vec), repr(Vec)[:200])


class JdumpTest(unittest.TestCase):
    def test_keeps_structure_instead_of_repr_string(self):
        v = {'row': TaggedDict({'id': 7, 'type': 9})}
        # default=str 会让整条记录变成不可解析的 repr 字符串，jdump 必须给出真结构
        self.assertNotIn("{'id': 7", edit._jdump(v))

    def test_explicit_default_not_overwritten(self):
        out = edit._jdump({'row': TaggedDict({'id': 7})}, default=str)
        self.assertIn('TaggedDict', out)

    def test_kwargs_pass_through(self):
        self.assertIn('\\u4e2d', json.dumps({'n': '中'}))
        self.assertIn('中', edit._jdump({'n': '中'}))


class OpEvalTest(unittest.TestCase):
    """op_eval 在离线环境跑：core/cm/em 为 None，只用自造类型。"""

    PROBE = ("class TD:\n"
             "    def __init__(s, d): s.d = d\n"
             "    def keys(s): return list(s.d)\n"
             "    def __getitem__(s, k): return s.d[k]\n")

    def test_bare_json_dumps_returns_actionable_hint(self):
        r = edit.op_eval({'code': self.PROBE +
                          "import json\n__result__ = json.dumps({'a': TD({'id': 1})})"})
        self.assertIn('error', r)
        self.assertIn('jdump', r['error'])
        self.assertIn('plain', r['error'])
        self.assertIn('not JSON serializable', r['error'])

    def test_jdump_helper_injected(self):
        r = edit.op_eval({'code': self.PROBE + "__result__ = jdump({'a': TD({'id': 1})})"})
        self.assertNotIn('error', r)
        self.assertEqual(json.loads(r['result'])['a'], {'id': 1})

    def test_plain_helper_injected(self):
        r = edit.op_eval({'code': self.PROBE +
                          "__result__ = {'a': plain(TD({'id': 1}))}"})
        self.assertNotIn('error', r)
        self.assertEqual(r['result']['a'], {'id': 1})

    def test_result_dict_needs_no_json_dumps(self):
        r = edit.op_eval({'code': self.PROBE + "__result__ = {'a': TD({'id': 1})}"})
        self.assertNotIn('error', r)
        self.assertEqual(r['result']['a'], {'id': 1})

    def test_other_exceptions_keep_plain_traceback(self):
        r = edit.op_eval({'code': "raise ValueError('boom')"})
        self.assertIn('ValueError: boom', r['error'])
        self.assertNotIn('jdump', r['error'])


if __name__ == '__main__':
    unittest.main()
