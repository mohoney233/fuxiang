#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fxcall — 与游戏内 fuxiang_bridge 通信的最小客户端。

直接读写 tools/fuxiang/ipc 目录，不需要管理员权限（桥已安装时）。
桥未安装时提示先用 fuxiang-inject install。

用法:
  python fxcall.py ping
  python fxcall.py eval "import game3d; __result__ = game3d.get_frame_count()"
  python fxcall.py execfile <path.py>
  python fxcall.py info
"""
import os, sys, json, time, random

# 2026-09-22：改为按 __file__ 推导（此前硬编码 <本机旧路径>，仓库搬家即失效）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


# 与 fxedit.STATE_RETRY_SEC 同因：MoveFileEx 替换目标的亚毫秒缺失窗口 +
# 心跳 1.0s 重写，对「读不到」短重试，避免把瞬时缺失误报成桥接未运行。
STATE_RETRY_SEC = 2.5

# eval 有两个同名通道：桥层原始 op_eval（本文件直达，命名空间只有 __result__）与
# edit 层 fuxiang_edit.op_eval（fxedit/MCP 走 dispatch 进来，预置 core/cm/em/jdump/plain）。
# 2026-09-19 建模师按提示词「core/cm 是 eval 预置变量」用了 fxcall.call('eval') →
# NameError: name 'cm' is not defined。这里给桥层也补上同一套预置，让两个通道语义一致。
# 整段包在 try 里：取不到预置也绝不能把底层通道弄坏（fxedit/fxlong 都靠它传输）。
_PRESET = (
    "try:\n"
    "    import sys\n"
    "    if %r not in sys.path:\n"
    "        sys.path.insert(0, %r)\n"
    "    from fuxiang_reload import smart_reload as _fx_sr\n"
    "    _fx = _fx_sr('fuxiang_edit')\n"
    "    _fxp = _fx.eval_presets()\n"
    "    core = _fxp.get('core'); room = _fxp.get('room')\n"
    "    cm = _fxp.get('cm'); em = _fxp.get('em')\n"
    "    GK = _fxp.get('GK'); jdump = _fxp.get('jdump'); plain = _fxp.get('plain')\n"
    "    FS = _fxp.get('FS'); FE = _fxp.get('FE')\n"
    "except Exception:\n"
    "    pass\n"
) % (os.path.join(ROOT, "bridge"), os.path.join(ROOT, "bridge"))


def call(op, args=None, timeout=60.0, poll=0.05):
    if op == "eval" and isinstance(args, dict) and isinstance(args.get("code"), str):
        args = dict(args)
        args["code"] = _PRESET + args["code"]
    age, st = state_age()
    if age is None:
        deadline = time.time() + STATE_RETRY_SEC
        while age is None and time.time() < deadline:
            time.sleep(0.05)
            age, st = state_age()
    if age is None or age > 10:
        print("桥接未运行（心跳: %s）。请先运行: fuxiang-inject install" % (
            "状态文件 %ss 内始终读不到" % STATE_RETRY_SEC if age is None else age),
            file=sys.stderr)
        return None

    cid = "c%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    cmd = {"id": cid, "op": op, "args": args or {}}
    cmd_path = os.path.join(CMD_DIR, cid + ".json")
    tmp = cmd_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False)
    os.replace(tmp, cmd_path)

    out_path = os.path.join(OUT_DIR, cid + ".json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(out_path):
            # 等写入完成
            for _ in range(20):
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    time.sleep(0.01)
            break
        time.sleep(poll)
    print("超时等待结果: %s" % out_path, file=sys.stderr)
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1]
    if op == "execfile":
        if len(sys.argv) < 3:
            print("缺少文件路径", file=sys.stderr)
            return 2
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            code = f.read()
        args = {"code": code}
        if len(sys.argv) > 3:
            args["timeout"] = float(sys.argv[3])
        op = "eval"
    elif op == "eval":
        args = {"code": sys.argv[2]}
    else:
        args = {}
    res = call(op, args)
    if res is None:
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
