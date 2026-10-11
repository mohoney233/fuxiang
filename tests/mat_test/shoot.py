#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mat_test/shoot.py — 用自定义视角对指定对象拍一张引擎截图。

用法: python tests/mat_test/shoot.py <out_abs.png> <pos_x> <pos_y> <pos_z> <fwd_x> <fwd_y> <fwd_z> [wait]
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "cli"))
sys.path.insert(0, ROOT)
from fxedit import call_edit  # noqa: E402
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("mcp_preview", os.path.join(ROOT, "mcp", "preview.py"))
P = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(P)


def _call(op, args, timeout=60):
    data = call_edit(op, args, timeout=timeout) or {}
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


def main():
    out = os.path.abspath(sys.argv[1])
    pos = [float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])]
    fwd = [float(sys.argv[5]), float(sys.argv[6]), float(sys.argv[7])]
    wait = float(sys.argv[8]) if len(sys.argv) > 8 else 0.8
    view = {"name": os.path.splitext(os.path.basename(out))[0],
            "position": pos, "forward": fwd, "up": [0, 1, 0], "mode": "perspect"}
    res = P.shot(_call, {"view": view, "out": out, "restore": True, "wait": wait})
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
