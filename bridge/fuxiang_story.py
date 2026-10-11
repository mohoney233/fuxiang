# -*- coding: utf-8 -*-
"""随笔浮想编辑器：剧情（STORY / DIALOGUE / CHOICE）读写 + 文件化导出导入。

数据模型（2026-09-21 实机反汇编 + 实机验证）
-------------------------------------------
编辑器里没有"一份剧情数据"，剧情是三种 UGC 逻辑组件的组合：

  STORY    (10007001, UGCStoryComponent)    剧情容器：名称 / 生效范围 / 跳转目标…
  DIALOGUE (10007002, UGCDialogueComponent) 对话节点：``story_ugc_id`` 指向所属剧情，
                                            ``sort_id`` 决定顺序，``content`` 是正文
  CHOICE   (10007003, UGCChoiceComponent)   选项：``dialogue_ugc_id`` 指向所属对话，
                                            ``jump_story`` / ``jump_dialogue`` 决定跳转

三者都挂在 ``UgcGlobalUnit`` 上（无实体、不占占用值），数据存在
``component.component_info['events']``（属性袋，形如 ``{type: property_id, info: 值}``）。
值一律是**单元素 list**：文本 ``["对白"]``、数值 ``[1]``、颜色 ``[["A0AED6"]]``。

写入一个字段必须走三步（缺一不可）：

  1. ``comp.update_component_info(events)`` 原生更新器 —— 组件 events 赋值 +
     ``_owner.apply_ugc_info()`` 单元级同步（保存通道读的就是单元级 ugc_info）+
     引擎实时应用，与游戏属性面板同一条路径。
     ⚠️ 不要用 ``init_ugc_info(info, True)``：反汇编确认它只改组件内存、不碰
     owner 单元，保存落库的仍是服务端的旧数据 —— 2026-09-21 剧情内容重进即失
     的根因（区域+信息提示配方用的 update_component_info 则验证可持久化）。
  2. ``comp._init_xxx(value)``            让**当前客户端**状态生效
     （``_init_*`` 不在 ``apply_components_client`` 的分发表里，只改袋子不会生效）
  3. ``ui._add_change_unit(unit.uid)``    登记进保存变更集

创建节点走**原生剧情面板**那条命令（``UGCStoryComponentManagerPart.create_*`` →
``send_battle_event(UGC, key='add_unit_story')``），是异步的服务端往返：**游戏主线程内
禁止等待**，因此创建是 begin / poll 两步，由调用方在进程外轮询。
详见下方「创建：原生 add_unit_story 通道」的注释（通用摆放会把同类节点挤进同一个
UgcGlobalUnit，保存时互相覆盖）。

op 一览
-------
  story_fields  字段字典（含允许值，只读）      story_tree    当前地图的剧情树（只读）
  story_create  发起创建一个节点（异步）        story_create_poll  查询并可选落字段
  story_set     改一个节点的字段                story_delete  删节点（可级联）
  story_export  导出到 json / md 文件           story_import  比对文件→生成变更计划（可写）
  story_import_next  推进导入计划里的下一个创建
"""
import os
import sys
import json
import time
import importlib

CT_STORY = 10007001
CT_DIALOGUE = 10007002
CT_CHOICE = 10007003

KIND_BY_CT = {CT_STORY: "story", CT_DIALOGUE: "dialogue", CT_CHOICE: "choice"}
CT_BY_KIND = {"story": CT_STORY, "dialogue": CT_DIALOGUE, "choice": CT_CHOICE}

ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
DEFAULT_EXPORT_DIR = os.path.join(ROOT_DIR, "projects", "story")


def _resolve_path(path):
    """相对路径按 tools/fuxiang/ 解析（游戏进程的 cwd 不是本目录）。"""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(ROOT_DIR, path))

FORMAT_NAME = "fuxiang-story"
FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# 字段表：语义名 -> (property_id, 编码类型, 原生方法, 中文名, 允许值)
# 依据：各组件类的 ITEM_PARAM_2_EVENT_TYPE / EVENT_TYPE_2_METHOD_NAME 与
#       GK.DM.ugc_item_property 的 property_params（实测 dump 见 probe/_story_schema.json）
# ---------------------------------------------------------------------------
FIELDS = {
    "story": {
        "name":              (1000700114703, "text", "_init_story_name", "剧情名称", None),
        "edit_time":         (1000700114702, "int", "_init_edit_time", "编辑时间(unix秒)", None),
        "enable_quit":       (1000700114709, "int", "_init_enable_quit", "允许退出", [0, 1]),
        "effect_type":       (1000700114706, "int", "_init_effect_type", "生效类型", [1051, 1052, 1053]),
        "effect_camp":       (1000700114707, "str", "_init_effect_camp", "生效阵营", None),
        "effect_class":      (1000700114708, "str", "_init_effect_class", "生效角色", None),
        "effect_count":      (1000700114710, "int", "_init_effect_count", "可触发次数", None),
        "only_trigger_self": (1000700114711, "int", "_init_only_trigger_self", "仅自己触发", [0, 1]),
        "only_enable_host":  (1000700114716, "int", "_init_only_enable_host", "仅房主生效", [0, 1]),
        "hide_player_units": (1000700114717, "int", "_init_hide_player_units", "隐藏玩家单位", [0, 1]),
        "jump_type":         (1000700114712, "int", "_init_jump_type", "跳转类型", [1511, 1226]),
        "jump_target":       (1000700114713, "int", "_init_jump_target", "跳转目标(剧情ugc_id)", None),
        "voice_config":      (1000700114714, "str", "_init_voice_config", "语音配置", None),
        # 事件槽（值=事件名）。方向按 80001 段第二元素判定：1=发送 / 2=接收
        #   start_play 使剧情开始播放=接收；end_play 剧情播放结束=发送；
        #   quit_play 使剧情立刻结束=接收（见 probe/_event_field_shapes.json）
        "start_play_event":  (1000700114704, "str", "_start_play", "触发播放的事件名", None),
        "end_play_event":    (1000700114705, "str", "_end_play", "播放结束发出的事件名", None),
        "quit_play_event":   (1000700114715, "str", "_quit_play", "立刻结束的事件名", None),
    },    "dialogue": {
        "story_id":    (1000700240001, "int", "_init_story_ugc_id", "所属剧情ugc_id", None),
        "sort_id":     (1000700240002, "int", "_init_dialogue_sort_id", "对话顺序", None),
        "talker_type": (1000700240003, "int", "_init_dialogue_talker_type", "说话人类型",
                        [1223, 1224, 1225, 1541]),
        "npc":         (1000700240004, "str", "_init_dialogue_npc", "说话人NPC", None),
        "custom":      (1000700240005, "text", "_init_dialogue_custom", "自定义说话人", None),
        "content":     (1000700240006, "text", "_init_dialogue_content", "对话内容", None),
        "perform_type": (1000700240101, "int", "_init_dialogue_perform_type", "演出类型",
                         [1229, 1230, 1231, 1232]),
        "show_quit":   (1000700240007, "int", "_init_dialogue_show_quit", "显示退出", [0, 1]),
        "start_event": (1000700240008, "str", "_dialogue_start", "对话开始发出的事件名", None),
        "end_event":   (1000700240009, "str", "_dialogue_end", "对话结束发出的事件名", None),
        "text_type":   (1000700217201, "int", "_init_dialogue_text_type", "文本类型",
                        [1081, 1082, 1083, 1084, 1085, 1086]),
        "text_size":   (1000700217202, "int", "_init_dialogue_text_size", "字号", None),
        "text_color":  (1000700217203, "color", "_init_dialogue_text_color", "文本颜色", None),
        "h_align":     (1000700217204, "int", "_init_dialogue_text_horizontal_align", "水平对齐",
                        [1521, 1522, 1523]),
        "v_align":     (1000700217205, "int", "_init_dialogue_text_vertial_align", "垂直对齐",
                        [1531, 1532, 1533]),
    },
    "choice": {
        "dialogue_id":  (1000700340001, "int", "_init_dialogue_ugc_id", "所属对话ugc_id", None),
        "sort_id":      (1000700340002, "int", "_init_choice_sort_id", "选项顺序", None),
        "jump_type":    (1000700340004, "int", "_init_jump_type", "跳转类型",
                         [1226, 1227, 1228, 1741]),
        "jump_story":   (1000700340005, "int", "_init_jump_story", "跳转剧情ugc_id", None),
        "jump_dialogue": (1000700340006, "int", "_init_jump_dialogue", "跳转对话ugc_id", None),
        "content":      (1000700340007, "text", "_init_choice_content", "选项内容", None),
        "enable":       (1000700340008, "int", "_init_enable", "是否启用", [0, 1]),
        "on_touched_event": (1000700340003, "str", "_on_touched",
                             "被点击时发出的事件名（原生默认与选项同名）", None),
    },
}

# 特殊联动：写某字段时需要同步镜像到另一个模板槽（原生行为，实机解剖确认）
# 事件槽位 property_id（值=事件名）。写入这些槽时要：①确保事件存在（没有就建）
# ②把引用 (ugc_id, 槽位序号, 0) 登记进事件注册表，否则保存后引用丢失。
# 实机依据：原生建选项时面板会自动建与选项同名的事件并绑到 40003（「选项1」事件实证）。
EVENT_SLOT_PIDS = {
    1000700114704,  # story 使剧情开始播放（接收）
    1000700114705,  # story 剧情播放结束（发送）
    1000700114715,  # story 使剧情立刻结束（发送）
    1000700240008,  # dialogue 对话开始（发送）
    1000700240009,  # dialogue 对话结束（发送）
    1000700340003,  # choice 选项被点击（发送）
}

# 树形结构的父子字段
LINK_FIELD = {"dialogue": "story_id", "choice": "dialogue_id"}
ORDER_FIELD = "sort_id"

