#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli/snapshot.py — 地图快照（L1：只读档案 / 导出 + 比对）

把当前编辑器地图的**全部可读状态**导出成一个 JSON 文件，并能对两份快照做 diff。

用法
----
  python cli/snapshot.py export [--out 路径.json] [--batch 200]
  python cli/snapshot.py inspect <文件> [--limit 12]
  python cli/snapshot.py diff <A> <B> [--tol 0.5] [--show 20]
  python cli/snapshot.py diff <A> <B> --json            # 机器可读

快照内容
--------
  map          地图设置 / 区域锚点 / 类型分布 / **地图属性**（默认背景音乐、排行榜、
               人数、封面与灵感笔记这类地图卡片级字段；2026-10-01 补，见 CODE_PROPS）
  components   全部组件：ugc_id、item_id、位置、旋转、缩放、完整 component_info
               （逻辑件的 `get_position3()` 是哨兵，真实坐标另记在 `pp`）
  events       事件表：事件键 + 每条引用（含稳定锚点，不依赖会变的 ugc_id）
  story        剧情树（与 `cli/story.py export` 同源，可直接喂给 story import）
  budget       导出时刻的编辑器记账（占用值 / 组件数 / 登记集合 / 待删除袋）

设计要点（都是踩过的坑）
------------------------
* **纯只读**：不写地图、不触发保存、不碰 `del_unit_uids`。
* **回传必须压平**：桥接层 `_safe()` 深度 >4 就变 `<deep>`、list/dict 各截 200。
  所以组件属性与剧情都在**游戏侧 `json.dumps` 成字符串**再回传（CLI 侧 loads）。
  实测：不压平的话 176 件的材质/颜色全是 `<deep>`。
* **组件分批 ≤200**：`_safe` 对 list 只留前 200 项，一批拿 5000 件会静默丢数据。
* **ugc_id 不可作锚点**：重载会重新编号。diff 用 `(item_id, x, z)` 量化锚点，
  与 `cli/event_import.py` 的匹配口径一致。
