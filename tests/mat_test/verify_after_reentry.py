#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mat_test/verify_after_reentry.py — 重进地图后核验材质/颜色/透明度/碰撞是否持久化。

用法: python tests/mat_test/verify_after_reentry.py
原理: 重进后 ugc_id 会重新编号，按 item_id=301001 的空间物件（position 不在 -65536）
列出属性袋，与期望值比对。期望值:
  位置 [-464.35, 47.62, 132.36] 附近: 颜色 FF0000*4 + 碰撞 0
  位置 [662.79, 0, -71.40] 附近:    材质 brick01 路径 + 透明度 0 + 碰撞 0
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402


def unwrap(node, key):
    for _ in range(5):
        if not isinstance(node, dict):
            return None
        if key in node:
            return node[key]
        nxt = node.get("value") if isinstance(node.get("value"), dict) else node.get("result")
        if not isinstance(nxt, dict):
            return None
        node = nxt
    return None


BRICK = "scene\\model\\h55_ugc\\h55_ugc_mtg\\h55_ugc_mtg_brick01.mtg"
EXPECT = [
    {"near": [-464.35, 47.62, 132.36], "label": "红色+碰撞关",
     "check": {"10000110006": [["FF0000", "FF0000", "FF0000", "FF0000"]],
               "10000102001": [0]}},
    {"near": [662.79, 0.0, -71.40], "label": "砖材质+不透明+碰撞关",
     "check": {"10000110005": [BRICK], "10000110007": [0],
               "10000102001": [0]}},
]
TOL = 5.0


def main():
    data = unwrap(call_edit("edit_targets", {"limit": 0}), "targets") or []
    geoms = [t for t in data if t.get("item_id") == 301001 and t.get("editable")]
    print("geometry targets:", [(t["ugc_id"], t.get("position")) for t in geoms])
    all_ok = True
    for exp in EXPECT:
        match = None
        for t in geoms:
            pos = t.get("position") or [None, None, None]
            if pos[0] is None:
                continue
            if all(abs(pos[i] - exp["near"][i]) <= TOL for i in range(3)):
                match = t
                break
        if not match:
            print("MISSING object near", exp["near"], "(", exp["label"], ")")
            all_ok = False
            continue
        detail = unwrap(call_edit("edit_target", {"ugc_id": match["ugc_id"]}), "target") or {}
        bag = {str(e.get("property_id") or e.get("type")): e.get("info")
               for e in (detail.get("properties") or [])}
        for pid, want in exp["check"].items():
            got = bag.get(pid)
            ok = got == want
            all_ok = all_ok and ok
            print("ugc_id=%s [%s] %s -> %s | want %s | %s"
                  % (match["ugc_id"], exp["label"], pid,
                     json.dumps(got, ensure_ascii=False)[:90],
                     json.dumps(want, ensure_ascii=False)[:60],
                     "OK" if ok else "MISMATCH"))
    print("RESULT:", "PERSISTED" if all_ok else "NOT VERIFIED / FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