# ---------------------------------------------------------------------------
# 原生模板槽位（2026-09-21 从用户手动创建的「剧情1」实机解剖固化，probe/_native_template.json）
# 每项: (property_id, disable_delete)。面板新建节点时按此顺序预置全部槽位，
# 大多带 disable_delete=true（面板不可删）。API 建的节点也补齐成同构，避免
# 保存/恢复路径按模板校验时丢弃非同构数据。
# ---------------------------------------------------------------------------
TEMPLATE_SLOTS = {
    "story": [
        (1000700114703, True),   # 剧情名称
        (1000700114702, None),   # 编辑时间（原生写 [unix秒]）
        (1000700114704, True),   # 使剧情开始播放（事件接收槽）
        (1000700114706, True),   # 生效类型
        (1000700114705, True),   # 剧情播放结束（事件发送槽）
        (1000700114709, True),   # 允许退出
        (1000700114710, True),   # 可触发次数
        (1000700114711, True),   # 仅自己触发
        (1000700114716, True),   # 仅房主生效
        (1000700114714, True),   # 语音配置
        (1000700114717, True),   # 隐藏玩家单位
        (1000700114715, True),   # 使剧情立刻结束（事件发送槽）
        (1000700114712, True),   # 跳转类型
    ],
    "dialogue": [
        (1000700240001, None),   # 所属剧情 ugc_id
        (1000700240002, True),   # 对话顺序
        (1000700240006, None),   # 对话内容
        (1000700240003, True),   # 说话人类型
        (1000700240008, True),   # 对话开始（事件发送槽）
        (1000700240009, True),   # 对话结束（事件发送槽）
        (1000700217201, True),   # 文本类型
        (1000700217202, True),   # 字号
        (1000700217203, True),   # 文本颜色
        (1000700217204, True),   # 水平对齐
        (1000700217205, True),   # 垂直对齐
    ],
    "choice": [
        (1000700340001, None),   # 所属对话 ugc_id
        (1000700340002, True),   # 选项顺序
        (1000700340007, None),   # 选项内容
        (1000700340003, True),   # 选项被点击（事件发送槽；原生把选项文本也写进这里）
        (1000700340004, True),   # 跳转类型
        (1000700340008, True),   # 是否启用
        (1000700340009, True),   # 设为可用（事件槽）
        (1000700340010, True),   # 设为不可用（事件槽）
    ],
}


def _complete_template(kind, events, fields=None):
    """把 events 补齐/重排成原生模板同构：模板槽按模板顺序，值缺省 null；
    用户字段值覆盖对应槽；模板外的槽保留在尾部（保持原相对顺序）。"""
    fields = fields or {}
    by_pid = {}
    for e in events:
        by_pid[str(e.get("type"))] = e
    ordered = []
    for pid, dd in TEMPLATE_SLOTS.get(kind, []):
        spid = str(pid)
        if spid in by_pid:
            slot = by_pid.pop(spid)
        else:
            slot = {"type": pid, "info": None}
        if dd is not None:
            slot["disable_delete"] = dd
        # 模板内槽位若正是本次要写的字段，值已经在 events 里（调用方先合并）
        ordered.append(slot)
    ordered.extend(by_pid.values())
    return ordered


