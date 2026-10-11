#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot_restore — 把快照**重建**到一张目标图里（L2）。

⚠️ 这是**写操作**。默认 dry-run（只出计划），确认后才 `--apply`。

流程
----
  1. place   摆物件（item_id + 位置 + 旋转 + 缩放），逐件 poll，建立 旧ugc_id→新ugc_id 映射
  2. props   整袋回写 component_info（含运动器绑定字段的 id 重映射）
  3. events  重建事件：create 事件键 + 按 (ugc_id, property_id, index, params_idx) 绑定
  4. story   剧情导入（异步创建链，默认关闭，需 --with-story）
  5. save    分批保存 + save_dirty 校验

用法
----
  python cli/snapshot.py restore <快照.json>                  # 计划（dry-run）
  python cli/snapshot.py restore <快照.json> --apply --limit 20
  python cli/snapshot.py restore <快照.json> --apply --no-props
  python cli/snapshot.py restore <快照.json> --apply          # 再跑一次 = 断点续做

设计要点（都是踩过的坑，别改回去）
--------------------------------
* **默认 dry-run**：写地图不可逆。计划里会把"跳过什么、为什么"讲清楚。
* **每批摆完立即保存**：`save_map` 会摘除未进入本次提交的对象（重进重复件）。
  连续保存前先 `save_map_poll {"timeout":0.01}` 释放在途护栏，否则第 2 次起被静默拒绝。
* **ugc_id 必须重映射**：重建后 id 全变。事件引用、运动器绑定字段
  （10000118701 / 10000018701）都要换成新 id，否则绑到不存在/错误的对象上。
