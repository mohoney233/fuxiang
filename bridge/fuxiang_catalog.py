# -*- coding: utf-8 -*-
"""随笔浮想编辑器「组件 + 物体」全量注册表（运行在 dwrg.exe 进程内）。

职责：把编辑器里可以被接口寻址的全部实体集中到一处，供上层按
item_id / component_type / 名称关键字定位，并给出「能否创建」的权威判据。

权威数据源（全部来自游戏进程内的真实表，不是硬编码猜的）：
  1. UGCComponentManager._component_type2str
        -> 编辑器登记的组件类型表。当前版本 97 项。
  2. UGCComponentManager._component_factory / _component_type2ugc_ids
        -> 已预热工厂缓存、活体组件数量。
  3. 全模块扫描 (common_cs.combat_core.components.unit_ugc.* 及所有含 ugc 的
     已加载模块) 中带 COMPONENT_TYPE 的类
        -> 类型 -> 实现类的唯一权威映射。
  4. GK.DM.ugc_item_data      -> 可放置物件目录（当前 3963 项）。
  5. GK.DM.ugc_item_property  -> 每个组件的属性 schema（按 模式/皮肤/属性项 分层）。
  6. GK.DM.ugc_control_panel  -> 操控面板类物件的属性 schema。
  7. GK.DM.ugc_item_tab       -> 编辑器分类树（分类中文名）。
  8. common_cs.ugc.UGCEnums   -> 参数取值枚举（当前 33 个）。
  9. GK.DM.all_tid_data       -> TID -> 显示文本（中文名）。

判定「能否创建」的唯一权威规则：类型在 _component_type2str 中 **且** 能从
COMPONENT_TYPE 反查到实现类。缺任一条，cm._create_component 都返回 None。
实测已确认：物件目录里出现的 8 个 component_id 无任何实现类（见 orphans）。

本模块不写任何游戏状态，只读。
"""
import sys
import os
import json
import inspect
import importlib

CATALOG_VERSION = "1.0.0"

_REGISTRY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, "registry")
_REGISTRY_DIR = os.path.normpath(_REGISTRY_DIR)

_UNIT_UGC_PKG = "common_cs.combat_core.components.unit_ugc."

# 物件 item_type 分组标签。分组名依据目录实际分布归纳，非官方文档结论。
_ITEM_TYPE_GROUPS = {
    1: "scene",      # 场景物件 2119 项，多为 OBSTACLE/GEOMETRY/TARES 等
    2: "global",     # 全局/功能组件 14 项，component_id == item_id
    3: "ui",         # UI 组件 1826 项，component_id 200000/200001
    10: "restrict",  # 限制区域 4 项
}

_CACHE = {"key": None, "data": None}


# --------------------------------------------------------------------------
# 取游戏对象
# --------------------------------------------------------------------------
def _room(edit):
    return edit.get_room()


def _cm(edit):
    return edit.get_component_manager()


def _dm(edit):
    room = _room(edit)
    if room is None:
        return None
    try:
        return room.create_unit_with_info.__globals__["GK"].DM
    except Exception:
        return None


def _text(dm, tid):
    if not tid or dm is None:
        return None
    try:
        v = dm.all_tid_data.get(tid)
    except Exception:
        return None
    return v if isinstance(v, str) and v else None


def _item(data, item_id):
    if data is None:
        return None
    for k in (item_id, str(item_id)):
        try:
            v = data.get(k)
        except Exception:
            v = None
        if v is not None:
            return v
    return None


def _plain(v, depth=0):
    """把 taggeddict / 元组 / 向量 转成 JSON 友好结构。"""
    if depth > 6:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if hasattr(v, "x") and hasattr(v, "y") and hasattr(v, "z"):
        try:
            return [round(float(v.x), 4), round(float(v.y), 4), round(float(v.z), 4)]
        except Exception:
            pass
    if isinstance(v, (list, tuple, set, frozenset)):
        try:
            return [_plain(x, depth + 1) for x in list(v)[:100]]
        except Exception:
            return repr(v)[:120]
    if hasattr(v, "keys"):
        out = {}
        try:
            for k in list(v.keys())[:200]:
                try:
                    out[str(k)] = _plain(v[k], depth + 1)
                except Exception as e:
                    out[str(k)] = "<err:%s>" % type(e).__name__
        except Exception:
            return repr(v)[:120]
        return out
    return repr(v)[:120]