* ⚠️ 预算里的「待删除袋」非空时，占用值包含本地待删除态，**不可跨会话比较**。
"""
import argparse
import hashlib
import json
import math
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit                     # noqa: E402
from fxapi import find_layer as _find_layer      # noqa: E402  # 剥壳真源

FORMAT = "fuxiang-snapshot"
VERSION = 1
DEFAULT_DIR = os.path.join(ROOT, "snapshots")
BATCH_MAX = 200          # ⛔ 硬上限：_safe 对 list 只留前 200 项


# ---------------------------------------------------------------------------
# 游戏侧代码（压平回传，避开 _safe 深度截断）
# ---------------------------------------------------------------------------
def _bridge_dir():
    return os.path.join(ROOT, "bridge").replace("\\", "/")


CODE_COMPONENTS = '''
import sys
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
_ids = sorted(cm._ugc_id2component.keys())[%START%:%END%]
# 撤销栈残骸：删掉的组件仍留在 _ugc_id2component 里（原生 Ctrl+Z 依赖它），重进才消失。
# 不标出来的话，"删完东西再导出"就把幽灵件一起抄进快照，还原时凭空多摆一件。
try:
    _bag = {str(k): set(v or []) for k, v in (room.del_unit_uids or {}).items()}
except Exception:
    _bag = {}
_del_unit = _bag.get("unit") or set()
_del_comp = _bag.get("component") or set()
res = []
for gid in _ids:
    comp = cm._ugc_id2component.get(gid)
    if comp is None:
        continue
    u = getattr(comp, "_owner", None)
    if u is None:
        continue
    # item_id 必须**逐件**问 room.get_component_item_id：一个单元可以挂多个组件，
    # 而 u.ugc_type 只是该单元第一件的 item_id（界面控件全图共用一个 UgcGlobalUnit，
    # 拿 ugc_type 会把所有控件当成同一件 ⇒ 还原时整批摆成同一个控件）。
    it = {"g": gid, "i": _fe.component_item_id(room, gid, getattr(u, "ugc_type", None))}
    if getattr(u, "uid", None) in _del_unit or gid in _del_comp:
        it["del"] = 1
    try:
        p = u.get_position3()
        it["p"] = [round(p.x, 3), round(p.y, 3), round(p.z, 3)]
    except Exception:
        pass
    # ⚠️ get_position3() 对部分逻辑件返回哨兵 [0,-65536,0]，而单元上的
    # `position` 才是真实坐标（实机：203022 交互区 gid=14994 = [928,-67.923,-147]）。
    # 只在两者不一致时多记一个 pp，正常件不膨胀文件。
    try:
        rp = u.position
        real = [round(rp.x, 3), round(rp.y, 3), round(rp.z, 3)]
        cur = it.get("p")
        if cur and max(abs(a - b) for a, b in zip(cur, real)) > 0.01:
            it["pp"] = real
    except Exception:
        pass
    try:
        d = u.ugc_dir
        it["r"] = [round(d.x, 3), round(d.y, 3), round(d.z, 3)]
    except Exception:
        pass
    try:
        s = u.ugc_size
        it["s"] = [round(s.x, 4), round(s.y, 4), round(s.z, 4)]
    except Exception:
        pass
    ci = getattr(comp, "component_info", None) or {}
    # 压平回传：`_safe` 深度 >4 会变 '<deep>'，而嵌套值（如颜色 [["FFFFFF"]]）
    # 正好落在第 5 层。故先尝试严格 json.dumps（完整保真），只有撞上引擎的
    # taggeddict 等不可序列化类型时才回退 _jdump（它用 _safe 兜底，可能截断
    # 深层，但至少不会整条坏掉）。
    try:
        it["ci"] = json.dumps(ci, ensure_ascii=False)
    except Exception:
        it["ci"] = _fe._jdump(ci)
    res.append(it)
__result__ = {"n": len(_ids), "total": len(cm._ugc_id2component), "items": res}
'''

CODE_COUNT = '__result__ = {"total": len(cm._ugc_id2component)}'

# 按 component_type 筛着抓（只回这几类的属性袋）。为什么要筛而不是整表抓：
# 判"目标图是否已经有这一层"要对着**目标图现状**比内容，而全图抓一万四千件太贵；
# 界面控件那层最多几十件，筛类型一次就够（2026-10-01 实机：新图 48 件、返回 43 KB）。
CODE_TYPE_CENSUS = '''
import sys, json
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
_types = set(int(x) for x in %TYPES%)
# 待保存提交的删除**不是地图内容**（与 CODE_COMPONENTS 的 `del` 标记、以及
# classify_components 的 deleted_residue 同一条口径）。2026-10-02 实机：清理现场时
# 127 件实验件被标记待删，本地索引里还留着（重进才消失）——不排掉的话，"目标图已经
# 有这一件"的判据会把**要被删掉的幽灵**当成已有件，于是计划里那几件被扣掉不摆，
# 交付的图就少内容（而且引用还会绑到那个幽灵号上）。占用/计数同理。
try:
    _bags = {str(k): set(v or []) for k, v in (room.del_unit_uids or {}).items()}
except Exception:
    _bags = {}
_del_unit = _bags.get("unit") or set()
_del_comp = _bags.get("component") or set()
res = []
for gid in sorted(cm._ugc_id2component.keys()):
    comp = cm._ugc_id2component.get(gid)
    if comp is None:
        continue
    ci = getattr(comp, "component_info", None) or {}
    cid = ci.get("component_id")
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        continue
    if cid not in _types:
        continue
    u0 = getattr(comp, "_owner", None)
    if getattr(u0, "uid", None) in _del_unit or gid in _del_comp:
        continue
    try:
        blob = json.dumps(ci, ensure_ascii=False)
    except Exception:
        blob = _fe._jdump(ci)
    # 变换也带上：判"目标图已经有这一件"必须连位置一起比 —— 作者把出厂角色挪过位置时，
    # 只比属性袋会把它当成"已有"而漏摆，目标图剩下的那件在错的地方（1:1 反而更差）。
    row = {"g": gid, "i": cid, "ci": blob}
    u = getattr(comp, "_owner", None)
    if u is not None:
        try:
            pos = u.get_position3()
            row["p"] = [round(pos.x, 3), round(pos.y, 3), round(pos.z, 3)]
        except Exception:
            pass
        try:
            d = u.ugc_dir
            row["r"] = [round(d.x, 3), round(d.y, 3), round(d.z, 3)]
        except Exception:
            pass
        try:
            sz = u.ugc_size
            row["s"] = [round(sz.x, 4), round(sz.y, 4), round(sz.z, 4)]
        except Exception:
            pass
    res.append(row)
__result__ = {"n": len(res), "rows": res}
'''

# 地图级「属性」层：`room.ugc_props_info` 里那些**不是组件、也不是玩法开关**的字段。
# 为什么要单独立一层（2026-10-01 用户报「还原出来的图没有地图默认背景音乐」）：
# BGM 不在组件袋里（203920 音乐事件在两张图上逐字段相同），也不在 map_setting 的
# 数字键里（MUSIC=7/SOUND=6 两张图都没写过），它在 props 的 `ugc_bgm_conf`
# ＝ {mode: BgmConf(1默认/2静音/3云音乐), param: {...}, from_comp: bool}。
# 这一层以前根本不采，于是"逐层一致"能在一张**开局没有音乐**的图上印绿灯。
# 一并采的还有 rank_setting / player_num / other_photos / ugc_top_view_photo /
# map_edit_data（灵感笔记那类快捷配置）：采到才有得比，采不到就报"没比成"。
# 已经由别的层覆盖的（ugc_setting→地图设置、event_info→事件、camp/class→队伍）不重复采。
CODE_PROPS = '''
import sys, json
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
core = _fe.get_core()
room = core.room
_keys = %KEYS%
out = {}
try:
    _p = getattr(room, "ugc_props_info", None) or {}
except Exception:
    _p = {}
for _k in _keys:
    try:
        # 键在不在，与值是 null，是两回事：还原侧要能分辨"这张图没设过"和"这层没采到"
        out[_k] = {"present": _k in _p, "value": _p.get(_k)}
    except Exception as e:
        out[_k] = {"present": False, "value": None, "err": type(e).__name__}
try:
    out["_music_event_ids"] = list(
        _fe._get_edit_ui(room)._get_music_event_mgr_part().get_music_event_id_list() or [])
except Exception:
    out["_music_event_ids"] = None
# 严格 dumps：走 _safe() 会被 "<deep>" 截断，那等于没采到
try:
    __result__ = {"props_json": json.dumps(out, ensure_ascii=False, sort_keys=True)}
except Exception:
    __result__ = {"props_json": _fe._jdump(out)}
'''

# 采哪几个 props 键（引擎里的 UGC_PROPS 全表见 SNAPSHOT.md「地图属性层」）
PROPS_KEYS = ("ugc_bgm_conf", "map_edit_data", "rank_setting", "player_num",
              "other_photos", "ugc_top_view_photo")

CODE_BUDGET = '''
import sys, json
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
core = _fe.get_core()
room = core.room
ui = _fe._get_edit_ui(room)


def _num(name):
    f = getattr(ui, name, None)
    if not callable(f):
        return None
    try:
        return int(f())
    except Exception:
        try:
            return f()
        except Exception:
            return None


try:
    _del = room.del_unit_uids or {}
    # 空桶不是待删除（摆放操作会在袋里留下 {'unit': []} 这种空桶），带着进文件只会
    # 让下游的"有待删除"真值判断全部误报；只有真挂着 id 的桶才改变占用口径。
    _del = {str(k): list(v or []) for k, v in _del.items() if v}
except Exception:
    _del = None

# 待删除袋的**身份**（不只是条数）：这批删除会在下一次保存（含退出编辑器自动保存）
# 时被提交。只印 `{'unit': 7, 'component': 4}` 数得出几件、认不出是哪件 ⇒ 等发现时已存进去。
# 两个桶都要落到组件上：'component' 桶存的直接是 ugc_id，'unit' 桶存的是单元 uid，
# 而一个单元可以挂多个组件（共用单元上会连带兄弟件一起看不见）。
_pending = []
try:
    _bags = {str(k): set(v or []) for k, v in (room.del_unit_uids or {}).items()}
except Exception:
    _bags = {}
_by_uid = {}
for _g, _c in (cm._ugc_id2component or {}).items():
    _by_uid.setdefault(getattr(_c, "_owner", None) and getattr(_c._owner, "uid", None),
                        []).append(_g)


def _pname(_c):
    for _e in ((_c.component_info or {}).get("events") or []):
        _v = (_e or {}).get("info")
        _v = _v[0] if isinstance(_v, (list, tuple)) and _v else _v
        if isinstance(_v, str) and _v and not _v.replace(".", "").isdigit():
            return _v
    return None


for _b, _ids in sorted(_bags.items()):
    for _x in sorted(_ids):
        _gs = [_x] if _b == "component" else list(_by_uid.get(_x) or [])
        if not _gs:
            _pending.append({"bucket": _b, "id": _x, "gid": None,
                             "cid": None, "name": None})
            continue
        for _g in _gs:
            _c = cm._ugc_id2component.get(_g)
            _pending.append({"bucket": _b, "id": _x, "gid": _g,
                             "cid": ((_c.component_info or {}).get("component_id")
                                     if _c else None),
                             "name": _pname(_c) if _c else None})
_reg = set(getattr(ui, "all_ugc_uid_set", set()) or set())
# ⚠️ 2026-10-02 实机：`get_all_ugc_component_nums()` 把**已登记但待保存删除**的残影
# 也算进去。清理现场后（5685 件挂着待删除标记、要等重进才从本地索引消失）本地计数
# 是 5585，而服务端真值只剩出厂 113 件 ⇒ 续做的"预算复核"会以 5585+9286>15000 拒写
# （那条闸门不受 --yes 放行）。待删的不是地图内容（与 CODE_COMPONENTS 的 `del` 标记、
# classify_components 的 deleted_residue 同一条口径），这里单列一个扣掉它的活值。
_ghost_registered = 0
# ⚠️ 桶里的值可能是 int 也可能是数字字符串（引擎两种都出现过），两边都收进判据 ——
# 2026-10-02 实机踩过：只比 `str(_g)` 会让判据恒为 False，扣不掉任何残影。
_del_comp_norm = set()
for _x in (_bags.get("component") or set()):
    _del_comp_norm.add(_x)
    try:
        _del_comp_norm.add(int(_x))
    except Exception:
        pass
_del_unit_norm = set()
for _x in (_bags.get("unit") or set()):
    _del_unit_norm.add(_x)
    try:
        _del_unit_norm.add(int(_x))
    except Exception:
        pass
for _g, _c in (cm._ugc_id2component or {}).items():
    _u = getattr(_c, "_owner", None)
    _uid = getattr(_u, "uid", None) if _u is not None else None
    if _uid is None or _uid not in _reg:
        continue
    if _g in _del_comp_norm or _uid in _del_unit_norm:
        _ghost_registered += 1
_cn_raw = _num("get_all_ugc_component_nums")
__result__ = {"occupancy": _num("get_current_occupancy"),
              "max_occupancy": _num("get_max_occupancy"),
              "component_nums": _cn_raw,
              "component_nums_live": (None if _cn_raw is None
                                      else int(_cn_raw) - _ghost_registered),
              "pending_deletion_registered": _ghost_registered,
              "max_component_nums": _num("get_max_ugc_component_nums"),
              "registered": len(getattr(ui, "all_ugc_uid_set", set()) or set()),
              "del_unit_uids": _del,
              "pending_deletions": _pending[:40],
              "pending_deletions_total": len(_pending)}
'''


# 地形采样。UGC 编辑器**没有地形雕刻能力**（详见 TERRAIN.md）：地形是「场景框架」
# 的一部分，由地图设置字段 14 决定，room 侧只能**查询**高度（`get_pos3_height`）。
# 所以能做的复刻是：记下框架 id + 采样一张高度场（供几何体拼装用）。
CODE_TERRAIN = '''
import sys, json, math3d
if %BRIDGE% not in sys.path:
    sys.path.insert(0, %BRIDGE%)
import fuxiang_edit as _fe
room = _fe.get_core().room
x0, z0, x1, z1, step = %X0%, %Z0%, %X1%, %Z1%, %STEP%
xs = []
v = x0
while v <= x1:
    xs.append(v)
    v += step
zs = []
v = z0
while v <= z1:
    zs.append(v)
    v += step
grid = []
nonnull = 0
for x in xs:
    row = []
    for z in zs:
        try:
            h = room.get_pos3_height(math3d.vector(float(x), 9999.0, float(z)))
            h = None if h is None else round(float(h), 2)
        except Exception:
            h = None
        if h is not None:
            nonnull += 1
        row.append(h)
    grid.append(row)
__result__ = {
    "grid_json": json.dumps(grid),
    "meta": {"x0": x0, "z0": z0, "x1": x1, "z1": z1, "step": step,
             "nx": len(xs), "nz": len(zs), "sampled": nonnull,
             "no_terrain_scene": getattr(room, "no_terrain_scene", None),
             "has_terrain_walls": getattr(room, "has_terrain_walls", None)},
}
'''


def _unwrap(node, key):
    """找含 key 的那一层；剥壳规则见 cli/fxapi.py::find_layer（唯一真源）。"""
    return _find_layer(node, key)


def _call(op, args=None, timeout=120):
    return call_edit(op, args or {}, timeout=timeout)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def _stamp(anomalies, layer, kind, msg):
    """把一条采集异常盖进印章列表（None = 调用方不收集，行为不变）。

    只 log 的异常留在 stdout 里，文件被转手之后就没人知道采集时缺没缺层 ——
    `check` 的 fetch_integrity 靠这个印章说话。
    """
    if anomalies is not None:
        anomalies.append({"layer": layer, "kind": kind, "msg": msg})


def fetch_components(batch=BATCH_MAX, log=print, anomalies=None):
    """分批抓全部组件。每批 ≤200（_safe 的 list 截断上限）。

    anomalies 传 list 进来就顺带盖采集印章：批次中止（aborted_batch，warn 级）
    与声明/实收不符（count_mismatch，info 级——多半是"无 _owner 组件被合法跳过"，
    但也兜着别的跳过原因，供人工分辨）。
    """
    d = _call("eval", {"code": CODE_COUNT})
    total = (_unwrap(d, "total") or {}).get("total") if d else None
    if not total:
        raise RuntimeError("抓不到组件总数（编辑器未打开地图？）")
    items = []
    aborted = False
    start = 0
    while start < total:
        step = min(int(batch or BATCH_MAX), BATCH_MAX)
        code = (CODE_COMPONENTS.replace("%BRIDGE%", repr(_bridge_dir()))
                               .replace("%START%", str(start))
                               .replace("%END%", str(start + step)))
        d = _call("eval", {"code": code})
        node = _unwrap(d, "items")
        got = (node or {}).get("items") or []
        if not got:
            log("  批 start=%d 无返回，中止（已收 %d）" % (start, len(items)))
            _stamp(anomalies, "components", "aborted_batch",
                   "组件批次 start=%d 无返回，中止：实收 %d / 声明 %d"
                   % (start, len(items), total))
            aborted = True
            break
        items.extend(got)
        log("  组件 %d / %d" % (len(items), total))
        start += step
    if len(items) != total and not aborted:
        log("  ⚠️ 声明 %d、实收 %d（可能有组件无 _owner，已跳过）" % (total, len(items)))
        _stamp(anomalies, "components", "count_mismatch",
               "组件声明 %d、实收 %d（批次无中止；多半是「无 _owner 组件被合法跳过」，"
               "也可能潜伏别的跳过原因）" % (total, len(items)))
    return items


def fetch_events(log=print, anomalies=None):
    d = _call("event_list", {})
    node = _unwrap(d, "events")
    if not node:
        log("  ⚠️ 事件表为空或读取失败")
        _stamp(anomalies, "events", "fetch_failed",
               "事件表读取失败（桥接无返回）⇒ 文件里的 0 条是缺省值，不是「这张图没有事件」")
        return {"count": 0, "events": [], "saved_keys": []}
    log("  事件 %d 条" % node.get("count", 0))
    return node


def fetch_story(log=print, anomalies=None):
    d = _call("story_export_doc", {})
    node = _unwrap(d, "doc_json")
    if not node:
        log("  ⚠️ 剧情读取失败（story_export_doc 无返回）")
        _stamp(anomalies, "story", "fetch_failed",
               "剧情读取失败（story_export_doc 无返回）⇒ story=null 是缺省值，不是「这张图没有剧情」")
        return None
    text = node.get("doc_json")
    if not text:
        _stamp(anomalies, "story", "fetch_failed",
               "剧情 doc_json 为空 ⇒ story=null 是缺省值，不是「这张图没有剧情」")
        return None
    try:
        doc = json.loads(text)
    except Exception as e:
        log("  ⚠️ 剧情 JSON 解析失败: %s" % e)
        _stamp(anomalies, "story", "fetch_failed",
               "剧情 JSON 解析失败: %s" % e)
        return None
    log("  剧情 %d / 对话 %d / 选项 %d"
        % (node.get("story_count", 0), node.get("dialogue_count", 0),
           node.get("choice_count", 0)))
    return doc


def fetch_terrain(step=100.0, bounds=None, log=print):
    """采样地形高度场（只读）。

    ⛔ 采样网格必须**压成 JSON 字符串**回传：`_safe` 对 list 只留前 200 项，
    一张 23×23 的网格直接回传会被静默截断（529 项 → 只剩 200）。
    """
    x0, z0, x1, z1 = bounds or (-1120.0, -1120.0, 1120.0, 1120.0)
    code = (CODE_TERRAIN.replace("%BRIDGE%", repr(_bridge_dir()))
                        .replace("%X0%", repr(float(x0)))
                        .replace("%Z0%", repr(float(z0)))
                        .replace("%X1%", repr(float(x1)))
                        .replace("%Z1%", repr(float(z1)))
                        .replace("%STEP%", repr(float(step))))
    d = _call("eval", {"code": code}, timeout=180)
    node = _unwrap(d, "grid_json")
    if not node:
        log("  ⚠️ 地形采样失败")
        return None
    meta = node.get("meta") or {}
    grid = json.loads(node.get("grid_json") or "[]")
    log("  地形采样 %s×%s = %d 点，有效高度 %d"
        % (meta.get("nx"), meta.get("nz"),
           (meta.get("nx") or 0) * (meta.get("nz") or 0), meta.get("sampled", 0)))
    return {"meta": meta, "grid": grid}


def fetch_budget(log=print):
    code = CODE_BUDGET.replace("%BRIDGE%", repr(_bridge_dir()))
    d = _call("eval", {"code": code})
    node = _unwrap(d, "occupancy") or _unwrap(d, "registered") or _unwrap(d, "del_unit_uids")
    return node or {}


def fetch_props(log=print):
    """地图级属性层（含「默认背景音乐」ugc_bgm_conf）。取不到返回 None，不返回空 dict。

    None 与 {} 必须分得开：None=这层没采到 ⇒ 验收时报"没比成"；
    采到了但值全是 null=这张图确实什么都没设 ⇒ 才是真的"两侧一致"。
    """
    code = (CODE_PROPS.replace("%BRIDGE%", repr(_bridge_dir()))
            .replace("%KEYS%", repr(list(PROPS_KEYS))))
    d = _call("eval", {"code": code})
    node = _unwrap(d, "props_json")
    text = (node or {}).get("props_json")
    if not text:
        log("  ⚠️ 地图属性层没采到（桥接没回 props_json）⇒ 这一层以后不能算比过")
        return None
    try:
        return json.loads(text)
    except Exception as e:
        log("  ⚠️ 地图属性层解析失败：%s" % e)
        return None


def fetch_map(log=print):
    d = _call("map_info", {})
    node = _unwrap(d, "map_setting")
    if not node:
        return {}
    return node


def build_snapshot(batch=BATCH_MAX, terrain_step=None, log=print):
    # 采集完整性印章：异常不进文件的话，转手后没人知道这份导出采集时缺没缺层。
    anomalies = []
    log("采集地图信息…")
    mp = fetch_map(log)
    log("采集组件…")
    comps = fetch_components(batch=batch, log=log, anomalies=anomalies)
    log("采集事件…")
    events = fetch_events(log, anomalies=anomalies)
    log("采集剧情…")
    story = fetch_story(log, anomalies=anomalies)
    log("采集预算记账…")
    budget = fetch_budget(log)
    log("采集地图属性（默认背景音乐这一层）…")
    props = fetch_props(log)
    terrain = None
    if terrain_step:
        log("采样地形高度场…")
        terrain = fetch_terrain(step=terrain_step, log=log)

    name = (mp.get("map_setting") or {}).get("name") or ""
    ms = mp.get("map_setting") or {}
    return {
        "format": FORMAT,
        "version": VERSION,
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tool": "cli/snapshot.py",
        "map": {
            "name": name,
            "map_setting": mp.get("map_setting"),
            "anchor_info": mp.get("anchor_info"),
            "type_distribution": mp.get("type_distribution"),
            "total_components": mp.get("total_components"),
            # 「场景框架」决定地形底子：ugc_setting_config[14]，可选 4001/4002/4003。
            # UGC 没有地形雕刻，复刻地形 = 复刻这个 id（详见 TERRAIN.md）。
            "framework_id": ms.get("14"),
            # 地图级属性（默认背景音乐等）：不是组件、也不是玩法开关，见 CODE_PROPS 注释
            "props": props,
        },
        "components": comps,
        "events": events,
        "story": story,
        "terrain": terrain,
        "budget": budget,
        "counts": {
            "components": len(comps),
            "events": (events or {}).get("count", 0),
            "stories": len((story or {}).get("stories", [])),
            # 空 = 这份是「所记即所是」的完整导出；非空时 check 的 fetch_integrity 会点名
            "fetch_anomalies": anomalies,
        },
    }


# ---------------------------------------------------------------------------
# 纯逻辑：锚点 / 归一化 / diff（可离线单测，不依赖游戏）
# ---------------------------------------------------------------------------
def _q(x, tol):
    """按容差量化坐标，使浮点抖动不产生假差异。"""
    try:
        return int(round(float(x) / tol))
    except (TypeError, ValueError):
        return None


def anchor_of(comp, tol=0.5):
    """稳定锚点 `(item_id, x格, z格)`。与 event_import 的匹配口径一致。

    不用 ugc_id：它每次重载都会重新编号。y 不参与（逻辑组件 y 是哨兵）。
    ⚠️ x/z 取**可信坐标**（`pp` 兜底），不能直接拿 `p`：逻辑件的 `p` 整条是哨兵
    `[0,-65536,0]`、真值只在 `pp` 里。实机 203022 交互区因此被坑过 —— 源图锚点算成
    `203022@0,0`、目标图算成 `203022@928,-147` ⇒ 两边一模一样的事件引用被报成「绑定变更」。
    """
    rp = real_pos(comp)
    p = rp if rp else (comp.get("p") or [None, None, None])
    return (comp.get("i"), _q(p[0], tol), _q(p[2], tol))


def _info_of(comp):
    """组件的 component_info（快照里存的是 JSON 字符串）。"""
    ci = comp.get("ci")
    if isinstance(ci, str):
        try:
            return json.loads(ci)
        except Exception:
            return {"_parse_error": True}
    return ci or {}


def _events_norm(info):
    """把 component_info['events'] 规范成可比较的 {property_id: value}。"""
    evs = (info or {}).get("events") or []
    out = {}
    for e in evs:
        if not isinstance(e, dict):
            continue
        out[str(e.get("type"))] = e.get("info")
    return out


# 属性袋里存**落盘 ugc_id**的字段（组合件成员表 / 运动器绑定的部件表）。
# 与 bridge/fuxiang_edit_ops.py::UGCID_REF_PROPERTIES 同源：还原时按这张表把旧 id
# 换成新 id，比对也必须按同一张表归一，否则两份不同会话导出的快照会报假差异。
# 2026-10-01 实机：春岚复原后又跑 accept --live，仍报「属性变更 4」，其中 3 条
# 就是组合件 20390018701 换了个号（[[14982]]→[[14998]]），内容一件没动。
ID_REF_PROPERTIES = ("10000118701", "10000018701", "20390018701", "20390003001")

# 剧情节点引用（对话→所属剧情、选项→所属对话/跳转目标）。与
# `bridge/fuxiang_edit_ops.py::STORY_REF_PROPERTIES` 同源。
# 2026-10-01 实机：春岚搬到新图后，对话件的「所属剧情」写着 117，而 117 在那张图上
# 是一件**时钟件**（真正的剧情件是 133）⇒ 这一族槽位不换号就会接错线，
# 而比对时不归一又会把它报成"值不同"（其实是同一指向、号变了）。
STORY_REF_PROPERTIES = ("1000700240001", "1000700340001",
                        "1000700340005", "1000700340006")
# 归一与换号**都用这张并集**：少一族槽，还原侧写错、比对侧误报，两边还不一致。
ALL_REF_PROPERTIES = ID_REF_PROPERTIES + STORY_REF_PROPERTIES


def _remap_ids(value, lut):
    """递归把 info 里的旧 ugc_id 换成配对给出的新 id（int / str 两种形态都认）。"""
    if isinstance(value, (list, tuple)):
        return [_remap_ids(v, lut) for v in value]
    if isinstance(value, dict):
        return {k: _remap_ids(v, lut) for k, v in value.items()}
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        new = lut.get(str(value))
        if new is None:
            return value
        # 保持原形态：袋里存的是 "123" 就换回 "123"，别把 str 换成 int 造出假差异
        return str(new) if isinstance(value, str) else new
    return value


def _id_lut(pairs):
    """配对结果 → {旧会话 ugc_id: 新会话 ugc_id}（键统一成字符串）。"""
    lut = {}
    for x, y in pairs:
        if x is None or y is None:
            continue
        gx, gy = x.get("g"), y.get("g")
        if gx is not None and gy is not None:
            lut[str(gx)] = gy
    return lut


def _renumber_only(ea, eb, lut):
    """把 a 侧 id 引用字段按配对换号后，是否与 b 侧逐字段相等。

    只在**白名单字段**上做替换：别的字段里也可能出现整数（模型号、材质号…），
    它们与组件 gid 撞号时若一并替换，就会把真差异洗成"一致"。
    """
    if not lut:
        return False
    return _norm_ids(ea, lut) == eb


def _norm_ids(ea, lut):
    return {k: (_remap_ids(v, lut) if str(k) in ALL_REF_PROPERTIES else v)
            for k, v in ea.items()}


def _within_tol(a, b, tol):
    """同形状同顺序，浮点差 ≤ tol、其余逐字段相等。

    tol 就是 `accept/diff --tol` 那条坐标容差：配对本来就按它认件，属性层用同一个
    尺度，不然会出现"位置算一致、袋里的坐标算不一致"这种自相矛盾的判据。
    """
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_within_tol(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_within_tol(a[k], b[k], tol) for k in a)
    return a == b


def _same_set(a, b, tol, _depth=0):
    """忽略**列表顺序**后能否一一配对（每个元素都在 tol 内找到归宿）。

    递归到嵌套层：出生点那种 `[{…,"bron":[点,点,点,点]}]` 结构，换序发生在**内层**，
    只看顶层会把它误判成"内容变了"（2026-10-01 春岚的 1000400113101 即此形）。
    代价是 O(n²) 配对，所以只在 `_SET_MATCH_MAX` 以内尝试，超过就从严报 value ——
    判据宁可吵一点，也不能把 accept 跑成几分钟的停顿。
    """
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    if max(len(a), len(b)) > _SET_MATCH_MAX:
        return False
    pool = list(b)
    for x in a:
        hit = next((i for i, y in enumerate(pool)
                    if _equiv(x, y, tol, _depth + 1)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def _equiv(a, b, tol, _depth=0):
    """顺序无关 + tol 内的等价（列表可换序，浮点可差 tol，字典按键）。"""
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_equiv(a[k], b[k], tol, _depth) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if _within_tol(a, b, tol):          # 顺序也没变，走快路
            return True
        return _same_set(a, b, tol, _depth)
    return _within_tol(a, b, tol)


# 差异性质：value=内容真的变了；resorted=同一批元素只是顺序不同；precision=顺序也没变、
# 只差在浮点末位（引擎跨会话重新规范化，2026-10-01 春岚的出生点列表就是这种）。
DIFF_KINDS = ("value", "resorted", "precision")
SOFT_KINDS = ("resorted", "precision")
_SET_MATCH_MAX = 400          # 换序匹配的元素上限，见 _same_set


def _diff_kind(va, vb, tol):
    if _within_tol(va, vb, tol):
        return "precision"
    if _same_set(va, vb, tol):
        return "resorted"
    return "value"


TRANSFORM_FIELDS = (("p", u"落点"), ("r", u"旋转"), ("s", u"缩放"), ("pp", u"真实位置"))
# 聚合口径从严：value > resorted > precision（renumber 不参与变换，只给袋字段用）。
_KIND_SEVERITY = {"renumber": -1, "precision": 0, "resorted": 1, "value": 2}


def _transform_diff(x, y, tol):
    """变换（旋转 / 缩放 / pp 真实位置）的差异：返回 `(差异字段名, {"<transform>": 性质})`。

    ⚠️ 必须与属性袋**各算一遍**，不能写成 `if 袋不同: … elif 变换不同: …`：
    一件同时"袋只差重编号"和"旋转不同"时，后一种分支会把旋转差异整件吞掉 ——
    归一做得越好，"只看一边"的分支越危险（少报比噪声坏）。
    """
    fields, kinds = [], []
    for key, label in TRANSFORM_FIELDS:
        va, vb = x.get(key), y.get(key)
        if key == "pp":
            # 单边有 pp 是**字段版本差**（pp 是 2026-09-30 才加的），不是挪动：
            # 新旧文件一比就满是假差异，真问题反被噪声盖掉。
            if not _pp_moved(x, y):
                continue
        elif key == "p":
            # 落点也要比：春岚→新图就有一件薄平台被引擎摆到 248 单位外、还低 58，
            # 而以前 `p` 不在比对字段里 ⇒ "0 差异"里藏着位置错（摆放侧也不回读落点）。
            # 两边任一是哨兵位（逻辑件 `get_position3` 撒谎）就没有可比性，跳过交给 pp。
            if _sentinel(va) or _sentinel(vb) or not va or not vb:
                continue
        if (va or None) == (vb or None):
            continue
        fields.append(label)
        kinds.append(_diff_kind(va, vb, tol))
    if not fields:
        return [], {}
    worst = max(kinds, key=lambda k: _KIND_SEVERITY.get(k, 2))
    return fields, {"<transform>": worst}


def _is_hard_change(row):
    """这件的属性变更是不是"内容变了"。没有 kinds（旧结果/纯变换差异）一律从严。"""
    kinds = row.get("kinds") or {}
    if not kinds:
        return True
    return any(v == "value" for v in kinds.values())


def _anchor_index(components, tol):
    """ugc_id → 稳定锚点字符串（事件引用需要靠它跨会话比较）。

    ⚠️ 光靠坐标**不够**：逻辑件的 `p` 恒为哨兵，332 个运动器会全部折叠成同一个锚点
    `203911@0,0` ⇒ "绑定从 A 挪到 B" 在 diff 里看起来**没变**（漏报，比噪声坏）。
    所以锚点一律带上内容签名摘要。

    ⚠️ 但**不许**按"同组第几件"编号：2026-10-01 在春岚上实测，只要图里多/少一件同签名的
    双胞胎，别的件标签就会跟着漂，把 7 条**一模一样**的引用报成"绑定变更"。
    标签只能由组件自己决定。内容完全相同（同 item、同坐标、同属性袋）的两件因此共用标签 ——
    这是**语义正确**而不是漏洞：它们本来就不可区分，引用从一件挪到另一件不算变化。
    """
    idx = {}
    for c in components or []:
        a = anchor_of(c, tol)
        sig = _content_sig(c)
        idx[c.get("g")] = "%s@%s,%s#%s" % (
            a[0], a[1], a[2], hashlib.md5(sig.encode("utf-8")).hexdigest()[:8])
    return idx


def _norm_event_table(snap, tol):
    """事件表 → `{事件键: {"sort_id", "in_save", "refs"}}`。

    以前每个事件只比 `(锚点, property_id, 当前值)` 三元组的排序，**扔掉了两样要紧的**：

    * `in_save`：事件在不在保存集合里（`get_save_event_info()` 只收 info 非空的键）。
      不在 ⇒ 保存载荷里根本没它，重进就没有这条事件 —— 这恰恰是本项目栽过的
      「保存后引用丢失」那一类，必须进判据。
    * `params_idx`：事件键在槽位 info 里的**下标**。同一个组件、同一个 property_id、
      值也相同，但注册表指的是另一个下标 ⇒ 那是一条陈旧/错位引用，光比前三个看不出来。

    `sort_id` 也带出来，但只作 **warn**（面板顺序；目标图若自带框架事件，序号会整体
    偏移，那不代表玩法不同）。
    """
    idx = _anchor_index(snap.get("components") or [], tol)
    out = {}
    for e in ((snap.get("events") or {}).get("events") or []):
        key = e.get("event_key")
        refs = []
        for r in e.get("refs") or []:
            uid = r.get("ugc_id")
            refs.append((idx.get(uid, "uid:%s" % uid),
                         str(r.get("property_id")),
                         str(r.get("current_value")),
                         str(r.get("params_idx"))))
        out[key] = {"sort_id": e.get("sort_id"), "in_save": bool(e.get("in_save")),
                    "refs": sorted(refs)}
    return out


def _story_field_capture(doc):
    """这份剧情采集里**实际出现**的字段名（按层）。

    导出器对空值是省键的（`effect_camp` 没设就不会出现），所以"某字段没出现"在
    同一版本内是"没设"；但**跨导出器版本**就不是了 —— 老文件整批字段都没采
    （实机：`snapshots/_verify_a.json` 的剧情节点只有 `ugc_id/owner_uid/dialogues`）。
    拿它跟新文件比，13 个字段全是 `None vs 1`，会凭空报一堆差异。
    ⇒ 返回每层"有没有采到可比字段"，没采到的那层要说**没比成**，不能装一致。
    """
    caps = {"story": set(), "dialogue": set(), "choice": set()}
    nodes = {"story": 0, "dialogue": 0, "choice": 0}
    def _walk(s_list, d_list, c_list):
        for s in (s_list or []):
            nodes["story"] += 1
            caps["story"] |= (set(s) & set(STORY_COMPARE_FIELDS["story"]))
            for d in (s.get("dialogues") or []) + (d_list or []):
                nodes["dialogue"] += 1
                caps["dialogue"] |= (set(d) & set(STORY_COMPARE_FIELDS["dialogue"]))
                for c in (d.get("choices") or []) + (c_list or []):
                    nodes["choice"] += 1
                    caps["choice"] |= (set(c) & set(STORY_COMPARE_FIELDS["choice"]))
    if isinstance(doc, dict):
        _walk(doc.get("stories"), doc.get("orphan_dialogues"), doc.get("orphan_choices"))
    has_nodes = nodes["story"] or nodes["dialogue"] or nodes["choice"]
    missing = [lv for lv in caps if nodes[lv] and not caps[lv]]
    return {"has_nodes": bool(has_nodes), "uncaptured_levels": missing,
            "captured": {lv: sorted(v) for lv, v in caps.items()}}


def _story_field_gap(cap_a, cap_b):
    """`{层: [只在一边采到过的字段]}` —— 版本差，这些字段**没法比**。

    导出器对空值是省键的，所以"两边都不出现"= 都没设 = 一致（不用跳）；
    但"一边出现、另一边从来不出现"是**老导出没这一列**，拿 None 去跟人家的 1 比
    就是一片假差异（实机：`snapshots/_verify_a.json` 的剧情节点只采到 3 个字段，
    现在的导出采到 13 个）。正确的说法是"这些字段没比成"，不是"这些字段不同"。
    """
    gaps = {}
    for lv in STORY_COMPARE_FIELDS:
        a = set((cap_a.get("captured") or {}).get(lv) or ())
        b = set((cap_b.get("captured") or {}).get(lv) or ())
        only = sorted(a ^ b)
        if only:
            gaps[lv] = only
    return gaps


def _flatten_gaps(gaps, uncaptured_levels):
    out = ["%s.%s" % (lv, f) for lv, fs in sorted(gaps.items()) for f in fs]
    out += ["%s 层整层未采" % lv for lv in uncaptured_levels]
    return out


def _event_capture(snap):
    """事件引用里的新维度在不在（`in_save` / `key_ok`）。老导出没采 ⇒ 那一维不能说一致。"""
    evs = ((snap.get("events") or {}).get("events") or [])
    refs = [r for e in evs for r in (e.get("refs") or [])]
    return {"events": len(evs),
            "has_in_save": bool(evs) and all("in_save" in e for e in evs),
            "has_key_ok": bool(refs) and all("key_ok" in r for r in refs)}


def _event_untrusted_refs(snap):
    """`key_ok=false` 的引用：槽位里已经没有这个事件键了 ⇒ 这条读数不能信。"""
    bad = []
    for e in ((snap.get("events") or {}).get("events") or []):
        for r in e.get("refs") or []:
            if r.get("key_ok") is False:
                bad.append("%s#%s" % (e.get("event_key"), r.get("ugc_id")))
    return bad


def _event_only_sort_moved(ea, eb, k):
    a, b = ea[k], eb[k]
    return a["refs"] == b["refs"] and a["in_save"] == b["in_save"] \
        and a["sort_id"] != b["sort_id"]


# 剧情层**参与比对**的字段。真源是 `bridge/fuxiang_story.py` 的 `FIELDS` 表
# （`tests/test_snapshot_story_layer.py` 用 AST 比对两张表，防工具侧与桥接侧各认一套，
# 手法与 `GROUP_CREATE_TYPES` 一致）。
# 不比的是三样易变项：`ugc_id`/`owner_uid`（落盘 id 只在本会话有效）、`edit_time`
# （每次编辑都变）。靠 id 相连的 `story_id`/`dialogue_id`/`jump_*` 不直接比数值，
# **先解析成名字**再比 —— 否则跨会话一比就全是假差异，而真差异反而看不出来。
STORY_COMPARE_FIELDS = {
    "story": ("enable_quit", "effect_type", "effect_camp", "effect_class",
              "effect_count", "only_trigger_self", "only_enable_host",
              "hide_player_units", "jump_type", "voice_config",
              "start_play_event", "end_play_event", "quit_play_event"),
    "dialogue": ("sort_id", "talker_type", "npc", "custom", "content",
                 "perform_type", "show_quit", "text_type", "text_size",
                 "text_color", "h_align", "v_align", "start_event", "end_event"),
    "choice": ("sort_id", "jump_type", "content", "enable", "on_touched_event"),
}
STORY_ID_FIELDS = ("story_id", "dialogue_id", "jump_story", "jump_dialogue",
                   "jump_target")
# 值为这些时算「未设置」，两边都未设置就是一致（导出会省掉空字段）
STORY_EMPTY = (None, "", 0, "0", [], {}, False)


def _story_id_maps(doc):
    """id → 名字解析表：剧情 `ugc_id→名`，对话 `ugc_id→"剧情名/对话序"`。"""
    s_map, d_map = {}, {}
    for s in (doc.get("stories") or []):
        if s.get("ugc_id") is not None:
            s_map[str(s.get("ugc_id"))] = s.get("name") or u"<无名剧情>"
        for d in (s.get("dialogues") or []):
            if d.get("ugc_id") is not None:
                d_map[str(d.get("ugc_id"))] = u"%s/%s" % (
                    s.get("name") or u"<无名剧情>", d.get("sort_id"))
    return s_map, d_map


def _story_resolved_id(node, field, s_map, d_map, unknown, where):
    raw = node.get(field)
    if isinstance(raw, (list, dict)):
        return raw                      # 形态不对：原样带出，交给差异去显示
    if raw in STORY_EMPTY:
        return None                     # 未设置
    key = str(raw)
    if field in ("jump_dialogue", "dialogue_id"):
        got = d_map.get(key)
    else:                               # jump_story / jump_target / story_id
        got = s_map.get(key)
    if got is None:
        unknown.append("%s.%s" % (where, field))
        return u"<id:%s 解析不到>" % key
    return got


def _story_node_scalars(node, level, s_map, d_map, unknown, where, skip_levels=(),
                        skip_fields=()):
    """一个剧情/对话/选项节点的可比字段。

    `skip_levels` / `skip_fields` 是"这一层/这一列**没比成**"的出口：一边整层没采到、
    或某个字段只在一边出现过（老导出压根没这列），取值去比就是成片 `None vs 1` 的假差异。
    """
    if level in skip_levels:
        return ()
    out = []
    for f in STORY_COMPARE_FIELDS[level]:
        if f in skip_fields:
            continue
        v = node.get(f)
        out.append((f, tuple(v) if isinstance(v, list) else v))
    for f in STORY_ID_FIELDS:
        if f in node:
            out.append((f, _story_resolved_id(node, f, s_map, d_map, unknown, where)))
    return tuple(sorted(out))


def _norm_story(snap, unknown=None, skip_levels=(), skip_fields=None):
    """剧情 → `{剧情名: 全部可比字段}`（旧代码只比 sort_id/正文/跳转，等于没比玩法）。

    为什么现在补：交付快照里每条剧情带着 `hide_player_units`（观影时是否藏人）、
    `effect_type/effect_count`（生效类型/可触发次数）、`only_trigger_self`、
    `start_play_event`（触发播放的事件名）等 13 个字段，对话还带 `talker_type`/
    `perform_type`/`show_quit` 等 14 个 —— 旧判据一个都不看，却印「剧情/对话/选项
    逐字一致」。这张图的剧情正是"开场观影"那段：藏人与否、谁能触发都变了，玩家
    一眼就看出来，而验收会说一致。

    `unknown` 传一个列表进来，就顺带收集「解析不到的 id 引用」——那些比不成，
    该报"没比成"而不是"一致"。
    """
    doc = snap.get("story") or {}
    if not isinstance(doc, dict):
        return {u"<story 不是文档 dict>": doc}
    if unknown is None:
        unknown = []
    skip_fields = skip_fields or {}
    s_map, d_map = _story_id_maps(doc)
    out = {}
    for s in (doc.get("stories") or []):
        name = s.get("name") or u"<无名剧情 %s>" % s.get("ugc_id")
        where = u"剧情《%s》" % name
        story_row = {"fields": _story_node_scalars(s, "story", s_map, d_map,
                                                   unknown, where, skip_levels,
                                                   skip_fields.get("story", ()))}
        dialogues = []
        for d in sorted(s.get("dialogues") or [],
                        key=lambda x: (x.get("sort_id") is None, x.get("sort_id"))):
            dwhere = u"%s 对话%s" % (where, d.get("sort_id"))
            drow = {"fields": _story_node_scalars(d, "dialogue", s_map, d_map,
                                                  unknown, dwhere, skip_levels,
                                                  skip_fields.get("dialogue", ()))}
            drow["choices"] = tuple(
                _story_node_scalars(c, "choice", s_map, d_map, unknown,
                                    u"%s 选项%s" % (dwhere, c.get("sort_id")),
                                    skip_levels, skip_fields.get("choice", ()))
                for c in sorted(d.get("choices") or [],
                                key=lambda x: (x.get("sort_id") is None,
                                               x.get("sort_id"))))
            dialogues.append(drow)
        story_row["dialogues"] = tuple(dialogues)
        out[name] = story_row
    # 孤儿节点也要进比对：还原时它们没有父可挂，但确实是要重建的内容。
    # 没进比对的话，"剧情层一致"会在孤儿批次整个丢掉的情况下照样绿灯。
    # 只在**有**孤儿节点时占一个键：两边都没有时不留键，免得把"空 vs 空"当成一致、
    # 又让"整批丢了"报不进 added/removed。
    orphans_d = tuple(
        _story_node_scalars(d, "dialogue", s_map, d_map, unknown, u"孤儿对话",
                            skip_levels, skip_fields.get("dialogue", ()))
        for d in (doc.get("orphan_dialogues") or []))
    orphans_c = tuple(
        _story_node_scalars(c, "choice", s_map, d_map, unknown, u"孤儿选项",
                            skip_levels, skip_fields.get("choice", ()))
        for c in (doc.get("orphan_choices") or []))
    if orphans_d:
        out[u"@孤儿对话"] = orphans_d
    if orphans_c:
        out[u"@孤儿选项"] = orphans_c
    return out


def _story_unresolved(snap):
    """解析不到的 id 引用（可能指向文件外的节点）⇒ 这一层**没比成**，不算一致。"""
    unknown = []
    _norm_story(snap, unknown)
    return unknown


def _story_diff_paths(sa, sb, limit=6):
    """把"哪个剧情/对话的哪个字段不同"点名到字段级。"""
    out = []
    for name in sorted(set(sa) & set(sb)):
        a, b = sa[name], sb[name]
        if isinstance(a, dict) and isinstance(b, dict):
            for fa, fb in zip([f for f, _ in a["fields"]], [v for _, v in a["fields"]]):
                bmap = dict(b["fields"])
                if bmap.get(fa) != fb:
                    out.append(u"%s·%s %r→%r" % (name, fa, fb, bmap.get(fa)))
            for i, (da, db) in enumerate(zip(a["dialogues"], b["dialogues"])):
                if da["fields"] != db["fields"]:
                    for f, va in da["fields"]:
                        vb = dict(db["fields"]).get(f)
                        if va != vb:
                            out.append(u"%s 对话%d·%s %r→%r" % (name, i, f, va, vb))
                if da["choices"] != db["choices"]:
                    out.append(u"%s 对话%d 的选项组不同" % (name, i))
        elif a != b:
            out.append(u"%s 整层不同" % name)
        if len(out) >= limit:
            break
    return out[:limit]


def _floor_cell(x, tol):
    try:
        return int(math.floor(float(x) / tol))
    except (TypeError, ValueError):
        return None


def _sentinel(p):
    """坐标是不是逻辑件的哨兵位（`get_position3()` 对这类件返回 [0,-65536,0]）。"""
    try:
        return bool(p) and len(p) > 1 and float(p[1]) <= SENTINEL_Y
    except (TypeError, ValueError):
        return False


def real_pos(comp):
    """这件的**可信坐标**：有 `pp` 用 `pp`（单元上的真值），否则用 `p`；哨兵位返回 None。

    为什么要单独一个函数（2026-10-01 实机，春岚→新图）：`get_position3()` 对逻辑件返回
    哨兵 `[0,-65536,0]`，真值只在 `pp` 里。旧口径配对只看 `p` ⇒ 源图那件交互区落在"哨兵格"、
    目标图同一件落在真坐标格 ⇒ 被报成一加一删（其实完全同一件）；更要紧的是**反向也会失灵**：
    一件几何被引擎摆到别处时，"同内容件按 2D 距离互吸"会把它配到别的件上，差异被洗掉。
    """
    p = comp.get("p") or []
    if len(p) >= 3 and not _sentinel(p):
        return [float(v) for v in p[:3]]      # p 可信就用 p（两边口径一致）
    pp = comp.get("pp") or []
    if len(pp) >= 3 and not _sentinel(pp):
        return [float(v) for v in pp[:3]]     # p 是哨兵 ⇒ 真值只在 pp
    return None


def _y_gap(x, y):
    """同格子候选的次级键：真实 y 差得远的排后面（0=对得上，1=对不上）。

    没有可信坐标（哨兵且无 pp）时一律 0 —— 不许因为"读不到"就把某件排到别人后面，
    那等于让判据偏好某一种配对。
    """
    rx, ry = real_pos(x), real_pos(y)
    if not rx or not ry:
        return 0
    try:
        return 0 if abs(float(rx[1]) - float(ry[1])) <= 0.5 else 1
    except (TypeError, ValueError):
        return 0


def _cell(comp, tol, use_real=False):
    """网格单元 `(item_id, x格, z格)`。`use_real` 时按**可信坐标**（`pp` 兜底）算。"""
    rp = real_pos(comp) if use_real else None
    p = rp if rp else (comp.get("p") or [None, None, None])
    return (comp.get("i"), _floor_cell(p[0], tol), _floor_cell(p[2], tol))


def _dist2d(x, y, use_real=False):
    """(x, z) 平面上的切比雪夫距离（取两轴最大差）。

    用 max 而不是欧氏：容差语义是「每根轴都别超出 tol」，对角线上
    dx=dz=0.4 的欧氏距离是 0.566，会被 0.5 的容差误判成两件。
    """
    px = (real_pos(x) or x.get("p")) if use_real else x.get("p")
    py = (real_pos(y) or y.get("p")) if use_real else y.get("p")
    px = px or [None, None, None]
    py = py or [None, None, None]
    try:
        return max(abs(float(px[0]) - float(py[0])),
                   abs(float(px[2]) - float(py[2])))
    except (TypeError, ValueError):
        return 0.0


def _content_sig(comp):
    """内容签名：归一后的属性袋。哨兵位逻辑件（运动器 333 个全都停在 (0,-65536,0)）
    靠坐标配对等于**任意配对** —— 真实截断会埋在噪声里，顺序一变就多出几处假差异。
    """
    try:
        return json.dumps(sorted(_events_norm(_info_of(comp)).items(),
                                 key=lambda kv: str(kv[0])),
                         ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return ""


def _pair_once(la, lb, tol, use_real=False):
    """按 (item_id, x, z) 最近邻配对，返回 (pairs, 未配对的 a)。

    ⛔ 不能只按量化格子硬配对：格边界两侧的微小抖动（如 10.0 与 10.4，
    容差 0.5）会落进不同格，进而被误报成「删一件 + 新增一件」。所以查
    3×3 邻域再取最近者，与 `cli/event_import.py` 的容差匹配口径一致。

    同距离时**优先配内容相同的**：逻辑件的 p 恒为哨兵，距离全为 0，
    纯按遍历顺序配会让"只是顺序变了"报成一堆属性变更（2026-10-01 实测：
    春岚删完残骸后 diff 平白多出 4 处属性变更，其中 3 处是运动器）。
    """
    grid = {}
    for c in la:
        grid.setdefault(_cell(c, tol, use_real), []).append(c)
    # 签名按对象预计算：哨兵位那一格里几百件互相扫，不能在循环里反复 json.dumps
    sig_a = {id(c): _content_sig(c) for c in la}
    used = set()
    pairs = []
    for y in lb:
        i = y.get("i")
        cx, cz = _cell(y, tol, use_real)[1], _cell(y, tol, use_real)[2]
        if cx is None or cz is None:
            cells = [(i, None, None)]
        else:
            cells = [(i, cx + dx, cz + dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1)]
        best, bestkey = None, None
        sig = _content_sig(y)
        for cell in cells:
            for x in grid.get(cell) or []:
                if id(x) in used:
                    continue
                # 次级键：同一格子里同袋的件（春岚那两块 98×0.05×62 的薄平台，
                # x/z 相同只差 y）按真实 y 对齐；否则交叉配对会把"其实都对"报成
                # 两处落点差异（判据自己造出来的假阳性，会把真问题盖掉）。
                key = (0 if sig_a[id(x)] == sig else 1, _dist2d(x, y, use_real),
                       _y_gap(x, y))
                if best is None or key < bestkey:
                    best, bestkey = x, key
        if best is not None and (bestkey is None or bestkey[1] <= tol):
            used.add(id(best))
            pairs.append((best, y))
        else:
            pairs.append((None, y))
    unpaired = [x for x in la if id(x) not in used]
    return pairs, unpaired


def _pair_components(la, lb, tol):
    """两遍配对：先按老口径（`p` 的格子），剩下没配上的再按**可信坐标**配一遍。

    为什么要两遍：逻辑件的 `p` 是哨位、真值只在 `pp`（实机：交互区 203022 源图
    `p=(0,-65536,0)` / 目标图 `p=(928,-67.9,-147)`）⇒ 一遍配不上就被报成"一加一删"。
    但第二遍只能补漏，不能当第一遍：单边 `pp`（字段是 2026-09-30 才加的）或两边都哨兵时，
    按真值配会把"同一件挪动了"洗成"两件不同的东西"——那时必须留在老口径里报成变换差异。
    """
    pairs, rest_a = _pair_once(la, lb, tol)
    rest_b = [y for x, y in pairs if x is None]
    if rest_a and rest_b:
        p2, _rest = _pair_once(rest_a, rest_b, tol, use_real=True)
        fix = dict((id(y), x) for x, y in p2 if x is not None)
        pairs = [(fix.get(id(y)) if x is None else x, y) for x, y in pairs]
        matched = set(id(x) for x, y in pairs if x is not None)
        rest_a = [c for c in la if id(c) not in matched]
    return pairs, rest_a


def _pp_moved(x, y):
    """真实坐标（`pp`）是否变了。

    ⛔ 只有一方有 `pp` 时不算差异：那字段是 2026-09-30 才加的，老快照没有它，
    把「没有 vs 有」报成挪动会让旧文件与新文件的 diff 全是噪声。
    """
    a, b = x.get("pp"), y.get("pp")
    return a is not None and b is not None and a != b


def _norm_map_setting(snap):
    """map_setting 的**玩法键**（纯数字，含框架 14）归一为 `{key: str(值)}`。

    只比这些：`name/desc/tags/sub_tags/topics` 是地图身份，跨账号本就该不同 ——
    报成差异只会教人忽略整份报告。
    """
    ms = (snap.get("map") or {}).get("map_setting") or {}
    return {str(k): str(v) for k, v in ms.items() if str(k).isdigit()}


def _map_identity(snap):
    """map_setting 的**身份键**（非数字键：name/desc/tags/sub_tags/topics）。

    跨账号复刻刻意不比（见 _norm_map_setting）；但「改图前存档，出事后定位」是
    **同图回归** —— 改名/改标签也是改动，`diff --include-identity` 时参与比对。
    """
    ms = (snap.get("map") or {}).get("map_setting") or {}
    return {str(k): v for k, v in ms.items() if not str(k).isdigit()}


def _map_props_layer(snap):
    """地图属性层（`map.props`）；**没采到过就返回 None**，别返回空 dict 冒充"什么都没设"。

    老导出的 `map` 里根本没有 `props` 这个键 ⇒ None；新导出即使图上一切都是默认值，
    也会带 `{key: {"present":..,"value":..}}` ⇒ 能比。这一区分是判据的地基：
    "这一层没采" 与 "这一层两边都是空的" 在验收里待遇完全不同。
    """
    p = (snap.get("map") or {}).get("props")
    if not isinstance(p, dict):
        return None
    out = {}
    for k, v in p.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and "present" in v:
            # 「键在但值是 null」与「键不在」是同一件事（这张图什么都没设）——
            # 分成两种会让两张干净的图比出"少了一项"的假差异。
            if v.get("value") is not None:
                out[str(k)] = v.get("value")
        else:
            if v is not None:
                out[str(k)] = v          # 直接存值的旧写法
    return out


def diff_map_props(a, b):
    """地图属性层逐项比（纯逻辑，可离线测）。一边没采到 ⇒ skipped，不算一致。"""
    pa, pb = _map_props_layer(a), _map_props_layer(b)
    skipped = pa is None or pb is None
    if skipped:
        return {"added": [], "removed": [], "changed": [], "skipped": True,
                "skipped_side": "from" if pa is None else "to", "compared": 0}
    added = [{"key": k, "value": pb[k]} for k in sorted(set(pb) - set(pa))]
    removed = [{"key": k, "value": pa[k]} for k in sorted(set(pa) - set(pb))]
    changed = [{"key": k, "from": pa[k], "to": pb[k]}
               for k in sorted(set(pa) & set(pb)) if pa[k] != pb[k]]
    return {"added": added, "removed": removed, "changed": changed,
            "skipped": False, "compared": len(pa)}


def _is_sentinel_pos(c):
    """位置是编辑器隐藏点 `[0, -65536, 0]`（没有 `pp` 时才算）。"""
    p = c.get("p") or []
    return len(p) >= 3 and abs(p[1] + 65536.0) < 1e-6 and not c.get("pp")


def _dup_universe(components):
    """重复判定只覆盖**坐标分不开、且属性袋有内容**的件（逻辑件，如运动器）。

    几何方块共用同一个属性袋是常态（春岚 14318 件方块同一个袋），它们靠坐标区分；
    把那种也算"重复"会让整份报告变成噪声，反而把真信号（多出来的运动器）埋掉。
    ⚠️ **空袋**的也要排除：春岚里 52 件逻辑件的属性袋是空的（剧情/控件这类），
    它们"逐字段相同"只是因为什么都没记 —— 报成重复组是伪信号。
    """
    out = []
    for c in (components or []):
        if c.get("del") or not _is_sentinel_pos(c):
            continue
        if not _events_norm(_info_of(c)):
            continue
        out.append(c)
    return out


def content_sig_counts(components):
    """`{内容签名: 件数}`，口径与 `_dup_universe` 一致。

    ⚠️ 对比"重复"必须用**同一口径的全量计数**：只统计成组的会让孤身一件被算成 0 件，
    于是 B 里的双胞胎被印成"前图 0 件"——把"多了一件"说成"多了一件全新的"。
    """
    out = {}
    for c in _dup_universe(components):
        out[_content_sig(c)] = out.get(_content_sig(c), 0) + 1
    return out


def duplicate_groups(components):
    """内容签名完全相同的组件分组（只留 2 件以上的）。

    为什么单独看这个：2026-10-01 实机，试点的"删了重建"在图里留下一件与 14841
    **逐字段相同**的多余运动器（15003）。它不会让件数变少、也不表现为"新增"
    （配对时被同内容的件吸走），最后混在几条属性变更里 —— 而快照交付要求 1:1，
    多一件少一件都得点名。口径见 `_dup_universe`：只查坐标分不开的逻辑件。
    """
    by_sig = {}
    for c in _dup_universe(components):
        by_sig.setdefault(_content_sig(c), []).append(c.get("g"))
    return [{"sig": s, "gids": sorted(v, key=str), "count": len(v)}
            for s, v in by_sig.items() if len(v) > 1 and v[0] is not None]


def diff_snapshots(a, b, tol=0.5, include_identity=False):
    """比对两份快照，返回结构化结果（纯逻辑，可离线测）。

    只报**稳定差异**：锚点用 (item_id, x, z) 最近邻匹配（受 tol 约束），
    属性按 property_id 归一，事件引用用锚点替换 ugc_id，剧情按名字+内容比对，
    地图玩法设置按**数字键**逐项比（一方没采到就标 skipped，不装作一致）。
    `include_identity` 打开后连地图身份键（名/简介/标签）一起比（默认不比，
    跨账号复刻时两边本就该不同；同图回归才需要它）。

    ⚠️ 删除残骸（`del:1`）**不算内容差异**：删除后组件仍留在 `_ugc_id2component`
    （原生 Ctrl+Z 依赖），重进才消失。当"新增"报就是假阳性 —— 2026-10-01 实测：
    试点删完 11 件后导出，diff 报「新增 11」，而 `component_nums` 两件没变。

    ⚠️ 落盘 ugc_id **只在本会话有效**（重进地图就重编号），所以属性袋里那些存 id 的
    字段（见 `ID_REF_PROPERTIES`）跨会话比一定对不上。这里把它们按配对换号后**再比一次**：
    换号后逐字段相等的记进 `renumbered`（不计入 `changed`、不影响 `identical`），
    换号后仍不等的才是真差异。事件层早就用锚点做同一件事，组件层以前漏了 ——
    报出来的「属性变更 4」里有 3 条是重编号，看图的人无从分辨。
    """
    res_a = [x for x in (a.get("components") or []) if x.get("del")]
    res_b = [x for x in (b.get("components") or []) if x.get("del")]
    ca = [x for x in (a.get("components") or []) if not x.get("del")]
    cb = [x for x in (b.get("components") or []) if not x.get("del")]
    pairs, unpaired_a = _pair_components(ca, cb, tol)
    lut = _id_lut(pairs)

    added, removed, changed, renumbered = [], [], [], []
    for x in unpaired_a:
        removed.append(x)
    for x, y in pairs:
        if x is None:
            added.append(y)
            continue
        ea, eb = _events_norm(_info_of(x)), _events_norm(_info_of(y))
        na = _norm_ids(ea, lut)
        bag_keys = []
        if ea != eb:
            bag_keys = sorted(set(ea) ^ set(eb))
            bag_keys += sorted(k for k in set(ea) & set(eb) if ea[k] != eb[k])
        bag_renum = bool(bag_keys) and bool(lut) and na == eb
        tf, tkinds = _transform_diff(x, y, tol)
        if not bag_keys and not tf:
            continue
        kinds = {}
        for k in bag_keys:
            if bag_renum:
                kinds[k] = "renumber"
            elif k not in na or k not in eb:
                kinds[k] = "value"
            else:
                kinds[k] = _diff_kind(na[k], eb[k], tol)
        kinds.update(tkinds)
        row = {"anchor": _fmt_anchor(y), "ugc_id": [x.get("g"), y.get("g")],
               "properties": sorted(bag_keys) + (["<transform>"] if tf else []),
               "kinds": kinds}
        if tf:
            row["transform_fields"] = tf
        # pp 也要比：逻辑件的 p 恒为哨兵，挪动只会体现在 pp 上，不比就是漏报
        if bag_renum and not tf:
            # 换号不算差异：落盘 ugc_id 只在本会话有效，重进/换账号都会重编。
            renumbered.append(row)
        else:
            changed.append(row)

    ea, eb = _norm_event_table(a, tol), _norm_event_table(b, tol)
    ev_added = sorted(set(eb) - set(ea))
    ev_removed = sorted(set(ea) - set(eb))
    ev_diff_names = sorted(k for k in set(ea) & set(eb) if ea[k] != eb[k])
    # 只差面板顺序的另记一档：不是玩法不同，但也不能说"逐条一致"
    ev_soft = sorted(k for k in ev_diff_names if _event_only_sort_moved(ea, eb, k))
    ev_changed = sorted(set(ev_diff_names) - set(ev_soft))
    ev_untrusted = _event_untrusted_refs(a) + _event_untrusted_refs(b)
    ev_cap_a, ev_cap_b = _event_capture(a), _event_capture(b)
    # 老导出没采那一维 ⇒ 说"没比成"，不能因为"两边都比上了"就印一致
    # （一边是 None、一边也没这维，比出来当然相等，可它根本没被比）
    ev_missing_dims = [n for n, ok in (("in_save", ev_cap_a["has_in_save"] and ev_cap_b["has_in_save"]),
                                       ("key_ok", ev_cap_a["has_key_ok"] and ev_cap_b["has_key_ok"]))
                       if not ok and (ev_cap_a["events"] or ev_cap_b["events"])]

    cap_a = _story_field_capture(a.get("story") or {})
    cap_b = _story_field_capture(b.get("story") or {})
    # 整层没采到 / 字段只在一边出现过（导出版本差）⇒ **不比这些**，并报"没比成"。
    # 反过来把它们当 None 去比，就会凭空报出一片"字段不同"，把真差异埋掉。
    st_skip = sorted(set(cap_a["uncaptured_levels"]) | set(cap_b["uncaptured_levels"]))
    st_gap = _story_field_gap(cap_a, cap_b)
    st_unknown = []
    sa = _norm_story(a, st_unknown, st_skip, st_gap)
    sb = _norm_story(b, st_unknown, st_skip, st_gap)
    st_added = sorted(set(sb) - set(sa))
    st_removed = sorted(set(sa) - set(sb))
    st_changed = sorted(k for k in set(sa) & set(sb) if sa[k] != sb[k])
    st_paths = _story_diff_paths(sa, sb)
    st_uncaptured = _flatten_gaps(st_gap, st_skip)

    # 地图级设置：几何之外的一层。少了它，"两份快照一致"会在玩法不同的图上也报一致。
    ma, mb = _norm_map_setting(a), _norm_map_setting(b)
    ms_skipped = not ma or not mb          # 旧导出没采到 ⇒ 未比对，不算通过也不算差异
    if ms_skipped:
        ms_added = ms_removed = ms_changed = []
    else:
        ms_added = [{"key": k, "value": mb[k]} for k in sorted(set(mb) - set(ma), key=int)]
        ms_removed = [{"key": k, "value": ma[k]} for k in sorted(set(ma) - set(mb), key=int)]
        ms_changed = [{"key": k, "from": ma[k], "to": mb[k]}
                      for k in sorted(set(ma) & set(mb), key=int) if ma[k] != mb[k]]

    # 地图属性层（默认背景音乐等）：组件之外、玩法开关之外的一层
    mp_diff = diff_map_props(a, b)

    # 地图身份键：默认不比（跨账号两边本就该不同），--include-identity 时参与
    # identical 判定。map_setting 本身有一方没采到 ⇒ 身份键同样无从比起。
    ia, ib = _map_identity(a), _map_identity(b)
    mi_skipped = ms_skipped or not include_identity
    if mi_skipped:
        mi_added = mi_removed = mi_changed = []
    else:
        mi_added = [{"key": k, "value": ib[k]} for k in sorted(set(ib) - set(ia))]
        mi_removed = [{"key": k, "value": ia[k]} for k in sorted(set(ia) - set(ib))]
        mi_changed = [{"key": k, "from": ia[k], "to": ib[k]}
                      for k in sorted(set(ia) & set(ib)) if ia[k] != ib[k]]

    # 同内容重复件：按**内容签名**比件数（ugc_id 每次重载都变，不能当身份）
    dup_a = content_sig_counts(ca)
    dup_b = duplicate_groups(cb)
    dup_extra = [{"gids": g["gids"], "count": g["count"],
                  "before_count": dup_a.get(g["sig"], 0)}
                 for g in dup_b if g["count"] > dup_a.get(g["sig"], 0)]

    return {
        "ok": True,
        "tolerance": tol,
        "from": {"captured_at": a.get("captured_at"), "map": (a.get("map") or {}).get("name"),
                 "components": len(ca)},
        "to": {"captured_at": b.get("captured_at"), "map": (b.get("map") or {}).get("name"),
               "components": len(cb)},
        "components": {"added": added, "removed": removed, "changed": changed,
                       "renumbered": renumbered,
                       "added_sentinel": [x for x in added if _is_sentinel_pos(x)],
                       "residue": {"from": len(res_a), "to": len(res_b)},
                       "duplicate_extra": dup_extra},
        "events": {"added": ev_added, "removed": ev_removed, "changed": ev_changed,
                   "sort_only": ev_soft, "untrusted": ev_untrusted,
                   "missing_dims": ev_missing_dims,
                   "unknown": bool(ev_untrusted) or bool(ev_missing_dims)},
        "story": {"added": st_added, "removed": st_removed, "changed": st_changed,
                  "changed_paths": st_paths, "unresolved": st_unknown,
                  "uncaptured_levels": st_uncaptured,
                  "unknown": bool(st_unknown) or bool(st_uncaptured),
                  "compared_fields": sum(len(v) for v in STORY_COMPARE_FIELDS.values())},
        "map_setting": {"added": ms_added, "removed": ms_removed,
                        "changed": ms_changed, "skipped": ms_skipped,
                        "compared": 0 if ms_skipped else len(ma)},
        "map_props": mp_diff,
        "map_identity": {"added": mi_added, "removed": mi_removed,
                         "changed": mi_changed, "skipped": mi_skipped,
                         "compared": 0 if mi_skipped else len(ia)},
        "identical": not (added or removed or changed or ev_added or ev_removed
                          or ev_changed or ev_soft or st_added or st_removed or st_changed
                          or ms_added or ms_removed or ms_changed or dup_extra
                          or mi_added or mi_removed or mi_changed
                          or mp_diff["added"] or mp_diff["removed"] or mp_diff["changed"]),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def summarize(snap):
    mp = snap.get("map") or {}
    bg = snap.get("budget") or {}
    st = snap.get("story") or {}
    ev = snap.get("events") or {}
    # 空桶不算待删除（旧导出可能带着摆放操作留下的 {'unit': []}）——不滤的话，
    # 摆过一件之后每次导出都喊一遍「不可跨会话比较」，把人喊麻，真告警反而被淹没。
    delbag = {k: v for k, v in (bg.get("del_unit_uids") or {}).items() if v}
    warn = []
    residue = sum(1 for c in (snap.get("components") or []) if c.get("del"))
    if residue:
        warn.append("⚠️ 快照里有 %d 件是**已删除、待保存提交**的组件（`del:1`）："
                    "它们不是地图内容，还原侧按 deleted_residue 跳过，重进后消失；"
                    "但**退出编辑器也会自动保存**，所以这批删除会被提交。身份：%s"
                    % (residue, format_residue_identities(
                        residue_identities(snap), residue)))
    if delbag:
        warn.append("⚠️ 导出时存在待删除标记 %s：占用值含本地待删除态，不可跨会话比较。"
                    "身份：%s —— 这是**待保存提交的删除**，退出编辑器也会自动保存，"
                    "届时这些件就真没了" % (
                        {k: len(v) for k, v in delbag.items()},
                        format_pending_deletions(bg.get("pending_deletions") or [],
                                                 bg.get("pending_deletions_total"))))
    return {
        "format": snap.get("format"),
        "version": snap.get("version"),
        "captured_at": snap.get("captured_at"),
        "map": mp.get("name"),
        "components": len(snap.get("components") or []),
        "type_distribution": mp.get("type_distribution"),
        "events": ev.get("count", len(ev.get("events") or [])),
        "stories": len(st.get("stories") or []),
        "dialogues": sum(len(s.get("dialogues", [])) for s in st.get("stories") or []),
        "choices": sum(len(d.get("choices", []))
                       for s in st.get("stories") or [] for d in s.get("dialogues") or []),
        "budget": {k: bg.get(k) for k in ("occupancy", "max_occupancy",
                                          "component_nums", "max_component_nums",
                                          "registered")},
        "warnings": warn,
    }


def print_diff(d, show=20):
    def _row(tag, n):
        print("  %-10s %d" % (tag, n))

    print("快照比对：%s (%s)  →  %s (%s)"
          % (d["from"]["map"] or "?", d["from"]["captured_at"],
             d["to"]["map"] or "?", d["to"]["captured_at"]))
    print("容差 %s 坐标单位\n" % d["tolerance"])
    c, e, s = d["components"], d["events"], d["story"]
    print("组件")
    _row("新增", len(c["added"]))
    _row("删除", len(c["removed"]))
    _row("属性变更", len(c["changed"]))
    renum = c.get("renumbered") or []
    _row("仅 id 重编号", len(renum))
    res = (c.get("residue") or {})
    if res.get("from") or res.get("to"):
        print("    ℹ️ 删除残骸（`del` 标记）不计入差异：前 %s 件 / 后 %s 件"
              "—— 重进地图后消失" % (res.get("from", 0), res.get("to", 0)))
    sent = c.get("added_sentinel") or []
    if sent:
        print("    ⚠️ 新增里有 %d 件在**哨兵位** [0,-65536,0]：像删除残骸/未确认态"
              "（没打 del 标记，所以仍按差异计），判落库要重进后回对" % len(sent))
    dupx = c.get("duplicate_extra") or []
    for g in dupx[:show]:
        ids = [str(x) for x in g["gids"]]
        print("    ⚠️ 同内容重复：后图 %d 件（前图 %d 件）ugc_id %s"
              " —— 逐字段相同，配对时会被同内容的件吸走，所以常常不表现为「新增」"
              % (g["count"], g["before_count"],
                 ",".join(ids[:6]) + ("…(%d 件)" % len(ids) if len(ids) > 6 else "")))
    for x in c["added"][:show]:
        print("    + %s" % _fmt_comp(x))
    for x in c["removed"][:show]:
        print("    - %s" % _fmt_comp(x))
    for x in c["changed"][:show]:
        kinds = x.get("kinds") or {}
        tag = ""
        if kinds and not _is_hard_change(x):
            tag = "  [%s]" % ",".join(sorted({kinds[k] for k in kinds}))
        tf = x.get("transform_fields") or []
        if tf:
            tag += "  变换差在%s" % "、".join(tf)
        print("    ~ %s  属性 %s%s" % (x["anchor"], ",".join(x["properties"][:6]), tag))
    for x in renum[:show]:
        print("    ⇒ %s  属性 %s 只是落盘 id 换了号（%s→%s），换号后逐字段相等"
              % (x["anchor"], ",".join(x["properties"][:6]),
                 x["ugc_id"][0], x["ugc_id"][1]))
    print("\n事件")
    _row("新增", len(e["added"]))
    _row("删除", len(e["removed"]))
    _row("绑定变更", len(e["changed"]))
    _row("仅面板顺序", len(e.get("sort_only") or []))
    if e.get("untrusted"):
        print("    ❓ %d 处引用 key_ok=false（%s）⇒ 这一层没比成"
              % (len(e["untrusted"]), e["untrusted"][:3]))
    for k in e["added"][:show]:
        print("    + %s" % k)
    for k in e["removed"][:show]:
        print("    - %s" % k)
    for k in e["changed"][:show]:
        print("    ~ %s" % k)
    print("\n剧情")
    _row("新增", len(s["added"]))
    _row("删除", len(s["removed"]))
    _row("内容变更", len(s["changed"]))
    for k in s["added"][:show]:
        print("    + %s" % k)
    for k in s["removed"][:show]:
        print("    - %s" % k)
    for p in (s.get("changed_paths") or [])[:show]:
        print("    ~ %s" % p)
    if s.get("unresolved"):
        print("    ❓ %d 处剧情引用解析不到 id（%s）⇒ 这一层没比成，别当一致"
              % (len(s["unresolved"]), s["unresolved"][:3]))
    m = d.get("map_setting") or {}
    print("\n地图设置")
    if m.get("skipped"):
        print("    ⚠️ 有一方没采到 map_setting（旧导出？）⇒ 这一层**未比对**，"
              "别把下面的\"一致\"当成玩法也对了")
    else:
        _row("新增键", len(m["added"]))
        _row("删除键", len(m["removed"]))
        _row("值变更", len(m["changed"]))
        for r in m["added"][:show]:
            print("    + key %s=%s" % (r["key"], r["value"]))
        for r in m["removed"][:show]:
            print("    - key %s=%s" % (r["key"], r["value"]))
        for r in m["changed"][:show]:
            print("    ~ key %s %s → %s" % (r["key"], r["from"], r["to"]))
        if not (m["added"] or m["removed"] or m["changed"]):
            print("    ✓ %d 个数字键一致（含框架 key 14；地图名/简介/标签不计）"
                  % m["compared"])
    mp = d.get("map_props") or {}
    print("\n地图属性（默认背景音乐这一层）")
    if mp.get("skipped"):
        print("    ⚠️ 有一方没采到地图属性层（%s 侧，旧导出？）⇒ 这一层**未比对**："
              "默认背景音乐/排行榜/人数这类地图级设置看不出来"
              % mp.get("skipped_side", "某"))
    else:
        _row("新增项", len(mp.get("added", [])))
        _row("删除项", len(mp.get("removed", [])))
        _row("值变更", len(mp.get("changed", [])))
        for r in (mp.get("added") or [])[:show]:
            print("    + %s=%s" % (r["key"], json.dumps(r["value"], ensure_ascii=False)[:120]))
        for r in (mp.get("removed") or [])[:show]:
            print("    - %s=%s" % (r["key"], json.dumps(r["value"], ensure_ascii=False)[:120]))
        for r in (mp.get("changed") or [])[:show]:
            print("    ~ %s：%s → %s" % (r["key"],
                                         json.dumps(r["from"], ensure_ascii=False)[:110],
                                         json.dumps(r["to"], ensure_ascii=False)[:110]))
        if not (mp.get("added") or mp.get("removed") or mp.get("changed")):
            print("    ✓ %d 项一致（默认背景音乐/排行榜/人数/灵感笔记等）" % mp.get("compared", 0))
    mi = d.get("map_identity")
    if mi and not mi.get("skipped"):
        print("\n地图身份（名/简介/标签——跨账号复刻默认不比，--include-identity 打开）")
        _row("新增键", len(mi["added"]))
        _row("删除键", len(mi["removed"]))
        _row("值变更", len(mi["changed"]))
        for r in mi["changed"][:show]:
            print("    ~ key %s: %s → %s"
                  % (r["key"], json.dumps(r["from"], ensure_ascii=False)[:90],
                     json.dumps(r["to"], ensure_ascii=False)[:90]))
        if not (mi["added"] or mi["removed"] or mi["changed"]):
            print("    ✓ %d 个身份键一致" % mi.get("compared", 0))
    verdict = "两份快照一致" if d["identical"] else "存在差异（见上）"
    if (d.get("map_setting") or {}).get("skipped"):
        verdict += "（地图设置未比对）"
    if mp.get("skipped"):
        verdict += "（地图属性未比对）"
    print("\n结论：%s" % verdict)


def _fmt_anchor(c):
    p = c.get("p") or []
    return "%s@%s,%s" % (c.get("i"),
                         p[0] if len(p) > 0 else "?",
                         p[2] if len(p) > 2 else "?")


def _fmt_comp(c):
    p = c.get("p") or []
    return "item=%s id=%s pos=(%s, %s, %s)" % (
        c.get("i"), c.get("g"),
        p[0] if len(p) > 0 else "?", p[1] if len(p) > 1 else "?",
        p[2] if len(p) > 2 else "?")


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 离线体检：交付前判断"这份快照能不能拿去还原"
# ---------------------------------------------------------------------------
GROUP_REF_FIELDS = ("18701", "03001")     # 属性袋里存 ugc_id 引用的字段尾号
SENTINEL_Y = -65536.0
UI_CUSTOM_TYPE = 200000
UI_NATIVE_TYPE = 200001        # item 6000001「技能控件」这类，还原默认不摆
UI_POS_PROPERTY = "20000010001"


def _iter_numbers(v):
    """把属性袋值里的数字（含嵌套 list、数字字符串）逐个吐出来。"""
    if isinstance(v, (list, tuple)):
        for x in v:
            for y in _iter_numbers(x):
                yield y
    elif isinstance(v, bool):
        return
    elif isinstance(v, int):
        yield v
    elif isinstance(v, str) and v.isdigit():
        yield int(v)


def _display_name_of(info):
    """从属性袋里挑一个"像名字"的字符串值（第一个非纯数字的字符串 info）。

    只用于把残骸/待删除件**认出来**：命名槽位号按类型不同（运动器是 20391102001），
    维护一张槽位表会漂，这里要的是"能不能认出这是哪件"，不是精确字段名。
    """
    for e in (info or {}).get("events") or []:
        v = (e or {}).get("info")
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if isinstance(v, str) and v and not v.replace(".", "").isdigit():
            return v
    return None


def format_pending_deletions(items, total=None):
    """把待删除袋的身份清单压成一行；没采到身份时退回条数。

    ⚠️ 这批删除**会在下一次保存时被提交**（退出编辑器也会自动保存），所以措辞不许写成
    "重进后消失"那种像噪声的说法 —— 那是"你的地图少了一件"的意思。
    """
    total = total if total is not None else len(items or [])
    if not items:
        return "（未能解析出身份，只有桶计数）"
    def _f(x):
        if x.get("gid") is None:
            return "%s桶 id=%s（挂在它上的组件已不在组件表里）" % (x.get("bucket"), x.get("id"))
        return "%s(cid=%s%s)" % (x.get("gid"), x.get("cid"),
                                 "「%s」" % x["name"] if x.get("name") else "")
    listed = "、".join(_f(x) for x in items)
    return ("%s%s" % (listed, "…另有 %d 件未列出" % (total - len(items)))
            if total > len(items) else listed)


def pending_deletions_present(budget):
    """待删除袋里**真的挂着 id**。

    空桶不算：摆放操作会留 {'unit': []} 这种空桶（2026-10-03 实机复现），把它当
    "有待删除" 会让 restore 的两道闸门在干净现场上喊假告警。旧导出文件里已经落了
    空桶的，也靠这个判据挡住 —— 与 CODE_BUDGET 的导出口径、summarize 的过滤同一条。
    """
    return any(v for v in ((budget or {}).get("del_unit_uids") or {}).values())


def residue_identities(snap, limit=4):
    """删除残骸（`del:1`）的**身份清单**：`[{g, cid, name}]`。

    为什么不是只报条数：2026-10-01 那次，导出警告印的是 `{'unit': 7, 'component': 4}`，
    而"退出编辑器会自动保存"意味着这批删除**会被提交**——数出来是几件、认不出是哪几件，
    等发现问题时已经存进去了。计数只能告警，身份才能让人当场判断"这个我该不该删"。
    """
    out = []
    for c in snap.get("components") or []:
        if not c.get("del"):
            continue
        info = _info_of(c)
        out.append({"g": c.get("g"), "cid": info.get("component_id"),
                    "name": _display_name_of(info)})
        if len(out) >= limit:
            break
    return out


def format_residue_identities(items, total):
    """把身份清单压成一行可读文本（含"还有 N 件未列出"）。"""
    def _fmt(x):
        return "%s(cid=%s%s)" % (x["g"], x["cid"],
                                 "「%s」" % x["name"] if x["name"] else "")
    tail = "…另有 %d 件未列出" % (total - len(items)) if total > len(items) else ""
    return "、".join(_fmt(x) for x in items) + tail


def validate_snapshot(snap):
    """纯离线核对快照文件（不连游戏）。返回 `{"items":[{key,level,msg}], "ok":bool}`。

    每条检查都对应这次跨账号交付里真踩到/真证过的失效面 —— 报"没问题"之前得先能查出：
    属性袋被截断、共用单元的身份串味、哨兵位（其实坐标在别处）、控件没画布坐标、
    删除残骸当内容、组合件成员指向文件外、缺框架号导致目标图地基不同。
    """
    out = []

    def add(key, level, msg):
        out.append({"key": key, "level": level, "msg": msg})

    comps = snap.get("components") or []
    if snap.get("format") != FORMAT:
        add("format", "blocker", "format=%r 不是 %r" % (snap.get("format"), FORMAT))
    if not comps:
        add("components", "blocker", "快照里没有组件")

    # ---- 文件自洽性：导出时写下的几个数必须等于正文的行数 ----
    # 为什么值得当阻塞项：一份被截断/手工拼过/导出中途断掉的 JSON，其余每一条检查都还能
    # 全过（身份唯一、袋可解析、引用在文件内……），然后安安静静少摆几百件。
    # 三个数各来自一次独立遍历（counts、map.total_components、type_distribution 求和），
    # 它们与行数不一致就说明文件不是同一次导出的自洽产物。
    cnt = snap.get("counts") or {}
    mp_ = snap.get("map") or {}
    td = mp_.get("type_distribution") or {}
    stated = {"counts.components": cnt.get("components"),
              "map.total_components": mp_.get("total_components"),
              "type_distribution 合计": (sum(td.values()) if td else None)}
    bad = {k: v for k, v in stated.items() if v is not None and v != len(comps)}
    if bad:
        add("self_consistency", "blocker",
            "文件自带的件数与正文行数不一致（正文 %d 行，%s）⇒ 这份快照不是自洽的一次导出，"
            "拿去还原会**安静地少摆**（后面每一条检查都可能全过）。请重导一份"
            % (len(comps), ", ".join("%s=%s" % (k, v) for k, v in sorted(bad.items()))))
    elif all(v is None for v in stated.values()):
        add("self_consistency", "info",
            "文件里没有 counts / total_components / type_distribution 这些自洽锚点（早期导出）"
            "⇒ 截断只能靠人工比行数")
    else:
        add("self_consistency", "ok",
            "正文 %d 行与 counts/total/type 分布三处自洽" % len(comps))

    gids, bad_ci, deep, residue = [], 0, 0, 0
    sentinel_only, no_pp, widget_no_pos, small_scale = 0, 0, 0, 0
    ui_native = 0
    refs_missing, groups_missing = [], []
    ref_candidates = {}
    present = {c.get("g") for c in comps}
    for c in comps:
        gids.append(c.get("g"))
        if c.get("del"):
            residue += 1
        raw = c.get("ci")
        try:
            ci = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            bad_ci += 1
            ci = {}
        if isinstance(raw, str) and "<deep>" in raw:
            deep += 1
        p = c.get("p") or [0, 0, 0]
        has_pp = bool(c.get("pp"))
        is_widget = ci.get("component_id") == UI_CUSTOM_TYPE
        if ci.get("component_id") == UI_NATIVE_TYPE:
            ui_native += 1
        has_canvas = False
        for e in (ci.get("events") or []):
            pid = str(e.get("type")) if isinstance(e, dict) else ""
            if pid == UI_POS_PROPERTY and isinstance(e.get("info"), (list, tuple)) \
                    and len(e.get("info") or []) >= 2:
                has_canvas = True
            if pid.endswith(GROUP_REF_FIELDS):
                for v in _iter_numbers(e.get("info")):
                    if v not in present:
                        refs_missing.append((c.get("g"), pid, v))
            else:
                # 白名单**外**的字段里出现"等于某个组件 gid"的整数 ⇒ 候选引用，报出来给人核
                for v in _iter_numbers(e.get("info")):
                    if v in present and v != c.get("g"):
                        ref_candidates[pid] = ref_candidates.get(pid, 0) + 1
        if isinstance(ci.get("group_components"), list):
            for v in ci["group_components"]:
                if v not in present:
                    groups_missing.append((c.get("g"), v))
        if len(p) > 1 and float(p[1]) <= SENTINEL_Y:
            if is_widget and not has_canvas:
                widget_no_pos += 1
            elif not is_widget and not has_pp:
                sentinel_only += 1
            elif has_pp:
                pass
        elif has_pp:
            pass
        if not has_pp and len(p) > 1 and float(p[1]) <= SENTINEL_Y and not is_widget:
            no_pp += 1
        s = c.get("s") or []
        if s and min(float(x) for x in s) < 0.1:
            small_scale += 1

    dup = len(gids) - len(set(gids))
    add("identity", "blocker" if dup else "ok",
        "ugc_id 重复 %d 条（还原会张冠李戴）" % dup if dup else "ugc_id 唯一")
    add("ci_parse", "blocker" if bad_ci else "ok",
        "%d 件 component_info 解析失败" % bad_ci if bad_ci
        else "component_info %d/%d 全部可解析" % (len(comps), len(comps)))
    add("ci_depth", "blocker" if deep else "ok",
        "%d 件属性袋含 <deep>（回传被截断，值不完整）" % deep if deep
        else "无 <deep> 截断（严格 dumps 生效）")
    residue_idents = residue_identities(snap) if residue else []
    add("residue", "warn" if residue else "ok",
        ("%d 件是删除后仍留在索引里的撤销栈残骸（del:1），还原会跳过；"
         "重进地图后重新导出才干净。⚠️ 这些删除**会在下次保存时被提交**"
         "（退出编辑器也会自动保存），身份：%s"
         % (residue, format_residue_identities(residue_idents, residue)))
        if residue else "无删除残骸行")
    add("sentinel", "info" if sentinel_only else "ok",
        ("%d 件位置是哨兵且没有 pp/画布坐标 ⇒ 还原需要 --allow-sentinel"
         "（运动器这类靠袋内绝对关键帧的不受影响）" % sentinel_only)
        if sentinel_only else "没有无坐标逻辑件")
    if no_pp and not sentinel_only:
        add("pp", "info", "%d 件的真值坐标另记在 pp" % no_pp)
    add("widget_pos", "warn" if widget_no_pos else "ok",
        ("%d 个界面控件（UI_CUSTOM）袋里没有画布 POS ⇒ 会被摆到 (0,0)，"
         "控件的坐标只存在于袋里（全图控件共用一个单元）" % widget_no_pos)
        if widget_no_pos else "控件画布坐标齐备")
    if ui_native:
        # 这一层的性质要写清楚：**通道开放、落库未证**，不是"做不到"。
        # 旧注释写的"界面控件，配置复杂"是工具链的过期结论（2026-10-01 用只读
        # plan_item 复核：server_place_supported / server_begin_allowed 都为 true，
        # 且 200001 早在 VERIFIED_TYPES 里），拿它当"引擎不支持"会少搬 48 件。
        add("ui_native", "info",
            "%d 件 UI_NATIVE（技能控件这类）默认**不摆** ⇒ 目标图会比源图少这些件。"
            "摆放通道已核（plan_item 放行），但落库未证 ⇒ 要 1:1 用 --include-ui 试点"
            "并以重进回读为准。⚠️ 这里是**离线按这份文件自己**算的：每张新图天生自带同样一批"
            "（2026-10-01 实机：新图 48 件与春岚 48 件按属性袋+位置逐件相同）⇒ 目标图是空白图时"
            "这一层不缺，`restore` 的预检会对着目标图现状给净缺口" % ui_native)
    add("group_refs", "warn" if groups_missing else "ok",
        ("%d 处 group_components 指向文件外的件（成组会残缺）：%s"
         % (len(groups_missing), groups_missing[:3]) if groups_missing else "成组互指都在文件内")
        )
    add("member_refs", "warn" if refs_missing else "ok",
        ("%d 处 ugc_id 引用指向文件外的件（还原后是悬空引用）：%s"
         % (len(refs_missing), refs_missing[:3])) if refs_missing
        else "属性袋内的 ugc_id 引用都在文件内")
    if ref_candidates:
        top = sorted(ref_candidates.items(), key=lambda kv: -kv[1])[:5]
        add("ref_candidates", "info",
            "%d 个「成员表白名单之外」的字段里出现过等于某个组件 gid 的整数（前列：%s）。"
            "比对与还原都**不**对这些字段换号 —— 春岚实测它们几乎全是撞号（模型号/枚举值/"
            "路径坐标/阵营序号），跟着换号会把真差异洗白。但换一张图时若某个字段**真是**组件引用，"
            "还原会原样把旧号写下去。分辨办法：拿同一张图**两次不同会话**的导出比一次，"
            "真引用会随重编号而变，撞号不会（这一条判据本轮就是这么把 27 个字段全排除的）"
            % (len(ref_candidates), ", ".join("%s×%d" % (p, n) for p, n in top)))
    if small_scale:
        add("scale_range", "info",
            "%d 件缩放超出编辑器滑块范围（还原侧带 allow_out_of_range_scale，"
            "手工 place_item 会被 plan() 拒）" % small_scale)

    mp = snap.get("map") or {}
    fw = mp.get("framework_id") or (mp.get("map_setting") or {}).get("14")
    add("framework", "warn" if not fw else "ok",
        "缺 framework_id（目标图地基可能对不上，--set-framework 也无法对齐）"
        if not fw else "场景框架 =%s" % fw)
    ms = mp.get("map_setting") or {}
    if not ms:
        add("map_setting", "warn", "map_setting 为空：目标图地图设置不会被动过")
    else:
        # 玩法键（纯数字键，框架键 14 另由 --set-framework 管）决定"长得一样之后玩起来
        # 是不是一样"；name/desc/tags 是地图身份，跨图复刻本就不带走。
        _num = sorted([k for k in ms if str(k).isdigit() and k != "14"], key=int)
        add("map_setting", "ok",
            ("玩法设置键 %d 个（%s…），还原时 --sync-settings 对齐；"
             "地图名/简介/标签不带走" % (len(_num), ",".join(_num[:5])))
            if _num else "只有地图身份键（名/简介/标签），没有可同步的玩法键")
    if not mp.get("anchor_info"):
        add("anchor", "warn", "缺 anchor_info：跨图预检算不出可建区边界")
    # 地图属性层（默认背景音乐等）：采没采决定交付时能不能说"BGM 也一并对过"。
    # 老导出没这层 ⇒ info 说清楚"这一层本文件里没有"，验收时会落成 unknown 而不是绿灯。
    props = mp.get("props")
    if not isinstance(props, dict):
        add("map_props", "warn",
            "这份导出**没有地图属性层**（旧版工具）⇒ 默认背景音乐/排行榜/人数这类地图级"
            "设置没采下来，还原出来的图可能开局静音；要 1:1 请用新版重新导出")
    else:
        _set = sorted(k for k, v in props.items()
                      if not k.startswith("_") and isinstance(v, dict) and v.get("present"))
        _bgm = (props.get("ugc_bgm_conf") or {}).get("value")
        add("map_props", "ok",
            "地图属性层已采：%s；默认背景音乐=%s"
            % (", ".join(_set) if _set else "（各项都未设置）",
               json.dumps(_bgm, ensure_ascii=False) if _bgm is not None else "未设置")
            + ("" if _bgm is None else
               "（还原侧 --set-bgm 走「更多→网易云音乐→背景音乐设置」那条原生调用，"
               "**实机只证到本会话生效、重进后回读仍是 null ⇒ 落库未成立**，"
               "这一项到目标图上要人工核；判据在 SNAPSHOT.md「地图属性」一节）"))

    bg = snap.get("budget") or {}
    cap = bg.get("max_component_nums")
    nums = bg.get("component_nums")
    if not cap:
        add("budget", "warn", "读不到 max_component_nums")
    elif nums is not None and nums > cap:
        # 2026-10-03 实机（快照测试图）：component_nums=20133 > max=15000，引擎自己
        # 两套读数打架。这种图上"件数层一致"的证明力下降，还原侧的预算闸门也会按
        # 这个上限把全量拒写 —— 必须点名，不能当正常印出来。
        add("budget", "warn",
            "源图组件数 %s 超过引擎上报的上限 %s —— 计数器与上限自相矛盾（上限读数或"
            "计数口径有问题）：还原侧的预算复核会按这个上限拒写，跨会话比件数的证明力"
            "也下降。交付前先在实机核对这两个数" % (nums, cap))
    else:
        add("budget", "ok",
            "源图组件数 %s / 上限 %s（B 账号上限若更低，全量会中途卡住）" % (nums, cap))
    ev = snap.get("events") or {}
    add("events", "ok", "事件 %d 条 / 引用 %d 处"
        % (ev.get("count", len(ev.get("events") or [])),
           sum(len(e.get("refs") or []) for e in (ev.get("events") or []))))
    story = snap.get("story")
    if isinstance(story, dict):
        n_st = len(story.get("stories") or [])
        n_dg = sum(len(s.get("dialogues") or []) for s in (story.get("stories") or []))
        add("story", "ok", "剧情 %d / 对话 %d（需 --with-story 重建）" % (n_st, n_dg))
        # 孤儿节点（挂不上父节点的对话/选项）是**导出的、但导入不会重建**的一类：
        # `story_import` 只遍历 `stories`（见 bridge/fuxiang_story.py 的计数与导入路径）。
        # 春岚这份是 0，所以今天不影响；换一张有孤儿的图就会"跑完全程、目标图少几件"，
        # 而且只在重进回对时才看得见 ⇒ 必须在交付前点名。
        od = len(story.get("orphan_dialogues") or [])
        oc = len(story.get("orphan_choices") or [])
        if od or oc:
            add("story_orphans", "warn",
                "剧情里有孤儿节点（对话 %d / 选项 %d：挂不上父节点的那批）——"
                "`--with-story` **不会重建它们**（导入只遍历 stories）⇒ 目标图必然少这些件，"
                "accept 会把它算进剧情差异。要真 1:1：先在源图里把孤儿挂回某条剧情（或删掉），"
                "再重新导出这份交付件" % (od, oc))
        else:
            add("story_orphans", "ok", "没有孤儿对话/选项（导入不会漏建这类节点）")
        # 采集**范围**也要报：对方拿 `accept` 逐字段验收时，只有这份文件采到的列才比得成。
        cap = _story_field_capture(story)
        if cap["uncaptured_levels"]:
            add("story_capture", "warn",
                "剧情节点存在，但 %s 层**一个可比字段都没采到**（这份是早期导出）⇒ 交过去"
                "之后这些层只能报「没比成」，要逐字段验收请重导"
                % "、".join(cap["uncaptured_levels"]))
        else:
            scope = ", ".join(
                "%s %d/%d" % (lv, len(cap["captured"].get(lv) or ()),
                              len(STORY_COMPARE_FIELDS[lv]))
                for lv in ("story", "dialogue", "choice") if cap["captured"].get(lv))
            if scope:
                add("story_capture", "info",
                    "剧情可比字段实际出现 %s（其余是空值，导出省键）⇒ 对方比对只在这些列上成立"
                    % scope)
    elif story:
        add("story", "warn", "story 段结构不是文档 dict，剧情可能不会被重建")

    ecap = _event_capture(snap)
    if ecap["events"] and (not ecap["has_in_save"] or not ecap["has_key_ok"]):
        dims = [n for n, ok in (("in_save（在不在保存集合）", ecap["has_in_save"]),
                                ("key_ok（引用槽位是否还成立）", ecap["has_key_ok"]))
                if not ok]
        add("event_dims", "warn",
            "事件 %d 条，但引用缺 %s 这两维（早期导出）⇒ `accept` 的事件层只能说「没比成」，"
            "看不出事件是否真进了保存载荷；要交付请重导一份" % (ecap["events"], "、".join(dims)))
    elif ecap["events"]:
        add("event_dims", "ok", "事件 %d 条，引用带 in_save / key_ok 两维，可判是否落库"
            % ecap["events"])

    # 采集完整性印章（counts.fetch_anomalies，build_snapshot 落盘）：异常留在 stdout
    # 的话，文件转手之后就没人知道采集时缺没缺层。缺省值（0 条事件 / story=null）
    # 会被 diff 当成"地图事实"去比 —— 必须在体检时点名。
    fa = (snap.get("counts") or {}).get("fetch_anomalies") or []
    _fa_sev = {"aborted_batch": "warn", "fetch_failed": "warn", "count_mismatch": "info"}
    if any(_fa_sev.get(x.get("kind"), "warn") == "warn" for x in fa):
        add("fetch_integrity", "warn",
            "导出时采集层有异常：%s ⇒ 相关层的读数是**缺省值不是地图事实**，"
            "拿去比对/还原会静默少内容，请重导一份"
            % "；".join(x.get("msg", str(x)) for x in fa))
    elif fa:
        add("fetch_integrity", "info",
            "采集备注：%s —— 多半是良性口径差，人工核一眼即可"
            % "；".join(x.get("msg", str(x)) for x in fa))

    blockers = [i for i in out if i["level"] == "blocker"]
    warns = [i for i in out if i["level"] == "warn"]
    return {"ok": not blockers, "items": out,
            "blockers": blockers, "warnings": warns}


def print_validation(v):
    icon = {"ok": "  ✓ ", "info": "  ℹ ", "warn": "  ⚠ ", "blocker": "  ⛔ "}
    for i in v["items"]:
        print("%s%-12s %s" % (icon.get(i["level"], "  - "), i["key"], i["msg"]))
    print("结论：%s" % ("可以交付还原" if v["ok"] else "有阻塞项，先修再交付"))


def acceptance_report(snap_a, snap_b, tol=0.5):
    """一层层回答：B（目标图当前状态）与 A（要交付的快照）**1:1 吗**。

    为什么要有这条命令：交付流程本来是 check → restore 预检 → apply → export → diff →
    verify-only 六个出口，每个各说一段话，判"能不能收"要靠人把六份输出拼起来 ——
    拼的时候最容易犯的错是把"没比对上"当成"一致"。这里把它收成一个结论，并且
    **`unknown` 不许算通过**（这一层没比成，就是不能说 1:1）。

    返回 `{"layers":[{key,level,counts,msg}], "ok":bool, "blocked":bool}`。
    """
    layers = []

    def add(key, level, msg, counts=None):
        layers.append({"key": key, "level": level, "msg": msg,
                       "counts": counts or {}})

    va, vb = validate_snapshot(snap_a), validate_snapshot(snap_b)
    blocked = not va["ok"] or not vb["ok"]
    add("文件", "blocker" if blocked else "ok",
        "两份快照都通过交付前体检" if not blocked else
        "有阻塞项（%s / %s）：先修文件再判 1:1" % (
            [i["key"] for i in va["items"] if i["level"] == "blocker"],
            [i["key"] for i in vb["items"] if i["level"] == "blocker"]))

    res_a = sum(1 for c in (snap_a.get("components") or []) if c.get("del"))
    res_b = sum(1 for c in (snap_b.get("components") or []) if c.get("del"))
    add("残骸", "ok" if not res_b else "warn",
        "目标图导出时没有待提交的删除" if not res_b else
        "目标图导出时带着 %d 件**待保存提交的删除**（身份见 inspect 的警告）："
        "这份读数本身还不干净，重进地图后重新导出再判" % res_b,
        {"from": res_a, "to": res_b})

    d = diff_snapshots(snap_a, snap_b, tol=tol)
    c = d["components"]
    dupx = c.get("duplicate_extra") or []
    sent = c.get("added_sentinel") or []
    renum = c.get("renumbered") or []
    hard, soft, quiet = [], [], []
    for x in c["changed"]:
        # 三种非"内容变了"的行里只有两种待遇：**换了序**要人看（顺序在这层可能就是数据，
        # 出生点/成员表谁在前谁在后说不准），**只差浮点末位**不必拦 —— `--tol` 就是本项目
        # 声明的坐标尺度，tol 以内的浮点差按定义就是同一个数；引擎落盘时会重新规范化末位，
        # 一次真还原永远消不掉它。把它压成 warn 等于宣布"跨账号还原不可能拿到 exit 0"，
        # 那条命令就废了。要更严自己加：`--tol 0.01` 时这些行会升级成 value。
        if _is_hard_change(x):
            hard.append(x)
        elif all(v in ("precision", "renumber")
                 for v in (x.get("kinds") or {}).values()):
            quiet.append(x)
        else:
            soft.append(x)
    comp_fail = bool(c["added"] or c["removed"] or hard or dupx)
    level = "fail" if comp_fail else ("warn" if soft else "ok")
    if not comp_fail and not soft:
        cmsg = "逐件一致（容差 %s，残骸 %d 件不计）" % (tol, c["residue"]["to"])
    else:
        cmsg = ("新增 %d / 删除 %d / 属性或变换变更 %d（其中内容变了 %d）/ 同内容重复 %d 组"
                % (len(c["added"]), len(c["removed"]), len(c["changed"]),
                   len(hard), len(dupx)))
        if sent:
            cmsg += "；哨兵位新增 %d 件（像未确认态）" % len(sent)
        if soft and not c["added"] and not c["removed"]:
            cmsg += ("；%d 件只差在**顺序/浮点末位**（%s），内容在容差内配对得上，"
                     "但仍要人眼确认顺序是不是数据"
                     % (len(soft), "、".join(sorted({k for x in soft
                                                     for k in x.get("kinds", {}).values()}))))
    if c["changed"] and not c["added"] and not c["removed"] and hard:
        cmsg += ("；只剩属性变更时，差异可能是任意配对的结果，"
                 "定位到具体组件要用 probe 或重进回对")
    if renum:
        cmsg += ("；另有 %d 件只是**跨会话落盘 id 重编号**"
                 "（成员表换了号，内容逐字段一致，不计入差异）" % len(renum))
    if quiet:
        cmsg += ("；另有 %d 件只差**浮点末位**（引擎落盘重新规范化，%s 以内视为同一个数），"
                 "不拦结论" % (len(quiet), tol))
    add("组件", level, cmsg,
        {"added": len(c["added"]), "removed": len(c["removed"]),
         "changed": len(c["changed"]), "changed_value": len(hard),
         "changed_soft": len(soft), "changed_precision": len(quiet),
         "duplicate_extra": len(dupx),
         "renumbered": len(renum), "residue": c["residue"]})

    e, s = d["events"], d["story"]
    ev_bad = bool(e["added"] or e["removed"] or e["changed"])
    ev_notes = (["%d 处引用 key_ok=false（%s）" % (len(e["untrusted"]), e["untrusted"][:3])]
                if e["untrusted"] else [])
    if e.get("missing_dims"):
        ev_notes.append("有一方没采到 %s 维（老导出）" % "/".join(e["missing_dims"]))
    # 先报**查到了且不同**，再报**没比成**：反过来会把坏消息的数字盖掉。
    # 但"没比成"永远跟着印出来 —— 只说"有 2 条不同"而漏掉"另有一维根本没比"是不诚实的通过。
    if ev_bad:
        add("事件", "fail",
            "新增 %d / 删除 %d / 绑定变更 %d：%s%s"
            % (len(e["added"]), len(e["removed"]), len(e["changed"]),
               (e["changed"][:4] or "—"),
               ("；⚠️ 另有 %s ⇒ 这些**没比成**" % "、".join(ev_notes)) if ev_notes else ""))
    elif ev_notes:
        add("事件", "unknown",
            "这一层**没比成**：%s —— 把「没这维/读数不能信」当成「一致」是不行的"
            % "、".join(ev_notes))
    elif e.get("sort_only"):
        add("事件", "warn",
            "%d 条事件的引用与保存状态都一致，只有**面板顺序**不同（%s）——"
            "玩法不受影响，但要人确认一下" % (len(e["sort_only"]), e["sort_only"][:3]))
    else:
        add("事件", "ok",
            "%d 条事件、引用（锚点+字段+值+槽位下标）与在不在保存集合里，逐条一致"
            % ((snap_b.get("events") or {}).get("count", 0)))
    st_bad = bool(s["added"] or s["removed"] or s["changed"])
    st_notes = []
    if s.get("uncaptured_levels"):
        st_notes.append("有 %d 列只在一边采到过（导出版本差：%s）"
                        % (len(s["uncaptured_levels"]), s["uncaptured_levels"][:3]))
    if s.get("unresolved"):
        st_notes.append("%d 处跳转 id 解析不到（%s）" % (len(s["unresolved"]),
                                                        s["unresolved"][:3]))
    if st_bad:
        add("剧情", "fail",
            "新增 %d / 删除 %d / 内容变更 %d：%s%s"
            % (len(s["added"]), len(s["removed"]), len(s["changed"]),
               (s.get("changed_paths") or ["—"])[:4],
               ("；⚠️ 另有 %s ⇒ 这些**没比成**" % "、".join(st_notes))
               if st_notes else ""))
    elif st_notes:
        add("剧情", "unknown",
            "这一层**没比成**：%s —— 把「没采到/解析不到」读成「一致」是不行的"
            % "、".join(st_notes))
    else:
        add("剧情", "ok",
            "剧情/对话/选项**逐字段**一致（比了 %d 个字段，含生效类型/可触发次数/"
            "隐藏玩家/触发事件这类玩法字段；id 型引用已解析成名字再比）"
            % s.get("compared_fields", 0))

    m = d["map_setting"]
    if m["skipped"]:
        add("地图设置", "unknown",
            "有一方没采到 map_setting ⇒ 这一层**没比成**，不能算 1:1")
    else:
        ms_bad = bool(m["added"] or m["removed"] or m["changed"])
        add("地图设置", "ok" if not ms_bad else "fail",
            "%d 个玩法键一致（地图名/简介/标签不计）" % m["compared"]
            if not ms_bad else
            "差异键：变更 %s / 多 %s / 少 %s ⇒ 玩法行为会和源图不同"
            % ([x["key"] for x in m["changed"]][:6],
               [x["key"] for x in m["added"]][:6],
               [x["key"] for x in m["removed"]][:6]))

    # 地图属性层（默认背景音乐 / 排行榜 / 灵感笔记这些不是组件也不是玩法键的东西）：
    # 2026-10-01 用户报「还原出来的图没有地图默认背景音乐」——这一层以前不采，
    # 于是七层全绿的验收能在一张开局静音的图上印绿灯。
    p = d.get("map_props") or {"added": [], "removed": [], "changed": [], "skipped": True}
    if p["skipped"]:
        add("地图属性", "unknown",
            "有一方没采到地图属性层（%s 侧）⇒ 默认背景音乐这一层**没比成**；"
            "老导出请重导一次" % p.get("skipped_side", "某"))
    else:
        mp_bad = bool(p["added"] or p["removed"] or p["changed"])
        add("地图属性", "ok" if not mp_bad else "fail",
            "%d 项一致（默认背景音乐/排行榜/人数/灵感笔记等，不是组件也不是玩法键）"
            % p["compared"] if not mp_bad else
            "差异：%s ⇒ 这些是地图级设置，逐件一致也会玩起来不一样"
            % ([x.get("key") for x in (p["changed"] + p["added"] + p["removed"])][:6]))

    ba = snap_a.get("budget") or {}
    bb = snap_b.get("budget") or {}
    nums = [(k, ba.get(k), bb.get(k)) for k in ("component_nums", "registered")
            if ba.get(k) is not None or bb.get(k) is not None]
    mismatch = [x for x in nums if x[1] != x[2]]
    add("件数", "ok" if not mismatch else "warn",
        ("件数计数器一致：%s" % ", ".join("%s=%s" % (k, v) for k, _, v in nums)
         if not mismatch else
         "计数不一致 %s —— 注意 occupancy 是本地记账（不能当判据），"
         "以逐件比对为准" % mismatch),
        {"rows": nums})

    ok = all(l["level"] == "ok" for l in layers) and not blocked
    return {"layers": layers, "ok": ok, "blocked": blocked,
            "unknown": [l["key"] for l in layers if l["level"] == "unknown"],
            "warn": [l["key"] for l in layers if l["level"] == "warn"],
            "fail": [l["key"] for l in layers if l["level"] == "fail"]}


def print_acceptance(rep):
    mark = {"ok": "✓", "warn": "ℹ️ ", "fail": "⛔", "unknown": "❓",
            "blocker": "⛔"}
    for l in rep["layers"]:
        print("  %s %-8s %s" % (mark.get(l["level"], "?"), l["key"], l["msg"]))
    if rep["unknown"]:
        print("  ⚠️ 没比成的层：%s ⇒ 不能声称 1:1" % "、".join(rep["unknown"]))
    if rep["warn"]:
        print("  ℹ️ 需要人工确认的层：%s" % "、".join(rep["warn"]))
    print("结论：%s" % ("**1:1 一致**" if rep["ok"] else
                    ("文件有阻塞项，先修再判" if rep["blocked"] else "不是 1:1（见上）")))


def cmd_accept(args):
    a = _load(args.snapshot)
    if getattr(args, "live", False):
        print("导出当前地图（只读）…")
        b = build_snapshot()
    elif not args.other:
        print("需要第二份快照（目标图当前状态）或 --live")
        return 2
    else:
        b = _load(args.other)
    print("1:1 验收：%s  ←  %s"
          % ((b.get("map") or {}).get("name") or "?",
             (a.get("map") or {}).get("name") or "?"))
    rep = acceptance_report(a, b, tol=args.tol)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    else:
        print_acceptance(rep)
    return 0 if rep["ok"] else 1


def required_flags(snap):
    """这份文件要摆成 1:1 需要哪些开关（纯离线，不连游戏）。

    为什么放在 `check` 里：交付方拿到的是一个 JSON，开关清单原本只有**接收方**跑
    `restore` 预检时才看得到（而那一步要求已经装了同版本工具链）。写进体检输出，
    文件自己就说清了"照抄哪串命令才是完整的"，也顺手挡住"少一整层"这种最贵的失误。
    """
    import argparse
    try:
        from snapshot_restore import build_plan, stage_requirements
    except Exception as exc:                 # 工具链被挪动/依赖缺失时不许把体检一起带崩
        return {"error": str(exc)[:120], "rows": []}
    types = set()
    for c in (snap.get("components") or []):
        cid = _info_of(c).get("component_id")
        if cid is not None:
            types.add(cid)
    ns = argparse.Namespace(include_ui=False, include_motion=False, allow_sentinel=False,
                            with_story=False, include_global=False,
                            include_not_placeable=False, only_types=None, only_ids=None,
                            sync_settings=False, sync_global=False)
    plan = build_plan(snap, types, ns)
    return {"rows": stage_requirements(snap, plan, ns), "skipped": plan["to_place"]}


def cmd_check(args):
    snap = _load(args.file)
    v = validate_snapshot(snap)
    print("快照：%s（%s）｜ 组件 %d"
          % ((snap.get("map") or {}).get("name"), snap.get("captured_at"),
             len(snap.get("components") or [])))
    print_validation(v)
    need = required_flags(snap)
    if need.get("error"):
        print("ℹ️ 开关清单没算出来（%s）——不影响上面的交付结论，但照抄命令前请先核工具链"
              % need["error"])
    elif need["rows"]:
        print("\n要摆成 1:1，命令得带这些开关（不带就是**少一整层**，跑完几十分钟才发现）：")
        for r in need["rows"]:
            print("  %-30s %6d 处   %s" % (r["stage"], r["count"], r["flag"]))
        # 顺手把整条命令拼出来：少打一漏最常见就是因为开关是分几行抄的
        flags = " ".join(sorted({r["flag"] for r in need["rows"]}))
        print("\n照抄这条（路径按需替换）：")
        print("  python cli/snapshot_restore.py <这张图的快照>.json --apply %s" % flags)
        if any(r["flag"] == "--include-ui" for r in need["rows"]):
            print("  （`--include-ui` 那批是「通道已核、跨重进落库未证」：带上它才是无保留的 1:1，"
                  "但要先小批试点；不带它就得配 `--allow-missing-stages`）")
            print("  ⚠️ 这里是**离线按快照自己**算的，没对着目标图判：界面控件/角色这类每图自带，"
                  "目标图若是新开图，那一层已经齐 ⇒ 照抄 `--include-ui` 会插一层重复件。"
                  "restore 的预检会按目标图现状给净缺口，并以「⊖ 计划扣掉 N 件」自动扣回")
    else:
        print("\n要摆成 1:1：这份文件不需要任何额外的层开关。")
    return 0 if v["ok"] else 1


def cmd_export(args):
    out = args.out
    if not out:
        name = _probe_name()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(ch for ch in (name or "map") if ch not in '\\/:*?"<>|').strip() or "map"
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        out = os.path.join(DEFAULT_DIR, "%s_%s.json" % (safe, stamp))
    d = os.path.dirname(os.path.abspath(out))
    if d:
        os.makedirs(d, exist_ok=True)
    snap = build_snapshot(batch=args.batch, terrain_step=args.terrain)
    # 原子落盘：先写同目录 tmp、再 os.replace 换名（与桥接心跳同一手法）。直接
    # open(out,"w") 的半截文件会占着交付路径冒充完整导出 —— check 虽能靠自洽锚点
    # 查出截断，但「存在一个同名文件」本身就是误导。
    tmp = "%s.tmp%d" % (out, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    size = os.path.getsize(out)
    print("已写入 %s（%.1f KB）" % (out, size / 1024.0))
    for k, v in summarize(snap).items():
        if k == "type_distribution":
            continue
        print("  %-14s %s" % (k, v))
    td = summarize(snap)["type_distribution"] or {}
    if td:
        top = sorted(td.items(), key=lambda kv: -kv[1])[:8]
        print("  类型 top8      %s" % ", ".join("%s×%d" % kv for kv in top))
    return 0


def _probe_name():
    d = _call("map_info", {})
    node = _unwrap(d, "map_setting") or {}
    return (node.get("map_setting") or {}).get("name")


def cmd_inspect(args):
    snap = _load(args.file)
    s = summarize(snap)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    td = snap.get("map", {}).get("type_distribution") or {}
    if td:
        print("\n类型分布（前 %d）：" % args.limit)
        for k, v in sorted(td.items(), key=lambda kv: -kv[1])[:args.limit]:
            print("  %-28s %d" % (k, v))
    return 0


def cmd_diff(args):
    a, b = _load(args.a), _load(args.b)
    d = diff_snapshots(a, b, tol=args.tol, include_identity=args.include_identity)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
    else:
        print_diff(d, show=args.show)
    # --exit-code：把结论编进退出码（一致 0 / 有差异 1），脚本化的「改前/改后比对」
    # 不必去解析输出。判据就是 print_diff「结论」行用的同一个 identical 字段 ——
    # 不另写一套，免得两个出口各说各话。
    if args.exit_code:
        return 0 if d["identical"] else 1
    return 0


def cmd_restore(args):
    """转发到 cli/snapshot_restore.py（写操作，默认 dry-run）。"""
    from snapshot_restore import main as _restore_main
    argv = [args.snapshot] + list(args.rest or [])
    return _restore_main(argv)


def main(argv=None):
    p = argparse.ArgumentParser(prog="snapshot", description="地图快照（导出 / 查看 / 比对 / 还原）")
    sub = p.add_subparsers(dest="cmd")

    pe = sub.add_parser("export", help="导出当前地图快照")
    pe.add_argument("--out", help="输出路径（默认 snapshots/<地图>_<时间>.json）")
    pe.add_argument("--batch", type=int, default=BATCH_MAX,
                    help="每批组件数，≤%d（默认 %d）" % (BATCH_MAX, BATCH_MAX))
    pe.add_argument("--terrain", type=float, default=0,
                    help="采样地形高度场的步长（如 100）；不传则不采样")
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser("inspect", help="查看快照摘要")
    pi.add_argument("file")
    pi.add_argument("--limit", type=int, default=12)
    pi.set_defaults(func=cmd_inspect)

    pc = sub.add_parser("check", help="离线体检快照文件：交付前判断能不能拿去还原")
    pc.add_argument("file")
    pc.set_defaults(func=cmd_check)

    pa = sub.add_parser("accept",
                        help="1:1 验收：逐层（文件/残骸/组件/事件/剧情/设置/件数）"
                             "给一个结论，**没比成的层不算通过**")
    pa.add_argument("snapshot", help="要达标的快照（交付文件）")
    pa.add_argument("other", nargs="?", help="目标图当前状态的快照")
    pa.add_argument("--live", action="store_true",
                    help="不读第二份文件，现导当前打开的地图来比（只读）")
    pa.add_argument("--tol", type=float, default=0.5)
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=cmd_accept)

    pd = sub.add_parser("diff", help="比对两份快照")
    pd.add_argument("a")
    pd.add_argument("b")
    pd.add_argument("--tol", type=float, default=0.5, help="坐标容差（默认 0.5）")
    pd.add_argument("--show", type=int, default=20, help="每类最多显示条数")
    pd.add_argument("--json", action="store_true", help="输出 JSON")
    pd.add_argument("--exit-code", action="store_true",
                    help="把结论编进退出码：一致 0 / 有差异 1（脚本化比对用）")
    pd.add_argument("--include-identity", action="store_true",
                    help="连地图身份键（名/简介/标签）一起比；默认不比"
                         "（跨账号复刻时两边本就该不同）")
    pd.set_defaults(func=cmd_diff)

    pr = sub.add_parser("restore", help="把快照重建到目标图（写操作，默认 dry-run）",
                        add_help=False)
    pr.add_argument("snapshot")
    pr.add_argument("rest", nargs=argparse.REMAINDER,
                    help="透传给 snapshot_restore：--apply --limit N --batch N "
                         "--no-props --no-events --with-story --include-ui "
                         "--include-motion --allow-sentinel --new-run --yes")
    pr.set_defaults(func=cmd_restore)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    if getattr(args, "batch", BATCH_MAX) > BATCH_MAX:
        args.batch = BATCH_MAX
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
