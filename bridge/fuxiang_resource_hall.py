# -*- coding: utf-8 -*-
"""随笔浮想编辑器：「资源大厅」（组合库）资源定位层 —— 只读。

定位结论（实机复核，dwrg.exe）
------------------------------
「资源大厅」有两层，**资源清单在 UI 层，不在 part 层**：

1) **列表页 UI** ``ui.UIBattleUGCPart.UIBattleUGCResourceHall.UIBattleUGCResourceHall``
   —— 真正的资源清单在这里：

   ====================  =================================================
   ``_cur_cards``       当前页签/排序/标签下**已渲染的卡片**（资源清单本体）
   ``_category``        当前页签：20001 官方推荐 / 20002 组合分享 / 20003 我的组合
   ``_cur_sort``        当前排序；``_cur_tag_id`` 当前标签；``_search_keyword`` 搜索词
   ``_top_tab_clones``  3 个页签节点；``_sort_node_map`` / ``_tag_node_map`` 节点表
   ====================  =================================================

   卡片结构：``{combo_id, name(已是中文), cover_url, combo_share_code, occupy_num,
   is_recommend, is_downloaded, is_selected, multi_select_mode, full_doc}``，
  其中 ``full_doc`` 是完整组合记录（``UGC_COMBO_KEY`` 子集）。

   加载路径：官方/组合分享走 ``_async_load_rank``（排行榜，异步生成器，
   对应 ``rank_ugc_combo_official_*`` 等 key）；我的走 ``_load_mine_recent``
   → ``part.request_mine_recent(typ)``。**实测官方页签 376 条。**

2) **数据 part** ``common_cs.ugc_edit_core.parts.composite_library.UGCEditCompositeLibraryPart``
   —— 只保存**本地/已下载**的组合：

   ======================  ====================================  ==================
   列表                    含义                                  取用方式
   ======================  ====================================  ==================
   ``_local_library``      我保存/自制的组合                     ``get_library(1)``
   ``_shared_library``     我已下载/分享过的组合（**非全量大厅**）  ``get_library(2)``
   ======================  ====================================  ==================

   ⚠️ ``_shared_library`` 实测只有 1 条（即已下载的那一个），**不要**把它当成
   大厅全量清单——全量在 UI 的 ``_cur_cards``。

资源本体是**组合记录**，字段由 ``ComuKeys.UGC_COMBO_KEY`` 定义（25 项），
``combo_data = {'root': {...}, 'child': [...], 'motion': [...]}``，
``child`` 每项形如 ``{'ugc_type': <component_id>, 'item_id': <物件id>,
'params': [{'type': <属性id>, 'info': [...]}]}``。
注意：组合包内 **``ugc_type`` 存的是 component_id**，与实体 unit 上
``ugc_type=item_id`` 的约定相反。

本模块只读：不下载、不上传、不分享、不创建、不删除、不切换页签。
"""
import importlib

PART_IFACE = ("common_cs.ugc_edit_core.parts.composite_library."
              "iUGCEditCompositeLibraryPart")
PART_IMPL = ("common_cs.ugc_edit_core.parts.composite_library."
             "UGCEditCompositeLibraryPart")
UI_LIBRARY = "ui.UIBattleUGCPart.UIBattleUGCCompositeLibraryPart"
UI_HALLINFO = "ui.UIBattleUGCPart.UIBattleUGCResourceHallInfo"
# 资源大厅【列表页】本体：真实渲染的卡片列表在这里
UI_RESOURCE_HALL = "ui.UIBattleUGCPart.UIBattleUGCResourceHall"
UI_HALL_KEY = "UIBattleUGCResourceHall"

