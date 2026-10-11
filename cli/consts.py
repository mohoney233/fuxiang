#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""consts — 工具链关键常量的**唯一真源**（2026-09-22）。

背景：同一批数字在文档里散落 4 处给出 3 个不同值 ——
  `SERVER_COMPONENT_TYPES` 实际 79，README/CATALOG/PLACEMENT/HANDOVER_FILES 写 76，
  `mcp/server.py` 的 tool 描述写 66；MCP 工具总数实际 66，MCP_USAGE 写 52、
  HANDOVER_FIX 写 50、CATALOG 写 29。手写数字必然过期。

本模块**直接从源码 AST 读取**，不改代码即可拿到真值。文档/脚本一律引用这里，
不要再手写数字。

用法:
    python cli/consts.py              # 人类可读摘要
    python cli/consts.py --json       # JSON（供脚本消费）
    python cli/consts.py --md         # Markdown 表格行（可直接粘进文档）
"""
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SCENE = os.path.join(ROOT, "bridge", "fuxiang_scene.py")
PLACEMENT = os.path.join(ROOT, "bridge", "fuxiang_placement.py")
EDIT = os.path.join(ROOT, "bridge", "fuxiang_edit.py")
SERVER_PY = os.path.join(ROOT, "mcp", "server.py")


def _assigns(path):
    """解析模块顶层赋值常量。frozenset({...}) 这类 Call 形态一并支持。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return out
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            # frozenset({...}) / set({...})
            if isinstance(node.value, ast.Call) and node.value.args:
                fname = getattr(node.value.func, "id", "")
                try:
                    inner = ast.literal_eval(node.value.args[0])
                except Exception:
                    continue
                value = frozenset(inner) if fname == "frozenset" else set(inner)
            else:
                continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                out[tgt.id] = value
    return out


def _count_tools():
    """统计 mcp/server.py 里注册的 MCP 工具数（tool("fuxiang_xxx", ...)）。"""
    try:
        with open(SERVER_PY, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return 0
    return len(re.findall(r'^\s*tool\(\s*"', src, re.M))


def _count_ops():
    """统计 bridge/fuxiang_edit.py 的 OPS 字典条目数。

    OPS 的值是函数引用（Name），literal_eval 取不到，故单独按 Dict 的键数统计。
    """
    try:
        with open(EDIT, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return 0
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "OPS":
                    return len(node.value.keys)
    return 0


def summary():
    s = _assigns(SCENE)
    p = _assigns(PLACEMENT)
    server = s.get("SERVER_COMPONENT_TYPES") or frozenset()
    verified = s.get("VERIFIED_TYPES") or frozenset()
    unsupported = s.get("UNSUPPORTED_TYPES") or set()
    excluded = s.get("SERVER_EXCLUDED_REASONS") or {}
    return {
        "server_component_types": len(server),
        "verified_types": len(verified),
        "unsupported_types": len(unsupported),
        # 两套阀门不等价：plan_item 走 VERIFIED，place_item 走 SERVER
        # （不等价是**设计**：差集类型各有不放行原因，见 SERVER_EXCLUDED_REASONS）
        "verified_not_server": sorted(verified - server),
        "server_not_verified": sorted(server - verified),
        "verified_not_server_reasons": {str(k): v for k, v in sorted(excluded.items())},
        "max_requests": p.get("MAX_REQUESTS"),
        "max_settled": p.get("MAX_SETTLED"),
        "edit_ops": _count_ops(),
        "mcp_tools": _count_tools(),
    }


def main():
    d = summary()
    argv = sys.argv[1:]
    if "--json" in argv:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    if "--md" in argv:
        print("| 项 | 值 |")
        print("|---|---|")
        print("| SERVER_COMPONENT_TYPES（服务端摆放阀门） | %d |" % d["server_component_types"])
        print("| VERIFIED_TYPES（plan_item 阀门） | %d |" % d["verified_types"])
        print("| UNSUPPORTED_TYPES | %d |" % d["unsupported_types"])
        print("| edit 层 op 数 | %d |" % d["edit_ops"])
        print("| MCP 工具数 | %d |" % d["mcp_tools"])
        print("| MAX_REQUESTS / MAX_SETTLED | %s / %s |" % (d["max_requests"], d["max_settled"]))
        return 0
    print("随笔浮想工具链关键常量（真源：直接从源码 AST 读取）\n")
    print("  SERVER_COMPONENT_TYPES  %d" % d["server_component_types"])
    print("  VERIFIED_TYPES          %d" % d["verified_types"])
    print("  UNSUPPORTED_TYPES       %d" % d["unsupported_types"])
    print("  edit op / MCP tool      %d / %d" % (d["edit_ops"], d["mcp_tools"]))
    print("  MAX_REQUESTS/MAX_SETTLED %s / %s" % (d["max_requests"], d["max_settled"]))
    print("\n  ⚠️ 两套阀门不等价 —— 注意这是**设计**，不是漏加：")
    print("     plan() 准入用 VERIFIED_TYPES，place_item(mode=server) 准入用 SERVER_COMPONENT_TYPES")
    print("     VERIFIED - SERVER (%d)（本地可构造、服务端路径故意不放行，各有原因）："
          % len(d["verified_not_server"]))
    reasons = d["verified_not_server_reasons"]
    for ct in d["verified_not_server"]:
        print("        %9s  %s" % (ct, reasons.get(str(ct), "（未登记原因，见 SERVER_EXCLUDED_REASONS）")))
    print("     SERVER - VERIFIED (%d)：%s（plan 需 allow_unverified=true）"
          % (len(d["server_not_verified"]), d["server_not_verified"]))
    print("\n文档里的手写数字已按本输出校正；以后改代码后重跑本命令即可核对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
