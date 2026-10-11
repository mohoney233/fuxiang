#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_import.py — 把事件文件（event_export.py 的产物）回放到地图里（P1）

用法:
  python cli/event_import.py <file> [--apply] [--prune] [--tolerance 2.0] [--labels P]

`--labels P`：标签文件位置，默认按本脚本的工具根解析。桥接控制台的额外桥接用的是通道克隆
（console/bridges/<名>/），不传主根的绝对路径就解析不到用户标签，锚点只能退回坐标。

行为:
  默认 **dry-run**：只输出执行计划（会做什么、有什么问题），不写地图。
  --apply  真正写入（create/bind/unbind/delete 走接口 op，最后可用 save_map 落盘）。
  --prune  删除"文件里没有、地图里有"的绑定；事件键同理（默认保留，不误删）。

设计:
  * 锚点匹配 = (item_id, x, z) 容差匹配（y 只告警）——ugc_id 每次重载都变，不能用。
  * 绑定按 raw_value **整段无损**写回（event_bind 的 info 参数）。事件键在 info 里的
    下标由字段权威形态决定，不一定是 0（如 20204111101 的键在第 3 位）。
  * 校验：悬空锚点 / 未知 property_id / 无接收者 / 无发送者 / 键重复。
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402
from fxapi import unwrap_key as _unwrap_key, find_layer_any as _find_any  # noqa: E402

try:
    sys.path.insert(0, os.path.join(ROOT, "cli"))
    from event_export import FIELD_SEMANTICS  # noqa: E402
except Exception:  # pragma: no cover
    FIELD_SEMANTICS = {}


def unwrap(node, key=None):
    """按字段名定位取该字段值；key 省略时取含 ok/count/events 的那一层。

    剥壳规则见 cli/fxapi.py::unwrap_key / find_layer（唯一真源）。
    注：本文件所有调用点均显式传 key，key=None 分支为兼容保留。
    """
    if key is not None:
        return _unwrap_key(node, key)
    return _find_any(node, ("ok", "count", "events"))


def val(resp):
    v = resp.get("value") if isinstance(resp, dict) else None
    return v if isinstance(v, dict) else {}


def live_state():
    """当前地图：事件表 + 可编辑对象锚点。"""
    ev = val(call_edit("event_list", {}))
    events = {e["event_key"]: e for e in (ev.get("events") or [])}
    targets = unwrap(call_edit("edit_targets", {"limit": 0}), "targets") or []
    objs = []
    for t in targets:
        uid = t.get("ugc_id")
        d = unwrap(call_edit("edit_target", {"ugc_id": uid}), "target") or {}
        tf = d.get("transform") or {}
        pos = tf.get("position")
        props = {}
        for e in (d.get("properties") or []):
            props[str(e.get("property_id") or e.get("type"))] = e.get("info")
        objs.append({
            "ugc_id": uid,
            "item_id": d.get("item_id") or t.get("item_id"),
            "component_type": d.get("component_type"),
            "position": pos,
            "name": d.get("component_name") or d.get("component_class"),
            "props": props,
        })
    return events, objs


def match_anchor(anchor, objs, tol):
    """按 (item_id, x, z) 匹配；返回 (obj, issues)。"""
    want_iid = anchor.get("item_id")
    want_pos = anchor.get("position") or [None, None, None]
    best, best_d = None, None
    for o in objs:
        if want_iid is not None and o.get("item_id") != want_iid:
            continue
        p = o.get("position") or [None, None, None]
        if None in (p[0], p[2]) or want_pos[0] is None or want_pos[2] is None:
            continue
        d = max(abs(p[0] - want_pos[0]), abs(p[2] - want_pos[2]))
        if d <= tol and (best_d is None or d < best_d):
            best, best_d = o, d
    if best is None:
        return None, "missing", "容差 %.1f 内找不到 %s @ %s" % (tol, anchor.get("label"), want_pos)
    detail = "距 %.2f" % (best_d or 0)
    if want_pos[1] is not None and best.get("position") and \
            abs(best["position"][1] - want_pos[1]) > max(5.0, tol * 3):
        detail += "；高度偏差 %.1f（按 x/z 匹配）" % abs(best["position"][1] - want_pos[1])
    return best, ("matched" if (best_d or 0) <= 0.05 else "moved"), detail


def _norm(v):
    """把值归一化用于比较（数字转 float，其余原样）。"""
    if isinstance(v, str):
        try:
            v = ast.literal_eval(v)
        except Exception:
            return v
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 6)
    return v


def _same_value(live, want):
    try:
        return _norm(live) == _norm(want)
    except Exception:
        return False


LABELS_PATH = os.path.join(ROOT, "projects", "event_dump", "labels.json")


def load_labels():
    path = LABELS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("labels") or {}
    except Exception:
        return {}