* **进度 jsonl 断点续做**：`request_id` 由 (run_id, index) 稳定派生 → 重跑幂等。
* **判据是 save_dirty 的脏缓冲归零**，不是 save_map_poll 的 ack（实测 60s 收不到）。
"""
import argparse
import ast
import hashlib
import json
import math
import os
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxlong import call_edit          # noqa: E402  # 不删回执，避免撞 safe-delete 阈值
from fxapi import find_layer as _find_layer  # noqa: E402
from snapshot import pending_deletions_present  # noqa: E402  # 与 summarize 同一条空桶口径

BAD = ("ambiguous", "conflict", "finalize_failed", "send_unknown")
POLL_TRIES = 40
SAVE_DRAIN_TRIES = 30
DEFAULT_PROGRESS_DIR = os.path.join(ROOT, "builds", "restore")

# 默认跳过：这几类不是"摆出来就有意义"的东西
SKIP_STORY_TYPES = {10007001, 10007002, 10007003}   # 走 story 阶段重建
# UI_NATIVE（item 6000001「技能控件」这类）默认不摆。⚠️ 原因**不是**"配置复杂/摆不出来"：
# 2026-10-01 用只读 `plan_item` 查过，阀门是放行的（`server_place_supported=true`、
# `server_begin_allowed=true`、无 block reason），而 200001 早就在 `VERIFIED_TYPES` 里。
# 真正的状态是**摆放通道开放、落库未证**（`persistence_verified=false`）——
# 顶格图加这 48 件预算还够（14933 ≤ 15000，见 probe/_rehearsal_ui.txt），
# 所以要 1:1 得用 `--include-ui` 试点 + 重进回读，而不是当成做不了。
# ⚠️ 但"目标图缺这 48 件"只在**那张图被改过控件**时才成立：2026-10-01 实机核对，
# 同一账号新开的空白图自带 48 件控件，与春岚那 48 件按属性袋+位置**逐件相同**
# ⇒ 对着空白图催 `--include-ui` 会插一层重复件。净缺口由 `stock_for_stages` 判。
SKIP_UI_TYPES = {200001}
SKIP_MOTION_TYPES = {203912}                        # 运动点，依附运动器，单独摆无意义
# 地图骨架自带、不该由快照重建的东西（新建地图时服务端已给一套）
SKIP_SKELETON_TYPES = {
    201009,     # ANCHOR 区域锚点（边界四角）
    203930,     # COORDINATE_OFFSET_EDITOR 编辑器坐标偏移
}
# 全局单例的编号段：GLOBAL/FOG/LUT/INSTALL/GAME_TIME/COUNTDOWN/SKYBOX 都是 1000xxxx，
# 而实体与道具类是 6 位（203003 / 101001 / 200001）。用这个分界比逐个枚举稳。
GLOBAL_TYPE_FLOOR = 10000000
SENTINEL_Y = -65536.0                               # 逻辑组件的位置哨兵
# 一次创建产出多件的类型：{component_type: 件数}。真源在 `bridge/fuxiang_scene.py`
# 的 GROUP_CREATE_TYPES（那边写着实机取证），这里只放还原侧需要的同一份事实，
# 由 tests/test_snapshot_restore.py 用 AST 比对两处，不许漂移。
GROUP_CREATE_TYPES = {204041: 2}


# ---------------------------------------------------------------------------
# 纯逻辑（可离线单测）
# ---------------------------------------------------------------------------
def idmap_path(progress_path, snapshot_path):
    """这次 run 的 id 映射落盘位置 —— 跟着 progress 文件走，不按快照名共用一个。

    2026-10-01 踩坑：几次 `--progress builds/restore/pilot_*.jsonl` 的试点把各自的
    idmap 全写进同一个 `idmap_<快照名>.json`，于是事后 `--verify-only` 读到的是
    "只有试点那几件"的映射（回对结论全错），全量跑之前还可能被它误导。
    """
    base = os.path.splitext(os.path.basename(progress_path or ""))[0]
    if base.endswith(".progress"):
        base = base[:-len(".progress")]
    if not base:
        base = os.path.splitext(os.path.basename(snapshot_path))[0]
    return os.path.join(os.path.dirname(progress_path) or DEFAULT_PROGRESS_DIR,
                        "idmap_%s.json" % base)


def _unwrap(node, key):
    return _find_layer(node, key)


def _info_of(comp):
    ci = comp.get("ci")
    if isinstance(ci, str):
        try:
            return json.loads(ci)
        except Exception:
            return {}
    return ci or {}


def component_id_of(comp):
    """组件类型：优先 component_info 里的 component_id。"""
    ci = _info_of(comp)
    cid = ci.get("component_id")
    if cid is None:
        return comp.get("i")
    try:
        return int(cid)
    except (TypeError, ValueError):
        return cid


def has_known_position(comp):
    """这件有没有"能落位的坐标" —— 两种坐标都不在 `get_position3()` 上。

    * 逻辑件（交互区 203022）：真值在单元 `position`，导出另记成 `pp`；
    * 界面控件（UI_CUSTOM 200000）：真值在属性袋 POS（画布 2D），
      全图控件**共用一个** UgcGlobalUnit，单元的 `get_position3()` 恒是哨兵。
    只按 `p` 判会把它们当"无坐标逻辑件"整批跳过（2026-10-01 实机：控件就是这么
    被 `sentinel_position` 吃掉、可摆数直接归零的）⇒ 还原出来是缺触发区缺控件的图。
    """
    if has_real_position(comp):
        return True
    return (_as_int(component_id_of(comp)) == UI_CUSTOM_TYPE
            and ui_canvas_position(comp) is not None)


def classify_components(components, placeable_types=None, include_ui=False,
                        include_motion=False, allow_sentinel=False,
                        include_global=False, include_not_placeable=False):
    """把快照组件分成「可摆」与「跳过（含原因）」两类。

    placeable_types：可选的 {component_type: True} 白名单（来自 `list_types`
    的 place_support == 'verified'）；给 None 时跳过这项检查。
    include_not_placeable：放行白名单外的类型（走 `allow_unverified`），
    给的是「角色 / 推荐检查点」这类**阀门还没实测记录但服务端受理**的件——
    放行只是让试点能跑，落库成不成仍要重进回对。
    """
    to_place, skipped = [], []
    for c in components or []:
        cid = component_id_of(c)
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            cid_int = None
        reason = None
        if cid_int in SKIP_STORY_TYPES:
            reason = "story_stage（由 --with-story 阶段重建）"
        elif cid_int in SKIP_UI_TYPES and not include_ui:
            reason = "ui（UI_NATIVE 通道开放但落库未证，要 1:1 用 --include-ui 试点）"
        elif cid_int in SKIP_MOTION_TYPES and not include_motion:
            reason = "motion_point（每张图自带 50 个运动点，不是作者摆的内容 ⇒ 默认不摆）"
        elif cid_int in SKIP_SKELETON_TYPES and not include_global:
            reason = "skeleton（地图骨架自带，需 --include-global）"
        elif (cid_int is not None and cid_int >= GLOBAL_TYPE_FLOOR
              and not include_global):
            reason = "global_singleton（全局单例，地图自带，需 --include-global）"
        elif (placeable_types is not None and cid_int not in placeable_types
              and not include_not_placeable):
            reason = "not_placeable（该类型 place_support≠verified）"
        elif c.get("del"):
            # 撤销栈残骸：删掉的组件仍留在 cm._ugc_id2component 里（Ctrl+Z 依赖它），
            # 重进才消失。它们**不是地图内容**，摆出去就是凭空多一件幽灵件。
            reason = "deleted_residue（删除后仍在索引里的残骸，重进即消失）"
        else:
            p = c.get("p") or [0, 0, 0]
            if (len(p) > 1 and float(p[1]) <= SENTINEL_Y and not allow_sentinel
                    and not has_known_position(c)):
                reason = "sentinel_position（逻辑组件无真实坐标，需 --allow-sentinel）"
        if reason:
            skipped.append({"g": c.get("g"), "i": c.get("i"), "component_type": cid,
                            "reason": reason})
        else:
            to_place.append(c)
    return to_place, skipped


def has_real_position(comp):
    """快照是否另外记到了单元上的真实坐标（`pp`）。

    `get_position3()` 对部分逻辑件返回哨兵 `[0,-65536,0]`，但单元 `position`
    上是真坐标（实机：春岚的 203022 交互区 = 触发「点击播放」的那件，
    真实位置 [928, -67.923, -147]）。这种件不是"无坐标的逻辑件"，
    既不该被 sentinel_position 跳过，也不该被兜底摆到 y=0。
    """
    pp = comp.get("pp")
    if not isinstance(pp, (list, tuple)) or len(pp) < 3:
        return False
    try:
        return float(pp[1]) > SENTINEL_Y
    except (TypeError, ValueError):
        return False


UI_CUSTOM_TYPE = 200000                    # 界面控件（UGCUiCustomComponent）
UI_POS_PROPERTY = 20000010001              # 画布 2D 落点 POS


def ui_canvas_position(comp):
    """界面控件（UI_CUSTOM）的落点是**画布 2D 坐标**，存在属性袋 POS 槽里。

    控件的身份与坐标都不在单元上 —— 全图控件共用**一个** `UgcGlobalUnit`
    （`UI_EDIT.md` §0），所以快照的 `p`/`r`/`s` 对它们没有意义。摆放若照用 `p`，
    整批控件会先叠在同一个点，之后才靠属性回写挪回去 —— 中间那次原生确认
    （`on_check_ui_component_limit` 的占用/越界弹窗判据）看到的却是重叠堆，判据失真。
    """
    for e in (_info_of(comp).get("events") or []):
        if not isinstance(e, dict) or str(e.get("type")) != str(UI_POS_PROPERTY):
            continue
        info = e.get("info")
        if isinstance(info, (list, tuple)) and len(info) >= 2:
            try:
                return [float(info[0]), float(info[1]), 0.0]
            except (TypeError, ValueError):
                return None
    return None


def check_ui_view(specs, ui_state):
    """预检：要摆控件时，界面编辑器视图开没开（`ui_state.view_open`）。

    视图关着时原生确认会退回通用登记路径（历史上 200000 的图标类件就是这么验过的，
    画布坐标随属性袋落库），不是"做不了"，但也**不是玩家手放那条路** ⇒ 报 warn
    让人自己选，别默默走完再声称一致。
    """
    n = sum(1 for s in (specs or [])
            if _as_int(s.get("component_type")) == UI_CUSTOM_TYPE)
    if not n:
        return {"level": "ok", "widgets": 0, "msg": "本次无界面控件（UI_CUSTOM）"}
    if not isinstance(ui_state, dict):
        return {"level": "warn", "widgets": n,
                "msg": "⚠️ 要摆 %d 个界面控件，但读不到 ui_state（视图开没开未知）" % n}
    if ui_state.get("view_open"):
        return {"level": "ok", "widgets": n,
                "msg": "界面编辑器视图已开，%d 个控件走原生确认" % n}
    return {"level": "warn", "widgets": n,
            "msg": ("⚠️ 要摆 %d 个界面控件，但界面编辑器视图没开 ⇒ 原生 "
                    "on_check_ui_component_limit 用不了，退回通用登记路径"
                    "（画布坐标仍随属性袋 POS 落库）。要贴玩家手放那条路，"
                    "请先在游戏里打开界面编辑器再跑。") % n}


def fetch_ui_state():
    try:
        d = call_edit("ui_state", {}, timeout=60) or {}
        return _unwrap(d, "view_open") or _find_layer(d, "view_open") or None
    except Exception:
        return None


def place_spec(comp):
    """快照组件 → 摆放参数。位置优先用 `pp`（单元上的真实坐标）。"""
    p = comp.get("pp") if has_real_position(comp) else None
    if not p:
        p = comp.get("p") or [0, 0, 0]
    if _as_int(component_id_of(comp)) == UI_CUSTOM_TYPE:
        p = ui_canvas_position(comp) or p          # 控件的落点在画布，不在单元
    if len(p) > 1 and float(p[1]) <= SENTINEL_Y:     # --allow-sentinel 时兜底
        p = [p[0], 0.0, p[2] if len(p) > 2 else 0.0]
    return {
        "item_id": comp.get("i"),
        "position": [float(v) for v in p],
        "rotation": [float(v) for v in (comp.get("r") or (0, 0, 0))],
        "scale": [float(v) for v in (comp.get("s") or (1, 1, 1))],
        "mode": "server",
        "component_type": component_id_of(comp),
        "old_g": comp.get("g"),
    }


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def collapse_group_creates(specs, comps_by_gid):
    """把「一次创建出多件」的快照件折叠成**一个**摆放请求（见 `GROUP_CREATE_TYPES`）。

    实机 2026-10-01：`add_unit(item 204041)` 一次产出 2 个 `UgcLogicUnit`，引擎自动把两件的
    `component_info['group_components']` 写成互指 ⇒ 快照里那一对是**一次创建的产物**。
    各发一次请求会摆出 4 件（翻倍），所以其余件并到主件上：
    `group_old_gs` 记成员旧 id（按 gid 升序）、`group_transforms` 记成员各自该落的变换。
    谁当主件不影响最终形状：成员只是按顺序各拿一份变换，属性回写按 id 映射逐件走。
    """
    out = []
    taken = set()
    by_g = {s["old_g"]: s for s in specs}
    for s in specs:
        if s["old_g"] in taken:
            continue
        ct = _as_int(s.get("component_type"))
        size = GROUP_CREATE_TYPES.get(ct)
        if not size or size < 2:
            out.append(s)
            taken.add(s["old_g"])
            continue
        # 成员 = 本件 + 「属性袋里 group_components 指回本件」的同类型计划内件
        pool = [s["old_g"]]
        for other in by_g.values():
            if other["old_g"] in pool or other["old_g"] in taken:
                continue
            if _as_int(other.get("component_type")) != ct:
                continue
            ci = _info_of(comps_by_gid.get(other["old_g"]) or {})
            if s["old_g"] in (ci.get("group_components") or []):
                pool.append(other["old_g"])
        pool = sorted(pool)[:size]
        leader = dict(by_g[pool[0]])
        members = pool[1:]
        if members:      # 没有伙伴的单件不加 group_* 键（别凭空等一个不来的成员）
            leader["group_old_gs"] = members
            leader["group_transforms"] = [
                {"position": list(by_g[m]["position"]), "rotation": list(by_g[m]["rotation"]),
                 "scale": list(by_g[m]["scale"])} for m in members]
        taken.update(pool)
        out.append(leader)
    return out


def parse_current_value(s):
    """事件引用里的 current_value 是 Python repr，还原成真实值。

    "['踏入灯下', 1286]" → ['踏入灯下', 1286]；失败返回 None（交给调用方跳过）。
    """
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return None


# 单元参数表不变量：params[0..2] = 变换三元组，params[3:] = 组件自己的事件袋。
# ⇒ 事件引用的 index 是单元级下标，组件本地下标 = index - PARAMS_HEADER。
# 与 bridge/fuxiang_edit_ops.PARAMS_HEADER 必须一致（那边是权威定义）。
PARAMS_HEADER = 3


def _info_index(snap):
    """{ugc_id: 快照组件} 索引。"""
    return {c.get("g"): c for c in (snap.get("components") or [])}


def own_slot_info(comp_index, ugc_id, index):
    """从**快照组件自身**的事件袋取槽位 info（权威值）。返回 (info, ok)。

    ⛔ 为什么不信引用里的 `current_value`：它由 `event_list` 解析而来，而 2026-09-23
    之前的版本是从**单元级** params 读的 —— 多个组件共用同一 unit 时（实测 4 个 CHOICE
    共用 unit 1000306）读到的是**主组件**的值。旧快照里因此存了错值
    （如「选项1」的槽位被记成 `['拆开信件']`）。照抄它回写会把槽位写坏。
    组件自身的 `ci.events[index-3].info` 才是权威值。
    """
    c = (comp_index or {}).get(ugc_id)
    if not c:
        return None, False
    own = _info_of(c).get("events") or []
    if index is None:
        return None, False
    oi = int(index) - PARAMS_HEADER
    if 0 <= oi < len(own):
        e = own[oi]
        if isinstance(e, dict):
            return e.get("info"), True
    return None, False


def event_bind_plan(events, id_map, only_old=None, comp_index=None):
    """事件表 → 绑定动作列表（旧 ugc_id 换成新 id）。

    返回 (actions, dangling)：dangling 是"引用的对象没被重建"的引用，无法绑定。

    `only_old` 给定时只处理 `ugc_id` 在该集合里的引用 —— 用于**第二遍补绑**：
    剧情节点在第一遍时还不在 id_map 里（悬空），剧情重建拿到新 id 后只需补绑这批。

    `comp_index` 给定时（= `_info_index(snap)`），槽位值从**组件自身事件袋**取，
    而不是引用里那个可能被污染的 `current_value`。
    """
    actions, dangling = [], []
    for e in ((events or {}).get("events") or []):
        key = e.get("event_key")
        for r in e.get("refs") or []:
            old = r.get("ugc_id")
            if only_old is not None and old not in only_old:
                continue
            new = id_map.get(old)
            if new is None:
                dangling.append({"event_key": key, "old_ugc_id": old,
                                 "property_id": r.get("property_id")})
                continue
            info, info_ok = own_slot_info(comp_index, old, r.get("index")) \
                if comp_index is not None else (None, False)
            src = "component_bag" if info_ok else "current_value"
            if not info_ok:
                info = parse_current_value(r.get("current_value"))
            if info is None:
                # 取不到整段就交给 event_bind 按字段权威形态初始化（别硬塞错值）
                src = "field_default"
            actions.append({
                "event_key": key,
                "ugc_id": new,
                "property_id": r.get("property_id"),
                "index": r.get("index"),
                "params_idx": r.get("params_idx"),
                "info": info,
                "info_source": src,
                "old_ugc_id": old,
            })
    return actions, dangling


def dangling_old_ids(events, id_map):
    """第一遍绑不了、指向未重建对象的旧 ugc_id 集合。"""
    _actions, dangling = event_bind_plan(events, id_map)
    return {d["old_ugc_id"] for d in dangling if d.get("old_ugc_id") is not None}


def merge_story_id_map(id_map, story_id_map):
    """把剧情阶段的 旧id→新id 并进总映射。

    返回 {merged, added, conflicts}：`conflicts` 是两处给出不同新 id 的旧 id
    （正常不该出现；出现说明同一个旧 id 被两套机制各自重建过，必须人工看一眼）。
    """
    added, conflicts = {}, {}
    for old, new in (story_id_map or {}).items():
        try:
            old_i = int(old)
        except (TypeError, ValueError):
            old_i = old
        cur = id_map.get(old_i)
        if cur is None:
            id_map[old_i] = new
            added[old_i] = new
        elif cur != new:
            conflicts[old_i] = {"existing": cur, "story": new}
    return {"merged": len(added), "added": added, "conflicts": conflicts}


# ---------------------------------------------------------------------------
# 跨图预检（纯逻辑，可离线单测）
# ---------------------------------------------------------------------------
def bbox_of(specs):
    """摆放清单的世界包围盒。"""
    if not specs:
        return None
    xs = [float(s["position"][0]) for s in specs]
    ys = [float(s["position"][1]) for s in specs]
    zs = [float(s["position"][2]) for s in specs]
    return {"x": [min(xs), max(xs)], "y": [min(ys), max(ys)],
            "z": [min(zs), max(zs)]}


def anchor_bounds(anchor_info):
    """anchor_info → 可建造区 x/z 范围。取不到返回 None。

    anchor_info 形如 {"gamearea0": [[1,[x,y,z]], [2,[x,y,z]], ...]}。
    """
    if not isinstance(anchor_info, dict):
        return None
    pts = []
    for _k, v in anchor_info.items():
        if not isinstance(v, (list, tuple)):
            continue
        for e in v:
            if isinstance(e, (list, tuple)) and len(e) >= 2 \
                    and isinstance(e[1], (list, tuple)) and len(e[1]) >= 3:
                pts.append((float(e[1][0]), float(e[1][2])))
    if len(pts) < 2:
        return None
    return {"x": [min(p[0] for p in pts), max(p[0] for p in pts)],
            "z": [min(p[1] for p in pts), max(p[1] for p in pts)]}


def check_framework(snap_fw, target_fw):
    """框架一致性。返回 {same, snapshot, target, level, msg}。"""
    same = (snap_fw is None or target_fw is None
            or str(snap_fw) == str(target_fw))
    return {"same": same, "snapshot": snap_fw, "target": target_fw,
            "level": "ok" if same else "warn",
            "msg": ("框架一致（%s）" % snap_fw) if same else
                   ("⚠️ 框架不同：快照 %s vs 目标 %s —— 地形高度基准不同，"
                    "摆过去的物件会整体悬空/陷入，建议 --set-framework"
                    % (snap_fw, target_fw))}


# key 14 是场景框架，由 --set-framework 单独负责（要重进才生效，风险级别不同）
FRAMEWORK_SETTING_KEY = "14"


def setting_numeric_keys(ms):
    """map_setting 里可同步的键 = 纯数字键。

    非数字键 `name` / `desc` / `tags` / `sub_tags` / `topics` 是**地图身份**，
    跨图复刻不该把源图的名字和标签盖到目标图上（设计如此，见 SNAPSHOT.md）。
    """
    return sorted([str(k) for k in (ms or {}) if str(k).isdigit()], key=int)


def _camp_ref_value(value):
    """从 `mode,1;type,0;error,0;camp_key,-1,` 这类串里取 camp_key 的值。取不到 None。

    ⚠️ 键和值是**逗号分开的相邻两段**（`camp_key` 然后 `-1`），不是 `camp_key=-1`。

    含队伍命名空间引用的设置键**不能当成普通标量搬**：目标图自定义过队名时
    同一数字指向的是另一支队。
    """
    toks = [t.strip() for t in str(value or "").replace(";", ",").split(",")
            if t.strip()]
    for i, t in enumerate(toks):
        if t == "camp_key":
            return toks[i + 1] if i + 1 < len(toks) else None
        if t.startswith("camp_key"):
            tail = t[len("camp_key"):].strip(" :=")
            if tail:
                return tail
    return None


def map_setting_diff(snap_ms, target_ms):
    """快照地图设置 ↔ 目标图当前设置（**只比玩法键**）。

    返回 {rows, diffs, level, msg, unknown}。目标读不到 → unknown，不算差异也不算通过。
    """
    keys = setting_numeric_keys(snap_ms)
    if not isinstance(target_ms, dict) or not target_ms:
        return {"rows": [], "diffs": [], "level": "unknown", "unknown": True,
                "msg": "目标图 map_setting 读不到，跳过设置对比（不做盲写）"}
    rows, diffs = [], []
    for k in keys:
        if k == FRAMEWORK_SETTING_KEY:
            continue                       # 框架另有 --set-framework 与独立告警
        want = str((snap_ms or {}).get(k))
        have = target_ms.get(k)
        have = "" if have is None else str(have)
        camp = _camp_ref_value(want)
        same = (want == have)
        r = {"key": k, "snapshot": want, "target": have, "same": same,
             "camp_ref": camp}
        rows.append(r)
        if not same:
            diffs.append(r)
    return {"rows": rows, "diffs": diffs, "level": "ok" if not diffs else "warn",
            "unknown": False,
            "msg": ("玩法设置与目标图一致（比了 %d 个键）" % len(rows) if not diffs
                    else "⚠️ %d 个玩法设置键与目标图不同：%s"
                         % (len(diffs), [d["key"] for d in diffs]))}


def map_props_status(snap_props, target_props):
    """地图属性层（默认背景音乐等）：源图设了什么 / 目标图现在有什么 / 能不能自动写。

    为什么这一层要单独对待：它不在组件袋、也不在 map_setting 的玩法键里，而在地图级属性
    `ugc_props_info['ugc_bgm_conf']`。写入通道是**面板自己那条**（2026-10-01 从开着的
    「更多 → 网易云音乐 → 背景音乐设置」面板 `UGCUICloudMusic.apply_ugc_bgm_conf` 反解出来：
    写 props + 发一个 `send_battle_event(UGC, {'key':'bgm_conf', ...})`）——
    `restore --set-bgm` 走的就是它；不加这个开关就只报不写。
    ⚠️ 不碰 `save_ugc_map_data`：那是"整条地图记录"级别的写入，载荷是增量还是整体覆盖
    没证清之前，拿一张 1.5 万件的图去试它不值。

    三档结论：`ok`（两边一致）/`diff`（不一致，可 --set-bgm 或人工补）/`unknown`
    （一边没采到，不能判 —— 老导出就是这个待遇，别把"没比成"说成"没问题"）。
    """
    lines = []
    if not isinstance(snap_props, dict):
        return {"level": "unknown",
                "lines": ["地图属性 [unknown] 这份导出**没采这一层**"
                          "（2026-10-01 之前的工具）⇒ 默认背景音乐有没有丢，看不出来；"
                          "请用新版重新导出"],
                "manual": []}
    if not isinstance(target_props, dict):
        return {"level": "unknown",
                "lines": ["地图属性 [unknown] 目标图这一层读不到（编辑器没打开地图？）"],
                "manual": []}
    manual, diffs = [], []
    for k in sorted(snap_props):
        if k.startswith("_"):
            continue
        sv = (snap_props.get(k) or {}).get("value")
        tv = (target_props.get(k) or {}).get("value")
        if sv == tv:
            if sv is not None:
                lines.append("    ✓ %s = %s（目标图一致）" % (k, _j(sv)))
            continue
        diffs.append(k)
        manual.append({"key": k, "snapshot": sv, "target": tv})
        lines.append("    ⚠️ %s：源图 %s / 目标图 %s"
                     % (k, _j(sv), _j(tv)))
    bgm_ids = (snap_props.get("_music_event_ids") or [])
    if bgm_ids:
        lines.append("    ℹ️ 源图音乐事件（组件 203920）ugc_id=%s —— 这一层是**组件**，"
                     "还原会带走；上面 ugc_bgm_conf 是**地图级**的另一层" % bgm_ids)
    if not diffs:
        return {"level": "ok", "lines": lines, "manual": []}
    return {"level": "diff", "lines": lines, "manual": manual}


def _j(v):
    try:
        return json.dumps(v, ensure_ascii=False)[:160]
    except Exception:
        return repr(v)[:160]


def sync_map_settings(snap_ms, target_ms=None, dry=False):
    """把快照里与目标图**不同**的玩法设置键逐项写到目标图。

    * 走 `set_map_setting`：原生 `set_map_setting_data(key, value, source='')`，
      幂等（值相同直接返回 changed=False），且候选值受 `ugc_setting_config` 校验。
    * 目标读不到就不动 —— 宁可不写，也不要在看不见现状的情况下改地图级行为。
    * key 14（框架）不在这里写：它要重进才生效，由 --set-framework 负责。
    """
    d = map_setting_diff(snap_ms, target_ms)
    if d["unknown"]:
        return {"skipped": True, "reason": d["msg"], "applied": [], "failed": [],
                "refused": [], "diffs": [], "level": "unknown"}
    applied, failed, refused = [], [], []
    for r in d["diffs"]:
        key, want = r["key"], r["snapshot"]
        ref = r.get("camp_ref")
        if ref not in (None, "-1", ""):
            refused.append({"key": key, "value": want, "camp_ref": ref,
                            "error": "含队伍引用 camp_key=%s，目标图队名命名空间不同时"
                                     "指向会错，交人工确认" % ref})
            continue
        if dry:
            applied.append({"key": key, "before": r["target"], "after": want,
                            "dry_run": True})
            continue
        resp = call_edit("set_map_setting", {"key": key, "value": want},
                         timeout=60) or {}
        # 回执有两种形状（op 结果在顶层 / 包在 value 里），与 --set-framework 那处保持一致，
        # 但一次解开后所有字段都从同一层取，别一半顶层一半 value。
        layer = resp if ("ok" in resp or "error" in resp) else (resp.get("value") or {})
        # op 里的 ok 已经包含"写完回读一遍、值要等于目标值"⇒ 失败必须把回读值带出来，
        # 否则"原生返回 False"和"写进去了但回读是别的值"这两种故障分不开。
        row = {"key": key, "before": r["target"], "after": want,
               "native_after": layer.get("after"),
               "changed": layer.get("changed", True),
               "error": layer.get("error")}
        (applied if layer.get("ok") else failed).append(row)
    return {"skipped": False, "reason": None, "diffs": d["diffs"],
            "applied": applied, "failed": failed, "refused": refused,
            "level": "error" if (failed or refused) else "ok"}


def settings_verify(snap_ms, target_ms):
    """重进后回读设置层：输出行 **+ 这一层的结论**。

    为什么要把 level 单独给出来：`--verify-only` 的 exit code 一直是"事件引用"的结论，
    设置层只在屏幕上印一行 `[diff]` 就参与不了判定 ⇒ 一句"exit 0 说明都落住了"会把
    没落住的那层一起说成落住了（文档写的却是"重进后 `--verify-only` 的 `设置` 行必须是 [ok]"）。
    """
    sd = map_setting_diff(snap_ms, target_ms)
    tail = "（重进后回读，这一层才算落库判据）"
    if sd["unknown"]:
        lines = ["  设置  [unknown] %s" % sd["msg"]]
    else:
        lines = ["  设置  [%s] %s%s" % (sd["level"], sd["msg"], tail)]
        for r in sd["diffs"][:6]:
            lines.append("      key %-4s 快照=%-24r 现在=%r"
                         % (r["key"], r["snapshot"], r["target"]))
        if len(sd["diffs"]) > 6:
            lines.append("      …另有 %d 个键没落住" % (len(sd["diffs"]) - 6))
    return {"lines": lines, "level": sd["level"], "diffs": sd["diffs"],
            "msg": sd["msg"]}


def settings_verify_lines(snap_ms, target_ms):
    """只要输出行的老接口（`--verify-only` 用 `settings_verify` 拿结论）。"""
    return settings_verify(snap_ms, target_ms)["lines"]


def same_map_guard(snap, target_info):
    """写之前先回答一件手册里只写了"建议"的事：**打开的是不是源图本身**。

    把 A 图的快照 `--apply` 回 A 图，等于把整张图在自己身上再复制一万四千件
    （id 全新增、不覆盖原件），而这一步之后只能靠"删掉一万四千件"收拾 ——
    恰好是本项目最容易被静默删错对象的形态（共用单元上的删除）。
    所以它必须在**任何写入之前**被拦下，而不是靠人读文档自觉。

    认法用两个独立证据，任一命中就起疑：
    * 图名相同（跨图复刻时目标图**不该**改名成源图名 —— 名字都不覆盖是设计）；
    * `component_nums` 与 `registered` 两个计数器都和快照对得上（顶格图差不到 0.5%）。

    只判不看开关：返回 `{level, msg, reasons}`，`--allow-same-map` 的解释归 `main()`。
    `block` 拦 `--apply`；`warn` 只提醒；`unknown` 是没读到目标图信息（不拦，但明说
    "这一项没查成"）。
    """
    mp = snap.get("map") or {}
    src_name = (mp.get("map_setting") or {}).get("name") or mp.get("name")
    if not target_info:
        return {"level": "unknown", "reasons": [],
                "msg": "没读到目标图信息 ⇒ 「是不是源图本身」这一项没查成"}
    tgt_name = (target_info.get("map_setting") or {}).get("name")
    reasons = []
    if src_name and tgt_name and str(src_name) == str(tgt_name):
        reasons.append("图名与快照相同（%s）" % tgt_name)
    sb = snap.get("budget") or {}
    tb = target_info.get("budget") or {}
    matched = []
    for k in ("component_nums", "registered"):
        a, b = sb.get(k), tb.get(k)
        if isinstance(a, int) and isinstance(b, int) and a > 0 \
                and abs(a - b) <= max(2, int(a * 0.005)):
            matched.append("%s=%s" % (k, b))
    if len(matched) >= 2:
        reasons.append("计数器逐一对上（%s）" % ", ".join(matched))
    elif len(matched) == 1 and tgt_name != src_name:
        return {"level": "warn", "reasons": reasons,
                "msg": "ℹ️ %s 与快照一致，但图名不同 ⇒ 大概只是巧合，确认一下目标图"
                       % matched[0]}
    if reasons:
        return {"level": "block", "reasons": reasons,
                "msg": "⛔ 当前打开的图**看起来就是这份快照的来源图**（%s）："
                       "--apply 会把整张图在自己身上再复制一遍，且只能靠批量删除收拾。"
                       "确认无误请加 --allow-same-map；否则请打开目标图（B）再跑"
                       % "；".join(reasons)}
    return {"level": "ok", "reasons": [],
            "msg": "目标图与快照不是同一张（图名 %r ≠ %r）" % (tgt_name, src_name)}


def check_bounds(bbox, bounds, margin=0.0):
    """场景包围盒是否落在目标可建区内。任一为空 → unknown。"""
    if not bbox or not bounds:
        return {"level": "unknown", "msg": "缺少包围盒或目标边界，跳过范围检查"}
    out = []
    for ax in ("x", "z"):
        lo, hi = bbox[ax]
        blo, bhi = bounds[ax]
        if lo < blo - margin or hi > bhi + margin:
            out.append("%s 轴 %.0f..%.0f 超出目标 %.0f..%.0f"
                       % (ax, lo, hi, blo, bhi))
    return ({"level": "ok", "msg": "场景在目标可建区内"} if not out else
            {"level": "warn", "msg": "⚠️ 超出可建区：" + "；".join(out)})


def plan_budget_need(snap, plan, specs, with_story=False):
    """这次还原**实际**要占多少预算。

    ⚠️ 不能用源图整图的读数当 need：一张图里总有一批件不摆（骨架/单例/UI/剧情三件套），
    也总有一批件是开关放开的（--allow-sentinel 的运动器）。两头误差叠在一起，
    在顶格图（春岚 14653/15000，余量 ~115）上会同时造成误放行与误拦。
    occupancy 每件成本不线性，按源图均值折算（宁高不低，向上取整）。
    """
    budget = snap.get("budget") or {}
    total_comps = len(snap.get("components") or [])
    # 口径是**快照件数**，不是请求数：成组创建（204041）一个请求摆两件，
    # 按请求算会少报预算，顶格图上就是误放行。
    need_components = covered_components(specs)
    if with_story:
        need_components += sum(1 for s in (plan or {}).get("skipped") or []
                               if str(s.get("reason", "")).startswith("story_stage"))
    occ = budget.get("occupancy")
    per_comp = (float(occ) / total_comps) if occ and total_comps else 1.0
    return {"components": need_components,
            "occupancy": int(math.ceil(need_components * max(1.0, per_comp))),
            "occupancy_per_component": round(per_comp, 4)}


def check_budget(snap_budget, target_budget, plan_counts=None):
    """预算余量。occupancy / component_nums 都要看。

    plan_counts 由 `plan_budget_need` 给出时按**实际计划件数**算 need；
    不给则退回整图口径（只有历史调用/单测用，实机预检一定要传）。
    """
    if not snap_budget or not target_budget:
        return {"level": "unknown", "msg": "缺预算数据"}
    need_map = {"occupancy": (plan_counts or {}).get("occupancy"),
                "component_nums": (plan_counts or {}).get("components")}
    rows = []
    for key, tkey in (("occupancy", "max_occupancy"),
                      ("component_nums", "max_component_nums")):
        need = need_map.get(key)
        basis = "计划" if need is not None else "快照整图"
        if need is None:
            need = snap_budget.get(key)
        cap = target_budget.get(tkey)
        cur = target_budget.get(key)
        if need is None or cap is None:
            continue
        free = (cap - cur) if cur is not None else cap
        rows.append({"metric": key, "need": need, "basis": basis,
                     "target_used": cur,
                     "target_cap": cap, "free": free,
                     "fits": free >= need})
    bad = [r for r in rows if not r["fits"]]
    return {"level": "ok" if not bad else "error", "rows": rows,
            "planned": bool(plan_counts),
            "msg": ("预算充足" if not bad else
                    "⛔ 预算不足：" + "；".join(
                        "%s 需 %s 余 %s" % (r["metric"], r["need"], r["free"])
                        for r in bad))}


def pair_global_singletons(snap_components, target_singletons):
    """快照的全局单例 ↔ 目标图已有的单例，按 component_type 配对。

    返回 (pairs, missing)：pairs=[{component_type, target_ugc_id, snap_comp}]，
    missing=[只在快照里有、目标图没有的类型]。同类型多件时按排序顺序一一对应。
    """
    by_type = {}
    for t in (target_singletons or []):
        by_type.setdefault(int(t.get("component_type")), []).append(t)
    for k in by_type:
        by_type[k].sort(key=lambda x: str(x.get("ugc_id")))

    snap_by_type = {}
    for c in (snap_components or []):
        try:
            ct = int(component_id_of(c))
        except (TypeError, ValueError):
            continue
        # ⚠️ 剧情三类的编号也在 1e7 以上，但它们不是单例，由 --with-story 单独重建
        if ct >= GLOBAL_TYPE_FLOOR and ct not in SKIP_STORY_TYPES:
            snap_by_type.setdefault(ct, []).append(c)
    for k in snap_by_type:
        snap_by_type[k].sort(key=lambda x: str(x.get("g")))

    pairs, missing = [], []
    for ct in sorted(set(snap_by_type) & set(by_type)):
        for sc, tg in zip(snap_by_type[ct], by_type[ct]):
            pairs.append({"component_type": ct, "target_ugc_id": tg.get("ugc_id"),
                          "snap_ugc_id": sc.get("g"), "snap_comp": sc})
    for ct in sorted(set(snap_by_type) - set(by_type)):
        missing.append({"component_type": ct, "count": len(snap_by_type[ct])})
    return pairs, missing


STORY_KIND_NAMES = {10007001: "STORY", 10007002: "DIALOGUE", 10007003: "CHOICE"}


def _dangling_owners(snap, old_ids, limit=12):
    """悬空引用的旧 ugc_id → 「类型(名字/内容)」，让计划可读。

    指向剧情三类的，就是「选了某选项→触发某事件」这类交互；指向物件的则是普通事件。
    ⚠️ 超过 `limit` 条必须**截断并补一份按类型的总账**：交付体检实机印过 233 条逐条列出
    （`#125 type=203911、#127 type=203911、…` 刷满一屏），那种输出的下场是整段被人跳过，
    而这段要传达的恰恰是"这 233 条全是没摆的运动器、出口只有一个"。
    """
    idx = {c.get("g"): c for c in (snap.get("components") or [])}
    out = []
    kinds = defaultdict(int)
    for old in sorted(old_ids):
        c = idx.get(old)
        if c is None:
            out.append("#%s(不在快照)" % old)
            kinds["不在快照"] += 1
            continue
        try:
            ct = int(component_id_of(c))
        except (TypeError, ValueError):
            ct = None
        kind = STORY_KIND_NAMES.get(ct)
        if not kind:
            out.append("#%s type=%s" % (old, ct))
            kinds["type %s" % ct] += 1
            continue
        label = _story_label(snap, old) or ""
        out.append("#%s %s%s" % (old, kind, ("「%s」" % label) if label else ""))
        kinds[kind] += 1
    if len(out) <= limit:
        return "、".join(out)
    return "%s…（共 %d 条，按类型：%s）" % (
        "、".join(out[:limit]), len(out),
        ", ".join("%s×%d" % kv for kv in sorted(kinds.items(), key=lambda kv: -kv[1])))


def _story_label(snap, ugc_id):
    """从快照剧情文档里取该节点的可读名字（剧情名 / 选项内容）。"""
    st = snap.get("story") or {}
    for s in (st.get("stories") or []):
        if s.get("ugc_id") == ugc_id:
            return s.get("name") or ""
        for d in (s.get("dialogues") or []):
            if d.get("ugc_id") == ugc_id:
                return d.get("content") or ""
            for c in (d.get("choices") or []):
                if c.get("ugc_id") == ugc_id:
                    return c.get("content") or ""
    for c in (st.get("orphan_choices") or []):
        if c.get("ugc_id") == ugc_id:
            return c.get("content") or ""
    return ""


CODE_ID_INDEX = '''
import sys
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
_cm = _fe.get_component_manager()
_room = _fe.get_room()
_lo, _hi = %LO%, %HI%
_idx = {}
_max = 0
_n_all = 0
for _gid, _c in ((_cm._ugc_id2component or {}) if _cm else {}).items():
    _n_all += 1
    try:
        _g = int(_gid)
    except (TypeError, ValueError):
        continue
    if _g > _max:
        _max = _g
    if _lo <= _g <= _hi:
        _idx[str(_gid)] = _fe.component_item_id(_room, _gid, None)
__result__ = {"index": _idx, "n": len(_idx), "total": _n_all,
              "max_gid": _max, "lo": _lo, "hi": _hi}
'''

# 回传层 `_safe` 把 dict/list 都截到 **200 条** ⇒ 一页最多取 150 个号，留余量。
LIVE_INDEX_WINDOW = 150
LIVE_INDEX_MAX_PAGES = 400      # 150 × 400 = 60000 个号，够任何一张编辑器图


def fetch_live_index(window=LIVE_INDEX_WINDOW):
    """当前地图的 `{ugc_id(str): item_id}`（轻量：不取属性袋，只取身份）。

    ⛔ **一次调用拿不全**：桥接回传层 `_safe` 把 dict 截到 200 条（2026-10-02 实机：
    6031 件时只回 200 条），而 `progress_audit` 拿它判"进度里的 ugc_id 还在不在图上"
    —— 于是 5700 件里 5613 件被误判成"找不到 ⇒ 会话过期"，续做被闸门直接拦下
    （真因是回传截断，不是重进重编号）。这里按 gid 窗口分页取全。

    返回 `(index, total)`；读不到（层缺失）时返回 `({}, None)` —— 调用方按"没比成"处理，
    不拿半张表当真表。
    """
    from snapshot import _bridge_dir as _bd
    out, total, max_gid = {}, None, 0
    lo = 0
    for _ in range(LIVE_INDEX_MAX_PAGES):
        code = (CODE_ID_INDEX.replace("%BRIDGE%", repr(_bd()))
                .replace("%LO%", str(lo)).replace("%HI%", str(lo + window)))
        d = call_edit("eval", {"code": code}, timeout=180) or {}
        layer = _find_layer(d, "index")
        if not isinstance(layer, dict) or "index" not in layer:
            return {}, None
        out.update(layer.get("index") or {})
        total = layer.get("total")
        max_gid = layer.get("max_gid") or 0
        lo += window + 1
        if lo > max_gid:
            break
    return out, total


def read_progress_records(path):
    """进度 jsonl 的**原始记录**（含没成功的），审计要看全部，不只 placed。"""
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def progress_audit(records, live_index):
    """断点续做的**可信度**审计：进度说"摆过"的件，图上到底还在不在。

    为什么需要：15k 的长跑靠 progress 续做，而 `load_progress` 把 `done` 里的 `old_g`
    一律跳过。某件若当时回执 `placed`、后来却被摘除/被别的操作删掉，续做会永远跳过它
    ⇒ 最终图少件，而过程日志全程"成功"。这类静默丢失只有拿实况身份回对才看得见。

    ⚠️ **只在同一会话内有效**：重进地图后 ugc_id 全部重编号，记录里的 id 不再指向那件东西。
    所以大面积缺失时报 `stale_session`（改判 1:1 请用 `snapshot.py accept --live`），
    而不是印一屏"缺失"让人以为还原坏了。
    """
    claims = [r for r in records or [] if r.get("old_g") is not None]
    placed = [r for r in claims
              if r.get("status") == "placed" and r.get("ugc_id") is not None]
    not_placed = [{"old_g": r.get("old_g"), "status": r.get("status")}
                  for r in claims
                  if not (r.get("status") == "placed" and r.get("ugc_id") is not None)]
    missing, wrong_item, unknown = [], [], []
    for r in placed:
        key = str(r["ugc_id"])
        if key not in (live_index or {}):
            missing.append({"old_g": r.get("old_g"), "ugc_id": r["ugc_id"],
                            "item_id": r.get("item_id")})
            continue
        live_it, want = live_index[key], r.get("item_id")
        if live_it is None or want is None:
            unknown.append(r["ugc_id"])        # 身份取不到 ⇒ 不猜，单独列出来
        elif int(live_it) != int(want):
            wrong_item.append({"old_g": r.get("old_g"), "ugc_id": r["ugc_id"],
                               "expected_item": want, "actual_item": int(live_it)})
    # ⚠️ "会话过期"的判据要**又要比例、又要样本量**：试点常常只有 1~2 条记录，
    # 那时"1/1 找不到"是真丢件，不是重进导致的重编号（拿比例 alone 会把真问题说成审计失效）。
    stale = bool(placed) and len(placed) >= 20 and len(missing) > len(placed) * 0.5
    inconclusive = bool(missing) and not stale and len(placed) < 20
    return {"claimed": len(placed), "records": len(claims),
            "missing": missing, "wrong_item": wrong_item,
            "identity_unknown": unknown, "not_placed": not_placed,
            "stale_session": stale, "inconclusive_small": inconclusive,
            "ok": (not missing and not wrong_item and not unknown
                   and not stale)}


def print_audit(a):
    print("进度回对：记录 %d 条，其中声称已摆放 %d 件" % (a["records"], a["claimed"]))
    if a["stale_session"]:
        print("  ❌ 声称摆过的 %d 件里 %d 件在当前图上找不到 ⇒ 大概率**已经重进过地图**"
              "（ugc_id 全部重编号，progress 里的身份不再指向那些件）。这条审计不适用，"
              "判 1:1 请改用 `snapshot.py accept --live`。" % (a["claimed"], len(a["missing"])))
        return
    if a["missing"]:
        print("  ⛔ 进度说摆过、但当前图上没有 %d 件：%s"
              % (len(a["missing"]),
                 ", ".join("old_g=%s→ugc_id=%s" % (m["old_g"], m["ugc_id"])
                           for m in a["missing"][:10])
                 + ("…" if len(a["missing"]) > 10 else "")))
        print("     后果：续做按 `old_g` 会永远跳过它们 ⇒ 最终图少件而过程日志全程成功。"
              "补做请用 --only-ids 单独摆这几件（或 --new-run 重跑）。")
        if a.get("inconclusive_small"):
            print("     ℹ️  这次声称摆过的只有 %d 件（样本太小），**无法**区分"
                  "「真的丢了」与「已经重进过、id 全变了」：请用 `accept --live` 逐件对账。"
                  % a["claimed"])
    if a["wrong_item"]:
        print("  ⛔ id 被别的件占了 %d 处：%s"
              % (len(a["wrong_item"]),
                 ", ".join("old_g=%s ugc_id=%s 期望 item=%s 实际=%s"
                           % (w["old_g"], w["ugc_id"], w["expected_item"], w["actual_item"])
                           for w in a["wrong_item"][:8])))
    if a["identity_unknown"]:
        print("  ℹ️  %d 件取不到 item_id（引擎侧 getter 没给值）⇒ **没比成**，不算通过"
              % len(a["identity_unknown"]))
    if a["not_placed"]:
        counts = {}
        for r in a["not_placed"]:
            counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
        print("  ℹ️  %d 条没声称成功（%s）⇒ 本来就该重做，不算丢失"
              % (len(a["not_placed"]), counts))
    if a["ok"]:
        print("  ✓ 声称摆过的件逐件对上（身份一致），续做判据可信")


def rebuild_id_map(snap_a, snap_b, tol=0.5):
    """重进之后（ugc_id 全部重编号）**从当前图实况重建** 旧ugc_id → 新ugc_id。

    为什么必须有这条路：一万四千件的还原现实上会跨会话（引擎更新、崩、人重进）。
    而 `run_id_for` 是按快照内容稳定派生的、`load_progress` 只按图名筛记录 ⇒
    重进后再跑同一条命令，会**跳过摆放**（那些 old_g 都算"摆过"）却拿着**过期 id**
    去跑属性回写与事件绑定 ⇒ 静默写到别的组件上。这不是理论：手册现在就在推荐
    "再跑一次补 --include-ui 那一层"。

    配对复用 `snapshot._pair_components`（含"同距离优先配内容签名"与残骸不计），
    所以这里不再实现第二套身份判据。
    """
    from snapshot import _pair_components
    ca = [c for c in (snap_a.get("components") or []) if not c.get("del")]
    cb = [c for c in (snap_b.get("components") or []) if not c.get("del")]
    pairs, unpaired_a = _pair_components(ca, cb, tol)
    id_map, extra = {}, []
    for x, y in pairs:
        if y is None:
            continue
        if x is None:
            extra.append(y.get("g"))
            continue
        id_map[x.get("g")] = y.get("g")
    return {"id_map": id_map, "todo": [x.get("g") for x in unpaired_a],
            "extra": extra, "a_total": len(ca), "b_total": len(cb),
            "mapped": len(id_map)}


def load_progress(path, rid=None, cur_map=None, new_run=False):
    """读进度 jsonl → (id_map, done_old_g 集合, 被忽略的记录数)。

    ⚠️ 续做判据是「这件东西摆过没有」（old_g），不是「第 i 条记录」：加了 --only-*
       之后试点与全量的 specs 顺序完全不同，按 index 判定会把没摆的当成摆过（漏件），
       或反过来重复摆（顶格图上代价最大）。
    ⚠️ old_g 只在**同一张目标图**上有意义：同一份快照在别的图上跑过的记录必须忽略，
       否则 id 映射会指向另一张图的组件。图名读不到时保守沿用（宁多摆不漏做）。
    """
    id_map, done, foreign = {}, set(), 0
    if not path or not os.path.exists(path):
        return id_map, done, foreign
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") != "placed" or rec.get("old_g") is None:
                continue
            if new_run and rec.get("run") != rid:
                foreign += 1
                continue
            if cur_map and rec.get("map") and rec["map"] != cur_map:
                foreign += 1
                continue
            done.add(rec["old_g"])
            id_map[rec["old_g"]] = rec.get("ugc_id")
    return id_map, done, foreign


def run_id_for(snapshot_path, items, new_run=False):
    """稳定派生：同快照重跑复用同一批 request_id → 天然幂等。"""
    if new_run:
        return "r%d" % int(time.time())
    h = hashlib.sha1(json.dumps(items, sort_keys=True, ensure_ascii=False)
                     .encode("utf-8"))
    return "h" + h.hexdigest()[:10]


# ---------------------------------------------------------------------------
# 游戏侧交互
# ---------------------------------------------------------------------------
def fetch_placeable_types():
    """可摆放类型集合（place_support == verified）。拿不到就返回 None（不限制）。"""
    try:
        d = call_edit("list_types", {}, timeout=60) or {}
        node = _unwrap(d, "types")
        types = (node or {}).get("types") or []
        return {int(t.get("type")) for t in types
                if t.get("place_support") == "verified"
                and str(t.get("type")).lstrip("-").isdigit()}
    except Exception:
        return None


def fetch_target_info():
    """目标图（当前打开的图）的框架 / 边界 / 预算 / 类型分布。跨图预检用。"""
    d = call_edit("map_info", {}, timeout=60) or {}
    layer = _find_layer(d, "map_setting") or {}
    ms = layer.get("map_setting") or {}
    anchors = _find_layer(d, "anchor_info")
    anchors = (anchors or {}).get("anchor_info")
    # 预算没有独立 op，走 eval；延迟导入避免与 snapshot.py 循环依赖
    budget = None
    try:
        from snapshot import CODE_BUDGET as _cb, _bridge_dir as _bd, _call as _ce
        d = _ce("eval", {"code": _cb.replace("%BRIDGE%", repr(_bd()))})
        budget = _find_layer(d, "occupancy") or _find_layer(d, "registered")
    except Exception:
        budget = None
    if isinstance(budget, dict) and budget.get("component_nums_live") is not None:
        # `component_nums` 里含"已登记但待保存删除"的残影（要等重进才从本地索引消失），
        # 预算判据用扣掉它的活值，并把原值留着给人看（不静默替换）。
        budget = dict(budget)
        budget["component_nums_raw"] = budget.get("component_nums")
        budget["component_nums"] = budget["component_nums_live"]
    return {
        "framework": ms.get("14"),
        "map_setting": {k: v for k, v in ms.items()
                        if isinstance(v, (str, int, float))},
        "bounds": anchor_bounds(anchors),
        "budget": dict(budget) if isinstance(budget, dict) else None,
        "type_distribution": dict(layer.get("type_distribution") or {}),
    }


def pending_deletion_note(budget):
    """目标图待删除袋的一句话，**带身份**（哪几件、叫什么）。

    为什么不能只印条数：这批删除会被下一次保存提交（退出编辑器也会自动保存），
    而 `{'unit': 7, 'component': 4}` 这种印法数得出几件、认不出是哪件 ——
    2026-10-01 就因此让一张图里"少了一个有名字的运动器"拖到事后才发现。
    """
    bag = (budget or {}).get("del_unit_uids") or {}
    # 空桶不是待删除（摆放操作会留 {'unit': []}），印出来就是假告警
    counts = {k: len(v) for k, v in bag.items() if v}
    try:
        from snapshot import format_pending_deletions as _f
        who = _f((budget or {}).get("pending_deletions") or [],
                 (budget or {}).get("pending_deletions_total"))
    except Exception:
        who = "（身份解析不可用）"
    return ("目标图有待删除袋 %s：%s。这些删除会在下一次保存时被提交"
            "（退出编辑器也会自动保存）⇒ 动手前先确认它们**是你自己要删的**；"
            "不是你删的，先别保存，按名字把原件从快照补回来"
            % (counts, who))


def fetch_target_singletons():
    """目标图已存在的全局单例（带 ci）。"""
    try:
        d = call_edit("global_singletons", {"with_ci": True}, timeout=60) or {}
    except Exception:
        return []
    layer = _find_layer(d, "components")
    return ((layer or {}).get("components") or [])


def sync_global_singletons(snap, dry=False):
    """把快照的全局单例属性**就地覆写**到目标图同类型的单例上。

    ⚠️ 不新建：目标图天生各有一套单例，再造一套会打架。
    ⚠️ 这里**没有 id 换号**（单例不是这次新建的物件，没有 old→new 映射可用）：
      若某个单例的袋里存了组件 id（成员表那类字段），旧号会原样写进目标图。
      春岚实测 8 处配对里 0 处带这种字段，所以先报不改；带图的就必须先看这条。
    """
    targets = fetch_target_singletons()
    pairs, missing = pair_global_singletons(snap.get("components") or [], targets)
    universe = {int(c["g"]) for c in (snap.get("components") or [])
                if c.get("g") is not None}
    applied, failed, ref_hits = [], [], []
    for p in pairs:
        ci = _info_of(p["snap_comp"])
        events = ci.get("events")
        if not events:
            continue
        for e in events:
            pid = str(e.get("type"))
            if not pid.endswith(("18701", "03001")):
                continue
            stack, ints = [e.get("info")], []
            while stack:
                x = stack.pop()
                if isinstance(x, (list, tuple)):
                    stack.extend(x)
                elif isinstance(x, int) and not isinstance(x, bool):
                    ints.append(x)
            hits = [v for v in ints if v in universe]
            if hits:
                ref_hits.append({"component_type": p["component_type"],
                                 "property_id": pid, "count": len(hits)})
        # ⚠️ 骨架自带的全局单例**不在** ui.all_ugc_uid_set（编辑器登记集合只收"摆放
        # 出来"的物件）⇒ 必须显式 allow_unregistered，否则 6 处单例同步全被挡下
        # （2026-09-23 实测：type=10001001/10002001/10003001/10003002/10009001 全失败）。
        r = call_edit("set_component_info", {
            "ugc_id": p["target_ugc_id"], "events": events,
            "expect_component_id": p["component_type"], "dry_run": bool(dry),
            "allow_unregistered": True,
        }, timeout=90) or {}
        (applied if r.get("ok") or r.get("dry_run") else failed).append(
            {"component_type": p["component_type"],
             "target_ugc_id": p["target_ugc_id"],
             "error": r.get("error")})
    return {"pairs": len(pairs), "applied": applied, "failed": failed,
            "refs_needing_remap": ref_hits,
            "missing_in_target": missing}


def place_one(spec, token):
    args = {
        "item_id": spec["item_id"],
        "position": list(spec["position"]),
        "rotation": list(spec.get("rotation") or (0, 0, 0)),
        "scale": list(spec.get("scale") or (1, 1, 1)),
        "request_id": token,
        "mode": spec.get("mode") or "server",
    }
    if spec.get("allow_unverified"):
        args["allow_unverified"] = True
    if spec.get("group_transforms"):
        # 成组创建（204041）：第 2..N 件各自的落点，摆放侧逐件确认
        args["group_transforms"] = spec["group_transforms"]
    # 还原抄的是**既有件的实测缩放**，不是玩家手输的新值：编辑器滑块下限会把它整批
    # 拦掉（2026-10-01 实机：春岚 214 个组合件 z=0.02 < 目录下限 0.1 ⇒ 组合层一件都摆不出）。
    # 引擎本身收这个值（这些件就在源图里），回读仍按请求值严格校验，越界值由回执
    # `catalog_scale_deviation` 显式带回来，不静默吞掉。
    args["allow_out_of_range_scale"] = True
    r = call_edit("place_item", args, timeout=90) or {}
    status, tries = r.get("status"), 0
    while status != "placed" and status not in BAD and tries < POLL_TRIES:
        wait = float(r.get("poll_after_ms") or 0) / 1000.0
        if wait > 0:
            time.sleep(wait)
        r = call_edit("place_poll", {"request_id": token}, timeout=90) or {}
        status = r.get("status")
        tries += 1
    return r, status, tries


# 批量路径仍 pending 的状态（继续轮询）；其余状态（placed / BAD / error / overdue）
# 都已定论 —— 「试了失败」逐件定性，与单件路径同一套语义（护栏 6）。
BATCH_PENDING = ("pending", "finalizing", "ambiguous")


def failure_reason(status, row):
    """非 placed 的进度行要带上的**原因**（缺陷 C：观测性）。

    2026-10-02 实机（run_apply_batch2.log）：100 条 `status:"exception"` 的进度行
    没有任何 error 字段，现场只剩"100 件失败"这个数字 —— 为什么失败、当时试了什么、
    桥说了什么，全丢了。这里把回执里可读的第一个原因字段落进进度行；回执里**真的
    没有**原因时也要说出来（"没有原因字段"本身是证据，不是沉默的借口）。

    ⚠️ `placed` 行**绝不加键**：progress 的逐件行格式被断点续做、`--check-progress`、
    `--resume-from` 和回归锁一起吃（护栏 5），不许为了可观测性去动成功行。
    """
    if status == "placed":
        return None
    row = row if isinstance(row, dict) else {}
    for key in ("error", "note", "block_reason", "reason"):
        val = row.get(key)
        if val:
            return str(val)[:300]
    return "回执里没有原因字段（status=%s，字段=%s）" % (status, sorted(row))


def place_batch_chunk(chunk, rid):
    """批量摆放一批：一条 place_batch 登记 + 批量 poll 到整批定论/超次。

    `chunk` 是 [(i, spec)]（i 必须与全量计划的位次一致，token 沿用
    "rs_<run>_<i>"，续做时同 token 幂等）。每批 2-3 次往返替代旧路径每件
    2-3 次（原型实测见 probe/_batch_place_bench.md：登记 0.003ms/件、
    N=1000 落定 ≤0.7s、finalize ≈2.1ms/件）。
    返回 {i: (row, status, tries)}；row 是逐件回执（含 ugc_id / group_ugc_ids /
    error）。登记调用本身失败会抛异常（调用方把这一整批当"没试"处理，
    不写进度——护栏 6：没试与试了失败要分开）。
    """
    items = []
    for i, spec in chunk:
        args_ = {
            "item_id": spec["item_id"],
            "position": list(spec["position"]),
            "rotation": list(spec.get("rotation") or (0, 0, 0)),
            "scale": list(spec.get("scale") or (1, 1, 1)),
            "request_id": "rs_%s_%05d" % (rid, i),
            "mode": spec.get("mode") or "server",
        }
        if spec.get("allow_unverified"):
            args_["allow_unverified"] = True
        if spec.get("group_transforms"):
            args_["group_transforms"] = spec["group_transforms"]
        # 同 place_one：还原抄的是既有件的实测缩放，编辑器滑块范围拦不住引擎
        args_["allow_out_of_range_scale"] = True
        items.append(args_)
    r = call_edit("place_batch", {"items": items}, timeout=120)
    # 2026-10-02 实机（run_apply_batch2.log）：护栏拒绝整批（46 条不属于本批的未定论
    # 请求）时 op 抛异常，而失败信封在调用方眼里只是一个"缺 results 的普通 dict"⇒
    # 这 100 件被逐件记成 status=exception、**一行原因都没留下**，还把整个后续批次
    # 的进度口径污染成"试了失败"（违反护栏 6：没试 ≠ 试了失败）。
    # 登记阶段整体失败 = 这批**没试**：抛出去，由调用方按"不写进度 + 停下 + 报原因"
    # 处理。token 是确定性的（rs_<run>_<i>），重跑幂等续做。
    if not isinstance(r, dict):
        raise RuntimeError("place_batch 未返回可用回执（%r）" % (r,))
    if r.get("error"):
        raise RuntimeError("place_batch 失败：%s" % str(r["error"])[:300])
    if not isinstance(r.get("results"), list):
        raise RuntimeError("place_batch 回执缺 results（键=%s）" % sorted(r))
    by_i, token_to_i = {}, {}
    for row in (r.get("results") or []):
        idx = row.get("index")
        if idx is None or not (0 <= idx < len(chunk)):
            continue
        i = chunk[idx][0]
        by_i[i] = (dict(row), row.get("status"), 0)
        if row.get("request_id"):
            token_to_i[row["request_id"]] = i
    tries = 0
    while tries < POLL_TRIES:
        unres = [row.get("request_id") for row, st, _t in by_i.values()
                 if st in BATCH_PENDING and row.get("request_id")]
        if not unres:
            break
        wait = float((r or {}).get("poll_after_ms") or 150) / 1000.0
        if wait > 0:
            time.sleep(wait)
        r = call_edit("place_poll_batch", {"request_ids": unres}, timeout=120) or {}
        for row in (r.get("results") or []):
            i = token_to_i.get(row.get("request_id"))
            if i is None or i not in by_i:
                continue
            prev, _st, _t = by_i[i]
            merged = dict(prev)
            merged.update(row)
            by_i[i] = (merged, row.get("status"), tries + 1)
        tries += 1
    return by_i


class RunWatch(object):
    """长跑的可观测与止损：心跳、失败原因当场可见、连续失败早停。

    为什么需要：`place_one` 失败也要走满 poll（`POLL_TRIES`），14885 件的循环里
    若从第 100 件起系统性失败（预算耗尽、桥接掉了、在途锁卡死、图被换掉…），
    旧代码会闷着头转几小时，把目标图留在半张状态，而过程只留下一行"失败 N"的事后总结。
    **止损要发生在当下**，并且要留下"为什么停"的证据，而不是让人猜。
    """

    def __init__(self, total, heartbeat=200, abort_after=12, t0=None, clock=None):
        self.total = int(total or 0)
        self.heartbeat = max(1, int(heartbeat or 200))
        # `--abort-after 0` = 不早停（`abort_after or 12` 会把 0 悄悄变成 12）
        try:
            self.abort_after = 0 if int(abort_after) == 0 else max(1, int(abort_after))
        except (TypeError, ValueError):
            self.abort_after = 12
        self.clock = clock or time.perf_counter
        self.t0 = t0 if t0 is not None else self.clock()
        self.seen = self.placed = self.failed = self.consec = 0
        self._reasons = set()
        self.abort = None

    def record(self, status, error=None):
        """记一件，返回要打印的行（可能为空）。连续成功会把连败计数清零。

        `status` 给字符串（阶段 1 的回执状态）或布尔（阶段 2/3 的成败）都可以 ——
        三个阶段共用同一套止损判据，不各写一遍。
        """
        ok = status is True or (isinstance(status, str) and status == "placed")
        self.seen += 1
        out = []
        if ok:
            self.placed += 1
            self.consec = 0
        else:
            self.failed += 1
            self.consec += 1
            key = "%s|%s" % (status if isinstance(status, str) else "failed",
                             str(error or "")[:120])
            if key not in self._reasons:
                self._reasons.add(key)
                out.append("  ↳ 新的失败形态（第 %d 件）：%s"
                           % (self.seen, str(error or status)[:160]))
        if self.abort is None and self.abort_after:
            if self.consec >= self.abort_after:
                self.abort = "连续 %d 件失败" % self.consec
            elif self.seen >= 40 and self.failed * 2 > self.seen:
                self.abort = "失败率过半（%d/%d）" % (self.failed, self.seen)
        if self.seen % self.heartbeat == 0 and self.abort is None:
            el = self.clock() - self.t0
            rate = self.seen / el if el > 0 else 0.0
            left = (self.total - self.seen) / rate if rate else 0.0
            out.append("  … %d/%d 件（成功 %d / 失败 %d）｜已用 %.0fs，按当前速度还需 %.0fs"
                       % (self.seen, self.total, self.placed, self.failed, el, left))
        return out


def retry_hint(stage_label, olds, snapshot_path, prog=None):
    """把"这批失败了"变成一条能直接抄的补做命令（最多列 20 个 old_g）。"""
    ids = sorted({str(x) for x in (olds or []) if x is not None})
    if not ids:
        return "  ↻ 补做：%s 后重跑同一条命令即可续做（按 old_g 跳过已完成的）" % stage_label
    shown = ",".join(ids[:20]) + ("…" if len(ids) > 20 else "")
    tail = " --progress %s" % prog if prog else ""
    return ("  ↻ 补做这一批（%d 个）：python cli/snapshot.py restore %s --apply%s "
            "--only-ids %s%s" % (len(ids), snapshot_path, tail, shown,
                                 "（上面只列前 20 个）" if len(ids) > 20 else ""))


def call_edit_soft(op, args=None, timeout=90):
    """同 `call_edit`，但把 **op 级失败**还原成 `{"error": ...}` 而不抛。

    2026-10-02：`fxapi.call` 的 L2 失败信封改成抛 `RuntimeError`（这是它 docstring 里的
    契约，批量摆放正是靠它把"护栏拒绝整批"当**没试**），但事件/剧情这两个阶段本来就是
    按"读回执里的 `error` 记一条失败、继续跑"写的 —— 继续用 `call_edit` 会让一次
    "事件已存在"把跑完摆放+属性（14884/14988 全绿）的长跑在最后一步打断。
    所以只有这两处用 soft；批量摆放/属性那两个"整批失败=没试"的路径仍走会抛的 call_edit。
    """
    try:
        return call_edit(op, args, timeout=timeout)
    except RuntimeError as exc:
        return {"error": str(exc)}


def save_and_check(token):
    try:
        call_edit("save_map_poll", {"timeout": 0.01}, timeout=30)
    except Exception:
        pass
    r = call_edit("save_map", {"request_id": token}, timeout=90) or {}
    if not r.get("ok"):
        return {"submitted": False, "save_error": r.get("error")}
    for _ in range(SAVE_DRAIN_TRIES):
        time.sleep(0.8)
        try:
            d = call_edit("save_dirty", {}, timeout=60) or {}
        except Exception as exc:
            print("    save_dirty 失败：%s" % str(exc)[:120], flush=True)
            continue
        if d.get("change_units_length") == 0 and d.get("save_events_length") == 0:
            d["submitted"] = True
            return d
    return {"submitted": False, "note": "脏缓冲未排空"}


def save_props_layer(token, log=None):
    """把地图属性层的写入**真正推给服务端**。

    2026-10-01 实机（小图 `2026.9.16`，128 件）：同一份 `ugc_bgm_conf` 写进 props 后
    连着两次 `save_map` 都**没落库**（重进后仍是 null），当时两次的共同点是
    `change_units = 0` —— 一次脏变更集都没有。第三次先做了一次**同值的**
    `edit_transform`（锚点 106 原坐标回写，坐标一个数字没变，只是让引擎登记一条变更），
    带着这条变更保存 ⇒ 重进后读回我写的标记值 ⇒ 落库成立。
    两个变量（真面板方法 / 非空保存）是**一起换的**，还没 bisect，所以这里把两件事
    都做足：先确认有变更集，没有就用一条同值变换登记一次，再保存。

    返回 {ok, dirtied_via, saved}。同值变换挑件的原则：可编辑、不是控件/框架件、
    位置读数正常；实在找不到就不假装 —— 报出来让人知道这次可能又不会落库。
    """
    say = log or (lambda *_a, **_k: None)
    d = call_edit("save_dirty", {}, timeout=60) or {}
    n = d.get("change_units_length")
    via = None
    if not n:                      # 0 与读不到都按"没有变更集"处理（None 曾骗过这条判据）
        tgt = call_edit("edit_targets", {"limit": 400}, timeout=90) or {}
        pick = None
        for t in (tgt.get("targets") or []):
            if not t.get("editable"):
                continue
            ct = t.get("component_type")
            if ct in (200001, 203912, 201009):       # 控件/运动点/锚点别碰
                continue
            pos = (t.get("transform") or {}).get("position") or []
            if len(pos) == 3 and all(isinstance(v, (int, float)) for v in pos):
                pick = t
                break
        if pick is None:
            for t in (tgt.get("targets") or []):      # 兜底：允许用锚点（同值回写不改地图）
                if not t.get("editable"):
                    continue
                pos = (t.get("transform") or {}).get("position") or []
                if len(pos) == 3 and all(isinstance(v, (int, float)) for v in pos):
                    pick = t
                    break
        if pick is None:
            say("  ⚠️ 找不到可登记的件 ⇒ 这次保存可能又是空变更集（落库存疑）")
        else:
            tr = pick.get("transform") or {}
            r = call_edit("edit_transform", {
                "ugc_id": pick.get("ugc_id"),
                "position": tr.get("position"),
                "rotation": tr.get("rotation"),
                "scale": tr.get("scale")}, timeout=60) or {}
            via = {"ugc_id": pick.get("ugc_id"), "component_type": pick.get("component_type"),
                   "ok": bool(r.get("ok"))}
            say("  为了让这次保存**不是空变更集**，对 ugc_id=%s（类型 %s）做了一次"
                "同值变换回写（坐标未变）：%s"
                % (pick.get("ugc_id"), pick.get("component_type"),
                   "成功" if r.get("ok") else "失败"))
    saved = save_and_check(token)
    return {"ok": bool(saved.get("submitted")), "dirtied_via": via, "saved": saved}


def landing_audit(specs, id_map, tol=1.0, chunk=400, log=None):
    """摆放完成后**回读落点**：请求坐标与实际位置逐件比一次。

    为什么单独有一道（2026-10-01 实机）：`place_one` 判的是"这件存在了"（placed 回执 +
    回读新 id），**不判"落在哪"**。14869 件里有一件被引擎放到 248 单位之外、还低 58，
    回执一切正常、件数也对得上，而比对侧当时连 `p` 都不比 ⇒ 两层一起沉默，
    最后是人在试玩里看出"落脚的平台更低"才发现。

    分批把 `{新id: [x,y,z]}` 发给桥、**只回不一致的那批**：桥接一条 eval 的回传会被截到
    200 条，拉整张位置表回来自己比会得到一份假的"全都不对"。

    2026-10-02 两处修正（否则这道审计本身不可信，核位就没有可信工具）：

    * **`<deep>` 截断**：回的 `is` 原先写成 `[[x,y,z],[x,y,z]]`（两种读法各一条），
      套进 `{"bad":[{...}]}` 正好 5 层，回传层的 `_safe(depth>4)` 把它**整片压成
      `<deep>`** —— 审计能报"11/15 件不在请求坐标上"，却说不出一件实际落在哪。
      现在只回**差距最小的那一个**三元组（外加 `alt` / `delta` / `unit_class`）。
    * **全局单元上的组件不算错位**：`UgcGlobalUnit`（运动器 203911 / 音乐事件 /
      剧情…）的组件**位置不在单元上**，真值在 component_info 属性袋里、由属性回写
      阶段写；拿请求坐标比单元的隐藏哨兵只会产生假警报（2026-10-01 那次单笔全量跑
      报的 333 件里 332 件就是这类）。这类单独计入 `exempt_global` 并**显式报出来**
      ——不是"跳过"，也不是"都对"。
    """
    say = log or (lambda *_a, **_k: None)
    want = {}
    for s in specs or []:
        new = (id_map or {}).get(s.get("old_g"))
        pos = s.get("position")
        if new is None or not pos or len(pos) < 3:
            continue
        try:
            want[int(new)] = [float(pos[0]), float(pos[1]), float(pos[2])]
        except (TypeError, ValueError):
            continue
    if not want:
        return {"checked": 0, "off": [], "unreadable": 0, "exempt_global": 0}
    from snapshot import _bridge_dir as _bd
    keys = sorted(want)
    off, unreadable, exempt_global = [], 0, 0
    for i in range(0, len(keys), chunk):
        part = dict((k, want[k]) for k in keys[i:i + chunk])
        code = ("""import sys, math\nsys.path.insert(0, %r)\n"""
                """_w = %r\n_tol = %r\nbad=[]; miss=0; exempt=0\n"""
                """for _g, _p in _w.items():\n"""
                """    _c = cm._ugc_id2component.get(_g)\n"""
                """    _u = getattr(_c, "_owner", None) if _c is not None else None\n"""
                """    if _u is None:\n"""
                """        miss += 1\n        continue\n"""
                """    _cands = []\n"""
                """    for _attr in ("get_position3", "position"):\n"""
                """        try:\n"""
                """            _v = getattr(_u, _attr)\n"""
                """            _v = _v() if callable(_v) else _v\n"""
                """            _cands.append([float(_v.x), float(_v.y), float(_v.z)])\n"""
                """        except Exception:\n"""
                """            pass\n"""
                """    if not _cands:\n"""
                """        miss += 1\n        continue\n"""
                """    # 两种读法有一种对得上就算落对：逻辑件 get_position3 是哨兵、真值在 position，\n"""
                """    # 而摆放请求走的是其中一个 —— 只比一种会把正常件报成错位。\n"""
                """    _ok = any(max(abs(a - b) for a, b in zip(_p, _c)) <= _tol for _c in _cands)\n"""
                """    if _ok:\n"""
                """        continue\n"""
                """    # 全局单元上的组件位置不在单元上（真值在属性袋）⇒ 单列一档，不报错位。\n"""
                """    _cls = type(_u).__name__\n"""
                """    if _cls == "UgcGlobalUnit":\n"""
                """        exempt += 1\n        continue\n"""
                """    # ⛔ 只回一个候选三元组：回 [[x,y,z],[x,y,z]] 是 5 层嵌套，回传层的\n"""
                """    # _safe(depth>4) 会把它整片压成 "<deep>"，读回来的实际坐标全不可读。\n"""
                """    _best = min(_cands, key=lambda c: max(abs(a - b) for a, b in zip(_p, c)))\n"""
                """    bad.append({"ugc_id": _g, "want": _p, "unit_class": _cls,\n"""
                """                "is": [round(v, 2) for v in _best],\n"""
                """                "alt": ([round(v, 2) for v in _cands[1]]\n"""
                """                        if len(_cands) > 1 else None),\n"""
                """                "delta": round(max(abs(a - b)\n"""
                """                                  for a, b in zip(_p, _best)), 2)})\n"""
                """__result__ = {"bad": bad, "miss": miss, "exempt_global": exempt}\n"""
                % (repr(_bd()), part, tol))
        try:
            d = call_edit("eval", {"code": code}, timeout=180) or {}
        except Exception as exc:
            say("  ⚠️ 落点回读这一批没做成（%s）——不许当成「都对」" % str(exc)[:80])
            continue
        node = _find_layer(d, "bad") or {}
        off += node.get("bad") or []
        unreadable += node.get("miss") or 0
        exempt_global += node.get("exempt_global") or 0
    return {"checked": len(want), "off": off, "unreadable": unreadable,
            "exempt_global": exempt_global}


