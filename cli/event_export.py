#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_export.py — 把地图内事件导出为可编辑文件（PoC）

用法:
  python cli/event_export.py [out_path] [--labels P]   # 默认 projects/event_dump/<map>_events.yaml

`--labels P`：标签文件位置，默认按本脚本的工具根解析；桥接控制台的额外桥接用的是通道克隆
（console/bridges/<名>/），不传主根的绝对路径就读不到用户标签、锚点只剩自动名。

输出两层：
  1) objects  —— 锚点表（label → 稳定身份：item_id / component_type / position / 导出时 ugc_id）
  2) events   —— 事件键 + 逐条引用（role / object / property / 语义名 / 解码后的值）

设计要点：
  * ugc_id 在重载后会重新编号，因此**锚点用 (item_id, position)**，ugc_id 只作参考。
  * 事件字段的语义（role/参数名）来自一张已知表；未知字段原样导出，不丢信息。
  * 枚举参数（如 1286=全部组件）用 TID 表解码为中文，失败则回退原值。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402
from fxapi import unwrap_key as _unwrap_key  # noqa: E402  # 剥壳真源


def unwrap(node, key):
    """按字段名定位并取该字段值；剥壳规则见 cli/fxapi.py::unwrap_key（唯一真源）。"""
    return _unwrap_key(node, key)


