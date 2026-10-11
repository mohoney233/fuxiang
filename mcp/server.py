#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fuxiang-mcp — 《第五人格》「随笔浮想」编辑器 MCP 服务器。

传输：stdio，JSON-RPC 2.0（换行分隔）。
链路：本进程 → tools/fuxiang/ipc 目录 → 游戏内 fuxiang_bridge → fuxiang_edit API 层。

环境要求：游戏已启动并进入「随笔浮想」编辑器，且桥接已安装
（首次使用需以管理员身份运行：fuxiang-inject install）。

⛔ 2026-09-22 起 MCP stdio 服务默认停用（通道收敛）。
  停用原因：本进程只是「66 个 tool 的转发门面」，其中 63 个是纯转发、参数 schema
  与 bridge 层重复声明（新增一个 op 要改 3 处，数字/结论还会在 tool 描述里过期，
  例如本文件曾长期写着「66 个组件类型」而实际为 79）。同一能力 cli/ 已 100% 覆盖
  （`cli/fxedit.py <op>` 可透传任意 op），保留两条等价通道只会增加漂移点。

  ⚠️ 本目录的 `mcp/preview.py` **不要一起删**：`cli/fxshot.py` 通过 importlib
  动态加载它来做预览编排（plan/shot/sweep/restore）。停用的只是本 stdio 服务。

  确需临时启用（例如接入没有 shell 权限的客户端）：
      PowerShell:  $env:FUXIANG_ENABLE_MCP=1
      cmd:         set FUXIANG_ENABLE_MCP=1
  命令行入口 cli/mcpcall.py / cli/mcpcallf.py 同样受该开关约束。