def field_spec(kind, name):
    return (FIELDS.get(kind) or {}).get(name)


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def _room(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != "WorldBattleUGCEdit":
        raise ValueError("当前不在随笔浮想编辑器（room 不可用）")
    return room


def _cm(edit):
    cm = edit.get_component_manager()
    if cm is None:
        raise ValueError("组件管理器不可用")
    return cm


def _ui(edit):
    ops = importlib.import_module("fuxiang_edit_ops")
    return ops._ui(edit)


def _plain(edit, v, depth=0):
    return edit._safe(v, depth)


def _history(room, record=None):
    buf = vars(room).setdefault("_fuxiang_story_history", [])
    if record is None:
        return buf
    buf.append(record)
    del buf[:-100]
    return buf


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------
def encode_value(spec, value):
    """把用户给的值编成属性袋里的 info（一律 list）。"""
    pid, vtype, _method, _label, allowed = spec
    if vtype == "text":
        return [str(value)]
    if vtype == "int":
        iv = int(value)
        if allowed and iv not in allowed:
            raise ValueError("取值 %r 不在允许集合 %s 内" % (iv, allowed))
        return [iv]
    if vtype == "color":
        if isinstance(value, list):
            return [list(value)]
        return [[str(value).lstrip("#").upper()]]
    if vtype == "str":
        return [str(value)]
    if vtype == "float":
        return [float(value)]
    raise ValueError("未知编码类型 %r" % vtype)


def decode_value(spec, info):
    """把属性袋里的 info 还原成标量（列表只取首元素）。

    ⛔ 数值字段的空串必须归成 None（2026-09-23 实机）：源图 `edit_time` 槽的 info
    就是 `[""]`（面板没填时原生写空串），照抄进文档后 `encode_value` 的 `int("")`
    会抛 ValueError，**整个节点**的字段写入被连累回滚 → 节点补不齐模板槽 →
    之后指向它的事件引用补绑时报「装不下下标」（事件「剧情被触发」就是这样丢的）。
    """
    vtype = spec[1] if len(spec) > 1 else None
    if isinstance(info, list):
        if len(info) == 1:
            v = info[0]
        elif not info:
            return None
        else:
            v = info
    else:
        v = info
    if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
        # 颜色： [["A0AED6"]]
        return v[0]
    if vtype in ("int", "float") and isinstance(v, str) and not v.strip():
        return None
    return v


def _events_map(edit, comp):
    ci = getattr(comp, "component_info", None)
    out = {}
    if not hasattr(ci, "get"):
        return out
    for e in (ci.get("events") or []):
        if not hasattr(e, "get"):
            continue
        out[str(e.get("type"))] = _plain(edit, e.get("info"))
    return out


def node_fields(edit, comp, kind):
    """读出一个节点所有已知字段（未设置的字段不出现在结果里）。"""
    raw = _events_map(edit, comp)
    out = {}
    for name, spec in (FIELDS.get(kind) or {}).items():
        pid = str(spec[0])
        if pid in raw:
            out[name] = decode_value(spec, raw[pid])
    return out


# ---------------------------------------------------------------------------
# 读取：剧情树
# ---------------------------------------------------------------------------
def _deleted(room):
    """本会话内已被原生命令层删除的 ugc_id。

    2026-09-21 实测：删除后 ``cm._ugc_id2component`` 仍保留残骸（这是游戏 Ctrl+Z
    撤销所依赖的，不能清理），且组件对象上没有任何「已删除」标记，因此只能在
    本模块记一笔会话内名单，读树时跳过。重进地图后名单自然失效（残骸也没了）。
    """
    return vars(room).setdefault("_fuxiang_story_deleted", set())


def _iter_kind(edit, cm, ct, skip=()):
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    out = []
    for ugc_id, comp in id2c.items():
        if getattr(comp, "COMPONENT_TYPE", None) != ct:
            continue
        try:
            ugc_id = int(ugc_id)
        except Exception:
            continue
        if ugc_id in skip:
            continue
        out.append((ugc_id, comp))
    return sorted(out, key=lambda x: x[0])


def collect(edit, include_deleted=False):
    """把当前地图里的剧情三件套收成 {stories, dialogues, choices}（含孤儿）。"""
    cm = _cm(edit)
    room = _room(edit)
    skip = set() if include_deleted else set(_deleted(room))
    nodes = {"story": [], "dialogue": [], "choice": []}
    for ct, kind in KIND_BY_CT.items():
        for ugc_id, comp in _iter_kind(edit, cm, ct, skip=skip):
            unit = getattr(comp, "_owner", None)
            nodes[kind].append({
                "ugc_id": ugc_id,
                "kind": kind,
                "comp": comp,
                "owner_uid": getattr(unit, "uid", None),
                "fields": node_fields(edit, comp, kind),
            })
    return nodes


def build_tree(edit, include_deleted=False):
    """按 剧情 → 对话 → 选项 组装，sort_id 升序；挂不上父节点的进 orphans。"""
    n = collect(edit, include_deleted=include_deleted)
    stories = [x for x in n["story"]]
    dlg_by_story = {}
    orphan_dlg = []
    for d in n["dialogue"]:
        sid = d["fields"].get("story_id")
        if sid is None:
            orphan_dlg.append(d)
        else:
            dlg_by_story.setdefault(int(sid), []).append(d)
    cho_by_dlg = {}
    orphan_cho = []
    for c in n["choice"]:
        did = c["fields"].get("dialogue_id")
        if did is None:
            orphan_cho.append(c)
        else:
            cho_by_dlg.setdefault(int(did), []).append(c)

    def clean(node, extra=()):
        d = {"ugc_id": node["ugc_id"], "owner_uid": node["owner_uid"]}
        d.update(node["fields"])
        for k in extra:
            d[k] = node[k]
        return d

    out_stories = []
    for s in sorted(stories, key=lambda x: x["ugc_id"]):
        sd = clean(s)
        sd["dialogues"] = []
        for d in sorted(dlg_by_story.get(s["ugc_id"], []),
                        key=lambda x: (x["fields"].get("sort_id") is None,
                                       x["fields"].get("sort_id"), x["ugc_id"])):
            dd = clean(d)
            dd["choices"] = [clean(c) for c in sorted(
                cho_by_dlg.get(d["ugc_id"], []),
                key=lambda x: (x["fields"].get("sort_id") is None,
                               x["fields"].get("sort_id"), x["ugc_id"]))]
            sd["dialogues"].append(dd)
        out_stories.append(sd)
    return {"stories": out_stories,
            "orphan_dialogues": [clean(d) for d in orphan_dlg],
            "orphan_choices": [clean(c) for c in orphan_cho]}


def op_story_tree(edit, args):
    """当前地图的剧情树（只读）。kind=story|dialogue|choice 可只看一层。"""
    tree = build_tree(edit, include_deleted=bool(args.get("include_deleted")))
    kind = args.get("kind")
    if kind == "story":
        for s in tree["stories"]:
            s.pop("dialogues", None)
    if args.get("compact"):
        return {"story_count": len(tree["stories"]),
                "dialogue_count": sum(len(s.get("dialogues", [])) for s in tree["stories"])
                                  + len(tree["orphan_dialogues"]),
                "choice_count": sum(len(d.get("choices", []))
                                    for s in tree["stories"] for d in s.get("dialogues", []))
                                + len(tree["orphan_choices"]),
                "stories": [{"ugc_id": s["ugc_id"], "name": s.get("name"),
                             "dialogue_count": len(s.get("dialogues", []))}
                            for s in tree["stories"]],
                "orphan_dialogues": len(tree["orphan_dialogues"]),
                "orphan_choices": len(tree["orphan_choices"])}
    return tree


def op_story_fields(edit, args):
    """字段字典：语义名 / property_id / 类型 / 允许值（只读，供写文件与排错）。"""
    kind = args.get("kind")
    if kind and kind not in FIELDS:
        raise ValueError("kind 必须是 %s 之一" % sorted(FIELDS))
    out = {}
    for k, tbl in FIELDS.items():
        if kind and k != kind:
            continue
        out[k] = {name: {"property_id": spec[0], "value_type": spec[1],
                         "label": spec[3], "allowed": spec[4]}
                  for name, spec in tbl.items()}
    return {"kinds": out, "note": "值一律写成标量；本模块负责编成属性袋的单元素 list。"}


# ---------------------------------------------------------------------------
# 写入：创建（异步 begin / poll）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 创建：原生 add_unit_story 通道（每个节点独立实体）
# ---------------------------------------------------------------------------
# ⚠️ 剧情三件套**不能**走通用摆放 fuxiang_scene.place —— 2026-09-21 实机定位：
#   通用 add_unit 把同类节点全挂在同一个 UgcGlobalUnit 上（实测 9 条对话共用单元
#   1000073、6 个选项共用 1000074），而保存报文
#   ``UIBattleUGCEdit._get_change_uid`` → ``_get_ugc_info(unit, True)`` 取的是
#   ``ugc_utils.get_ugc_property(unit, 'component_info')``，即**单元主组件**
#   （``unit.ugc_component``，只有一个）的属性袋 ⇒ 一个单元只能带出一个节点的数据，
#   保存时同类节点互相覆盖。
#   原生「剧情」面板建节点用的是 UGCStoryComponentManagerPart 的三个命令：
#     create_stroy_component()                        # 引擎拼写如此（stroy），同时建 1 条首对白
#     create_dialogue_component(parent_story_idx)     # idx = 面板 sort 表里的下标
#     create_choice_component(story_idx, dialogue_idx)
#   三者都发 send_battle_event(GAME_BATTLE_EVENT_TYPE.UGC,
#           {'uid': GK.g_unit.uid, 'key': 'add_unit_story', 'create_info': [info]})
#   服务端为每个节点建独立单元，回调再 register_*_component_sort_id 登进面板表
#   （story_component_sort_dict / dialogue_component_sort_dict / choice_component_sort_dict）
#   —— 这三张表就是编辑器「剧情」面板的数据源，所以原生路径建出来立刻可见。
PANEL_TABLE_BY_KIND = {
    "story": "story_component_sort_dict",
    "dialogue": "dialogue_component_sort_dict",
    "choice": "choice_component_sort_dict",
}
CREATE_METHOD_NAMES = {
    "story": ("create_stroy_component", "create_story_component"),
    "dialogue": ("create_dialogue_component",),
    "choice": ("create_choice_component",),
}


def _panel(edit):
    """取编辑器的剧情面板 part（节点注册表就在它身上）。"""
    room = _room(edit)
    ipart = None
    for name in list(sys.modules):
        if name.endswith("UGCStoryComponentManagerPart"):
            ipart = getattr(sys.modules[name], "iUGCStoryComponentManagerPart", None)
            if ipart is not None:
                break
    if ipart is None:
        raise ValueError("找不到 iUGCStoryComponentManagerPart——当前不在剧情编辑器里？")
    part = room.get_ugc_edit_part(ipart)
    if part is None:
        raise ValueError("room.get_ugc_edit_part(iUGCStoryComponentManagerPart) 返回 None")
    return part


def _panel_uids(part, kind):
    return {int(k) for k in (getattr(part, PANEL_TABLE_BY_KIND[kind], None) or {})}


def _panel_sort_idx(part, kind, ugc_id):
    """ugc_id → 面板 sort 下标（原生创建命令要的是下标，不是 ugc_id）。"""
    table = getattr(part, PANEL_TABLE_BY_KIND[kind], None) or {}
    for k, v in table.items():
        try:
            if int(k) == int(ugc_id):
                return v
        except (TypeError, ValueError):
            continue
    return None


def _story_of_dialogue(edit, dialogue_uid):
    comp = _cm(edit)._ugc_id2component.get(int(dialogue_uid))
    if comp is None:
        return None
    return _plain(edit, getattr(comp, "_story_ugc_id", None))


def _native_create_begin(edit, kind, fields):
    """发起一次原生节点创建，返回 request_id（新节点身份由面板表差集给出）。"""
    if kind not in PANEL_TABLE_BY_KIND:
        raise ValueError("kind 必须是 %s 之一" % sorted(PANEL_TABLE_BY_KIND))
    part = _panel(edit)
    story_idx = dialogue_idx = None
    if kind == "dialogue":
        sid = fields.get("story_id")
        if sid is None:
            raise ValueError("创建 dialogue 需要 fields.story_id（所属剧情 ugc_id）")
        story_idx = _panel_sort_idx(part, "story", sid)
        if story_idx is None:
            raise ValueError("剧情 %s 不在面板表 story_component_sort_dict 里" % sid)
    elif kind == "choice":
        did = fields.get("dialogue_id")
        if did is None:
            raise ValueError("创建 choice 需要 fields.dialogue_id（所属对话 ugc_id）")
        dialogue_idx = _panel_sort_idx(part, "dialogue", did)
        if dialogue_idx is None:
            raise ValueError("对话 %s 不在面板表 dialogue_component_sort_dict 里" % did)
        sid = fields.get("story_id") or _story_of_dialogue(edit, did)
        story_idx = _panel_sort_idx(part, "story", sid) if sid is not None else None
        if story_idx is None:
            raise ValueError("选项所属剧情 %s 不在面板表里" % sid)
    before = {k: _panel_uids(part, k) for k in PANEL_TABLE_BY_KIND}
    rid = "story-native-%s-%d" % (kind, int(time.time() * 1000) % 1000000)
    store = vars(_room(edit)).setdefault("_fuxiang_story_native_create", {})
    for stale in [k for k, v in store.items()
                  if not isinstance(v, dict) or time.time() - v.get("at", 0) > 120]:
        store.pop(stale, None)
    fn = None
    for name in CREATE_METHOD_NAMES[kind]:
        fn = getattr(part, name, None)
        if callable(fn):
            break
    if fn is None:
        raise ValueError("原生创建方法缺失: %s" % (CREATE_METHOD_NAMES[kind],))
    if kind == "story":
        fn()
    elif kind == "dialogue":
        fn(story_idx)
    else:
        fn(story_idx, dialogue_idx)
    vars(_room(edit)).setdefault("_fuxiang_story_native_create", {})[rid] = {
        "kind": kind, "before": before, "polls": 0, "at": time.time()}
    return rid


def _native_create_poll(edit, rid):
    """查面板表差集：新出现的 uid 即本次创建的节点。

    原生「新建剧情」连带建一条首对白（create_stroy_component 一次发两个 create_info），
    那对白一并返回，交给调用方复用为文件的第 1 条对话，避免多出空节点。

    结果**缓存**在请求上（弹出交给超时清理逻辑）：调用方是一次一次的轮询，
    中途任何一次 IPC 抖动都不该让已完成的创建变成「没有该请求」。
    """
    room = _room(edit)
    store = vars(room).setdefault("_fuxiang_story_native_create", {})
    state = store.get(rid)
    if not state:
        return {"status": "unknown", "error": "没有该创建请求: %s" % rid}
    if state.get("result"):
        return dict(state["result"])
    part = _panel(edit)
    kind = state["kind"]
    new_main = sorted(_panel_uids(part, kind) - state["before"][kind])
    new_dlg = sorted(_panel_uids(part, "dialogue") - state["before"]["dialogue"])
    state["polls"] += 1
    if not new_main:
        if time.time() - state["at"] > 30:
            return {"status": "timeout", "error": "面板注册表 30 秒内没出现新 %s 节点" % kind}
        return {"status": "pending", "request_id": rid}
    if kind == "story" and len(new_dlg) < 1 and state["polls"] < 8:
        return {"status": "pending", "request_id": rid}
    out = {"status": "placed", "request_id": rid, "kind": kind, "ugc_id": new_main[0]}
    if kind == "story":
        out["auto_dialogue_ugc_id"] = new_dlg[0] if new_dlg else None
    out["pending_delete_cleared"] = _clear_pending_delete(
        edit, [out["ugc_id"], out.get("auto_dialogue_ugc_id")])
    state["result"] = out
    return dict(out)


def op_story_create(edit, args):
    """发起创建一个剧情节点（story / dialogue / choice），异步。

    args: kind（必填）, fields（可选，placed 后自动落）, request_id（可选）。
    dialogue/choice 必须在 fields 里给出 story_id / dialogue_id（父节点 ugc_id）。
    返回 request_id；随后调用 story_create_poll。
    """
    kind = args.get("kind")
    if kind not in CT_BY_KIND:
        raise ValueError("kind 必须是 %s 之一" % sorted(CT_BY_KIND))
    fields = args.get("fields") or {}
    unknown = [k for k in fields if field_spec(kind, k) is None]
    if unknown:
        return {"ok": False, "error": "未知字段 %s（可用：%s）"
                % (unknown, sorted(FIELDS[kind]))}
    room = _room(edit)
    fields = dict(fields)
    if kind == "story" and "edit_time" not in fields:
        fields["edit_time"] = int(time.time())   # 原生新建剧情会写编辑时间
    rid = args.get("request_id") or _native_create_begin(edit, kind, fields)
    pending = vars(room).setdefault("_fuxiang_story_pending_fields", {})
    pending[rid] = {"kind": kind, "fields": fields}
    return {"status": "pending", "request_id": rid, "kind": kind,
            "pending_fields": fields}


def _clear_pending_delete(edit, uids):
    """摘除服务端创建时给新组件打上的「待删除」标记。

    与 fuxiang_placement._confirm_global_component 同一坑：带标记的组件在保存时会被
    ``get_del_unit_uids`` 当删除上送，服务端刚建完就收到删除 ⇒ 落库为空。
    """
    room = _room(edit)
    cleared = []
    for uid in [u for u in uids if u is not None]:
        for bucket in ("component", "unit"):
            try:
                if room.check_del_unit_uids(int(uid), bucket):
                    room.change_del_unit_uids(False, int(uid), bucket)
                    cleared.append("%s:%s" % (bucket, uid))
            except Exception:
                continue
    return cleared


def op_story_create_poll(edit, args):
    """查询创建请求；placed 后自动落 fields 并登记保存变更集。"""
    rid = args.get("request_id")
    if not rid:
        raise ValueError("需要 request_id")
    room = _room(edit)
    res = _native_create_poll(edit, rid)
    if res.get("status") != "placed":
        return res
    ugc_id = res.get("ugc_id")
    pending = vars(room).get("_fuxiang_story_pending_fields") or {}
    spec = pending.pop(rid, None)
    # 无论有没有 fields 都执行一次落字段：_apply_fields 会把属性袋补齐成原生模板同构
    # （TEMPLATE_SLOTS）并经 update_component_info 同步 owner 单元——这是持久化的关键。
    if spec is not None and ugc_id is not None:
        try:
            applied = _apply_fields(edit, ugc_id, spec["kind"], spec.get("fields") or {})
            res["fields_applied"] = applied
        except Exception as exc:
            res["fields_error"] = "%s: %s" % (type(exc).__name__, exc)
    return res


# ---------------------------------------------------------------------------
# 写入：改字段
# ---------------------------------------------------------------------------
def _find(edit, ugc_id):
    cm = _cm(edit)
    id2c = getattr(cm, "_ugc_id2component", None) or {}
    comp = id2c.get(ugc_id) or id2c.get(str(ugc_id))
    if comp is None:
        for k, v in id2c.items():
            if str(k) == str(ugc_id):
                comp = v
                break
    if comp is None:
        raise ValueError("未找到组件 ugc_id=%r" % ugc_id)
    kind = KIND_BY_CT.get(getattr(comp, "COMPONENT_TYPE", None))
    if kind is None:
        raise ValueError("ugc_id=%r 不是剧情组件（component_type=%s）"
                         % (ugc_id, getattr(comp, "COMPONENT_TYPE", None)))
    return comp, kind


def _ensure_event(edit, name):
    """确保事件存在；不存在就通过原生命令层创建（进撤销栈）。"""
    cm = _cm(edit)
    reg = getattr(cm, "_ugc_params_events_key", {}).get("event") or {}
    entry = reg.get(name)
    if entry is not None and entry.get("info") is not None:
        return {"ok": True, "event_key": name, "existed": True}
    ops = importlib.import_module("fuxiang_edit_ops")
    return ops.op_event_create(edit, {"name": name})


def _params_header():
    """`unit.params` 里变换三元组的长度（= fuxiang_edit_ops.PARAMS_HEADER）。

    事件注册表存的引用是 `(ugc_id, index, params_idx)`，index 是**单元级**下标
    （见 fuxiang_edit_ops 模块头的「单元参数表结构不变量」）⇒ 登记时必须写
    `own_index + PARAMS_HEADER`，写本地下标会登记出一条指向**别的字段**的幽灵引用。
    """
    try:
        return int(getattr(importlib.import_module("fuxiang_edit_ops"),
                           "PARAMS_HEADER", 3))
    except Exception:
        return 3


def _register_event_refs(edit, comp, events):
    """把事件槽引用 (ugc_id, 槽位序号, 0) 登记进事件注册表。

    事件槽 info 里写了事件名但注册表没有引用记录时，保存后引用会丢
    （event_bind 的原生路径会登记，直写属性袋必须自己补）。

    ⚠️ 槽位序号是**单元级**（own_index + PARAMS_HEADER）。2026-09-23 实机：这里少加了
    PARAMS_HEADER，4 个选项各多出一条 (ugc, 3, 0) 的幽灵引用（解析回组件本地下标 0 =
    「所属对话ugc_id」字段），event_list 的 `bad_refs` 正是这么暴露的。
    """
    cm = _cm(edit)
    reg = getattr(cm, "_ugc_params_events_key", {}).get("event") or {}
    ugc_id = getattr(comp, "_ugc_id", None)
    header = _params_header()
    registered = []
    for idx, e in enumerate(events):
        try:
            pid = int(e.get("type"))
        except Exception:
            continue
        if pid not in EVENT_SLOT_PIDS:
            continue
        info = e.get("info")
        key = info[0] if isinstance(info, list) and info else None
        if not key or not isinstance(key, str):
            continue
        entry = reg.get(key)
        if entry is None or entry.get("info") is None:
            continue
        refs = list(entry.get("info") or [])
        ref = (ugc_id, idx + header, 0)
        if ref in refs:
            continue
        # 走 ops 的替换接口（`modify_ugc_params_events_key`）：`change_` 只能追加，
        # 写入后无法再摘除，排错时会分不清"没写进去"还是"清不掉"（见 _set_event_refs 注释）
        try:
            ops = importlib.import_module("fuxiang_edit_ops")
            ops._set_event_refs(cm, key, refs + [ref])
        except Exception:
            cm.change_ugc_params_events_key("event", key, refs + [ref])
        registered.append({"event": key, "ref": ref})
    return registered


def _apply_fields(edit, ugc_id, kind, fields, dry_run=False):
    """写入字段：属性袋 + 单元级同步 + 原生 _init_* + 登记变更集；失败回滚属性袋。

    2026-09-21 重大修正（剧情内容重进即失的根因）：
      旧实现用 ``comp.init_ugc_info(info, True)`` —— 反汇编确认它**只改组件内存**，
      从不调用 ``_owner.apply_ugc_info()``；而保存上送读的是**单元级**
      ``ui._get_ugc_info(unit)``（save_change 只发 change_uid，组件数据由单元
      ugc_info 提供）。结果保存落库的一直是服务端的空默认数据，重进全丢。
      现改用原生 ``comp.update_component_info(events)``：一次完成
      「组件 events 赋值 → _owner.apply_ugc_info() 单元级同步 → 引擎实时应用」，
      与游戏属性面板的写路径完全一致（区域+信息提示已验证配方同款）。
    """
    comp, actual_kind = _find(edit, ugc_id)
    if kind and kind != actual_kind:
        raise ValueError("ugc_id=%s 是 %s，不是 %s" % (ugc_id, actual_kind, kind))
    kind = actual_kind
    unknown = [k for k in fields if field_spec(kind, k) is None]
    if unknown:
        raise ValueError("未知字段 %s（%s 可用：%s）" % (unknown, kind, sorted(FIELDS[kind])))

    cm = _cm(edit)
    room = _room(edit)
    ui = _ui(edit)
    unit = getattr(comp, "_owner", None)
    ci = getattr(comp, "component_info", None)
    if not hasattr(ci, "get"):
        raise ValueError("组件没有 component_info")
    old_events = [_plain(edit, e) for e in (ci.get("events") or [])]
    old_unit_params = None
    try:
        old_unit_params = [_plain(edit, e) for e in ((ui._get_ugc_info(unit).get("params") or [])[3:])]
    except Exception:
        old_unit_params = None

    encoded, skipped_empty, encode_bad = {}, [], []
    for name, value in fields.items():
        spec = field_spec(kind, name)
        # ⛔ 单个字段编不出来不得让**整个节点**的写入回滚：节点补不齐模板槽后，
        #    指向它的事件引用补绑会因「装不下下标」失败（2026-09-23 实机：源图
        #    edit_time="" 让 int("") 抛错，STORY 节点卡在 2 槽，事件「剧情被触发」丢失）。
        #    空值的语义就是"没设置"，跳过即可（保留目标图上的既有值，别用垃圾覆盖）。
        if value is None or (spec[1] != "text"
                             and isinstance(value, str) and not value.strip()):
            skipped_empty.append({"field": name, "value": value})
            continue
        try:
            encoded[str(spec[0])] = encode_value(spec, value)
        except Exception as exc:
            encode_bad.append({"field": name, "value": _plain(edit, value),
                               "error": "%s: %s" % (type(exc).__name__, exc)})
    # 选项默认行为：与原生一致，自动建一个与选项同名的事件并绑到「选项被点击」槽
    if kind == "choice" and "content" in fields and "on_touched_event" not in fields:
        try:
            _ensure_event(edit, str(fields["content"]))
            encoded[str(1000700340003)] = [str(fields["content"])]
        except Exception:
            pass  # 事件创建失败不阻塞选项本身；事件槽的值仍会写入
    # 事件槽的值必须指向已存在的事件（注册表里有才能随保存持久化）
    for pid, info in list(encoded.items()):
        if int(pid) in EVENT_SLOT_PIDS and info:
            try:
                _ensure_event(edit, str(info[0]))
            except Exception:
                pass

    new_events = []
    touched = set()
    for e in (ci.get("events") or []):
        if not hasattr(e, "get"):
            continue
        pid = str(e.get("type"))
        slot = {"type": e.get("type"), "info": _plain(edit, e.get("info"))}
        if e.get("disable_delete") is not None:
            slot["disable_delete"] = e.get("disable_delete")
        if pid in encoded:
            slot["info"] = encoded[pid]
            touched.add(pid)
        new_events.append(slot)
    existing = {str(e.get("type")) for e in new_events}
    for pid, info in encoded.items():
        if pid in existing:
            continue
        new_events.append({"type": int(pid), "info": info})
        touched.add(pid)
    # 补齐/重排成原生模板同构（见 TEMPLATE_SLOTS 注释）
    new_events = _complete_template(kind, new_events)

    if dry_run:
        return {"dry_run": True, "ugc_id": ugc_id, "kind": kind,
                "before_events": old_events, "would_write": new_events,
                "touched": sorted(touched), "skipped_empty": skipped_empty,
                "encode_bad": encode_bad}

    logs = []
    try:
        # 原生更新器：组件 events + _owner.apply_ugc_info() + 引擎实时应用
        comp.update_component_info(new_events)
        logs.append("update_component_info 已写属性袋并同步 owner 单元 ugc_info")
        # 事件槽引用登记进注册表（否则保存后引用丢失）
        try:
            ev_refs = _register_event_refs(edit, comp, new_events)
            if ev_refs:
                logs.append("已登记事件引用: %s" % [r["event"] for r in ev_refs])
        except Exception as exc:
            logs.append("事件引用登记失败: %s: %s" % (type(exc).__name__, exc))
        applied = []
        for name, value in fields.items():
            spec = field_spec(kind, name)
            if str(spec[0]) not in encoded:
                continue  # 空值/编码失败：本次没写这个字段
            if spec[0] in EVENT_SLOT_PIDS:
                # spec[2] 是事件**处理器**（_start_play/_on_touched/…）不是初始化器；
                # 在编辑会话里调用它等于真的播放剧情，且 WorldBattleUGCEdit 没有
                # room.enable_story → AttributeError 会连累整次写入回滚（2026-09-21 实机）。
                logs.append("事件槽 %s 只写属性袋，不调用处理器 %s" % (name, spec[2]))
                continue
            method = getattr(comp, spec[2], None)
            if method is None:
                logs.append("缺少原生方法 %s（只写了属性袋）" % spec[2])
                continue
            native = decode_value(spec, encoded[str(spec[0])])
            method(native)
            applied.append({"field": name, "method": spec[2], "value": native})
        # 回读校验：组件袋是硬校验；单元级镜像只做提示。
        # 2026-09-21 实机修正：剧情三件套**按类型共享一个 UgcGlobalUnit**（面板表
        # story/dialogue/choice_component_sort_dict 就是这么组织的），而单元级
        # _get_ugc_info(unit) 的 params 取的是 unit.ugc_component 那**一个**主组件的袋
        # ——同类第 2、3…个节点写袋后，镜像里仍是主组件的值。把它当硬校验会误判成
        # 「回读不一致」并整次回滚（实测 dialogue#1 的两个节点就是这样被吞掉：
        # expected [1] got [0]/[5]）。所以单元级只记录差异，成败以组件袋为准。
        got = _events_map(edit, comp)
        mismatched = []
        for pid, info in encoded.items():
            if _plain(edit, got.get(pid)) != info:
                mismatched.append({"property_id": pid, "expected": info, "got": got.get(pid)})
        try:
            unit_params = ui._get_ugc_info(unit).get("params") or []
            unit_map = {str(p.get("type")): _plain(edit, p.get("info"))
                        for p in unit_params if hasattr(p, "get")}
            unit_lag = [{"property_id": pid, "unit_mirror": unit_map.get(pid)}
                        for pid, info in encoded.items() if unit_map.get(pid) != info]
            if unit_lag:
                logs.append("单元级镜像仍是同类其它节点的袋（共享全局单元所致，非错误）: %s"
                            % [x["property_id"] for x in unit_lag][:6])
        except Exception as exc:
            logs.append("单元级回读失败: %s: %s" % (type(exc).__name__, exc))
        if mismatched:
            raise RuntimeError("属性回读不一致: %s" % mismatched[:4])
        # 剧情/对话/选项都是「一个单元挂多个同类组件」，原生 _add_change_unit 只会登记
        # 该单元"当前"那一个组件 → 必须用 reg_change 把本节点自己补进 change_units。
        edit.reg_change(ui, unit, comp)
        logs.append("已登记 reg_change(uid=%s, comp=%s)" % (unit.uid, getattr(comp, "_ugc_id", None)))
        _history(room, {"kind": "story_set", "ugc_id": ugc_id, "node_kind": kind,
                        "fields": {k: _plain(edit, v) for k, v in fields.items()},
                        "at": time.strftime("%H:%M:%S")})
        return {"ok": True, "ugc_id": ugc_id, "kind": kind, "applied": applied,
                "touched": sorted(touched), "steps": logs,
                "skipped_empty": skipped_empty, "encode_bad": encode_bad,
                "unit_synced": True,
                "persistence_verified": False,
                "note": "已写入并同步到保存通道；持久化须在游戏内保存后重进核实。"}
    except Exception as exc:
        try:
            comp.update_component_info(old_events)
        except Exception:
            pass
        return {"ok": False, "ugc_id": ugc_id, "error": "%s: %s" % (type(exc).__name__, exc),
                "steps": logs, "rolled_back": True}


def op_story_set(edit, args):
    """改一个剧情节点的字段。args: ugc_id, fields{...}, dry_run(默认 false)。"""
    ugc_id = args.get("ugc_id")
    if ugc_id is None:
        raise ValueError("需要 ugc_id")
    fields = args.get("fields") or {}
    if not fields:
        raise ValueError("需要 fields（{字段名: 值}）")
    return _apply_fields(edit, int(ugc_id), args.get("kind"), fields,
                         dry_run=bool(args.get("dry_run")))


# ---------------------------------------------------------------------------
# 写入：删除
# ---------------------------------------------------------------------------
def _story_family_ids(edit):
    """当前地图里**存活**的剧情族组件 id（story / dialogue / choice）。"""
    tree = build_tree(edit, include_deleted=False)
    out = set()
    for s in tree["stories"]:
        out.add(int(s["ugc_id"]))
        for d in s.get("dialogues") or []:
            out.add(int(d["ugc_id"]))
            out.update(int(c["ugc_id"]) for c in d.get("choices") or [])
    for d in tree.get("orphan_dialogues") or []:
        out.add(int(d["ugc_id"]))
    for c in tree.get("orphan_choices") or []:
        out.add(int(c["ugc_id"]))
    return out


def plan_del_flags(bag, live, targets):
    """纯函数：给定待删除袋 / 存活剧情族 id / 本次目标，算出该怎么校正。

    抽出来是为了**能离线测试**（游戏内那部分逻辑不好测，而这段决策恰恰风险最高：
    算错会删掉不该删的剧情）。规则：
      * ``to_clear``：袋里**存活且非目标**的 → 原生删除误标上去的，必须摘掉
        （袋里那些「已不存在」的 id 不动：它们本来就是待删除的正当成员）
      * ``to_mark`` ：目标里**还没被标记**的 → 补上；**这才是服务端删除的唯一有效通道**
    两者都按升序返回，便于比对。
    """
    bag = {int(x) for x in bag or ()}
    live = {int(x) for x in live or ()}
    targets = {int(t) for t in targets or ()}
    return {"to_clear": sorted((bag & live) - targets),
            "to_mark": sorted(targets - bag)}


def _reconcile_del_flags(edit, targets, dry_run=False):
    """把待删除袋里**剧情族**的标记校正成「恰好只有 targets」。

    2026-09-22 实机定位（删→保存→重进 4/4 一致，并已在线级报文上验证）：

    1. 保存时真正上送删除的通道是 ``del_unit_uids['component']`` → 报文字段
       ``del_uid.component``。插桩 ``GK.g_unit.send_battle_event`` 抓到 evt=2006 的
       `save_change` 报文实测为 ``'del_uid': {'unit': [], 'component': [167,168,169,170]}``
       ——**与我们调用的 ``ui._del_unit()`` 无关**，它只负责本地移除 + 撤销栈。
    2. `_del_unit()` 对全局组件（剧情族）会把标记写到**存活的兄弟节点**上：
       袋子清空 → 删 A(targets=[167,168]) → 袋子变成 [169,170]（另一条存活剧情）。
       两次独立复现。于是保存后重进的结果是「**要删的 A 复活、没要删的 B 消失**」，
       即 §8.6 记录的「删除记到别的活节点头上」。

    本函数做两件事，且**只碰剧情族组件**：
      * 摘掉存活节点上的误标：``(bag ∩ 存活) - targets``
      * 给 targets 补标记：``targets - bag``（服务端删除的唯一有效通道）

    注意**不要**动 `'unit'` 桶：全部 story 共用一个单元 uid、全部 dialogue 共用另一个
    （实测 story=1000073 / dialogue=1000074），标 unit 等于整族删除。
    """
    room = _room(edit)
    if not hasattr(room, "change_del_unit_uids"):
        return {"skipped": "room 无 change_del_unit_uids"}
    targets = {int(t) for t in targets}
    live = _story_family_ids(edit)
    try:
        bag = {int(x) for x in
               (getattr(room, "del_unit_uids", {}) or {}).get("component", []) or []}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    plan = plan_del_flags(bag, live, targets)
    to_clear, to_mark = plan["to_clear"], plan["to_mark"]
    if dry_run:
        return {"dry_run": True, "bag_before": sorted(bag), "would_clear": to_clear,
                "would_mark": to_mark, "live_story_ids": len(live)}
    cleared, marked, errors = [], [], []
    for uid in to_clear:
        try:
            room.change_del_unit_uids(False, uid, "component")
            cleared.append(uid)
        except Exception as e:
            errors.append("clear %s: %s" % (uid, type(e).__name__))
    for uid in to_mark:
        try:
            room.change_del_unit_uids(True, uid, "component")
            marked.append(uid)
        except Exception as e:
            errors.append("mark %s: %s" % (uid, type(e).__name__))
    return {"bag_before": sorted(bag), "cleared_misattributed": cleared,
            "marked_targets": marked, "errors": errors,
            "live_story_ids": len(live),
            "note": "只校正剧情族组件的 'component' 桶；'unit' 桶刻意不动（story/dialogue 各共用一个单元）。"}


def op_story_delete(edit, args):
    """删除剧情节点。cascade=true 时连带子节点（剧情→对话→选项）。

    ⚠️ 删除是**两段式**的：本 op 先走原生命令层（本地移除 + 进撤销栈），再校正
    `del_unit_uids['component']` 待删除标记——**后者才是保存时真正上送删除的通道**，
    且原生删除会把标记写到别的存活节点上（见 `_reconcile_del_flags`）。
    因此对「已本地删除但没落库（重进复活）」的节点，**重复调用本 op 即可修复**：
    原生命令层会以「没有可删除的对象」拒绝，但标记校正仍会执行。
    """
    ugc_id = int(args.get("ugc_id")) if args.get("ugc_id") is not None else None
    ids = args.get("ugc_ids") or ([ugc_id] if ugc_id is not None else [])
    if not ids:
        raise ValueError("需要 ugc_id 或 ugc_ids")
    ids = [int(x) for x in ids]
    targets = list(ids)
    if args.get("cascade"):
        tree = build_tree(edit)
        for s in tree["stories"]:
            if s["ugc_id"] in ids:
                for d in s.get("dialogues", []):
                    targets.append(d["ugc_id"])
                    targets += [c["ugc_id"] for c in d.get("choices", [])]
            else:
                for d in s.get("dialogues", []):
                    if d["ugc_id"] in ids:
                        targets += [c["ugc_id"] for c in d.get("choices", [])]
    targets = sorted(set(targets))
    del_mod = importlib.import_module("fuxiang_edit_del")
    res = del_mod.op_del_objects(edit, {"ugc_ids": targets,
                                        "dry_run": bool(args.get("dry_run")),
                                        "allow_unregistered": bool(args.get("allow_unregistered"))})
    if not args.get("dry_run") and isinstance(res, dict) and res.get("ok"):
        _deleted(_room(edit)).update(targets)
    res["targets"] = targets
    res["cascade"] = bool(args.get("cascade"))
    # 校正待删除标记（即使原生命令层拒绝也要做，否则重进时节点"复活"）
    if not args.get("dry_run"):
        try:
            res["del_flags"] = _reconcile_del_flags(edit, targets)
        except Exception as exc:
            res["del_flags"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    res["note"] = ("删除走编辑器原生命令层（可 Ctrl+Z 撤销），随后校正 "
                   "del_unit_uids['component'] 待删除标记——后者才是保存时真正上送"
                   "删除的通道，不校正会出现「要删的复活、没要删的消失」。"
                   "索引残骸不会被清理，本模块的剧情树会跳过它们；持久化仍需保存。"
                   + ("" if res.get("ok") else "｜本次原生命令层未删成功（目标可能已本地删除），"
                                               "仅校正了标记。"))
    return res


# ---------------------------------------------------------------------------
# 导出文件
# ---------------------------------------------------------------------------
def _map_name(edit):
    """地图名（与 op_map_info 同源：UGCEditMapSettingPart）。"""
    try:
        from common_cs.ugc_edit_core.parts.map_setting.iUGCEditMapSettingPart import (
            iUGCEditMapSettingPart)
        p = edit.get_core().get_part(iUGCEditMapSettingPart)
        if p is not None and hasattr(p, "get_map_setting_data"):
            data = edit._safe(p.get_map_setting_data()) or {}
            return (data.get("name") or "").strip().strip('"')
    except Exception:
        pass
    return ""


def export_doc(edit):
    """生成可序列化的剧情文档（json 与 md 的共同中间层）。"""
    tree = build_tree(edit)
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "map": {"name": _map_name(edit)},
        "stories": tree["stories"],
        "orphan_dialogues": tree["orphan_dialogues"],
        "orphan_choices": tree["orphan_choices"],
    }


def to_markdown(doc):
    """把剧情文档渲染成方便人读写的 Markdown（可被 story_import 回读）。"""
    lines = ["# 剧情导出 — %s" % (doc.get("map", {}).get("name") or "未命名地图"),
             "",
             "> format: %s / version: %s / exported_at: %s" % (
                 doc.get("format"), doc.get("version"), doc.get("exported_at")),
             ""]
    if not doc.get("stories"):
        lines.append("（当前地图没有任何剧情）")
    for si, s in enumerate(doc.get("stories", []), 1):
        lines.append("## %d. 剧情：%s" % (si, s.get("name") or "(未命名)"))
        lines.append("")
        lines.append("- ugc_id: %s" % s.get("ugc_id"))
        for k, v in s.items():
            if k in ("ugc_id", "dialogues", "owner_uid", "name"):
                continue
            lines.append("- %s: %s" % (k, v))
        lines.append("")
        for di, d in enumerate(s.get("dialogues", []), 1):
            lines.append("### %d.%d 对话（ugc_id=%s, sort=%s）"
                         % (si, di, d.get("ugc_id"), d.get("sort_id")))
            lines.append("")
            for k, v in d.items():
                # content 由下面的引用块承载，避免同一句写两遍
                if k in ("ugc_id", "choices", "owner_uid", "sort_id", "content"):
                    continue
                lines.append("- %s: %s" % (k, v))
            lines.append("")
            content = d.get("content")
            if content:
                lines.append("> %s" % content)
                lines.append("")
            for c in d.get("choices", []):
                jump = ""
                if c.get("jump_dialogue") is not None:
                    jump = " → 对话 %s" % c["jump_dialogue"]
                elif c.get("jump_story") is not None:
                    jump = " → 剧情 %s" % c["jump_story"]
                lines.append("- 选项(ugc_id=%s, sort=%s)%s: %s"
                             % (c.get("ugc_id"), c.get("sort_id"), jump, c.get("content")))
                for k, v in c.items():
                    if k in ("ugc_id", "owner_uid", "sort_id", "content",
                             "jump_dialogue", "jump_story"):
                        continue
                    lines.append("    - %s: %s" % (k, v))
            lines.append("")
    for bucket, title in (("orphan_dialogues", "未挂到剧情的对话"),
                          ("orphan_choices", "未挂到对话的选项")):
        if doc.get(bucket):
            lines.append("## %s" % title)
            lines.append("")
            for n in doc[bucket]:
                lines.append("- ugc_id=%s %s" % (n.get("ugc_id"), n))
            lines.append("")
    return "\n".join(lines) + "\n"


def op_story_export(edit, args):
    """导出剧情到文件。args: path（可省，默认 projects/story/<地图>_<时间>.json）,
    format=json|md（默认按扩展名判断）。"""
    doc = export_doc(edit)
    path = _resolve_path(args.get("path"))
    fmt = args.get("format")
    if not fmt:
        fmt = "md" if str(path or "").lower().endswith(".md") else "json"
    fmt = str(fmt).lower()
    if fmt not in ("json", "md"):
        raise ValueError("format 必须是 json 或 md")
    if not path:
        name = (doc.get("map", {}).get("name") or "map").strip().strip('"')
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "map"
        os.makedirs(DEFAULT_EXPORT_DIR, exist_ok=True)
        path = os.path.join(DEFAULT_EXPORT_DIR,
                            "%s_%s.%s" % (safe, time.strftime("%Y%m%d_%H%M%S"), fmt))
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    if fmt == "md":
        text = to_markdown(doc)
    elif fmt == "json":
        text = json.dumps(doc, ensure_ascii=False, indent=2)
    else:
        raise ValueError("format 必须是 json 或 md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True, "path": path, "format": fmt, "bytes": len(text.encode("utf-8")),
            "story_count": len(doc["stories"]),
            "dialogue_count": sum(len(s.get("dialogues", [])) for s in doc["stories"]),
            "choice_count": sum(len(d.get("choices", []))
                                for s in doc["stories"] for d in s.get("dialogues", [])),
            "orphan_dialogues": len(doc["orphan_dialogues"]),
            "orphan_choices": len(doc["orphan_choices"])}


def op_story_export_doc(edit, args):
    """导出剧情但不落盘：内容以 JSON 字符串回传（快照功能用）。

    与 `op_story_export` 同源（同一份 `export_doc`），差别只在**不写文件**。
    ⛔ 返回体里的 `doc_json` 必须是字符串：直接回传 doc 会被 `_safe` 的深度
    截断（stories→dialogues→choices→fields 已超 4 层，最里层会变成 `<deep>`），
    CLI 侧 loads 回来才是完整的。
    """
    doc = export_doc(edit)
    text = json.dumps(doc, ensure_ascii=False)
    return {"ok": True,
            "doc_json": text,
            "format": doc.get("format"),
            "version": doc.get("version"),
            "map_name": (doc.get("map") or {}).get("name"),
            "story_count": len(doc["stories"]),
            "dialogue_count": sum(len(s.get("dialogues", [])) for s in doc["stories"]),
            "choice_count": sum(len(d.get("choices", []))
                                for s in doc["stories"] for d in s.get("dialogues", [])),
            "orphan_dialogues": len(doc["orphan_dialogues"]),
            "orphan_choices": len(doc["orphan_choices"])}


# ---------------------------------------------------------------------------
# 导入文件
# ---------------------------------------------------------------------------
def parse_markdown(text):
    """把 story_export 产出的 Markdown 解析回文档结构（严格按导出格式）。"""
    doc = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "map": {"name": ""},
           "stories": [], "orphan_dialogues": [], "orphan_choices": []}
    cur_story = None
    cur_dlg = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# 剧情导出"):
            continue
        if line.startswith("## "):
            body = line[3:].strip()
            if body.startswith("未挂到") or body.startswith("未挂到对话"):
                cur_story, cur_dlg = None, None
                continue
            # "1. 剧情：名字"
            name = body.split("：", 1)[1] if "：" in body else body
            cur_story = {"ugc_id": None, "name": name.strip(), "dialogues": []}
            cur_dlg = None
            doc["stories"].append(cur_story)
            continue
        if line.startswith("### "):
            if cur_story is None:
                continue
            cur_dlg = {"ugc_id": None, "sort_id": None, "choices": []}
            # "1.1 对话（ugc_id=817, sort=1）"
            head = line[4:]
            for part in head.split("（")[1].split("）")[0].split(",") if "（" in head else []:
                if "=" in part:
                    k, v = part.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "ugc_id" and v.isdigit():
                        cur_dlg["ugc_id"] = int(v)
                    elif k == "sort" and v.isdigit():
                        cur_dlg["sort_id"] = int(v)
            cur_story["dialogues"].append(cur_dlg)
            continue
        if line.startswith("> ") and cur_dlg is not None:
            cur_dlg["content"] = line[2:].strip()
            continue
        if line.startswith("- 选项(") and cur_dlg is not None:
            head, _, rest = line.partition(": ")
            meta = head[len("- 选项("):].split(")")[0]
            choice = {"ugc_id": None, "sort_id": None, "content": rest.strip()}
            for part in meta.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "ugc_id" and v.isdigit():
                        choice["ugc_id"] = int(v)
                    elif k == "sort" and v.isdigit():
                        choice["sort_id"] = int(v)
            jump = rest
            if "→ 对话 " in head or "→ 对话 " in rest:
                seg = (head + " " + rest).split("→ 对话 ", 1)[1]
                num = seg.split()[0]
                if num.isdigit():
                    choice["jump_dialogue"] = int(num)
            elif "→ 剧情 " in head or "→ 剧情 " in rest:
                seg = (head + " " + rest).split("→ 剧情 ", 1)[1]
                num = seg.split()[0]
                if num.isdigit():
                    choice["jump_story"] = int(num)
            cur_dlg["choices"].append(choice)
            continue
        if line.startswith("    - ") and cur_dlg is not None and cur_dlg["choices"]:
            # 选项的附加字段：`- k=v` / `- k：v` / `- k: v` 三种分隔符都要认
            body = line.strip()[2:]
            k = v = None
            for sep in ("=", "：", ":"):
                if sep in body:
                    k, v = body.split(sep, 1)
                    break
            if k is not None:
                cur_dlg["choices"][-1][k.strip()] = _coerce_scalar(v.strip())
            continue
        if line.startswith("- ") and ":" in line:
            k, v = line[2:].split(":", 1)
            k, v = k.strip(), v.strip()
            if cur_dlg is not None and cur_dlg.get("_reading") != "story" and k != "ugc_id":
                pass
            target = cur_dlg if cur_dlg is not None else cur_story
            if target is None:
                continue
            if k == "ugc_id":
                if v.isdigit():
                    target["ugc_id"] = int(v)
            elif k == "content":
                target["content"] = v
            elif k in ("name",):
                target["name"] = v
            else:
                target[k] = _coerce_scalar(v)
            continue
        if line.startswith("- ugc_id="):
            continue
    # Markdown 里对话的 - xxx: yyy 行在 ### 之后、> 之前，已被写入 cur_dlg；
    # 但剧情级字段（在 ## 之后、第一个 ### 之前）也走同一分支——此时 cur_dlg 为 None，
    # 因此上面的 target 选择是正确的。
    return doc


