#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fuxiang-http — 《第五人格》「随笔浮想」编辑器的 HTTP/JSON API 服务。

复用 mcp/server.py 的分发层（TOOLS / TOOL_MAP / call_edit / handle_tool_call），
把全部编辑器能力以 Rest-ish 的 HTTP/JSON 暴露给脚本、curl 与一般程序。

链路：
  curl / 脚本 ─HTTP/JSON→ 本进程(ThreadingHTTPServer)
       └→ server.handle_tool_call / server.call_edit
              └→ ipc/cmd + ipc/out 目录 ─→ 游戏内 fuxiang_bridge → 主线程

说明：
  * 默认仅绑定 127.0.0.1（本机调用）。如需局域网访问，用 --host 显式监听。
  * 桥是单一会话（文件握手），所有触桥调用用全局锁串行化，避免并发覆盖请求文件。
  * 环境要求：游戏已进入「随笔浮想」编辑器且桥已装（fuxiang-inject install）。

用法:
  python mcp/http_server.py [--host 127.0.0.1] [--port 17699]

⛔ 2026-09-22 起默认停用（安全收敛）。
  停用原因：本服务**零鉴权**，且 `/api/op/<op>` 允许任意 op 透传（含 `eval`），
  等价于「游戏进程内任意 Python 代码执行」；绑定回环只能挡住外部主机，挡不住
  本机任意进程与浏览器（无 Origin/CORS 校验）。且 preview 工具故意绕全局锁，
  会与锁内调用交错。
  编辑能力已 100% 由 cli/ 覆盖（`cli/fxedit.py <op>` 可透传任意 op），无需本通道。

  确需临时启用时显式放开：
      PowerShell:  $env:FUXIANG_ENABLE_HTTP=1; python mcp/http_server.py
      cmd:         set FUXIANG_ENABLE_HTTP=1 && python mcp/http_server.py
  启用期间只允许本机可信进程访问，用完立即关闭。
"""
import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  # 复用 mcp/server.py 的分发层

# 桥失联时返回给调用方的安装提示
INSTALL_HINT = ("游戏未运行或桥接未安装/心跳过期。请先进入「随笔浮想」编辑器并以管理员身份运行："
                "tools\\fuxiang\\injector\\bin\\Debug\\net8.0-windows\\fuxiang-inject.exe install")

# 桥为单一会话（ipc/cmd + ipc/out 文件握手），并发调用会互相覆盖请求文件，必须串行。
_bridge_lock = threading.Lock()


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------
def _json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def _parse_value(content):
    """handle_tool_call 返回 [{"type":"text","text": <json>}]，反解析出 value。"""
    if not content:
        return None
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except Exception:
        return {"value_raw": text}


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise _HttpError(400, "请求体不是合法 JSON")


def _args_from_query(handler):
    """支持 ?args=<json-encoded> 作为 GET 调用的参数。"""
    from urllib.parse import parse_qs, unquote
    qs = dict(parse_qs(handler.path.split("?", 1)[-1]))
    raw = (qs.get("args") or [""])[0]
    if not raw:
        return {}
    try:
        return json.loads(unquote(raw))
    except Exception:
        raise _HttpError(400, "query ?args= 不是合法 JSON")


class _HttpError(Exception):
    """带状态码的错误，code 为 HTTP 状态码。"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# 桥调用（串行）与错误分类
# --------------------------------------------------------------------------
def _bridge(fn, *args, **kw):
    """在全局锁内执行一次触桥调用。"""
    with _bridge_lock:
        return fn(*args, **kw)


def _classify(e):
    """把异常归类为 (http_code, body_dict)。"""
    msg = str(e)
    if isinstance(e, server.BridgeError):
        if ("桥接未安装" in msg or "心跳已过期" in msg or "桥接未运行" in msg):
            return 503, {"ok": False, "error": msg, "hint": INSTALL_HINT}
        if ("参数错误" in msg or "未知工具" in msg or "未知预览动作" in msg):
            return 400, {"ok": False, "error": msg}
        # 其余 BridgeError 是游戏侧/桥侧执行失败
        return 502, {"ok": False, "error": msg}
    return 500, {"ok": False, "error": (msg or traceback.format_exc())}