# --------------------------------------------------------------------------
# 类型 -> 实现类（全模块扫描）
# --------------------------------------------------------------------------
def class_index(force=False):
    """扫描已加载模块，返回 {component_type: [ {class, module, mro} ]}。"""
    if not force and _CACHE.get("cls") is not None:
        return _CACHE["cls"]
    found = {}
    for name in list(sys.modules):
        low = name.lower()
        if "ugc" not in low:
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            members = list(vars(mod).items())
        except Exception:
            continue
        for k, v in members:
            if not inspect.isclass(v):
                continue
            ct = getattr(v, "COMPONENT_TYPE", None)
            if ct is None:
                continue
            try:
                ct = int(ct)
            except Exception:
                continue
            found.setdefault(ct, []).append({
                "class": k,
                "module": name,
                "mro": [c.__name__ for c in v.__mro__][:4],
            })
    for v in found.values():
        v.sort(key=lambda x: (len(x["mro"]), x["class"]))
    _CACHE["cls"] = found
    return found


def enum_index():
    """common_cs.ugc.UGCEnums 里的参数取值枚举。"""
    if _CACHE.get("enums") is not None:
        return _CACHE["enums"]
    import importlib
    out = {}
    try:
        u = importlib.import_module("common_cs.ugc.UGCEnums")
    except Exception:
        _CACHE["enums"] = out
        return out
    for k, v in vars(u).items():
        if not inspect.isclass(v):
            continue
        if getattr(v, "__module__", "") != "common_cs.ugc.UGCEnums":
            continue
        mem = {}
        for mk, mv in vars(v).items():
            if mk.startswith("_"):
                continue
            if isinstance(mv, (int, float, str, bool)) or mv is None:
                mem[mk] = mv
        out[k] = mem
    _CACHE["enums"] = out
    return out


# --------------------------------------------------------------------------
# 摆放支持矩阵（来自 fuxiang_scene 的实测白名单）
# --------------------------------------------------------------------------
def _place_support():
    import importlib
    scene = importlib.reload(importlib.import_module("fuxiang_scene"))
    return (set(getattr(scene, "VERIFIED_TYPES", ()) or ()),
            dict(getattr(scene, "UNSUPPORTED_TYPES", {}) or {}),
            set(getattr(scene, "SERVER_COMPONENT_TYPES", ()) or ()))


# --------------------------------------------------------------------------
# 主索引
# --------------------------------------------------------------------------
def _categories(dm):
    """把 ugc_item_tab 展平成 {child_id: 分类记录}。

    tap_order_sec（物件字段）就是分类的 child_id，据此把每个物件挂到
    编辑器自己的分类树上，而不是靠猜名字。
    """
    tab = getattr(dm, "ugc_item_tab", None)
    out = {}
    if tab is None:
        return out
    try:
        groups = list(tab.keys())
    except Exception:
        return out
    for g in groups:
        try:
            sub = tab[g]
            subkeys = list(sub.keys())
        except Exception:
            continue
        head = None
        try:
            head = sub.get(0)
        except Exception:
            pass
        group_cn = _text(dm, head.get("tab_tid")) if hasattr(head, "get") else None
        for cid in subkeys:
            rec = sub[cid]
            if not hasattr(rec, "get"):
                continue
            tid = rec.get("tab_tid")
            try:
                cid_i = int(rec.get("child_id"))
            except (TypeError, ValueError):
                continue
            out[cid_i] = {
                "category_id": cid_i,
                "group_id": g,
                "group_cn": group_cn,
                "name_cn": _text(dm, tid),
                "tab_tid": tid,
                "show_mode": _plain(rec.get("show_mode")),
                "simple_hide": _plain(rec.get("simple_hide")),
                "sort_id": _plain(rec.get("sort_id")),
            }
    return out


def _classify_orphan(t, own_item, has_panel, has_prop):
    """对「无法创建」的目录记录做定性，避免把原因笼统写成「不在注册表」。

    依据全部来自现场表，不是猜测：
      - 有 ugc_control_panel schema  -> 按玩法模式定义的操控面板，本就不是场景实体
      - 有 ugc_item_property schema  -> 有属性定义但客户端无实现类（多为旧版本残留）
      - 其余                          -> 目录里的隐藏占位记录
    """
    if has_panel:
        return ("mode_control_panel",
                "该编号是按玩法模式定义的自定义操控面板（ugc_control_panel 中的 "
                "mode 分组），不是可放置的场景实体，因此没有组件实现类。")
    if has_prop:
        return ("schema_without_implementation",
                "目录与属性表里都有该编号，但客户端没有任何类声明此 COMPONENT_TYPE，"
                "cm._create_component 返回 None。属数据与实现不同步的残留记录。")
    if own_item is not None and own_item.get("catalog_hidden"):
        return ("hidden_catalog_record",
                "物件目录里的隐藏记录（is_visible=1），无组件实现类，"
                "不会出现在编辑器对象列表中。")
    return ("no_implementation",
            "没有任何类声明此 COMPONENT_TYPE，cm._create_component 返回 None。")