def _coerce_scalar(v):
    if v in ("None", "null", ""):
        return None
    if v.isdigit():
        return int(v)
    try:
        return float(v)
    except Exception:
        return v


def load_doc(edit, args):
    """从 path / data / text 载入剧情文档（json 或 md）。"""
    if args.get("data") is not None:
        doc = args["data"]
    elif args.get("text"):
        text = args["text"]
        doc = json.loads(text) if text.lstrip().startswith("{") else parse_markdown(text)
    else:
        path = _resolve_path(args.get("path"))
        if not path:
            raise ValueError("需要 path / data / text 之一")
        if not os.path.exists(path):
            raise ValueError("文件不存在：%s（相对路径按 %s 解析）" % (path, ROOT_DIR))
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        doc = json.loads(text) if text.lstrip().startswith("{") else parse_markdown(text)
    if not isinstance(doc, dict) or "stories" not in doc:
        raise ValueError("文件不是剧情文档（缺少 stories）")
    return doc


def _flatten(doc):
    """文档 → 扁平节点列表。

    2026-09-21 改为三段式出队（全部剧情 → 全部对话 → 全部选项）：选项的
    jump_dialogue 可以指向同剧情里**后面的**对话，必须等所有对话建完再建选项。
    选项支持 `jump_dialogue_ref`（同剧情内对话的 1-based 序号）——文件里写不出
    未来节点的 ugc_id，导入时由 story_import_next 按序号解析成真实 id。
    """
    stories, dialogues, choices = [], [], []
    for si, s in enumerate(doc.get("stories", []), 1):
        fields = {k: v for k, v in s.items()
                  if k not in ("dialogues", "ugc_id", "owner_uid")}
        stories.append({"kind": "story", "ugc_id": s.get("ugc_id"), "fields": fields,
                        "path": "story#%d" % si})
        for di, d in enumerate(s.get("dialogues", []), 1):
            df = {k: v for k, v in d.items()
                  if k not in ("choices", "ugc_id", "owner_uid")}
            dialogues.append({"kind": "dialogue", "ugc_id": d.get("ugc_id"), "fields": df,
                              "story_ugc_id": s.get("ugc_id"), "story_index": si - 1,
                              "path": "story#%d/dialogue#%d" % (si, di)})
            for ci, c in enumerate(d.get("choices", []), 1):
                cf = {k: v for k, v in c.items() if k not in ("ugc_id", "owner_uid")}
                ref = cf.pop("jump_dialogue_ref", None)   # 1-based 序号引用，导入时解析
                choices.append({"kind": "choice", "ugc_id": c.get("ugc_id"), "fields": cf,
                                "dialogue_ugc_id": d.get("ugc_id"),
                                "dialogue_index": di - 1, "story_index": si - 1,
                                "jump_dialogue_ref": (int(ref) if ref else None),
                                "path": "story#%d/dialogue#%d/choice#%d" % (si, di, ci)})
    return stories + dialogues + choices


