# -*- coding: utf-8 -*-
"""随笔浮想「界面编辑器」编辑层 —— 定位 / 新增控件 / 绑事件 / 删除 / 落库自检。

界面编辑器是什么
----------------
它不是场景编辑器的一个面板，而是**另一套视图**：``UIBattleUGCCustomUI``
（模块 ``ui.UIBattleUGCPart.UIBattleUGCCustomUIPart``，实例挂在 ``GK.g_ui_mgr`` 的
``ui_dict`` 里，owner 是 ``UIBattleUGCEdit``）。19 个子 part 的分工见 ``UI_PARTS_CN``。
取实例只能走 ``vars(g_ui_mgr)['ui_dict']``：``g_ui_mgr.get_ui(name)`` 对未注册的
名字返回 ``Swallow`` 哨兵（**不是 None**），拿到会当成"实例存在"而写错对象。

控件的数据模型
--------------
放置出来的控件 = 组件类型 **200000 UI_CUSTOM**（类 ``UGCUiCustomComponent``）。
⚠️ **全图界面控件共用同一个 ``UgcGlobalUnit``**（2026-09-23 实机：30 件控件、跨 27 个分类、
几十个不同 item_id，全挂在 uid 1000313 上，``unit.is_single_component`` 仍写着 True）。
⇒ 控件"是哪件"**只能问** ``room.get_component_item_id(ugc_id)``；``unit.ugc_type`` 只是
第一件的 item_id，拿它当身份会把所有控件都读成同一件（本模块曾因此误判，见 UI_EDIT.md §9）。
各自那份袋进报文靠的是保存通道逐件切换：``_get_change_uid`` 对
``change_units['component']`` 里的每个 ugc_id 先 ``ui.change_component_id(ugc_id)``
再读 ``_get_ugc_info(unit)['params']`` —— 而"报文里的那份"就等于**该组件事件袋里 info 非空
的项**（``_save_shape`` 直接这么算，不动编辑器状态）；要照抄那次切换做深度核对用
``_mirror_shape``（``ui_verify`` 的 ``with_mirror_probe``；它会临时改"当前主组件"的指向）。
另有 **200001 UI_NATIVE**（技能/操作位，实测一张图 48 个、共用 unit 1000057）——
那是原生交互按钮，不在界面编辑器的画布上摆放，本模块只列不改。

⚠️ 控件单元**不在** ``ui.all_ugc_uid_set`` 里（那是场景物件的登记集合），
所以任何写入都要 ``allow_unregistered=true``，否则被数值层的护栏挡下。

属性袋：``comp.component_info['events'] = [{'type': <property_id>, 'info': [...]}, ...]``
  property_id → 语义 由组件类的两张表推出：
    ``ITEM_PARAM_2_EVENT_TYPE``   property_id → UiCustomEventType（1=POS, 7=DOWN_SEND…）
    ``EVENT_TYPE_2_METHOD_NAME``  event_type   → 应用方法（``_set_ui_pos`` / ``_down_send``…）
  事件类槽位（``property_type`` 以 80001 开头，来自 ``GK.DM.ugc_item_property``）：
    20000016605 点击(sender) / 16606 松手 / 16607 长按 —— info = [事件名, 1286]
    20000016603 使控件显示(receiver) / 16604 使控件隐藏 —— info = [事件名]
  ⛔ 第二元素 **1286 不是序号**，而是字段定义里的默认值（``event_field_shape`` 的
  ``default_info=['', 1286]``）。数值层 ``op_event_bind`` 本就按该形态写，别手填。

新增控件的原生链路（本模块照抄，不发明）
------------------------------------------
调色板拖放落地 ``UIBattleUGCRightComponentView._handle_2d_end``：
  1. 服务端 ``ui._add_unit_server([item_id])`` 建件 —— 客户端收到后先挂**待确认态**
     （``room.del_unit_uids['component']`` 里有它的 ugc_id）；
  2. ``ui.on_check_ui_component_limit([comp], end_pos, update_params)``：
     ``end_pos`` 只用 ``.x`` / ``.y``（画布 2D 坐标，设计尺寸 1334×750，原点左下）；
     内部跑 ``ui_component_detector_chain``（占用值校验，可能弹窗），通过后
     ``_get_create_ui_components_commands`` 产出
     ``UGCUIObjCommand(view='UIBattleUGCCustomUI') + UGCObjChangeCommand(type=True)
     + UGCOccupancyChangeCommand(type=True)`` 并 ``_add_change_unit(unit.uid)``，
     最后经 ``iUIBattleUGCCustomUICommandControl.do_command`` 进撤销栈与保存变更集。
  ⚠️ ``_get_create_ui_components_commands`` / ``_get_del_ui_components_commands`` 在
  ``ui._get_is_show_custom_ui_view()`` 为假时**直接返回空命令**：界面编辑器没打开时
  "调了成功、什么都没发生"。故所有写操作先检查该开关，不满足就明确报错。
  ⚠️ 该链路的确认代码在 ``bridge/fuxiang_placement.py::_confirm_ui_component``
  （摆放状态机在 place_poll 里按组件类型分流），本模块只装配参数并回读校验。

删除走 ``ui.on_del_ui_component([ugc_id], last_update_params)``；真正上送的删除
通道仍是 ``room.del_unit_uids['component']``（见 README「删除」节）。

持久化判据
----------
``ui._get_ugc_info(unit, is_save=True)`` 就是保存报文里这个控件的样子（前 3 项是
-999 占位的变换三元组，之后才是属性袋）。``ui_edit_verify`` 用它 + 变更集 +
事件注册表三方对照，**但真判据仍是保存后重进**，故所有写接口一律返回
``persistence_verified: false``。
"""
import math

UI_CUSTOM_TYPE = 200000
UI_NATIVE_TYPE = 200001
UI_VIEW_NAME = "UIBattleUGCCustomUI"
# 画布设计尺寸（运行时从 p_area 读，读不到才用它兜底，只影响默认落点提示）
CANVAS_FALLBACK_SIZE = (1334.0, 750.0)
DEFAULT_POS_RATIO = (0.5, 0.5)          # 未给 pos 时落在画布中心