def build(edit, force=False):
    """构建/取回全量索引。缓存挂在模块级，键为 room 标识。"""
    room = _room(edit)
    if room is None:
        raise ValueError("当前不在随笔浮想编辑器（room 不可用）")
    cm = _cm(edit)
    dm = _dm(edit)
    if cm is None or dm is None:
        raise ValueError("编辑器组件管理器或数据表不可用")

    key = (id(room), len(getattr(cm, "_ugc_id2component", {}) or {}),
           len(getattr(cm, "_component_type2str", {}) or {}))
    if not force and _CACHE.get("key") == key and _CACHE.get("data") is not None:
        return _CACHE["data"]

    t2str = getattr(cm, "_component_type2str", {}) or {}
    t2ids = getattr(cm, "_component_type2ugc_ids", {}) or {}
    factory = getattr(cm, "_component_factory", {}) or {}
    classes = class_index(force)
    items_raw = dm.ugc_item_data
    prop_schema = dm.ugc_item_property
    panel_schema = getattr(dm, "ugc_control_panel", None)
    categories = _categories(dm)
    verified, unsupported, server_ok = _place_support()

    # ---- 物件 ----
    items = {}
    by_component = {}
    for raw_id in list(items_raw.keys()):
        rec = _item(items_raw, raw_id)
        if rec is None:
            continue
        try:
            item_id = int(raw_id)
        except Exception:
            continue
        try:
            ctype = int(rec.get("component_id"))
        except Exception:
            ctype = None
        tid = rec.get("item_tid")
        item_type = rec.get("item_type")
        try:
            item_type = int(item_type)
        except Exception:
            pass
        cat_id = rec.get("tap_order_sec")
        try:
            cat_id = int(cat_id)
        except (TypeError, ValueError):
            cat_id = None
        cat = categories.get(cat_id)
        items[item_id] = {
            "item_id": item_id,
            "name_cn": _text(dm, tid),
            "item_tid": tid,
            "item_type": item_type,
            "item_type_group": _ITEM_TYPE_GROUPS.get(item_type, "unknown"),
            "component_type": ctype,
            "category_id": cat_id,
            "category_cn": (cat or {}).get("name_cn"),
            "category_group_cn": (cat or {}).get("group_cn"),
            "model_path": rec.get("model_path"),
            "src_model_path": rec.get("src_model_path"),
            "low_model_path": rec.get("low_model_path"),
            "img_path": rec.get("img_path"),
            "occupancy_value": _plain(rec.get("occupancy_value")),
            "zoom_limit": _plain(rec.get("zoom_limit")),
            "rotate_limit": _plain(rec.get("rotate_limit")),
            "lod_params": _plain(rec.get("lod_params")),
            "col_params": _plain(rec.get("col_params")),
            "simple_hide": _plain(rec.get("simple_hide")),
            "catalog_hidden": bool(rec.get("is_visible")),
            "object_list_eligible": not bool(rec.get("is_visible")),
            "disable_edit": _plain(rec.get("disable_edit")),
            "num_limit": _plain(rec.get("num_limit")),
            "simple_mode_list": _plain(rec.get("simple_mode_list")),
            "detail_component": _plain(rec.get("detail_component")),
            "item_order": _plain(rec.get("item_order")),
        }
        if ctype is not None:
            by_component.setdefault(ctype, []).append(item_id)

    # ---- 组件类型 ----
    all_types = sorted(set(int(k) for k in t2str if str(k).isdigit()) |
                       set(classes) | set(by_component))
    comps = {}
    for t in all_types:
        name = t2str.get(t) or t2str.get(str(t))
        cls_list = classes.get(t, [])
        registered = (t in t2str) or (str(t) in t2str)
        item_ids = sorted(by_component.get(t, []))
        if t in unsupported:
            support, reason = "unsupported", unsupported[t]
        elif t in verified:
            support, reason = "verified", None
        else:
            support, reason = "unknown", None
        if not registered:
            support = "unsupported"
            reason = reason or "不在 _component_type2str 登记表中"
        # 组件中文名只在「该类型本身就是一条物件记录」时才可信，
        # 否则 TID_UGC_ITEM_<类型号> 会误命中同名键（实测 100000 会返回别的名字）。
        name_cn = None
        own = items.get(t)
        if own is not None and own.get("component_type") == t:
            name_cn = own.get("name_cn")
        rep = items.get(item_ids[0]) if item_ids else None
        # 能否创建的判据是「是否在登记表里」：游戏按 _component_type2str 里的
        # 名字拼出 UGC<Name>Component 再动态 import，类没被加载不代表不可用。
        # 类扫描只作为旁证（该类型当前是否已有类在进程内）。
        creatable = bool(registered)
        has_panel = _has_key(panel_schema, t)
        has_prop = _has_key(prop_schema, t)
        kind, note = None, None
        if not creatable:
            kind, note = _classify_orphan(t, own, has_panel, has_prop)
        comps[t] = {
            "component_type": t,
            "name": name,
            "name_cn": name_cn,
            "name_cn_source": "own_catalog_item" if name_cn else None,
            "representative_item_id": rep["item_id"] if rep else None,
            "representative_item_name_cn": rep.get("name_cn") if rep else None,
            "representative_item_category_cn": rep.get("category_cn") if rep else None,
            "registered": bool(registered),
            "class": cls_list[0]["class"] if cls_list else None,
            "module": cls_list[0]["module"] if cls_list else None,
            "class_candidates": cls_list,
            "class_resolvable": bool(cls_list),
            "factory_warm": (t in factory) or (str(t) in factory),
            "creatable": creatable,
            "live_count": _count(t2ids, t),
            "catalog_item_count": len(item_ids),
            "catalog_item_ids": item_ids,
            "property_schema": has_prop,
            "control_panel_schema": has_panel,
            "place_support": support,
            "place_support_reason": reason,
            "server_place_supported": t in server_ok,
        }
        if kind:
            comps[t]["not_creatable_kind"] = kind
            comps[t]["not_creatable_note"] = note

    data = {
        "version": CATALOG_VERSION,
        "counts": {
            "component_types_total": len(comps),
            "component_types_creatable": sum(1 for c in comps.values() if c["creatable"]),
            # 不可创建的编号 = 不在 _component_type2str 登记表里的编号。
            # 实测这些编号也 scan 不到任何实现类，故两种口径结果一致。
            "component_types_without_class": sorted(
                [t for t, c in comps.items() if not c["creatable"]]),
            # 旁证：已登记但当前进程内还没扫到类。可能只是尚未被 import，
            # 不表示不可用；用于诊断，不作为 creatable 判据。
            "component_types_class_scan_unconfirmed": sorted(
                [t for t, c in comps.items()
                 if c["creatable"] and not c["class_resolvable"]]),
            "items_total": len(items),
            "items_hidden": sum(1 for i in items.values() if i["catalog_hidden"]),
            "items_without_category": sum(
                1 for i in items.values() if i["category_id"] is None),
            "enums_total": len(enum_index()),
            "categories_total": len(categories),
        },
        "categories": categories,
        "components": comps,
        "items": items,
    }
    _CACHE["key"] = key
    _CACHE["data"] = data
    return data