def _resolve_jump_ref(node, nf, created):
    """把 jump_dialogue_ref（同剧情内对话 1-based 序号）解析成真实 ugc_id。"""
    ref = node.get("jump_dialogue_ref")
    if not ref:
        return None
    nf.pop("jump_dialogue_ref", None)
    target = "story#%d/dialogue#%d" % ((node.get("story_index") or 0) + 1, int(ref))
    for c in created:
        if c.get("path") == target:
            nf["jump_dialogue"] = c["ugc_id"]
            return c["ugc_id"]
    raise ValueError("jump_dialogue_ref=%s 解析失败：%s 不在本次导入中"
                     "（引用既有对话请直接写 ugc_id）" % (ref, target))


def diff_doc(edit, doc, match_by="ugc_id"):
    """比对文档与当前地图，产出 create / update / delete / unchanged 计划。"""
    tree = build_tree(edit)
    live = {}
    for s in tree["stories"]:
        live[("story", s["ugc_id"])] = s
        for d in s.get("dialogues", []):
            live[("dialogue", d["ugc_id"])] = d
            for c in d.get("choices", []):
                live[("choice", c["ugc_id"])] = c
    for d in tree["orphan_dialogues"]:
        live[("dialogue", d["ugc_id"])] = d
    for c in tree["orphan_choices"]:
        live[("choice", c["ugc_id"])] = c

    plan = {"create": [], "update": [], "unchanged": [], "delete": []}
    matched = set()
    for node in _flatten(doc):
        kind = node["kind"]
        ugc_id = node.get("ugc_id")
        key = (kind, ugc_id)
        cur = live.get(key) if ugc_id is not None else None
        if cur is None and ugc_id is not None and match_by == "name" and kind == "story":
            for (k2, uid2), v in live.items():
                if k2 == "story" and (v.get("name") or "") == (node["fields"].get("name") or ""):
                    cur = v
                    key = (k2, uid2)
                    break
        changed = {}
        if cur is None:
            plan["create"].append(node)
            continue
        matched.add(key)
        for fk, fv in node["fields"].items():
            if fv is None:
                continue
            cur_v = cur.get(fk)
            if isinstance(cur_v, int) and isinstance(fv, str) and fv.isdigit():
                fv = int(fv)
            if cur_v != fv:
                changed[fk] = fv
        if changed:
            plan["update"].append({"ugc_id": key[1], "kind": kind, "fields": changed,
                                   "path": node["path"]})
        else:
            plan["unchanged"].append({"ugc_id": key[1], "kind": kind, "path": node["path"]})
    for key, node in live.items():
        if key not in matched:
            plan["delete"].append({"ugc_id": key[1], "kind": key[0],
                                   "name": node.get("name") or node.get("content")})
    return plan


