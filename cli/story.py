#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli/story.py — 随笔浮想「剧情编辑」命令行入口。

用法
----
  python cli/story.py tree [--compact]                  看当前地图的剧情树
  python cli/story.py fields [story|dialogue|choice]    看可写字段与允许值
  python cli/story.py set <ugc_id> -f name=xxx -f content=yyy [--dry-run]
  python cli/story.py create <story|dialogue|choice> -f k=v ...    （自动轮询到 placed）
  python cli/story.py create-many <kind> <n> [-f k=v ...]   批量建 n 个同类节点
  python cli/story.py delete <ugc_id[,ugc_id...]> [--cascade] [--force] [--dry-run]
                                                    （--force = allow_unregistered，剧情节点必需）
  python cli/story.py export [--out 路径.json|.md]      导出剧情文件
  python cli/story.py import <路径> [--apply] [--prune] 比对/应用剧情文件
  python cli/story.py resume                          续跑被中断的导入（进度在编辑器会话里）

说明
----
* 创建节点走服务端异步协议：本脚本在**游戏进程外**轮询（游戏主线程里禁止等待），
  默认最多等 30 秒。
* 导入默认 dry_run（只打印计划）；加 --apply 才写入，创建部分会自动串成
  begin → poll → 落字段 → 下一个。
* 任何写入后都要在游戏内保存（Ctrl+S / 保存按钮）才会落库。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fxedit import call_edit  # noqa: E402
from fxapi import envelope_value as _envelope_value  # noqa: E402  # 剥壳真源

POLL_TIMEOUT = 30.0
POLL_FIRST = 0.0
POLL_INTERVAL = 0.15


def _parse_fields(pairs):
    """接受 `k=v`（推荐）、`-f=k=v` 与 `-f k=v` 三种写法。"""
    out = {}
    norm = []
    skip = False
    for i, p in enumerate(pairs):
        if skip:
            skip = False
            continue
        if p == "-f":                      # -f k=v
            if i + 1 < len(pairs):
                norm.append(pairs[i + 1])
                skip = True
            continue
        if p.startswith("-f="):            # -f=k=v
            norm.append(p[3:])
            continue
        norm.append(p)
    for p in norm:
        if p.startswith("--"):
            continue
        if "=" not in p:
            raise ValueError("字段必须写成 key=value：%r" % p)
        k, v = p.split("=", 1)
        k = k.strip()
        v = v.strip()
        if v.lower() in ("none", "null", ""):
            out[k] = None
        elif v.isdigit():
            out[k] = int(v)
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _unwrap(res):
    """dispatch 的返回是 {ok, value} 外壳；判断状态时要用 value 层。

    剥壳规则见 cli/fxapi.py::envelope_value（唯一真源）。
    """
    return _envelope_value(res)


def _print(res):
    if res is None:
        print("调用失败（桥接未运行或超时）")
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if isinstance(res, dict) and res.get("ok") is False:
        return 1
    return 0


def cmd_tree(args):
    # 与 set/create/delete 一致：剥掉 L2 外壳再打印，否则同一 CLI 里两种输出形态。
    # 出错时 dispatch 回 {"ok":false,"error":...}（无 value 键），_unwrap 原样返回，
    # _print 仍能据 res["ok"] 给出非零退出码。
    return _print(_unwrap(call_edit("story_tree", {"compact": "--compact" in args})))


def cmd_fields(args):
    kind = args[0] if args else None
    return _print(_unwrap(call_edit("story_fields", {"kind": kind})))


def cmd_set(args):
    if not args:
        print("需要 ugc_id")
        return 2
    ugc_id = args[0]
    fields = _parse_fields([a for a in args[1:] if a.startswith("-f=") or "=" in a])
    dry = "--dry-run" in args
    return _print(call_edit("story_set", {"ugc_id": int(ugc_id), "fields": fields,
                                          "dry_run": dry}))


def _wait_poll(op, request_id, timeout=POLL_TIMEOUT):
    """在游戏进程外轮询（游戏主线程内禁止 sleep）。

    单次调用取不到结果（IPC 抖动 / 主线程忙）不算失败：继续轮询到超时为止。
    2026-09-21 实机：导入链中途一次 None 直接放弃，留下半截导入（6 个选项没建）。
    """
    deadline = time.time() + timeout
    delay = POLL_FIRST
    last = None
    while time.time() < deadline:
        if delay:
            time.sleep(delay)
        try:
            got = _unwrap(call_edit(op, {"request_id": request_id}))
        except Exception:
            got = None
        if got is not None:
            last = got
            status = last.get("status")
            if status in ("placed", "ambiguous", "conflict", "finalize_failed", "overdue",
                          "unknown", "timeout"):
                return last
        delay = POLL_INTERVAL
    print("轮询超时（%ss），最后一次状态：%s" % (timeout, (last or {}).get("status")),
          file=sys.stderr)
    return last


