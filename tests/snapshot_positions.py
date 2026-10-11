#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot_positions.py — 抓取编辑器全部已登记对象的变换位置，存档为 JSON。

用法：
  python tests/snapshot_positions.py [out_file]

用于「保存前记录位置 → 手动重进 → 对比位置是否持久化」的验收。
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../cli")
import mcpcall  # noqa: E402

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "builds", "position_check")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "positions.json")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    res = mcpcall.call("fuxiang_edit_targets", {"limit": 0}, 120)
    # fuxiang_edit_targets 直接返回 {total, editable, blocked, targets}
    if not isinstance(res, dict):
        res = {}
    rows_raw = res.get("targets")
    if not rows_raw and isinstance(res.get("value"), dict):
        rows_raw = res["value"].get("targets")
    rows_raw = rows_raw or []
    rows = []
    for r in rows_raw:
        t = r.get("transform") or {}
        rows.append({
            "ugc_id": r.get("ugc_id"),
            "item_id": r.get("item_id"),
            "name": r.get("name") or r.get("name_cn"),
            "position": t.get("position"),
            "rotation": t.get("rotation"),
            "scale": t.get("scale"),
        })
    payload = {"count": len(rows), "rows": rows}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
    print("saved_to=", OUT)


if __name__ == "__main__":
    main()