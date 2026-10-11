#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxapi — 与游戏内 fuxiang_edit 通信的**唯一真源**（2026-09-22 通道收敛）。

背景：此前同一件事有三条通道（裸 IPC / HTTP:17699 / MCP stdio）和两套剥壳约定，
导致项目里散落 9 份各自实现的 unwrap，`fxedit` 停在信封层、`fxlong` 停在业务值层，
换 import 就出错。HTTP 与 MCP stdio 现已默认停用，本模块收敛剩下的裸 IPC 通道。

返回值信封（自外向内）：
    L0  {"id","ok","value","elapsed"}            桥接回执      fuxiang_bridge.py:244
    L1  {"result": ...}                          桥 eval 值    fuxiang_bridge.py:168
    L2  {"ok":..., "value": X} 或 {"ok":false,"error":...}   dispatch  fuxiang_edit.py:1850
    L3  X                                        业务值（op 自己的返回）
    ※ eval op 的 X 又是 {"result": 实际值}，再剥一层。

本模块统一提供两个粒度，**避免再出现「换 import 就要改调用方」**：

    call(op, args)           → L3 业务值（eval 再剥一层 result）；出错抛 RuntimeError
    call_envelope(op, args)  → L2 dispatch 信封；出错返回 None 并打印（兼容 fxedit 语义）

用法:
    from fxapi import call, call_envelope, unwrap, eval_json
    r = call('place_item', {'item_id': 202009, 'position': [0, 0, 0]})
    env = call_envelope('catalog_find', {'query': '桌子'})

