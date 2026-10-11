#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 MCP 工具调用辅助：一次 stdio 会话调一个工具。

⚠️ 2026-09-22 起 MCP stdio 通道**默认停用**（零鉴权 + 与 CLI 能力等价，属重复通道）。
本文件保留只为「临时排障」：确需时 PowerShell $env:FUXIANG_ENABLE_MCP=1 再用。
日常编辑请直接用 CLI（python cli/fxedit.py <op> [json_args]），出图用 cli/fxshot.py。

用法：
    python cli/mcpcall.py <tool_name> ['<json args>'] [--timeout 1800]

示例（仅在 FUXIANG_ENABLE_MCP=1 时有效）：
    python cli/mcpcall.py fuxiang_status
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp", "server.py")
MCP_ENABLE_ENV = "FUXIANG_ENABLE_MCP"


def mcp_enabled():
    return os.environ.get(MCP_ENABLE_ENV) == "1"


def call(name, args=None, timeout=1800):
    if not mcp_enabled():
        raise RuntimeError(
            "MCP 通道已停用（2026-09-22 收敛）。等价 CLI：python cli/fxedit.py <op> [json_args]"
            "；确需临时启用：PowerShell $env:%s=1 ／ cmd set %s=1"
            % (MCP_ENABLE_ENV, MCP_ENABLE_ENV))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mcpcall", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": args or {}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-u", SERVER],
        input="".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode:
        raise RuntimeError("MCP 进程异常退出：\n%s" % proc.stderr[-2000:])
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("MCP 无输出。stderr:\n%s" % proc.stderr[-2000:])
    reply = json.loads(lines[-1])["result"]
    text = reply["content"][0]["text"]
    if reply.get("isError"):
        raise RuntimeError(text)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
    timeout = 1800
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
    try:
        out = call(name, args, timeout)
    except RuntimeError as e:
        # 通道停用时给出干净提示而非裸 traceback（与 cli/fxhttp.py 一致）
        sys.stderr.write("%s\n" % e)
        sys.exit(2)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
