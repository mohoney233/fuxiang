#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览渲染实机验证：走真正的 MCP stdio 服务器端到端跑一遍。

用法（游戏须已进入随笔浮想编辑器且桥接已安装）：
    python tests/preview_live.py                 # 规划 + 平面图，不动相机
    python tests/preview_live.py --shots         # 再加一整套引擎渲染出图
    python tests/preview_live.py --shots --fit union

产物：tests/preview_result.json，以及 preview/ 下的 PNG。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp", "server.py")
REPORT = os.path.join(ROOT, "tests", "preview_result.json")


def call(name, args=None, timeout=1800):
    """一次 stdio 会话调用一个工具，返回解析后的结果。"""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "preview-live", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": args or {}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-u", SERVER],
        input="".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode:
        raise RuntimeError("MCP 进程异常退出：\n%s" % proc.stderr[-2000:])
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("MCP 无输出。stderr:\n%s" % proc.stderr[-2000:])
    reply = json.loads(lines[-1])["result"]
    text = reply["content"][0]["text"]
    if reply.get("isError"):
        raise RuntimeError(text)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def main():
    shots = "--shots" in sys.argv
    fit = "content"
    if "--fit" in sys.argv:
        fit = sys.argv[sys.argv.index("--fit") + 1]

    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "fit": fit}
    t0 = time.time()

    print("[1/5] preview_state")
    report["state_before"] = call("fuxiang_preview_state")
    cam_before = report["state_before"].get("camera", {})
    print("     相机位置", cam_before.get("position"),
          "投影", "透视" if cam_before.get("perspective") else "正交")

    print("[2/5] preview_plan")
    report["plan"] = call("fuxiang_preview_plan", {"fit": fit})
    plan = report["plan"]
    print("      空间物件 %s · 边界锚点 %s · 非空间组件 %s"
          % (plan.get("content_count"), plan.get("boundary_count"),
             plan.get("non_spatial_count")))
    print("      取景范围", plan.get("bounds"))
    for v in plan.get("views") or []:
        print("      · %-8s %s %s" % (v.get("name"), v.get("mode"),
                                      v.get("position")))

    print("[3/5] preview_layout（路线 C，不动相机）")
    report["layout"] = call("fuxiang_preview_layout",
                            {"fit": fit, "out": os.path.join(
                                ROOT, "preview", "layout_%s.png" % fit)})
    print("      平面图", report["layout"].get("png"),
          report["layout"].get("png_bytes"), "字节")

    print("[4/5] preview_restore 校验相机未偏移")
    try:
        report["restore_after_layout"] = call("fuxiang_preview_restore")
        print("      还原 verified =",
              report["restore_after_layout"].get("verified"))
    except RuntimeError as exc:
        # 未摆过位时没有备份，属正常情况
        report["restore_after_layout"] = {"skipped": str(exc)[:200]}
        print("      跳过（未摆过位，无备份）")

    report["shots"] = None
    if shots:
        print("[5/5] preview_sweep（路线 A，引擎渲染出图）")
        report["shots"] = call("fuxiang_preview_sweep",
                               {"fit": fit, "restore": True})
        for s in report["shots"].get("shots") or []:
            print("      · %-8s ok=%-5s %8s 字节  %s"
                  % (s.get("view"), s.get("ok"), s.get("bytes"), s.get("path")))
        print("      还原:", report["shots"].get("restored", {}).get("verified"))
    else:
        print("[5/5] 跳过出图（未传 --shots）")

    print("[check] preview_state 收尾")
    report["state_after"] = call("fuxiang_preview_state")
    cam_after = report["state_after"].get("camera", {})
    report["camera_unchanged"] = (
        cam_before.get("position") == cam_after.get("position") and
        cam_before.get("forward") == cam_after.get("forward"))
    report["elapsed"] = round(time.time() - t0, 2)

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n相机未被改动:", report["camera_unchanged"])
    print("耗时 %.1fs · 报告 %s" % (report["elapsed"], REPORT))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