def _doc_id_to_path(doc):
    """文档里每个节点的 ugc_id → 它的 path（`story#N/dialogue#N/choice#N`）。

    跨图导入的关键：文档里存着**源图**的 ugc_id，而 `story_id` / `dialogue_id` /
    `jump_dialogue` 这些内部引用写的也是源图 id。导入到新图后 id 全变，必须按
    「旧 id → 文档 path → 本次新建的 new id」重映射，否则新节点会指向不存在的节点。
    path 由 `_flatten` 生成，只依赖文档自身的数组顺序，纯结构化。
    """
    out = {}
    for node in _flatten(doc):
        uid = node.get("ugc_id")
        if uid is not None:
            out[uid] = node["path"]
    return out


def _node_fields(edit, node, created, id_path=None):
    """节点的待写字段：过滤 None + 注入父 ugc_id（父节点是本次新建的）+ 解析跳转引用。

    `id_path` = `_doc_id_to_path(doc)`；给出时，父/跳转引用会先按
    「旧 id → path → 本次新建的 new id」重映射，映射不到则保留原值
    （同图导入时节点本就存在，原值即正确值，语义不变）。
    """
    nf = {k: v for k, v in node["fields"].items() if v is not None}
    by_path = {c["path"]: c["ugc_id"] for c in (created or [])
               if c.get("ugc_id") is not None}
    id_path = id_path or {}

    def _remap(old):
        if old is None:
            return None
        p = id_path.get(old)
        if p is None:
            return old
        return by_path.get(p, old)

    if node["kind"] == "story" and "edit_time" not in nf:
        nf["edit_time"] = int(time.time())
    if node["kind"] == "dialogue":
        sid = _remap(nf.get("story_id"))
        if sid is None:
            sid = _remap(node.get("story_ugc_id"))
        if sid is None:
            sid = by_path.get("story#%d" % ((node.get("story_index") or 0) + 1))
        if sid is not None:
            nf["story_id"] = sid
    if node["kind"] == "choice":
        did = _remap(nf.get("dialogue_id"))
        if did is None:
            did = _remap(node.get("dialogue_ugc_id"))
        if did is None:
            did = by_path.get("story#%d/dialogue#%d"
                              % ((node.get("story_index") or 0) + 1,
                                 (node.get("dialogue_index") or 0) + 1))
        if did is not None:
            nf["dialogue_id"] = did
        # jump_dialogue：文档里是源图的对话 ugc_id，跨图必须重映射
        if nf.get("jump_dialogue") is not None:
            nf["jump_dialogue"] = _remap(nf["jump_dialogue"])
        _resolve_jump_ref(node, nf, created)
    return nf


