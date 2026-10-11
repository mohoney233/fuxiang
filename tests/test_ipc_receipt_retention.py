# -*- coding: utf-8 -*-
"""ipc/out 回执保留与归档的离线回归（不接触游戏进程）。

背景（2026-09-19）：cli/fxedit.py::call_bridge 过去每次调用都 os.remove 回执，
长循环累计撞上宿主 safe-delete 批量阈值后进程被**静默终止**（退出码 1、无 traceback），
本工作区实测两次。现在改为保留回执 + cli/fxsweep.py 改名归档，本文件锁住两件事：
  1) call_bridge 成功返回后回执必须还在；
  2) 归档只搬够老的文件、只改名不删除（否则会搬走在途回执、或重新踩钩子）。
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cli'))
import fxedit  # noqa: E402
import fxcall  # noqa: E402
import fxsweep  # noqa: E402


def fake_responder(cmd_dir, out_dir, stop):
    """最小游戏侧 poll 循环：读 ipc/cmd 一条，往 ipc/out 写一条同名回执。"""
    while not stop.is_set():
        try:
            names = sorted(n for n in os.listdir(cmd_dir) if n.endswith('.json'))
        except OSError:
            names = []
        for n in names:
            p = os.path.join(cmd_dir, n)
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    cmd = json.load(f)
                os.remove(p)
                with open(os.path.join(out_dir, cmd['id'] + '.json'), 'w',
                          encoding='utf-8') as f:
                    json.dump({'id': cmd['id'], 'ok': True, 'value': {'pong': True}}, f)
            except (OSError, ValueError, KeyError):
                continue
        time.sleep(0.01)


class IpcDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cmd = os.path.join(self.tmp.name, 'ipc', 'cmd')
        self.out = os.path.join(self.tmp.name, 'ipc', 'out')
        os.makedirs(self.cmd)
        os.makedirs(self.out)
        self.state = os.path.join(self.tmp.name, 'ipc', 'bridge_state.json')
        self._saved = (fxedit.CMD_DIR, fxedit.OUT_DIR, fxedit.STATE,
                       fxsweep.OUT_DIR, fxsweep.ROOT)
        fxedit.CMD_DIR, fxedit.OUT_DIR, fxedit.STATE = self.cmd, self.out, self.state
        fxsweep.OUT_DIR = self.out
        fxsweep.ROOT = self.tmp.name  # 归档目录也落在临时区，不碰工作区
        self.beat()

    def tearDown(self):
        (fxedit.CMD_DIR, fxedit.OUT_DIR, fxedit.STATE,
         fxsweep.OUT_DIR, fxsweep.ROOT) = self._saved
        self.tmp.cleanup()

    def beat(self, age=0.0):
        with open(self.state, 'w', encoding='utf-8') as f:
            json.dump({'alive': True, 'now': time.time() - age}, f)

    def receipts(self):
        return sorted(n for n in os.listdir(self.out) if n.endswith('.json'))


class ReceiptRetentionTest(IpcDirCase):
    def test_receipt_kept_after_successful_call(self):
        stop = threading.Event()
        t = threading.Thread(target=fake_responder, args=(self.cmd, self.out, stop),
                             daemon=True)
        t.start()
        try:
            data = fxedit.call_bridge('ping', {}, timeout=10)
        finally:
            stop.set()
            t.join(timeout=2)
        self.assertIsNotNone(data, 'call_bridge 未取得回执')
        self.assertTrue(data.get('ok'))
        self.assertEqual(self.receipts(), [data['id'] + '.json'],
                         '回执必须保留：删除会累计撞宿主 safe-delete 阈值并静默杀进程')
        self.assertEqual([n for n in os.listdir(self.cmd) if n.endswith('.tmp')], [],
                         '.tmp 应由 os.replace 落定，不留残渣')

    def test_stale_bridge_state_still_refuses(self):
        self.beat(age=999)
        self.assertIsNone(fxedit.call_bridge('ping', {}, timeout=2))
        self.assertEqual(self.receipts(), [])


class FxSweepTest(IpcDirCase):
    def touch(self, name, age):
        p = os.path.join(self.out, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('{}')
        ts = time.time() - age
        os.utime(p, (ts, ts))
        return p

    def run_main(self, *argv):
        """main() 自己解析 argv，单测里要把它接管掉。"""
        saved = sys.argv
        sys.argv = ['fxsweep.py'] + list(argv)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = fxsweep.main()
        finally:
            sys.argv = saved
        return rc, json.loads(buf.getvalue())

    def test_skips_in_flight_receipt(self):
        fresh = self.touch('c_fresh.json', 1)
        old = self.touch('c_old.json', 500)
        self.assertEqual(fxsweep.collect(60), ['c_old.json'])
        rc, _report = self.run_main()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(fresh), '在途回执不得被搬走')
        self.assertFalse(os.path.exists(old), '老回执应已离开 ipc/out')

    def test_archives_by_rename_not_delete(self):
        self.touch('c_old.json', 500)
        _rc, report = self.run_main()
        self.assertEqual(report['moved'], 1)
        self.assertEqual(os.listdir(report['archive']), ['c_old.json'],
                         '归档目录要拿到文件本体——改名，不是删除')
        self.assertEqual(self.receipts(), [])

    def test_dry_run_touches_nothing(self):
        old = self.touch('c_old.json', 500)
        _rc, report = self.run_main('--dry-run')
        self.assertEqual(report['would_move'], 1)
        self.assertTrue(os.path.exists(old))

    def test_nothing_to_do_is_quiet_success(self):
        rc, report = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(report['moved'], 0)


class HeartbeatGapTest(IpcDirCase):
    """「心跳 None」= 状态文件读不到，多为 MoveFileEx 替换窗口的亚毫秒缺失，
    不是桥死。call_bridge 必须短重试跨过它（2026-09-19 jp-city-0524 实机命中）。"""

    def test_transient_missing_state_recovers(self):
        os.remove(self.state)  # 先制造缺失，0.3s 后由"心跳"重建
        stop = threading.Event()
        t = threading.Thread(target=fake_responder, args=(self.cmd, self.out, stop),
                             daemon=True)
        t.start()

        def heal():
            time.sleep(0.3)
            with open(self.state, 'w', encoding='utf-8') as f:
                json.dump({'alive': True, 'now': time.time()}, f)

        h = threading.Thread(target=heal)
        h.start()
        try:
            data = fxedit.call_bridge('ping', {}, timeout=10)
        finally:
            h.join(timeout=2)
            stop.set()
            t.join(timeout=2)
        self.assertIsNotNone(data, '瞬时缺失应被重试吸收，而不是误报桥接未运行')
        self.assertTrue(data.get('ok'))

    def test_persistent_missing_state_still_refuses(self):
        os.remove(self.state)
        saved = fxedit.STATE_RETRY_SEC
        fxedit.STATE_RETRY_SEC = 0.2
        try:
            self.assertIsNone(fxedit.call_bridge('ping', {}, timeout=5))
        finally:
            fxedit.STATE_RETRY_SEC = saved


class EvalChannelPresetTest(IpcDirCase):
    """两条 eval 通道必须同预置：桥层原始 eval 曾被裸用，导致按文档写代码的
    调用方拿到 NameError: name 'cm' is not defined（2026-09-19 建模师实机）。
    fxcall.call('eval') 现在必须把 core/cm/em/jdump/plain 的 prologue 拼在前面，
    且仍原样带上用户代码；非 eval op 不得被改写。"""

    def setUp(self):
        super().setUp()
        self._saved_call = (fxcall.CMD_DIR, fxcall.OUT_DIR, fxcall.STATE)
        fxcall.CMD_DIR, fxcall.OUT_DIR, fxcall.STATE = self.cmd, self.out, self.state
        self.seen = {}

    def tearDown(self):
        (fxcall.CMD_DIR, fxcall.OUT_DIR, fxcall.STATE) = self._saved_call
        super().tearDown()

    def responder(self, stop):
        while not stop.is_set():
            names = sorted(n for n in os.listdir(self.cmd) if n.endswith('.json'))
            for n in names:
                p = os.path.join(self.cmd, n)
                with open(p, 'r', encoding='utf-8') as f:
                    cmd = json.load(f)
                os.remove(p)
                self.seen = cmd
                with open(os.path.join(self.out, cmd['id'] + '.json'), 'w',
                          encoding='utf-8') as f:
                    json.dump({'id': cmd['id'], 'ok': True, 'value': {'result': 'x'}}, f)
            time.sleep(0.01)

    def _run(self, op, args):
        stop = threading.Event()
        t = threading.Thread(target=self.responder, args=(stop,), daemon=True)
        t.start()
        try:
            return fxcall.call(op, args, timeout=10)
        finally:
            stop.set()
            t.join(timeout=2)

    def test_eval_gets_preset_prologue(self):
        data = self._run('eval', {'code': '__result__ = len(cm._ugc_id2component)'})
        self.assertIsNotNone(data)
        sent = self.seen['args']['code']
        for name in ('core', 'room', 'cm', 'em', 'GK', 'jdump', 'plain', 'FS', 'FE'):
            self.assertIn(name + ' = ', sent, '预置缺少 %s' % name)
        self.assertTrue(sent.endswith('__result__ = len(cm._ugc_id2component)'),
                        '用户代码必须原样附在 prologue 之后')
        self.assertIn('except Exception', sent, 'prologue 必须兜底，不能弄坏传输通道')

    def test_user_timeout_arg_survives(self):
        self._run('eval', {'code': '__result__ = 1', 'timeout': 42})
        self.assertEqual(self.seen['args']['timeout'], 42)

    def test_other_ops_not_rewritten(self):
        self._run('ping', {})
        self.assertEqual(self.seen['op'], 'ping')
        self.assertNotIn('code', self.seen.get('args', {}))


if __name__ == '__main__':
    unittest.main()
