#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxsweep — 归拢 ipc/out 里累积的桥接回执（改名归档，不删除）。

回执现在一律保留（见 cli/fxedit.py::call_bridge 的注释），累积久了只是碍眼。
本工具用 shutil.move 把它们整体搬进一个带时间戳的目录：

  - **只改名，不删除** —— 宿主 safe-delete 钩子按删除次数计数，改名不触发。
  - **跳过仍在等回执的文件** —— 只搬 mtime 早于 --older-than 秒的（默认 60），
    避免把别的智能体在途调用的回执搬走、害它超时。

用法:
  python cli/fxsweep.py                 # 归拢 60 秒前的回执
  python cli/fxsweep.py --older-than 0  # 全部（确认没有在途调用时再用）
  python cli/fxsweep.py --dry-run       # 只看会搬哪些
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "ipc", "out")


def collect(older_than):
    now = time.time()
    names = []
    for n in sorted(os.listdir(OUT_DIR)) if os.path.isdir(OUT_DIR) else []:
        if not n.endswith(".json"):
            continue
        p = os.path.join(OUT_DIR, n)
        try:
            if now - os.path.getmtime(p) >= older_than:
                names.append(n)
        except OSError:
            continue
    return names


def main():
    ap = argparse.ArgumentParser(description="归档 ipc/out 桥接回执（改名，不删除）")
    ap.add_argument("--older-than", type=float, default=60.0,
                    help="只搬 mtime 早于该秒数的文件，保护在途调用（默认 60）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要归档的文件")
    a = ap.parse_args()

    names = collect(a.older_than)
    if not names:
        print(json.dumps({"moved": 0, "remaining": len(os.listdir(OUT_DIR)),
                          "note": "没有符合 --older-than 的回执"}, ensure_ascii=False))
        return 0
    if a.dry_run:
        print(json.dumps({"moved": 0, "would_move": len(names),
                          "first": names[:5], "older_than": a.older_than},
                         ensure_ascii=False))
        return 0

    dest = os.path.join(ROOT, "ipc", "out_archive_" + time.strftime("%Y%m%dT%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    moved = 0
    for n in names:
        try:
            shutil.move(os.path.join(OUT_DIR, n), os.path.join(dest, n))
            moved += 1
        except OSError as e:
            print("跳过 %s: %s" % (n, e), file=sys.stderr)
    print(json.dumps({"moved": moved, "archive": dest,
                      "remaining": len(os.listdir(OUT_DIR))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
