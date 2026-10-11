#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare_positions.py — 对比保存前/重载后的位置快照，找出被移动对象。

用法：
  python tests/compare_positions.py before.json after.json

判据：仅对比"有实义位置"的物件（忽略原点 UI 组件、y=-65536 的运动点/逻辑位）。
ugc_id 在重载后可能重编号，故按 (item_id, 位置) 匹配。
"""
import sys, os, json


def load(p):
    d = json.load(open(p, encoding="utf-8"))
    return d.get("rows") if isinstance(d, dict) else d


def meaningful(r):
    pos = r.get("position")
    if not pos or any(v is None for v in pos):
        return False
    x, y, z = (float(pos[0]), float(pos[1]), float(pos[2]))
    if abs(y) > 1000 or abs(x) < 1e-6 and abs(y) < 1e-6 and abs(z) < 1e-6:
        # 排除 y=-65536 哨兵位与原点
        return abs(y) < 1000 and not (abs(x) < 1e-6 and abs(y) < 1e-6 and abs(z) < 1e-6)
    return True


def norm(p):
    return [round(float(v), 2) for v in p] if p else None


def fmt(r):
    return {"item_id": r.get("item_id"),
            "rot": r.get("rotation"), "scale": r.get("scale"),
            "pos": norm(r.get("position"))}


def key(r):
    it = str(r.get("item_id"))
    p = norm(r.get("position"))
    return (it, "-".join(str(int(v)) for v in p) if p else "?")


def main():
    before = load(sys.argv[1])
    after = load(sys.argv[2])
    b = {key(r): fmt(r) for r in before if meaningful(r)}
    a = {key(r): fmt(r) for r in after if meaningful(r)}
    print("before meaningful=%d  after meaningful=%d" % (len(b), len(a)))
    print("=" * 60)

    all_keys = sorted(set(b) | set(a), key=lambda x: (x[0], x[1]))
    changed = 0
    for k in all_keys:
        bi = b.get(k)
        ai = a.get(k)
        if bi == ai:
            continue
        changed += 1
        if bi and ai:
            print("[CHANGED] item=%s\n   before: pos=%s rot=%s scale=%s\n   after : pos=%s rot=%s scale=%s"
                  % (k[0], bi["pos"], bi["rot"], bi["scale"], ai["pos"], ai["rot"], ai["scale"]))
        elif bi:
            print("[ONLY_BEFORE][重载后消失] item=%s pos=%s" % (k[0], bi["pos"]))
        else:
            print("[ONLY_AFTER][重载后新增]  item=%s pos=%s" % (k[0], ai["pos"]))
    if changed == 0:
        print("无差异：所有有实义位置的物件在保存后保持一致，位置已持久化。")
    else:
        print("=" * 60)
        print("差异对象数: %d" % changed)


if __name__ == "__main__":
    main()