class Handler(BaseHTTPRequestHandler):
    server_version = "fuxiang-http/1.0"

    # ---- 基础辅助 ----
    def _send(self, code, obj):
        body = _json(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
        pass

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/" or path == "":
                return self._send(200, {
                    "ok": True,
                    "server": server.SERVER_NAME,
                    "version": server.SERVER_VERSION,
                    "hello": "随笔浮想编辑器 HTTP API. 接口文档见 tools/fuxiang/API.md",
                    "routes": ["/api/health", "/api/tools", "/api/tools/<tool>",
                               "/api/op/<op>", "/api/op", "/api/route"],
                })
            if path == "/api/health":
                return self._health()
            if path == "/api/tools":
                return self._send(200, tools_payload())
            if path == "/api/route":
                return self._routes()
            if path.startswith("/api/tools/"):
                name = path[len("/api/tools/"):].lstrip("/")
                return self._call_tool(name, _args_from_query(self))
            if path.startswith("/api/op/"):
                op = path[len("/api/op/"):].lstrip("/")
                return self._call_op(op, _args_from_query(self))
        except _HttpError as e:
            return self._send(e.code, {"ok": False, "error": str(e)})
        except server.BridgeError as e:
            code, body = _classify(e)
            return self._send(code, body)
        except Exception as e:
            return self._send(500, {"ok": False, "error": (str(e) or traceback.format_exc())})
        return self._send(404, {"ok": False, "error": "not found: %s" % self.path})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path.startswith("/api/tools/"):
                name = path[len("/api/tools/"):].lstrip("/")
                return self._call_tool(name, _read_body(self))
            if path.startswith("/api/op/"):
                op = path[len("/api/op/"):].lstrip("/")
                return self._call_op(op, _read_body(self))
            if path == "/api/op":
                body = _read_body(self)
                op = body.get("op")
                if not op:
                    raise _HttpError(400, "缺少 op 字段")
                return self._call_op(str(op), body.get("args") or {})
        except _HttpError as e:
            return self._send(e.code, {"ok": False, "error": str(e)})
        except server.BridgeError as e:
            code, body = _classify(e)
            return self._send(code, body)
        except Exception as e:
            return self._send(500, {"ok": False, "error": (str(e) or traceback.format_exc())})
        return self._send(404, {"ok": False, "error": "not found: %s" % self.path})

    # ---- 各端点实现 ----
    def _health(self):
        st = server.bridge_state()
        if st is None:
            return self._send(200, {"ok": True, "server": server.SERVER_NAME,
                                    "version": server.SERVER_VERSION,
                                    "bridge_alive": False, "bridge_age": None,
                                    "bridge_installed": False})
        from time import time
        age = round(time() - float(st.get("now", 0)), 2)
        return self._send(200, {"ok": True, "server": server.SERVER_NAME,
                                "version": server.SERVER_VERSION,
                                "bridge_alive": age <= server.STATE_MAX_AGE,
                                "bridge_age": age, "bridge_installed": True,
                                "bridge_pid": st.get("pid")})

    def _routes(self):
        return self._send(200, {"ok": True, "routes": {
            "GET /": "说明与路由列表",
            "GET /api/health": "健康与桥状态",
            "GET /api/tools": "工具清单（名称/描述/schema）",
            "POST /api/tools/<tool>": "调用编辑器工具，body 为参数 JSON",
            "POST /api/op/<op>": "原始 edit op 透传",
            "POST /api/op": "原始 op 透传，body 形如 {op, args}",
            "GET /api/route": "本路由清单",
        }})

    def _call_tool(self, name, args):
        if name not in server.TOOL_MAP:
            raise _HttpError(404, "未知工具: %s（用 /api/tools 查看全部）" % name)
        if name.startswith("fuxiang_preview_"):
            # 预览调用会跨帧等待/文件轮询，属长调用，放到锁外执行
            content = server.handle_tool_call(name, args)
            return self._send(200, {"ok": True, "tool": name, "value": _parse_value(content)})
        content = _bridge(server.handle_tool_call, name, args)
        return self._send(200, {"ok": True, "tool": name, "value": _parse_value(content)})

    def _call_op(self, op, args):
        value = _bridge(server.call_edit, op, args)
        return self._send(200, {"ok": True, "op": op, "value": value})


def tools_payload():
    return {"ok": True, "count": len(server.TOOLS),
            "tools": [{"name": t["name"], "description": t["description"],
                       "inputSchema": t["inputSchema"]} for t in server.TOOLS]}


HTTP_ENABLE_ENV = "FUXIANG_ENABLE_HTTP"


def main():
    # 默认停用：无鉴权 + /api/op 任意透传（含 eval）= 游戏进程内任意代码执行。
    if os.environ.get(HTTP_ENABLE_ENV) != "1":
        print("HTTP API 已停用（2026-09-22 安全收敛）。\n"
              "原因：本服务零鉴权，且 /api/op/<op> 允许任意 op 透传（含 eval）。\n"
              "编辑请改用 CLI：python cli/fxedit.py <op> [json_args]\n"
              "确需临时启用：PowerShell $env:%s=1 ／ cmd set %s=1，后重试（用完立即关闭）。"
              % (HTTP_ENABLE_ENV, HTTP_ENABLE_ENV),
              file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="随笔浮想编辑器 HTTP/JSON API 服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认本机 127.0.0.1）")
    ap.add_argument("--port", type=int, default=17699, help="监听端口（默认 17699）")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("随笔浮想 HTTP API 已启动: http://%s:%d" % (args.host, args.port))
    print("  工具 %d 个。命令行客户端: python cli/fxhttp.py health|calls|tools" % len(server.TOOLS))
    to_health = _health_probe()
    print("  桥状态: %s" % to_health)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")


def _health_probe():
    st = server.bridge_state()
    if st is None:
        return "未安装/未启动（fuxiang-inject install）"
    from time import time
    age = round(time() - float(st.get("now", 0)), 2)
    if age <= server.STATE_MAX_AGE:
        return "在线 (pid=%s, age=%.2fs)" % (st.get("pid"), age)
    return "心跳过期 (age=%.2fs)" % age


if __name__ == "__main__":
    sys.exit(main())