def apply_props(comp_new_gid, comp, id_map, dry=False):
    """整袋回写属性，并把运动器绑定字段的旧 id 换成新 id。

    ⚠️ 摆出来的**全局单元类**组件（运动器 203911 / 音乐事件 203920 / 剧情三件套…）
    不在 `ui.all_ugc_uid_set` 里 —— 登记集合只收"编辑器摆放出来并有独立实体"的物件。
    2026-10-01 实机：春岚的运动器 placed 之后整袋回写被这条护栏拒了
    （「物件不在编辑器登记集合内」），于是**运动层还原成空壳**，而 CLI 只报"失败 1"。
    这与 2026-09-23 单例同步那次是同一堂课：`allow_unregistered` 是这类件的必要出口。
    只在**确实是这条拒绝理由**时升级重试一次，别的错误不动（避免把"还没确认放置"
    这种真故障也一并放行）。
    """
    ci = _info_of(comp)
    events = ci.get("events")
    if not events:
        return {"skipped": True, "reason": "no_events"}
    remap = {str(k): v for k, v in (id_map or {}).items()}
    try:
        from snapshot import ALL_REF_PROPERTIES as _ref
    except Exception:
        _ref = ("10000118701", "10000018701", "20390018701", "20390003001")
    args = {"ugc_id": comp_new_gid, "events": events,
            "expect_component_id": ci.get("component_id"),
            # 换号范围要与验收归一**同一张表**：少一族槽（剧情节点引用）就会把
            # 旧号原样写下去 —— 实机把对话件的「所属剧情」接到了别的东西上。
            "remap_properties": list(_ref),
            "remap": remap, "dry_run": bool(dry)}
    r = call_edit("set_component_info", args, timeout=90) or {}
    err = str(r.get("error") or "")
    if not r.get("ok") and "不在编辑器登记集合" in err:
        r = call_edit("set_component_info",
                      dict(args, allow_unregistered=True), timeout=90) or {}
        if r.get("ok") or r.get("dry_run"):
            r["escalated"] = "allow_unregistered"    # 让调用方看得见这次放宽
    return r