"""
import sys, os, json, time, random, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD_DIR = os.path.join(ROOT, "ipc", "cmd")
OUT_DIR = os.path.join(ROOT, "ipc", "out")
STATE_FILE = os.path.join(ROOT, "ipc", "bridge_state.json")
BRIDGE_LOG = os.path.join(ROOT, "ipc", "bridge.log")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fuxiang-ugc-editor"
SERVER_VERSION = "0.8.0"

STATE_MAX_AGE = 10.0     # 心跳超过该秒数视为失联
DEFAULT_TIMEOUT = 60.0

# --------------------------------------------------------------------------
# IPC：与游戏内桥通信
# --------------------------------------------------------------------------
class BridgeError(Exception):
    pass


def bridge_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def check_alive():
    st = bridge_state()
    if st is None:
        raise BridgeError(
            "桥接未安装或从未启动。请先以管理员身份运行："
            "tools\\fuxiang\\injector\\bin\\Debug\\net8.0-windows\\fuxiang-inject.exe install")
    age = time.time() - float(st.get("now", 0))
    if age > STATE_MAX_AGE:
        raise BridgeError(
            "桥接心跳已过期（%.1f 秒未更新）。游戏可能已关闭或桥接被卸载，"
            "请重新运行 fuxiang-inject install。" % age)


def call_bridge(op, args=None, timeout=DEFAULT_TIMEOUT):
    """发一条命令给游戏内桥，等待结果。"""
    check_alive()
    for d in (CMD_DIR, OUT_DIR):
        os.makedirs(d, exist_ok=True)

    cid = "mcp%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    cmd_path = os.path.join(CMD_DIR, cid + ".json")
    out_path = os.path.join(OUT_DIR, cid + ".json")

    with open(cmd_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"id": cid, "op": op, "args": args or {}}, f, ensure_ascii=False)
    os.replace(cmd_path + ".tmp", cmd_path)

    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        if os.path.exists(out_path):
            for _ in range(30):
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    break
                except Exception:
                    time.sleep(0.01)
            break
        time.sleep(0.04)

    if data is None:
        raise BridgeError("等待游戏响应超时（%.0fs）。命令文件：%s" % (timeout, cmd_path))
    try:
        os.remove(out_path)
    except Exception:
        pass

    if not data.get("ok"):
        raise BridgeError("游戏内执行失败：\n%s" % data.get("error"))
    return data.get("value")


def call_edit(op, args=None, timeout=DEFAULT_TIMEOUT):
    """调用游戏内 fuxiang_edit API 层（每次 reload，支持热更新）。"""
    payload = json.dumps({"op": op, "args": args or {}}, ensure_ascii=False)
    code = (
        "import importlib, json, sys, os\n"
        "sys.path.insert(0, %r)\n"
        "m = importlib.import_module('fuxiang_edit')\n"
        "importlib.reload(m)\n"
        "__result__ = m.dispatch(%r)\n" % (os.path.join(ROOT, "bridge"), payload)
    )
    value = call_bridge("eval", {"code": code, "timeout": timeout}, timeout=timeout)
    if not isinstance(value, dict):
        raise BridgeError("意外的返回类型: %r" % (value,))
    # 桥的 eval op 返回 {"result": <dispatch 的返回值>} 或 {"error": ...}
    if "error" in value:
        raise BridgeError(str(value["error"])[:4000])
    inner = value.get("result")
    if not isinstance(inner, dict):
        raise BridgeError("编辑器 API 层未返回结构化结果: %r" % (inner,))
    if not inner.get("ok"):
        raise BridgeError(json.dumps(inner, ensure_ascii=False, default=str))
    return inner.get("value")


# --------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------
def tool(name, desc, schema):
    return {"name": name, "description": desc, "inputSchema": schema}


TOOLS = [
    tool("fuxiang_status",
         "检查《第五人格》「随笔浮想」(UGC) 编辑器的连接与运行状态：游戏进程、编辑器核心、"
         "组件管理器是否可用。做任何操作前应先调用本工具确认在线。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_map_info",
         "获取当前正在编辑的随笔浮想地图概况：地图名称/描述/标签、游戏区域锚点、"
         "各类组件的数量分布。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_list_types",
         "列出随笔浮想中的全部组件类型，含编号、英文名、中文名、实现类、目录物件数、"
         "当前已放置数，以及 creatable（能否通过接口创建）与 place_support"
         "（verified/unsupported/unknown）。用 only 过滤：creatable/orphan/"
         "place_verified/live。要放物体、或想知道某个编号是否存在，都先查这里。",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "按编号/英文名/中文名/类名过滤。"},
             "only": {"type": "string",
                      "enum": ["creatable", "orphan", "place_verified", "live"],
                      "description": "只看可创建的 / 无实现类的孤儿 / 已实测可摆放的 / 场景中有实例的。"},
             "limit": {"type": "integer", "description": "返回条数，默认 200。"},
             "offset": {"type": "integer", "description": "分页偏移。"},
             "refresh": {"type": "boolean", "description": "强制重建索引。"},
         }}),

    tool("fuxiang_list_items",
         "列出「随笔浮想」中可放置的物件目录（3963 项）。每项含 item_id、中文名 name_cn、"
         "所属分类（category_cn/category_group_cn，即编辑器左栏分类）、模型路径、占用值、"
         "以及 component_type（对应组件类型编号）。摆场景前用本工具查找想放的物件。",
         {"type": "object", "properties": {
             "query": {"type": "string",
                       "description": "关键字，匹配 item_id / 中文名 / item_tid / model_path。"},
             "item_type": {"type": ["integer", "string"],
                           "description": "按物件大类过滤：1=场景物件 2=全局/功能组件 3=UI组件 10=限制区域。"},
             "category_id": {"type": ["integer", "string"],
                             "description": "按分类 ID 过滤，取值见 fuxiang_categories。"},
             "limit": {"type": "integer", "description": "返回条数，默认 50。"},
             "offset": {"type": "integer", "description": "分页偏移，默认 0。"},
             "include_hidden": {"type": "boolean", "description": "默认 false，排除原生对象目录隐藏记录；同模型不代表同物件。"},
         }}),

    tool("fuxiang_place_item",
         "异步请求服务器创建物件，立即返回 request_id；随后用 fuxiang_place_poll 完成原生确认。"
         "服务端 add_unit 无类型白名单（实测 79 个组件类型可服务端摆放，含几何体、"
         "点光源、密码机、道具箱、柜子、NPC 等；唯一真不受理的是 203013 图片墙）；"
         "客户端 SERVER_COMPONENT_TYPES 只是保守阀门，不在其中会报错但**不代表服务端拒绝**，"
         "可在 fuxiang_eval 内临时扩展该集合后摆放。"
         "等待期间勿手动拖入物件。超时继续查询原请求，勿重新创建。"
         "返回 placed 只代表编辑器已登记：保存会重置基线，未进入该次保存的对象会被摘除登记"
         "（重进后可能重复或消失），因此每批摆完立即保存、末次保存后不要再摆放。"
         "mode=local_preview 可显式创建不持久化的本地预览。",
         {"type": "object", "properties": {
             "item_id": {"type": ["integer", "string"],
                         "description": "物件 ID，用 fuxiang_list_items 查询。"},
             "position": {"type": "array", "items": {"type": "number"},
                          "minItems": 3, "maxItems": 3,
                          "description": "世界坐标 [x, y, z]。"},
             "rotation": {"type": "array", "items": {"type": "number"},
                          "minItems": 3, "maxItems": 3,
                          "description": "旋转欧拉角 [rx, ry, rz]，单位度，默认 [0,0,0]。"},
             "scale": {"type": ["number", "array"],
                       "description": "缩放，数值或 [x,y,z]，默认 1。"},
             "allow_unverified": {"type": "boolean",
                                  "description": "允许摆放未经完整验证的组件类型（默认 false）。"},
             "request_id": {"type": "string", "description": "可选幂等键；重试须沿用同一键和参数。"},
             "mode": {"type": "string", "enum": ["server", "local_preview"],
                      "description": "默认 server；local_preview 不保存到服务器。"},
         }, "required": ["item_id"]}),

    tool("fuxiang_place_poll", "短调用查询摆放请求；服务器物件就绪后执行一次原生确认，不在主线程等待。",
         {"type": "object", "properties": {"request_id": {"type": "string"}},
          "required": ["request_id"]}),

    tool("fuxiang_recover_pending",
         "恢复「服务端已创建、但原生确认未完成」的物件登记（典型成因：place_item 与 "
         "place_poll 之间地图 room 被重建，物件卡在 del_unit_uids 待确认态且不在 "
         "all_ugc_uid_set，旧 request_id 已失效）。只走客户端命令层（登记/占用/撤销栈），"
         "不向服务端重发创建，因此不会造重复件。默认 dry_run 只列清单；"
         "确认无误后传 dry_run=false 执行，随后必须立即 fuxiang_save_map 并核对 dropped_count。",
         {"type": "object", "properties": {
             "ugc_ids": {"type": "array", "items": {"type": "integer"},
                         "description": "要恢复的 ugc_id；省略则扫描全部待确认态物件。"},
             "dry_run": {"type": "boolean", "description": "默认 true，只列出不改动。"},
             "limit": {"type": "integer", "description": "单批上限，默认 50。"},
         }}),

    tool("fuxiang_material_info",
         "只读：逐件对比实体运行时材质与组件属性袋材质。返回 prop_mtg/prop_color/prop_alpha、"
         "runtime.group_data（原生 mtg 名）、slot 的 technique/transparent_mode/alpha，"
         "并在两者 basename 不一致时给出 mismatch。用于排查「属性写了 mtg 但表面像是别的材质」。\n"
         "注意：引擎的 render.material_group 不是序列（无 __iter__/__len__/__getitem__），"
         "只能按 count + get_at(i) 取；slot 的 filename/shader 常为空串。mismatch 只说明"
         "属性袋与运行时不一致，不等于渲染失败，须截图确认。",
         {"type": "object", "properties": {
             "ugc_ids": {"type": "array", "items": {"type": "integer"},
                         "description": "要检查的 ugc_id，单批最多 50 个。"},
         }, "required": ["ugc_ids"]}),

    tool("fuxiang_plan_item",
         "只读校验一次摆放请求（不写入场景）。返回解析后的组件类型、实体类型、"
         "模型路径与变换；参数非法时返回原因。摆放前建议先用它检查。",
         {"type": "object", "properties": {
             "item_id": {"type": ["integer", "string"], "description": "物件 ID。"},
             "position": {"type": "array", "items": {"type": "number"},
                          "description": "[x, y, z] 世界坐标。"},
             "rotation": {"type": "array", "items": {"type": "number"},
                          "description": "[rx, ry, rz] 角度。"},
             "scale": {"type": ["number", "array"],
                       "description": "缩放，数值或 [x,y,z]。"},
             "allow_unverified": {"type": "boolean",
                                  "description": "允许摆放未经完整验证的组件类型（默认 false）。"},
         }, "required": ["item_id"]}),

    tool("fuxiang_list_components",
         "列出当前地图中已放置的组件（物件）。可按类型编号过滤，支持分页。",
         {"type": "object", "properties": {
             "type": {"type": ["integer", "string"],
                      "description": "组件类型编号，如 100000(OBSTACLE)。留空返回全部。"},
             "limit": {"type": "integer", "description": "返回条数上限，默认 200。"},
             "offset": {"type": "integer", "description": "分页偏移，默认 0。"},
         }}),

    tool("fuxiang_get_component",
         "获取单个组件的详细信息；full=true 时返回全部属性与方法（体积较大，谨慎使用）。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "组件的 UGC ID。"},
             "full": {"type": "boolean", "description": "是否返回完整属性与方法列表。"},
             "child_key": {"type": "string", "description": "可选，附加读取该 key 下的参数/事件信息。"},
         }, "required": ["ugc_id"]}),

    tool("fuxiang_create_component",
         "低层接口：给没有组件的已有实体附加组件。需 component_type 与 owner_uid。"
         "普通场景摆放使用 fuxiang_place_item；本接口未覆盖各类组件完整初始化。",
         {"type": "object", "properties": {
             "info": {"type": "object",
                      "description": "组件定义字典，至少含 component_type、owner_uid。"},
         }, "required": ["info"]}),

    tool("fuxiang_remove_component",
         "低层接口：移除指定组件索引。对本接口创建的本地物件会同时销毁实体；其他实体不会销毁。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "要删除的组件 UGC ID。"},
         }, "required": ["ugc_id"]}),

    tool("fuxiang_repair_index",
         "自愈工具：把仍存活但脱离管理器索引的组件重新登记回去，并报告索引不一致"
         "（ghost：索引有但对象已无；orphan：对象有但索引无）。"
         "当增/删操作后组件数量出现异常时先调用本工具。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_pending_saves",
         "查看组件管理器的本地事件重放队列。它不是服务器保存收据；"
         "place_item 不往此队列插入合成事件。",
         {"type": "object", "properties": {
             "limit": {"type": "integer", "description": "返回条数，默认 50。"},
         }}),

    tool("fuxiang_get_params",
         "读取组件的参数与事件数据。child_key 通常为 'camp' 或 'class'，key 为组件 ID。",
         {"type": "object", "properties": {
             "child_key": {"type": "string", "description": "参数分组 key，如 'camp'/'class'。"},
             "key": {"type": "string", "description": "组件 key（ugc_id）。留空返回整张表。"},
         }}),

    tool("fuxiang_set_params",
         "修改组件的参数与事件数据（属性面板/事件逻辑的底层写入口）。",
         {"type": "object", "properties": {
             "child_key": {"type": "string", "description": "参数分组 key。"},
             "key": {"type": "string", "description": "组件 key（ugc_id）。"},
             "info": {"type": "object", "description": "要写入的参数/事件数据。"},
         }, "required": ["child_key", "key", "info"]}),

    tool("fuxiang_create_event",
         "创建一个 UGC 事件（随笔浮想的逻辑/事件系统节点）。",
         {"type": "object", "properties": {
             "event_type": {"type": ["integer", "string"], "description": "事件类型编号。"},
             "args": {"type": "object", "description": "事件参数。"},
         }, "required": ["event_type"]}),

    tool("fuxiang_save",
         "设置游戏组件保存标记，可选调用地图改名入口。未验证完整地图保存流程，"
         "成功返回不代表服务器已保存，也不保证本地创建物件持久化。",
         {"type": "object", "properties": {
             "name": {"type": "string", "description": "可选，同时更新地图名称。"},
             "with_dump": {"type": "boolean",
                           "description": "是否回传事件数据快照（较大），默认 false。"},
         }}),

    tool("fuxiang_save_map",
         "【主动保存地图·发起】向服务端发起一次保存，与点编辑器保存按钮同链路"
         "（ui._save_change → send_battle_event(UGC,{key:'save_change',...})）。"
         "主线程只发事件并挂 ugc_save_finish 回执监听，不等待；立即返回 request_id，"
         "请随后用 fuxiang_save_map_poll 轮询回执。同一时间只允许一笔在途保存。"
         "收到回执=服务端已接受；真实持久化仍建议保存后退出重进核对。",
         {"type": "object", "properties": {
             "request_id": {"type": "string", "description": "可选幂等键；缺省自动生成。"},
             "stale_after": {"type": "number",
                             "description": "在途请求被视为过期可释放的秒数，默认 120。"},
         }}),

    tool("fuxiang_save_map_poll",
         "【主动保存地图·轮询】查询在途保存的回执。status=saved 表示 room.ugc_save_finish "
         "已被引擎回调（服务端已回执）；timeout 表示超时未回执；pending 请稍后再轮询。",
         {"type": "object", "properties": {
             "request_id": {"type": "string", "description": "发起时返回的 request_id。"},
             "timeout": {"type": "number", "description": "本次判定超时的秒数，默认 30。"},
         }}),

    tool("fuxiang_save_dirty",
         "【保存·脏状态诊断】读编辑器当前的『未保存改动缓冲』：ui.change_units 与 "
         "cm.save_component_info 的长度。游戏只在改动集被服务端受理并处理后才清空该缓冲；"
         "因此：编辑后它们非空=脏；fuxiang_save_map 提交后若被清空=改动已上送服务端"
         "（持久化信号，不依赖退出重进）。",
         {"type": "object", "properties": {
             "detail": {"type": "boolean",
                        "description": "同时返回 change_units 的原始载荷（排障用）。"},
         }}),

    tool("fuxiang_eval",
         "高级逃生舱：在《第五人格》进程内的编辑器上下文中执行 Python 代码。"
         "预置变量：core(UGCEditCore)、cm(UGCComponentManager)、em(UGCEventManager)、"
         "GK(游戏全局门面，GK.DM.* 是注册表/色板入口；不在编辑器时为 None)、"
         "jdump(obj)→JSON 字符串、plain(obj)→JSON 友好结构。"
         "把返回值赋给 __result__（直接给对象即可，接口会自动转换）。"
         "⚠️ 引擎注册表返回的 taggeddict 不继承 dict，"
         "json.dumps 会抛「Object of type taggeddict is not JSON serializable」；"
         "须改用 jdump(...)，不要用 json.dumps(..., default=str)"
         "（不报错，但会把嵌套结构压成不可解析的 repr 字符串）。"
         "仅在其它工具不够用时才用。",
         {"type": "object", "properties": {
             "code": {"type": "string", "description": "要执行的 Python 代码。"},
         }, "required": ["code"]}),
]

TOOL_MAP = {
    "fuxiang_status": ("status", {}),
    "fuxiang_map_info": ("map_info", {}),
    "fuxiang_list_types": ("list_types", ["query", "only", "limit", "offset", "refresh"]),
    "fuxiang_list_components": ("list_components", ["type", "limit", "offset"]),
    "fuxiang_get_component": ("get_component", ["ugc_id", "full", "child_key"]),
    "fuxiang_material_info": ("material_info", ["ugc_ids"]),
    "fuxiang_create_component": ("create_component", ["info"]),
    "fuxiang_remove_component": ("remove_component", ["ugc_id"]),
    "fuxiang_repair_index": ("repair_index", {}),
    "fuxiang_pending_saves": ("pending_saves", ["limit"]),
    "fuxiang_list_items": ("list_items", ["query", "item_type", "category_id",
                                          "limit", "offset", "include_hidden"]),
    "fuxiang_place_item": ("place_item", ["item_id", "position", "rotation", "scale",
                                          "model_path", "unit_type", "allow_unverified", "request_id", "mode"]),
    "fuxiang_place_poll": ("place_poll", ["request_id"]),
    "fuxiang_recover_pending": ("recover_pending", ["ugc_ids", "dry_run", "limit"]),
    "fuxiang_plan_item": ("plan_item", ["item_id", "position", "rotation", "scale",
                                        "allow_unverified"]),
    "fuxiang_remove_item": ("remove_item", ["ugc_id"]),
    "fuxiang_set_transform": ("set_transform", ["ugc_id", "position", "rotation", "scale"]),
    "fuxiang_get_params": ("get_params", ["child_key", "key"]),
    "fuxiang_set_params": ("set_params", ["child_key", "key", "info"]),
    "fuxiang_create_event": ("create_event", ["event_type", "args"]),
    "fuxiang_save": ("save", ["name", "with_dump"]),
    "fuxiang_save_map": ("save_map", ["request_id", "stale_after"]),
    "fuxiang_save_map_poll": ("save_map_poll", ["request_id", "timeout"]),
    "fuxiang_save_dirty": ("save_dirty", ["detail"]),
    "fuxiang_eval": ("eval", ["code"]),
}

TOOLS.extend([
    tool("fuxiang_remove_item", "清理本接口的本地预览；服务端物件拒绝本地销毁，须在游戏内删除并保存。",
         {"type": "object", "properties": {"ugc_id": {"type": ["integer", "string"]}},
          "required": ["ugc_id"]}),
    tool("fuxiang_set_transform", "修改本接口当前会话物件的变换，更新模型、碰撞和逻辑位置；服务端物件登记变更。单位为度，未接入变换撤销。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"]},
             "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
             "rotation": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
             "scale": {"type": ["number", "array"]}}, "required": ["ugc_id"]}),

    # ---- 全量注册表（组件 + 物体 统一定位）----
    tool("fuxiang_catalog",
         "【定位总表】返回编辑器「组件 + 物体」全量注册表的概览：组件类型总数/可创建数、"
         "无实现类的孤儿编号、物件总数、隐藏记录数、枚举数、分类数，以及摆放支持矩阵"
         "（已实测可摆放 / 已知不可摆放 / 未实测）。不确定某个编号是否存在、"
         "或想先了解编辑器里到底有什么，先用本工具。",
         {"type": "object", "properties": {
             "refresh": {"type": "boolean", "description": "强制重建索引（默认用缓存）。"},
         }}),

    tool("fuxiang_find",
         "【统一定位】按 item_id、component_type、名称关键字或枚举名，一次定位到具体的"
         "组件类型或物件。返回的记录包含：中文名、编辑器分类路径、实现类、模型路径、"
         "creatable（能否通过接口创建）与不可创建的原因。"
         "这是「某个物体/组件能不能被接口调用」的权威判据入口。",
         {"type": "object", "properties": {
             "item_id": {"type": ["integer", "string"], "description": "物件 ID，精确定位。"},
             "component_type": {"type": ["integer", "string"], "description": "组件类型编号，精确定位。"},
             "query": {"type": "string", "description": "关键字，模糊匹配中文名/英文名/类名/模型路径。"},
             "enum": {"type": "string", "description": "枚举名，返回其全部取值（如 MotionType）。"},
             "kind": {"type": "string", "enum": ["any", "item", "component_type", "enum"],
                      "description": "只保留某一类结果，默认 any。"},
             "limit": {"type": "integer", "description": "返回条数，默认 20。"},
             "offset": {"type": "integer", "description": "分页偏移。"},
             "refresh": {"type": "boolean", "description": "强制重建索引。"},
         }}),

    tool("fuxiang_component_detail",
         "【组件档案】给定 component_type，返回它的完整档案：英文/中文名、实现类与模块、"
         "是否已登记、工厂是否预热、目录中的物件有哪些（含中文名与分类）、"
         "属性 schema 是否可得、摆放支持状态。加 with_schema=true 同时返回属性 schema。",
         {"type": "object", "properties": {
             "component_type": {"type": ["integer", "string"], "description": "组件类型编号。"},
             "with_schema": {"type": "boolean", "description": "同时返回属性 schema。"},
             "item_limit": {"type": "integer", "description": "附带返回的物件条数，默认 30。"},
             "refresh": {"type": "boolean", "description": "强制重建索引。"},
         }, "required": ["component_type"]}),

    tool("fuxiang_component_schema",
         "【属性 schema】返回某个组件类型的属性定义表。两个来源："
         "ugc_item_property（按 玩法模式/皮肤类/属性项 三层，是编辑器属性面板的数据源）、"
         "ugc_control_panel（操控面板类物件专用）。做属性编辑前用它确认可写字段。",
         {"type": "object", "properties": {
             "component_type": {"type": ["integer", "string"], "description": "组件类型编号。"},
         }, "required": ["component_type"]}),

    tool("fuxiang_enums",
         "【取值域】列出 UGC 组件属性的取值枚举（当前 33 个）：运动类型、比较/赋值类型、"
         "音乐类型、目标类型、UI 锚点/对齐、对比度等级等。写属性值前用它确认合法取值。",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "按枚举名过滤，如 'Motion'。"},
         }}),

    tool("fuxiang_categories",
         "【分类树】列出编辑器自己的物件分类树（左侧「添加」面板的分类），"
         "含分组中文名、分类中文名与每类物件数量。返回的 category_id 可用于 "
         "fuxiang_list_items 的 category_id 过滤。",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "按分类名过滤。"},
             "refresh": {"type": "boolean", "description": "强制重建索引。"},
         }}),

    tool("fuxiang_catalog_export",
         "把全量注册表导出为 JSON 文件到 tools/fuxiang/registry/"
         "（component_types.json / items.json / enums.json / summary.json），"
         "便于离线检索与版本比对。返回文件清单与字节数。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_catalog_items",
         "【全量枚举物件】逐个列出物件目录（默认全 3963 项，含未在编辑器列表显示的记录）。"
         "每项带中文名、编辑器分类、组件类型与实现类、模型路径、占用值，"
         "以及 in_object_list（是否在编辑器「全部对象」里可见）与 hidden_reason。"
         "limit=0 一次取回全部，用于离线核对或批量筛选；"
         "visibility 区分「列表可见 / 原生隐藏记录 / 被当前编辑模式屏蔽的分类」。",
         {"type": "object", "properties": {
             "query": {"type": "string",
                       "description": "关键字，匹配 item_id / 中文名 / item_tid / 组件类型 / 模型路径。"},
             "category_id": {"type": ["integer", "string"],
                             "description": "按编辑器分类过滤（见 fuxiang_categories）。"},
             "group_id": {"type": ["integer", "string"],
                          "description": "按分类的一级分组过滤（如 1=角色、2=功能）。"},
             "component_type": {"type": ["integer", "string"], "description": "按组件类型编号过滤。"},
             "item_type": {"type": ["integer", "string"],
                           "description": "按物件大类过滤：1=场景 2=全局/功能 3=UI 10=限制区域。"},
             "visibility": {"type": "string", "enum": ["all", "listed", "hidden", "ui_hidden"],
                            "description": "all=全部（默认）；listed=编辑器列表可见；"
                                           "hidden=原生 is_visible 隐藏记录；"
                                           "ui_hidden=因当前编辑模式分类屏蔽而不显示。"},
             "limit": {"type": "integer", "description": "返回条数，默认 100；0 表示全部。"},
             "offset": {"type": "integer", "description": "分页偏移，默认 0。"},
             "refresh": {"type": "boolean", "description": "强制重建索引。"},
         }}),

    tool("fuxiang_catalog_verify",
         "【全量可达性自检】一次调用遍历全部物件与全部组件类型，验证每个 item_id 与 "
         "component_type 都能被接口命中，并列出：无法置入目录的组件类型（orphan）、"
         "目录里存在但无实现类的编号、没有官方中文文案的物件、"
         "以及当前编辑模式的分类屏蔽状态。complete=true 表示 100% 可达。"
         "需要在报告或交付中给出「所有组件和物体都可通过接口调用」的机器可核验证据时用本工具。",
         {"type": "object", "properties": {}}),

    # ---- 资源大厅（组合库）----
    tool("fuxiang_resource_hall",
         "【资源大厅总览】「资源大厅」在编辑器里就是**组合（Composite）库**：内含官方（PGC）"
         "与玩家自制的组合物体。本工具返回大厅结构：三个分类（官方/玩家分享/我的）、"
         "五种排序、组合标签（含中文名）、两个库（local/shared）的当前条数与加载状态、"
         "排行榜 key、记录字段表，以及数据下发通道。"
         "想「看看大厅里有什么、结构长什么样」先用本工具。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_resource_hall_items",
         "【资源大厅清单】枚举大厅里的组合物体记录（官方 PGC + 玩家自制）。每条返回："
         "组合 ID、名称与中文名、描述与中文描述、作者（uid/昵称）、标签、分享码、"
         "下载量、占用值、封面 URL，以及组合本体包含的对象数量。"
         "官方条目可通过 official_only 单独筛出。",
         {"type": "object", "properties": {
             "library": {"type": "string", "enum": ["all", "local", "shared"],
                         "description": "all=全部（默认）；shared=大厅/已分享；local=我保存的。"},
             "tag": {"type": ["integer", "string"],
                     "description": "按组合标签过滤（见 fuxiang_resource_hall 的 tags）。"},
             "official_only": {"type": "boolean", "description": "只返回官方（PGC）条目。"},
             "query": {"type": "string",
                       "description": "关键字，匹配 ID/中英文名/作者/标签/分享码。"},
             "limit": {"type": "integer", "description": "返回条数，默认 200；0 表示全部。"},
             "offset": {"type": "integer", "description": "分页偏移。"},
         }}),

    tool("fuxiang_resource_hall_item",
         "【组合详情】按 combo_id 或分享码取单条组合物体，并把组合本体 combo_data 拆解为："
         "root / child / motion 三段，child 每项含 item_id、ugc_type 与该物件的全部属性参数。"
         "加 with_param_detail=true 会把参数 id 解析到属性表条目。"
         "用它看清一个官方/玩家组合到底由哪些物件拼成。",
         {"type": "object", "properties": {
             "combo_id": {"type": ["integer", "string"], "description": "组合 ID。"},
             "share_code": {"type": "string", "description": "分享码（与 combo_id 二选一）。"},
             "with_param_detail": {"type": "boolean",
                                   "description": "把参数 id 解析到 ugc_item_property 条目。"},
         }}),

    tool("fuxiang_resource_hall_verify",
         "【资源大厅自检】遍历两个库里的全部组合记录，检查记录结构是否符合 "
         "UGC_COMBO_KEY 字段表、combo_data 是否可解析，并统计官方/玩家条目数。"
         "complete 只表示结构可解析（服务端按需下发字段，缺 like_cnt/score/limited_* 属正常）。"
         "库为空通常意味着尚未在游戏内打开过资源大厅。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_resource_hall_ui",
         "【资源大厅实况】读取**游戏内已打开**的资源大厅列表页状态：当前页签（官方推荐/"
         "组合分享/我的组合）、排序、标签、搜索词、管理模式，以及三个页签各自的中文名与"
         "可选排序、十个标签的中文名，并给出当前已渲染的卡片数量。"
         "加 include_cards=true 一并返回卡片清单。"
         "⚠️ 卡片只存在于大厅打开期间，未打开时返回 open=false。",
         {"type": "object", "properties": {
             "include_cards": {"type": "boolean", "description": "同时返回卡片清单。"},
             "with_full_doc": {"type": "boolean",
                               "description": "卡片附带完整组合记录（体积较大）。"},
         }}),

    tool("fuxiang_resource_hall_cards",
         "【资源清单】列出资源大厅**当前页签**下的资源卡片：组合 ID、中文名、分享码、"
         "占用值、下载量、点赞/评分、作者、标签（中文）、封面 URL、是否已下载、是否推荐。"
         "支持中文关键字搜索（如 query=桌）、标签过滤、只看已下载/推荐。"
         "limit=0 返回全部（官方页签实测 376 条）。",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "关键字，匹配中文名/ID/作者/标签/分享码。"},
             "tag": {"type": ["integer", "string"], "description": "按标签 id 过滤。"},
             "recommend_only": {"type": "boolean", "description": "只看推荐条目。"},
             "downloaded_only": {"type": "boolean", "description": "只看已下载条目。"},
             "with_full_doc": {"type": "boolean", "description": "附带完整组合记录。"},
             "limit": {"type": "integer", "description": "返回条数，默认 200；0 表示全部。"},
             "offset": {"type": "integer", "description": "分页偏移。"},
         }}),

    tool("fuxiang_resource_hall_export",
         "【资源清单快照】把资源大厅当前页签的完整卡片清单导出为 JSON 到 "
         "tools/fuxiang/registry/（默认按页签名命名，如 resource_hall_OFFICIAL.json），"
         "便于离线检索与版本比对。返回文件路径与字节数。",
         {"type": "object", "properties": {
             "filename": {"type": "string", "description": "可选自定义文件名。"},
         }}),

    # ---- 可编辑物体的数值修改 ----
    tool("fuxiang_edit_targets",
         "【可编辑对象清单】列出场景中所有组件对象及其**可编辑性判定**：ugc_id、owner_uid、"
         "组件类型/实现类、item_id、当前变换，以及不能编辑的具体原因"
         "（实体已不在场景 / 待删除 / 未在编辑器登记集合 / 实体类型不支持 UGC 变换接口，如角色类）。"
         "改任何数值前先用它确认目标可改。",
         {"type": "object", "properties": {
             "component_type": {"type": ["integer", "string"], "description": "按组件类型过滤。"},
             "item_id": {"type": ["integer", "string"], "description": "按物件 id 过滤。"},
             "limit": {"type": "integer", "description": "返回条数，默认 200；0 表示全部。"},
             "offset": {"type": "integer", "description": "分页偏移。"},
         }}),

    tool("fuxiang_edit_target",
         "【对象详情】定位单个对象，返回它的变换与**属性袋**（component_info.events）。"
         "加 with_schema=true 会把该组件类型的属性定义表联表返回：每个 property_id 的当前值、"
         "对应的 event_type 与应用方法、property_type 编码、取值范围等 —— "
         "这是「这个对象的哪个数值可以改、改成什么」的权威依据。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "对象 ugc_id。"},
             "owner_uid": {"type": ["integer", "string"], "description": "或按实体 uid 定位。"},
             "with_schema": {"type": "boolean", "description": "联表属性定义（默认 false）。"},
         }}),

    tool("fuxiang_edit_transform",
         "【改位置/旋转/缩放】修改**已存在**对象的位置、旋转、缩放（度 / 世界坐标）。"
         "会同时更新渲染、碰撞与逻辑位置，并登记进编辑器保存变更集。"
         "写入后自动回读校验，不一致会**自动回滚**并如实返回。"
         "dry_run=true 只预览不写。持久化仍需在游戏内保存后重进核实。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "对象 ugc_id（推荐）。"},
             "owner_uid": {"type": ["integer", "string"], "description": "或按实体 uid。"},
             "position": {"type": "array", "items": {"type": "number"},
                          "minItems": 3, "maxItems": 3, "description": "[x,y,z] 世界坐标。"},
             "rotation": {"type": "array", "items": {"type": "number"},
                          "minItems": 3, "maxItems": 3, "description": "[rx,ry,rz] 欧拉角，单位度。"},
             "scale": {"type": ["number", "array"], "description": "缩放，数值或 [x,y,z]。"},
             "dry_run": {"type": "boolean", "description": "只预览不写入。"},
             "persist_via_recreate": {"type": "boolean",
                                      "description": "true=改用「删除原对象+按目标变换重摆」的 add_unit 管线来持久化位置"
                                                     "（就地 edit_transform 不经服务端实时位置同步，重载会被旧值拉回）。"
                                                     "代价：新对象 ugc_id 会变化。", "default": False},
         }}),

    tool("fuxiang_edit_props",
         "【改属性数值】修改对象的组件属性袋（component_info.events）。props 形如 "
         "{\"property_id\": 值}，property_id 从 fuxiang_edit_target(with_schema=true) 取；"
         "remove 传入要清除的 property_id 列表。写入前会校验属性是否属于该组件类型的 schema"
         "（未知名可直接拒绝），写入后回读校验、不一致自动回滚，并登记保存变更集。"
         "支持 dry_run 预览。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "对象 ugc_id（推荐）。"},
             "owner_uid": {"type": ["integer", "string"], "description": "或按实体 uid。"},
             "props": {"type": "object", "description": "{property_id: 值} 要写入的属性。"},
             "remove": {"type": "array", "items": {"type": ["integer", "string"]},
                        "description": "要清除的属性 id 列表。"},
             "sync_keys": {"type": "array", "items": {"type": "string"},
                           "description": "可选：需要额外刷新的内部属性名（触发面板回调）。"},
             "allow_unknown_property": {"type": "boolean",
                                        "description": "允许写入 schema 中不存在的属性 id（默认 false）。"},
             "dry_run": {"type": "boolean", "description": "只预览不写入。"},
         }}),

    tool("fuxiang_edit_history",
         "【改动记录】查看本会话经接口做过的数值改动（变换 / 属性），含前后值。"
         "最多保留 100 条，便于复盘与核对。",
         {"type": "object", "properties": {
             "limit": {"type": "integer", "description": "返回最近多少条，默认 20。"},
         }}),

    # ---- 预览渲染（不人工进编辑器也能出图）----
    tool("fuxiang_preview_plan",
         "【预览·规划】只算不拍：统计地图里可参与取景的物件，算出包围盒与推荐视角列表"
         "（top 正俯视 + iso_ne/nw/se/sw 四角斜视）。"
         "会自动剔除三类「看不见的东西」：界面控件、逻辑类未落位组件（挂在 y=-65536）、"
         "以及目录分组为 global/ui 的全局组件（挂在原点，会把包围盒撑到整张地图）。"
         "fit=content 只框实际摆放的道具、fit=map 框整张地图边界、fit=union 取并集（默认）。"
         "返回的 views 可直接喂给 fuxiang_preview_shot。",
         {"type": "object", "properties": {
             "fit": {"type": "string", "enum": ["content", "map", "union"],
                     "description": "取景范围，默认 union。"},
             "views": {"type": "array", "items": {"type": "string"},
                       "description": "需要的视角名，默认全部（top/iso_ne/iso_nw/iso_se/iso_sw）。"},
             "margin": {"type": "number", "description": "取景外扩系数，默认 1.18。"},
             "fov": {"type": "number", "description": "斜视图竖直视角（度），默认 55。"},
             "elevation": {"type": "number", "description": "斜视仰角（度），默认 35。"},
             "dist_mul": {"type": "number",
                          "description": "斜视距离倍率，默认 1.0；<1 拉近看细节，>1 拉远看得更全。"},
             "span_mul": {"type": "number", "description": "正俯视正交跨度倍率，默认 1.0。"},
             "aspect": {"type": "number", "description": "视口宽高比，默认 1.6（1920x1200）。"},
         }}),

    tool("fuxiang_preview_shot",
         "【预览·出图】把编辑器相机摆到指定视角，让引擎渲染一帧后导出 PNG，"
         "然后（可选）还原相机。出图靠引擎自己的帧循环，不介入渲染管线。"
         "用法：先 fuxiang_preview_plan 拿 views，把其中一个 view 传进来即可。"
         "内部按「摆位 → 等 2 帧 → 出图 → 轮询落盘」执行；引擎是异步写文件的，"
         "本工具会等到文件确实落盘才返回。返回真实文件路径与字节数。",
         {"type": "object", "properties": {
             "view": {"description": "视角名（如 top / iso_ne）或视角对象（plan 返回的 views 元素）。"},
             "fit": {"type": "string", "enum": ["content", "map", "union"],
                     "description": "view 传视角名时的取景范围，默认 content。"},
             "out": {"type": "string", "description": "输出 PNG 绝对路径，默认写到 tools/fuxiang/preview/。"},
             "restore": {"type": "boolean", "description": "出图后是否立刻还原相机，默认 false。"},
             "wait": {"type": "number", "description": "摆位与出图之间的等待秒数，默认 0.45。"},
             "timeout": {"type": "number", "description": "等待文件落盘的秒数，默认 12。"},
             "margin": {"type": "number", "description": "取景外扩系数。"},
             "fov": {"type": "number", "description": "斜视图竖直视角（度）。"},
             "elevation": {"type": "number", "description": "斜视仰角（度）。"},
             "dist_mul": {"type": "number", "description": "斜视距离倍率。"},
             "span_mul": {"type": "number", "description": "正俯视正交跨度倍率。"},
         }, "required": ["view"]}),

    tool("fuxiang_preview_sweep",
         "【预览·全套】一键拍一整套：自动规划视角 → 逐个摆位出图 → 默认把相机还原。"
         "每个视角一张 PNG，并写一份 manifest.json 记录视角、路径、字节数与还原结果。"
         "这是「不进编辑器人工看场景」的主入口。",
         {"type": "object", "properties": {
             "fit": {"type": "string", "enum": ["content", "map", "union"],
                     "description": "取景范围，默认 content。"},
             "views": {"type": "array", "items": {"type": "string"},
                       "description": "要拍的视角名，默认 top + 四角斜视。"},
             "out_dir": {"type": "string", "description": "输出目录，默认 tools/fuxiang/preview/shots/。"},
             "prefix": {"type": "string", "description": "文件名前缀，默认 preview。"},
             "restore": {"type": "boolean", "description": "拍完是否还原相机，默认 true。"},
             "wait": {"type": "number", "description": "每张图摆位后的等待秒数，默认 0.45。"},
             "fov": {"type": "number", "description": "斜视图竖直视角（度）。"},
             "elevation": {"type": "number", "description": "斜视仰角（度）。"},
             "dist_mul": {"type": "number", "description": "斜视距离倍率。"},
             "span_mul": {"type": "number", "description": "正俯视正交跨度倍率。"},
         }}),

    tool("fuxiang_preview_layout",
         "【预览·平面图】导出 2D 俯视平面图 PNG（路线 C）。只读组件数据，"
         "在主机侧用 Pillow 绘制：坐标网格、地图边界框、每个空间物件的旋转标记与编号、"
         "右侧物件明细表，以及非空间组件清单。"
         "与 fuxiang_preview_sweep 互补：sweep 给「像游戏里那样」的渲染图，"
         "本工具给「一次看全图」的布局图，且完全不改动相机、零崩溃风险。",
         {"type": "object", "properties": {
             "fit": {"type": "string", "enum": ["content", "map", "union"],
                     "description": "画布取景范围，默认 union。"},
             "out": {"type": "string", "description": "输出 PNG 绝对路径，默认写到 tools/fuxiang/preview/。"},
             "width": {"type": "integer", "description": "画布宽度，默认 1800。"},
             "height": {"type": "integer", "description": "画布高度，默认 1200。"},
             "panel_width": {"type": "integer", "description": "右侧明细面板宽度，默认 520。"},
         }}),

    tool("fuxiang_preview_restore",
         "【预览·还原】把编辑器相机还原到本会话第一次摆位前的姿态，并回读校验偏差。"
         "fuxiang_preview_shot / _sweep 若中断，用它收拾现场。",
         {"type": "object", "properties": {
             "clear": {"type": "boolean", "description": "还原后是否清除备份，默认 true。"},
         }}),

    tool("fuxiang_preview_state",
         "【预览·诊断】报告相机姿态（位置/朝向/FOV/投影模式）、备份是否存在、"
         "已摆位与已出图次数、以及出图接口是否可用。排障先看它。",
         {"type": "object", "properties": {}}),

    # ---- 组合下载（资源大厅 → 玩家账号库；写操作）----
    tool("fuxiang_combo_download_status",
         "【下载·诊断】查看组合下载能力与限制：落点库（分享/已下载）、"
         "库容量上限与当前占用、并发约束、待确认请求与最近下载记录。"
         "**发起下载前先看它**，确认库没满。",
         {"type": "object", "properties": {}}),

    tool("fuxiang_combo_download",
         "【下载一个组合】按分享码把资源大厅里的一个组合下载到你的账号组合库。"
         "⚠️ 这是**服务端写操作**：会真实改动你的账号数据，且本接口**不提供回滚**"
         "（删除需在游戏内资源大厅的管理模式里做）。"
         "限制：① 组合库上限 200 条（`ugc_max_shared_combo_num`），满了会被拒；"
         "② 一次只允许一笔在途请求；③ 服务端为异步处理，实测约 15–40 秒才回包。"
         "调用后立即返回，须用 fuxiang_combo_download_poll 轮询结果。"
         "本工具**只接受单条 share_code**，不支持批量。",
         {"type": "object", "properties": {
             "share_code": {"type": "string",
                            "description": "组合分享码，取自 fuxiang_resource_hall_cards 的 share_code。"},
         }, "required": ["share_code"]}),

    tool("fuxiang_combo_download_poll",
         "【下载·轮询】查询在途下载的结果。返回 downloaded（含新增组合 id、名称、"
         "是否已带 combo_data）/ pending（继续轮询同一笔）/ unknown（超时，结果未知，"
         "不会自动重发）。不要在 pending 期间重复发起下载。",
         {"type": "object", "properties": {}}),

    # ---- 从场景删除对象（原生命令层）----
    tool("fuxiang_edit_delete_preview",
         "【删除·预览】只读：解析目标并返回**将被删除的对象清单**（含组合级联预估）、"
         "被护栏拦下的目标及原因、当前占用值与组件数。"
         "删除是破坏性操作，执行前先用它核对。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "对象 ugc_id。"},
             "ugc_ids": {"type": "array", "items": {"type": ["integer", "string"]},
                         "description": "批量（最多 50 个）。"},
             "owner_uid": {"type": ["integer", "string"], "description": "或按实体 uid 定位。"},
             "allow_unregistered": {"type": "boolean",
                                    "description": "允许删除不在编辑器登记集合里的历史遗留物（默认 false）。"},
         }}),

    tool("fuxiang_edit_delete",
         "【删除对象】从场景中删除已存在对象，走**游戏原生命令层**"
         "（_do_normal_del_commands）：自动处理组合级联、占用刷新、撤销栈与变更登记，"
         "与手动删除等价，游戏内 Ctrl+Z 可撤销。"
         "护栏：只删编辑器已登记对象；拒绝待删除对象；**不支持按 item_id/位置模糊圈选**；"
         "单次最多 50 个；部分目标不可删时默认整体拒绝（可传 skip_rejected=true 只删可删的）。"
         "删除后回读校验，未删掉的会如实列入 still_remaining 并置 ok=false。"
         "⚠️ 强烈建议先 fuxiang_edit_delete_preview 核对清单。"
         "持久化需在游戏内保存后重进核实。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"], "description": "对象 ugc_id。"},
             "ugc_ids": {"type": "array", "items": {"type": ["integer", "string"]},
                         "description": "批量（最多 50 个）。"},
             "owner_uid": {"type": ["integer", "string"], "description": "或按实体 uid。"},
             "allow_unregistered": {"type": "boolean",
                                    "description": "允许删除未登记的历史遗留物（默认 false）。"},
             "skip_rejected": {"type": "boolean",
                               "description": "部分目标不可删时，只删除可删的那些（默认 false，整体拒绝）。"},
             "dry_run": {"type": "boolean", "description": "只预览不写入。"},
         }}),

    # ---- 剧情编辑（STORY 10007001 / DIALOGUE 10007002 / CHOICE 10007003）----
    tool("fuxiang_story_fields",
         "【剧情编辑·字典】返回剧情/对话/选项三类节点的可写字段：语义名、property_id、"
         "值类型（text/int/str/color）、中文名与允许取值。写文件前先查它确认字段名与取值。",
         {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["story", "dialogue", "choice"],
                      "description": "只看某一类（可省=全部）。"},
         }}),

    tool("fuxiang_story_tree",
         "【剧情编辑·读取】把当前地图里的剧情读成 剧情→对话→选项 三层树："
         "对话按 sort_id 排序挂在 story_id 指向的剧情下，选项挂在 dialogue_id 指向的对话下；"
         "挂不上父节点的进 orphan_*（父节点被删或 story_id/dialogue_id 没写）。"
         "这是「导出剧情」的读端，也是写完回读校验的手段。",
         {"type": "object", "properties": {
             "compact": {"type": "boolean", "description": "只返回条数概要（默认 false）。"},
             "kind": {"type": "string", "enum": ["story", "dialogue", "choice"]},
             "include_deleted": {"type": "boolean",
                                 "description": "含本会话已删除节点的索引残骸（默认跳过）。"},
         }}),

    tool("fuxiang_story_create",
         "【剧情编辑·创建】发起创建 1 个剧情节点（story/dialogue/choice），异步："
         "返回 request_id 后用 fuxiang_story_create_poll 查询，placed 时会自动落 fields。"
         "⚠️ 走服务端路径，同一时刻只允许一个在途请求。",
         {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["story", "dialogue", "choice"]},
             "fields": {"type": "object",
                        "description": "初始字段，如 {\"name\":\"序章\",\"enable_quit\":1}"
                                       " / {\"story_id\":816,\"sort_id\":1,\"content\":\"…\"}"},
             "request_id": {"type": "string", "description": "可省，自动生成。"},
         }, "required": ["kind"]}),

    tool("fuxiang_story_create_poll",
         "【剧情编辑·创建】查询创建请求；placed 后自动落字段并登记保存变更集。",
         {"type": "object", "properties": {
             "request_id": {"type": "string"},
         }, "required": ["request_id"]}),

    tool("fuxiang_story_set",
         "【剧情编辑·写入】改一个剧情节点的字段（三步法：写属性袋 → 调原生 _init_* → "
         "登记变更集，并回读校验，不一致会回滚）。⚠️ 改完要在游戏内保存才落库。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"]},
             "fields": {"type": "object", "description": "{字段名: 值}，见 fuxiang_story_fields。"},
             "kind": {"type": "string", "enum": ["story", "dialogue", "choice"],
                      "description": "可选校验：类型不符会拒绝。"},
             "dry_run": {"type": "boolean", "description": "只返回将要写入的 events。"},
         }, "required": ["ugc_id", "fields"]}),

    tool("fuxiang_story_delete",
         "【剧情编辑·删除】删除剧情节点，cascade=true 连带子节点（剧情→对话→选项）。"
         "走编辑器原生命令层（可 Ctrl+Z 撤销）。剧情三件套不在编辑器登记集合里，"
         "必须传 allow_unregistered=true。⚠️ 索引残骸不会被清理，本接口的剧情树会自动跳过。",
         {"type": "object", "properties": {
             "ugc_id": {"type": ["integer", "string"]},
             "ugc_ids": {"type": "array", "items": {"type": ["integer", "string"]}},
             "cascade": {"type": "boolean"},
             "allow_unregistered": {"type": "boolean"},
             "dry_run": {"type": "boolean"},
         }}),

    tool("fuxiang_story_export",
         "【剧情编辑·导出】把当前地图的剧情导出成文件（json=权威格式可回读；"
         "md=方便人读写的 Markdown，同样可回读）。默认落在 tools/fuxiang/projects/story/。",
         {"type": "object", "properties": {
             "path": {"type": "string",
                      "description": "输出路径；相对路径按 tools/fuxiang/ 解析。可省=自动生成。"},
             "format": {"type": "string", "enum": ["json", "md"],
                        "description": "可省=按扩展名判断，默认 json。"},
         }}),

    tool("fuxiang_story_import",
         "【剧情编辑·导入】拿剧情文件与当前地图比对，给出 create/update/delete/unchanged 计划。"
         "dry_run（默认 true）只给计划；dry_run=false 会立即执行 update（和 prune 下的 delete），"
         "然后发起第一个 create——创建是异步的，之后用 fuxiang_story_import_next 逐个推进。",
         {"type": "object", "properties": {
             "path": {"type": "string",
                      "description": "json 或 md 文件；相对路径按 tools/fuxiang/ 解析。"},
             "dry_run": {"type": "boolean", "description": "默认 true。"},
             "prune": {"type": "boolean",
                       "description": "删除文件里没有的剧情节点（谨慎）。"},
             "match_by": {"type": "string", "enum": ["ugc_id", "name"], "description": "默认 ugc_id。"},
         }}),

    tool("fuxiang_story_import_next",
         "【剧情编辑·导入】推进导入计划：查询当前创建请求，placed 则落字段（父节点是本次新建的"
         "会自动补上新 ugc_id），然后发起下一个，直到 done=true。",
         {"type": "object", "properties": {}}),
])

TOOL_MAP.update({
    "fuxiang_catalog": ("catalog_summary", ["refresh"]),
    "fuxiang_find": ("catalog_find", ["item_id", "component_type", "query", "enum",
                                      "kind", "limit", "offset", "refresh"]),
    "fuxiang_component_detail": ("catalog_type", ["component_type", "with_schema",
                                                  "item_limit", "refresh"]),
    "fuxiang_component_schema": ("catalog_schema", ["component_type"]),
    "fuxiang_enums": ("catalog_enums", ["query"]),
    "fuxiang_categories": ("catalog_categories", ["query", "refresh"]),
    "fuxiang_catalog_export": ("catalog_export", []),
    "fuxiang_catalog_items": ("catalog_items", ["query", "category_id", "group_id",
                                                "component_type", "item_type", "visibility",
                                                "limit", "offset", "refresh"]),
    "fuxiang_catalog_verify": ("catalog_verify", []),
    "fuxiang_resource_hall": ("hall_info", []),
    "fuxiang_resource_hall_items": ("hall_items", ["library", "tag", "official_only",
                                                   "query", "limit", "offset"]),
    "fuxiang_resource_hall_item": ("hall_item", ["combo_id", "share_code",
                                                 "with_param_detail"]),
    "fuxiang_resource_hall_verify": ("hall_verify", []),
    "fuxiang_resource_hall_ui": ("hall_ui", ["include_cards", "with_full_doc"]),
    "fuxiang_resource_hall_cards": ("hall_cards", ["query", "tag", "recommend_only",
                                                   "downloaded_only", "with_full_doc",
                                                   "limit", "offset"]),
    "fuxiang_resource_hall_export": ("hall_export", ["filename"]),
    "fuxiang_edit_targets": ("edit_targets", ["component_type", "item_id", "limit", "offset"]),
    "fuxiang_edit_target": ("edit_target", ["ugc_id", "owner_uid", "with_schema"]),
    "fuxiang_edit_transform": ("edit_transform", ["ugc_id", "owner_uid", "position",
                                                  "rotation", "scale", "dry_run",
                                                  "persist_via_recreate"]),
    "fuxiang_edit_props": ("edit_props", ["ugc_id", "owner_uid", "props", "remove",
                                          "sync_keys", "allow_unknown_property", "dry_run"]),
    "fuxiang_edit_history": ("edit_history", ["limit"]),
    # 预览渲染：实际分派见 handle_preview_call（需要跨帧等待与文件轮询，
    # 不是简单透传，故这里只登记名称以便通过参数校验）
    "fuxiang_preview_plan": ("preview_plan", None),
    "fuxiang_preview_shot": ("preview_shot", None),
    "fuxiang_preview_sweep": ("preview_sweep", None),
    "fuxiang_preview_layout": ("preview_layout", None),
    "fuxiang_preview_restore": ("preview_restore", None),
    "fuxiang_preview_state": ("preview_state", None),
    "fuxiang_combo_download_status": ("combo_download_status", []),
    "fuxiang_combo_download": ("combo_download", ["share_code"]),
    "fuxiang_combo_download_poll": ("combo_download_poll", []),
    "fuxiang_edit_delete_preview": ("delete_preview",
                                    ["ugc_id", "ugc_ids", "owner_uid", "allow_unregistered"]),
    "fuxiang_edit_delete": ("delete_objects",
                            ["ugc_id", "ugc_ids", "owner_uid", "allow_unregistered",
                             "skip_rejected", "dry_run"]),
    # 剧情编辑
    "fuxiang_story_fields": ("story_fields", ["kind"]),
    "fuxiang_story_tree": ("story_tree", ["compact", "kind", "include_deleted"]),
    "fuxiang_story_create": ("story_create", ["kind", "fields", "request_id"]),
    "fuxiang_story_create_poll": ("story_create_poll", ["request_id"]),
    "fuxiang_story_set": ("story_set", ["ugc_id", "fields", "kind", "dry_run"]),
    "fuxiang_story_delete": ("story_delete", ["ugc_id", "ugc_ids", "cascade",
                                              "allow_unregistered", "dry_run"]),
    "fuxiang_story_export": ("story_export", ["path", "format"]),
    "fuxiang_story_import": ("story_import", ["path", "dry_run", "prune", "match_by"]),
    "fuxiang_story_import_next": ("story_import_next", []),
})


# --------------------------------------------------------------------------
# 预览渲染分派
#
# 与其它工具不同，预览需要"摆位 → 跨帧等待 → 出图 → 轮询落盘"的多步编排，
# 等待必须发生在**本进程**而不是游戏主线程（项目纪律：主线程只做短操作）。
# 具体实现在 mcp/preview.py。
# --------------------------------------------------------------------------
def handle_preview_call(name, arguments):
    import preview as preview_mod

    action = name[len("fuxiang_preview_"):]
    fn = {
        "plan": preview_mod.plan,
        "state": preview_mod.state,
        "restore": preview_mod.restore,
        "shot": preview_mod.shot,
        "sweep": preview_mod.sweep,
        "layout": preview_mod.layout,
    }.get(action)
    if fn is None:
        raise BridgeError("未知预览动作: %s" % action)
    return fn(call_edit, arguments)


def handle_tool_call(name, arguments):
    if name not in TOOL_MAP:
        raise BridgeError("未知工具: %s" % name)
    if not isinstance(arguments, dict):
        raise BridgeError("arguments 必须为对象")
    schema = next(t['inputSchema'] for t in TOOLS if t['name'] == name)
    missing = set(schema.get('required', [])) - set(arguments)
    unknown = set(arguments) - set(schema.get('properties', {}))
    if missing or unknown:
        raise BridgeError("参数错误：缺少 %s，未知 %s" % (sorted(missing), sorted(unknown)))
    if name.startswith("fuxiang_preview_"):
        value = handle_preview_call(name, arguments)
        return [{"type": "text",
                 "text": json.dumps(value, ensure_ascii=False, indent=1, default=str)}]
    op, keys = TOOL_MAP[name]
    if name == "fuxiang_eval":
        args = {"code": arguments.get("code", "")}
    else:
        args = {k: arguments[k] for k in keys if k in arguments}
    value = call_edit(op, args)
    return [{"type": "text",
             "text": json.dumps(value, ensure_ascii=False, indent=1, default=str)}]


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------
def reply(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_reply(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def process(req):
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            content = handle_tool_call(name, arguments)
            return reply(msg_id, {"content": content, "isError": False})
        except Exception as e:
            return reply(msg_id, {
                "content": [{"type": "text",
                             "text": "ERROR: %s" % (e,)}],
                "isError": True,
            })
    if method == "ping":
        return reply(msg_id, {})
    if method in ("shutdown",):
        return reply(msg_id, {})
    if msg_id is None:
        return None   # 通知类，忽略
    return error_reply(msg_id, -32601, "method not found: %s" % method)


MCP_ENABLE_ENV = "FUXIANG_ENABLE_MCP"


def main():
    # 默认停用：与 cli/ 能力等价的第二条通道，保留只会增加维护漂移点。
    if os.environ.get(MCP_ENABLE_ENV) != "1":
        sys.stderr.write(
            "MCP stdio 服务已停用（2026-09-22 通道收敛）。\n"
            "编辑请改用 CLI：python cli/fxedit.py <op> [json_args]\n"
            "预览出图：python cli/fxshot.py plan|shot|sweep|restore\n"
            "确需临时启用：PowerShell $env:%s=1 ／ cmd set %s=1\n"
            % (MCP_ENABLE_ENV, MCP_ENABLE_ENV))
        sys.stderr.flush()
        return 2
    # 让 stdout 纯粹承载协议
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            out = process(req)
        except Exception:
            out = error_reply(req.get("id"), -32603, traceback.format_exc()[-2000:])
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