# ComuKeys.UGC_COMPOSITE_* 读不到时的兜底（已实机核对）
FALLBACK = {
    "LIBRARY_TYPE": {"LOCAL": 1, "SHARED": 2},
    "HALL_CATEGORY": {"OFFICIAL": 20001, "SHARED": 20002, "MINE": 20003},
    "HALL_SORT": {"RECOMMEND": "0", "LATEST": "1", "HOTTEST": "2",
                  "RECENT_DL": "3", "RECENT_USE": "4"},
    "PACK_COMP_TYPE": {"ROOT": "root", "CHILD": "child", "MOTION": "motion"},
    "EDITABLE_STATE": {"UNLIMITED": "0", "UNOWNABLE": "1", "UNEDITABLE": "2"},
    "SHARED_RANK_TYPE": {"USE": "use", "DOWNLOAD": "download"},
    "REVIEW_STATUS": {"REVIEWING": "0", "PASSED": "1", "REFUSED": "2"},
    "RANK_KEYS": {
        "HOT": "rank_ugc_combo_hot",
        "NEW": "rank_ugc_combo_new",
        "REC": "rank_ugc_combo_rec",
        "OFFICIAL_HOT": "rank_ugc_combo_official_hot",
        "OFFICIAL_REC": "rank_ugc_combo_official_rec",
    },
}
# 记录字段的中文说明（来自 ComuKeys.UGC_COMBO_KEY，含义据 UI 用法归纳）
COMBO_FIELDS = {
    "_id": "组合 ID（详情页按它寻址）",
    "name": "名称（多为 TID，需查 all_tid_data）",
    "desc": "描述 TID",
    "author": "作者 {uid, name, face_id, face_frame_id, face_vfx_id}",
    "cover": "封面图 URL 列表",
    "tag": "标签 id（查 DM.ugc_combo_tag）",
    "combo_data": "组合本体 root/child/motion",
    "combo_permission": "分享权限（UGC_COMPOSITE_EDITABLE_STATE 口径）",
    "combo_share_code": "分享码（可用它拉取同一组合）",
    "create_ts": "创建时间戳",
    "last_ts": "最近修改时间戳",
    "publish_ts": "发布时间戳",
    "dl_cnt": "下载次数",
    "like_cnt": "点赞数",
    "score": "评分",
    "occupy_num": "占用值",
    "gm_rank": "官方排名",
    "gm_ver": "版本号",
    "fp_photos": "首图/缩略图信息",
    "photo_extra": "图片附加信息 {md5}",
    "limited_hot": "限时-热门排序位",
    "limited_new": "限时-最新排序位",
    "limited_rec": "限时-推荐排序位",
    "limited_official_hot": "限时-官方热门排序位",
    "limited_official_rec": "限时-官方推荐排序位",
}


# ---------------------------------------------------------------------------
# 访问器
# ---------------------------------------------------------------------------
def _room(edit):
    return edit.get_room()


def _globals(edit):
    room = _room(edit)
    if room is None:
        raise ValueError("当前不在随笔浮想编辑器（room 不可用）")
    return room.create_unit_with_info.__globals__


def _dm(edit):
    return getattr(_globals(edit).get("GK"), "DM", None)


def _comukeys():
    try:
        return importlib.import_module("common_cs.ComuKeys")
    except Exception:
        return None


def enum_of(name):
    """取 ComuKeys 里的类枚举 -> dict；取不到用兜底表。"""
    ck = _comukeys()
    key = "UGC_COMPOSITE_" + name
    v = getattr(ck, key, None) if ck is not None else None
    if isinstance(v, type):
        out = {k: getattr(v, k) for k in dir(v)
               if not k.startswith("_") and isinstance(getattr(v, k), (int, str, float))}
        if out:
            return out
    return dict(FALLBACK.get(name, {}))


def combo_key_map():
    ck = _comukeys()
    v = getattr(ck, "UGC_COMBO_KEY", None) if ck is not None else None
    if isinstance(v, type):
        out = {k: getattr(v, k) for k in dir(v)
               if not k.startswith("_") and isinstance(getattr(v, k), str)}
        if out:
            return out
    return {}


def get_part(edit):
    """取组合库 part。"""
    core = edit.get_core()
    if core is None:
        raise ValueError("UGCEditCore 不可用（需处于随笔浮想编辑器）")
    iface = importlib.import_module(PART_IFACE).iUGCEditCompositeLibraryPart
    part = core.get_part(iface)
    if part is None:
        raise ValueError("组合库 part 不可用（UGCEditCompositeLibraryPart 未注册）")
    return core, part


def tid_text(edit, tid):
    dm = _dm(edit)
    if not isinstance(tid, str):
        return None
    try:
        v = dm.all_tid_data.get(tid)
    except Exception:
        return None
    return v if isinstance(v, str) and v else None