# 语义别名 → (property_id, 期望的应用方法)。写接口两种标识都收；
# 真实可用性由 _alias_map() 在运行时按引擎表校验，漂移即报错而不是静默写错。
PROP_ALIAS = {
    "pos": (20000010001, "_set_ui_pos"),
    "rot": (20000010004, "_set_ui_rot"),
    "height": (20000010007, "_set_ui_height"),
    "width": (20000010008, "_set_ui_width"),
    "alpha": (20000010009, "_set_ui_alpha"),
    "name": (20000016501, "_set_skill_name"),
    "visible": (20000016502, "_set_init_ui_visible"),
    "show_object_type": (20000016503, "_set_show_object_type"),
    "show_camp": (20000016504, "_set_show_camp"),
    "show_class": (20000016505, "_set_show_class"),
    "add_color": (20000016506, "_set_add_color"),
    "ui_color": (20000016507, "_set_ui_color"),
    "only_send_ui": (20000016601, "_set_only_send_ui"),
    "interaction_layer": (20000016602, "_set_interaction_layer"),
    "on_show": (20000016603, "_show_node"),
    "on_hide": (20000016604, "_hide_node"),
    "on_click": (20000016605, "_down_send"),
    "on_release": (20000016606, "_up_send"),
    "on_long_press": (20000016607, "_press_send"),
    "press_time": (20000016608, "_press_send_time"),
    "press_enable": (20000016609, "_set_press_enable"),
    "bind_anchor": (20000016809, "_set_bind_anchor"),
    "content": (20000017001, "_set_content"),
    "font_type": (20000017002, "_set_font_type"),
    "font_size": (20000017003, "_set_font_size"),
    "font_color": (20000017004, "_set_font_color"),
    "font_h_align": (20000017005, "_set_font_horizontal_align"),
    "font_v_align": (20000017006, "_set_font_vertical_align"),
}
# 事件类槽位：值里带事件名，必须先在事件注册表里存在 ⇒ 只能走 ui_edit_bind
EVENT_SLOTS = ("on_show", "on_hide", "on_click", "on_release", "on_long_press")

# 界面编辑器 19 个子 part 的分工（由类名 + 实测调用链整理）
UI_PARTS_CN = {
    "UIBattleUGCCustomUIDataPart": "数据层：选中集合 / 临时选中 / 临时删除 / 隐藏件显示开关",
    "UIBattleUGCCustomUICommonLogicPart": "公共逻辑：控件 2D 节点的创建（create_node）",
    "UIBattleUGCCustomUIMainPart": "画布主体",
    "UIBattleUGCCustomUIWindowPart": "画布窗口/视口",
    "UIBattleUGCCustomUIOptionHandlePart": "拖拽·缩放·旋转操作手柄（含缩放/吸附参数、p_area 画布）",
    "UIBattleUGCCustomUISelectBoxPart": "框选与复制（_on_click_btn_copy_end → add_copy_unit_ui_custom）",
    "UIBattleUGCCustomUILeftTopPart": "左上工具条：返回/设置/保存/撤销/替换/原始",
    "UIBattleUGCCustomUILeftBottomPart": "左下区",
    "UIBattleUGCCustomUILeftSwitchPart": "左侧页签切换（模型开关）",
    "UIBattleUGCCustomUIBottomPart": "底部条：删除按钮（_on_click_del_node → on_del_ui_component）",
    "UIBattleUGCCustomUIRightComponentPart": "右侧「控件」页：可放置控件目录",
    "UIBattleUGCCustomUIRightPropertyPart": "右侧「属性」页：位置/尺寸/透明度…",
    "UIBattleUGCCustomUIRightMorePropertyPart": "右侧「更多属性」页",
    "UIBattleUGCCustomUIRightEventPart": "右侧「事件」页：点击/松手/长按绑定（bind_master/bind_params）",
    "UIBattleUGCCustomUIRightColorPart": "右侧「颜色」页",
    "UIBattleUGCCustomUIRightAnchorPart": "右侧「锚点」页（挂到原生控件）",
    "UIBattleUGCCustomUIRightEnterBtnPart": "右侧「进入按钮」页",
    "UIBattleUGCCustomUIRightDeletePart": "右侧「删除区」：拖到此处删除（node_delete）",
    "UIBattleUGCCustomUIRightViewMgrPart": "右侧页签管理器（视图切换）",
}


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def _ops(edit):
    return edit.smart_reload("fuxiang_edit_ops")


def _scene(edit):
    return edit.smart_reload("fuxiang_scene")


def _catalog(edit):
    return edit.smart_reload("fuxiang_catalog")


def _room(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != "WorldBattleUGCEdit":
        raise ValueError("当前不在随笔浮想编辑器")
    return room


def _edit_ui(edit):
    ui = _scene(edit).get_edit_ui(edit)
    if ui is None:
        raise ValueError("编辑器主 UI（UIBattleUGCEdit）不可用")
    return ui


def get_custom_ui(edit):
    """取界面编辑器实例 UIBattleUGCCustomUI；没打开时返回 None。"""
    try:
        gk = _room(edit).create_unit_with_info.__globals__["GK"]
    except Exception:
        return None
    gui = getattr(gk, "g_ui_mgr", None)
    ud = vars(gui).get("ui_dict") or {} if gui is not None else {}
    cus = ud.get(UI_VIEW_NAME)
    return cus if type(cus).__name__ == UI_VIEW_NAME else None


def _view_open(ui):
    """界面编辑器是否在前视图 —— 关着时原生的创建/删除命令会静默返回空。"""
    try:
        return bool(ui._get_is_show_custom_ui_view())
    except Exception:
        return False


def _require_view_open(ui):
    if _view_open(ui):
        return None
    return {"ok": False, "error": "界面编辑器当前没在前视图"
                                  "（ui._get_is_show_custom_ui_view() 为假）",
            "note": "原生 _get_create_ui_components_commands / _get_del_ui_components_commands "
                    "在该开关为假时直接返回空命令：调了也不落库。"
                    "请先在游戏内切到界面编辑器（或用 ui_edit_state 看 view_open）再重试。"}


# ---------------------------------------------------------------------------
# 控件枚举 / 属性表
# ---------------------------------------------------------------------------
def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _plain(v, depth=0):
    if depth > 4:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x, depth + 1) for x in list(v)[:40]]
    if hasattr(v, "keys"):
        return {str(k): _plain(v[k], depth + 1) for k in list(v.keys())[:40]}
    return repr(v)[:80]


def _type_ids(cm, ctype):
    bag = getattr(cm, "_component_type2ugc_ids", None) or {}
    ids = bag.get(ctype)
    if ids is None:
        try:
            ids = bag.get(str(ctype))
        except Exception:
            ids = None
    return sorted(x for x in (_as_int(i) for i in (ids or [])) if x is not None)


def widgets(edit, ctype=UI_CUSTOM_TYPE):
    """按组件类型取控件 [(ugc_id, comp, unit)]，跳过实体已销毁的。"""
    cm = edit.get_component_manager()
    out = []
    for gid in _type_ids(cm, ctype):
        comp = cm._ugc_id2component.get(gid) or cm._ugc_id2component.get(str(gid))
        unit = getattr(comp, "_owner", None) if comp is not None else None
        if unit is None:
            continue
        out.append((gid, comp, unit))
    return out


def component_type(comp):
    for base in type(comp).__mro__:
        ct = getattr(base, "COMPONENT_TYPE", None)
        if ct is not None:
            return _as_int(ct)
    return None