def props_batch_item_args(comp_new_gid, comp, id_map):
    """构造一袋 set_component_info_batch 的入参（与 apply_props 的单件入参同源）。

    remap 换号范围也要与验收归一同一张表（ALL_REF_PROPERTIES）——少一族槽
    （剧情节点引用）就会把旧号原样写下去，见 apply_props 的注。
    """
    ci = _info_of(comp)
    remap = {str(k): v for k, v in (id_map or {}).items()}
    try:
        from snapshot import ALL_REF_PROPERTIES as _ref
    except Exception:
        _ref = ("10000118701", "10000018701", "20390018701", "20390003001")
    return {"ugc_id": comp_new_gid, "events": ci.get("events"),
            "expect_component_id": ci.get("component_id"),
            "remap_properties": list(_ref),
            "remap": remap}


def apply_props_chunk(chunk, id_map):
    """批量属性回写一批：一条 set_component_info_batch 循环写 N 袋。

    `chunk` 是快照组件列表（每件 new_gid = id_map[c.g]，无映射的件不进批）。
    「不在编辑器登记集合」的 allow_unregistered 升级重试由 op **逐件**保留
    （护栏 8），CLI 不再二次升级。返回 {old_g: row}，row 为逐件回执：
    ok / dry_run / error / escalated。
    """
    items, olds = [], []
    for c in chunk:
        new_gid = id_map.get(c.get("g"))
        if new_gid is None:
            continue
        item = props_batch_item_args(new_gid, c, id_map)
        if not item.get("events"):
            continue
        items.append(item)
        olds.append(c.get("g"))
    if not items:
        return {}
    r = call_edit("set_component_info_batch", {"items": items}, timeout=300) or {}
    out = {}
    for row in (r.get("results") or []):
        idx = row.get("index")
        if idx is None or not (0 <= idx < len(olds)):
            continue
        out[str(olds[idx])] = row
    return out


def apply_events(events, id_map, dry=False, only_old=None, comp_index=None,
                 watch=None):
    """按 id 映射重建事件绑定。`only_old` 非空时只补绑这批旧 id 的引用。

    `watch`（`RunWatch`）给上就获得与阶段 1/2 相同的止损：系统性失败时**当场停**，
    并把没轮到的引用数说清楚——事件绑定一条一次往返，一万多条时不能闷头转。
    """
    actions, dangling = event_bind_plan(events, id_map, only_old=only_old,
                                        comp_index=comp_index)
    if dry:
        return {"planned": len(actions), "dangling": dangling, "applied": []}
    created, applied, failed = [], [], []
    # 新建事件按**源图的 sort_id 顺序**发：注册表的 sort_id 是按创建先后分配的，
    # 按引用出现的先后去建，面板顺序就会和源图不同（实机：`春嵐_全片原曲`、
    # `春嵐_点击播放` 两条只有顺序差，引用与保存状态全一致 ⇒ 收口只能等人盖章）。
    # 这里只改发送顺序，不改任何绑定结果。
    _order = {}
    for _r in ((events or {}).get("events") or []):
        if _r.get("event_key") is not None and _r.get("sort_id") is not None:
            _order[_r["event_key"]] = _r["sort_id"]
    if _order:
        actions = sorted(actions, key=lambda a: (
            _order.get(a.get("event_key"), 1 << 30), str(a.get("event_key"))))
    lines, aborted = [], None
    for a in actions:
        key = a["event_key"]
        if key not in created:
            r = call_edit_soft("event_create", {"name": key}, timeout=60) or {}
            created.append(key)
        # ⛔ 不下发 `index`（快照里那一个是**源节点**的本地下标，跨图/跨节点不可移植：
        #    模板槽顺序不同的两个节点，同一个 own_index 指的是不同字段。2026-09-23 实机
        #    「测试」因此被写到 enable(…40008) 而非「设为可用」(…40009)，且槽里恰好放着
        #    同名键 ⇒ key_ok 仍是 true，只有比 property_id 才发现）。定位一律用 property_id。
        args = {"ugc_id": a["ugc_id"], "event_key": key,
                "property_id": a["property_id"],
                "params_idx": a["params_idx"]}
        if a.get("property_id") is None:
            args["index"] = a["index"]  # 兜底：没有 pid 的引用才退回下标
        if a.get("info") is not None:
            args["info"] = a["info"]
        r = call_edit_soft("event_bind", args, timeout=60) or {}
        (applied if r.get("ok") else failed).append(
            {"event_key": key, "ugc_id": a["ugc_id"],
             "old_ugc_id": a.get("old_ugc_id"),
             "src_index": a.get("index"),
             "bound_property_id": r.get("property_id") or a.get("property_id"),
             "located_by": r.get("located_by"),
             "error": r.get("error")})
        if watch is not None:
            lines.extend(watch.record(bool(r.get("ok")), r.get("error")))
            if watch.abort:
                aborted = watch.abort
                break
    return {"created": created, "applied": applied, "failed": failed,
            "dangling": dangling, "lines": lines, "aborted": aborted,
            "untried": max(0, len(actions) - len(applied) - len(failed))}


def verify_event_refs(events, id_map, scope_new_ids=None):
    """还原后核对：目标图里的事件引用与快照**逐条**比对。

    比对单位 = (事件键, 新 ugc_id) → 该组合下 (property_id, params_idx) 的**集合**。

    ⛔ 不能按 (事件键, 新 ugc, params_idx) 单点比：幽灵引用与正确引用可能共用同一个
    params_idx（`(359,8,0)` 幽灵 vs `(359,9,0)` 正确，params_idx 都是 0），后写的那条
    会把前一条盖掉 —— 于是"多余引用"永远查不出来。按集合比就没这个问题。
    ⛔ 也不能只看 `key_ok`：槽里"恰好"放着同名键也会 key_ok=True，但可能落在**另一个
    字段**上（2026-09-23 实机：事件「测试」落在 enable(…40008) 而非「设为可用」(…40009)）。
    ⚠️ 「幽灵」只在**本次纳入判据的对象**里判（`scope_new_ids`，默认取 id_map 的值域）：
    试点只摆几件时，目标图上其余既有物件本来就带着自己的引用，全量比对会把它们
    一律报成幽灵（2026-10-01 实机：试点 2 件后回对报出 6 条"幽灵"，其实是春岚
    自带的运动器/时钟引用）。真多余引用不会因此漏掉 —— 全量还原时所有实物都在 id_map 里。
    """
    expected, unmapped = defaultdict(set), []
    for e in ((events or {}).get("events") or []):
        key = e.get("event_key")
        for r in (e.get("refs") or []):
            old = r.get("ugc_id")
            new = id_map.get(old)
            if new is None:
                unmapped.append({"event_key": key, "ugc_id": old,
                                 "property_id": r.get("property_id")})
                continue
            expected[(str(key), str(new))].add(
                (str(r.get("property_id")), str(r.get("params_idx") or 0)))
    got = call_edit("event_list", {}, timeout=90) or {}
    node = _unwrap(got, "events") or {}
    actual = defaultdict(set)
    for e in (node.get("events") or []):
        key = e.get("event_key")
        for r in (e.get("refs") or []):
            actual[(str(key), str(r.get("ugc_id")))].add(
                (str(r.get("property_id")), str(r.get("params_idx") or 0)))
    missing, pid_mismatch, extra = [], [], []
    out_of_scope = []
    scope = {str(v) for v in (scope_new_ids if scope_new_ids is not None
                              else (id_map or {}).values())}
    matched_n = 0
    for k in sorted(set(expected) | set(actual)):
        exp, act = expected.get(k, set()), actual.get(k, set())
        matched_n += len(exp & act)      # 「一致」按逐条引用算，别被同组的多余引用带偏
        if exp == act:
            continue
        exp_pids = sorted(p for p, _i in exp)
        act_pids = sorted(p for p, _i in act)
        if len(exp) == len(act) and exp_pids != act_pids and not (exp & act):
            # 数量对得上、没有交集、只是点位不同 → 「点位不符」（最隐蔽的一类）
            pid_mismatch.append({"event_key": k[0], "ugc_id": k[1],
                                 "expected_property_id": exp_pids,
                                 "got_property_id": act_pids})
            continue
        for pid, idx in sorted(exp - act):
            missing.append({"event_key": k[0], "ugc_id": k[1],
                            "property_id": pid, "params_idx": idx})
        for pid, idx in sorted(act - exp):
            row = {"event_key": k[0], "ugc_id": k[1],
                   "property_id": pid, "params_idx": idx}
            (extra if k[1] in scope else out_of_scope).append(row)
    return {"ok": not (missing or pid_mismatch or extra or unmapped),
            "expected": sum(len(v) for v in expected.values()),
            "matched": matched_n,
            "missing": missing, "pid_mismatch": pid_mismatch,
            "extra": extra, "unmapped": unmapped,
            "out_of_scope": out_of_scope}


def _print_verify(v):
    """打印回对结果（`restore` 收尾与 `--verify-only` 共用）。"""
    print("\n== 事件引用回对 ==")
    print("  一致 %d / 应有的 %d 条%s"
          % (v["matched"], v["expected"], "   ✅ 全部对上" if v["ok"] else ""))
    if v.get("unmapped"):
        print("  ⚠️ %d 条引用的对象没进 id 映射（没重建？）：%s"
              % (len(v["unmapped"]), v["unmapped"][:3]))
    for m in (v.get("missing") or [])[:6]:
        print("  ⛔ 缺失    %s → ugc=%s @%s[%s]"
              % (m["event_key"], m["ugc_id"], m["property_id"], m["params_idx"]))
    for m in (v.get("pid_mismatch") or [])[:6]:
        print("  ⛔ 点位不符 %s → ugc=%s：应在 %s，实际在 %s"
              % (m["event_key"], m["ugc_id"],
                 m["expected_property_id"], m["got_property_id"]))
    for m in (v.get("extra") or [])[:6]:
        print("  ⚠️ 多余引用（幽灵）%s → ugc=%s @%s[%s]"
              % (m["event_key"], m["ugc_id"], m["property_id"], m["params_idx"]))
    if v.get("out_of_scope"):
        print("  ℹ️ %d 条目标图引用**未纳入本次判据**（它们的物件不在本次 id 映射里 —— "
              "试点只摆几件时属正常，跑全量后会一并核对）"
              % len(v["out_of_scope"]))


def story_node_count(doc):
    """快照里要建多少剧情侧节点（剧情 + 对话 + 选项）。

    用来判"目标图是不是已经有这批节点了"——`story_import` 不查重，重跑一次就翻倍，
    而这三件套不占 `component_nums`，件数上根本看不出来。
    """
    d = doc or {}
    stories = d.get("stories") or []
    dialogs = sum(len(s.get("dialogues") or []) for s in stories)
    choices = sum(len(x.get("choices") or [])
                  for s in stories for x in (s.get("dialogues") or []))
    return len(stories) + dialogs + choices