# ---------------------------------------------------------------------------
# 记录规范化
# ---------------------------------------------------------------------------
def _combo_summary(edit, rec, library_label):
    """把一条组合记录压成便于定位的摘要（不含 combo_data 本体）。"""
    if not hasattr(rec, "get"):
        return {"error": "记录结构异常", "repr": repr(rec)[:120]}
    cd = rec.get("combo_data") or {}
    parts = {}
    total_children = 0
    if hasattr(cd, "keys"):
        for k in cd.keys():
            v = cd.get(k)
            try:
                n = len(v)
            except TypeError:
                n = 1 if v else 0
            parts[str(k)] = n
            if str(k) == "child":
                total_children = n
    tag = rec.get("tag")
    tag_cn = None
    try:
        tcfg = _dm(edit).ugc_combo_tag.get(tag)
        if tcfg is not None:
            tag_cn = tid_text(edit, tcfg.get("tag_tid"))
    except Exception:
        pass
    author = rec.get("author") or {}
    return {
        "combo_id": str(rec.get("_id")),
        "library": library_label,
        "name": rec.get("name"),
        "name_cn": tid_text(edit, rec.get("name")),
        "desc": rec.get("desc"),
        "desc_cn": tid_text(edit, rec.get("desc")),
        "is_official_pgc": str(rec.get("name") or "").startswith("TID_UGC_PGC"),
        "author": {
            "uid": str(author.get("uid")) if hasattr(author, "get") else None,
            "name": author.get("name") if hasattr(author, "get") else None,
        },
        "tag": tag,
        "tag_cn": tag_cn,
        "share_code": rec.get("combo_share_code"),
        "permission": str(rec.get("combo_permission")),
        "dl_cnt": rec.get("dl_cnt"),
        "like_cnt": rec.get("like_cnt"),
        "score": rec.get("score"),
        "occupy_num": rec.get("occupy_num"),
        "gm_ver": rec.get("gm_ver"),
        "create_ts": rec.get("create_ts"),
        "publish_ts": rec.get("publish_ts"),
        "cover_url": (list(rec.get("cover")) or [None])[0] if rec.get("cover") else None,
        "combo_data_parts": parts,
        "child_count": total_children,
    }


def _combo_detail(edit, rec, library_label, with_param_detail=False):
    out = _combo_summary(edit, rec, library_label)
    cd = rec.get("combo_data") or {}
    detail = {}
    prop_cache = {}
    if hasattr(cd, "keys"):
        for key in cd.keys():
            v = cd.get(key)
            if isinstance(v, (list, tuple)):
                rows = []
                for elem in list(v)[:400]:
                    if not hasattr(elem, "get"):
                        rows.append({"repr": repr(elem)[:80]})
                        continue
                    params = []
                    for p in list(elem.get("params") or [])[:60]:
                        if hasattr(p, "get"):
                            item = {"type": p.get("type"), "info": _plain(p.get("info"))}
                            if with_param_detail:
                                item["property"] = _param_property(
                                    edit, elem.get("item_id"), elem.get("ugc_type"), p.get("type"), prop_cache)
                            params.append(item)
                        else:
                            params.append({"repr": repr(p)[:60]})
                    rows.append({
                        "item_id": elem.get("item_id"),
                        "ugc_type": elem.get("ugc_type"),
                        "param_count": len(elem.get("params") or []),
                        "params": params,
                    })
                detail[str(key)] = {"count": len(v), "elements": rows[:60]}
            elif hasattr(v, "keys"):
                detail[str(key)] = {"dict_keys": [str(x) for x in list(v.keys())[:40]]}
            else:
                detail[str(key)] = {"repr": repr(v)[:200]}
    out["combo_data"] = detail
    return out


def _plain(v, depth=0):
    if depth > 4:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_plain(x, depth + 1) for x in list(v)[:40]]
    if hasattr(v, "keys"):
        return {str(k): _plain(v[k], depth + 1) for k in list(v.keys())[:40]}
    return repr(v)[:80]


def _param_property(edit, item_id, ugc_type, param_type, cache):
    """尽力把组合包里的 params.type（属性 id）解析成属性表里的条目。

    params.type 与 DM.ugc_item_property 中的 property_id 同源，但分层结构
    （模式 / 皮肤类 / 属性项）需要遍历，故做一次深搜并缓存。
    """
    if param_type is None:
        return None
    key = str(ugc_type if ugc_type is not None else item_id)
    if key not in cache:
        table = {}
        try:
            props = _dm(edit).ugc_item_property
            root = props.get(int(key)) if str(key).isdigit() else None
            if root is not None:
                stack = [root]
                seen = 0
                while stack and seen < 20000:
                    cur = stack.pop()
                    seen += 1
                    if hasattr(cur, "keys"):
                        pid = None
                        try:
                            pid = cur.get("property_id")
                        except Exception:
                            pid = None
                        if pid is not None:
                            table[str(pid)] = {
                                "property_type": cur.get("property_type"),
                                "property_params": _plain(cur.get("property_params")),
                                "tab_id": cur.get("tab_id"),
                                "source_data_type": cur.get("source_data_type"),
                                "source_data_id": cur.get("source_data_id"),
                            }
                        for k in cur.keys():
                            child = cur.get(k)
                            if hasattr(child, "keys"):
                                stack.append(child)
        except Exception:
            table = {}
        cache[key] = table
    return cache[key].get(str(param_type))


