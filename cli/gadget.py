#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gadget.py — 机关模板：保存 / 实例化（P2-1）

把一组"对象 + 事件绑定"打包成模板，之后一条命令搬到任意位置复制一份，
事件按相对坐标自动重连，新对象自动登记标签。

用法:
  python cli/gadget.py save 进入提示机关 --labels 进入区域,成功提示
  python cli/gadget.py show 进入提示机关
  python cli/gadget.py instantiate 进入提示机关 --at -700,51,300 [--apply] [--suffix _2]

默认 dry-run；`--apply` 才真摆放/建事件。摆放走服务端路径（plcae_item mode=server + poll）。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402
from fxapi import unwrap_key as _unwrap_key  # noqa: E402  # 剥壳真源
from event_export import EXPOSED_PROPS  # noqa: E402

GADGET_DIR = os.path.join(ROOT, "projects", "gadgets")
LABELS_PATH = os.path.join(ROOT, "projects", "event_dump", "labels.json")


def val(r):
    v = r.get("value") if isinstance(r, dict) else None
    return v if isinstance(v, dict) else {}


def unwrap(node, key=None):
    """按字段名定位并取该字段值；剥壳规则见 cli/fxapi.py::unwrap_key（唯一真源）。

    注：本文件所有调用点均显式传 key（"targets" / "target"）。
    """
    if key is None:
        return None
    return _unwrap_key(node, key)


def load_labels():
    if not os.path.exists(LABELS_PATH):
        return {}
    return (json.load(open(LABELS_PATH, encoding="utf-8")) or {}).get("labels") or {}


