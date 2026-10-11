#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxshot — 命令行出预览图（复用 mcp/preview.py 的主机侧编排）。

用法:
  python cli/fxshot.py sweep [fit] [--out-dir D]  # 全套视角（content/map/union）
  python cli/fxshot.py shot <view> [fit]     # 单个视角: top / corner_ne / ...
  python cli/fxshot.py plan [fit]            # 只出规划
  python cli/fxshot.py restore               # 还原相机
  python cli/fxshot.py layout [fit] [--out P] [--width N] [--height N] [--panel N]
                                             # 2D 俯视平面图（Pillow 主机侧绘制）

`--out-dir D`：sweep/shot 的产物目录，默认 <脚本工具根>/preview/shots。桥接控制台的额外
桥接用的是通道克隆（console/bridges/<名>/），不传就会把 5 张图拍进通道目录、主图库看不到。

2026-09-22：`layout` 子命令补齐。此前 2D 平面图（preview_layout）**只能经 MCP/HTTP
调用**，cli/ 下没有入口，是关闭 MCP 后唯一的能力缺口；现与 shot/sweep 共用
mcp/preview.py 的编排（该模块被本文件 importlib 加载，**不要随 MCP 一起删**）。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    argv = sys.argv[1:]
    out_dir = None
    if "--out-dir" in argv:
        i = argv.index("--out-dir")
        if i + 1 >= len(argv):
            print("缺少 --out-dir 的取值", file=sys.stderr)
            return 2
        out_dir = argv[i + 1]
        del argv[i:i + 2]
    cmd = argv[0] if argv else "plan"
    fit = argv[1] if len(argv) > 1 else "content"
    if cmd == "plan":
        print(json.dumps(_call("preview_plan", {"fit": fit}), ensure_ascii=False, indent=2)[:4000])
    elif cmd == "shot":
        view = fit
        f = argv[2] if len(argv) > 2 else "content"
        out = P.shot(_call, {"view": view, "fit": f, "out_dir": out_dir or os.path.join(ROOT, "preview", "shots")})
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif cmd == "sweep":
        args = {"fit": fit, "restore": True}
        if out_dir:
            args["out_dir"] = out_dir
        out = P.sweep(_call, args)
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
    elif cmd == "restore":
        print(json.dumps(_call("preview_restore", {"clear": True}), ensure_ascii=False, indent=2))
    elif cmd == "layout":
        opts = {"fit": fit}
        rest = argv[2:]
        i = 0
        while i < len(rest):
            k = rest[i]
            if k in ("--out", "--width", "--height", "--panel"):
                key = {"--out": "out", "--width": "width",
                       "--height": "height", "--panel": "panel_width"}[k]
                if i + 1 >= len(rest):
                    print("缺少 %s 的取值" % k, file=sys.stderr)
                    return 2
                v = rest[i + 1]
                opts[key] = v if key == "out" else int(v)
                i += 2
            else:
                print("未知参数: %s" % k, file=sys.stderr)
                return 2
        out = P.layout(_call, opts)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