def _prop_table(edit, comp=None):
    """UI_CUSTOM 的运行时属性字典：str(property_id) -> 语义与形态。

    真源全在引擎侧：``GK.DM.ugc_item_property[200000]``（property_type / params）
    + 组件类的 ``ITEM_PARAM_2_EVENT_TYPE`` / ``EVENT_TYPE_2_METHOD_NAME``
    + ``UiCustomEventType`` 枚举名。``label`` / ``direction`` / ``default_info``
    只有事件类字段有，取自 ``_ops.event_field_shape``（需要一个活控件；地图上一个控件
    都没有时为 None，不影响写接口）。
    """
    ops = _ops(edit)
    schema = ops.property_schema(edit, UI_CUSTOM_TYPE) or {}
    p2e, e2m = {}, {}
    cm = edit.get_component_manager()
    cls = (getattr(cm, "_component_factory", None) or {}).get(UI_CUSTOM_TYPE)
    for klass in (type(comp),) if comp is not None else ((cls,) if cls else ()):
        for base in getattr(klass, "__mro__", ()):
            for attr, tgt in (("ITEM_PARAM_2_EVENT_TYPE", p2e),
                              ("EVENT_TYPE_2_METHOD_NAME", e2m)):
                d = getattr(base, attr, None)
                if isinstance(d, dict):
                    for k, v in d.items():
                        tgt.setdefault(str(k), v)
    et_names = {}
    try:
        mod = __import__("common_cs.combat_core.components.unit_ugc.UGCUiCustomComponent",
                         fromlist=["UiCustomEventType"])
        enum = getattr(mod, "UiCustomEventType", None)
        for k in dir(enum) if enum is not None else ():
            if k.startswith("_"):
                continue
            member = getattr(enum, k, None)
            # 这个枚举不是 enum.Enum，成员就是裸 int：.value 取不到时按 int 本身算
            value = getattr(member, "value", member if isinstance(member, int) else None)
            if isinstance(value, int) and not isinstance(value, bool):
                et_names[k] = value
    except Exception:
        pass
    rows = {}
    for pid, defn in schema.items():
        key = str(pid)
        et = p2e.get(key)
        if isinstance(et, list):        # 有的组件类把 event_type 定义成 list，不可哈希
            et = et[0] if et else None
        rows[key] = {
            "property_id": _as_int(pid) if _as_int(pid) is not None else pid,
            "event_type": et,
            "event_name": next((n for n, v in et_names.items() if v == et), None),
            "setter": e2m.get(str(et)) if et is not None else None,
            "property_type": (defn or {}).get("property_type"),
            "property_params": (defn or {}).get("property_params"),
            "tab_id": (defn or {}).get("tab_id"),
            "label": None, "direction": None, "key_index": None, "default_info": None,
        }
    if comp is not None:
        for pid, row in rows.items():
            if not str(row.get("property_type") or "").startswith("80001"):
                continue
            try:
                shape = ops.event_field_shape(comp, int(pid))
            except Exception:
                continue
            row.update({"label": shape.get("label"), "direction": shape.get("direction"),
                        "key_index": shape.get("key_index"),
                        "default_info": _plain(shape.get("default_info"))})
    return rows


def _alias_map(edit, comp=None):
    """别名 → 属性行；并用运行时表校验别名映射（版本变了会明确报错，不静默写错）。"""
    table = _prop_table(edit, comp)
    out = {}
    for alias, (pid, setter) in PROP_ALIAS.items():
        row = table.get(str(pid))
        if row is None:
            raise ValueError("别名 %s 的 property_id=%s 不在 UI_CUSTOM 属性表里 —— "
                             "客户端字段变了，请重新核对 PROP_ALIAS" % (alias, pid))
        if row.get("setter") != setter:
            raise ValueError("别名 %s（property_id=%s）期望应用方法 %s，运行时是 %s —— "
                             "语义变了，请重新核对 PROP_ALIAS"
                             % (alias, pid, setter, row.get("setter")))
        out[alias] = dict(row, alias=alias)
    return out


def _by_pid(aliases):
    return {str(row["property_id"]): row for row in aliases.values()}


def _resolve_prop(aliases, key):
    """把 pos / on_click / 20000016605 / "20000016605" 统一成 int property_id。"""
    if key in aliases:
        return int(aliases[key]["property_id"])
    pid = _as_int(key)
    if pid is None:
        raise ValueError("无法识别的属性名/property_id：%r（可用别名见 ui_edit_schema）" % (key,))
    return pid


def _bag(comp):
    """组件属性袋 → {str(property_id): info}（保序）。"""
    ci = getattr(comp, "component_info", None)
    out = {}
    for e in ((ci or {}).get("events") or []):
        if hasattr(e, "get"):
            out[str(e.get("type"))] = _plain(e.get("info"))
    return out


def _item_names(edit, item_ids):
    """item_id -> 中文名/分类（全量目录有模块级缓存，命中即免费）。"""
    try:
        items = (_catalog(edit).build(edit) or {}).get("items") or {}
    except Exception:
        return {}
    out = {}
    for iid in item_ids:
        rec = items.get(iid) or items.get(str(iid)) or {}
        out[iid] = {"name_cn": rec.get("name_cn"), "item_tid": rec.get("item_tid"),
                    "category_id": rec.get("category_id"),
                    "category_cn": rec.get("category_cn"),
                    "category_group_cn": rec.get("category_group_cn"),
                    "occupancy_value": _plain(rec.get("occupancy_value"))}
    return out


def _save_shape(comp):
    """该控件进保存报文的属性段（**无副作用**）。

    `ui._get_change_uid()` 对 `change_units['component']` 里每个 ugc_id 先
    `ui.change_component_id(ugc_id)` 再读 `_get_ugc_info(unit, is_save=True)['params']`：
    同 item_id 的控件实测**共用一个** UgcGlobalUnit（按钮 362/363 都在 uid 1000313 上，
    `unit.is_single_component` 仍写着 True），各自的数据全靠那次切换进报文。
    而镜像的属性段 = 该组件事件袋里 **info 非空** 的那几项（2026-09-23 实机逐件比对
    一致，见 UI_EDIT.md §9）⇒ 这里直接从袋里算，不动编辑器状态。
    """
    out = []
    for e in ((getattr(comp, "component_info", None) or {}).get("events") or []):
        if not hasattr(e, "get"):
            continue
        info = _plain(e.get("info"))
        if info is not None:
            out.append({"type": e.get("type"), "info": info})
    return out


