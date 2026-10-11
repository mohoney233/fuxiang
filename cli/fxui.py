#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli/fxui.py — 随笔浮想「界面编辑器（UI 编辑）」命令行入口。

用法
----
  python cli/fxui.py state                         界面编辑器定位与概览（视图/计数/画布）
  python cli/fxui.py parts                         19 个子面板 part 的分工与入口
  python cli/fxui.py catalog [关键字] [--limit N]  可放置控件目录（limit 0=全部）
  python cli/fxui.py schema [--ugc-id N]           控件字段字典（别名/property_id/事件槽）
  python cli/fxui.py list [--native]               地图上已有控件
  python cli/fxui.py get <ugc_id>                  单个控件（含保存报文形态）
  python cli/fxui.py add <item_id|中文名> [--pos x,y] [--dry-run]
  python cli/fxui.py move <ugc_id> <x> <y>         挪到画布 2D 坐标
  python cli/fxui.py set <ugc_id> -f k=v [-f k=v] [--dry-run]
  python cli/fxui.py bind <ugc_id> [slot] <事件名> [--create-event] [--dry-run]
  python cli/fxui.py unbind <ugc_id> <事件名>
  python cli/fxui.py delete <ugc_id[,ugc_id...]> [--yes]
  python cli/fxui.py verify                        落库前自检（是否进报文/事件引用一致）

说明
----
* 控件坐标是**画布 2D** [x, y]（设计尺寸 1334×750，原点左下），与场景 [x,y,z] 无关。
* `add` 走服务端建件 + 原生确认（与玩家从调色板拖放同一条链路），脚本内自动轮询到
  placed；游戏主线程里不等待。
* 任何写入都要求界面编辑器**在前视图**（`state` 里的 view_open=true）：关着时原生
  创建/删除命令会静默返回空，等于什么都没发生。
* 写完都要在游戏内保存（Ctrl+S / 保存按钮）才落库；**退出编辑器也会自动保存**。
  详见 UI_EDIT.md。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fxapi import call, unwrap_key  # noqa: E402

POLL_TIMEOUT = 30.0
POLL_INTERVAL = 0.15


def _print(res):
    if res is None:
        print("调用失败（桥接未运行或超时）")
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 1 if isinstance(res, dict) and res.get("ok") is False else 0


def _flag(args, name):
    return name in args