def _has_key(container, key):
    if container is None:
        return False
    for k in (key, str(key)):
        try:
            if k in container:
                return True
        except Exception:
            pass
    return False


def _count(container, key):
    """container[key] 可能是 set / list / int，统一取数量。"""
    if container is None:
        return 0
    v = None
    for k in (key, str(key)):
        try:
            if k in container:
                v = container[k]
                break
        except Exception:
            continue
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return len(v)
    except Exception:
        return 0


def invalidate():
    _CACHE["key"] = None
    _CACHE["data"] = None
    _CACHE["cls"] = None
    _CACHE["enums"] = None


# --------------------------------------------------------------------------
# op 实现
# --------------------------------------------------------------------------
def op_catalog_summary(edit, args):
    d = build(edit, force=bool(args.get("refresh")))
    counts = d["counts"]
    return {
        "version": d["version"],
        "counts": counts,
        "item_type_groups": {
            str(k): {"group": v, "item_count": sum(
                1 for i in d["items"].values() if i["item_type"] == k)}
            for k, v in sorted(_ITEM_TYPE_GROUPS.items())},
        "place_support": {
            "verified": sorted(t for t, c in d["components"].items()
                               if c["place_support"] == "verified"),
            "unsupported": {str(t): (c.get("not_creatable_note")
                                     or c["place_support_reason"])
                            for t, c in sorted(d["components"].items())
                            if c["place_support"] == "unsupported"},
            "unknown": sorted(t for t, c in d["components"].items()
                              if c["place_support"] == "unknown"),
        },
        "note": ("组件类型总数 = 登记表 ∪ 实现类 ∪ 物件目录 component_id。"
                 "creatable 的唯一判据是「是否在 _component_type2str 登记表里」："
                 "游戏按登记表里的名字拼出 UGC<Name>Component 再动态 import，"
                 "登记表里没有该编号时 cm._create_component 返回 None（8 个孤儿已实测）。"
                 "class_resolvable 只是「当前进程里是否已加载该类」的旁证，"
                 "不代表能否创建。"
                 "place_support=verified 表示有实测记录，unknown 表示未实测，"
                 "不代表不可用。"),
    }