def apply_story(doc, dry=False):
    """导入剧情，返回 `story_id_map`（源图 ugc_id → 本图新 ugc_id）。

    第一遍事件绑定跑在剧情之前，所以指向剧情/选项节点的事件引用当时一定悬空；
    这里把导入过程里 `story_import_next` 逐节点回传的 path→new ugc_id 收齐，
    交给调用方并进总映射做第二遍补绑。

    ⚠️ 同时收集逐节点的**字段写入告警**（`fields_error` / `encode_bad`）：剧情节点创建
    成功但字段没写全，本地看不出区别，却会让节点缺模板槽 → 事件补绑装不下。
    2026-09-23 实机（源图 edit_time="") 就是这么静默丢了 1 条引用，所以这里必须回传。
    """
    r = call_edit_soft("story_import", {"data": doc, "dry_run": bool(dry)}, timeout=90) or {}
    if dry or r.get("dry_run"):
        r["story_id_map"] = {}
        return r
    # 异步创建链：逐个推进（每次都回传累计 created / id_map）
    # ⚠️ 终止条件用 `done`，不要用「没有 request_id」：op 在「完成/出错」两种情况下
    #    返回体里的 request_id 仍是**上一个**请求的 id，按它判断会多轮几次甚至空转。
    steps = 0
    last = {}
    field_issues = []
    while steps < 400:
        nxt = call_edit_soft("story_import_next", {}, timeout=90) or {}
        last = nxt
        steps += 1
        err = nxt.get("fields_error")
        if err:
            field_issues.append({"node": nxt.get("index"), "error": err})
        fa = nxt.get("fields_applied") or {}
        if isinstance(fa, dict):
            if fa.get("ok") is False:
                field_issues.append({"node": nxt.get("index"),
                                     "error": fa.get("error") or "fields_applied.ok=false"})
            for bad in (fa.get("encode_bad") or []):
                field_issues.append({"node": nxt.get("index"),
                                     "skipped_field": bad.get("field"),
                                     "error": bad.get("error")})
            for sk in (fa.get("skipped_empty") or []):
                field_issues.append({"node": nxt.get("index"),
                                     "skipped_field": sk.get("field"),
                                     "error": "空值跳过"})
        if nxt.get("done") or nxt.get("error"):
            break
        time.sleep(0.3)
    r["import_next_steps"] = steps
    smap = last.get("id_map") or {}
    if not smap and last.get("created"):
        # 兜底：老版本 op 不回 id_map 时，用文档 path 自己求交
        smap = _story_id_map_from_created(snap_doc=doc, created=last["created"])
    r["story_id_map"] = smap
    r["created_count"] = len(last.get("created") or [])
    r["import_done"] = bool(last.get("done"))
    r["field_issues"] = field_issues
    return r


def _doc_node_paths(doc):
    """文档 → [(ugc_id, path, label)]；path 规则与 bridge 的 `_flatten` **逐字一致**。"""
    out = []
    for si, s in enumerate(doc.get("stories") or [], 1):
        out.append((s.get("ugc_id"), "story#%d" % si, s.get("name")))
        for di, d in enumerate(s.get("dialogues") or [], 1):
            out.append((d.get("ugc_id"), "story#%d/dialogue#%d" % (si, di),
                        d.get("content")))
            for ci, c in enumerate(d.get("choices") or [], 1):
                out.append((c.get("ugc_id"),
                            "story#%d/dialogue#%d/choice#%d" % (si, di, ci),
                            c.get("content")))
    return out


def _tree_node_paths(tree):
    """目标图当前剧情树 → {path: (ugc_id, label)}（与上面的 path 规则同源）。"""
    out = {}
    for si, s in enumerate((tree or {}).get("stories") or [], 1):
        out["story#%d" % si] = (s.get("ugc_id"), s.get("name"))
        for di, d in enumerate(s.get("dialogues") or [], 1):
            out["story#%d/dialogue#%d" % (si, di)] = (d.get("ugc_id"), d.get("content"))
            for ci, c in enumerate(d.get("choices") or [], 1):
                key = "story#%d/dialogue#%d/choice#%d" % (si, di, ci)
                out[key] = (c.get("ugc_id"), c.get("content"))
    return out


def story_map_from_tree(doc, tree):
    """按**结构位置**把文档里的剧情节映射到目标图当前节点（回对 / 定点修复用）。

    ⚠️ 位置匹配必须带内容校验（story 比 name、dialogue/choice 比 content）：目标图节点
    顺序或内容不同时**宁可不映射**（回对报 unmapped），也不能映射错 —— 映射错的后果是把
    事件绑到别的节点上。不匹配的条目在第二个返回值里报出。
    """
    tps = _tree_node_paths(tree)
    out, mismatched = {}, []
    for old, path, label in _doc_node_paths(doc or {}):
        got = tps.get(path)
        if not got or got[0] is None or old is None:
            continue
        new, new_label = got
        if label and new_label and str(label) != str(new_label):
            mismatched.append({"path": path, "doc": label, "target": new_label})
            continue
        out[str(old)] = new
    return out, mismatched


def fetch_story_tree():
    """当前地图的剧情树（层数不固定 ⇒ 用剥壳器找 `stories`）。"""
    r = call_edit("story_tree", {}, timeout=90) or {}
    node = _unwrap(r, "stories")
    if isinstance(node, list):
        return {"stories": node}
    return node or {}


def _story_id_map_from_created(snap_doc, created):
    """兜底求交：文档里节点 ugc_id → path（本地复刻 `_flatten` 的编号规则）→ new id。

    ⛔ path 规则只允许有一处实现（`_doc_node_paths`），否则映射会错位
    （错位的后果是把事件绑到别的剧情节点上）。
    """
    by_path = {c["path"]: c.get("ugc_id") for c in (created or [])
               if c.get("ugc_id") is not None}
    out = {}
    for old, path, _label in _doc_node_paths(snap_doc or {}):
        new = by_path.get(path)
        if old is not None and new is not None and new != old:
            out[str(old)] = new
    return out


# ---------------------------------------------------------------------------
# 计划 / 主流程
# ---------------------------------------------------------------------------
def parse_int_list(raw):
    """'203911,203900' → {203911, 203900}。空值返回 None（表示不限制）。"""
    if raw is None or raw == "":
        return None
    out = set()
    for tok in str(raw).replace(" ", "").split(","):
        if not tok:
            continue
        if not tok.lstrip("-").isdigit():
            raise ValueError("--only-* 只能是逗号分隔的整数：%s" % tok)
        out.add(int(tok))
    return out or None


def covered_components(specs):
    """计划/清单覆盖的**快照件数**。

    成组创建的类型一个请求摆出多件（`group_old_gs` 记成员），按请求数算会少报，
    预算与「筛掉多少件」都因此要用这个口径。
    """
    return sum(1 + len(s.get("group_old_gs") or []) for s in specs or [])


def select_specs(specs, only_types=None, only_ids=None):
    """试点选择：只保留指定 component_type / 快照 ugc_id 的件。

    两个条件同时给时取**交集**（"在这些类型里只搬这几件"）。
    ⚠️ 成组创建的成员并进了主件请求 ⇒ `--only-ids` 命中任一成员都要保留该请求，
    否则 `--only-ids 14993`（成员）会把整对检查点筛没。
    返回 (保留清单, 被筛掉的件数)。
    """
    if not only_types and not only_ids:
        return specs, 0
    kept = []
    for s in specs:
        ct = s.get("component_type")
        try:
            ct = int(ct)
        except (TypeError, ValueError):
            pass
        if only_types and ct not in only_types:
            continue
        ids = {s.get("old_g")} | set(s.get("group_old_gs") or [])
        if only_ids and not (ids & only_ids):
            continue
        kept.append(s)
    return kept, covered_components(specs) - covered_components(kept)


DANGLING_FIX_HINTS = {
    "story_stage": "--with-story（剧情节点重建后按 path 补绑）",
    "sentinel_position": "--allow-sentinel（运动器/音乐事件等逻辑件默认整批不摆）",
    "ui": "--include-ui（⚠️ 先看预检那行的净缺口：每张新图自带同样一批控件，"
          "空白图上这一层不缺）",
    "motion_point": "⛔ 不该摆：运动点每图自带正好 50 个（框架件，不是作者内容），"
                    "`--include-motion` 只会让目标图多出一层重复点。引用指向它 ⇒ "
                    "是那些**运动器**这次没摆（逻辑件默认跳过），要它加 --allow-sentinel",
    "global_singleton": "--sync-global（就地覆写目标图自带单例）",
    "skeleton": "--include-global（地图骨架自带，一般不该要）",
    "被 --limit 截断": "正常：那是 --limit 截的那一刀，去掉 --limit 跑全量时会摆出来并补绑",
    "目标图已有同内容": "不用管：目标图上那件**已经在**（出厂自带或上一跑摆的），"
                        "引用按它的已有号补绑，不是缺口",
    "deleted_residue": "不用管：那是删除后仍留在索引里的撤销栈残骸，重进地图即消失"
                           "（不是缺口，摆出来才会变幽灵件）",
    "not_placeable": "⚠️ 可试点：--include-not-placeable 会带着 allow_unverified 摆（角色/推荐检查点这类，服务端受理但阀门无实测记录，成败要重进回对）",
    "未被试点过滤选中": "正常：去掉 --only-* 跑全量时会摆出来并补绑",
}


def dangling_advice(dangling, reason_by_g):
    """悬空引用**按跳过原因**分组给出出口。[(原因, 条数, 出口), ...]

    ⚠️ 春岚实测 234 条悬空里 232 条指向运动器（sentinel_position）。旧版一律提示
    "加 --with-story 会补绑" —— 那条路对运动器无效，会让人以为已经救回来了。
    `snapshot_audit` 与本文件共用这一处定义，避免两边口径漂移。
    """
    by = {}
    for d in dangling or []:
        g = d.get("old_ugc_id")
        reason = reason_by_g.get(g, "不在本次计划里（源图已无此组件？）")
        key = str(reason).split("（")[0]
        by[key] = by.get(key, 0) + 1
    return [(k, n, DANGLING_FIX_HINTS.get(k, "未识别，对照「跳过原因」判断"))
            for k, n in sorted(by.items(), key=lambda kv: -kv[1])]


def _skip_reason_index(snap, plan, specs, only=None, limit=None, overlapped=None):
    """旧 ugc_id → 「这次为什么不摆」。

    三类都得单独成行，否则出口话术会把人带偏：
    * `--only-*` 筛掉 → "去掉 --only-* 跑全量会补绑"（对）
    * `--limit` 截断 → 同样会被补绑（以前也印成 `--only-*`，让人去动一个根本没带的开关）
    * **目标图已有同内容**（`stock_for_stages` 扣掉的出厂件）→ 它不是"没摆"，
      而是**已经在图上**，引用按那个已有号补绑；印成"没选中"会让人以为还缺一件
    """
    reason = {s.get("g"): s.get("reason")
              for s in ((plan or {}).get("skipped") or [])}
    selected = {s.get("old_g") for s in (specs or [])}
    for s in (specs or []):
        # 成组创建的成员并进了主件请求，它们也算"这次要摆的"
        selected.update(s.get("group_old_gs") or [])
    ov = set(overlapped or {})
    for c in (snap.get("components") or []):
        g = c.get("g")
        if g is None or g in reason or g in selected:
            continue
        if g in ov:
            reason[g] = "目标图已有同内容（出厂件，不重复摆）"
        elif only:
            reason[g] = "未被试点过滤选中（--only-*）"
        elif limit is not None:
            reason[g] = "被 --limit 截断（本次只摆前 N 件）"
        else:
            reason[g] = "不在本次计划里（源图已无此组件？）"
    return reason


def bag_ref_dangling(snap, specs):
    """属性袋里指向**本次不会重建的组件**的 id 引用。

    为什么要有这条：还原写袋时只换 `id_map` 里的旧号（`UGCID_REF_PROPERTIES` 那几张成员表），
    引用落在跳过名单里 ⇒ 那个数字**原样写进目标图**，而目标图上同号的是**另一个组件**
    ⇒ 静默接错线，袋里看着"有个 id"、回读也过，只有重进比对才暴露。事件层早就有悬空预测
    （`event_bind_plan` 的 dangling），袋层以前一条都没有 —— 2026-10-01 我手工复原组合件
    时撞的就是这一族的反面（成员表被写成空/旧号）。

    返回 `[{owner, owner_item, property_id, ref, ref_item, in_snapshot, reason}]`。
    """
    from snapshot import ID_REF_PROPERTIES
    placed = set()
    for s in (specs or []):
        if s.get("old_g") is not None:
            placed.add(int(s["old_g"]))
        for g in (s.get("group_old_gs") or []):
            placed.add(int(g))
    comps = [c for c in (snap.get("components") or []) if not c.get("del")]
    by_g = {int(c["g"]): c for c in comps if c.get("g") is not None}

    def _refs(row):
        ci = row.get("ci")
        try:
            ci = json.loads(ci) if isinstance(ci, str) else (ci or {})
        except Exception:
            return
        for e in (ci.get("events") or []):
            pid = str(e.get("type"))
            if pid not in ID_REF_PROPERTIES:
                continue
            stack = [e.get("info")]
            while stack:
                x = stack.pop()
                if isinstance(x, (list, tuple)):
                    stack.extend(x)
                elif isinstance(x, int) and not isinstance(x, bool):
                    yield pid, x

    out = []
    for row in comps:
        if int(row["g"]) not in placed:
            continue
        for pid, ref in _refs(row):
            if ref in placed:
                continue
            target = by_g.get(ref)
            out.append({"owner": int(row["g"]), "owner_item": row.get("i"),
                        "property_id": pid, "ref": ref,
                        "ref_item": (target or {}).get("i"),
                        "in_snapshot": target is not None})
    return out


def bag_ref_dangling_note(dangling, reason_index, limit=6):
    """把悬空的袋引用说成一句可执行的话（按"为什么不摆"分组，组数封顶）。"""
    if not dangling:
        return None
    by_reason = defaultdict(list)
    for d in dangling:
        by_reason[reason_index.get(d["ref"]) or "不在这份快照里（文件外引用）"].append(d)
    lines = ["属性袋里有 %d 处引用指向**本次不重建**的组件 ⇒ 那个旧号会原样写进目标图，"
             "而目标图上同号的是别的组件（静默接错线）：" % len(dangling)]
    ordered = sorted(by_reason.items(), key=lambda kv: -len(kv[1]))
    for reason, items in ordered[:limit]:
        ex = ", ".join("%s→%s" % (i["owner"], i["ref"]) for i in items[:3])
        lines.append("  ×%-3d 因为「%s」；例：%s" % (len(items), reason, ex))
    if len(ordered) > limit:
        lines.append("  …另有 %d 类原因" % (len(ordered) - limit))
    lines.append("  出路：让这批件进入计划（`--include-ui` / `--include-motion` / "
                 "`--include-global` / 放开 `--only-*`）；确认无害才 `--allow-dangling-refs`")
    return "\n".join(lines)


# 出厂自带层的判据：这几类不是"快照有、目标图没有"的缺口，而是**每张新图都有的一份默认件**
# （2026-10-01 实机，樱花残羽艾玛的地图 = 出厂新图：界面控件 48 件与春岚那 48 件
# 按属性袋逐件比全同；角色 1 件也全同于春岚的 4 件之一）。以前闸门无条件催
# `--include-ui` / `--include-not-placeable`，照着催就会在目标图上**再插一层**——
# 与 `--include-motion` 那次把出厂运动点当成缺口是同一类自伤。
STOCK_AWARE_STAGES = ("ui", "not_placeable")
STOCK_CENSUS_MAX = 400          # 目标图这类件超过这个数就不再抓（一张图不可能自带这么多）


def fetch_target_type_rows(types, timeout=120):
    """只抓目标图里这几类 component_type 的属性袋（判"这一层是不是已经有了"）。

    为什么不整表抓：全图一万四千件的采集要几十分钟，而判据只需要这几类
    （实机：新图界面控件 48 件、一次调用 43 KB）。
    """
    try:
        from snapshot import CODE_TYPE_CENSUS as _cc, _bridge_dir as _bd
    except Exception:
        return None
    types = sorted(int(t) for t in (types or []))
    if not types:
        return []
    code = _cc.replace("%BRIDGE%", repr(_bd())).replace("%TYPES%", repr(types))
    try:
        d = call_edit("eval", {"code": code}, timeout=timeout)
    except Exception:
        return None
    node = _find_layer(d, "rows")
    if not isinstance(node, dict) or "rows" not in node:
        return None
    return node.get("rows") or []


def _sig_no_ids(c):
    """组件的**内容签名**：属性袋整袋（丢掉存落盘 ugc_id 的字段）+ 位置/旋转/缩放。

    与 `cli/snapshot.py::ID_REF_PROPERTIES` 同源，判"目标图已经有这一件"才能和
    验收比对口径一致 —— 否则新图自己那批件的号会把它认成"内容不同"。
    ⚠️ 变换必须在签名里：作者把出厂角色挪过位置后，属性袋照样全同，只比袋就会
    把他那件当成"目标图已有"而漏摆 ⇒ 目标图留下的是摆错地方的那一件（比多一件更糟）。
    """
    try:
        from snapshot import ID_REF_PROPERTIES as _ref
        drop = set(str(x) for x in _ref)
    except Exception:
        drop = set()

    def _prune(x):
        if isinstance(x, dict):
            return {k: _prune(v) for k, v in x.items() if str(k) not in drop}
        if isinstance(x, list):
            return [_prune(v) for v in x]
        return x

    return json.dumps([_prune(_info_of(c)), c.get("p"), c.get("r"), c.get("s")],
                      ensure_ascii=False, sort_keys=True, default=str)


def content_overlap(snap_comps, target_rows, types):
    """快照里"目标图已经有同样内容"的那批件（按签名做**多重集**相减）。

    多重集而不是集合：目标图有 1 件同内容就只抵掉快照里的 1 件，快照自己那
    3 件重复的仍要摆 —— 用集合抵会把作者的重复件吃掉。
    返回 `[(快照旧g, component_type, 目标图上那件的 g)]`：第三项要给 id 映射用，
    扣掉的件不是"不建"，而是**已经建好在那儿**，指向它的引用必须换成那个号。
    """
    pool = {}
    for r in target_rows or []:
        cid = r.get("i")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if cid not in types:
            continue
        pool.setdefault(_sig_no_ids(r), []).append((cid, r.get("g")))
    out = []
    for c in snap_comps or []:
        try:
            cid = int(component_id_of(c))
        except (TypeError, ValueError):
            continue
        if cid not in types:
            continue
        bank = pool.get(_sig_no_ids(c))
        if bank:
            out.append((c.get("g"),) + bank.pop(0))
    return out


def _cid_int(c):
    try:
        return int(component_id_of(c))
    except (TypeError, ValueError):
        return None


def _type_dist_counts(td):
    """`{"UI_NATIVE(200001)": 48}` → `{200001: 48}`（目标图类型分布，来自 map_info）。"""
    out = {}
    for k, v in (td or {}).items():
        code = str(k)
        if "(" in code and code.endswith(")"):
            code = code[code.rindex("(") + 1:-1]
        try:
            out[int(code)] = int(v or 0)
        except (TypeError, ValueError):
            continue
    return out


def ids_live_on_target(ids, timeout=90):
    """逐个点验这些 ugc_id **此刻**还在不在当前图上，返回还在的个数。

    ⚠️ 不能用 `fetch_live_index()` 判：那条通道回传的 dict 被桥接截到 200 条（实机
    n=11559 而 len=200），拿 200 个样本去查一张一万一千件的图，命中率天生就低
    —— 我自己写的新鲜度闸门就是这么把一次**合法**的续做误拒掉的（2026-10-01）。
    这里改成把号发给桥、让它按 `_ugc_id2component` 直接回答。
    """
    want = [int(x) for x in list(ids)[:24] if x is not None]
    if not want:
        return None
    try:
        from snapshot import _bridge_dir as _bd
    except Exception:
        return None
    code = ("import sys\nsys.path.insert(0, %s)\n_w = %r\n"
            '__result__ = {"hit": [i for i in _w if i in cm._ugc_id2component]}'
            % (repr(_bd()), want))
    try:
        d = call_edit("eval", {"code": code}, timeout=timeout)
    except Exception:
        return None
    node = _find_layer(d, "hit") or {}
    return len(node.get("hit") or [])


def stock_for_stages(snap, plan, placeable_types, target_info):
    """对着**目标图现状**算每层还净缺多少（返回 (每层净数, 要从计划里扣掉的旧 g)）。

    扣件的范围 = 目标图**已经有**、且数量 ≤ `STOCK_CENSUS_MAX` 的那些类（也就是出厂件：
    控件 / 默认角色 / 103002·203930 这类"客户端阀门根本不给摆"的编辑器件），
    再排掉走 `--sync-global` 写袋路径的全局单例与剧情件。
    2026-10-01 实机：新图 113 件逐件与春岚比下来全是出厂默认件，而闸门当时无条件
    写「UI_NATIVE → `--include-ui` 48 处缺口」；照它带上开关就是在目标图上再插一层。
    ⚠️ 判据取自**快照内容**，不取自 plan 的跳过原因：带了 `--include-ui` 时那批件
    已经不在跳过原因里了，而恰恰是带了开关才最需要扣掉重复。
    目标图那类一件都没有 ⇒ 整层都是真缺口，省掉采集；读不到目标图 ⇒ 返回空，
    退回老口径（但 note 里写明"没对着目标图判"）。
    """
    comps = snap.get("components") or []
    cids = [_cid_int(c) for c in comps]
    td = _type_dist_counts((target_info or {}).get("type_distribution"))
    cand = set(t for t in cids
               if t is not None and t < GLOBAL_TYPE_FLOOR
               and t not in SKIP_STORY_TYPES
               and 0 < td.get(t, 0) <= STOCK_CENSUS_MAX)
    stage_types = {}
    if any(cid in SKIP_UI_TYPES for cid in cids):
        stage_types["ui"] = sorted(SKIP_UI_TYPES)
    if placeable_types:
        ts = sorted(set(cid for cid in cids
                        if cid is not None and cid not in placeable_types
                        and cid not in SKIP_STORY_TYPES and cid not in SKIP_UI_TYPES
                        and cid not in SKIP_MOTION_TYPES
                        and cid not in SKIP_SKELETON_TYPES
                        and cid < GLOBAL_TYPE_FLOOR))
        if ts:
            stage_types["not_placeable"] = ts
    if not cand and not stage_types:
        return {}, {}
    want = cand | set(t for v in stage_types.values() for t in v)
    fetch_types = set(t for t in want if 0 < td.get(t, 0) <= STOCK_CENSUS_MAX)
    rows = fetch_target_type_rows(fetch_types) if fetch_types else []
    if rows is None:
        return {}, {}
    excluded = dict((g, (cid, tg)) for g, cid, tg in content_overlap(comps, rows, cand))
    stock = {}
    for stage, types in stage_types.items():
        ts = set(types)
        sub_snap = [c for c in comps if _cid_int(c) in ts]
        n_ov = len(content_overlap(sub_snap, rows, ts))
        stock[stage] = {"snap_n": len(sub_snap), "on_target": n_ov,
                        "net": len(sub_snap) - n_ov, "types": sorted(ts)}
    return stock, excluded