def _advance_import_queue(edit, state):
    """推进导入队列：原生建剧情时连带出的首对白直接复用，其余逐个发起创建。

    返回本次在途的创建步骤（给 story_import_next 轮询），队列为空时返回 None。
    """
    room = _room(edit)
    queue = state["queue"]
    while state["index"] < len(queue):
        node = queue[state["index"]]
        path = node["path"]
        alias = (state.get("alias") or {}).pop(path, None)
        nf = _node_fields(edit, node, state["created"], state.get("id_path"))
        if alias is not None:
            try:
                applied = _apply_fields(edit, alias, node["kind"], nf)
            except Exception as exc:
                applied = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            state["created"].append({"path": path, "kind": node["kind"],
                                     "ugc_id": alias, "fields_applied": applied})
            state["index"] += 1
            continue
        rid = _native_create_begin(edit, node["kind"], nf)
        vars(room).setdefault("_fuxiang_story_pending_fields", {})[rid] = {
            "kind": node["kind"], "fields": nf}
        state["request_id"] = rid
        return {"request_id": rid, "index": state["index"], "total": len(queue),
                "kind": node["kind"], "path": path, "status": "pending"}
    state["request_id"] = None
    return None


def op_story_import(edit, args):
    """把剧情文件比对/应用到当前地图。

    dry_run（默认 true）只给计划；dry_run=false 会**立即**执行 update，
    并按 prune 执行 delete，然后**发起第一个 create**（异步，需 story_import_next 推进）。
    """
    doc = load_doc(edit, args)
    plan = diff_doc(edit, doc, match_by=args.get("match_by") or "ugc_id")
    dry = bool(args.get("dry_run", True))
    result = {"ok": True, "dry_run": dry, "plan": {
        "create": [{"kind": n["kind"], "path": n["path"],
                    "fields": {k: v for k, v in n["fields"].items() if v is not None}}
                   for n in plan["create"]],
        "update": plan["update"],
        "delete": plan["delete"],
        "unchanged": plan["unchanged"]},
        "counts": {k: len(v) for k, v in plan.items()}}
    if dry:
        result["note"] = "dry_run：未写入任何数据。确认后加 dry_run=false 执行。"
        return result

    applied, failed = [], []
    for up in plan["update"]:
        try:
            r = _apply_fields(edit, up["ugc_id"], up["kind"], up["fields"])
            if r.get("ok") is False:
                failed.append({"ugc_id": up["ugc_id"], "error": r.get("error")})
            else:
                applied.append(up)
        except Exception as exc:
            failed.append({"ugc_id": up["ugc_id"], "error": "%s: %s" % (type(exc).__name__, exc)})
    result["updated"] = applied
    result["update_failed"] = failed

    deleted = []
    if args.get("prune") and plan["delete"]:
        try:
            r = op_story_delete(edit, {"ugc_ids": [d["ugc_id"] for d in plan["delete"]],
                                       "dry_run": False})
            deleted.append({"count": len(plan["delete"]), "result": r.get("ok", r)})
        except Exception as exc:
            failed.append({"delete": "%s: %s" % (type(exc).__name__, exc)})
    result["deleted"] = deleted

    # 创建必须串成 begin/poll 链（游戏主线程内不能等待），这里只推进到第一个在途请求
    room = _room(edit)
    state = {"queue": list(plan["create"]), "index": 0, "created": [],
             "alias": {}, "request_id": None, "started_at": time.strftime("%H:%M:%S"),
             # 旧 ugc_id → 文档 path：跨图导入时把内部引用（story_id/dialogue_id/
             # jump_dialogue）重映射到本次新建的节点，否则会指向源图的旧 id
             "id_path": _doc_id_to_path(doc)}
    vars(room)["_fuxiang_story_import"] = state
    step = _advance_import_queue(edit, state)
    result["create_step"] = step
    result["done"] = step is None
    result["note"] = ("创建是异步的：每发起一个后调用 story_import_next 推进，"
                      "全部完成后记得在游戏内保存。")
    return result