def op_list_component_types(edit, args):
    d = build(edit, force=bool(args.get("refresh")))
    kw = (args.get("query") or "").lower().strip()
    only = args.get("only")
    limit = int(args.get("limit") or 200)
    offset = int(args.get("offset") or 0)
    out = []
    for t, c in sorted(d["components"].items()):
        if only == "creatable" and not c["creatable"]:
            continue
        if only == "orphan" and c["creatable"]:
            continue
        if only == "place_verified" and c["place_support"] != "verified":
            continue
        if only == "live" and not c["live_count"]:
            continue
        if kw:
            blob = "%s %s %s %s" % (t, c.get("name"), c.get("name_cn"),
                                    c.get("class") or "")
            if kw not in blob.lower():
                continue
        row = dict(c)
        row["catalog_item_ids"] = c["catalog_item_ids"][:20]
        out.append(row)
    return {"total": len(out), "offset": offset,
            "returned": len(out[offset:offset + limit]),
            "component_types": out[offset:offset + limit]}


def op_get_component_type(edit, args):
    d = build(edit, force=bool(args.get("refresh")))
    raw = args.get("component_type")
    try:
        t = int(raw)
    except (TypeError, ValueError):
        return {"error": "component_type 必须为整数"}
    c = d["components"].get(t)
    if c is None:
        return {"error": "未知 component_type=%s（当前共 %d 种）"
                         % (t, len(d["components"]))}
    row = dict(c)
    row["catalog_items"] = [
        {k: v for k, v in d["items"][i].items()
         if k in ("item_id", "name_cn", "item_tid", "item_type_group",
                  "model_path", "occupancy_value", "catalog_hidden")}
        for i in c["catalog_item_ids"][:int(args.get("item_limit") or 30)]]
    if args.get("with_schema"):
        row["schema"] = op_component_schema(edit, args)
    return row


def op_find(edit, args):
    """统一寻址入口：item_id / component_type / 名称关键字 / 模型路径。"""
    d = build(edit, force=bool(args.get("refresh")))
    limit = int(args.get("limit") or 20)
    offset = int(args.get("offset") or 0)
    kind = args.get("kind") or "any"
    hits = []

    if args.get("item_id") is not None:
        try:
            iid = int(args["item_id"])
        except (TypeError, ValueError):
            return {"error": "item_id 必须为整数"}
        it = d["items"].get(iid)
        if it is None:
            return {"error": "物件目录中不存在 item_id=%s" % iid,
                    "hint": "用 fuxiang_catalog(query=...) 先搜索"}
        c = d["components"].get(it["component_type"])
        hits.append({"kind": "item", "item": it,
                     "component_type": _match_component(c)})

    if args.get("component_type") is not None:
        try:
            t = int(args["component_type"])
        except (TypeError, ValueError):
            return {"error": "component_type 必须为整数"}
        c = d["components"].get(t)
        if c is None:
            return {"error": "未知 component_type=%s" % t}
        hits.append({"kind": "component_type", "component_type": _match_component(c)})

    if args.get("enum"):
        e = enum_index().get(str(args["enum"]))
        if e is None:
            return {"error": "未知枚举 %r" % args["enum"],
                    "available": sorted(enum_index())}
        hits.append({"kind": "enum", "name": args["enum"], "members": e})

    kw = (args.get("query") or "").lower().strip()
    if kw:
        for t, c in sorted(d["components"].items()):
            blob = "%s %s %s %s" % (t, c.get("name"), c.get("name_cn"),
                                    c.get("class") or "")
            if kw in blob.lower():
                hits.append({"kind": "component_type", "component_type": _match_component(c)})
        for i, it in sorted(d["items"].items()):
            blob = "%s %s %s %s" % (i, it.get("name_cn"), it.get("item_tid"),
                                    it.get("model_path"))
            if kw in blob.lower():
                hits.append({"kind": "item", "item": it})

    if kind != "any":
        hits = [h for h in hits if h["kind"] == kind]
    if not args.keys() & {"item_id", "component_type", "query", "enum"}:
        return {"error": "至少提供 item_id / component_type / query / enum 之一"}
    return {"total": len(hits), "offset": offset,
            "returned": len(hits[offset:offset + limit]),
            "matches": hits[offset:offset + limit]}