def _mirror_shape(ui, comp, unit):
    """照抄保存通道的"切换后读镜像"写法，供 ui_verify 的深度核对用。

    ⚠️ 它会改编辑器"当前主组件"的指向（实测**不**登记进 change_units：2026-09-23 探针
    `change_units_length` 前后都是 0），但仍是全局状态，故默认不调，只在
    `with_mirror_probe=true` 时各读一次，用来证明"镜像里那份确实是本控件的袋"。
    """
    cur_id = getattr(getattr(unit, "ugc_component", None), "_ugc_id", None)
    switch = cur_id is not None and str(cur_id) != str(comp._ugc_id)
    try:
        if switch:
            ui.change_component_id(comp._ugc_id)
        info = ui._get_ugc_info(unit, is_save=True)
    except Exception:
        return None
    finally:
        if switch:
            try:
                ui.change_component_id(cur_id)
            except Exception:
                pass
    params = (info or {}).get("params") or []
    return [{"type": e.get("type"), "info": _plain(e.get("info"))}
            for e in params if hasattr(e, "get")][3:]



def _has(container, value):
    try:
        return any(str(x) == str(value) for x in (container or []))
    except Exception:
        return False


def _reg_state(ui, room, comp, unit):
    """控件在保存变更集 / 待删除袋 / 场景登记集合里的位置。"""
    up = getattr(ui, "ugc_params", None) or {}
    cu = up.get("change_units") or {}
    delbag = getattr(room, "del_unit_uids", None) or {}
    return {
        "in_change_unit": _has(cu.get("unit"), unit.uid),
        "in_change_component": _has(cu.get("component"), comp._ugc_id),
        "pending_delete_component": _has(delbag.get("component"), comp._ugc_id),
        "pending_delete_unit": _has(delbag.get("unit"), unit.uid),
        # 场景物件的登记集合；控件本来就不在里面（不是异常），故只作信息给出
        "in_all_ugc_uid_set": _has(getattr(ui, "all_ugc_uid_set", set()), unit.uid),
    }


def _real_item_id(room, gid, comp):
    """控件真实是哪件：``room.get_component_item_id(ugc_id)``。

    ⚠️ **不要**用 ``unit.ugc_type`` —— 全图界面控件共用一个 UgcGlobalUnit，那个字段只是
    **第一件**的 item_id（2026-09-23 实机：5101000 建出来的两件，单元的 ugc_type 仍是
    5201002；get_component_item_id 才分别给出 5101000 / 5101038）。
    """
    fn = getattr(room, "get_component_item_id", None)
    try:
        if callable(fn):
            got = _as_int(fn(gid))
            if got is not None:
                return got
    except Exception:
        pass
    return _as_int(getattr(getattr(comp, "_owner", None), "ugc_type", None))


def _widget_record(ui, room, gid, comp, unit, names, aliases, shares=None):
    bag = _bag(comp)
    by_pid = _by_pid(aliases)
    props = {}
    for pid, info in bag.items():
        if info is None:
            continue
        row = by_pid.get(str(pid))
        props[(row or {}).get("alias", pid)] = info
    events = {}
    for alias in EVENT_SLOTS:
        row = aliases.get(alias) or {}
        info = bag.get(str(row.get("property_id")))
        if isinstance(info, list) and any(x not in (None, "") for x in info):
            events[alias] = {"event_key": info[0], "info": info,
                             "direction": row.get("direction"), "label": row.get("label")}
    main_id = getattr(getattr(unit, "ugc_component", None), "_ugc_id", None)
    item_id = _real_item_id(room, gid, comp)
    named = names.get(item_id) or {}
    return {
        "ugc_id": gid,
        "owner_uid": getattr(unit, "uid", None),
        "unit_class": type(unit).__name__,
        # 全图控件共用一个单元（实测）；落库靠 change_component_id 逐件切换
        "same_unit_widgets": sorted(g for g in (shares or {}).get(id(unit), []) if g != gid),
        "is_unit_main_component": str(main_id) == str(gid),
        "component_class": type(comp).__name__,
        "component_type": UI_CUSTOM_TYPE,
        "item_id": item_id,
        # 单元上那个 ugc_type 只是第一件的 item_id，保留是为了看清"为什么不能用它"
        "unit_ugc_type": getattr(unit, "ugc_type", None),
        "item_name_cn": named.get("name_cn"),
        "category_cn": named.get("category_cn"),
        "category_group_cn": named.get("category_group_cn"),
        "properties": props,
        "events": events,
        "registration": _reg_state(ui, room, comp, unit),
        "in_save_payload": bool(_save_shape(comp)),
    }


def _unit_shares(edit):
    """{id(unit): [该单元上的界面控件 ugc_id]} —— 一次算好，供清单逐件引用。"""
    out = {}
    for gid, _comp, unit in widgets(edit):
        out.setdefault(id(unit), []).append(gid)
    return out


def _canvas_size(edit):
    cus = get_custom_ui(edit)
    hp = (getattr(cus, "sub_part_dict", None) or {}).get(
        "UIBattleUGCCustomUIOptionHandlePart") if cus is not None else None
    for attr in ("p_area", "p_area_new"):
        try:
            cs = getattr(hp, attr).getContentSize()
            return [float(cs.width), float(cs.height)]
        except Exception:
            continue
    return list(CANVAS_FALLBACK_SIZE)


def _find_item(edit, query):
    """按中文名 / item_tid / 数字 id 在目录里精确匹配一个界面控件。"""
    if query is None:
        return None
    s = str(query).strip()
    if s.isdigit():
        return int(s)
    items = (_catalog(edit).build(edit) or {}).get("items") or {}
    hits = [iid for iid, rec in items.items()
            if rec.get("component_type") == UI_CUSTOM_TYPE
            and s in (rec.get("name_cn"), rec.get("item_tid"))]
    return hits[0] if len(hits) == 1 else None


