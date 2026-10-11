# -*- coding: utf-8 -*-
"""cli/fxapi.py 剥壳约定的离线回归（不接触游戏进程）。

背景（2026-09-22 通道收敛）：同一件事此前有三条通道、**9 份各自实现的 unwrap**，
`fxedit` 停在 L2 信封、`fxlong` 停在业务值层，换 import 就出错。现在剥壳统一到
cli/fxapi.py，本文件锁住四条语义，防止再次分叉：

    unwrap          "剥到底"（用于 call()）
    unwrap_key      "按字段名定位，返回 node[key]"（event_*.py / gadget.py 用）
    find_layer      "按字段名定位，返回该层本身"（scan_*.py 取 items 层用）
    find_layer_any  "同一层命中任一 key 即返回" —— **不是**逐 key 分别下探
    envelope_value  只剥一层 L2 外壳（story.py 用，需自己读 ok/error）

信封层级（自外向内）：
    L0 {"id","ok","value","elapsed"}  L1 {"result":...}  L2 {"ok","value":X}  L3 X
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cli'))
import fxapi  # noqa: E402


class TestUnwrap(unittest.TestCase):
    def test_peels_full_chain_to_business_value(self):
        chain = {"ok": True, "value": {"result": {"ok": True, "value": {"a": 1}}}}
        self.assertEqual(fxapi.unwrap(chain), {"a": 1})

    def test_eval_op_extra_layer(self):
        # eval op 的 L3 自身是 {"result": 实际值}，要多剥一层
        self.assertEqual(fxapi.unwrap({"ok": True, "value": {"result": 42}}), 42)

    def test_does_not_peel_business_dict_with_extra_keys(self):
        # 业务值恰好叫 value，但同层还有别的键 → 不是外壳，必须原样返回
        biz = {"value": 1, "name": "x"}
        self.assertIs(fxapi.unwrap(biz), biz)

    def test_stops_on_plain_business_dict(self):
        biz = {"total": 3, "items": []}
        self.assertIs(fxapi.unwrap(biz), biz)

    def test_non_dict_passthrough(self):
        for v in (None, 0, "s", [1, 2]):
            self.assertEqual(fxapi.unwrap(v), v)

    def test_does_not_loop_forever_on_self_reference(self):
        d = {"ok": True}
        d["value"] = d          # 自引用：不得死循环
        self.assertIsInstance(fxapi.unwrap(d, depth=4), dict)


class TestUnwrapKey(unittest.TestCase):
    def test_finds_key_at_nested_layer(self):
        node = {"ok": True, "value": {"ok": True, "value": {"count": 7}}}
        self.assertEqual(fxapi.unwrap_key(node, "count"), 7)

    def test_returns_none_when_absent(self):
        self.assertIsNone(fxapi.unwrap_key({"ok": True, "value": {}}, "nope"))

    def test_non_dict_node(self):
        self.assertIsNone(fxapi.unwrap_key(None, "x"))
        self.assertIsNone(fxapi.unwrap_key("s", "x"))


class TestFindLayer(unittest.TestCase):
    def test_returns_the_layer_not_the_value(self):
        node = {"ok": True, "value": {"items": [1, 2], "total": 2}}
        layer = fxapi.find_layer(node, "items")
        self.assertEqual(layer, {"items": [1, 2], "total": 2})
        self.assertEqual(layer["total"], 2)     # 同层其它键要能一起用


class TestFindLayerAny(unittest.TestCase):
    def test_returns_layer_containing_any_key(self):
        node = {"ok": True, "value": {"count": 3, "events": []}}
        layer = fxapi.find_layer_any(node, ("ok", "count", "events"))
        self.assertEqual(layer, node)           # 浅层已含 ok → 就返回浅层

    def test_prefers_shallower_layer(self):
        # 关键语义：浅层含第二个/第三个 key 时也要在浅层停住，
        # 而不是为了找 "ok" 一路下探（旧 event_import 的 key=None 分支即此语义）
        node = {"count": 0, "value": {"ok": True, "events": [1]}}
        self.assertIs(fxapi.find_layer_any(node, ("ok", "count", "events")), node)

    def test_none_when_no_key_anywhere(self):
        self.assertIsNone(fxapi.find_layer_any({"a": 1}, ("x", "y")))


class TestEnvelopeValue(unittest.TestCase):
    def test_peels_one_shell(self):
        self.assertEqual(fxapi.envelope_value({"ok": True, "value": {"a": 1}}), {"a": 1})

    def test_error_envelope_returned_as_is(self):
        # 出错信封没有 value 键 → 原样返回，调用方才能读 ok/error
        err = {"ok": False, "error": "boom"}
        self.assertIs(fxapi.envelope_value(err), err)

    def test_non_dict_value_keeps_envelope(self):
        # value 不是 dict 时保留外壳（旧 story.py::_unwrap 只剥 dict 值）
        env = {"ok": True, "value": 5}
        self.assertIs(fxapi.envelope_value(env), env)


class TestDispatchErrorSurfaces(unittest.TestCase):
    """dispatch 的失败信封必须被 `call` 当错误，而不是当业务值原样返回。

    2026-10-02 实机（批量摆放 run-2）：护栏拒绝整批时 op 抛异常，dispatch 回
    `{"ok":False,"error":...,"details":...}`；`_send` 当时只拦 L1 那层，于是 `call`
    把这个信封当业务值交出去 —— 批量摆放读不到 `results`，把 100 件逐件记成
    `exception` 且**一行原因都没留下**。契约本来就是「call 出错抛 RuntimeError」。
    """

    @staticmethod
    def _bridge(value):
        return patch.object(fxapi, "call_bridge",
                            lambda *a, **k: {"ok": True, "value": value})

    def test_call_raises_on_dispatch_error_envelope(self):
        env = {"result": {"ok": False, "error": "护栏拒绝整批", "details": {}}}
        with self._bridge(env):
            with self.assertRaises(RuntimeError) as cm:
                fxapi.call("place_batch", {"items": []})
        self.assertIn("护栏拒绝整批", str(cm.exception))

    def test_call_still_unwraps_a_success_envelope(self):
        with self._bridge({"result": {"ok": True, "value": {"results": []}}}):
            self.assertEqual(fxapi.call("place_batch", {}), {"results": []})

    def test_business_value_with_its_own_ok_false_is_not_an_error(self):
        # op 自己返回 {"ok":False,...}（如 save_map 没保存上）时 L2 的 ok 仍是 True
        # ⇒ 不许抛，调用方要能自己读那个 ok（snapshot_restore 就靠它）
        with self._bridge({"result": {"ok": True,
                                      "value": {"ok": False,
                                                "error": "没保存上"}}}):
            self.assertIs(fxapi.call("save_map", {}).get("ok"), False)

    def test_call_envelope_also_reports_it(self):
        env = {"result": {"ok": False, "error": "boom"}}
        with self._bridge(env):
            self.assertIsNone(fxapi.call_envelope("place_batch", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
