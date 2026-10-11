#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_labels.py — 对象标签（稳定身份）管理（P1.5 / P2 地基）

标签解决的问题：ugc_id 每次重载都变、坐标会随移动漂移。
标签文件 `projects/event_dump/labels.json` 由用户直接编辑：

  {"labels": {"红方块": {"item_id": 301001, "position": [-420, 50, 150], "note": ""}}}

匹配规则：同 item_id + (x,z) 容差内最近者；找不到=missing，多个并列=ambiguous。

用法:
  python cli/event_labels.py dump [--tolerance 2] [--labels P]     # 从地图生成/刷新标签文件（保留已有标签）
  python cli/event_labels.py check                    # 校验标签↔对象匹配
  python cli/event_labels.py set <label> --ugc-id N   # 给对象打标签
  python cli/event_labels.py set <label> --at X,Z     # 按坐标打标签
  python cli/event_labels.py rename <old> <new>
  python cli/event_labels.py remove <label>

`--labels P` 覆盖标签文件位置：默认按本脚本所在工具根解析，桥接控制台给额外通道
（console/bridges/<名>/ 下的通道克隆）传主工具根的绝对路径，避免标签写进通道目录。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402
from fxapi import unwrap_key as _unwrap_key, unwrap as _peel  # noqa: E402  # 剥壳真源

LABELS_PATH = os.path.join(ROOT, "projects", "event_dump", "labels.json")

# 常用组件中文名（来自目录 name_cn；未知则用编号）
CN_NAMES = {
    100000: "障碍物", 100001: "几何体", 202003: "道具箱", 202014: "地下室盖板",
    203003: "区域", 203004: "信息提示", 203025: "消息事件", 203911: "运动器",
    203912: "运动点", 101001: "角色", 201009: "边界锚点",
}


def unwrap(node, key=None):
    """按字段名定位取该字段值；key 省略时**取最内层**（剥不动就返回当前层）。

    剥壳规则见 cli/fxapi.py（唯一真源）。注：本文件所有调用点均显式传 key。
    """
    if key is not None:
        return _unwrap_key(node, key)
    return _peel(node)


def live_objects():
    targets = unwrap(call_edit("edit_targets", {"limit": 0}), "targets") or []
    total = len(targets)
    # 逐件问一次编辑器，两万件要几分钟；不印进度的话界面看起来像卡死
    print("枚举场景物件：%d 件（逐件问一次，大图需要几分钟，请等它跑完）" % total, flush=True)
    objs = []
    for n, t in enumerate(targets, 1):
        uid = t.get("ugc_id")
        d = unwrap(call_edit("edit_target", {"ugc_id": uid}), "target") or {}
        tf = d.get("transform") or {}
        pos = tf.get("position")
        objs.append({
            "ugc_id": uid,
            "item_id": d.get("item_id") or t.get("item_id"),
            "component_type": d.get("component_type"),
            "position": pos,
            "label_in_game": d.get("component_name"),
        })
        if n % 200 == 0:
            print("  …已读取 %d/%d" % (n, total), flush=True)
    return objs


def load_labels():
    if not os.path.exists(LABELS_PATH):
        return {"labels": {}}
    return json.load(open(LABELS_PATH, encoding="utf-8"))