def _refresh_canvas(ui):
    """改完属性/结构后让画布重读一遍。取不到刷新接口不算失败（数据已写对）。"""
    for name in ("refresh_ui_custom", "refresh_ui_custom_view"):
        fn = getattr(ui, name, None)
        if callable(fn):
            try:
                fn()
                return {"by": name, "ok": True}
            except Exception as exc:
                return {"by": name, "ok": False,
                        "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"by": None, "ok": False, "error": "没有可用的 refresh_ui_custom*"}


# ---------------------------------------------------------------------------
# 只读 ops
# ---------------------------------------------------------------------------
def op_ui_state(edit, args):
    """界面编辑器定位与概览：视图实例 / 子 part 数 / 控件计数 / 画布尺寸 / 事件表。"""
    ui = _edit_ui(edit)
    cm = edit.get_component_manager()
    cus = get_custom_ui(edit)
    out = {"ok": True, "view_name": UI_VIEW_NAME,
           "view_open": _view_open(ui),
           "view_instance": type(cus).__name__ if cus is not None else None,
           "sub_part_count": len((getattr(cus, "sub_part_dict", None) or {}))
                             if cus is not None else 0,
           "canvas_design_size": _canvas_size(edit),
           "ui_custom_count": len(_type_ids(cm, UI_CUSTOM_TYPE)),
           "ui_native_count": len(_type_ids(cm, UI_NATIVE_TYPE)),
           "widget_unit_model": "全图界面控件共用同一个 UgcGlobalUnit；是哪件要看 "
                                "room.get_component_item_id(ugc_id)，unit.ugc_type 只是第一件的",
           "widget_registration_note": "控件单元不在 all_ugc_uid_set（写操作需 "
                                       "allow_unregistered，本模块已内置）"}
    try:
        out["component_nums"] = ui.get_all_ugc_component_nums()
        out["occupancy"] = ui.get_current_occupancy()
        out["max_item_num"] = (getattr(ui, "ugc_params", None) or {}).get("max_item_num")
    except Exception:
        pass
    try:
        reg = _ops(edit)._event_registry(edit)
        out["events"] = sorted(str(k) for k in reg.keys()
                               if (reg[k] or {}).get("info") is not None)
    except Exception:
        out["events"] = None
    out["note"] = ("控件=组件类型 200000(UI_CUSTOM)，全图控件共用一个 UgcGlobalUnit，"
                   "是哪件要问 room.get_component_item_id；"
                   "200001(UI_NATIVE) 是原生技能/操作位，不在画布上摆放。"
                   "写操作要求 view_open=true；落库判据是保存后重进，详见 UI_EDIT.md。")
    return out


def op_ui_parts(edit, args):
    """界面编辑器各子 part 的定位：类名 / 分工 / 字段 / 关键方法。"""
    cus = get_custom_ui(edit)
    if cus is None:
        return {"ok": False, "error": "界面编辑器未打开：拿不到 %s 实例" % UI_VIEW_NAME}
    rows = []
    for name, part in list((getattr(cus, "sub_part_dict", None) or {}).items()):
        methods = [a for a in dir(part) if not a.startswith("__")
                   and callable(getattr(part, a, None))]
        rows.append({"part": str(name),
                     "role": UI_PARTS_CN.get(str(name), "（未归类）"),
                     "class": type(part).__name__,
                     "fields": sorted(str(k) for k in vars(part).keys())[:40],
                     "methods": [m for m in methods if any(
                         t in m.lower() for t in ("create", "add", "del", "event", "bind",
                                                  "node", "select", "refresh", "view"))][:25]})
    rows.sort(key=lambda r: r["part"])
    return {"ok": True, "view": type(cus).__name__, "count": len(rows),
            "owner": type(getattr(cus, "owner", None)).__name__, "parts": rows,
            "note": "owner 是场景编辑器主 UI（UIBattleUGCEdit）；"
                    "事件绑定页 = UIBattleUGCCustomUIRightEventPart（bind_master/bind_params）。"}


def op_ui_catalog(edit, args):
    """界面编辑器可放置的控件目录（组件类型 200000 的全部物件）。"""
    limit = args.get("limit")
    limit = 40 if limit is None else int(limit)
    query = str(args.get("query") or "").strip().lower()
    cat = _as_int(args.get("category_id"))
    items = (_catalog(edit).build(edit) or {}).get("items") or {}
    rows = []
    for iid in sorted(items):
        rec = items[iid]
        if rec.get("component_type") != UI_CUSTOM_TYPE:
            continue
        if cat is not None and rec.get("category_id") != cat:
            continue
        if query:
            blob = "%s %s %s %s" % (iid, rec.get("name_cn") or "",
                                    rec.get("item_tid") or "", rec.get("category_cn") or "")
            if query not in blob.lower():
                continue
        rows.append({"item_id": iid, "name_cn": rec.get("name_cn"),
                     "category_id": rec.get("category_id"),
                     "category_cn": rec.get("category_cn"),
                     "category_group_cn": rec.get("category_group_cn"),
                     "occupancy_value": _plain(rec.get("occupancy_value")),
                     "num_limit": _plain(rec.get("num_limit")),
                     "img_path": rec.get("img_path"),
                     "catalog_hidden": bool(rec.get("catalog_hidden"))})
    total = len(rows)
    return {"ok": True, "component_type": UI_CUSTOM_TYPE, "total": total,
            "returned": total if limit <= 0 else min(limit, total),
            "items": rows if limit <= 0 else rows[:limit],
            "note": "limit=0 取全部；分组见 category_group_cn（图片/按钮/功能/角色）。"
                    "这些正是场景编辑器目录里被 ignore_tab_id 挡掉的组。"}


def op_ui_schema(edit, args):
    """UI_CUSTOM 字段字典：别名 / property_id / 事件类型 / 应用方法 / 面板文案 / 形态。"""
    cm = edit.get_component_manager()
    gid = _as_int(args.get("ugc_id"))
    comp = None
    if gid is not None:
        comp = cm._ugc_id2component.get(gid) or cm._ugc_id2component.get(str(gid))
        if comp is None:
            return {"ok": False, "error": "组件索引中没有 ugc_id=%s" % gid}
    elif widgets(edit):
        comp = widgets(edit)[0][1]
    table = _prop_table(edit, comp)
    aliases = _alias_map(edit, comp)
    mapped = {str(row["property_id"]) for row in aliases.values()}
    return {"ok": True, "component_type": UI_CUSTOM_TYPE,
            "labels_from": ("活控件 ugc_id=%s" % comp._ugc_id if comp is not None else
                            "地图暂无控件：label/direction 取不到，不影响写接口"),
            "properties": [dict(aliases[a], is_event_slot=a in EVENT_SLOTS)
                           for a in sorted(aliases, key=lambda x: aliases[x]["property_id"])],
            "unmapped_properties": [
                {"property_id": row["property_id"], "property_type": row["property_type"],
                 "event_name": row["event_name"]}
                for pid, row in sorted(table.items(), key=lambda kv: _as_int(kv[0]) or 0)
                if pid not in mapped],
            "event_slots": list(EVENT_SLOTS),
            "value_shapes": {
                "pos": "[x, y] 画布 2D 坐标（**不是**场景 [x,y,z]）",
                "width / height": "数值（像素）",
                "alpha / press_time / font_size / interaction_layer": "数值",
                "visible / only_send_ui / press_enable": "引擎布尔形态（30001），按原样写",
                "name / content": "文本",
                "ui_color / font_color / add_color": "颜色：引擎双层值，原样传",
                "on_click / on_release / on_long_press": "[事件名, 1286] —— 1286 是字段默认值，"
                                                        "由 ui_edit_bind 负责，勿手填",
                "on_show / on_hide": "[事件名]"},
            "note": "写接口用别名或 property_id 皆可；事件槽请改用 ui_edit_bind。"}


def op_ui_list(edit, args):
    """地图上已有控件的清单（include_native=true 时附带 UI_NATIVE 概要）。"""
    ui = _edit_ui(edit)
    room = _room(edit)
    found = widgets(edit)
    aliases = _alias_map(edit, found[0][1]) if found else _alias_map(edit)
    ids = [_real_item_id(room, gid, comp) for gid, comp, _u in found]
    names = _item_names(edit, ids)
    shares = _unit_shares(edit)
    want = None if not args.get("ugc_ids") else {_as_int(x) for x in args["ugc_ids"]}
    rows = []
    for gid, comp, unit in found:
        if want is not None and gid not in want:
            continue
        rows.append(_widget_record(ui, room, gid, comp, unit, names, aliases, shares))
    out = {"ok": True, "count": len(rows), "component_type": UI_CUSTOM_TYPE,
           "view_open": _view_open(ui), "widgets": rows}
    if args.get("include_native"):
        nat = [{"ugc_id": gid, "owner_uid": getattr(u, "uid", None),
                "item_id": getattr(u, "ugc_type", None), "properties": _bag(c)}
               for gid, c, u in widgets(edit, UI_NATIVE_TYPE)]
        out["ui_native_count"] = len(nat)
        out["ui_native_note"] = ("UI_NATIVE 是原生技能/操作位（一个单元挂 N 个组件），"
                                 "不在界面编辑器画布上，本模块不支持增删改它")
        if args.get("with_native_detail"):
            out["ui_native"] = nat
    return out


def op_ui_get(edit, args):
    """单个控件：属性袋 + 事件绑定 + 登记状态 + 保存报文形态。"""
    ui = _edit_ui(edit)
    room = _room(edit)
    cm = edit.get_component_manager()
    gid = _as_int(args.get("ugc_id"))
    if gid is None:
        return {"ok": False, "error": "需要 ugc_id"}
    comp = cm._ugc_id2component.get(gid) or cm._ugc_id2component.get(str(gid))
    unit = getattr(comp, "_owner", None) if comp is not None else None
    if unit is None:
        return {"ok": False, "error": "组件索引中没有 ugc_id=%s（或实体已销毁）" % gid}
    if component_type(comp) != UI_CUSTOM_TYPE:
        return {"ok": False, "error": "ugc_id=%s 的组件类型是 %s，不是界面控件(200000)"
                                      % (gid, component_type(comp))}
    names = _item_names(edit, [_real_item_id(room, gid, comp)])
    rec = _widget_record(ui, room, gid, comp, unit, names, _alias_map(edit, comp),
                         _unit_shares(edit))
    rec["ok"] = True
    rec["property_bag"] = _bag(comp)
    rec["save_payload_params"] = _save_shape(comp)
    return rec


def op_ui_verify(edit, args):
    """落库前自检：控件是否进报文、事件引用双向是否对得上、有没有待删除标记。

    `with_mirror_probe=true` 时额外照抄保存通道的"逐件切换后读单元镜像"写法比对一次，
    用来证明报文里那份确实属于本控件（同 item_id 共用单元才有意义）。默认关：它会临时
    改编辑器"当前主组件"的指向（实测不入 change_units、不标脏地图）。
    """
    ui = _edit_ui(edit)
    room = _room(edit)
    reg = _ops(edit)._event_registry(edit)
    probe = bool(args.get("with_mirror_probe"))
    rows, problems = [], []
    dirty_by_probe = []
    for gid, comp, unit in widgets(edit):
        bag = _bag(comp)
        params = _save_shape(comp)
        state = _reg_state(ui, room, comp, unit)
        pos_pid = str(PROP_ALIAS["pos"][0])
        pos_bag = bag.get(pos_pid)
        events = []
        for alias in EVENT_SLOTS:
            info = bag.get(str(PROP_ALIAS[alias][0]))
            if not (isinstance(info, list) and info and info[0] not in (None, "")):
                continue
            name = info[0]
            entry = reg.get(name)
            refs = [tuple(r) for r in ((entry or {}).get("info") or []) if isinstance(
                r, (list, tuple))]
            events.append({"slot": alias, "event_key": name,
                           "event_in_registry": bool(entry) and
                                                (entry or {}).get("info") is not None,
                           "ref_back_to_widget": any(r and r[0] == gid for r in refs)})
        row = {"ugc_id": gid, "item_id": _real_item_id(room, gid, comp),
               "params_in_save": len(params),
               "has_pos": any(str(p.get("type")) == pos_pid for p in params),
               "events": events, "registration": state}
        if probe:
            mirror = _mirror_shape(ui, comp, unit)
            pos_mirror = next((p.get("info") for p in (mirror or [])
                               if str(p.get("type")) == pos_pid), "<<无>>")
            row["mirror_probe"] = {"params": len(mirror or []),
                                   "pos": _plain(pos_mirror),
                                   "matches_bag": _plain(pos_mirror) == _plain(pos_bag)}
            if mirror is None:
                problems.append({"ugc_id": gid, "why": "读单元镜像失败（change_component_id/"
                                                      "_get_ugc_info 不可用）"})
            elif not row["mirror_probe"]["matches_bag"]:
                # 共用单元的控件靠 change_component_id 逐件切换才各读各的袋；对不上说明
                # 报文里那份是兄弟控件的值（重进后会互相覆盖），必须当场报出来
                problems.append({"ugc_id": gid,
                                 "why": "镜像里的 POS 与本控件的袋不一致：报文会带上兄弟控件的值",
                                 "component_bag": _plain(pos_bag), "mirror": _plain(pos_mirror)})
            if _reg_state(ui, room, comp, unit)["in_change_component"]:
                dirty_by_probe.append(gid)   # 兜底：真被登记了就要如实报出来
        rows.append(row)
        if not params:
            problems.append({"ugc_id": gid, "why": "保存报文里没有这个控件的属性"})
        if params and not row["has_pos"]:
            problems.append({"ugc_id": gid, "why": "报文里没有 POS(20000010001)，重进会落回默认位"})
        if state["pending_delete_component"]:
            problems.append({"ugc_id": gid, "why": "在待删除袋里 —— 保存会把它删掉"})
        for e in events:
            if not e["event_in_registry"]:
                problems.append({"ugc_id": gid,
                                 "why": "绑定的事件 %r 不在事件注册表里（触发侧找不到它）"
                                        % e["event_key"]})
            elif not e["ref_back_to_widget"]:
                problems.append({"ugc_id": gid,
                                 "why": "事件 %r 的引用里没有这个控件（记账与槽位不一致）"
                                        % e["event_key"]})
    return {"ok": not problems, "view_open": _view_open(ui), "count": len(rows),
            "widgets": rows, "problems": problems,
            "mirror_probe": {"ran": probe, "dirty_widgets": dirty_by_probe} if probe else None,
            "note": "自检只看内存里的登记状态；persistence 仍须游戏内保存 + 重进核实"
                    "（重进后 ugc_id 整体重编号，请按 item_id/名称比对）。"}


# ---------------------------------------------------------------------------
# 写 ops
# ---------------------------------------------------------------------------
def op_ui_add(edit, args):
    """新增一个界面控件（服务端建件 → place_poll 里走原生 UI 确认）。

    args: item_id | item | name（三者其一，name 走目录精确匹配）,
          pos [x, y]（缺省=画布中心）, request_id（幂等编号，可省）, dry_run
    """
    ui = _edit_ui(edit)
    blocked = _require_view_open(ui)
    if blocked:
        return blocked
    item_id = _as_int(args.get("item_id")) if args.get("item_id") is not None else \
        _find_item(edit, args.get("item") or args.get("name"))
    if item_id is None:
        return {"ok": False, "error": "需要 item_id，或用 item/name 让目录精确匹配一个控件",
                "hint": "可放清单：ui_edit_catalog {'query':'按钮','limit':0}"}
    items = (_catalog(edit).build(edit) or {}).get("items") or {}
    rec = items.get(item_id)
    if rec is None:
        return {"ok": False, "error": "目录里没有 item_id=%s" % item_id}
    if rec.get("component_type") != UI_CUSTOM_TYPE:
        return {"ok": False, "error": "item_id=%s 的组件类型是 %s，不是界面控件(200000)"
                                      % (item_id, rec.get("component_type"))}
    pos = _pos2(args["pos"]) if args.get("pos") is not None else _center_pos(edit)
    scene_args = {"item_id": item_id, "position": [pos[0], pos[1], 0.0], "mode": "server"}
    if args.get("request_id") is not None:
        scene_args["request_id"] = args["request_id"]
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "item_id": item_id,
                "item_name_cn": rec.get("name_cn"), "pos": pos,
                "would_call": {"op": "place_item", "args": scene_args},
                "confirm": "place_poll 见组件类型 200000 时走原生 "
                           "ui.on_check_ui_component_limit([comp], pos, update_params)",
                "note": "确认成功只是客户端接纳；须 ui_edit_verify + 游戏内保存才算落库。"}
    try:
        res = _scene(edit).place(edit, scene_args)
    except Exception as exc:
        return {"ok": False, "error": "摆放请求未受理: %s" % exc,
                "note": "同一地图会话一次只允许一笔未定论请求；"
                        "先用 ui_edit_add_poll 查完上一笔。"}
    res["item_name_cn"] = rec.get("name_cn")
    res["ui_pos"] = pos
    res["next"] = "用 request_id 调 ui_edit_add_poll 直到 status=placed"
    return res


