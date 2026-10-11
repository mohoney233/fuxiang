#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_doc_commands — 文档里每一条命令都必须**真的能解析**。

为什么需要：交付手册与 SNAPSHOT.md 里的命令行是别人直接复制粘贴去跑的。这一整轮里
出现过两次"文档写了但跑不了"：旧版不认 `--sync-settings`（照抄就 `unrecognized arguments`），
以及 `--limit 0` 的语义和手册的期待完全不同。这类错误在测试全绿的情况下照样活着，
因为**没人把文档里的命令交给 argparse 走一遍**。

这里做三件事（全部离线，不连游戏）：
1. 文档里每个 `--flag` 必须存在于对应 CLI 的 parser（含子命令）；
2. `python cli/fxedit.py <op>` 里的 op 名必须在注册表里；
3. 扫描正则自己要先证明抓到了命令（防止模式过期后静默通过）。
"""
import ast
import glob
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
import snapshot as sn_cli          # noqa: E402
import snapshot_restore as sr      # noqa: E402

DOCS = ["SNAPSHOT.md", os.path.join("snapshots", "chunlan_1to1_20260930.restore_notes.md")]


def _help_text(module, argv):
    import contextlib
    import io
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            module.main(list(argv))
        except SystemExit:
            pass
    return buf.getvalue()


def _help_tokens(module, argv):
    """跑一次 `--help` 并把帮助文本里的选项/子命令名捞出来。

    刻意**不**去 patch `argparse.ArgumentParser` 抓 parser 对象：那会连 argparse 自己的
    内部构造一起替换掉，实机跑出来是深层递归 + MemoryError。读帮助文本反而更贴近
    操作者实际敲的东西。
    """
    import contextlib
    import io
    return set(re.findall(r"--?[A-Za-z][A-Za-z0-9_-]*", _help_text(module, argv)))


class TestDocCommandsParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.restore_flags = _help_tokens(sr, ["--help"])
        usage = _help_text(sn_cli, ["--help"])
        m = re.search(r"\{([^}]*)\}", usage)
        cls.subcommands = set((m.group(1).split(",") if m else []))
        cls.sub_flags = {sub: {t for t in _help_tokens(sn_cli, [sub, "--help"])
                               if t.startswith("--")}
                         for sub in cls.subcommands}
        cls.ops = cls._edit_ops()
        cls.commands = cls._doc_commands()
        assert cls.restore_flags, "restore --help 没读到任何开关"
        assert cls.subcommands, "没读到 snapshot 的子命令清单"

    @staticmethod
    def _edit_ops():
        """AST 直读 op 注册表（不 import 引擎侧模块）。"""
        path = os.path.join(ROOT, "bridge", "fuxiang_edit.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in ("OPS", "HANDLERS")
                    for t in node.targets):
                if isinstance(node.value, ast.Dict):
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            names.add(k.value)
        return names

    @staticmethod
    def _doc_commands():
        """抽出文档代码块里的命令行，把行尾 `\\` 续行拼回一条。"""
        got = []
        for doc in DOCS:
            path = os.path.join(ROOT, doc)
            if not os.path.exists(path):
                continue
            pending = None
            with io.open(path, encoding="utf-8") as fh:
                raw_lines = fh.read().splitlines()
            for raw in raw_lines:
                line = raw.rstrip("\n")
                s = line.strip()
                if pending is not None:
                    s = pending + " " + s
                    pending = None
                if s.endswith("\\"):
                    pending = s[:-1].strip()
                    continue
                if s.startswith("python ") and "cli/" in s:
                    got.append((doc, re.sub(r"\s+#.*$", "", s)))
            assert pending is None
        return got

    def test_scanner_actually_found_commands(self):
        self.assertGreaterEqual(len(self.commands), 12,
                                msg="文档命令的写法变了，本测试的模式已过期")

    def test_every_documented_flag_exists(self):
        bad = []
        for doc, cmd in self.commands:
            if "snapshot_restore.py" in cmd:
                flags = self.restore_flags
            else:
                m = re.search(r"cli/snapshot\.py\s+([a-z_]+)", cmd)
                if not m:
                    continue
                sub = m.group(1)
                if sub not in self.subcommands:
                    bad.append("%s: 子命令 %r 不存在（%s）" % (doc, sub, cmd))
                    continue
                # `snapshot.py restore …` 的开关归 snapshot_restore 的 parser
                flags = (self.restore_flags if sub == "restore"
                         else self.sub_flags.get(sub, set()))
            for tok in re.findall(r"(?<!\S)(--[A-Za-z][A-Za-z0-9-]*)", cmd):
                if tok not in flags:
                    bad.append("%s: 开关 %s 不被 %s 识别\n    %s"
                               % (doc, tok, cmd.split()[2] if len(cmd.split()) > 2 else "",
                                  cmd))
        self.assertEqual(bad, [], msg="文档里有 CLI 不认的写法：\n" + "\n".join(bad))

    def test_documented_fxedit_ops_exist(self):
        if not self.ops:
            self.fail("没从 fuxiang_edit 的注册表里读出 op 名（结构变了）")
        bad = []
        for doc, cmd in self.commands:
            m = re.search(r"cli/fxedit\.py\s+([a-z_][a-z0-9_]*)", cmd)
            if m and m.group(1) not in self.ops:
                bad.append("%s: op %r 不存在（%s）" % (doc, m.group(1), cmd))
        self.assertEqual(bad, [], msg="\n".join(bad))

    def test_unknown_flag_is_caught_not_tolerated(self):
        """自证：闸门对不存在的开关真的会报错（不然"全绿"没意义）。"""
        self.assertIn("--sync-settings", self.restore_flags)
        self.assertIn("--abort-after", self.restore_flags)
        self.assertNotIn("--sync-settingz", self.restore_flags)
        self.assertNotIn("--limit0", self.restore_flags)
        self.assertIn("accept", self.subcommands)
        self.assertIn("--live", self.sub_flags["accept"])
        self.assertNotIn("--live", self.sub_flags["check"])


class TestAllCliDocs(unittest.TestCase):
    """把上一条的思路推到**全部** `cli/*.py` 入口：CLI 必须自描述，文档必须指得对。

    2026-10-01 实测出来的两种失效都在此锁住：
    * `story.py` 的 `--help` 少列了 `resume` / `create-many` —— 靠帮助发现命令的人
      看不见已存在的能力（而文档写着它们，扫描器反过来把"文档有、help 没有"当成
      文档坏了，说明这种不一致真的会误导人）；
    * 我自己补写 help 行时先写错了一次（`create-many --count N`，真实签名是位置参数），
      所以 help 的内容也要被校验，不是写完就算对。
    """

    @staticmethod
    def _scripts():
        out = []
        for name in sorted(os.listdir(os.path.join(ROOT, "cli"))):
            if not name.endswith(".py") or name.startswith("_"):
                continue
            path = os.path.join(ROOT, "cli", name)
            with io.open(path, encoding="utf-8") as fh:
                src = fh.read()
            if "__main__" in src:
                out.append((name[:-3], src))
        return out

    @staticmethod
    def _registry(src):
        """脚本自己登记的命令名：argparse 子解析器 + `COMMANDS = {...}` + op 表。"""
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "add_parser" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                names.add(str(node.args[0].value))
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in ("COMMANDS", "OPS")
                    for t in node.targets) and isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant):
                        names.add(str(k.value))
        return names

    @staticmethod
    def _help(script, sub=None):
        import subprocess
        cmd = [sys.executable, os.path.join(ROOT, "cli", script + ".py")]
        if sub:
            cmd.append(sub)
        cmd.append("--help")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                               encoding="utf-8")
            return (r.stdout or "") + (r.stderr or "")
        except Exception:
            return ""

    @staticmethod
    def _mentions(txt, name):
        """词边界匹配。裸 `in` 会让 `resume` 被 `resumexxx` 蒙过去（实测假通过过一次）。"""
        return bool(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(name), txt))

    def test_clis_are_self_describing(self):
        missing, checked = [], 0
        for script, src in self._scripts():
            reg = self._registry(src)
            if not reg:
                continue
            checked += 1
            help_txt = self._help(script)
            lost = {n for n in reg if not self._mentions(help_txt, n)}
            if lost:
                missing.append("cli/%s.py 的 --help 没列出：%s" % (script, sorted(lost)))
        self.assertGreaterEqual(checked, 5, msg="只校验了 %d 个入口，覆盖面不够" % checked)
        self.assertEqual(missing, [], msg="CLI 不自描述（能力存在但帮助里没有）：\n"
                                          + "\n".join(missing))

    def test_documented_subcommands_are_real(self):
        """文档里 `cli/x.py <tok>` 的 tok 必须出现在该 CLI 自己的输出里。

        为什么用"帮助文本包含"而不是"代码里有没有登记"：`fxedit.py` / `fxapi.py` 这类
        **op 分发器**的注册表在 `bridge/fuxiang_edit.py`，不在 cli 脚本里；而它们的
        `--help` 输出本来就会把可用 op 全列出来（跟人看到的一样）。
        """
        help_txt, bad, seen = {}, [], set()
        for doc, cmd in self._doc_commands_all():
            m = re.search(r"cli/([a-z_]+)\.py\s+(\S+)", cmd)
            if not m:
                continue
            script, tok = m.group(1), m.group(2)
            # 位置参数常常就是文件路径（`snapshot_audit.py snapshots/x.json`）：
            # 带斜杠或带扩展名的都不当"子命令"看，否则全是假阳性
            if ("/" in tok or "\\" in tok or "." in tok
                    or tok in ("-h", "--help")
                    or os.path.exists(os.path.join(ROOT, "cli", tok + ".py"))):
                continue
            if not re.fullmatch(r"[a-z_][a-z0-9_-]*", tok):
                continue
            if script not in help_txt:
                path = os.path.join(ROOT, "cli", script + ".py")
                if not os.path.exists(path):
                    help_txt[script] = ""
                    continue
                with io.open(path, encoding="utf-8") as fh:
                    src = fh.read()
                help_txt[script] = self._help(script) + " " + \
                    self._help(script if script not in ("fxedit", "fxapi") else "fxedit") \
                    + " " + src
            seen.add(script)
            if not self._mentions(help_txt[script], tok):
                bad.append("%s: `cli/%s.py %s` 在该 CLI 的帮助/注册表里找不到"
                           % (doc, script, tok))
        self.assertGreaterEqual(len(seen), 6,
                                msg="只覆盖到 %s，这条检查面太窄" % sorted(seen))
        self.assertEqual(bad, [], msg="文档指向不存在的子命令：\n" + "\n".join(sorted(set(bad))))

    @staticmethod
    def _doc_commands_all():
        got = []
        for path in sorted(glob.glob(os.path.join(ROOT, "*.md"))) + sorted(
                glob.glob(os.path.join(ROOT, "snapshots", "*.md"))):
            pending = None
            with io.open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            for raw in lines:
                s = raw.strip()
                if pending is not None:
                    s = pending + " " + s
                    pending = None
                if s.endswith("\\"):
                    pending = s[:-1].strip()
                    continue
                if "cli/" in s and "python" in s:
                    got.append((os.path.basename(path), re.sub(r"\s+#.*$", "", s)))
        return got

    def test_scan_itself_has_teeth(self):
        """自证：真的能发现"能力存在但 help 没写"——先确认 story.py 现在是对称的。"""
        src = io.open(os.path.join(ROOT, "cli", "story.py"), encoding="utf-8").read()
        reg = self._registry(src)
        self.assertIn("resume", reg)
        self.assertTrue(self._mentions(self._help("story"), "resume"))
        self.assertTrue(self._mentions(self._help("story"), "create-many"))
        # 词边界：`resumexxx` 不算提到了 `resume`（裸 `in` 会假通过，实测过）
        self.assertFalse(self._mentions("cli/story.py resumexxx", "resume"))
        # 而一个不存在的命令不许出现在帮助里却不登记（反向：拼错的名字会被上面两条抓到）
        self.assertNotIn("resumex", reg)


if __name__ == "__main__":
    unittest.main()