def _story_id_map(state):
    """老 ugc_id → 本次新建 ugc_id（供还原侧重绑事件引用用）。

    建法与 `_node_fields` 的重映射同源：旧 id → 文档 path → created 里的 new id。
    """
    id_path = state.get("id_path") or {}
    by_path = {c["path"]: c["ugc_id"] for c in (state.get("created") or [])
               if c.get("ugc_id") is not None}
    out = {}
    for old, p in id_path.items():
        new = by_path.get(p)
        if new is not None and new != old:
            out[old] = new
    return out


def op_story_import_next(edit, args):
    """推进导入：查询当前创建请求 → placed 则落字段 → 发起下一个。"""
    room = _room(edit)
    state = vars(room).get("_fuxiang_story_import")
    if not state or not state.get("queue"):
        return {"done": True, "note": "没有进行中的导入"}
    rid = state.get("request_id")
    if not rid:
        step = _advance_import_queue(edit, state)
        return {"index": state["index"], "total": len(state["queue"]),
                "done": step is None, "next": step,
                "created": state["created"],
                "id_map": _story_id_map(state)}
    res = _native_create_poll(edit, rid)
    status = res.get("status")
    out = {"index": state["index"], "total": len(state["queue"]),
           "request_id": rid, "status": status, "done": False,
           "created": state["created"], "id_map": _story_id_map(state)}
    if status == "pending":
        out["note"] = "仍在等待服务端下发，稍后再查（不要重发）"
        return out
    if status != "placed":
        out["error"] = "创建未成功：%s %s" % (status, res.get("error") or "")
        out["done"] = True
        return out

    node = state["queue"][state["index"]]
    ugc_id = res.get("ugc_id")
    pending = vars(room).get("_fuxiang_story_pending_fields") or {}
    spec = pending.pop(rid, None)
    fields = dict(spec.get("fields") or {}) if spec else {}
    if fields:
        try:
            out["fields_applied"] = _apply_fields(edit, ugc_id, node["kind"], fields)
        except Exception as exc:
            out["fields_error"] = "%s: %s" % (type(exc).__name__, exc)
    out["pending_delete_cleared"] = res.get("pending_delete_cleared")
    state["created"].append({"path": node["path"], "kind": node["kind"], "ugc_id": ugc_id})
    if node["kind"] == "story" and res.get("auto_dialogue_ugc_id"):
        # 原生「新建剧情」连带建一条首对白：留给文件的 dialogue#1 复用，避免多出空节点
        state.setdefault("alias", {})["%s/dialogue#1" % node["path"]] = \
            res["auto_dialogue_ugc_id"]
    state["index"] += 1

    step = _advance_import_queue(edit, state)
    out["next"] = step
    out["done"] = step is None
    # 回传时以**最新**的 created 重算（上面那两份是推进前的快照）
    out["created"] = state["created"]
    out["id_map"] = {str(k): v for k, v in _story_id_map(state).items()}
    if out["done"]:
        out["note"] = ("全部创建完成；id_map 为「源图 ugc_id → 本图新 ugc_id」，"
                       "可用于重绑指向剧情节点的事件引用。请在游戏内保存后重进核实持久化。")
    return out