# 已知事件字段契约。property_id -> (角色, 语义, 与 info 逐位对齐的参数名列表)
#
# 2026-09-18 P3-1 全量解码（探针 probe/p3_decode_family.py / p3_decode_comp.py）：
#   判据 = 字段 property_type 含 '80001' 段（该段第二元素 1=发送 / 2=接收）；
#   'property_type' 的 ';' 段与 'property_params' 的 '{}' 段一一对齐（383/383 字段 0 例外）；
#   info 的形态 = ugc_utils.get_default_value_info(定义)[0] 各段顺序拼接（反汇编
#   UGCBaseComponent.apply_components_client:1332 实证）——**所以事件键不一定在 info[0]**，
#   名字列表按 info 逐位给出即可正确解码（如 20204111101 的键在第 3 位）。
#   语义名与 5 个既往实测契约（11106/11107/11205/03001/03004）逐条校准一致。
FIELD_SEMANTICS = {
    # ---- 区域 REGION 203003 ----
    "20300311106": ("sender", "区域.进入区域时", ["事件键", "事件接收者"]),
    "20300311107": ("sender", "区域.离开区域时", ["事件键", "事件接收者"]),
    "20300311108": ("receiver", "区域.使区域被销毁", ["事件键"]),
    "20300311109": ("receiver", "区域.使区域变成可进入", ["事件键"]),
    "20300311110": ("receiver", "区域.使区域变成不可进", ["事件键"]),
    "20300311114": ("receiver", "区域.使区域变成可见", ["事件键"]),
    "20300311115": ("receiver", "区域.使区域变成不可见", ["事件键"]),
    "20300311116": ("receiver", "区域.使被销毁区域恢复", ["事件键"]),
    "20300311117": ("receiver", "区域.接收事件·传送到坐标", ["事件键", "x", "y", "z"]),
    "20300311119": ("receiver", "区域.接收事件·旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    # ---- 信息提示 MIST 203004 ----
    "20300411205": ("receiver", "信息提示.使文字出现", ["事件键"]),
    "20300411206": ("receiver", "信息提示.使文字消失", ["事件键"]),
    # ---- 道具箱 BOX 202003 ----
    "20200303001": ("sender", "道具箱.完成开箱时", ["事件键", "事件接收者"]),
    "20200303002": ("receiver", "道具箱.使箱子可以被打开", ["事件键"]),
    # ⚠️ 事件键在第 2 位：info = [手持物 id 列表, 事件键, 事件接收者]
    "20200303005": ("sender", "道具箱.开箱获得手持物后发送", ["手持物", "事件键", "事件接收者"]),
    # ---- 障碍物 OBSTACLE 100000 / 几何体 GEOMETRY 100001 / 人数检测 202041 ----
    # 三者共用同一套「显隐 / 传送 / 旋转」接收字段（各组件 event_type 编号不同）
    "10000003002": ("receiver", "障碍物.使自己变为可见", ["事件键"]),
    "10000003003": ("receiver", "障碍物.使自己变为不可见", ["事件键"]),
    "10000003004": ("receiver", "障碍物.接收事件·传送到坐标", ["事件键", "x", "y", "z"]),
    "10000003005": ("receiver", "障碍物.接收事件·旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    "10000103002": ("receiver", "几何体.使自己变为可见", ["事件键"]),
    "10000103003": ("receiver", "几何体.使自己变为不可见", ["事件键"]),
    "10000103004": ("receiver", "几何体.接收事件·传送到坐标", ["事件键", "x", "y", "z"]),
    "10000103005": ("receiver", "几何体.接收事件·旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    "20204103002": ("receiver", "人数检测.使自己变为可见", ["事件键"]),
    "20204103003": ("receiver", "人数检测.使自己变为不可见", ["事件键"]),
    "20204103004": ("receiver", "人数检测.接收事件·传送到坐标", ["事件键", "x", "y", "z"]),
    "20204103005": ("receiver", "人数检测.接收事件·旋转到方向", ["事件键", "dir_x", "dir_y", "dir_z"]),
    # ⚠️ 事件键在第 3 位：info = [玩家角色数量, 持续时间(秒), 事件键]
    "20204111101": ("sender", "人数检测.玩家角色数量达标", ["玩家角色数量", "持续时间", "事件键"]),
    # ---- 消息事件 CHAT_EVENT 203025 ----
    "20302516908": ("sender", "消息事件.检测符合要求", ["事件键"]),
    "20302516912": ("receiver", "消息事件.使检测功能生效", ["事件键"]),
    "20302516913": ("receiver", "消息事件.使检测功能失效", ["事件键"]),
    # ---- 地下室盖板 202014 ----
    "20201403002": ("receiver", "盖板.使盖板变成可见", ["事件键"]),
    "20201403003": ("receiver", "盖板.使盖板变成不可见", ["事件键"]),
}

# 事件相关对象的"可直接编辑属性"（用户最常改的行为参数；中文名取自 TID 文案）
EXPOSED_PROPS = {
    203003: {"20300311101": "可看见", "20300311104": "可进入", "20300311105": "规则生效次数",
             "20300311111": "对哪些玩家生效", "20300311112": "进入区域生效次数",
             "20300311113": "离开区域生效次数", "20300310005": "颜色",
             "20300310006": "材质", "20300310007": "透明度"},
    203004: {"20300411201": "显示文字内容", "20300411204": "文字内容持续时间",
             "20300411207": "对哪些玩家生效", "20300411208": "文字背景样式",
             "20300411209": "被其他信息顶替", "20300411210": "被顶替后持续时间",
             "20300411211": "生效对象"},
    203025: {"20302516901": "对哪些玩家显示", "20302516904": "检测消息类型",
             "20302516905": "检测消息内容", "20302516906": "检测消息内容匹配",
             "20302516907": "检测消息要求", "20302516911": "检测功能初始生效"},
    202041: {"20204103001": "可看见", "20204102001": "材质", "20204102002": "颜色",
             "20204102003": "透明度"},
    100001: {"10000110005": "材质", "10000110006": "颜色", "10000110007": "透明度",
             "10000102001": "碰撞", "10000118701": "绑定运动器"},
    100000: {"10000002001": "开启碰撞", "10000003001": "可看见", "10000002003": "隐藏距离",
             "10000018701": "绑定运动器"},
}

# 已知枚举解码（来自 TID_BATTLE_UGC_PROSETTING_OPTION_*，实测）
ENUM_NAMES = {
    1286: "全部组件",
    1287: "交互者所属职业",
    1288: "非交互者所属职业",
    1051: "全部玩家",
    1314: "事件发出者",
    1341: "自定义消息",
    1351: "完全一致",
    1501: "是",
    1091: "无背景样式",
}


def decode_value(prop_id, info):
    """把 info 列表按字段契约解码为 {参数名: 值}。info 可能是 repr 字符串（接口回传）。"""
    if isinstance(info, str):
        import ast
        try:
            info = ast.literal_eval(info)
        except Exception:
            return {"原始值": info}
    if not isinstance(info, list):
        return {"原始值": info}
    names = FIELD_SEMANTICS.get(str(prop_id), (None, None, None))[2]
    out = {}
    for i, v in enumerate(info):
        name = names[i] if names and i < len(names) else ("参数%d" % i)
        if isinstance(v, int) and v in ENUM_NAMES:
            out[name] = "%s(%s)" % (ENUM_NAMES[v], v)
        else:
            out[name] = v
    return out


def load_label_map(path=None):
    """labels.json -> {ugc_id: label}（用坐标+item_id 匹配，稳过 ugc_id）。"""
    path = path or os.path.join(ROOT, "projects", "event_dump", "labels.json")
    if not os.path.exists(path):
        return {}
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    return doc.get("labels") or {}


def match_label(label_map, item_id, position, tol=2.0):
    """匹配标签；优先用户命名（auto != True），其次自动标签。"""
    best, best_d, best_auto = None, None, True
    for name, e in label_map.items():
        if item_id is not None and e.get("item_id") != item_id:
            continue
        p = e.get("position") or [None, None, None]
        if not position or p[0] is None:
            continue
        d = max(abs(p[0] - position[0]), abs(p[2] - position[2]))
        if d > tol:
            continue
        auto = bool(e.get("auto"))
        if (best is None or auto < best_auto or (auto == best_auto and d < (best_d or 1e9))):
            best, best_d, best_auto = name, d, auto
    return best


def main():
    argv = sys.argv[1:]
    labels_arg = None
    if "--labels" in argv:
        i = argv.index("--labels")
        if i + 1 >= len(argv):
            print("缺少 --labels 的取值", file=sys.stderr)
            return 2
        labels_arg = argv[i + 1]
        del argv[i:i + 2]
    out_path = argv[0] if argv else None
    data = call_edit("event_list", {})
    events = (data.get("value") or {}).get("events") or []
    targets = (unwrap(call_edit("edit_targets", {"limit": 0}), "targets")) or []

    # 锚点表：ugc_id -> 稳定身份
    anchors = {}
    label_map = load_label_map(labels_arg)
    total = len(targets)
    print("枚举场景物件：%d 件（逐件问一次，大图需要几分钟，请等它跑完）" % total, flush=True)
    for n, t in enumerate(targets, 1):
        uid = t.get("ugc_id")
        detail = unwrap(call_edit("edit_target", {"ugc_id": uid}), "target") or {}
        tf = detail.get("transform") or t.get("transform") or {}
        pos = tf.get("position")
        pos_r = [round(x, 2) for x in pos] if pos else None
        label = match_label(label_map, detail.get("item_id") or t.get("item_id"), pos_r or [])
        exposed = {}
        exposed_names = {}
        for pid, cn in (EXPOSED_PROPS.get(detail.get("component_type")) or {}).items():
            for e in (detail.get("properties") or []):
                if str(e.get("property_id") or e.get("type")) == str(pid):
                    exposed[pid] = e.get("info")
                    exposed_names[pid] = cn
                    break
        anchors[uid] = {
            "label": label or "%s_%s" % ((detail.get("component_name") or detail.get("component_class") or "obj"),
                                         uid),
            "has_label": bool(label),
            "exposed_props": exposed or None,
            "exposed_prop_names": exposed_names or None,
            "item_id": detail.get("item_id") or t.get("item_id"),
            "component_type": detail.get("component_type"),
            "position": [round(x, 2) for x in pos] if pos else None,
            "rotation": [round(x, 2) for x in (tf.get("rotation") or [])] or None,
            "ugc_id_at_export": uid,
            "owner_uid_at_export": detail.get("owner_uid"),
        }
        if n % 200 == 0:
            print("  …已读取 %d/%d" % (n, total), flush=True)

    doc = {
        "meta": {
            "schema_version": 1,
            "tool": "fuxiang event_export",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "锚点用 (item_id, position) 定位；ugc_id 重载后会变，仅作参考。",
            "event_count": len(events),
        },
        "objects": anchors,
        "events": [],
    }
    for e in events:
        # 跳过已删除态（info=None / 无引用且未在保存口径）
        if not e.get("in_save") and not (e.get("refs") or []):
            continue
        item = {"key": e["event_key"], "sort_id": e.get("sort_id"), "in_save": e.get("in_save"),
                "bindings": []}
        for r in e.get("refs") or []:
            pid = str(r.get("property_id"))
            role, semantic, _ = FIELD_SEMANTICS.get(pid, ("unknown", "未解码字段", None))
            raw = r.get("current_value")
            if isinstance(raw, str):
                import ast as _ast
                try:
                    raw = _ast.literal_eval(raw)
                except Exception:
                    pass
            item["bindings"].append({
                "role": role,
                "semantic": semantic,
                "object": (anchors.get(r.get("ugc_id")) or {}).get("label") or ("ugc_id_%s" % r.get("ugc_id")),
                "object_ugc_id_at_export": r.get("ugc_id"),
                "property_id": pid,
                "raw_value": raw,          # 无损：导入按此写回
                "value": decode_value(pid, raw),
            })
        doc["events"].append(item)

    if not out_path:
        out_dir = os.path.join(ROOT, "projects", "event_dump")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "map_events.json")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("导出完成:", out_path)
    print("事件数:", len(doc["events"]), "| 锚点对象数:", len(doc["objects"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