⚠️ 与 cli/fxedit.py::call_edit 的关系：两者**语义等价**（都返回 L2 信封），fxedit 保留
原实现是因为它正在被其它智能体的在跑脚本 import；默认语义统一（4b）延后到它们收工后。
新脚本一律用本模块。
"""
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BRIDGE_DIR = os.path.join(ROOT, "bridge")
CMD_DIR = os.path.join(ROOT, "ipc", "cmd")
OUT_DIR = os.path.join(ROOT, "ipc", "out")
STATE = os.path.join(ROOT, "ipc", "bridge_state.json")

# 心跳每 1.0s 重写一次（tmp+os.replace）。Windows 的 MoveFileEx 替换目标文件内部
# 是「先删后改名」，亚毫秒窗口内读者会拿到 ENOENT；手动误删也有 ≤1s 自愈期。
# 对 None（读不到）短重试跨过该窗口，不要把瞬时缺失误报成「桥接未运行」。
STATE_RETRY_SEC = 2.5
STATE_MAX_AGE = 10.0

DEFAULT_TIMEOUT = 120.0     # 长跑友好；短调用可显式覆盖


def state_age():
    """返回 (心跳年龄秒, 状态字典)；读不到返回 (None, None)。"""
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return time.time() - float(s.get("now", 0)), s
    except Exception:
        return None, None


def call_bridge(op, args=None, timeout=60.0):
    """发一条桥接层命令并等回执。返回 L0 完整回执；桥未运行时返回 None。

    ⚠️ 回执**不删除**（2026-09-19 起）。删除是纯清理动作，却会被宿主的 safe-delete
    钩子按批量计数（实测 50/turn 拦下并静默终止进程：退出码 1、无 traceback），
    循环类脚本一旦撞上就是在批中途死掉且无现场线索。要归拢用 cli/fxsweep.py
    （shutil.move 改名归档，不产生删除）。
    """
    age, _st = state_age()
    if age is None:
        deadline = time.time() + STATE_RETRY_SEC
        while age is None and time.time() < deadline:
            time.sleep(0.05)
            age, _st = state_age()
    if age is None or age > STATE_MAX_AGE:
        sys.stderr.write("桥接未运行（心跳 %s）。请先运行 fuxiang-inject install\n" %
                         ("状态文件 %ss 内始终读不到" % STATE_RETRY_SEC if age is None
                          else "%.1fs 前" % age))
        return None
    cid = "fxapi%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    tmp = os.path.join(CMD_DIR, cid + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"id": cid, "op": op, "args": args or {}}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(CMD_DIR, cid + ".json"))
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
        sys.stderr.write("等待结果超时: %s\n" % out_path)
    return data


def _dispatch_code(op, args):
    """生成在游戏内执行 fuxiang_edit.dispatch 的代码（用 smart_reload 按 mtime 缓存）。"""
    payload = json.dumps({"op": op, "args": args or {}}, ensure_ascii=False)
    # 守卫式插入：dispatch 每次**都**跑这段代码，无条件 insert 的话一次 1.5 万件
    # 还原会在游戏进程 sys.path 里按次累积几千条重复桥路径（2026-10-03 修复）。
    return ("import sys\n"
            "if %r not in sys.path:\n"
            "    sys.path.insert(0, %r)\n"
            "from fuxiang_reload import smart_reload\n"
            "m = smart_reload('fuxiang_edit')\n"
            "__result__ = m.dispatch(%r)\n" % (BRIDGE_DIR, BRIDGE_DIR, payload))


def _send(op, args, timeout):
    """下发一次 dispatch，返回 L2 信封；桥/执行出错时抛或返回 None（由调用方决定）。"""
    data = call_bridge("eval", {"code": _dispatch_code(op, args), "timeout": timeout},
                       timeout=timeout)
    if data is None:
        return None, "桥接无响应（心跳过期？）: %s" % op
    if not data.get("ok"):
        return None, "桥接执行失败 %s: %s" % (op, str(data.get("error"))[:400])
    inner = data.get("value")
    if isinstance(inner, dict) and "error" in inner:
        return None, "%s 出错: %s" % (op, str(inner["error"])[:400])
    # L1 → L2
    envelope = inner.get("result") if isinstance(inner, dict) else None
    if isinstance(envelope, dict) and envelope.get("ok") is False \
            and "error" in envelope:
        # 2026-10-02 实机（批量摆放 run-2）：dispatch 的**失败信封**
        # （fuxiang_edit.dispatch 捕获异常后返回 {"ok":False,"error":...,"details":...}）
        # 此前只被 call_envelope 拦，`call` 把它当**业务值**原样返回 —— 于是
        # 「op 抛异常」（如护栏拒绝整批）在调用方眼里就是一个缺了预期字段的普通
        # dict：批量摆放把它当"每件都失败"记了 100 行 exception，**一行原因都没留下**。
        # 本模块的契约本来就是「call 出错抛 RuntimeError」，这里把 L2 补齐。
        # 只有 dispatch 自己的失败信封会命中：业务值整包在 L2 的 value 里，
        # op 自己返回 {"ok":False,...} 时 L2 的 ok 仍是 True，语义不受影响。
        return None, "%s 出错: %s" % (op, str(envelope["error"])[:400])
    return envelope, None


def unwrap(value, depth=8):
    """逐层剥壳取业务结果，不写死层数（桥接返回值可能多包一层）。

    L2 {"ok","value"} → L3；L3 里若仍是 {"result":...}（eval 的形态）继续剥。
    """
    while depth > 0 and isinstance(value, dict):
        if "result" in value and set(value) <= {"ok", "value", "result"}:
            value, depth = value["result"], depth - 1
        elif "value" in value and set(value) <= {"ok", "value"}:
            value, depth = value["value"], depth - 1
        else:
            break
    return value


def unwrap_key(node, key, depth=6):
    """逐层下探，找到**含 key 的那一层**并返回该层的 `node[key]`。

    用于"返回值层数不确定、但知道字段名"的调用方（cli/event_*.py、cli/gadget.py）。
    与 `unwrap` 的区别：unwrap 是"剥到底"，本函数是"按字段名定位"。
    """
    for _ in range(depth):
        if not isinstance(node, dict):
            return None
        if key in node:
            return node[key]
        nxt = (node.get("value") if isinstance(node.get("value"), dict)
               else node.get("result"))
        if not isinstance(nxt, dict) or nxt is node:
            return None
        node = nxt
    return None


def find_layer(node, key, depth=6):
    """逐层下探，找到**含 key 的那一层**并返回**该层本身**（不是 node[key]）。

    用于"要整层一起用"的调用方（cli/scan_*.py 取 items 所在层）。
    """
    for _ in range(depth):
        if not isinstance(node, dict):
            return None
        if key in node:
            return node
        nxt = (node.get("value") if isinstance(node.get("value"), dict)
               else node.get("result"))
        if not isinstance(nxt, dict) or nxt is node:
            return None
        node = nxt
    return None


def find_layer_any(node, keys, depth=6):
    """逐层下探，找到**含 keys 中任意一个**的那一层并返回该层。

    注意语义：是"同一层里命中任一即返回"，不是"逐个 key 分别下探"
    （后者会跳过只含第二个 key 的浅层，与 cli/event_import.py 原实现不等价）。
    """
    keys = tuple(keys)
    for _ in range(depth):
        if not isinstance(node, dict):
            return None
        if any(k in node for k in keys):
            return node
        nxt = (node.get("value") if isinstance(node.get("value"), dict)
               else node.get("result"))
        if not isinstance(nxt, dict) or nxt is node:
            return None
        node = nxt
    return None


def envelope_value(res):
    """只剥一层 L2 外壳取 value（{"ok","value"} → X）；已是业务值时原样返回。

    用于"需要自己读 ok/error"的调用方（cli/story.py 的 _unwrap 即此语义）。
    """
    if isinstance(res, dict) and "value" in res:
        v = res["value"]
        return v if isinstance(v, dict) else res
    return res


def call(op, args=None, timeout=DEFAULT_TIMEOUT):
    """调一个 edit op，返回**业务值**（L3，eval 再剥一层）；出错抛 RuntimeError。"""
    envelope, err = _send(op, args, timeout)
    if err:
        raise RuntimeError(err)
    return unwrap(envelope)


def call_envelope(op, args=None, timeout=60.0):
    """调一个 edit op，返回 **L2 dispatch 信封** {"ok","value"}；出错返回 None 并打印。

    语义与 cli/fxedit.py::call_edit 完全一致，用于需要读 ok/error 的调用方。
    """
    envelope, err = _send(op, args, timeout)
    if err:
        sys.stderr.write(err + "\n")
        return None
    return envelope


def eval_json(code, timeout=DEFAULT_TIMEOUT):
    """执行 eval 并把 __result__（JSON 字符串）解析成 Python 对象。"""
    value = call("eval", {"code": code}, timeout=timeout)
    if isinstance(value, str):
        return json.loads(value)
    return value


def main():
    """命令行入口：python cli/fxapi.py <op> [json_args] [--envelope]"""
    argv = sys.argv[1:]
    envelope = "--envelope" in argv
    if envelope:
        argv.remove("--envelope")
    if not argv:
        print(__doc__)
        return 2
    op = argv[0]
    args = {}
    if len(argv) > 1:
        try:
            args = json.loads(argv[1])
        except Exception as e:
            sys.stderr.write("args 不是合法 JSON: %s\n" % e)
            return 2
    try:
        res = call_envelope(op, args) if envelope else call(op, args)
    except RuntimeError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    if res is None:
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if not isinstance(res, dict) or res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