def resolve_ref(doc, b, objs, labels, tol):
    """把一条绑定解析到当前地图对象：优先标签 → 退回该标签/锚点的坐标匹配。"""
    name = b.get("object")
    entry = labels.get(name) if isinstance(name, str) else None
    if entry:
        # 用标签记录的 (item_id, x, z) 匹配当前对象
        o, how, detail = match_anchor(
            {"label": name, "item_id": entry.get("item_id"),
             "position": entry.get("position")}, objs, tol)
        return o, "标签" + how, detail
    anchor = (doc.get("objects") or {}).get(str(b.get("object_ugc_id_at_export"))) or {}
    o, how, detail = match_anchor(anchor, objs, tol)
    return o, "坐标" + how, detail


def build_plan(doc, events, objs, tol, prune):
    plan = {"create_events": [], "bind": [], "update": [], "props": [], "unbind": [],
            "delete_events": [], "issues": [], "warnings": []}
    labels = load_labels()
    file_events = {e["key"]: e for e in doc.get("events") or []}
    seen = set()
    for key, e in file_events.items():
        if key in seen:
            plan["issues"].append("事件键重复: %s" % key)
        seen.add(key)
        if key not in events:
            plan["create_events"].append(key)
        has_sender = has_receiver = False
        for b in e.get("bindings") or []:
            pid = str(b.get("property_id"))
            role = b.get("role") or FIELD_SEMANTICS.get(pid, ("unknown",))[0]
            has_sender = has_sender or role == "sender"
            has_receiver = has_receiver or role == "receiver"
            if role == "unknown":
                plan["warnings"].append("%s: 未解码字段 %s（按原值写回）" % (key, pid))
            obj, how, detail = resolve_ref(doc, b, objs, labels, tol)
            if obj is None:
                plan["issues"].append("%s: 无法定位对象『%s』（%s）" % (key, b.get("object"), detail))
                continue
            if not str(how).endswith("matched"):
                plan["warnings"].append("%s: 对象『%s』按%s匹配（%s）"
                                        % (key, b.get("object"), how, detail))
            raw = b.get("raw_value")
            if isinstance(raw, str):
                try:
                    raw = ast.literal_eval(raw)
                except Exception:
                    plan["issues"].append("%s: raw_value 无法解析 %r" % (key, raw))
                    continue
            if not isinstance(raw, list):
                plan["issues"].append("%s: raw_value 不是列表 %r" % (key, raw))
                continue
            live_ref = None
            for r in (events.get(key, {}) or {}).get("refs") or []:
                if r.get("ugc_id") == obj["ugc_id"] and str(r.get("property_id")) == pid:
                    live_ref = r
                    break
            target = {"key": key, "ugc_id": obj["ugc_id"], "property_id": pid,
                      "value": raw}
            if live_ref is None:
                # 文件内重复条目去重（幂等）
                if target not in plan["bind"]:
                    plan["bind"].append(target)
            elif not _same_value(live_ref.get("current_value"), raw):
                target["live_value"] = live_ref.get("current_value")
                plan["update"].append(target)
            else:
                plan.setdefault("already_bound", []).append(target)
        if not has_sender:
            plan["warnings"].append("%s: 没有发送方（只有接收方，事件不会被触发）" % key)
        if not has_receiver:
            plan["warnings"].append("%s: 没有接收方（事件发了没人响应）" % key)
    # 对象属性覆盖（DSL 的 属性: 段）
    for name, props in (doc.get("object_props") or {}).items():
        entry = labels.get(name)
        if not entry:
            plan["issues"].append("属性覆盖: 标签『%s』不在 labels.json 中" % name)
            continue
        obj, how, detail = match_anchor(
            {"label": name, "item_id": entry.get("item_id"), "position": entry.get("position")},
            objs, tol)
        if obj is None:
            plan["issues"].append("属性覆盖: 无法定位『%s』（%s）" % (name, detail))
            continue
        for pid, value in (props or {}).items():
            live = (obj.get("props") or {}).get(str(pid))
            if _same_value(live, value):
                plan.setdefault("props_noop", []).append("%s.%s" % (name, pid))
                continue
            plan["props"].append({"label": name, "ugc_id": obj["ugc_id"],
                                  "property_id": str(pid), "value": value,
                                  "live_value": live})

    # 检查 A：实时注册表里的"陈旧引用"（槽位值已不是该事件键）
    for key, e in events.items():
        for r in e.get("refs") or []:
            cur = r.get("current_value")
            try:
                cur = ast.literal_eval(cur) if isinstance(cur, str) else cur
            except Exception:
                pass
            if isinstance(cur, list) and cur and isinstance(cur[0], str) and cur[0] != key:
                plan["warnings"].append(
                    "陈旧引用: 事件『%s』仍指向 %s.%s，但该槽位现值属于『%s』"
                    % (key, r.get("ugc_id"), r.get("property_id"), cur[0]))
    # 检查 B：文件内多个事件争用同一槽位
    slot_owner = {}
    for key, e in file_events.items():
        for b in e.get("bindings") or []:
            k = (b.get("object_ugc_id_at_export"), str(b.get("property_id")))
            if k in slot_owner and slot_owner[k] != key:
                plan["issues"].append("文件冲突: %s 与 %s 争用同一槽位 %s" % (slot_owner[k], key, k))
            slot_owner[k] = key
    if prune:
        for key, e in events.items():
            if key not in file_events:
                plan["delete_events"].append(key)
                continue
            keep = set()
            for b in (file_events[key].get("bindings") or []):
                obj, _, _ = resolve_ref(doc, b, objs, labels, tol)
                if obj:
                    keep.add((obj["ugc_id"], str(b.get("property_id"))))
            for r in e.get("refs") or []:
                if (r.get("ugc_id"), str(r.get("property_id"))) not in keep:
                    plan["unbind"].append({"key": key, "ugc_id": r.get("ugc_id"),
                                           "property_id": r.get("property_id")})
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只出计划）")
    ap.add_argument("--prune", action="store_true", help="删除文件里没有的绑定/事件")
    ap.add_argument("--tolerance", type=float, default=2.0, help="锚点 x/z 容差")
    ap.add_argument("--labels", help="标签文件位置（默认按本脚本的工具根解析）")
    args = ap.parse_args()
    if args.labels:
        global LABELS_PATH
        LABELS_PATH = os.path.abspath(args.labels)

    try:
        with open(args.file, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        print("找不到文件: %s" % args.file)
        return 3
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print("%s 不是本接口可解析的 JSON 事件文件（需要 cli/event_export.py 导出的格式）: %s"
              % (args.file, e))
        return 3
    if (doc.get("meta") or {}).get("schema_version") != 1:
        print("警告: 文件 schema_version 不是 1，仍尝试解析")
    events, objs = live_state()
    plan = build_plan(doc, events, objs, args.tolerance, args.prune)

    print("=== 执行计划 (%s) ===" % ("APPLY" if args.apply else "DRY-RUN"))
    print("新建事件 :", plan["create_events"])
    print("新增绑定 :", ["%s@%s.%s" % (b["key"], b["ugc_id"], b["property_id"]) for b in plan["bind"]])
    print("更新参数 :", ["%s@%s.%s %s -> %s" % (b["key"], b["ugc_id"], b["property_id"],
                       b.get("live_value"), b["value"]) for b in plan["update"]])
    print("已存在跳过:", ["%s@%s.%s" % (b["key"], b["ugc_id"], b["property_id"])
                     for b in plan.get("already_bound") or []])
    print("写属性   :", ["%s.%s=%s" % (p["label"], p["property_id"], p["value"]) for p in plan["props"]])
    print("属性未变 :", (plan.get("props_noop") or [])[:6], "共 %d 项" % len(plan.get("props_noop") or []))
    print("解绑(--prune):", ["%s@%s.%s" % (b["key"], b["ugc_id"], b["property_id"]) for b in plan["unbind"]])
    print("删除事件(--prune):", plan["delete_events"])
    if plan["warnings"]:
        print("--- 警告 ---")
        for w in plan["warnings"]:
            print("  !", w)
    if plan["issues"]:
        print("--- 阻塞问题 ---")
        for i in plan["issues"]:
            print("  x", i)
        print("存在阻塞问题：已中止（未写地图）。")
        return 2
    if not args.apply:
        print("（dry-run 完成；加 --apply 执行）")
        return 0

    errs = []
    for key in plan["create_events"]:
        r = call_edit("event_create", {"name": key})
        if not (val(r) or {}).get("ok"):
            errs.append("create %s: %s" % (key, (val(r) or {}).get("error")))
    for b in plan["bind"]:
        r = call_edit("event_bind", {"ugc_id": b["ugc_id"], "event_key": b["key"],
                                     "property_id": b["property_id"], "info": b["value"]})
        if not (val(r) or {}).get("ok"):
            errs.append("bind %s: %s" % (b, (val(r) or {}).get("error")))
    for b in plan["update"]:
        r = call_edit("edit_props", {"ugc_id": b["ugc_id"],
                                     "props": {str(b["property_id"]): b["value"]}})
        if not (val(r) or {}).get("ok"):
            errs.append("update %s: %s" % (b, (val(r) or {}).get("error")))
    for p in plan["props"]:
        r = call_edit("edit_props", {"ugc_id": p["ugc_id"], "props": {p["property_id"]: p["value"]}})
        if not (val(r) or {}).get("ok"):
            errs.append("props %s: %s" % (p, (val(r) or {}).get("error")))
    for b in plan["unbind"]:
        r = call_edit("event_unbind", {"ugc_id": b["ugc_id"], "event_key": b["key"]})
        if not (val(r) or {}).get("ok"):
            errs.append("unbind %s: %s" % (b, (val(r) or {}).get("error")))
    for key in plan["delete_events"]:
        r = call_edit("event_delete", {"event_key": key})
        if not (val(r) or {}).get("ok"):
            errs.append("delete %s: %s" % (key, (val(r) or {}).get("error")))
    print("=== 执行结果 ===")
    print("错误:", errs or "无")
    print("提示：改动已进本地变更集，需 save_map 落盘。")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