def save_labels(doc):
    os.makedirs(os.path.dirname(LABELS_PATH), exist_ok=True)
    doc["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(doc, open(LABELS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def match(entry, objs, tol):
    """返回 (obj, how, detail)。how ∈ matched/moved/missing/ambiguous。"""
    want_iid, want_pos = entry.get("item_id"), entry.get("position") or [None, None, None]
    cands = []
    for o in objs:
        if want_iid is not None and o.get("item_id") != want_iid:
            continue
        p = o.get("position") or [None, None, None]
        if None in (p[0], p[2]) or want_pos[0] is None or want_pos[2] is None:
            continue
        d = max(abs(p[0] - want_pos[0]), abs(p[2] - want_pos[2]))
        if d <= tol:
            cands.append((d, o))
    if not cands:
        return None, "missing", "容差 %.1f 内无同 item_id 对象" % tol
    cands.sort(key=lambda x: x[0])
    if len(cands) > 1 and abs(cands[0][0] - cands[1][0]) < 0.01:
        return None, "ambiguous", "多个候选并列（距离 %.2f）" % cands[0][0]
    d, o = cands[0]
    return o, ("matched" if d <= 0.05 else "moved"), "距离 %.2f" % d


def cmd_dump(args):
    objs = live_objects()
    doc = load_labels()
    labels = doc.get("labels") or {}
    used = set()
    # 1) 已有标签先重新匹配，匹配上就更新坐标（吸收"对象被移动"）
    for name, entry in labels.items():
        o, how, detail = match(entry, objs, args.tolerance)
        entry["last_match"] = how
        entry["last_detail"] = detail
        if o:
            entry["matched_ugc_id"] = o["ugc_id"]
            entry["position"] = [round(x, 3) for x in o["position"]]
            used.add(o["ugc_id"])
    # 2) 未覆盖的对象自动生成标签（中文名_序号）
    counters = {}
    for o in objs:
        if o["ugc_id"] in used or o.get("item_id") in (6000001, 10006001):
            continue
        base = CN_NAMES.get(o.get("component_type")) or str(o.get("component_type") or o.get("item_id"))
        counters[base] = counters.get(base, 0) + 1
        name = "%s_%d" % (base, counters[base])
        while name in labels:
            counters[base] += 1
            name = "%s_%d" % (base, counters[base])
        labels[name] = {
            "item_id": o.get("item_id"),
            "component_type": o.get("component_type"),
            "position": [round(x, 3) for x in (o.get("position") or [0, 0, 0])],
            "matched_ugc_id": o["ugc_id"],
            "last_match": "matched",
            "auto": True,          # 自动生成；优先使用用户命名（无 auto 标记）
            "note": "",
        }
    doc["labels"] = labels
    save_labels(doc)
    print("标签文件:", LABELS_PATH)
    print("标签数:", len(labels), "| 对象数:", len(objs))
    print("提示: 直接编辑该文件的键名即可给对象起名；改完跑 check 或 dump 刷新。")
    return 0


def cmd_check(args):
    doc = load_labels()
    objs = live_objects()
    bad = 0
    for name, entry in (doc.get("labels") or {}).items():
        o, how, detail = match(entry, objs, args.tolerance)
        mark = {"matched": "OK  ", "moved": "移动 ", "missing": "缺失 ", "ambiguous": "歧义 "}[how]
        if how not in ("matched",):
            bad += 1
        print("  %s %-22s -> ugc_id=%-5s %s" % (mark, name, (o or {}).get("ugc_id"), detail))
    print("总计: %d 个标签, %d 个需要处理" % (len(doc.get("labels") or {}), bad))
    return 0


def cmd_set(args):
    doc = load_labels()
    labels = doc.setdefault("labels", {})
    objs = live_objects()
    target = None
    if args.ugc_id is not None:
        target = next((o for o in objs if o["ugc_id"] == args.ugc_id), None)
    elif args.at is not None:
        want = [float(x) for x in args.at.split(",")]
        entry = {"item_id": None, "position": [want[0], None, want[1]]}
        best, best_d = None, None
        for o in objs:
            p = o.get("position") or [None, None, None]
            if p[0] is None:
                continue
            d = max(abs(p[0] - want[0]), abs(p[2] - want[1]))
            if best_d is None or d < best_d:
                best, best_d = o, d
        target = best if best_d is not None and best_d <= args.tolerance else None
    if not target:
        print("找不到目标对象（可用 --ugc-id 或 --at x,z 且距离 ≤ 容差）")
        return 1
    labels[args.label] = {
        "item_id": target.get("item_id"),
        "component_type": target.get("component_type"),
        "position": [round(x, 3) for x in target["position"]],
        "matched_ugc_id": target["ugc_id"],
        "last_match": "matched",
        "note": "",
    }
    save_labels(doc)
    print("已打标签: %s -> ugc_id=%s %s" % (args.label, target["ugc_id"], target["position"]))
    return 0


def cmd_prune(args):
    """清理：删除匹配不到的自动标签（用户命名默认保留，--all 则一并清）。"""
    doc = load_labels()
    objs = live_objects()
    labels = doc.get("labels") or {}
    removed = []
    for name in list(labels.keys()):
        entry = labels[name]
        o, how, _ = match(entry, objs, args.tolerance)
        if o:
            continue
        if entry.get("auto") or args.all:
            labels.pop(name)
            removed.append(name)
    doc["labels"] = labels
    save_labels(doc)
    print("已清理 %d 个标签（剩余 %d）" % (len(removed), len(labels)))
    return 0


def cmd_prune(args):
    """清理：删除匹配不到的自动标签（用户命名默认保留，--all 则一并清）。"""
    doc = load_labels()
    objs = live_objects()
    labels = doc.get("labels") or {}
    removed = []
    for name in list(labels.keys()):
        entry = labels[name]
        o, how, _ = match(entry, objs, args.tolerance)
        if o:
            continue
        if entry.get("auto") or args.all:
            labels.pop(name)
            removed.append(name)
    doc["labels"] = labels
    save_labels(doc)
    print("已清理 %d 个标签（剩余 %d）" % (len(removed), len(labels)))
    return 0


def cmd_rename(args):
    doc = load_labels()
    labels = doc.get("labels") or {}
    if args.old not in labels:
        print("标签不存在:", args.old)
        return 1
    labels[args.new] = labels.pop(args.old)
    save_labels(doc)
    print("已重命名:", args.old, "->", args.new)
    return 0


def cmd_remove(args):
    doc = load_labels()
    labels = doc.get("labels") or {}
    if labels.pop(args.label, None) is None:
        print("标签不存在:", args.label)
        return 1
    save_labels(doc)
    print("已删除标签:", args.label)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "check", "set", "rename", "remove", "prune"])
    ap.add_argument("--all", action="store_true", help="prune 时连用户命名一起清（慎用）")
    ap.add_argument("label", nargs="?")
    ap.add_argument("new", nargs="?")
    ap.add_argument("--ugc-id", type=int)
    ap.add_argument("--at", help="x,z")
    ap.add_argument("--tolerance", type=float, default=2.0)
    ap.add_argument("--labels", help="标签文件位置（默认按本脚本的工具根解析）")
    args = ap.parse_args()
    if args.labels:
        global LABELS_PATH
        LABELS_PATH = os.path.abspath(args.labels)
    return {"dump": cmd_dump, "check": cmd_check, "set": cmd_set,
            "rename": cmd_rename, "remove": cmd_remove, "prune": cmd_prune}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
