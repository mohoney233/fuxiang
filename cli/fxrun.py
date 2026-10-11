#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxrun — 把一个本地 .py 脚本内容通过 eval op 送到游戏内执行。

用法:
  python cli/fxrun.py probe/xxx.py [timeout]

脚本内可直接用预置变量: UGCEditCore / cm / em / room / GK。
执行结果放在变量 RESULT 里即可回传；不设则回传 None。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fxedit import call_edit  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    data = call_edit("eval", {"code": code}, timeout=timeout)
    if data is None:
        print("无返回（超时或桥接异常）")
        return 2
    text = json.dumps(data, ensure_ascii=False, indent=2)
    out_path = os.path.splitext(path)[0] + ".out.json"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("已写入 %s (%d bytes)" % (out_path, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