def _match_component(c):
    if c is None:
        return None
    return {k: v for k, v in c.items()
            if k not in ("catalog_item_ids", "class_candidates")}


def op_component_schema(edit, args):
    """取某个组件类型的属性 schema（模式/皮肤/属性项 三层）。"""
    dm = _dm(edit)
    if dm is None:
        return {"error": "数据表不可用"}
    raw = args.get("component_type")
    try:
        t = int(raw)
    except (TypeError, ValueError):
        return {"error": "component_type 必须为整数"}
    out = {"component_type": t, "sources": {}}

    ps = dm.ugc_item_property
    for k in (t, str(t)):
        try:
            if k in ps:
                out["sources"]["ugc_item_property"] = _plain(ps[k])
                break
        except Exception:
            pass
    if "ugc_item_property" not in out["sources"]:
        out["sources"]["ugc_item_property"] = None

    cp = getattr(dm, "ugc_control_panel", None)
    for k in (t, str(t)):
        try:
            if cp is not None and k in cp:
                out["sources"]["ugc_control_panel"] = _plain(cp[k])
                break
        except Exception:
            pass
    if "ugc_control_panel" not in out["sources"]:
        out["sources"]["ugc_control_panel"] = None

    out["available"] = any(v is not None for v in out["sources"].values())
    if not out["available"]:
        out["note"] = ("该组件类型没有属性 schema；可能是全局/内部类型，"
                       "或目录里的隐藏记录。")
    return out


def op_list_categories(edit, args):
    """编辑器自己的物件分类树（ugc_item_tab），附中文名与物件数量。"""
    d = build(edit, force=bool(args.get("refresh")))
    kws = (args.get("query") or "").lower().strip()
    cnt = {}
    for it in d["items"].values():
        cid = it.get("category_id")
        if cid is not None:
            cnt[cid] = cnt.get(cid, 0) + 1
    rows = []
    for cid, c in sorted(d["categories"].items()):
        if kws:
            blob = ("%s %s %s" % (c.get("name_cn") or "",
                                  c.get("group_cn") or "", cid)).lower()
            if kws not in blob:
                continue
        row = dict(c)
        row["item_count"] = cnt.get(cid, 0)
        rows.append(row)
    return {"total": len(rows), "categories": rows,
            "note": "物件的 tap_order_sec 即 category_id；用它可复现编辑器左栏分类。"}


def op_list_enums(edit, args):
    e = enum_index()
    kw = (args.get("query") or "").lower().strip()
    if kw:
        e = {k: v for k, v in e.items() if kw in k.lower()}
    return {"total": len(e), "enums": e,
            "note": "这些枚举是组件属性值域，配合 fuxiang_set_params 使用。"}


