#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxedit — 直接调用游戏内 fuxiang_edit API 层的命令行客户端。

fxcall.py 只能打到桥接层的 op（ping/info/eval/modules/...）。
本工具走与 MCP 完全相同的链路（reload + dispatch），因此可以用来验证
新增的 edit op，不需要重启 MCP 服务器。

用法:
  python fxedit.py <op> [json_args]
  python fxedit.py catalog_summary
  python fxedit.py catalog_find '{"query":"桌子","limit":5}'
  python fxedit.py catalog_export
  python fxedit.py list_types
"""
import os
import sys
import json
import time
import random

# 2026-09-22：改为按 __file__ 推导（此前硬编码 <本机旧路径>，仓库搬家即失效）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(ROOT, "bridge")
CMD_DIR = os.path.join(ROOT, "ipc", "cmd")
OUT_DIR = os.path.join(ROOT, "ipc", "out")
STATE = os.path.join(ROOT, "ipc", "bridge_state.json")


def state_age():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return time.time() - float(s.get("now", 0)), s
    except Exception:
        return None, None


# 心跳每 1.0s 重写一次（tmp+os.replace）。Windows 的 MoveFileEx 替换目标文件内部
# 是「先删后改名」，亚毫秒窗口内读者会拿到 ENOENT；手动误删也有 ≤1s 自愈期。
# 对 None（读不到）短重试跨过该窗口，不要把瞬时缺失误报成「桥接未运行」。
# 2026-09-19 建模师在 jp-city-0524 实机命中一次（16:23，桥实际存活）。
STATE_RETRY_SEC = 2.5


def call_bridge(op, args=None, timeout=60.0):
    age, _st = state_age()
    if age is None:
        deadline = time.time() + STATE_RETRY_SEC
        while age is None and time.time() < deadline:
            time.sleep(0.05)
            age, _st = state_age()
    if age is None or age > 10:
        print("桥接未运行（心跳 %s）。请先运行 fuxiang-inject install" %
              ("状态文件 %ss 内始终读不到" % STATE_RETRY_SEC if age is None else age),
              file=sys.stderr)
        return None
    cid = "cli%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    with open(os.path.join(CMD_DIR, cid + ".json.tmp"), "w", encoding="utf-8") as f:
        json.dump({"id": cid, "op": op, "args": args or {}}, f, ensure_ascii=False)
    os.replace(os.path.join(CMD_DIR, cid + ".json.tmp"),
               os.path.join(CMD_DIR, cid + ".json"))
    out_path = os.path.join(OUT_DIR, cid + ".json")
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        if os.path.exists(out_path):
            for _ in range(20):
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    break
                except Exception:
                    time.sleep(0.01)
            break
        time.sleep(0.04)
    if data is None:
        print("等待结果超时: %s" % out_path, file=sys.stderr)
        return None
    # 回执**保留**，不再 os.remove（2026-09-19 起）。删除是纯清理动作，却会被宿主
    # 的 safe-delete 钩子按批量计数（实测 50/turn 拦下并静默终止进程：退出码 1、
    # 无 traceback，见 cli/fxlong.py 与 builds/accept128/accept.py）。循环类脚本
    # 一旦撞上就是在批中途死掉，且现场没有任何线索。
    # 游戏侧只清 ipc/cmd、从不枚举 ipc/out，读取一律按精确 cid 路径，因此残留文件惰性、
    # 不影响正确性；要归拢请用 cli/fxsweep.py（shutil.move 改名，不产生删除）。
    return data


def call_edit(op, args=None, timeout=60.0):
    payload = json.dumps({"op": op, "args": args or {}}, ensure_ascii=False)
    # 守卫式插入（同 fxapi._dispatch_code）：本函数每次 dispatch 都执行，
    # 无条件 insert 会在游戏进程 sys.path 里按次累积重复桥路径（2026-10-03 修复）。
    code = ("import sys\n"
            "if %r not in sys.path:\n"
            "    sys.path.insert(0, %r)\n"
            "from fuxiang_reload import smart_reload\n"
            "m = smart_reload('fuxiang_edit')\n"
            "__result__ = m.dispatch(%r)\n" % (BRIDGE_DIR, BRIDGE_DIR, payload))
    data = call_bridge("eval", {"code": code, "timeout": timeout}, timeout=timeout)
    if data is None:
        return None
    if not data.get("ok"):
        print("桥接执行失败:\n%s" % data.get("error"), file=sys.stderr)
        return None
    val = data.get("value")
    if isinstance(val, dict) and "error" in val:
        print("桥接 eval 出错:\n%s" % val["error"], file=sys.stderr)
        return None
    return val.get("result") if isinstance(val, dict) else None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1]
    args = {}
    if len(sys.argv) > 2:
        try:
            args = json.loads(sys.argv[2])
        except Exception as e:
            print("args 不是合法 JSON: %s" % e, file=sys.stderr)
            return 2
    res = call_edit(op, args)
    if res is None:
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