# 跳过原因 → 「要 1:1 就得开哪个开关」。`STAGE_NEEDS_NONE` 表示这类**本来就不该由快照重建**，
# 催用户去开那个开关反而是伤害（见下面运动点那条证据）。
# 文案与 `classify_components` 里的 reason 前缀一一对上。
STAGE_NEEDS = {
    "story_stage": ("剧情层", "--with-story"),
    "ui": ("技能控件 UI_NATIVE", "--include-ui"),
    "not_placeable": ("白名单外类型（角色/推荐检查点）", "--include-not-placeable"),
    "sentinel_position": ("无真实坐标的逻辑件", "--allow-sentinel"),
}
# 不该摆的三类。运动点这条有实机证据：`gpt6和风街区`（0 台运动器、361 件）、
# `樱花残羽艾玛的地图`（6 台运动器）、春岚（332 台）里 203912 **都是正好 50 个**
# ⇒ 它是每张图自带的框架件，不是作者摆的内容。真去 `--include-motion` 会在目标图上
# 多出 50 件（它本来就有自己那 50 个）—— 所以这里不能把催成"缺口"。
STAGE_NEEDS_NONE = ("skeleton", "global_singleton", "deleted_residue", "motion_point")


def stage_requirements(snap, plan, args, stock=None):
    """这份快照要 1:1 还成什么样、当前命令漏了哪一层（写之前就要说清）。

    为什么要有：B 账号那一跑要摆一万四千件、几十分钟，跑完才发现"剧情层是空的"或
    "少摆 48 件控件"就等于白跑一次（还要重来或手工补）。以前这些只在手册正文里写着，
    命令行少打一个开关没有任何东西会拦。

    `stock`（`stock_for_stages` 的结果）把「快照有 48 件」和「目标图缺 48 件」分开：
    出厂自带的那批不该催成缺口。没给（离线/读不到目标图）时退回老口径，
    但会在 note 里写明"没对着目标图判"。
    """
    out = []
    counts = dict((plan or {}).get("skipped_by_reason") or {})
    for reason, n in sorted(counts.items()):
        key = reason.split("（", 1)[0]
        if key in STAGE_NEEDS_NONE or key not in STAGE_NEEDS:
            continue
        label, flag = STAGE_NEEDS[key]
        attr = flag.lstrip("-").replace("-", "_")
        st = (stock or {}).get(key)
        net = n if st is None else st["net"]
        item = {"stage": label, "flag": flag, "count": net,
                "satisfied": bool(getattr(args, attr, False)) or net == 0}
        if st and st["on_target"]:
            item["note"] = ("目标图已有同内容的 %d 件（出厂自带，不是作者摆的）⇒ "
                            "净缺 %d 件；带 %s 也只补差额，重复的那 %d 件会从计划里扣掉"
                            % (st["on_target"], net, flag, st["on_target"]))
        elif st is None and key in STOCK_AWARE_STAGES:
            item["note"] = "（没对着目标图判：读不到目标图这几类件，按快照全量催开关）"
        out.append(item)
    ms = ((snap.get("map") or {}).get("map_setting") or {})
    gameplay = [k for k in ms if str(k).isdigit() and str(k) != FRAMEWORK_SETTING_KEY]
    if gameplay:
        out.append({"stage": "玩法设置（%d 个键）" % len(gameplay),
                    "flag": "--sync-settings", "count": len(gameplay),
                    "satisfied": bool(getattr(args, "sync_settings", False))})
    nsingles = count_global_singletons(snap)
    if nsingles:
        out.append({"stage": "全局单例观感（天空/雾/LUT…）", "flag": "--sync-global",
                    "count": nsingles,
                    "satisfied": bool(getattr(args, "sync_global", False))})
    st = snap.get("story") or {}
    if st.get("stories") and not any(x["stage"] == "剧情层" for x in out):
        out.append({"stage": "剧情层", "flag": "--with-story",
                    "count": len(st.get("stories") or []),
                    "satisfied": bool(getattr(args, "with_story", False))})
    return out


def count_global_singletons(snap):
    """快照里带全局单例编号（≥ GLOBAL_TYPE_FLOOR、非剧情三类）的件数。"""
    n = 0
    for c in (snap.get("components") or []):
        try:
            cid = int(component_id_of(c))
        except (TypeError, ValueError):
            continue
        if cid >= GLOBAL_TYPE_FLOOR and cid not in SKIP_STORY_TYPES:
            n += 1
    return n


def missing_stage_note(reqs):
    """把缺口写成一句可执行的话（补齐开关，或显式声明这次不要这一层）。"""
    gaps = [r for r in reqs if not r["satisfied"]]
    if not gaps:
        return None
    lines = ["要 1:1 还缺这些层（当前命令少带了开关；不补就跑完会**少一整层**）："]
    for r in gaps:
        lines.append("  %-28s %6d 处   缺 %s" % (r["stage"], r["count"], r["flag"]))
    lines.append("  明知这次不要这些层，就加 --allow-missing-stages 再跑（会在结果里记下少摆的量）")
    return "\n".join(lines)


def build_plan(snap, placeable_types=None, args=None):
    args = args or argparse.Namespace(include_ui=False, include_motion=False,
                                      allow_sentinel=False, with_story=False)
    comps = snap.get("components") or []
    to_place, skipped = classify_components(
        comps, placeable_types,
        include_ui=getattr(args, "include_ui", False),
        include_motion=getattr(args, "include_motion", False),
        allow_sentinel=getattr(args, "allow_sentinel", False),
        include_global=getattr(args, "include_global", False),
        include_not_placeable=getattr(args, "include_not_placeable", False))
    specs = [place_spec(c) for c in to_place]
    # 放行来的白名单外类型，摆放要显式带 allow_unverified（`plan()` 那侧的阀门），
    # 否则 place_item 会在预演阶段就拒掉。判据用 placeable_types，不靠跳过原因，
    # 免得 `--include-*` 组合变化时标记漂移。
    if placeable_types is not None:
        for s in specs:
            try:
                if int(s["component_type"]) not in placeable_types:
                    s["allow_unverified"] = True
            except (TypeError, ValueError):
                s["allow_unverified"] = True
    # 成组创建（204041 推荐检查点这类：一次 add_unit 出 2 件、引擎自动写互指的
    # group_components）⇒ 把成员并到主件请求上，否则各发一次会摆出翻倍件数。
    specs = collapse_group_creates(specs, {c.get("g"): c for c in to_place})
    only_types = parse_int_list(getattr(args, "only_types", None))
    only_ids = parse_int_list(getattr(args, "only_ids", None))
    specs, only_dropped = select_specs(specs, only_types, only_ids)
    events = snap.get("events") or {}
    return {
        "total_components": len(comps),
        "to_place": covered_components(specs),
        "skipped": skipped,
        "skipped_by_reason": _count_reason(skipped),
        "only": ({"types": sorted(only_types or ()), "ids": sorted(only_ids or ()),
                  "dropped": only_dropped}
                 if (only_types or only_ids) else None),
        "specs": specs,
        "events": {"count": events.get("count", 0),
                   "refs": sum(len(e.get("refs") or [])
                               for e in (events.get("events") or []))},
        "story": {"stories": len((snap.get("story") or {}).get("stories") or []),
                  "enabled": bool(getattr(args, "with_story", False))},
    }


def _count_reason(skipped):
    out = {}
    for s in skipped:
        key = s["reason"].split("（")[0]
        out[key] = out.get(key, 0) + 1
    return out