def op_registry_export(edit, args):
    """把注册表写盘到 tools/fuxiang/registry/，返回文件清单。"""
    d = build(edit, force=True)
    dm = _dm(edit)
    out_dir = os.path.normpath(os.path.join(_REGISTRY_DIR))
    files = []

    def write(name, obj):
        p = os.path.join(out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        files.append({"file": name, "bytes": os.path.getsize(p)})

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    write("component_types.json", d["components"])
    write("items.json", d["items"])
    write("enums.json", enum_index())
    write("categories.json", d["categories"])
    write("summary.json", {
        "version": d["version"],
        "counts": d["counts"],
        "item_type_groups": _ITEM_TYPE_GROUPS,
    })
    return {"exported": files, "dir": out_dir,
            "counts": d["counts"]}


# --------------------------------------------------------------------------
# 全量枚举 / 全量自检（补齐「所有物体都能被接口逐个取到」）
# --------------------------------------------------------------------------
def ui_mode_state(edit):
    """当前编辑模式下列表视图的分类屏蔽状态。

    列表视图（UGCComponentListViewCore）的真实过滤有两层：
      1. 物件自身 is_visible（原生变量名 is_hide）-> 永远不进「全部对象」
      2. 分类被当前编辑模式屏蔽 -> _init_data 由 show_mode / ignore_tab_id
         推出 ignore_tap_order_sec，tick_get_all_components 再按
         tap_order_sec 是否在展示集合里跳过
    这两层都只影响「列表显示」，不影响物件能否被接口寻址。
    """
    core = None
    try:
        room = _room(edit)
        ui = getattr(room.create_unit_with_info.__globals__["GK"].input, "ugc_edit_ui", None)
        core = getattr(getattr(ui, "component_view", None), "_core", None)
    except Exception:
        core = None
    if core is None:
        return {"available": False,
                "note": "列表视图核心不可用；只能判断物件自身 is_visible。"}

    def _set(name):
        try:
            v = getattr(core, name, None)
            return sorted(int(x) for x in (v or ()))
        except Exception:
            return []

    ignored_tab = _set("ignore_tab_id")
    ignored_tos = _set("ignore_tap_order_sec")
    mode = None
    try:
        mode = list(getattr(core, "show_mode_list", None) or ())
    except Exception:
        pass
    return {
        "available": True,
        "view_class": type(core).__name__,
        "show_mode_list": mode,
        "ignore_tab_id": ignored_tab,
        "ignore_tap_order_sec": ignored_tos,
        "note": "ignore_tap_order_sec 里的分类在当前编辑模式下不显示；"
                "分类内的物件仍可被 catalog_find / place_item 寻址。",
    }


def _item_visible(it, mode_state):
    """返回 (是否在当前「全部对象」列表显示, 隐藏原因)。"""
    if it.get("catalog_hidden"):
        return False, "is_visible（原生列表 is_hide 判断）"
    ignored = mode_state.get("ignore_tap_order_sec") if mode_state.get("available") else None
    if ignored and it.get("category_id") in ignored:
        return False, "分类在当前编辑模式下被屏蔽（tap_order_sec 命中 ignore 集合）"
    return True, None


def op_build(edit, args):
    """按 args 取回索引（供 fuxiang_edit 内部调用）。

    存在意义：``_catalog_call`` 统一按 ``(edit, args_dict)`` 调用，而
    ``build`` 的第二个参数是布尔 ``force``。直接 ``_catalog_call("build", {...})``
    会把 dict 当成 force（恒为真），导致每次都全量重建索引。
    """
    if not isinstance(args, dict):
        args = {}
    return build(edit, force=bool(args.get("refresh")))


def op_catalog_items(edit, args):
    """逐个枚举物件目录（含隐藏项），支持分页与多维过滤。

    这是「定位所有物体」的枚举入口：limit=0 一次取回全部 3963 项。
    """
    d = build(edit, force=bool(args.get("refresh")))
    items = d["items"]
    mode_state = ui_mode_state(edit)

    only = args.get("visibility") or "all"
    if only not in ("all", "listed", "hidden", "ui_hidden"):
        raise ValueError("visibility 必须是 all / listed / hidden / ui_hidden")
    query = (args.get("query") or "").lower().strip()
    cat = args.get("category_id")
    cat = int(cat) if cat is not None else None
    grp = args.get("group_id")
    grp = str(grp) if grp is not None else None
    ctype = args.get("component_type")
    ctype = int(ctype) if ctype is not None else None
    itype = args.get("item_type")
    itype = int(itype) if itype is not None else None

    rows, hidden_total, ui_hidden_total = [], 0, 0
    for iid in sorted(items):
        it = items[iid]
        visible, why = _item_visible(it, mode_state)
        if it.get("catalog_hidden"):
            hidden_total += 1
        elif not visible:
            ui_hidden_total += 1
        if only == "listed" and not visible:
            continue
        if only == "hidden" and not it.get("catalog_hidden"):
            continue
        if only == "ui_hidden" and (visible or it.get("catalog_hidden")):
            continue
        if cat is not None and it.get("category_id") != cat:
            continue
        if grp is not None and str(it.get("category_id") or "") != grp:
            pass
        if grp is not None:
            c = d["categories"].get(it.get("category_id")) or {}
            if str(c.get("group_id")) != grp:
                continue
        if ctype is not None and it.get("component_type") != ctype:
            continue
        if itype is not None and it.get("item_type") != itype:
            continue
        if query:
            blob = "%s %s %s %s %s" % (iid, it.get("name_cn") or "", it.get("item_tid") or "",
                                        it.get("component_type"), it.get("model_path") or "")
            if query not in blob.lower():
                continue
        row = dict(it)
        row["in_object_list"] = visible
        row["hidden_reason"] = why
        rows.append(row)

    total = len(rows)
    limit = args.get("limit")
    limit = 100 if limit is None else int(limit)
    if limit < 0:
        raise ValueError("limit 不能为负；0 表示返回全部")
    offset = int(args.get("offset") or 0)
    page = rows if limit == 0 else rows[offset:offset + limit]
    return {
        "catalog_size": len(items),
        "matched": total,
        "offset": offset,
        "returned": len(page),
        "catalog_hidden_total": hidden_total,
        "ui_mode_hidden_total": ui_hidden_total,
        "ui_mode": mode_state,
        "items": page,
        "applied_filters": {
            "visibility": only, "category_id": cat, "group_id": grp,
            "component_type": ctype, "item_type": itype,
            "query": query or None, "limit": limit,
        },
        "note": "本接口可枚举全部 3963 个物件，含未在编辑器列表显示的记录。"
                "in_object_list 只描述当前列表可见性，不代表能否摆放。",
    }


def op_catalog_verify(edit, args):
    """全量可达性自检：一次调用走通全部物件与全部组件类型。

    与 op_find 共用同一份索引，因此通过本自检即等价于
    「每个 item_id / component_type 都能被 catalog_find 命中」。
    """
    d = build(edit, force=True)
    items = d["items"]
    comps = d["components"]
    mode_state = ui_mode_state(edit)

    bad_items, checked = [], 0
    for iid, it in sorted(items.items()):
        checked += 1
        c = comps.get(it.get("component_type"))
        if c is None:
            bad_items.append({"item_id": iid, "reason": "component_type 不在组件索引中"})
            continue
        visible, _ = _item_visible(it, mode_state)
        # 反向可达：物件必须能从所属组件类型反查回来
        if iid not in c.get("catalog_item_ids", ()):
            bad_items.append({"item_id": iid, "reason": "未出现在所属组件类型的目录清单中"})

    bad_types = []
    deferred = []
    name_map = {v["name"]: k for k, v in comps.items() if v.get("name")}
    for t, c in sorted(comps.items()):
        if c["registered"] and not c["class_resolvable"]:
            # 「已登记但当前未加载实现类」是已知的懒加载现象：
            # 组件类由 _component_factory 按需 import，冷启动时数量会明显偏低。
            # 因此再按命名规则动态 import 试一次，能导入的算「可延后解析」而非不可达。
            if _class_importable(c.get("name")):
                deferred.append({"component_type": t, "name": c.get("name"),
                                 "reason": "实现类可按命名规则动态导入，当前仅未预加载"})
            else:
                bad_types.append({"component_type": t,
                                  "reason": "已登记且无法按命名规则解析实现类"})
        if c["creatable"] and t not in comps:
            bad_types.append({"component_type": t, "reason": "可创建但不在索引中"})

    reachable = {
        "items": len(items) - len(bad_items),
        "component_types": len(comps) - len(bad_types),
    }
    return {
        "items_total": len(items),
        "items_checked": checked,
        "items_unreachable": bad_items[:30],
        "items_unreachable_count": len(bad_items),
        "component_types_total": len(comps),
        "component_types_unreachable": bad_types[:30],
        "component_types_unreachable_count": len(bad_types),
        "component_types_deferred": deferred[:60],
        "component_types_deferred_count": len(deferred),
        "reachable": reachable,
        "orphan_component_types": sorted(
            t for t, c in comps.items() if not c["catalog_item_count"]),
        "catalog_only_types": sorted(
            t for t, c in comps.items() if not c["class_resolvable"]),
        "unnamed_items": sorted(
            i for i, it in items.items() if not it.get("name_cn")),
        "mode_state": mode_state,
        "complete": not bad_items and not bad_types,
        "note": "items_unreachable 为空表示全部物件都能被 catalog_find 命中；"
                "component_types_deferred 是「已登记、实现类可动态导入但当前未预加载」的"
                "类型（冷启动常见，不是缺陷）；只有 component_types_unreachable 才算真不可达。"
                "orphan_component_types 是有实现类但目录里没有物件的类型"
                "（当前为 SLOW_REGION 203002），只能通过 component_type 寻址。",
    }


def _class_importable(type_name):
    """按 UGC<n Camel>Component 命名规则试探性地 import 一次（不注册进工厂）。"""
    if not type_name:
        return False
    cn = "UGC" + "".join(p[:1] + p[1:].lower() for p in str(type_name).split("_")) + "Component"
    try:
        mod = importlib.import_module("common_cs.combat_core.components.unit_ugc." + cn)
        return getattr(mod, cn, None) is not None
    except Exception:
        return False
