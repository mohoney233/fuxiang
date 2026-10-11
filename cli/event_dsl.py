#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_dsl.py — 事件语义 DSL（P2：人可读可编辑层）

方向：
  to-dsl   机器文件(map_events.json) → 语义 DSL(events.yaml)
  from-dsl 语义 DSL → 机器文件（再用 cli/event_import.py 导入地图）

DSL 样例：
```yaml
版本: 1
对象:
  进入区域: {类型: 区域, 位置: [-430.0, 1.5, 200.0]}
  成功提示: {类型: 信息提示, 位置: [-430.0, 53.0, 205.0],
             属性: {"20300411201": ["测试成功"]}}     # 未解码字段可原样写
事件:
  - 事件: 进入提示
    当: {对象: 进入区域, 时机: 进入区域时, 接收范围: 全部组件}
    则:
      - {对象: 成功提示, 动作: 显示信息}
```
名表（时机/动作/枚举/组件名）可在 `registry/semantic_names.json` 覆盖扩展。

运行解释器需带 PyYAML：
  <python> cli/event_dsl.py to-dsl ...
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    import yaml
except Exception:  # 允许退回 JSON
    yaml = None

# ---- 名表（可被 registry/semantic_names.json 覆盖）----
# property_id -> ("when"|"then", 动作/时机名, 与 info 逐位对齐的参数名列表)
# 语义来自 cli/event_export.py::FIELD_SEMANTICS（2026-09-18 全量解码，判据见该处注释）。
FIELD_ROLE = {
    # 区域 203003
    "20300311106": ("when", "进入区域时", ["事件键", "事件接收者"]),
    "20300311107": ("when", "离开区域时", ["事件键", "事件接收者"]),
    "20300311108": ("then", "使区域被销毁", ["事件键"]),
    "20300311109": ("then", "使区域变成可进入", ["事件键"]),
    "20300311110": ("then", "使区域变成不可进", ["事件键"]),
    "20300311114": ("then", "使区域变成可见", ["事件键"]),
    "20300311115": ("then", "使区域变成不可见", ["事件键"]),
    "20300311116": ("then", "使被销毁区域恢复", ["事件键"]),
    "20300311117": ("then", "区域传送到坐标", ["事件键", "x", "y", "z"]),
    "20300311119": ("then", "区域旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    # 信息提示 203004
    "20300411205": ("then", "使文字出现", ["事件键"]),
    "20300411206": ("then", "使文字消失", ["事件键"]),
    # 道具箱 202003
    "20200303001": ("when", "完成开箱时", ["事件键", "事件接收者"]),
    "20200303002": ("then", "使箱子可以被打开", ["事件键"]),
    # 障碍物 100000 / 几何体 100001 / 人数检测 202041 共用同一套显隐·传送·旋转字段。
    # ⚠️ 动作名必须**全局唯一**：from-dsl 靠 动作名→property_id 反查（THEN_BY_NAME），
    #    重名会让 DSL 编译到错误的组件上。
    "10000003002": ("then", "障碍物变为可见", ["事件键"]),
    "10000003003": ("then", "障碍物变为不可见", ["事件键"]),
    "10000003004": ("then", "障碍物传送到坐标", ["事件键", "x", "y", "z"]),
    "10000003005": ("then", "障碍物旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    "10000103002": ("then", "几何体变为可见", ["事件键"]),
    "10000103003": ("then", "几何体变为不可见", ["事件键"]),
    "10000103004": ("then", "几何体传送到坐标", ["事件键", "x", "y", "z"]),
    "10000103005": ("then", "几何体旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    "20204103002": ("then", "人数检测变为可见", ["事件键"]),
    "20204103003": ("then", "人数检测变为不可见", ["事件键"]),
    "20204103004": ("then", "人数检测传送到坐标", ["事件键", "x", "y", "z"]),
    "20204103005": ("then", "人数检测旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    # ⚠️ 事件键不在第 1 位：info = [玩家角色数量, 持续时间(秒), 事件键]
    "20204111101": ("when", "玩家角色数量达标", ["玩家角色数量", "持续时间", "事件键"]),
    # 消息事件 203025
    "20302516908": ("when", "检测符合要求", ["事件键"]),
    "20302516912": ("then", "使检测功能生效", ["事件键"]),
    "20302516913": ("then", "使检测功能失效", ["事件键"]),
    # 地下室盖板 202014
    "20201403002": ("then", "使盖板变成可见", ["事件键"]),
    "20201403003": ("then", "使盖板变成不可见", ["事件键"]),
}
# 事件键在 info 里的下标（由参数名列表里的「事件键」位置推出，兜底 0）。
# 权威来源是字段定义本身：游戏侧 bridge.fuxiang_edit_ops.event_field_shape() 会直接读
# property_type/property_params 算出 key_index；这里只是 DSL 编译期的等价副本。
FIELD_KEY_INDEX = {pid: next((i for i, n in enumerate(p) if n == "事件键"), 0)
                   for pid, (_r, _n, p) in FIELD_ROLE.items()}
# DSL 里省略某参数时的回填值（与游戏 schema 默认一致；其余按 0）
PARAM_DEFAULTS = {"事件接收者": 1286}
ENUM_NAMES = {1286: "全部组件", 1287: "交互者所属职业", 1288: "非交互者所属职业",
              1051: "全部玩家", 1314: "事件发出者", 1091: "无背景样式",
              1341: "自定义消息", 1351: "完全一致", 1501: "是"}
CN_COMPONENTS = {100000: "障碍物", 100001: "几何体", 202003: "道具箱", 202014: "地下室盖板",
                 202041: "人数检测", 203003: "区域", 203004: "信息提示",
                 203025: "消息事件", 203911: "运动器"}


def _key_index(params):
    return next((i for i, n in enumerate(params) if n == "事件键"), 0)


def _load_overrides():
    path = os.path.join(ROOT, "registry", "semantic_names.json")
    if not os.path.exists(path):
        return
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    for k, v in (doc.get("fields") or {}).items():
        FIELD_ROLE[k] = tuple(v)
        FIELD_KEY_INDEX[k] = _key_index(v[2])
    ENUM_NAMES.update({int(k): v for k, v in (doc.get("enums") or {}).items()})
    CN_COMPONENTS.update({int(k): v for k, v in (doc.get("components") or {}).items()})


_load_overrides()
WHEN_BY_NAME = {v[1]: k for k, v in FIELD_ROLE.items() if v[0] == "when"}
THEN_BY_NAME = {v[1]: k for k, v in FIELD_ROLE.items() if v[0] == "then"}
ENUM_BY_NAME = {v: k for k, v in ENUM_NAMES.items()}


def decode_enum(v):
    return ENUM_NAMES.get(v, v)


def encode_enum(v):
    return ENUM_BY_NAME.get(v, v)


# ---------------- to-dsl ----------------
def to_dsl(machine, out_path):
    doc = json.load(open(machine, encoding="utf-8"))
    labels = {}
    for uid, a in (doc.get("objects") or {}).items():
        labels[uid] = a.get("label") or ("obj_%s" % uid)
    out = {"版本": 1, "说明": "由 event_dsl.py to-dsl 生成；改完用 from-dsl 编译回机器文件再导入。",
           "对象": {}, "事件": []}
    # 只保留参与事件的对象 + 事件文内引用的对象
    used = set()
    for e in doc.get("events") or []:
        for b in e.get("bindings") or []:
            used.add(str(b.get("object_ugc_id_at_export")))
    for uid in used:
        a = (doc.get("objects") or {}).get(uid)
        if not a:
            continue
        item = {"类型": CN_COMPONENTS.get(a.get("component_type"), a.get("component_type")),
                "位置": a.get("position")}
        if a.get("exposed_props"):
            item["属性"] = a["exposed_props"]
        if a.get("exposed_prop_names"):
            item["属性说明"] = a["exposed_prop_names"]
        if a.get("exposed_props"):
            item["属性"] = a["exposed_props"]
        if a.get("exposed_prop_names"):
            item["属性说明"] = a["exposed_prop_names"]
        out["对象"][a.get("label")] = item
    for e in doc.get("events") or []:
        ev = {"事件": e["key"]}
        thens = []
        for b in e.get("bindings") or []:
            pid = str(b.get("property_id"))
            role, name, params = FIELD_ROLE.get(pid, (None, None, None))
            raw = b.get("raw_value")
            if not isinstance(raw, list):
                raw = [raw]
            label = labels.get(str(b.get("object_ugc_id_at_export")), b.get("object"))
            if role == "when":
                entry = {"对象": label, "时机": name or pid}
            else:
                entry = {"对象": label, "动作": name or pid}
            # 逐位输出非事件键参数（含传送坐标等，旧版会丢掉这些值）
            for i, nm in enumerate(params or []):
                if nm == "事件键" or i >= len(raw):
                    continue
                entry[nm] = decode_enum(raw[i])
            ki = FIELD_KEY_INDEX.get(pid, 0)
            if ki < len(raw) and raw[ki] != e["key"]:
                entry["事件键"] = raw[ki]          # 仅在与事件名不一致时显式写出
            if role == "when":
                ev["当"] = entry
            else:
                thens.append(entry)
        if thens:
            ev["则"] = thens
        out["事件"].append(ev)
    text = yaml.safe_dump(out, allow_unicode=True, sort_keys=False) if yaml else json.dumps(out, ensure_ascii=False, indent=2)
    open(out_path, "w", encoding="utf-8").write(text)
    print("DSL 已写出:", out_path)
    return 0


# ---------------- from-dsl ----------------
def from_dsl(dsl_path, machine_path, source_machine=None):
    raw = open(dsl_path, encoding="utf-8").read()
    if yaml:
        doc = yaml.safe_load(raw)
    else:
        doc = json.loads(raw)
    src = json.load(open(source_machine, encoding="utf-8")) if source_machine and os.path.exists(source_machine) else {}
    src_objs = {v.get("label"): (k, v) for k, v in (src.get("objects") or {}).items()}
    out = {"meta": {"schema_version": 1, "tool": "event_dsl from-dsl"},
           "objects": {}, "object_props": {}, "events": []}
    name2item = {}
    for label, o in (doc.get("对象") or {}).items():
        old = src_objs.get(label)
        if old:
            uid, a = old
            out["objects"][uid] = dict(a)
        else:
            out["objects"][label] = {"label": label, "item_id": None, "component_type": None,
                                     "position": o.get("位置"), "rotation": None,
                                     "ugc_id_at_export": None}
            name2item[label] = o.get("类型")
        if o.get("属性"):
            out["object_props"][label] = o["属性"]
    for e in doc.get("事件") or []:
        item = {"key": e["事件"], "sort_id": None, "in_save": True, "bindings": []}

        def build(pid, src):
            """按参数名逐位回填；事件键放在该字段的权威下标上。"""
            params = FIELD_ROLE[pid][2]
            ki = FIELD_KEY_INDEX.get(pid, 0)
            raw = []
            for i, nm in enumerate(params):
                if i == ki:
                    raw.append(src.get("事件键", e["事件"]))
                    continue
                v = src.get(nm)
                if v is None:
                    v = PARAM_DEFAULTS.get(nm, 0)
                raw.append(encode_enum(v))
            return raw

        when = e.get("当")
        if when:
            pid = WHEN_BY_NAME.get(when.get("时机"))
            if not pid:
                print("警告: 未知时机", when.get("时机"))
            else:
                raw_value = build(pid, when)
                item["bindings"].append({"role": "sender", "semantic": when.get("时机"),
                                         "object": when.get("对象"),
                                         "object_ugc_id_at_export": _uid_of(src_objs, when.get("对象")),
                                         "property_id": pid, "raw_value": raw_value,
                                         "value": dict(zip(FIELD_ROLE[pid][2], raw_value))})
        for t in e.get("则") or []:
            pid = THEN_BY_NAME.get(t.get("动作"))
            if not pid:
                print("警告: 未知动作", t.get("动作"))
                continue
            raw_value = build(pid, t)
            item["bindings"].append({"role": "receiver", "semantic": t.get("动作"),
                                     "object": t.get("对象"),
                                     "object_ugc_id_at_export": _uid_of(src_objs, t.get("对象")),
                                     "property_id": pid, "raw_value": raw_value,
                                     "value": dict(zip(FIELD_ROLE[pid][2], raw_value))})
        out["events"].append(item)
    json.dump(out, open(machine_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("机器文件已编译:", machine_path, "| 事件", len(out["events"]), "| 属性覆盖", len(out["object_props"]))
    return 0


def _uid_of(src_objs, label):
    v = src_objs.get(label)
    try:
        return int(v[0]) if v else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["to-dsl", "from-dsl"])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--source-machine", help="from-dsl 时用于继承锚点信息的机器文件")
    args = ap.parse_args()
    if args.cmd == "to-dsl":
        return to_dsl(args.src, args.dst)
    return from_dsl(args.src, args.dst, args.source_machine)


if __name__ == "__main__":
    sys.exit(main())