def cmd_create(args):
    if not args:
        print("需要 kind（story / dialogue / choice）")
        return 2
    kind = args[0]
    fields = _parse_fields(args[1:])
    res = _unwrap(call_edit("story_create", {"kind": kind, "fields": fields}))
    if res is None:
        return 1
    rid = res.get("request_id")
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if not rid:
        print("创建未受理：%s" % res.get("error"), file=sys.stderr)
        return 1
    poll = _wait_poll("story_create_poll", rid)
    print(json.dumps(poll, ensure_ascii=False, indent=1, default=str))
    return 0 if poll and poll.get("status") == "placed" else 1


def cmd_delete(args):
    if not args:
        print("需要 ugc_id（多个用逗号分隔）")
        return 2
    ids = [int(x) for x in args[0].split(",") if x.strip()]
    dry = "--dry-run" in args
    cascade = "--cascade" in args
    # 剧情三件套挂在共享的 UgcGlobalUnit 上，不在编辑器的对象登记集合里，
    # 删除必须显式 allow_unregistered（--force）。
    force = "--force" in args or "--allow-unregistered" in args
    return _print(call_edit("story_delete", {"ugc_ids": ids, "dry_run": dry,
                                             "cascade": cascade,
                                             "allow_unregistered": force}))


def cmd_export(args):
    out = None
    fmt = None
    pending = None                      # 上一个 flag 等待它的空格取值
    for a in args:
        if pending is not None:
            if a.startswith("--"):
                continue
            if pending == "out":
                out = a
            else:
                fmt = a
            pending = None
            continue
        if a.startswith("--out="):
            out = a.split("=", 1)[1]
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
        elif a == "--out":
            pending = "out"
        elif a == "--format":
            pending = "format"
        elif a and not a.startswith("-"):
            out = a
    return _print(call_edit("story_export", {"path": out, "format": fmt}))


def _drive_import(res):
    """推进创建链：每一步的下一个 request_id 可能在 create_step 或 next 里。"""
    while not isinstance(res, dict) or not res.get("done"):
        if not isinstance(res, dict):
            return 1
        rid = ((res.get("create_step") or {}).get("request_id")
               or (res.get("next") or {}).get("request_id"))
        poll = _wait_poll("story_import_next", rid or "")
        print(json.dumps(poll, ensure_ascii=False, indent=1, default=str))
        if poll is None:
            return 1
        if poll.get("error"):
            return 1
        if poll.get("done"):
            break
        res = poll
    return 0


def cmd_import(args):
    path = None
    apply_ = "--apply" in args
    prune = "--prune" in args
    for a in args:
        if not a.startswith("-"):
            path = a
    if not path:
        print("需要剧情文件路径")
        return 2
    res = _unwrap(call_edit("story_import", {"path": path, "dry_run": not apply_,
                                             "prune": prune}))
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if not apply_ or not isinstance(res, dict):
        return 0 if isinstance(res, dict) else 1
    return _drive_import(res)


def cmd_resume(_args):
    """继续被中断的导入：创建进度记在编辑器会话里，不重新发计划、不会重复建节点。"""
    return _drive_import(_unwrap(call_edit("story_import_next", {})))


def cmd_create_many(args):
    """批量创建（调试用）：create-many <kind> <n> [-f k=v ...]"""
    if len(args) < 2:
        print("需要 kind 与数量")
        return 2
    kind, n = args[0], int(args[1])
    fields = _parse_fields(args[2:])
    for i in range(n):
        r = _unwrap(call_edit("story_create", {"kind": kind, "fields": fields}))
        rid = (r or {}).get("request_id")
        if not rid:
            print("创建失败：%s" % r)
            return 1
        p = _wait_poll("story_create_poll", rid)
        print("%d -> %s" % (i, (p or {}).get("ugc_id")))
    return 0


COMMANDS = {
    "tree": cmd_tree,
    "fields": cmd_fields,
    "set": cmd_set,
    "create": cmd_create,
    "create-many": cmd_create_many,
    "delete": cmd_delete,
    "export": cmd_export,
    "import": cmd_import,
    "resume": cmd_resume,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("未知命令 %s（可用：%s）" % (cmd, ", ".join(sorted(COMMANDS))))
        return 2
    return COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