def op_ui_add_poll(edit, args):
    """查询 op_ui_add 的结果（确认分流在 bridge/fuxiang_placement.py）。"""
    return _scene(edit).place_poll(edit, args)


def op_ui_move(edit, args):
    """把控件挪到画布 2D 坐标 [x, y]（等价 ui_set {'pos': [...]}，单独给个顺手的口子）。"""
    return op_ui_set(edit, {"ugc_id": args.get("ugc_id"),
                            "props": {"pos": _pos2(args.get("pos"))},
                            "dry_run": bool(args.get("dry_run"))})


def op_ui_set(edit, args):
    """改控件属性（pos/width/height/alpha/visible/name/content/字体/颜色/交互层…）。

    args: ugc_id, props {别名|property_id: 值}, dry_run
    事件类槽位（on_click 等）会被拒 —— 它们要走 op_ui_bind 才同时更新事件注册表。
    """
    ui = _edit_ui(edit)
    blocked = _require_view_open(ui)
    if blocked:
        return blocked
    gid = _as_int(args.get("ugc_id"))
    raw = args.get("props")
    if gid is None:
        return {"ok": False, "error": "需要 ugc_id"}
    if not isinstance(raw, dict) or not raw:
        return {"ok": False, "error": "需要 props（对象：{属性名或 property_id: 值}）"}
    cm = edit.get_component_manager()
    comp = cm._ugc_id2component.get(gid) or cm._ugc_id2component.get(str(gid))
    if comp is None:
        return {"ok": False, "error": "组件索引中没有 ugc_id=%s" % gid}
    aliases = _alias_map(edit, comp)
    by_pid = _by_pid(aliases)
    pos_pid = int(aliases["pos"]["property_id"])
    size_pids = {int(aliases["width"]["property_id"]), int(aliases["height"]["property_id"])}
    # 文本类字段（控件名 / 文案）在玩家手放的控件里是 ["文本"] 而不是 "文本"
    # （实测对照：手放的 362 是 {"20000016501": ["按钮"]}），这里替调用方包一层。
    list_text_pids = {int(aliases[k]["property_id"]) for k in ("name", "content")}
    props, event_keys = {}, []
    for key, value in raw.items():
        try:
            pid = _resolve_prop(aliases, key)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        row = by_pid.get(str(pid)) or {}
        if row.get("alias") in EVENT_SLOTS:
            event_keys.append(row["alias"])
            continue
        if pid == pos_pid:
            props[str(pid)] = _pos2(value)
        elif pid in size_pids:
            props[str(pid)] = [_num(value, key)]
        elif pid in list_text_pids and isinstance(value, str):
            props[str(pid)] = [value]
        else:
            props[str(pid)] = value
    if event_keys:
        return {"ok": False, "error": "事件类槽位请用 ui_edit_bind：%s" % event_keys,
                "note": "它们的 info 形态是 [事件名, 1286]，直接写会绕开事件注册表的反向登记。"}
    if not props:
        return {"ok": False, "error": "props 里没有可写的属性"}
    res = _ops(edit).op_edit_props(edit, {
        "ugc_id": gid, "props": props, "allow_unregistered": True,
        "allow_unknown_property": bool(args.get("allow_unknown_property")),
        "dry_run": bool(args.get("dry_run"))})
    if isinstance(res, dict) and res.get("ok") and not args.get("dry_run"):
        res["ui_id"] = gid
        res["canvas_refresh"] = _refresh_canvas(ui)
        res["note"] = ("属性已写进控件属性袋并登记保存变更集（change_units），画布刷新见 "
                       "canvas_refresh。持久化仍须游戏内保存 + 重进核实。")
    return res