# ---------------------------------------------------------------------------
# op 实现
# ---------------------------------------------------------------------------
def op_hall_info(edit, args):
    """资源大厅（组合库）总览：结构常量 + 当前状态 + 记录字段表。"""
    core, part = get_part(edit)
    lt = enum_of("LIBRARY_TYPE")
    loaded = {
        "local_loaded": bool(getattr(part, "_local_loaded", False)),
        "shared_loaded": bool(getattr(part, "_shared_loaded", False)),
    }
    libs = {}
    for label, t in lt.items():
        try:
            L = part.get_library(t)
            libs[label] = {"library_type": t, "count": len(L),
                           "is_full": bool(part.is_library_full(t))}
        except Exception as e:
            libs[label] = {"library_type": t, "error": "%s: %s" % (type(e).__name__, e)}

    tags = {}
    try:
        for k, v in (_dm(edit).ugc_combo_tag or {}).items():
            tid = v.get("tag_tid")
            tags[str(k)] = {
                "id": v.get("id"),
                "name": v.get("name"),
                "name_cn": tid_text(edit, tid),
                "main_tag_sort": v.get("main_tag_sort"),
                "seek_tag_sort": v.get("seek_tag_sort"),
                "is_visible": v.get("is_visible"),
                "is_can_share": v.get("is_can_share"),
            }
    except Exception as e:
        tags = {"error": "%s: %s" % (type(e).__name__, e)}

    # 条目是否已下载（用于判断「大厅里的资源是否已在本地」）
    downloaded = []
    try:
        for rec in (part.get_library(lt.get("LOCAL", 1)) or []):
            if hasattr(rec, "get"):
                downloaded.append(str(rec.get("_id")))
    except Exception:
        pass

    return {
        "resource_hall": {
            "part_class": type(part).__name__,
            "part_interface": PART_IFACE.rsplit(".", 1)[-1],
            "library_ui_class": UI_LIBRARY.rsplit(".", 1)[-1],
            "detail_ui_class": UI_HALLINFO.rsplit(".", 1)[-1],
        },
        "hall_categories": enum_of("HALL_CATEGORY"),
        "hall_sorts": enum_of("HALL_SORT"),
        "library_types": lt,
        "pack_component_types": enum_of("PACK_COMP_TYPE"),
        "editable_states": enum_of("EDITABLE_STATE"),
        "shared_rank_types": enum_of("SHARED_RANK_TYPE"),
        "review_status": enum_of("REVIEW_STATUS"),
        "rank_keys": dict(FALLBACK["RANK_KEYS"]),
        "libraries": libs,
        "loaded": loaded,
        "downloaded_combo_ids": downloaded,
        "tags": tags,
        "record_fields": dict(COMBO_FIELDS),
        "combo_keys_const": combo_key_map(),
        "transport": {
            "positive_read": "part.get_library(library_type) / get_composite_by_idx(library_type, idx)",
            "session_payload": "get_recover_info() -> {'local_library': [...], 'shared_library': [...]}",
            "inbound_apply": "apply_async_ready_data(data) / apply_recover_info(data)",
            "rpc_channel": "core.call_server(<iUGCEditCompositeLibraryPart>, ...)",
            "rpc_reply_shape": "{'data': [record, ...]}",
            "entry_ui": "UIBattleUGCEditCompositeLibraryPart.request_open_resource_hall(combo_id)",
        },
        "note": "资源大厅内容 = _shared_library（官方 PGC + 玩家分享）；"
                "_local_library 是我保存/下载的组合。两者随编辑器会话由服务器下发。"
                "本接口只读，不触发下载/上传/分享。",
    }


