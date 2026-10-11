# -*- coding: utf-8 -*-
"""文档里的「计数」必须等于源码（离线回归，不接触游戏进程）。

背景（2026-09-22）：同一批数字（服务端阀门类型数、本地白名单数、MCP 工具数）曾在
4+ 处文档里各写一遍，漂移到 76/66/29 等过期值且互不相同。现在真源是
`python cli/consts.py`（AST 直读源码），本文件把文档里出现的这些数字**锁到源码上**：
以后新增/删除一个 op、一个工具、一个阀门类型而忘了改文档，这里会直接失败。

只断言"文档写的 == 源码算的"，不断言具体数值（数值变了改源码即可，不用改测试）。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cli'))
import consts  # noqa: E402

S = consts.summary()


def read(name):
    return (ROOT / name).read_text(encoding='utf-8')


def nums(pattern, text, group=1):
    """取出某文档里所有符合 pattern 的数字（可能多处出现）。"""
    return [int(m.group(group)) for m in re.finditer(pattern, text)]


class TestDocCounts(unittest.TestCase):
    def test_server_component_types(self):
        want = S['server_component_types']
        cases = [
            ('README.md', r'扩容至 \*\*(\d+) 个类型\*\*'),
            ('PLACEMENT.md', r'现已扩容至 \*\*(\d+) 个类型\*\*'),
            ('HANDOVER_FILES.md', r'该表扩至 \*\*(\d+) 类\*\*'),
            ('CATALOG.md', r'\*\*(\d+) 种\*\*已有服务端摆放的'),
        ]
        for doc, pat in cases:
            got = nums(pat, read(doc))
            self.assertTrue(got, '%s 里没找到阀门类型数的写法，测试模式已过期：%s' % (doc, pat))
            for v in got:
                self.assertEqual(v, want, '%s 写 %d，源码 SERVER_COMPONENT_TYPES=%d' % (doc, v, want))

    def test_verified_and_unsupported_types(self):
        text = read('README.md')
        pair = re.search(r'白名单为 (\d+) 类型；已知不支持的 (\d+) 类型', text)
        self.assertIsNotNone(pair, 'README.md 里没找到「白名单为 N 类型；已知不支持的 M 类型」写法')
        self.assertEqual(int(pair.group(1)), S['verified_types'],
                         'README 白名单数与源码 VERIFIED_TYPES 不符')
        self.assertEqual(int(pair.group(2)), S['unsupported_types'],
                         'README 不支持类型数与源码 UNSUPPORTED_TYPES 不符')

    def test_mcp_tool_count(self):
        want = S['mcp_tools']
        cases = [
            ('CATALOG.md', r'工具总数 (\d+)（真源'),
            ('MCP_USAGE.md', r'（当前 \*\*(\d+) 个\*\*'),
            ('MCP_USAGE.md', r'## 四、(\d+) 个接口全景'),
            ('HANDOVER_FIX.md', r'(\d+) 个工具（真源'),
        ]
        for doc, pat in cases:
            got = nums(pat, read(doc))
            self.assertTrue(got, '%s 里没找到 MCP 工具数的写法，测试模式已过期：%s' % (doc, pat))
            for v in got:
                self.assertEqual(v, want, '%s 写 %d，源码 MCP 工具数=%d' % (doc, v, want))

    def test_counts_are_reachable_from_code(self):
        # 计数必须真的能被解析出来（AST 解析失败会返回 0/None，静默骗过上面几条）
        for key in ('server_component_types', 'verified_types', 'unsupported_types',
                    'edit_ops', 'mcp_tools', 'max_requests', 'max_settled'):
            self.assertTrue(S.get(key), 'consts.summary() 的 %s 为空 —— AST 解析可能失效' % key)

    def test_two_valves_are_documented_inequivalence(self):
        # 两套阀门差集是「设计」不是漏加：差集类型必须有原因说明
        for ct in S['verified_not_server']:
            self.assertIn(str(ct), S['verified_not_server_reasons'],
                          '差集类型 %s 没有 SERVER_EXCLUDED_REASONS 说明' % ct)


def _count_tests(path):
    """AST 数一个测试文件里的 test 方法数（不用真的跑它）。"""
    import ast
    tree = ast.parse(path.read_text(encoding='utf-8'))
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and sub.name.startswith('test'):
                n += 1
    return n


class TestDocTestCounts(unittest.TestCase):
    """文档里写的「N 项」必须等于测试文件里真实的 test 方法数。

    背景（2026-09-23）：SNAPSHOT.md 的「测试」章节把 `test_snapshot_restore.py` 写成
    29 项（实际 54）、`test_snapshot_diff.py` 写成 26 项（实际 17）—— 是加测试时忘了
    回头改数字。文档里的假计数比没有计数更坏：它让人以为覆盖到了。

    2026-10-01 改成**全量扫描**：原先只锁 4 个硬编码文件名，而 docs 里其实有 13 处计数
    （新加的测试文件默认不在闸门里 ⇒ 一样会烂）。现在扫所有 `*.md` 里的
    "`tests/<文件>`（N 项）"写法逐一对源码，并且**文件不存在也算失败**
    （文档指向删掉的测试文件，是最难发现的那种漂移）。
    """

    PATTERN = re.compile(r'tests/([A-Za-z_0-9]+\.py)`?\s*（\*{0,2}(\d+) 项')

    def _doc_number(self, doc, fname):
        """取 "`tests/<fname>`（**N 项**）" 里的 N。"""
        pat = r'tests/%s`\s*（\*{0,2}(\d+) 项' % re.escape(fname)
        got = nums(pat, read(doc))
        self.assertTrue(got, '%s 里没找到 `tests/%s`（N 项）的写法，测试模式已过期'
                        % (doc, fname))
        return got

    def _all_doc_claims(self):
        claims = []
        for doc in sorted(ROOT.glob('*.md')):
            text = doc.read_text(encoding='utf-8')
            for m in self.PATTERN.finditer(text):
                claims.append((doc.name, m.group(1), int(m.group(2))))
        return claims

    def test_claims_are_found(self):
        """扫描正则失效会静默通过（一条断言都没有）⇒ 必须先证明抓到了东西。"""
        self.assertGreaterEqual(len(self._all_doc_claims()), 10,
                                msg='文档里的「N 项」写法变了，本测试的模式已过期')

    def test_every_documented_count_matches_code(self):
        cache = {}
        bad, missing = [], []
        for doc, fname, claimed in self._all_doc_claims():
            path = ROOT / 'tests' / fname
            if not path.exists():
                missing.append('%s 指向不存在的测试文件 %s' % (doc, fname))
                continue
            if fname not in cache:
                cache[fname] = _count_tests(path)
            if cache[fname] != claimed:
                bad.append('%s 写 %s=%d 项，实际 %d 项' % (doc, fname, claimed,
                                                         cache[fname]))
        self.assertEqual(missing, [], msg='\n'.join(missing))
        self.assertEqual(bad, [], msg='\n'.join(bad))

    def test_snapshot_md_counts(self):
        # 逐条明确报错（上面那条抓全量，这条保证 4 个老面孔仍被单独点名）
        for fname in ('test_snapshot_diff.py', 'test_snapshot_restore.py',
                      'test_story_import_remap.py', 'test_story_field_guard.py'):
            real = _count_tests(ROOT / 'tests' / fname)
            for v in self._doc_number('SNAPSHOT.md', fname):
                self.assertEqual(v, real, 'SNAPSHOT.md 写 %s 有 %d 项，实际 %d 项'
                                 % (fname, v, real))

    def test_total_count(self):
        total = sum(_count_tests(p) for p in (ROOT / 'tests').glob('test_*.py'))
        got = nums(r'\*\*(\d+) 项全绿', read('SNAPSHOT.md'))
        self.assertTrue(got, 'SNAPSHOT.md 里没找到「**N 项全绿**」的写法')
        for v in got:
            self.assertEqual(v, total, 'SNAPSHOT.md 写 %d 项全绿，实际 %d 项' % (v, total))


if __name__ == '__main__':
    unittest.main(verbosity=2)