def op_ui_bind(edit, args):
    """给控件绑事件：on_click(点击) / on_release(松手) / on_long_press(长按) /
    on_show(使控件显示) / on_hide(使控件隐藏)。

    args: ugc_id, slot（缺省 on_click）| property_id, event_key(事件名),
          create_event=true（事件不存在则先建）, dry_run
    """
    ui = _edit_ui(edit)
    blocked = _require_view_open(ui)
    if blocked:
        return blocked
    gid = _as_int(args.get("ugc_id"))
    key = args.get("event_key")
    if gid is None or not isinstance(key, str) or not key.strip():
        return {"ok": False, "error": "需要 ugc_id 与 event_key（事件名字符串）"}
    key = key.strip()
    cm = edit.get_component_manager()
    comp = cm._ugc_id2component.get(gid) or cm._ugc_id2component.get(str(gid))
    if comp is None:
        return {"ok": False, "error": "组件索引中没有 ugc_id=%s" % gid}
    aliases = _alias_map(edit, comp)
    by_pid = _by_pid(aliases)
    if args.get("property_id") is not None:
        pid = _resolve_prop(aliases, args["property_id"])
        row = by_pid.get(str(pid)) or {}
        if row.get("alias") not in EVENT_SLOTS:
            return {"ok": False, "error": "property_id=%s 不是事件槽位（可用：%s）"
                                         % (pid, list(EVENT_SLOTS)),
                    "direction": row.get("direction")}
    else:
        slot = args.get("slot") or "on_click"
        if slot not in aliases:
            return {"ok": False, "error": "未知槽位 %r，可用：%s" % (slot, list(EVENT_SLOTS))}
        if slot not in EVENT_SLOTS:
            return {"ok": False, "error": "%s 不是事件槽位（可用：%s）"
                                         % (slot, list(EVENT_SLOTS))}
        row = aliases[slot]
        pid = int(row["property_id"])
    ops = _ops(edit)
    reg = ops._event_registry(edit)
    created = False
    if key not in reg or (reg[key] or {}).get("info") is None:
        if not args.get("create_event"):
            return {"ok": False, "error": "事件不存在: %s" % key,
                    "existing": sorted(str(k) for k in reg.keys()
                                       if (reg[k] or {}).get("info") is not None),
                    "hint": "传 create_event=true 先建事件（与原生命令层同链路，进撤销栈）"}
        got = ops.op_event_create(edit, {"name": key})
        if not got.get("ok"):
            return {"ok": False, "error": "建事件失败: %s" % got.get("error"), "create": got}
        created = True
    res = ops.op_event_bind(edit, {"ugc_id": gid, "event_key": key, "property_id": pid,
                                   "dry_run": bool(args.get("dry_run"))})
    if isinstance(res, dict) and res.get("ok") and not args.get("dry_run"):
        res.update({"slot": row.get("alias"), "slot_label": row.get("label"),
                    "direction": row.get("direction"), "event_created": created,
                    "property_id": pid, "ui_id": gid,
                    "canvas_refresh": _refresh_canvas(ui),
                    "note": "事件名已写进槽位并在事件注册表登记反向引用（保存时随 "
                            "change_units 一起上送）。持久化仍须游戏内保存 + 重进核实。"})
    return res