def op_hall_items(edit, args):
    """枚举资源大厅里的组合记录。"""
    core, part = get_part(edit)
    lt = enum_of("LIBRARY_TYPE")
    which = args.get("library") or "all"
    if which not in ("all", "local", "shared"):
        raise ValueError("library 必须是 all / local / shared")
    tag_filter = args.get("tag")
    tag_filter = str(tag_filter) if tag_filter is not None else None
    kw = (args.get("query") or "").strip().lower()
    official_only = bool(args.get("official_only"))
    limit = args.get("limit")
    limit = 200 if limit is None else int(limit)
    if limit < 0:
        raise ValueError("limit 不能为负；0 表示全部")
    offset = int(args.get("offset") or 0)

    targets = []
    if which in ("all", "shared"):
        targets.append(("shared", lt.get("SHARED", 2)))
    if which in ("all", "local"):
        targets.append(("local", lt.get("LOCAL", 1)))

    rows = []
    for label, t in targets:
        try:
            lib = part.get_library(t)
        except Exception as e:
            rows.append({"library": label, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        for rec in lib:
            s = _combo_summary(edit, rec, label)
            if official_only and not s.get("is_official_pgc"):
                continue
            if tag_filter is not None and str(s.get("tag")) != tag_filter:
                continue
            if kw:
                blob = " ".join(str(x) for x in (
                    s.get("combo_id"), s.get("name"), s.get("name_cn"), s.get("desc_cn"),
                    (s.get("author") or {}).get("name"), s.get("tag_cn"),
                    s.get("share_code"), s.get("child_count"))).lower()
                if kw not in blob:
                    continue
            rows.append(s)

    total = len(rows)
    page = rows if limit == 0 else rows[offset:offset + limit]
    return {
        "total": total,
        "offset": offset,
        "returned": len(page),
        "official_count": sum(1 for r in rows if r.get("is_official_pgc")),
        "items": page,
        "applied_filters": {"library": which, "tag": tag_filter,
                            "official_only": official_only, "query": kw or None, "limit": limit},
        "note": "官方组合当前以名称前缀 TID_UGC_PGC_ 识别；"
                "玩家自制来自 _shared_library / _local_library。"
                "用 fuxiang_resource_hall_item 取单条的组合本体（combo_data）。",
    }


def op_hall_item(edit, args):
    """按 combo_id 或分享码取单条组合的完整信息（含 combo_data 拆解）。"""
    core, part = get_part(edit)
    lt = enum_of("LIBRARY_TYPE")
    combo_id = args.get("combo_id")
    share_code = args.get("share_code")
    if combo_id is None and share_code is None:
        raise ValueError("需要 combo_id 或 share_code")
    combo_id = str(combo_id) if combo_id is not None else None

    hit = None
    for label, t in (("local", lt.get("LOCAL", 1)), ("shared", lt.get("SHARED", 2))):
        try:
            lib = part.get_library(t)
        except Exception:
            continue
        for rec in lib:
            if not hasattr(rec, "get"):
                continue
            if combo_id is not None and str(rec.get("_id")) == combo_id:
                hit = (label, rec)
                break
            if share_code is not None and rec.get("combo_share_code") == share_code:
                hit = (label, rec)
                break
        if hit:
            break
    if hit is None:
        # 兜底 2：查列表页 UI 的当前页签卡片（未下载的组合只有卡片元数据）
        inst = hall_ui(edit)
        if inst is not None:
            cards = getattr(inst, "_cur_cards", None) or []
            for c in cards:
                if not hasattr(c, "get"):
                    continue
                cid = str(c.get("combo_id"))
                if (combo_id is not None and cid == combo_id) or \
                        (share_code is not None and c.get("combo_share_code") == share_code):
                    row = _card_row(edit, c, True)
                    cat_map = enum_of("HALL_CATEGORY")
                    inv = {v: k for k, v in cat_map.items()}
                    return {
                        "found": True,
                        "source": "resource_hall_ui._cur_cards",
                        "library": inv.get(getattr(inst, "_category", None)),
                        "combo_data_available": False,
                        "combo_data_hint": "该组合尚未下载，客户端只持有卡片元数据（full_doc）；"
                                           "组合本体 combo_data 只在下载/使用后落到 "
                                           "UGCEditCompositeLibraryPart，届时可再用本接口读取。",
                        **row,
                    }
        # 兜底 3：按索引试一遍 part 层
        return {"found": False,
                "lookup": {"combo_id": combo_id, "share_code": share_code},
                "counts": {k: len(part.get_library(v) or []) for k, v in lt.items()},
                "reason": "part 层（我的/已下载）与大厅当前页签里都没有该组合，"
                          "或其所在页签当前未加载。"}
    label, rec = hit
    out = _combo_detail(edit, rec, label, bool(args.get("with_param_detail")))
    out["found"] = True
    out["source"] = "part._%s_library" % label
    # combo_data 只在「已下载/已使用」后才到客户端；part 层记录也可能只剩元数据
    out["combo_data_available"] = bool(out.get("combo_data_parts"))
    if not out["combo_data_available"]:
        out["combo_data_hint"] = ("该记录当前只有元数据（无 combo_data）；"
                                  "组合本体需在下载/使用后才下发到客户端。")
    out["is_downloaded"] = str(rec.get("_id")) in {
        str(r.get("_id")) for r in (part.get_library(lt.get("LOCAL", 1)) or [])
        if hasattr(r, "get")}
    try:
        out["review_status"] = part.get_combo_review_status(str(rec.get("_id")))
    except Exception:
        out["review_status"] = None
    return out


def op_hall_verify(edit, args):
    """自检：记录字段完整性、combo_data 可解析性、库状态。"""
    core, part = get_part(edit)
    lt = enum_of("LIBRARY_TYPE")
    keys = combo_key_map()
    known = set(keys.values()) or set(COMBO_FIELDS)

    checked, missing, parse_fail, official, player = 0, [], [], 0, 0
    metadata_only = []
    per_lib = {}
    for label, t in lt.items():
        try:
            lib = list(part.get_library(t))
        except Exception as e:
            per_lib[label] = {"error": "%s: %s" % (type(e).__name__, e)}
            continue
        per_lib[label] = {"count": len(lib)}
        for idx, rec in enumerate(lib):
            checked += 1
            if not hasattr(rec, "get"):
                parse_fail.append({"library": label, "index": idx, "reason": "记录不是映射"})
                continue
            got = {str(k) for k in rec.keys()}
            lack = sorted(known - got)
            if lack:
                missing.append({"library": label, "combo_id": str(rec.get("_id")),
                                "absent_optional_fields": lack})
            cd = rec.get("combo_data")
            if not hasattr(cd, "keys"):
                # combo_data 只在「已下载/已使用」后才落到客户端；
                # 大厅刷新后 part 层记录可能只剩元数据 —— 这是**合法状态**，
                # 不是结构损坏，因此单独计数而不是判为解析失败。
                metadata_only.append({"library": label, "combo_id": str(rec.get("_id")),
                                      "reason": "无 combo_data：该条目尚未下载/组合本体未下发"})
            elif str(rec.get("name") or "").startswith("TID_UGC_PGC"):
                official += 1
            else:
                player += 1

    return {
        "libraries": per_lib,
        "records_checked": checked,
        "official_pgc": official,
        "player_made": player,
        "metadata_only_records": metadata_only[:20],
        "metadata_only_count": len(metadata_only),
        "records_missing_schema_fields": missing[:20],
        "records_missing_schema_field_count": len(missing),
        "parse_failures": parse_fail[:20],
        "parse_failure_count": len(parse_fail),
        "combo_schema_field_count": len(known),
        "loaded": {"local": bool(getattr(part, "_local_loaded", False)),
                   "shared": bool(getattr(part, "_shared_loaded", False))},
        "complete": not parse_fail and bool(per_lib),
        "note": "schema 缺失字段是**允许的**：服务端按需下发，例如 like_cnt / score / "
                "limited_* 只出现在排行榜条目里。metadata_only 表示记录只有元数据、"
                "没有组合本体（未下载），同样是合法状态。因此 complete 只表示「结构可解析」。"
                "库为空通常意味着尚未在游戏内打开过资源大厅。",
    }


# ---------------------------------------------------------------------------
# 列表页（UIBattleUGCResourceHall）—— 用户实际看到的资源
# ---------------------------------------------------------------------------
# 资源大厅打开后，真正的资源清单在列表页 UI 上：
#   _cur_cards        list[dict]  当前分类/排序/标签下的卡片（权威展示集）
#   _category         int         当前页签（UGC_COMPOSITE_HALL_CATEGORY）
#   _cur_sort         str         当前排序（UGC_COMPOSITE_HALL_SORT）
#   _cur_tag_id       int         当前标签（DM.ugc_combo_tag）
#   _top_tab_clones   3 个页签节点；_sort_node_map 排序节点；_tag_node_map 标签节点
# 每张卡片：{combo_id, name(已是中文), cover_url, combo_share_code, occupy_num,
#            is_recommend, is_downloaded, is_selected, multi_select_mode,
#            full_doc: 完整组合记录（UGC_COMBO_KEY 子集）}
# 加载路径：OFFICIAL/SHARED 走 _async_load_rank（排行榜，异步生成器）；
#          MINE 走 _load_mine_recent → part.request_mine_recent(typ)。
# 本模块只读取已渲染的卡片，不触发任何加载或切换。


def hall_ui(edit):
    """取资源大厅列表页实例；未打开返回 None。"""
    try:
        G = _globals(edit)
        gui = getattr(G.get("GK"), "g_ui_mgr", None)
        ud = vars(gui).get("ui_dict") if gui is not None else None
        if not isinstance(ud, dict):
            return None
        return ud.get(UI_HALL_KEY)
    except Exception:
        return None


def _card_row(edit, card, with_doc=False):
    if not hasattr(card, "get"):
        return {"error": "卡片结构异常", "repr": repr(card)[:120]}
    fd = card.get("full_doc") or {}
    fd = fd if hasattr(fd, "get") else {}
    au = fd.get("author") or {}
    au = au if hasattr(au, "get") else {}
    tag = fd.get("tag")
    row = {
        "combo_id": str(card.get("combo_id")),
        "name": card.get("name"),
        "share_code": card.get("combo_share_code"),
        "occupy_num": card.get("occupy_num"),
        "is_recommend": bool(card.get("is_recommend")),
        "is_downloaded": bool(card.get("is_downloaded")),
        "is_selected": bool(card.get("is_selected")),
        "cover_url": card.get("cover_url"),
        "author": {"uid": str(au.get("uid")) if au.get("uid") is not None else None,
                   "name": au.get("name")},
        "tag": tag,
        "tag_cn": None,
        "dl_cnt": fd.get("dl_cnt"),
        "like_cnt": fd.get("like_cnt"),
        "score": fd.get("score"),
        "gm_ver": fd.get("gm_ver"),
        "permission": str(fd.get("combo_permission")) if fd.get("combo_permission") is not None else None,
        "create_ts": fd.get("create_ts"),
        "publish_ts": fd.get("publish_ts"),
        "official_name_tid": fd.get("name"),
        "desc_tid": fd.get("desc"),
    }
    try:
        tcfg = _dm(edit).ugc_combo_tag.get(tag)
        if tcfg is not None:
            row["tag_cn"] = tid_text(edit, tcfg.get("tag_tid"))
    except Exception:
        pass
    if with_doc:
        row["full_doc"] = _plain(fd)
    return row


def op_hall_ui(edit, args):
    """读取资源大厅列表页的实况状态与卡片清单。"""
    inst = hall_ui(edit)
    if inst is None:
        return {
            "open": False,
            "reason": "资源大厅未打开。请在游戏内打开「资源大厅」后重试；"
                      "卡片清单只存在于该 UI 打开期间。",
            "ui_class": UI_HALL_KEY,
        }
    state = {
        "category": getattr(inst, "_category", None),
        "sort": getattr(inst, "_cur_sort", None),
        "tag_id": getattr(inst, "_cur_tag_id", None),
        "cur_combo_id": getattr(inst, "_cur_combo_id", None),
        "search_keyword": getattr(inst, "_search_keyword", None),
        "is_manage_mode": bool(getattr(inst, "_is_manage_mode", None)),
        "pick_mode": bool(getattr(inst, "_pick_mode", None)),
        "mine_loaded_typ": getattr(inst, "_mine_loaded_typ", None),
        "mine_pending_typ": getattr(inst, "_mine_pending_typ", None),
        "saved_filter": None,
        "top_tab_count": len(getattr(inst, "_top_tab_clones", None) or []),
    }
    try:
        state["saved_filter"] = _plain(inst._get_saved_filter())
    except Exception:
        pass

    cat_map = enum_of("HALL_CATEGORY")
    inv_cat = {v: k for k, v in cat_map.items()}
    state["category_name"] = inv_cat.get(state["category"])
    sort_map = enum_of("HALL_SORT")
    inv_sort = {str(v): k for k, v in sort_map.items()}
    state["sort_name"] = inv_sort.get(str(state["sort"]))

    taxonomy = {}
    try:
        taxonomy["categories"] = [
            dict(_plain(c), name_cn=tid_text(edit, c.get("tab_tid")))
            for c in (inst._get_category_group() or [])]
    except Exception as e:
        taxonomy["categories_err"] = "%s: %s" % (type(e).__name__, e)
    sorts = {}
    for label, t in cat_map.items():
        try:
            raw = inst._get_sort_group(t) or []
        except Exception as e:
            sorts[label] = "%s: %s" % (type(e).__name__, e)
            continue
        rows = []
        for s in raw:
            p = _plain(s)
            if isinstance(p, dict):
                rows.append(p)
            elif isinstance(p, (list, tuple)) and len(p) == 2:
                rows.append({"value": p[0], "tid": p[1]})
            else:
                rows.append({"value": p})
        sorts[label] = rows
    taxonomy["sorts_by_category"] = sorts
    try:
        taxonomy["tags"] = [dict(_plain(t), name_cn=tid_text(edit, t.get("tag_tid")))
                            for t in (inst._get_tag_list() or [])]
    except Exception as e:
        taxonomy["tags_err"] = "%s: %s" % (type(e).__name__, e)

    cards = list(getattr(inst, "_cur_cards", None) or [])
    rows = []
    if args.get("include_cards"):
        rows = [_card_row(edit, c, bool(args.get("with_full_doc"))) for c in cards]

    return {
        "open": True,
        "ui_class": UI_HALL_KEY,
        "state": state,
        "taxonomy": taxonomy,
        "card_count": len(cards),
        "cards": rows,
        "note": "cards 是当前页签/排序/标签下**已渲染**的卡片。"
                "切换页签后重新调用即可读取该页签的清单。"
                "完整资源记录见 fuxiang_resource_hall_cards(with_full_doc=true)。",
    }


def op_hall_cards(edit, args):
    """枚举资源大厅当前页签下的卡片（资源清单）。"""
    inst = hall_ui(edit)
    if inst is None:
        return {"open": False, "total": 0, "cards": [],
                "reason": "资源大厅未打开。请在游戏内打开「资源大厅」后重试。"}
    cards = list(getattr(inst, "_cur_cards", None) or [])
    tag_filter = args.get("tag")
    tag_filter = str(tag_filter) if tag_filter is not None else None
    kw = (args.get("query") or "").strip().lower()
    only_rec = bool(args.get("recommend_only"))
    only_dl = bool(args.get("downloaded_only"))
    with_doc = bool(args.get("with_full_doc"))
    limit = args.get("limit")
    limit = 200 if limit is None else int(limit)
    if limit < 0:
        raise ValueError("limit 不能为负；0 表示全部")
    offset = int(args.get("offset") or 0)

    rows = []
    for c in cards:
        row = _card_row(edit, c, with_doc)
        if tag_filter is not None and str(row.get("tag")) != tag_filter:
            continue
        if only_rec and not row.get("is_recommend"):
            continue
        if only_dl and not row.get("is_downloaded"):
            continue
        if kw:
            blob = " ".join(str(x) for x in (
                row.get("combo_id"), row.get("name"), row.get("share_code"),
                (row.get("author") or {}).get("name"), row.get("tag_cn"))).lower()
            if kw not in blob:
                continue
        rows.append(row)

    total = len(rows)
    page = rows if limit == 0 else rows[offset:offset + limit]
    cat_map = enum_of("HALL_CATEGORY")
    inv_cat = {v: k for k, v in cat_map.items()}
    return {
        "open": True,
        "category": getattr(inst, "_category", None),
        "category_name": inv_cat.get(getattr(inst, "_category", None)),
        "sort": getattr(inst, "_cur_sort", None),
        "tag_id": getattr(inst, "_cur_tag_id", None),
        "source_card_count": len(cards),
        "total": total,
        "offset": offset,
        "returned": len(page),
        "official_count": None,
        "cards": page,
        "applied_filters": {"tag": tag_filter, "recommend_only": only_rec,
                            "downloaded_only": only_dl, "query": kw or None, "limit": limit},
        "note": "这是资源大厅**当前页签**的清单。官方页签（20001）实测 376 条；"
                "切换到玩家分享/我的后重新调用可读对应清单。",
    }


def op_hall_export(edit, args):
    """把当前页签的卡片清单导出为 JSON 快照到 registry/。"""
    import json
    import os
    inst = hall_ui(edit)
    if inst is None:
        return {"open": False, "reason": "资源大厅未打开，无快照可导出。"}
    cards = list(getattr(inst, "_cur_cards", None) or [])
    rows = [_card_row(edit, c, True) for c in cards]
    cat = getattr(inst, "_category", None)
    cat_map = enum_of("HALL_CATEGORY")
    inv = {v: k for k, v in cat_map.items()}
    payload = {
        "category": cat,
        "category_name": inv.get(cat),
        "sort": getattr(inst, "_cur_sort", None),
        "tag_id": getattr(inst, "_cur_tag_id", None),
        "count": len(rows),
        "cards": rows,
    }
    root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "registry"))
    if not os.path.isdir(root):
        os.makedirs(root)
    name = args.get("filename") or ("resource_hall_%s.json" % (inv.get(cat) or cat))
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return {"exported": path, "bytes": os.path.getsize(path),
            "category": cat, "category_name": inv.get(cat), "count": len(rows)}