def print_plan(plan, snap, limit=12):
    print("快照：%s（%s）" % ((snap.get("map") or {}).get("name") or "?",
                              snap.get("captured_at")))
    print("组件 %d → 可摆 %d ｜ 跳过 %d"
          % (plan["total_components"], plan["to_place"], len(plan["skipped"])))
    if plan.get("only"):
        o = plan["only"]
        print("试点选择：component_type=%s ugc_id=%s ⇒ 又从可摆清单里筛掉 %d 件"
              % (o["types"] or "不限", o["ids"] or "不限", o["dropped"]))
    if plan["skipped_by_reason"]:
        print("跳过原因：%s" % ", ".join("%s×%d" % kv
                                         for kv in sorted(plan["skipped_by_reason"].items())))
    print("事件 %d 条 / %d 条引用 ｜ 剧情 %d 条（%s）"
          % (plan["events"]["count"], plan["events"]["refs"],
             plan["story"]["stories"],
             "启用" if plan["story"]["enabled"] else "默认关闭，需 --with-story"))
    print("\n前 %d 件待摆：" % min(limit, len(plan["specs"])))
    for s in plan["specs"][:limit]:
        print("  item=%-8s pos=%s rot=%s scale=%s"
              % (s["item_id"], [round(v, 1) for v in s["position"]],
                 s["rotation"], s["scale"]))
    if len(plan["specs"]) > limit:
        print("  ...（共 %d 件）" % len(plan["specs"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="snapshot restore", description="快照 → 目标图重建")
    ap.add_argument("snapshot")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=None,
                    help="只处理前 N 件。不给=全量；给 0=一件都不摆（只跑预检与写前闸门）。"
                         "⚠️ 早先默认是 0 且代码写 `if a.limit:`，于是"
                         "`--limit 0` 反而等于全量（2026-10-01 差点按 14881 件去摆）")
    ap.add_argument("--batch", type=int, default=50, help="每多少件保存一次")
    ap.add_argument("--placement-batch", type=int, default=0,
                    help="批量摆放：每批件数（0=默认，走旧的逐件路径）。1-128；"
                         "一条命令登记整批、批量 poll 收尾，往返从每件 2-3 次"
                         "摊薄为每批 2-3 次（引擎吞吐原型实测见 "
                         "probe/_batch_place_bench.md）。保存节奏随批（每批一次）")
    ap.add_argument("--props-batch", type=int, default=0,
                    help="批量属性回写：每批件数（0=默认，走旧的逐件路径）。"
                         "建议 30-50（一条命令循环写 N 袋，逐件护栏与升级重试"
                         "都保留；太大单条 eval 回传会截断/主线程回调变长）")
    ap.add_argument("--abort-after", type=int, default=12,
                    help="连续这么多件摆放失败就停下（系统性失败时别再空转几小时；"
                         "0 = 不早停）。停下时已摆的会照常保存，重跑同一条命令即续做")
    ap.add_argument("--heartbeat", type=int, default=200,
                    help="每多少件播报一次进度与预计剩余时间")
    ap.add_argument("--no-props", action="store_true", help="跳过属性回写")
    ap.add_argument("--no-events", action="store_true", help="跳过事件重建")
    ap.add_argument("--with-story", action="store_true", help="重建剧情（异步链，较慢）")
    ap.add_argument("--force-story-import", action="store_true",
                    help="明知目标图已有部分剧情节点仍再导一遍（会在目标图上翻倍，"
                         "默认拒绝；半截导入请改用 `python cli/story.py resume` 续跑）")
    ap.add_argument("--include-ui", action="store_true", help="连界面控件一起摆")
    ap.add_argument("--include-motion", action="store_true", help="连运动点一起摆")
    ap.add_argument("--allow-sentinel", action="store_true",
                    help="位置是哨兵的逻辑组件也摆（落在 y=0）")
    ap.add_argument("--allow-same-map", action="store_true",
                    help="明知当前打开的就是快照的来源图，仍要往它自己身上再摆一遍"
                         "（默认拒绝：会把整张图复制一份，只能靠批量删除收拾）")
    ap.add_argument("--allow-dangling-refs", action="store_true",
                    help="明知属性袋里有引用指向**本次不重建**的组件，仍继续写"
                         "（默认拒绝：那个旧号会原样落到目标图，同号是别的组件 = 静默接错线）")
    ap.add_argument("--allow-missing-stages", action="store_true",
                    help="明知这次少带了 --with-story / --sync-settings / --include-ui 这类开关、"
                         "目标图会**少一整层**，仍要继续（默认拒绝：跑完几十分钟才发现问题更贵）")
    ap.add_argument("--only-types", default=None,
                    help="试点：只搬这些 component_type（逗号分隔，如 203911,203900）")
    ap.add_argument("--only-ids", default=None,
                    help="试点：只搬这些快照 ugc_id（逗号分隔；与 --only-types 同时给取交集）")
    ap.add_argument("--include-global", action="store_true",
                    help="连全局单例/骨架（天空盒/雾/LUT/锚点…）也摆")
    ap.add_argument("--include-not-placeable", action="store_true",
                    help="place_support≠verified 的类型也摆（摆放带 allow_unverified）。"
                         "给角色 101001/102001、推荐检查点 204041 这类「服务端受理但阀门"
                         "没有实测记录」的件试点用；成败判据仍是重进后的读数。")
    ap.add_argument("--new-run", action="store_true", help="强制全新 run（重摆一遍）")
    ap.add_argument("--resume-from", default=None,
                    help="跨会话续做：给**当前目标图导出的快照**，按实况重建 "
                         "旧ugc_id→新ugc_id，只补缺的那批（重进过地图后必须走这条，"
                         "否则旧 id 会把属性写到别的组件上）")
    ap.add_argument("--allow-stale-progress", action="store_true",
                    help="明知 progress 里的 id 已过期仍按老路续做（默认拒绝）")
    ap.add_argument("--progress", default=None)
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（脚本用）")
    # 跨图还原（把 A 图搭出来的场景搬到 B 图）
    ap.add_argument("--preflight", action="store_true",
                    help="跨图预检：框架/边界/预算对比（只读，dry-run 默认也做）")
    ap.add_argument("--set-framework", action="store_true",
                    help="预检发现框架不一致时，把目标图的框架改成快照的")
    ap.add_argument("--sync-settings", action="store_true",
                    help="把快照里与目标图不同的**玩法设置键**写到目标图"
                         "（地图名/简介/标签不动，框架另有 --set-framework）")
    ap.add_argument("--sync-global", action="store_true",
                    help="把全局单例（天空盒/雾/LUT/胜负条件…）的属性就地覆写到目标图")
    ap.add_argument("--set-bgm", action="store_true",
                    help="把快照里的**地图默认背景音乐**（map.props.ugc_bgm_conf）写到目标图："
                         "走「更多 → 网易云音乐 → 背景音乐设置」那条原生通道"
                         "（面板原方法，退化为同一个 battle event）；不加就只报告不动这一层")
    ap.add_argument("--verify-only", action="store_true",
                    help="只做「事件引用回对」：用上次运行落盘的 id 映射 + 目标图当前"
                         "剧情树（自动反推）比对当前地图与快照（只读，不写任何东西）")
    ap.add_argument("--check-progress", action="store_true",
                    help="只读审计「断点续做」的可信度：progress 声称摆过的件，"
                         "当前图上还在不在、身份对不对（长跑最怕"
                         "「跳过一件其实没摆上的」）")
    a = ap.parse_args(argv)

    # 批量开关的边界（护栏 1 / eval 回传 200 条上限）：越界立即拒绝，别跑到一半炸
    if a.placement_batch and not 1 <= a.placement_batch <= 128:
        print("⛔ --placement-batch 必须在 1-128（MAX_REQUESTS=128，批量大小"
              " + 在途未定论数不得超过它）。", flush=True)
        return 2
    if a.props_batch and not 1 <= a.props_batch <= 200:
        print("⛔ --props-batch 必须在 1-200（op 回传按 200 条截断；建议 30-50）。", flush=True)
        return 2

    with open(a.snapshot, encoding="utf-8") as f:
        snap = json.load(f)

    # 交付前体检（2026-10-01）：这份文件能不能拿去还原，先在**写入之前**判完。
    # 这次踩过的每一类（<deep> 截断、ugc_id 重复、控件没画布坐标、残骸当内容、
    # 引用指向文件外、缺 framework_id）都在这里变成一条明确结论，而不是摆到一半才炸。
    try:
        from snapshot import validate_snapshot, print_validation
        _v = validate_snapshot(snap)
    except Exception as exc:                 # 体检本身坏了不能挡住还原
        _v = None
        print("⚠️ 快照体检跳过（%s）" % str(exc)[:120], flush=True)
    if _v is not None:
        if (_v["blockers"] or not a.apply) and not a.check_progress:
            print("== 快照体检 ==")
            print_validation(_v)
        elif _v["warnings"] and not a.check_progress:
            print("ℹ️ 快照体检有 %d 条警告（跑 snapshot.py check %s 看详情）"
                  % (len(_v["warnings"]), a.snapshot), flush=True)
        if a.apply and not _v["ok"]:
            print("⛔ 体检有阻塞项，已拒绝写入。先按上面结论修快照（或重导一份干净的）。")
            return 1

    # ---- --check-progress：只读审计续做判据（连地图之前先别建计划）----
    if a.check_progress:
        p = a.progress or os.path.join(
            DEFAULT_PROGRESS_DIR, "%s.progress.jsonl"
            % os.path.splitext(os.path.basename(a.snapshot))[0])
        recs = read_progress_records(p)
        if not recs:
            print("进度回对：%s 没有记录（这条路径还没跑过？）" % os.path.basename(p))
            return 2
        try:
            live, total = fetch_live_index()
        except Exception as exc:
            print("取当前地图身份失败：%s" % str(exc)[:160])
            return 2
        print("当前地图组件 %s 个" % total)
        cur = ((fetch_target_info() or {}).get("map_setting") or {}).get("name")
        same_map = [r for r in recs if not r.get("map") or not cur or r["map"] == cur]
        if len(same_map) != len(recs):
            print("  ℹ️  忽略 %d 条属于别的图（%s）的记录"
                  % (len(recs) - len(same_map), cur))
        audit = progress_audit(same_map, live)
        print_audit(audit)
        return 0 if audit["ok"] else 1

    # ---- --verify-only：只回对，不还原（2026-09-23 加：静默失败必须能事后查出来）----
    if a.verify_only:
        p = a.progress or os.path.join(
            DEFAULT_PROGRESS_DIR, "%s.progress.jsonl"
            % os.path.splitext(os.path.basename(a.snapshot))[0])
        idf = idmap_path(p, a.snapshot)
        id_map = {}
        if os.path.exists(idf):
            with open(idf, encoding="utf-8") as f:
                id_map = {int(k): v for k, v in (json.load(f) or {}).items()}
        print("回对：%s" % os.path.basename(a.snapshot))
        print("id 映射：已摆物件 %d 条（%s）"
              % (len(id_map), os.path.basename(idf) if os.path.exists(idf)
                 else "无落盘文件，只有剧情部分"))
        # 剧情节：从目标图当前树按结构位置反推（不依赖这次运行是否落过 idmap）
        # ⚠️ 必须**默认**做，不能挂在 --with-story 后面：落盘的 idmap 只含「摆放出来的
        #    物件」，剧情/选项节点的旧 id 不在里面。不反推的话，那几条指向剧情节点的事件
        #    引用会被当成「未映射」，同时它们在目标图里**看得见**（新 id）⇒ 又被报成
        #    「幽灵引用」。2026-09-23 实机就是这样把 9/9 全对误报成 3/9 + 6 条幽灵。
        # 合并语义与主流程共用 merge_story_id_map，避免两套规则漂移。
        try:
            smap, mism = story_map_from_tree(snap.get("story") or {},
                                             fetch_story_tree())
            if smap:
                m = merge_story_id_map(id_map, smap)
                print("剧情节点映射：按结构位置匹配 %d 条%s%s"
                      % (len(smap), ("，⚠️ 内容不一致未映射 %s" % mism[:3])
                         if mism else "",
                         ("，⚠️ 与落盘 idmap 冲突 %s" % list(m["conflicts"])[:3])
                         if m["conflicts"] else ""))
        except Exception as exc:
            print("剧情树反推失败：%s" % str(exc)[:140])
        v = verify_event_refs(snap.get("events") or {}, id_map)
        _print_verify(v)
        # ⚠️ 回对用的是**落盘的 id 映射**：重进后 id 全变，映射过期 ⇒ 结论不可信
        #    （读操作虽不写坏地图，但会给出看起来很有底的错结论）
        try:
            _live, _n = fetch_live_index()
            if _live and id_map:
                hit = sum(1 for g in id_map.values() if str(g) in _live)
                if hit * 2 < len(id_map):
                    print("  ❌ id 映射疑似已过期（%d 条里只有 %d 条还在当前图上）"
                          " ⇒ 上面的回对结论**不可信**，请改用 `snapshot.py accept --live`"
                          % (len(id_map), hit))
        except Exception:
            pass
        rc = 0 if v["ok"] else 1
        try:
            _ti = fetch_target_info() or {}
            sv = settings_verify((snap.get("map") or {}).get("map_setting"),
                                 _ti.get("map_setting"))
            for ln in sv["lines"]:
                print(ln)
            # 设置层也要进结论：只印一行 `[diff]` 却 exit 0，等于把"没落住"说成"落住了"
            if sv["level"] != "ok":
                print("  ⇒ 设置层 %s ⇒ 整条命令不通过（exit 1）；这一层与 id 映射无关，"
                      "重进后照样能判" % ("没比成（unknown）" if sv["level"] == "unknown"
                                        else "没落住（diff）"))
                rc = 1
        except Exception as exc:
            print("  设置  [unknown] 回读失败：%s ⇒ 整条命令不通过" % str(exc)[:120])
            rc = 1
        # 地图属性层（默认背景音乐等）：读得到就一并回对，读不到/不一致都压住结论
        try:
            from snapshot import fetch_props as _fp
            tp = _fp(log=lambda *x: None)
            ps = map_props_status((snap.get("map") or {}).get("props"), tp)
            print("地图属性（默认背景音乐这一层）  [%s]" % ps["level"])
            for ln in ps["lines"]:
                print(ln)
            if ps["level"] != "ok":
                print("  ⇒ 这一层 %s ⇒ 整条命令不通过（exit 1）。"
                      "编辑器侧没有它的写入通道：值在上面，落到目标图要人工设一次"
                      % ("没采到/没比成（unknown）" if ps["level"] == "unknown"
                         else "与源图不一致（diff）"))
                rc = 1
        except Exception as exc:
            print("  地图属性 [unknown] 回读失败：%s ⇒ 整条命令不通过" % str(exc)[:120])
            rc = 1
        return rc

    types = fetch_placeable_types()
    plan = build_plan(snap, types, a)
    specs = plan["specs"]

    # ---- 目标图现状（只读）：既喂给跨图预检，也喂给"出厂自带层"判据 ----
    tgt = None
    try:
        tgt = fetch_target_info()
    except Exception as exc:
        print("\n预检失败（不影响还原，只是少了核对）：%s" % str(exc)[:160])
    stock, overlapped = stock_for_stages(snap, plan, types, tgt)
    deduct_note = None
    # 扣掉的件不是"不建"，而是**目标图上已经建好**：引用它们的属性袋/事件要认那个已有号，
    # 否则会被判成"指向本次不重建的组件"（悬空）而把整条命令拦下来。
    stock_specs = [{"old_g": g} for g in sorted(overlapped or {})]
    if overlapped:
        _b = len(specs)
        specs = [s for s in specs if s.get("old_g") not in overlapped]
        plan["specs"] = specs
        _by = defaultdict(int)
        for cid, _tg in overlapped.values():
            _by[cid] += 1
        deduct_note = ("  ⊖ 计划扣掉 %d 件（原 %d → %d）：目标图**已经有同内容同位置**的这些件"
                       "（每张图自带的出厂件，按类型 %s）⇒ 再摆就是翻倍；"
                       "挪过位置的同内容件不算已有，照摆"
                       % (_b - len(specs), _b, len(specs),
                          ", ".join("%d×%d" % kv for kv in sorted(_by.items()))))

    if a.limit is not None:
        if a.limit < 0:
            print("⛔ --limit 不能是负数（%d）" % a.limit)
            return 1
        # ⚠️ `0` 就是"一件都不摆"。写 `if a.limit:` 会把 0 当成 falsy 而**完全不截断**，
        # 于是 `--apply --limit 0` 变成"全量摆"（2026-10-01 实机差点撞上，被同图闸门+预算闸门拦住）
        specs = specs[:a.limit]
        if a.limit == 0:
            print("（--limit 0 ⇒ 一件都不摆，只跑预检与写前闸门）")

    print_plan(plan, snap)
    if deduct_note:
        print(deduct_note)

    # ---- 这次命令能不能跑成 1:1（少带开关 = 少一整层，跑完几十分钟才发现就白跑）----
    stage_reqs = stage_requirements(snap, plan, a, stock=stock)
    stage_note = missing_stage_note(stage_reqs)
    print("\n== 1:1 需要这些层 ==")
    for r in stage_reqs:
        print("  %s %-30s %6d 处" % ("✓" if r["satisfied"] else "✗",
                                     "%s → %s" % (r["stage"], r["flag"]), r["count"]))
        if r.get("note"):
            print("     %s" % r["note"])
    if stage_note:
        print("\n%s" % stage_note)
    else:
        print("  （没有缺的层）")
    # 孤儿对话/选项：导出来有、导入**不重建**（`story_import` 只遍历 stories）
    _st = snap.get("story") or {}
    _od, _oc = len(_st.get("orphan_dialogues") or []), len(_st.get("orphan_choices") or [])
    if _od or _oc:
        print("  ⚠️ 剧情里有孤儿节点（对话 %d / 选项 %d）：**任何开关都不会重建它们** ⇒ "
              "目标图必然少这些件。要 1:1 请先在源图把孤儿挂回剧情或删掉，再重导这份快照"
              % (_od, _oc))

    # ---- 跨图预检（只读；`tgt` 已在上面取过，判"出厂自带层"要用同一份现状）----
    if tgt:
        fw = check_framework((snap.get("map") or {}).get("framework_id"),
                             tgt.get("framework"))
        msd = map_setting_diff(((snap.get("map") or {}).get("map_setting")),
                               tgt.get("map_setting"))
        bd = check_bounds(bbox_of(specs), tgt.get("bounds"))
        bg = check_budget(snap.get("budget"), tgt.get("budget"),
                          plan_budget_need(snap, plan, specs, a.with_story))
        smg = same_map_guard(snap, tgt)
        print("\n== 跨图预检 ==")
        print("  目标  [%s] %s" % (smg["level"], smg["msg"]))
        print("  框架  [%s] %s" % (fw["level"], fw["msg"]))
        print("  设置  [%s] %s" % (msd["level"], msd["msg"]))
        for r in msd["diffs"][:6]:
            print("      key %-4s 快照=%-24r 目标=%r"
                  % (r["key"], r["snapshot"], r["target"]))
        if len(msd["diffs"]) > 6:
            print("      …另有 %d 个键不同" % (len(msd["diffs"]) - 6))
        print("  范围  [%s] %s" % (bd["level"], bd["msg"]))
        print("  预算  [%s] %s" % (bg["level"], bg["msg"]))
        # 地图属性层（默认背景音乐等）：几何、玩法键都对得上也会"玩起来不一样"的一层。
        # 这一层以前根本不采；写入走面板那条原生通道，要 --set-bgm 才动。
        try:
            from snapshot import fetch_props as _fp0
            ps = map_props_status((snap.get("map") or {}).get("props"),
                                  _fp0(log=lambda *x: None))
            print("  地图属性 [%s] %s" % (
                ps["level"],
                {"ok": "与目标图一致（默认背景音乐/排行榜/人数等）",
                 "diff": "⚠️ %d 项与目标图不同 ⇒ 默认背景音乐那项可加 --set-bgm 自动写，"
                         "其余项要人工在目标图上设一次" % len(ps["manual"]),
                 "unknown": "没比成（有一方没采到）"}[ps["level"]]))
            for ln in ps["lines"]:
                print(ln)
            if ps["level"] == "diff" and a.apply and not a.set_bgm:
                print("      ⚠️ 本次没带 --set-bgm ⇒ 还原跑完这一层还是源图的值对不上；"
                      "带上它（--set-bgm --apply）会走面板那条原生通道写入，"
                      "落库结论仍要重进后由 accept --live 给")
        except Exception as exc:
            print("  地图属性 [unknown] 读取失败：%s" % str(exc)[:120])
        uv = check_ui_view(specs, fetch_ui_state() if any(
            _as_int(s.get("component_type")) == UI_CUSTOM_TYPE for s in specs) else None)
        if uv["widgets"] or uv["level"] != "ok":
            print("  控件  [%s] %s" % (uv["level"], uv["msg"]))
        for r in (bg.get("rows") or []):
            print("      %-15s 需 %-6s(%s) 目标已用 %-6s 上限 %-6s 余 %s"
                  % (r["metric"], r["need"], r.get("basis"), r["target_used"],
                     r["target_cap"], r["free"]))
        if not fw["same"] and a.set_framework and a.apply:
            v = fw["snapshot"]
            r = call_edit("set_map_setting",
                          {"key": "14", "value": str(v)}, timeout=60) or {}
            ok = (r.get("ok") if "ok" in r
                  else (r.get("value") or {}).get("ok"))
            print("  改框架 → %s：%s" % (v, "成功" if ok else r))
            if ok:
                print("    ⚠️ 框架改动可能需要重进地图才完全生效。")
        elif not fw["same"] and not a.set_framework:
            print("    （要自动对齐加 --set-framework --apply）")
        if msd["diffs"] and not a.sync_settings:
            print("    （玩法设置差异要落到目标图加 --sync-settings --apply；"
                  "不一致时几何即便 1:1，玩法行为也会和源图不同）")
        elif msd["diffs"] and a.sync_settings and not a.apply:
            print("    （--sync-settings 只在 --apply 时写入，这里是预演）")
        if bg["level"] == "error" and a.apply and not a.yes:
            print("  ⛔ 预算不足，已停止。确认要强拆请加 --yes。")
            return 1
        if bg["level"] == "error" and a.apply and a.yes:
            print("  ⚠️ 预检按**整份计划**算出预算不足，而这次带了 --resume-from/续做 ⇒ "
                  "已摆过的件会被跳过，这个「不足」可能是假的。不拦你，但动手前会按"
                  "「这次真会摆的件数」复核一次，那一条才是判据（且不受 --yes 放行）。")
        if pending_deletions_present(tgt.get("budget")):
            print("  ⚠️ %s" % pending_deletion_note(tgt["budget"]))

    # ---- 袋内引用的悬空预测（指向本次不重建的组件 ⇒ 旧号会原样写到目标图上）----
    bag_dangling = bag_ref_dangling(snap, specs + stock_specs)
    bag_note = bag_ref_dangling_note(
        bag_dangling, _skip_reason_index(snap, plan, specs, only=(a.only_types or a.only_ids),
                                               limit=a.limit, overlapped=overlapped))
    if bag_note:
        print("\n== 属性袋悬空引用 ==")
        print(bag_note)

    if not a.apply:
        # 事件引用的可绑性预测：指向剧情/选项节点的会在 place 阶段悬空，
        # 需要 --with-story 重建后用 id 映射补绑。
        ev = snap.get("events") or {}
        placed_olds = {s.get("old_g") for s in specs} | set(overlapped or {})
        act0, dang0 = event_bind_plan(ev, {g: g for g in placed_olds},
                                   comp_index=_info_index(snap))
        if dang0:
            olds = {d["old_ugc_id"] for d in dang0 if d.get("old_ugc_id") is not None}
            print("\n== 事件引用预测 ==")
            print("  本次直接可绑 %d 条，悬空 %d 条（指向 %d 个本次不摆的组件）"
                  % (len(act0), len(dang0), len(olds)))
            owners = _dangling_owners(snap, olds)
            if owners:
                print("  悬空指向：%s" % owners)
            # ⚠️ 一定要按原因分报：只说"加 --with-story"会把运动器那批误导成已解决
            for key, n, hint in dangling_advice(
                    dang0, _skip_reason_index(snap, plan, specs, only=(a.only_types or a.only_ids),
                                               limit=a.limit, overlapped=overlapped)):
                print("    %s ×%d → %s" % (key, n, hint))
        if a.sync_global:
            r = sync_global_singletons(snap, dry=True)
            print("\n全局单例同步（dry-run）：配对 %d，目标图缺失类型 %s"
                  % (r["pairs"], r["missing_in_target"] or "无"))
            if r["refs_needing_remap"]:
                print("  ⚠️ %d 个单例字段里存着组件 id（%s）—— 单例同步**没有** old→new 映射，"
                      "这些号会原样写到目标图（同号=别的组件）。这张图要 1:1 就得先处理这条"
                      % (len(r["refs_needing_remap"]), r["refs_needing_remap"][:3]))
        if a.sync_settings:
            r = sync_map_settings(((snap.get("map") or {}).get("map_setting")),
                                  (tgt or {}).get("map_setting"), dry=True)
            print("\n地图设置同步（dry-run）：计划写入 %s%s%s"
                  % ([x["key"] for x in r["applied"]][:10] or "无",
                     ("，拒绝（含队伍引用）%s" % [x["key"] for x in r["refused"]])
                     if r["refused"] else "",
                     "（目标设置读不到，不盲写）" if r["skipped"] else ""))
        print("\ndry-run：未做任何写操作。确认后加 --apply。")
        return 0

    # ---- 写前闸门：目标图是源图本身就直接拒写（放在 yes 提示之前）----
    if a.apply and not a.allow_same_map:
        _g = same_map_guard(snap, tgt)
        if _g["level"] == "block":
            print("\n⛔ %s" % _g["msg"])
            return 1
    if a.apply and bag_dangling and not a.allow_dangling_refs:
        print("\n⛔ 属性袋里有 %d 处引用指向本次不重建的组件（见上）。"
              "先让被指的件进入计划，或确认无害再加 `--allow-dangling-refs`。"
              % len(bag_dangling))
        return 1

    # ---- 写前确认 ----
    print("\n⚠️ 即将向**当前打开的地图**写入 %d 件物件。"
          "这会改动地图（可保存落库），建议先确认目标图是空白图/测试图。" % len(specs))
    if pending_deletions_present((tgt or {}).get("budget")):
        # 这次保存会把**上一批未提交的删除**一起带走，所以必须出现在 yes 之前
        print("⚠️ %s" % pending_deletion_note(tgt["budget"]))
    if not a.yes:
        try:
            ans = input("继续？输入 yes 确认：").strip().lower()
        except EOFError:
            ans = ""
        if ans != "yes":
            print("已取消。")
            return 1

    rid = run_id_for(a.snapshot, specs, a.new_run)

    # ---- 阶段 0a：地图玩法设置（几何之外的一层：几何 1:1 但设置不一致 = 长得一样、玩起来不一样）----
    if a.sync_settings:
        r = sync_map_settings(((snap.get("map") or {}).get("map_setting")),
                              (tgt or {}).get("map_setting"))
        print("地图设置同步：%s写入 %d / 失败 %d / 交人工 %d"
              % (("已跳过（%s）" % r["reason"]) if r["skipped"] else "",
                 len(r["applied"]), len(r["failed"]), len(r["refused"])), flush=True)
        for f in (r["failed"] + r["refused"])[:4]:
            print("  key=%s 目标=%s 回读=%s：%s"
                  % (f["key"], f.get("after"), f.get("native_after"),
                     str(f.get("error"))[:140]), flush=True)
        if r["applied"]:
            save_and_check("rs_save_settings_%s" % rid)

    # ---- 阶段 0a2：地图默认背景音乐（`ugc_props_info['ugc_bgm_conf']`）----
    # 这一层既不是组件也不是玩法键，2026-10-01 之前根本不采，于是"七层全绿"的图可以开局静音。
    # 写入走面板自己那条通道（`UGCUICloudMusic.apply_ugc_bgm_conf`／同一个 battle event），
    # 不是自造的 `save_ugc_map_data` 载荷 —— 后者的合并语义没证清之前不动它。
    bgm_want = ((snap.get("map") or {}).get("props") or {}).get("ugc_bgm_conf") or {}
    if bgm_want.get("value") is not None:
        conf = bgm_want["value"]
        if a.set_bgm:
            r = call_edit("set_map_bgm", {"mode": conf.get("mode"),
                                          "param": conf.get("param") or {}}, timeout=60) or {}
            got = (r.get("value") or r)
            print("地图默认背景音乐：%s" % json.dumps(
                {"changed": got.get("changed"), "ok": got.get("ok"),
                 "via": got.get("via"), "conf": got.get("conf")},
                ensure_ascii=False)[:400], flush=True)
            if got.get("ok") and got.get("changed"):
                # 光 save 不够：空变更集的保存不落这条（2026-10-01 实机，见 save_props_layer）
                r2 = save_props_layer("rs_save_bgm_%s" % rid, log=print)
                print("  ⇒ 属性层保存：%s" % ("已上送（脏缓冲归零）" if r2["ok"]
                                        else "未确认上送 ⇒ 重进后务必用 accept --live 复核"),
                      flush=True)
            elif not got.get("ok"):
                print("  ⚠️ 没写进去：%s ⇒ 这一层要人工在「更多 → 网易云音乐 → 背景音乐设置」里设"
                      % str(got.get("error"))[:160], flush=True)
        else:
            print("  ℹ️ 快照带着默认背景音乐 %s；没加 --set-bgm ⇒ 不动这一层"
                  % json.dumps(conf, ensure_ascii=False)[:160], flush=True)

    # ---- 阶段 0b：全局单例观感同步（就地覆写，不新建）----
    if a.sync_global:
        r = sync_global_singletons(snap)
        print("全局单例同步：配对 %d / 成功 %d / 失败 %d / 目标缺失 %s"
              % (r["pairs"], len(r["applied"]), len(r["failed"]),
                 r["missing_in_target"] or "无"), flush=True)
        if r["refs_needing_remap"]:
            print("  ⚠️ %d 个单例字段存着组件 id（%s）：单例没有 old→new 映射，"
                  "这些号按原样落库 ⇒ 重进后要用 accept 逐字段回对"
                  % (len(r["refs_needing_remap"]), r["refs_needing_remap"][:3]), flush=True)
        for f in (r["failed"] or [])[:3]:
            print("  失败 type=%s ugc_id=%s：%s"
                  % (f["component_type"], f["target_ugc_id"],
                     str(f.get("error"))[:140]), flush=True)
        save_and_check("rs_save_global_%s" % rid)
    prog = a.progress or os.path.join(DEFAULT_PROGRESS_DIR, "%s.progress.jsonl"
                                      % os.path.splitext(os.path.basename(a.snapshot))[0])
    os.makedirs(os.path.dirname(prog), exist_ok=True)

    # ---- 断点续做（或按当前图实况**重建** id 映射）----
    cur_map = ((tgt or {}).get("map_setting") or {}).get("name")
    foreign = 0
    if a.resume_from:
        try:
            with open(a.resume_from, encoding="utf-8") as f:
                other = json.load(f)
        except Exception as exc:
            print("⛔ --resume-from 的文件读不到（%s）：%s"
                  % (a.resume_from, str(exc)[:160]), flush=True)
            print("   它必须是**同一张目标图**当前导出的快照："
                  "`python cli/snapshot.py export --out builds/now.json`", flush=True)
            return 2
        rb = rebuild_id_map(snap, other)
        id_map = rb["id_map"]
        done = set(id_map)
        fa = (snap.get("map") or {}).get("framework_id")
        fb = (other.get("map") or {}).get("framework_id")
        print("按当前图重建 id 映射（%s）：配对 %d / 待补 %d / 当前图多出 %d"
              % (os.path.basename(a.resume_from), rb["mapped"], len(rb["todo"]),
                 len(rb["extra"])), flush=True)
        # ⚠️ `--resume-from` 给的那份导出**必须是当前状态**。本项目的 `save_map` 会重洗落盘
        # id（2026-10-01 实机：一次长跑里的分批保存就让 progress 的 11349/11435 个号解析不上，
        # 全程没重进过）。给一份旧导出时配对**照样配得上**（配对按内容+位置），但 id_map 里
        # 那些号已经不作数 ⇒ 属性回写会写到别的组件上，而回读仍然"ok"。动手前抽 200 个号问一次。
        _sample = [g for g in list(id_map.values()) if g is not None][:24]
        _hit = ids_live_on_target(_sample) if _sample else None
        if _hit is not None:
            if _hit < len(_sample) * 0.8:
                print("⛔ --resume-from 用的那份导出**不是当前状态**：抽 %d 个号只有 %d 个还在"
                      "当前图里（save 会重洗落盘 id）⇒ 按这份映射回写属性会写到别的组件上，"
                      "而且回读看不出来。重新导一次再续做："
                      "`python cli/snapshot.py export --out builds/now.json`"
                      % (len(_sample), _hit), flush=True)
                return 1
            print("  id 映射新鲜度核对：点验 %d/%d 个号仍在当前图里 ⇒ 这份导出可以用"
                  % (_hit, len(_sample)), flush=True)
        else:
            print("  ⚠️ 点验没做成（读不到当前图）⇒ 这一条**没验成**，配对结果请人工看"
                  "一眼（不拦，但别说它过了）", flush=True)
        if fa and fb and str(fa) != str(fb):
            print("  ⚠️ 两份快照的框架不同（%s vs %s）⇒ 坐标基准不一致，配对结果要人工看"
                  % (fa, fb), flush=True)
        if rb["extra"]:
            print("  ℹ️  当前图里有 %d 件对不上快照（可能是别的来源摆的，本命令不删它们）：%s"
                  % (len(rb["extra"]),
                     ",".join(str(x) for x in rb["extra"][:10])
                     + ("…" if len(rb["extra"]) > 10 else "")), flush=True)
        if not rb["mapped"] and rb["todo"]:
            print("  ⛔ 一件都没配上：确认 %s 真的是这张目标图导出的（而不是另一张图）。"
                  % os.path.basename(a.resume_from), flush=True)
            return 1
    else:
        id_map, done, foreign = load_progress(prog, rid=rid, cur_map=cur_map,
                                              new_run=a.new_run)
        if done or foreign:
            print("断点续做：跳过已完成 %d 件，已有 id 映射 %d 条%s"
                  % (len(done), len(id_map),
                     ("（另有 %d 条属于别的图/别的 run，已忽略）" % foreign)
                     if foreign else ""))
        # ⛔ 过期 id 闸门：重进后 ugc_id 全部重编号，而 `run_id_for` 是按快照内容派生的
        # （跨会话稳定）、`load_progress` 只按图名筛记录 ⇒ 这里会照常"续做"，
        # 跳过摆放却拿**旧 id** 去写属性/绑事件 ⇒ 静默写到别的组件上。
        if id_map and not a.allow_stale_progress:
            try:
                _live, _n = fetch_live_index()
            except Exception:
                _live = None
            if _live:
                aud = progress_audit(read_progress_records(prog), _live)
                if aud["stale_session"]:
                    print("⛔ 这份 progress 的 ugc_id 已经**过期**（当前图里 %d/%d 件找不到，"
                          "几乎确定中间重进过地图：id 全部重编号）。"
                          % (len(aud["missing"]), aud["claimed"]), flush=True)
                    print("   继续跑会**跳过摆放**却按旧 id 写属性 ⇒ 写到别的组件上。"
                          "两条出路：", flush=True)
                    print("     ① 推荐：先 `python cli/snapshot.py export --out builds/now.json`，"
                          "再 `restore … --resume-from builds/now.json`（按实况重建映射，"
                          "只补缺的那批）；", flush=True)
                    print("     ② 明知要重来一遍：加 --new-run（注意会在当前图上翻倍）。",
                          flush=True)
                    return 1

    # ---- 预算复核：按**这次真会摆的件数**算 ----
    # 2026-10-01 实机：跑到 11421 件时中途保存把 progress 里的 id 洗成了过期态，只能走
    # `--resume-from` 续跑，而预检仍按**整份计划**报「component_nums 需 14872 / 余 3692
    # ⇒ ⛔ 预算不足」——那 11421 件被重复计了一次，合法的续做被判成不可能；更糟的是这道
    # error 会被 `--yes` 直接放过（手册让加 --yes），于是一个说假话的闸门同时会误拦和失灵。
    # 真正的判据要等 resume/续做把"还要建什么"定下来之后才算得出来，就放在这里。
    remaining = [s for s in specs if s.get("old_g") not in done]
    if a.apply and tgt and tgt.get("budget") and len(remaining) != len(specs):
        bg2 = check_budget(snap.get("budget"), tgt["budget"],
                           plan_budget_need(snap, plan, remaining, a.with_story))
        print("\n== 预算复核（按这次真会摆的 %d 件算；预检那步只能按整份计划）=="
              % len(remaining))
        for r in bg2["rows"]:
            print("  %-15s 需 %-8s 目标已用 %-8s 上限 %-8s %s"
                  % (r["metric"], r["need"], r["target_used"], r["target_cap"],
                     "够" if r["fits"] else "⛔ 不够"))
        if bg2["level"] != "ok":
            print("   ⇒ 续做后的余量确实不够 ⇒ 拒写。这条**不受 --yes 放行**："
                  "上面那步按整份计划算出来的「不足」可能是假的，这里的不是。", flush=True)
            return 1

    # ---- 少带开关的闸门：放在进度/resume 处理**之后** ----
    # 为什么不与"同图闸门"并到最前面：`--resume-from` 会改写"这次还要建什么"
    # （剧情层可能上一跑已经建好、id 也重建回来了），在最前面判就会把已经补齐的层
    # 误报成缺口。缺口报告本身早在预检里就印了（`== 1:1 需要这些层 ==`），
    # 这里只是把"明知故犯"的出口收在真正动手之前。
    stage_reqs2 = stage_requirements(snap, plan, a, stock=stock)
    stage_note2 = missing_stage_note(stage_reqs2)
    # 试点（--only-types / --only-ids）**不适用**：那次本来就只摆几件试路，
    # 拿"少一层"判它等于每次都误报 —— 操作者已经用筛法声明过"这不是全量"。
    pilot_mode = bool(a.only_types or a.only_ids)
    if a.apply and stage_note2 and not a.allow_missing_stages and not pilot_mode:
        print("\n⛔ 上面标 ✗ 的这些层这次不会重建 ⇒ 跑完拿到的不是 1:1（少一整层比慢更糟）。"
              "\n   补上开关重跑；确认这次就不要这些层，再加 --allow-missing-stages。", flush=True)
        return 1

    # ---- 扣掉的出厂件直接接入 id 映射（它们不是"不建"，是**已经在目标图上**）----
    # 属性袋与事件里指向这些旧号的，必须换成目标图上那个已有号；不接的话
    # ① 写前那道悬空闸门会把整条命令拦下（2026-10-01 试点后重跑就是这么被 5 处拦住的），
    # ② 放行则把旧号原样写下去 —— 目标图上同号是别的组件，静默接错线。
    if overlapped:
        _seed = 0
        for _old, (_cid, _tg) in overlapped.items():
            if _tg is None or _old in id_map:
                continue
            id_map[_old] = _tg
            _seed += 1
        if a.apply and _seed:
            print("  ℹ️ 目标图已有的 %d 件直接接入 id 映射（旧 %d → 已有 %d），"
                  "指向它们的引用按这个号补绑，不再算悬空"
                  % (_seed, len(overlapped), _seed), flush=True)

    # ---- 阶段 1：摆放 ----
    t0 = time.perf_counter()
    placed = failed = skipped_n = 0
    scale_dev_n = 0          # 回执带 catalog_scale_deviation 的件数
    pending = 0
    aborted = None
    watch = RunWatch(len(specs), heartbeat=a.heartbeat, abort_after=a.abort_after,
                     t0=t0)
    with open(prog, "a", encoding="utf-8") as pf:
        if a.placement_batch:
            # ---- 批量摆放路径（--placement-batch N，默认 0=关闭）----
            # 往返按批摊薄（每批 2-3 次 vs 旧路径每件 2-3 次）；进度/id_map 记录
            # 格式与逐件路径**逐字一致**（护栏 5），保存节奏随批（护栏 7），
            # 失败逐件定性、没试的批不写进度（护栏 6）。
            todo = [(i, s) for i, s in enumerate(specs) if s["old_g"] not in done]
            skipped_n += covered_components(specs) - covered_components(
                [s for _i, s in todo])
            for cs in range(0, len(todo), max(1, a.placement_batch)):
                chunk = todo[cs:cs + max(1, a.placement_batch)]
                if watch.abort:
                    aborted = watch.abort
                    print("⛔ 早停：%s。已摆好的 %d 件会照常保存，不丢进度。"
                          % (watch.abort, placed), flush=True)
                    print("   先处理失败形态本身（桥接是否还活着 / 预算够不够 /"
                          " 打开的是不是目标图），然后**重跑同一条命令**即可续做"
                          "（按 old_g 跳过已完成的）。", flush=True)
                    break
                try:
                    results = place_batch_chunk(chunk, rid)
                except Exception as exc:
                    # 整批在登记前就失败（护栏拒绝/桥接掉线）：这批**没试**，
                    # 不写任何进度；token 是确定性的（rs_<run>_<i>），重跑幂等续做。
                    aborted = "批量摆放请求失败：%s" % str(exc)[:160]
                    print("⛔ %s" % aborted, flush=True)
                    print("   本批 %d 件**没试**（不记进度）。已摆好的 %d 件会照常"
                          "保存。" % (len(chunk), placed), flush=True)
                    break
                last_i = chunk[-1][0]
                # 回执里没有这一件时的兜底：不能只写一句 "exception" 就完事
                miss_row = {"error": "本批 %d 件的回执里没有这一件（登记阶段就没覆盖）"
                                     % len(chunk)}
                for i, spec in chunk:
                    row, status, tries = results.get(i, (miss_row, "exception", -1))
                    reason = failure_reason(status, row)
                    token = "rs_%s_%05d" % (rid, i)
                    new_gid = (row or {}).get("ugc_id")
                    # 成组创建（204041）：成员 id 一起进 id_map 与进度（护栏 4 的映射侧）
                    group_ids = (row or {}).get("group_ugc_ids") or []
                    mapped = [(spec["old_g"], new_gid)] + [
                        (old_g, gid) for old_g, gid in
                        zip(spec.get("group_old_gs") or [], group_ids[1:])]
                    if (row or {}).get("catalog_scale_deviation"):
                        scale_dev_n += 1
                    if status == "placed" and new_gid is not None:
                        for old_g, gid in mapped:
                            if gid is not None:
                                id_map[old_g] = gid
                        placed += len(mapped)
                    else:
                        failed += 1
                    for old_g, gid in mapped:
                        rec = {"i": i, "token": token, "status": status,
                               "old_g": old_g, "ugc_id": gid,
                               "item_id": spec["item_id"],
                               "run": rid, "map": cur_map,
                               "poll_tries": tries, "t": round(time.time(), 3)}
                        if reason:
                            # 只给失败行加 error：placed 行的键集合被护栏 5 锁着
                            rec["error"] = reason
                        pf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    pf.flush()
                    pending += 1
                    for ln in watch.record(status, reason):
                        print(ln, flush=True)
                if pending >= max(1, a.batch):
                    d = save_and_check("rs_save_%s_%d" % (rid, last_i))
                    print("  保存（批 %d-%d）：%s" % (chunk[0][0], last_i,
                                                     "已上送" if d.get("submitted")
                                                     else "未确认(%s)" % d.get("note", d.get("save_error"))),
                          flush=True)
                    pending = 0
        else:
            for i, spec in enumerate(specs):
                token = "rs_%s_%05d" % (rid, i)
                if spec["old_g"] in done:
                    skipped_n += covered_components([spec])
                    continue
                try:
                    r, status, tries = place_one(spec, token)
                except Exception as exc:
                    status, r, tries = "exception", {"error": str(exc)[:300]}, -1
                reason = failure_reason(status, r)
                new_gid = (r or {}).get("ugc_id")
                # 成组创建（204041）：其余成员的新 id 也在回执里，必须一起进 id_map 与进度。
                # 漏了它们 = 那件没有归属（属性回写跳过它），且重跑时不被认成"已摆过" ⇒ 翻倍。
                group_ids = (r or {}).get("group_ugc_ids") or []
                mapped = [(spec["old_g"], new_gid)] + [
                    (old_g, gid) for old_g, gid in
                    zip(spec.get("group_old_gs") or [], group_ids[1:])]
                if (r or {}).get("catalog_scale_deviation"):
                    scale_dev_n += 1
                if status == "placed" and new_gid is not None:
                    for old_g, gid in mapped:
                        if gid is not None:
                            id_map[old_g] = gid
                    placed += len(mapped)
                else:
                    failed += 1
                for old_g, gid in mapped:
                    rec = {"i": i, "token": token, "status": status, "old_g": old_g,
                           "ugc_id": gid, "item_id": spec["item_id"],
                           "run": rid, "map": cur_map,
                           "poll_tries": tries, "t": round(time.time(), 3)}
                    if reason:
                        # 只给失败行加 error：placed 行的键集合被护栏 5 锁着
                        rec["error"] = reason
                    pf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                pf.flush()
                pending += 1
                for ln in watch.record(status, reason):
                    print(ln, flush=True)
                if watch.abort:
                    aborted = watch.abort
                    print("⛔ 早停：%s（停在第 %d 件）。已摆好的 %d 件会照常保存，不丢进度。"
                          % (watch.abort, i, placed), flush=True)
                    print("   先处理失败形态本身（桥接是否还活着 / 预算够不够 /"
                          " 打开的是不是目标图），然后**重跑同一条命令**即可续做"
                          "（按 old_g 跳过已完成的）。", flush=True)
                    break
                if pending >= max(1, a.batch):
                    d = save_and_check("rs_save_%s_%d" % (rid, i))
                    print("  保存 @%d：%s" % (i, "已上送" if d.get("submitted")
                                              else "未确认(%s)" % d.get("note", d.get("save_error"))),
                          flush=True)
                    pending = 0
    if pending:
        d = save_and_check("rs_save_%s_final" % rid)
        print("  保存（末批）：%s" % ("已上送" if d.get("submitted")
                                     else "未确认(%s)" % d.get("note", d.get("save_error"))),
              flush=True)
    print("摆放完成：成功 %d / 失败 %d / 跳过(续做) %d ｜ %.1fs"
          % (placed, failed, skipped_n, time.perf_counter() - t0), flush=True)
    if scale_dev_n:
        print("  ℹ️ %d 件的缩放超出编辑器滑块范围（抄自源图既有值，引擎已受理并回读一致）。"
              % scale_dev_n, flush=True)

    with open(idmap_path(prog, a.snapshot), "w", encoding="utf-8") as f:
        json.dump(id_map, f, ensure_ascii=False, indent=1)

    # ---- 落点回读：`placed` 回执只证明这件存在，不证明它落在请求的坐标上 ----
    # 实机（2026-10-01）：14869 件里有一件被引擎放到 248 单位之外、还低 58，回执正常、
    # 件数对得上，是人在试玩里看出「落脚平台更低」才发现的。
    la = landing_audit(specs, id_map, log=lambda m: print(m, flush=True))
    if la.get("exempt_global"):
        # 显式报出来（不是静默跳过）：这些件的位置真值不在单元上，本口径核不了。
        print("  ℹ️ 另有 %d 件是全局单元上的组件（运动器/音乐事件/剧情…），"
              "位置真值在属性袋、不在单元上 ⇒ 不参与本次坐标核验"
              % la["exempt_global"], flush=True)
    if la["off"]:
        print("  ⛔ 落点回读：%d/%d 件不在请求坐标上（容差 1.0）⇒ 摆成功了但摆错了地方"
              % (len(la["off"]), la["checked"]), flush=True)
        for r in la["off"][:8]:
            print("     新 id=%s 请求=%s 实际=%s 偏差=%s 单元类=%s"
                  % (r["ugc_id"], [round(v, 2) for v in r["want"]], r.get("is"),
                     r.get("delta"), r.get("unit_class", "?")), flush=True)
        print('     单件修回：python cli/fxedit.py edit_transform '
              '\'{"ugc_id":N,"position":[x,y,z],"persist_via_recreate":true}\' '
              '——重摆会把袋清成默认，之后要整袋回写属性', flush=True)
    elif la["checked"]:
        print("  ✓ 落点回读：%d 件都在请求坐标上（容差 1.0）%s%s"
              % (la["checked"],
                 ("，另有 %d 件读不到位置（没算通过）" % la["unreadable"])
                 if la["unreadable"] else "",
                 ("，另有 %d 件全局组件不参与坐标核验" % la["exempt_global"])
                 if la.get("exempt_global") else ""), flush=True)

    if aborted:
        # 属性/事件阶段照例跳过：它们靠 id_map 驱动，重跑时会把这次已摆的一并补上，
        # 不必在半张图上继续写（那只会再制造一批看不懂的失败）。
        print("已落 id 映射 %d 条 ⇒ 重跑同一条命令从停在的地方继续。" % len(id_map),
              flush=True)
        return 1

    # ---- 阶段 2：属性回写 ----
    if not a.no_props:
        todo = [c for c in (snap.get("components") or [])
                if id_map.get(c.get("g")) is not None]
        # 计划里的件**实际没摆上**（摆放失败/早停）时，袋里指向它的引用会换不到号 ⇒
        # 原样写下旧号，而目标图上同号的是别的组件。写之前先报，让人有机会停在写之前。
        mid = bag_ref_dangling(snap, [{"old_g": g} for g in id_map])
        if mid:
            print("⚠️ 属性袋里有 %d 处引用指向**本次没摆上**的组件（%s 等）——"
                  "这些旧号会原样写到目标图上，接错到同号的别的组件；"
                  "先续做把缺的件补齐再跑属性回写，否则这一跑不可能 1:1。"
                  % (len(mid), ", ".join(str(x["ref"]) for x in mid[:8])), flush=True)
        ok = bad = 0
        missing_rows = 0        # 批量回执里缺的件（没轮到，不是失败也不是成功）
        failed_olds = []
        w2 = RunWatch(len(todo), heartbeat=a.heartbeat,
                      abort_after=a.abort_after, t0=time.perf_counter())
        if a.props_batch:
            # ---- 批量属性回写路径（--props-batch N，默认 0=关闭）----
            # 一条 set_component_info_batch 循环写 N 袋；逐件护栏与
            # 「不在编辑器登记集合 → allow_unregistered」的升级重试都在 op 里
            # **逐件**保留（护栏 8）。没有 events 的件与逐件路径一样记成功跳过。
            no_ev = [c for c in todo if not _info_of(c).get("events")]
            todo_ev = [c for c in todo if _info_of(c).get("events")]
            for c in no_ev:
                w2.record(True)
            ok += len(no_ev)
            for cs in range(0, len(todo_ev), max(1, a.props_batch)):
                chunk = todo_ev[cs:cs + max(1, a.props_batch)]
                if w2.abort:
                    break
                try:
                    rows = apply_props_chunk(chunk, id_map)
                except Exception as exc:
                    # 整批没试成（桥接掉线等）：这批不记失败（护栏 6：没试≠失败），
                    # 停下来让人处理；已写完的保留。
                    print("⛔ 批量属性回写请求失败：%s（本批 %d 件**没试**）"
                          % (str(exc)[:160], len(chunk)), flush=True)
                    print(retry_hint("属性回写", failed_olds, a.snapshot, prog),
                          flush=True)
                    save_and_check("rs_save_props_abort_%s" % rid)
                    return 1
                for c in chunk:
                    row = rows.get(str(c.get("g")))
                    if row is None:
                        # 回执缺这件：不算成功也不算失败，按没轮到留待续做
                        # （但要计入并在总结里说出来——"成功 0 / 失败 0"看起来是绿的，
                        #  而一件都没写进去，2026-10-02 批量首跑就是这么被蒙过去的）
                        missing_rows += 1
                        continue
                    if row.get("ok") or row.get("dry_run"):
                        ok += 1
                    else:
                        bad += 1
                        failed_olds.append(c.get("g"))
                    for ln in w2.record(bool(row.get("ok")), row.get("error")):
                        print(ln, flush=True)
                    if w2.abort:
                        break
            if w2.abort:
                print("⛔ 早停（属性回写）：%s。已写完的 %d 件保留、已落盘；"
                      "剩余 %d 件**没处理**（不是失败，是没轮到）。"
                      % (w2.abort, ok, max(0, len(todo) - w2.seen)), flush=True)
                print(retry_hint("属性回写", failed_olds, a.snapshot, prog), flush=True)
                save_and_check("rs_save_props_abort_%s" % rid)
                return 1
        else:
            for c in todo:
                new_gid = id_map.get(c.get("g"))
                r = apply_props(new_gid, c, id_map)
                if r.get("ok"):
                    ok += 1
                elif r.get("skipped"):
                    w2.record(True)
                    continue
                else:
                    bad += 1
                    failed_olds.append(c.get("g"))
                for ln in w2.record(r.get("ok"), r.get("error")):
                    print(ln, flush=True)
                if w2.abort:
                    print("⛔ 早停（属性回写）：%s。已写完的 %d 件保留、已落盘；"
                          "剩余 %d 件**没处理**（不是失败，是没轮到）。"
                          % (w2.abort, ok, max(0, len(todo) - w2.seen)), flush=True)
                    print(retry_hint("属性回写", failed_olds, a.snapshot, prog), flush=True)
                    save_and_check("rs_save_props_abort_%s" % rid)
                    return 1
        print("属性回写：成功 %d / 失败 %d%s" % (ok, bad,
              ("，回执缺件 %d（没轮到，待续做）" % missing_rows)
              if missing_rows else ""), flush=True)
        if failed_olds:
            print(retry_hint("属性回写", failed_olds, a.snapshot, prog), flush=True)
        save_and_check("rs_save_props_%s" % rid)

    # ---- 阶段 3：事件（第一遍：物件间的绑定）----
    leftover = set()
    if not a.no_events:
        ev = snap.get("events") or {}
        n_actions = len(event_bind_plan(ev, id_map,
                                        comp_index=_info_index(snap))[0])
        w3 = RunWatch(n_actions, heartbeat=a.heartbeat,
                      abort_after=a.abort_after, t0=time.perf_counter())
        r = apply_events(ev, id_map,
                            comp_index=_info_index(snap), watch=w3)
        for ln in (r.get("lines") or []):
            print(ln, flush=True)
        print("事件：绑定 %d / 失败 %d / 悬空引用 %d"
              % (len(r.get("applied") or []), len(r.get("failed") or []),
                 len(r.get("dangling") or [])), flush=True)
        if r.get("aborted"):
            print("⛔ 早停（事件重建）：%s。还有 %d 条引用**没试过**（不是失败，是没轮到）。"
                  % (r["aborted"], r.get("untried", 0)), flush=True)
            olds = [f.get("old_ugc_id") for f in (r.get("failed") or [])]
            print(retry_hint("事件重建", olds, a.snapshot, prog), flush=True)
            save_and_check("rs_save_events_abort_%s" % rid)
            return 1
        for f in (r.get("failed") or [])[:3]:
            print("  绑定失败 %s → ugc=%s：%s"
                  % (f.get("event_key"), f.get("ugc_id"),
                     str(f.get("error"))[:140]), flush=True)
        leftover = {d["old_ugc_id"] for d in (r.get("dangling") or [])
                    if d.get("old_ugc_id") is not None}
        if leftover:
            owners = _dangling_owners(snap, leftover)
            print("  悬空引用指向：%s" % (owners or "未知"), flush=True)
            # ⚠️ 别一律说"加 --with-story 就补绑"：指向运动器/未摆物件的那批它救不回来
            for key, n, hint in dangling_advice(
                    r.get("dangling") or [], _skip_reason_index(snap, plan, specs, only=(a.only_types or a.only_ids),
                                               limit=a.limit, overlapped=overlapped)):
                print("    %s ×%d → %s" % (key, n, hint), flush=True)
        save_and_check("rs_save_events_%s" % rid)

    # ---- 阶段 4：剧情 ----
    if a.with_story:
        doc = snap.get("story") or {}
        expected = story_node_count(doc)
        # ⛔ 先查目标图是不是已经有这批节点：`story_import` **不查重**，重跑一次
        # （同会话重跑、或 `--resume-from` 跨会话续做）就把剧情层整批再建一遍。
        # 而剧情三件套不占 `component_nums`，事后从件数上根本看不出来 ⇒ 只能在这里拦。
        have, mism, tree = {}, [], None
        try:
            tree = fetch_story_tree()
            have, mism = story_map_from_tree(doc, tree)
        except Exception as exc:
            print("剧情：读目标图剧情树失败（%s）⇒ 无法判断是否已导入"
                  % str(exc)[:120], flush=True)
        matched = len(have or {})
        if expected and matched >= expected:
            print("剧情：目标图已有 %d/%d 个节点（结构+内容匹配）⇒ **跳过导入**，"
                  "只并入 id 映射做事件补绑" % (matched, expected), flush=True)
            if have:
                m = merge_story_id_map(id_map, have)
                print("剧情 id 映射：并入 %d 条%s"
                      % (m["merged"],
                         ("，⚠️ 冲突 %s" % list(m["conflicts"])[:5])
                         if m["conflicts"] else ""), flush=True)
        elif matched:
            print("⚠️ 剧情：目标图上已有 %d 个匹配节点、但快照要 %d 个 ⇒ **不导入**"
                  "（半截状态再导一遍必然翻倍）。"
                  "请先 `python cli/story.py resume` 续跑上一次的导入，"
                  "或用 `--force-story-import` 明知故犯。" % (matched, expected), flush=True)
            if not a.force_story_import:
                tree = None
        if expected and (not matched or a.force_story_import) and tree is not None:
            r = apply_story(doc)
            print("剧情：%s" % json.dumps({k: v for k, v in r.items()
                                          if k in ("ok", "counts", "import_next_steps",
                                                   "created_count", "import_done")},
                                         ensure_ascii=False)[:200], flush=True)
            smap = r.get("story_id_map") or {}
            if smap:
                m = merge_story_id_map(id_map, smap)
                print("剧情 id 映射：并入 %d 条%s"
                      % (m["merged"], ("，⚠️ 冲突 %s" % list(m["conflicts"])[:5])
                         if m["conflicts"] else ""), flush=True)
            # ⚠️ 节点建出来了但字段没写全 = 本地看不出区别的静默失败（缺模板槽 → 事件补绑装不下）
            issues = r.get("field_issues") or []
            if issues:
                print("⚠️ 剧情字段写入告警 %d 处（节点可能缺模板槽）：" % len(issues),
                      flush=True)
                for it in issues[:5]:
                    print("    节点#%s %s：%s" % (it.get("node"),
                                                  it.get("skipped_field") or "",
                                                  str(it.get("error"))[:120]), flush=True)
            save_and_check("rs_save_story_%s" % rid)

        # ---- 阶段 5：补绑第一遍悬空、现在指向新建剧情节点的事件引用 ----
        if leftover and not a.no_events and smap:
            r2 = apply_events(snap.get("events") or {}, id_map, only_old=leftover,
                                  comp_index=_info_index(snap))
            print("事件补绑（剧情节点）：绑定 %d / 失败 %d / 仍悬空 %d"
                  % (len(r2.get("applied") or []), len(r2.get("failed") or []),
                     len(r2.get("dangling") or [])), flush=True)
            for f in (r2.get("failed") or [])[:3]:
                print("  补绑失败 %s → ugc=%s：%s"
                      % (f.get("event_key"), f.get("ugc_id"),
                         str(f.get("error"))[:140]), flush=True)
            save_and_check("rs_save_events2_%s" % rid)
        elif leftover:
            print("⚠️ 仍有 %d 条引用指向未重建的对象，未能补绑。" % len(leftover),
                  flush=True)

    # ---- 阶段 6：回对自检（引用逐条比对 property_id，别只看 key_ok）----
    if not a.no_events:
        _print_verify(verify_event_refs(snap.get("events") or {}, id_map))

    # 剧情映射是后并进来的 ⇒ 用**最终** id_map 覆写一次，`--verify-only` 才能事后回对
    with open(idmap_path(prog, a.snapshot), "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in id_map.items()}, f,
                  ensure_ascii=False, indent=1)

    print("\n完成。⚠️ 请**重进地图**核对：本地记账可能是假阳性，"
          "重进后的读数才是判据（可用 snapshot export + diff 与快照对账）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