def op_ui_unbind(edit, args):
    """解绑控件上的事件（清槽位 + 摘事件注册表里的引用）。"""
    ui = _edit_ui(edit)
    blocked = _require_view_open(ui)
    if blocked:
        return blocked
    gid = _as_int(args.get("ugc_id"))
    key = args.get("event_key")
    if gid is None or not isinstance(key, str) or not key.strip():
        return {"ok": False, "error": "需要 ugc_id 与 event_key"}
    res = _ops(edit).op_event_unbind(edit, {"ugc_id": gid, "event_key": key.strip()})
    if isinstance(res, dict) and res.get("ok"):
        res["ui_id"] = gid
        res["canvas_refresh"] = _refresh_canvas(ui)
    return res


def op_ui_delete(edit, args):
    """删除控件（原生删除命令）。⚠️ 默认 dry_run：先看会删到哪些。

    args: ugc_id | ugc_ids, dry_run=false 才真删
    """
    ui = _edit_ui(edit)
    blocked = _require_view_open(ui)
    if blocked:
        return blocked
    room = _room(edit)
    cm = edit.get_component_manager()
    ids = args.get("ugc_ids") or ([args["ugc_id"]] if args.get("ugc_id") is not None else [])
    ids = [_as_int(x) for x in ids]
    if not ids or None in ids:
        return {"ok": False, "error": "需要 ugc_id 或 ugc_ids（整数）"}
    if len(ids) > 50:
        return {"ok": False, "error": "单批最多 50 个"}
    targets = []
    for gid in ids:
        comp = cm._ugc_id2component.get(gid)
        unit = getattr(comp, "_owner", None) if comp is not None else None
        if unit is None:
            return {"ok": False, "error": "ugc_id=%s 不在组件索引里（或实体已销毁）" % gid}
        if component_type(comp) != UI_CUSTOM_TYPE:
            return {"ok": False, "error": "ugc_id=%s 的组件类型是 %s，不是界面控件；"
                                          "场景物件请用 edit_delete"
                                         % (gid, component_type(comp))}
        targets.append((gid, comp, unit))
    delbag = [str(x) for x in (getattr(room, "del_unit_uids", None) or {})
              .get("component", [])]
    if args.get("dry_run", True):
        return {"ok": True, "dry_run": True,
                "would_delete": [{"ugc_id": g, "item_id": getattr(u, "ugc_type", None),
                                  "name": _bag(c).get(str(PROP_ALIAS["name"][0]))}
                                 for g, c, u in targets],
                "already_pending_delete": [g for g, _, _ in targets if str(g) in delbag],
                "note": "默认 dry_run，传 dry_run=false 才真删。**退出编辑器会自动保存**，"
                        "删除标记一旦上送就没有反悔机会，删前核对清单。"}
    try:
        ui.on_del_ui_component([g for g, _, _ in targets],
                               {"refresh_occupancy": True, "refresh_component_nums": True,
                                "refresh_ui_custom": True})
    except Exception as exc:
        return {"ok": False, "error": "on_del_ui_component 失败: %s: %s"
                                      % (type(exc).__name__, exc)}
    after = [str(x) for x in (getattr(room, "del_unit_uids", None) or {}).get("component", [])]
    marked = [g for g, _, _ in targets if str(g) in after]
    missed = [g for g, _, _ in targets if str(g) not in after]
    return {"ok": not missed, "requested": [g for g, _, _ in targets],
            "marked_for_delete": marked, "not_marked": missed,
            "still_in_component_table": [g for g, _, _ in targets
                                         if cm._ugc_id2component.get(g) is not None],
            "persistence_verified": False,
            "note": "只有进了 room.del_unit_uids['component'] 的才会随保存上送（README"
                    "「删除」节）。not_marked 非空说明命令被拦下，先查现场不要重试保存。"}


def _num(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s 必须是数值，收到 %r" % (name, value))
    v = float(value)
    if not math.isfinite(v):
        raise ValueError("%s 不是有限数值" % name)
    return v


def _pos2(value):
    """控件坐标是画布 2D：接受 [x, y]。显式拒掉 3 元组，免得把场景摆放的习惯带进来。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("pos 必须是画布 2D 坐标 [x, y]（界面编辑器不是场景 [x,y,z]），"
                        "收到 %r" % (value,))
    return [_num(value[0], "pos[0]"), _num(value[1], "pos[1]")]


def _center_pos(edit):
    size = _canvas_size(edit)
    return [round(size[0] * DEFAULT_POS_RATIO[0], 2), round(size[1] * DEFAULT_POS_RATIO[1], 2)]
