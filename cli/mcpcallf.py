#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcpcall 的文件参数版：避免 PowerShell 5.1 传 JSON 时的引号转义问题。

⚠️ 2026-09-22 起 MCP stdio 通道**默认停用**；本文件仅为临时排障保留，
需先 set FUXIANG_ENABLE_MCP=1。日常编辑请用 CLI（cli/fxedit.py / cli/fxshot.py）。

用法:
    python cli/mcpcallf.py <tool_name> [args_json_file|-] [out_file]
    args_json_file 为 JSON 参数文件路径；传 "-" 或省略表示无参数。
    指定 out_file 时结果同时写入该文件（UTF-8）。

示例:
    python cli/mcpcallf.py fuxiang_status
    python cli/mcpcallf.py fuxiang_edit_targets args.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpcall import call  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    name = sys.argv[1]
    args = None
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        # utf-8-sig 兼容 PowerShell 写出的带 BOM 文件
        with open(sys.argv[2], encoding="utf-8-sig") as f:
            args = json.load(f)
    try:
        out = call(name, args)
    except RuntimeError as e:
        sys.stderr.write("%s\n" % e)
        sys.exit(2)
    text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf-8") as f:
            f.write(text)
        print("saved:", sys.argv[3])
    else:
        print(text)


if __name__ == "__main__":
    main()