def _value_after(args, name, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _pos(text):
    """--pos 700,300 / move 的 <x> <y> 都归一成 [x, y]。"""
    if text is None:
        return None
    parts = [p.strip() for p in str(text).split(",") if p.strip() != ""]
    if len(parts) != 2:
        raise ValueError("pos 需要两个数：--pos x,y（画布 2D 坐标）")
    return [float(parts[0]), float(parts[1])]


def _fields(pairs):
    """-f k=v：值支持 数字 / 逗号列表（pos=700,300）/ true,false / 文本。"""
    norm = []
    skip = False
    for i, p in enumerate(pairs):
        if skip:
            skip = False
            continue
        if p == "-f" and i + 1 < len(pairs):
            norm.append(pairs[i + 1])
            skip = True
            continue
        if p.startswith("-f="):
            norm.append(p[3:])
        elif p.startswith("--") or "=" not in p:
            continue
        else:
            norm.append(p)
    out = {}
    for p in norm:
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.lower() in ("none", "null", ""):
            out[k] = None
        elif v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif "," in v:
            try:
                out[k] = [float(x) for x in v.split(",")]
            except ValueError:
                out[k] = v
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def cmd_state(_args):
    return _print(call("ui_state", {}))


def cmd_parts(_args):
    return _print(call("ui_parts", {}))


def cmd_catalog(args):
    query = next((a for a in args if not a.startswith("-")), None)
    limit = _value_after(args, "--limit")
    return _print(call("ui_catalog", {"query": query,
                                       "limit": int(limit) if limit else 40}))


def cmd_schema(args):
    gid = _value_after(args, "--ugc-id")
    return _print(call("ui_schema", {"ugc_id": int(gid)} if gid else {}))


def cmd_list(args):
    return _print(call("ui_list", {"include_native": _flag(args, "--native")}))


def cmd_get(args):
    if not args:
        print("需要 ugc_id")
        return 2
    return _print(call("ui_get", {"ugc_id": int(args[0])}))


def _wait_placed(request_id, timeout=POLL_TIMEOUT):
    """在游戏进程外轮询到定论（游戏主线程内禁止 sleep）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            got = call("ui_add_poll", {"request_id": request_id})
        except Exception as exc:
            got = {"error": str(exc)}
        last = got
        status = unwrap_key(got, "status") if isinstance(got, dict) else None
        if status in ("placed", "ambiguous", "conflict", "finalize_failed", "overdue"):
            return last
        time.sleep(POLL_INTERVAL)
    print("轮询超时（%ss），最后一次状态：%s" % (timeout, last), file=sys.stderr)
    return last


def cmd_add(args):
    target = next((a for a in args if not a.startswith("-")), None)
    if target is None:
        print(__doc__)
        return 2
    dry = _flag(args, "--dry-run")
    payload = {"dry_run": dry}
    try:
        pos = _pos(_value_after(args, "--pos"))
    except ValueError as exc:
        print(str(exc))
        return 2
    if pos:
        payload["pos"] = pos
    rid = _value_after(args, "--request-id")
    if rid:
        payload["request_id"] = rid
    if target.isdigit():
        payload["item_id"] = int(target)
    else:
        payload["name"] = target
    res = call("ui_add", payload)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if dry or not isinstance(res, dict):
        return 0 if isinstance(res, dict) else 1
    rid = res.get("request_id")
    if not rid:
        print("建件未受理：%s" % res.get("error"), file=sys.stderr)
        return 1
    poll = _wait_placed(rid)
    print(json.dumps(poll, ensure_ascii=False, indent=1, default=str))
    return 0 if unwrap_key(poll or {}, "status") == "placed" else 1


def cmd_move(args):
    if len(args) < 3:
        print("用法：move <ugc_id> <x> <y> [--dry-run]")
        return 2
    return _print(call("ui_move", {"ugc_id": int(args[0]),
                                   "pos": [float(args[1]), float(args[2])],
                                   "dry_run": _flag(args, "--dry-run")}))


def cmd_set(args):
    if not args:
        print("需要 ugc_id")
        return 2
    props = _fields(args[1:])
    if not props:
        print("至少一个 -f 属性，如 -f width=200 -f pos=700,300")
        return 2
    return _print(call("ui_set", {"ugc_id": int(args[0]), "props": props,
                                  "dry_run": _flag(args, "--dry-run")}))


def cmd_bind(args):
    # 位置参数少一个：slot 可省（默认 on_click），靠「末位是事件名」区分
    if len(args) < 2:
        print("用法：bind <ugc_id> [slot] <事件名> [--create-event] [--dry-run]")
        return 2
    gid = int(args[0])
    rest = [a for a in args[1:] if not a.startswith("--")]
    slot, key = "on_click", rest[-1]
    if len(rest) >= 2:
        slot, key = rest[0], rest[1]
    return _print(call("ui_bind", {"ugc_id": gid, "slot": slot, "event_key": key,
                                   "create_event": _flag(args, "--create-event"),
                                   "dry_run": _flag(args, "--dry-run")}))


def cmd_unbind(args):
    if len(args) < 2:
        print("用法：unbind <ugc_id> <事件名>")
        return 2
    return _print(call("ui_unbind", {"ugc_id": int(args[0]), "event_key": args[1]}))


def cmd_delete(args):
    if not args:
        print("需要 ugc_id（多个用逗号分隔）；真删要加 --yes")
        return 2
    ids = [int(x) for x in args[0].split(",") if x.strip()]
    return _print(call("ui_delete", {"ugc_ids": ids, "dry_run": not _flag(args, "--yes")}))


def cmd_verify(_args):
    return _print(call("ui_verify", {}))


COMMANDS = {
    "state": cmd_state,
    "parts": cmd_parts,
    "catalog": cmd_catalog,
    "schema": cmd_schema,
    "list": cmd_list,
    "get": cmd_get,
    "add": cmd_add,
    "move": cmd_move,
    "set": cmd_set,
    "bind": cmd_bind,
    "unbind": cmd_unbind,
    "delete": cmd_delete,
    "verify": cmd_verify,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("未知命令 %s（可用：%s）" % (cmd, ", ".join(sorted(COMMANDS))))
        return 2
    try:
        return COMMANDS[cmd](sys.argv[2:])
    except Exception as exc:
        print("%s 失败: %s" % (cmd, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
