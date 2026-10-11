#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxhttp — 随笔浮想编辑器 HTTP API 的 curl 式命令行客户端。

依赖仅标准库 urllib.request。需先启动服务：
  python mcp/http_server.py [--host 127.0.0.1] [--port 17699]

⛔ 2026-09-22 起 HTTP 通道默认停用（零鉴权 + /api/op 任意透传含 eval）。
  本客户端随之默认拒绝；编辑请改用等同能力的 CLI：
      python cli/fxedit.py <op> [json_args]
      python cli/fxcall.py eval "<code>"
  确需临时使用：PowerShell $env:FUXIANG_ENABLE_HTTP=1（同时用该变量启动 mcp/http_server.py）。

用法:
  python cli/fxhttp.py health
  python cli/fxhttp.py tools
  python cli/fxhttp.py call <tool_name> ['<json args>']
  python cli/fxhttp.py op <op> ['<json args>']
  python cli/fxhttp.py call fuxiang_status --base http://127.0.0.1:17699
"""
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_BASE = "http://127.0.0.1:17699"
HTTP_ENABLE_ENV = "FUXIANG_ENABLE_HTTP"


def request(base, method, path, payload=None, timeout=120):
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error": str(e)}
        return e.code, body
    except urllib.error.URLError as e:
        return 0, {"ok": False,
                   "error": "无法连接 %s（服务未启动？请先运行 mcp/http_server.py）: %s" % (url, e)}


def _args(payload_str):
    if not payload_str:
        return {}
    try:
        v = json.loads(payload_str)
        return v if isinstance(v, dict) else {"arg": v}
    except Exception:
        print("参数不是合法 JSON: %s" % payload_str, file=sys.stderr)
        sys.exit(2)


def main():
    if os.environ.get(HTTP_ENABLE_ENV) != "1":
        print("HTTP API 已停用（2026-09-22 安全收敛）。\n"
              "编辑请改用 CLI：python cli/fxedit.py <op> [json_args]\n"
              "确需临时启用：PowerShell $env:%s=1 ／ cmd set %s=1（服务端同样需要该变量）。"
              % (HTTP_ENABLE_ENV, HTTP_ENABLE_ENV),
              file=sys.stderr)
        return 2
    base, argv = DEFAULT_BASE, sys.argv[1:]
    if "--base" in argv:
        i = argv.index("--base")
        base = argv[i + 1]
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]

    if cmd == "health":
        status, body = request(base, "GET", "/api/health")
    elif cmd == "tools":
        status, body = request(base, "GET", "/api/tools")
    elif cmd in ("call", "op"):
        if len(argv) < 2:
            print("缺少工具名/op", file=sys.stderr)
            return 2
        name = argv[1]
        payload = _args(argv[2] if len(argv) > 2 else None)
        path = "/api/tools/%s" % name if cmd == "call" else "/api/op/%s" % name
        status, body = request(base, "POST", path, payload)
    elif cmd == "route":
        status, body = request(base, "GET", "/api/route")
    else:
        print(__doc__)
        return 2

    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0 if status and 200 <= status < 300 else (1 if status else 2)


if __name__ == "__main__":
    sys.exit(main())