def save_labels(labels):
    os.makedirs(os.path.dirname(LABELS_PATH), exist_ok=True)
    doc = {"labels": labels, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(doc, open(LABELS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def resolve(entry, objs, tol=2.0):
    for o in objs:
        if entry.get("item_id") is not None and o.get("item_id") != entry["item_id"]:
            continue
        p, q = o.get("position") or [], entry.get("position") or []
        if not p or not q or p[0] is None:
            continue
        if max(abs(p[0] - q[0]), abs(p[2] - q[2])) <= tol:
            return o
    return None


def live_objects():
    targets = unwrap(call_edit("edit_targets", {"limit": 0}), "targets") or []
    objs = []
    for t in targets:
        uid = t.get("ugc_id")
        d = unwrap(call_edit("edit_target", {"ugc_id": uid}), "target") or {}
        tf = d.get("transform") or {}
        objs.append({"ugc_id": uid, "item_id": d.get("item_id") or t.get("item_id"),
                     "component_type": d.get("component_type"), "position": tf.get("position"),
                     "rotation": tf.get("rotation"), "scale": tf.get("scale"),
                     "props": {str(e.get("property_id") or e.get("type")): e.get("info")
                               for e in (d.get("properties") or [])}})
    return objs


# 写属性袋后需要同步到运行时的字段：(component_type, property_id) -> 原生方法名。
# 判据（2026-09-18 反汇编 UGCBaseComponent.apply_components_client:1332）：
# 该方法**只**把 11 个方法名桥接到 _client_* 变体——
#   _set_color/_set_material/_set_init_visible/_set_alpha/_set_collision/
#   _set_ui_pos/_set_ui_rot/_set_ui_height/_set_ui_width/_set_lod/_set_coordinate_offset
# 其余（全部 `_init_*` 与 _appear/_enter_region/... 逻辑字段）在**编辑会话内不会被应用**，
# 想立刻在编辑器/试玩外看到效果必须显式调用原生方法。
# 注意：这只影响"当前会话的运行时"；持久化读的是属性袋，保存+重进后引擎自行生效。
RUNTIME_REFRESH = {
    (203003, "20300311104"): "_init_accessible",
    (203003, "20300311111"): "_init_effect_type",
    (203003, "20300311112"): "_init_enter_effect_times",
    (203003, "20300311113"): "_init_leave_effect_times",
    (203004, "20300411201"): "_init_content",
    (203004, "20300411204"): "_init_duration",
    (203004, "20300411207"): "_init_effect_units",
    (203004, "20300411208"): "_init_style",
    (203004, "20300411209"): "_init_enable_replace",
    (203004, "20300411210"): "_init_delay_time",
    (203004, "20300411211"): "_init_specific_effect_type",
    (203025, "20302516901"): "_init_effect_type",
    (203025, "20302516907"): "_init_match_type",
    (203025, "20302516911"): "_init_available",
}


def refresh_runtime(ugc_id, comp_type, props):
    """写袋后把 _init_* 类字段同步到运行时（apply_components_client 不覆盖这些）。"""
    calls = []
    for pid, value in (props or {}).items():
        m = RUNTIME_REFRESH.get((comp_type, str(pid)))
        if not m:
            continue
        v = value[0] if isinstance(value, list) and value else value
        calls.append((m, v))
    if not calls:
        return
    code = chr(10).join([
        "room = core.room",
        "cm = room.ugc_component_manager",
        "c = cm._ugc_id2component.get(%d)" % ugc_id,
        "out = []",
        "for name, val in %r:" % (calls,),
        "    try:",
        "        getattr(c, name)(val)",
        "        out.append(name)",
        "    except Exception as e:",
        "        out.append(name + ':err:' + type(e).__name__)",
        "__result__ = out",
    ])
    return val(call_edit("eval", {"code": code})).get("result")


def cmd_save(args):
    labels = load_labels()
    objs = live_objects()
    wanted = [x.strip() for x in args.labels.split(",") if x.strip()]
    picked = []
    for name in wanted:
        entry = labels.get(name)
        if not entry:
            print("标签不存在:", name)
            return 1
        o = resolve(entry, objs)
        if not o:
            print("标签定位失败:", name)
            return 1
        picked.append((name, o))
    origin = picked[0][1]["position"]
    gadget = {"name": args.name, "origin": [round(x, 3) for x in origin], "objects": [], "events": []}
    for name, o in picked:
        exposed = {}
        for pid in (EXPOSED_PROPS.get(o["component_type"]) or {}):
            if str(pid) in o["props"]:
                exposed[str(pid)] = o["props"][str(pid)]
        gadget["objects"].append({
            "label": name, "item_id": o["item_id"], "component_type": o["component_type"],
            "offset": [round(o["position"][i] - origin[i], 3) for i in range(3)],
            "rotation": o.get("rotation"), "scale": o.get("scale"), "props": exposed})
    # 事件：凡引用到这些对象的都收进模板
    ev = val(call_edit("event_list", {}))
    names = [n for n, _ in picked]
    for e in ev.get("events") or []:
        binds = []
        for b in e.get("refs") or []:
            o = next((x for _, x in picked if x["ugc_id"] == b.get("ugc_id")), None)
            if not o:
                continue
            binds.append({"label": [n for n, x in picked if x is o][0],
                          "property_id": str(b.get("property_id")),
                          "raw_value": eval(b["current_value"]) if isinstance(b.get("current_value"), str)
                          else b.get("current_value")})
        if binds:
            gadget["events"].append({"key": e["event_key"], "bindings": binds})
    os.makedirs(GADGET_DIR, exist_ok=True)
    path = os.path.join(GADGET_DIR, "%s.json" % args.name)
    json.dump(gadget, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("模板已保存:", path)
    print("对象 %d 个 | 事件 %d 个" % (len(gadget["objects"]), len(gadget["events"])))
    return 0


def cmd_show(args):
    path = os.path.join(GADGET_DIR, "%s.json" % args.name)
    print(open(path, encoding="utf-8").read()[:2000])
    return 0


def cmd_instantiate(args):
    path = os.path.join(GADGET_DIR, "%s.json" % args.name)
    g = json.load(open(path, encoding="utf-8"))
    at = [float(x) for x in args.at.split(",")]
    suffix = args.suffix or "_副本"
    print("=== 实例化『%s』→ %s (suffix=%s) ===" % (g["name"], at, suffix))
    for o in g["objects"]:
        tgt = [round(at[i] + o["offset"][i], 3) for i in range(3)]
        print("  放置 %-10s item=%-7s → %s" % (o["label"], o["item_id"], tgt))
    for e in g["events"]:
        print("  事件 %-10s 绑定 %s" % (e["key"], [b["label"] for b in e["bindings"]]))
    if not args.apply:
        print("（dry-run；加 --apply 执行）")
        return 0

    labels = load_labels()
    placed = {}
    errors = []
    existing = live_objects()
    for o in g["objects"]:
        tgt = [round(at[i] + o["offset"][i], 3) for i in range(3)]
        # 幂等：目标位置已有同 item_id 的对象 → 复用，不重复摆放
        reuse = None
        for eo in existing:
            if eo.get("item_id") != o["item_id"]:
                continue
            ep = eo.get("position") or [None, None, None]
            if ep[0] is None:
                continue
            if max(abs(ep[0] - tgt[0]), abs(ep[2] - tgt[2])) <= args.tolerance and                     abs((ep[1] or 0) - tgt[1]) <= max(5.0, args.tolerance * 3):
                reuse = eo
                break
        if reuse:
            print("  复用已有对象 %s (ugc_id=%s) @ %s" % (o["label"], reuse["ugc_id"], reuse["position"]))
            placed[o["label"]] = {"ugc_id": reuse["ugc_id"], "position": reuse["position"]}
            if o.get("props"):
                call_edit("edit_props", {"ugc_id": reuse["ugc_id"], "props": o["props"]})
                print("    运行时刷新:", refresh_runtime(reuse["ugc_id"], o["component_type"], o["props"]))
            continue
        rid = "gadget-%s-%s-%d" % (g["name"], o["label"], int(time.time()))
        call_edit("place_item", {"item_id": o["item_id"], "position": tgt,
                                 "rotation": o.get("rotation") or [0, 0, 0],
                                 "scale": o.get("scale") or [1, 1, 1],
                                 "mode": "server", "request_id": rid})
        status = None
        p = {}
        for i in range(30):
            p = val(call_edit("place_poll", {"request_id": rid}))
            status = p.get("status")
            print("    [%s] poll %d: %s" % (o["label"], i, status))
            if status in ("placed", "ambiguous", "conflict", "finalize_failed", "overdue"):
                break
            time.sleep(4)
        if status != "placed":
            errors.append("放置 %s 失败: %s (%s)" % (o["label"], status, str(p.get("error") or p.get("note"))[:80]))
            continue
        ugc_id = p.get("ugc_id")
        placed[o["label"]] = {"ugc_id": ugc_id, "position": tgt}
        # 属性
        if o.get("props"):
            call_edit("edit_props", {"ugc_id": ugc_id, "props": o["props"]})
            print("    运行时刷新:", refresh_runtime(ugc_id, o["component_type"], o["props"]))
        # 标签登记（便于后续文件化管理）
        new_label = "%s%s" % (o["label"], suffix)
        labels[new_label] = {"item_id": o["item_id"], "component_type": o["component_type"],
                             "position": tgt, "matched_ugc_id": ugc_id, "last_match": "matched",
                             "note": "gadget 实例化自 %s" % g["name"]}
    # 事件
    for e in g["events"]:
        new_key = "%s%s" % (e["key"], suffix)
        raw = call_edit("event_create", {"name": new_key})
        err_text = str((raw or {}).get("error") or "") + str(((raw or {}).get("details") or {}).get("error") or "")
        if not (raw or {}).get("ok") and "已存在" not in err_text:
            errors.append("建事件 %s 失败: %s" % (new_key, err_text[:80]))
        else:
            print("  事件 %s 就绪" % new_key)
        for b in e["bindings"]:
            obj = placed.get(b["label"])
            if not obj:
                continue
            # 整段透传模板形态；事件键位置由 op 按字段权威形态改写为 new_key
            rr = val(call_edit("event_bind", {"ugc_id": obj["ugc_id"], "event_key": new_key,
                                              "property_id": b["property_id"],
                                              "info": list(b["raw_value"])}))
            if not rr.get("ok"):
                errors.append("绑定 %s@%s 失败: %s" % (new_key, obj["ugc_id"], rr.get("error")))
    save_labels(labels)
    print("=== 实例化结果 ===")
    print("已放置:", json.dumps(placed, ensure_ascii=False))
    print("错误:", errors or "无")
    print("提示：需 save_map 落盘；新对象标签已写入 labels.json。")
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["save", "show", "instantiate"])
    ap.add_argument("name")
    ap.add_argument("--labels", help="save 时：逗号分隔的标签（第一个作为原点）")
    ap.add_argument("--at", help="instantiate 时：目标坐标 x,y,z")
    ap.add_argument("--suffix", help="新事件/标签后缀，默认 _副本")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tolerance", type=float, default=2.0)
    args = ap.parse_args()
    return {"save": cmd_save, "show": cmd_show, "instantiate": cmd_instantiate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
