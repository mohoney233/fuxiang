# 随笔浮想编辑器 HTTP API 文档

> 《第五人格》「随笔浮想」编辑器对外 HTTP/JSON 接口汇总。
> 本服务只是 **MCP（`mcp/server.py`）同一分发层的 HTTP 薄封装**：两者走同一条桥（`ipc/cmd`+`ipc/out` → 游戏内 `fuxiang_bridge` → 主线程），功能与参数完全一致。
> MCP 面向智能体（自动发现工具/schema），本 HTTP API 面向脚本、curl 与一般程序。**二者并存互不冲突。**

---

## 一、为什么封装成 HTTP

- MCP stdio 对**外部脚本/程序**不友好：每次调用都要起一个子进程走 `initialize`+`tools/call`，无持久会话、无法并发。
- 封装成**常驻 HTTP/JSON 服务**（默认仅本机 `127.0.0.1`）后，`curl` 与任意语言可直接 `POST /api/...`，消除“每次起子进程”的最大开销；真实延迟恰是游戏侧文件握手的下限，与 MCP 相同。

**速度/风险结论**：已封装的 host 侧 HTTP 是“方便 + 够快 + 低风险”的最优平衡；更极致的“游戏进程内直连”需在注入桥里开网络监听，风险明显更高，未采用。

---

## 二、启动与前置条件

前置：游戏已进入「随笔浮想」编辑器，且桥已安装（首次以管理员身份运行）。

```powershell
& '...\fuxiang-inject.exe' install            # 装桥（每次进编辑器进程后一次）
& $py tools/fuxiang/mcp/http_server.py --port 17699   # 启动 HTTP 服务
```

- 运行环境：`<你的-python.exe-完整路径>`（Python 3.12）。
- 默认 `127.0.0.1:17699`；`--host` 可监听更多网卡（非本机即存在被访问风险，谨慎）。
- 退出：`Ctrl+C`；常驻时 `GET /api/health` 检查桥在线。

**配套命令行客户端**（等价 curl，自动格式化 JSON）：

```powershell
& $py tools/fuxiang/cli/fxhttp.py health
& $py tools/fuxiang/cli/fxhttp.py tools
& $py tools/fuxiang/cli/fxhttp.py call <tool_name> '{"query":"桌子"}'
& $py tools/fuxiang/cli/fxhttp.py op <op> '{"limit":5}'
```

**curl 示例**：

```bash
curl -s http://127.0.0.1:17699/api/health
curl -s -X POST http://127.0.0.1:17699/api/tools/fuxiang_status
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"query":"桌子","limit":3}' http://127.0.0.1:17699/api/tools/fuxiang_list_items
```

---

## 三、HTTP 基础约定

| 项 | 约定 |
|---|---|
| Base URL | `http://127.0.0.1:17699` |
| 编码 | 请求/响应均为 `application/json; charset=utf-8`，中文不转义 |
| 路由 | 见下节；工具调用 `POST /api/tools/<tool>`，body 为参数 JSON |
| 成功 | `HTTP 200`，`{"ok": true, "tool": ..., "value": ...}`（op 透传为 `{"ok": true, "op": ..., "value": ...}`） |
| 工具清单 | `GET /api/tools` → `{count, tools:[{name, description, inputSchema}]}`（无需桥，静态） |
| 健康检查 | `GET /api/health` → `{bridge_alive, bridge_age, bridge_installed}` |
| 自描述 | `GET /api/route` 返回本服务支持的完整路由 |

### 错误码

| HTTP | 含义 | 响应体 |
|---|---|---|
| 400 | 参数非法 / 请求体非 JSON | `{"ok": false, "error": ...}` |
| 404 | 未知工具 / op / 路由 | `{"ok": false, "error": ...}` |
| 502 | 游戏侧/桥侧执行失败（`BridgeError`） | `{"ok": false, "error": ...}` |
| 503 | 桥未装 / 心跳过期（游戏未启动或未 `install`） | `{"ok": false, "error": ..., "hint": <安装提示>}` |
| 500 | 其它异常 | `{"ok": false, "error": ...}` |

> value 若为无法 JSON 化的情形会退化为 `{"ok": true, "value":{"value_raw":"<原文本>"}}`，不丢数据。

### 并发与幂等

- 桥为**单一会话**，服务用全局锁串行化所有触桥调用；并发请求会自动排队。
- `fuxiang_place_item` / `fuxiang_combo_download` 是**异步**命令，分别用 `fuxiang_place_poll` / `fuxiang_combo_download_poll` 取结果，不要在主线程长轮询。
- 写操作建议用幂等 `request_id`/单条 `share_code`。

---

## 四、工具 ↔ op 映射表

| 工具（`POST /api/tools/<名>`） | 游戏侧 op | 只读 |
|---|---|---|
| `fuxiang_status` | `status` | ✅ |
| `fuxiang_map_info` | `map_info` | ✅ |
| `fuxiang_list_types` | `list_types` | ✅ |
| `fuxiang_list_items` | `list_items` | ✅ |
| `fuxiang_list_components` | `list_components` | ✅ |
| `fuxiang_get_component` | `get_component` | ✅ |
| `fuxiang_place_item` | `place_item` | 写 |
| `fuxiang_place_poll` | `place_poll` | — |
| `fuxiang_plan_item` | `plan_item` | ✅ |
| `fuxiang_remove_item` | `remove_item` | 写 |
| `fuxiang_set_transform` | `set_transform` | 写 |
| `fuxiang_create_component` | `create_component` | 写 |
| `fuxiang_remove_component` | `remove_component` | 写 |
| `fuxiang_repair_index` | `repair_index` | 写 |
| `fuxiang_pending_saves` | `pending_saves` | ✅ |
| `fuxiang_get_params` | `get_params` | ✅ |
| `fuxiang_set_params` | `set_params` | 写 |
| `fuxiang_create_event` | `create_event` | 写 |
| `fuxiang_save` | `save` | 写(见注) |
| `fuxiang_save_map` | `save_map` | 写 |
| `fuxiang_save_map_poll` | `save_map_poll` | — |
| `fuxiang_save_dirty` | `save_dirty` | ✅ |
| `fuxiang_eval` | `eval` | ⚠️高危 |
| `fuxiang_catalog` | `catalog_summary` | ✅ |
| `fuxiang_catalog_items` | `catalog_items` | ✅ |
| `fuxiang_catalog_verify` | `catalog_verify` | ✅ |
| `fuxiang_find` | `catalog_find` | ✅ |
| `fuxiang_component_detail` | `catalog_type` | ✅ |
| `fuxiang_component_schema` | `catalog_schema` | ✅ |
| `fuxiang_categories` | `catalog_categories` | ✅ |
| `fuxiang_enums` | `catalog_enums` | ✅ |
| `fuxiang_catalog_export` | `catalog_export` | 导出 |
| `fuxiang_resource_hall` | `hall_info` | ✅ |
| `fuxiang_resource_hall_items` | `hall_items` | ✅ |
| `fuxiang_resource_hall_item` | `hall_item` | ✅ |
| `fuxiang_resource_hall_verify` | `hall_verify` | ✅ |
| `fuxiang_resource_hall_ui` | `hall_ui` | ✅ |
| `fuxiang_resource_hall_cards` | `hall_cards` | ✅ |
| `fuxiang_resource_hall_export` | `hall_export` | 导出 |
| `fuxiang_edit_targets` | `edit_targets` | ✅ |
| `fuxiang_edit_target` | `edit_target` | ✅ |
| `fuxiang_edit_transform` | `edit_transform` | 写 |
| `fuxiang_edit_props` | `edit_props` | 写 |
| `fuxiang_edit_history` | `edit_history` | ✅ |
| `fuxiang_edit_delete_preview` | `delete_preview` | ✅ |
| `fuxiang_edit_delete` | `delete_objects` | 写 |
| `fuxiang_preview_plan` | `preview_plan`¹ | ✅ |
| `fuxiang_preview_shot` | `preview_shot`¹ | 写(相机) |
| `fuxiang_preview_sweep` | `preview_sweep`¹ | 写(相机) |
| `fuxiang_preview_layout` | `preview_layout`¹ | 导出 |
| `fuxiang_preview_restore` | `preview_restore`¹ | 写(相机) |
| `fuxiang_preview_state` | `preview_state`¹ | ✅ |
| `fuxiang_combo_download_status` | `combo_download_status` | ✅ |
| `fuxiang_combo_download` | `combo_download` | ⚠️服务端写 |
| `fuxiang_combo_download_poll` | `combo_download_poll` | — |
| `fuxiang_story_fields` | `story_fields` | ✅ |
| `fuxiang_story_tree` | `story_tree` | ✅ |
| `fuxiang_story_create` | `story_create` | 写 |
| `fuxiang_story_create_poll` | `story_create_poll` | — |
| `fuxiang_story_set` | `story_set` | 写 |
| `fuxiang_story_delete` | `story_delete` | 写 |
| `fuxiang_story_export` | `story_export` | 导出 |
| `fuxiang_story_import` | `story_import` | 写(默认 dry_run) |
| `fuxiang_story_import_next` | `story_import_next` | 写 |

> 剧情系列（STORY 10007001 / DIALOGUE 10007002 / CHOICE 10007003）的完整用法、
> 文件格式与字段表见 **[STORY_EDIT.md](STORY_EDIT.md)**；命令行入口 `cli/story.py`。

> ¹ 预览系列由 `mcp/preview.py` 主机侧编排（跨帧等待 + 文件轮询），非简单透传，故映射表中单独标注；属**长调用**，HTTP 层不占全局锁。
> `fuxiang_save` 是旧标记接口，**不等于真实地图保存**；`fuxiang_combo_download` 会真实改动账号组合库且**无回滚**。
> 原始 op 透传任督二脉：`POST /api/op/<op>`（body=args）或 `POST /api/op`（body=`{"op":..., "args":...}`），可调用上表任意游戏侧 op 或尚未封装成工具的低层 op。

---

## 五、工具分章

### A. 状态与地图

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_status` | 检查编辑器连接：游戏/核心/组件管理器 | — |
| `fuxiang_map_info` | 当前地图名称/描述/标签、区域锚点、组件数量分布 | — |

### B. 类型与物件编目（定位）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_list_types` | 全部组件类型（编号/英文/中文名/实现类/可创建性） | `query` `only` `limit` `offset` `refresh` |
| `fuxiang_list_items` | 可放置物件目录（3963 项） | `query` `item_type` `category_id` `limit` `offset` `include_hidden` |
| `fuxiang_find` | 统一定位（item_id / component_type / 名称/枚举） | `item_id` `component_type` `query` `enum` `kind` `limit` |
| `fuxiang_catalog` | 注册表概览（类型数/可创建数/物件数/支持矩阵） | `refresh` |
| `fuxiang_catalog_items` | 全量枚举物件（limit=0 全部） | `query` `category_id` `group_id` `component_type` `item_type` `visibility` `limit` `offset` |
| `fuxiang_catalog_verify` | 全量可达性自检（complete=true=100%可达） | — |
| `fuxiang_component_detail` | 单个组件类型档案（实现类/目录物件/可创建性/schema） | `component_type` `with_schema` `item_limit` |
| `fuxiang_component_schema` | 组件属性 schema（属性面板数据源） | `component_type` |
| `fuxiang_categories` | 编辑器分类树（左栏）+ 各类物件数 | `query` `refresh` |
| `fuxiang_enums` | 属性取值枚举（33 个） | `query` |
| `fuxiang_catalog_export` | 导出注册表快照到 `registry/` | — |

摆场景流程：`fuxiang_catalog` → `fuxiang_list_items` → `fuxiang_plan_item` → `fuxiang_place_item` → `fuxiang_place_poll`。

### C. 摆放与实体（写操作）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_plan_item` | 只读校验一次摆放请求（不改场景） | `item_id` `position` `rotation` `scale` `allow_unverified` |
| `fuxiang_place_item` | 异步请求创建物件，立即返回 `request_id` | `item_id` `position` `rotation` `scale` `request_id` `mode` |
| `fuxiang_place_poll` | 查询摆放结果并执行一次原生确认 | `request_id` |
| `fuxiang_remove_item` | 清理**本地预览**（服务端物件拒绝本地销毁） | `ugc_id` |
| `fuxiang_set_transform` | 修改本会话物件的变换（渲染+碰撞+逻辑位） | `ugc_id` `position` `rotation` `scale` |
| `fuxiang_get_component` | 取单个组件详情（full=true 全量） | `ugc_id` `full` `child_key` |
| `fuxiang_list_components` | 列出场景已放置组件 | `type` `limit` `offset` |
| `fuxiang_create_component` | 低层：给实体附加组件 | `info` |
| `fuxiang_remove_component` | 低层：移除组件索引 | `ugc_id` |
| `fuxiang_repair_index` | 自愈：重登记脱离索引的存活组件 | — |
| `fuxiang_pending_saves` | 本地事件重放队列（诊断） | `limit` |

> `mode=local_preview` 可显式创建不持久化的本地实体。返回一律 `persistence_verified:false`，真实保存需游戏内保存+重进核实。

**摆放相关字段（2026-09-19）**

- `fuxiang_plan_item` 返回 **`server_begin_allowed`**：直接告诉你随后 `fuxiang_place_item`
  会不会被客户端阀门拒绝（`SERVER_COMPONENT_TYPES`）。`false` 时附
  `server_begin_block_reason`（含可操作的绕法）。**它只是阀门判定，不代表服务端不受理**——
  服务端 `add_unit` 无类型白名单。
- `fuxiang_place_poll` 返回 **`entity_mode`**：
  - 缺省/`entity`：常规实体，已做变换写入与回读校验。
  - **`global_component`**：该类型（203911 运动器 / 203930 / 203920 / 203921 /
    10007001 / 200001 / 200000）服务端**只创建组件**并挂在 `UgcGlobalUnit` 上，
    **没有独立实体、不占占用值**；`transform.position` 可能是哨兵位 `[0,-65536,0]`，
    这是正常的，不要当成失败。确认方式为登记保存变更集（`_add_change_unit`）。
- **请求账本上限**：`MAX_REQUESTS = 128` 只统计**未定论**请求（pending / send_unknown /
  overdue / ambiguous / conflict / finalize_failed），**与编辑器「组件数 /15000、占用值
  /30000」无关**，1 次请求也不等于 1 个物件。已定论的请求会在下一次 `place_item` 时被
  回收进紧凑留痕表（`MAX_SETTLED = 1024` 条，FIFO），同一 `request_id` 在表内仍可幂等
  回查，绝不重发或重建。因此反复摆放不会再累积撞上限；详见 README 的摆放一节。
- `transform.rotation` / `transform.scale` 为 `null`：实体类没有 `ugc_dir`/`ugc_size`
  读属性（BoxUnit/NPC 等 21 类），回读不到；摆位仍然正确。

### D. 修改已存在对象（数值）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_edit_targets` | 列对象 + 可编辑性判定 | `component_type` `item_id` `limit` `offset` |
| `fuxiang_edit_target` | 单对象变换 + 属性袋（with_schema 联表定义） | `ugc_id` `owner_uid` `with_schema` |
| `fuxiang_edit_transform` | 改位置/旋转/缩放（回读校验+自动回滚） | `ugc_id` `owner_uid` `position` `rotation` `scale` `dry_run` `persist_via_recreate` |
| `fuxiang_edit_props` | 改组件属性（props 写 / remove 清） | `ugc_id` `owner_uid` `props` `remove` `allow_unknown_property` `dry_run` |
| `fuxiang_edit_history` | 本会话改动记录 | `limit` |

### D2. 剧情编辑（STORY / DIALOGUE / CHOICE）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_story_fields` | 字段字典：语义名/property_id/允许值 | `kind` |
| `fuxiang_story_tree` | 读 剧情→对话→选项 三层树 | `compact` `kind` `include_deleted` |
| `fuxiang_story_create` | 发起创建节点（异步） | `kind` `fields` `request_id` |
| `fuxiang_story_create_poll` | 查询创建；placed 自动落字段 | `request_id` |
| `fuxiang_story_set` | 改字段（三步法+回读+回滚） | `ugc_id` `fields` `kind` `dry_run` |
| `fuxiang_story_delete` | 删节点（可级联，需 allow_unregistered） | `ugc_id` `ugc_ids` `cascade` `dry_run` |
| `fuxiang_story_export` | 导出 json / md 文件 | `path` `format` |
| `fuxiang_story_import` | 文件 ↔ 地图 比对/应用（默认 dry_run） | `path` `dry_run` `prune` `match_by` |
| `fuxiang_story_import_next` | 推进导入里的下一个创建 | — |

详见 [STORY_EDIT.md](STORY_EDIT.md)。

### E. 从场景删除（原生命令层，破坏性）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_edit_delete_preview` | 只读：将删清单/组合级联/被拦目标 | `ugc_id` `ugc_ids` `owner_uid` `allow_unregistered` |
| `fuxiang_edit_delete` | 执行删除（批量≤50、可 skip_rejected） | `ugc_id` `ugc_ids` `owner_uid` `skip_rejected` `dry_run` |

> 删除**前务必先预览**；运动点（203912）不可经此路径删除；删除走撤销栈，游戏内 Ctrl+Z 可撤。

### F. 资源大厅（组合库）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_resource_hall` | 大厅结构（页签/排序/标签/库） | — |
| `fuxiang_resource_hall_items` | 组合记录枚举（官方+玩家） | `library` `tag` `official_only` `query` `limit` |
| `fuxiang_resource_hall_item` | 单个组合详情（拆解 combo_data） | `combo_id` `share_code` `with_param_detail` |
| `fuxiang_resource_hall_ui` | 已打开大厅的实况（页签/卡片数） | `include_cards` `with_full_doc` |
| `fuxiang_resource_hall_cards` | 当前页签资源卡片（中文搜索/标签/已下载） | `query` `tag` `recommend_only` `downloaded_only` `limit` |
| `fuxiang_resource_hall_verify` | 两个库结构自检 | — |
| `fuxiang_resource_hall_export` | 导出当前页签快照到 `registry/` | `filename` |

### G. 组合下载（服务端写，无回滚）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_combo_download_status` | 能力/库上限与占用/待确认请求 | — |
| `fuxiang_combo_download` | 按单条共享码下载一个组合（异步） | `share_code` |
| `fuxiang_combo_download_poll` | 查询在途下载结果 | — |

> 库上限 200、一次仅一笔在途、服务端回包约 15–40 秒；只接受**单条 share_code**。

### H. 预览渲染（不出图人工看场景）

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_preview_plan` | 只算不拍：包围盒 + 推荐视角 | `fit` `views` `fov` `elevation` ... |
| `fuxiang_preview_shot` | 摆位→渲染一帧→导出 PNG→（可选）还原 | `view` `fit` `out` `restore` `wait` |
| `fuxiang_preview_sweep` | 一键拍全套 + manifest.json | `fit` `views` `out_dir` `prefix` `restore` |
| `fuxiang_preview_layout` | 2D 俯视平面图 PNG（Pillow 绘制） | `fit` `out` `width` `height` |
| `fuxiang_preview_restore` | 相机还原到备份姿态 | `clear` |
| `fuxiang_preview_state` | 诊断：相机姿态/备份/出图可用性 | — |

> `fit` 三档：`content`（只框道具）/ `map`（整张地图）/ `union`（并集，默认）。预览**不建/毁相机、不回读像素**（引擎级硬禁止，否则崩进程）。

### I. 高级逃生舱

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_eval` | 在游戏进程编辑器上下文执行 Python；预置 `core/cm/em`，结果赋给 `__result__` | `code` |

> ⚠️ `fuxiang_eval` 是逃生舱，仅当其它工具不够用时才用（可在进程内执行任意代码）。

### J. 主动保存与持久化语义（重要）

服务端是保存权威；客户端 API 只提交改动集，**真实持久化以「保存后退出重进」为准**。三个保存相关工具：

| 工具 | 效果 | 主要参数 |
|---|---|---|
| `fuxiang_save_map` | 发起一次服务端保存（async）。与点编辑器保存按钮同链路 `ui._save_change() → send_battle_event(UGC,{key:'save_change',...})`；主线程只发事件，立即返回 `request_id` | `request_id` `stale_after` |
| `fuxiang_save_map_poll` | 轮询`ugc_save_finish`回执：`saved` / `pending` / `timeout` | `request_id` `timeout` |
| `fuxiang_save_dirty` | 诊断：读 `ui.change_units` / `cm.save_component_info` 的脏缓冲长度（`detail=true` 可看原始载荷）；**并附带上次保存前后的编辑器登记集合差异**（`registry_before` / `registry_after` / `dropped_count` / `dropped_uids`，非空时给 `warning`） | `detail` |

**验证方法与实测结论（2026-09-18）**

1. **脏缓冲排空可作"保存已上送服务端"的会话内判据**：`fuxiang_save_map` 提交后 `change_units` 被清空 ⇒ 改动集已上送并被处理。实测：编辑后 `change_units_length=2` → 保存后 `=0`。
2. **就地 `edit_transform` 移动 ⇏ 持久化**：服务端持久化读的是「拖拽时的实时位置同步」通道（`room.request_sync_pos / on_sync_unit_pos` 族），而 API 只改本地 + 标记 uid，不触发该同步 ⇒ **重载后被服务端旧值拉回**（实测：移到新位置后重进回到手动拖拽位）。
3. **add_unit 管线可持久化位置（路径 A，已验证）**：`edit_transform` 加 `persist_via_recreate=true` 改为「删除原对象 + 按目标变换重摆」（add_unit），实测重载后新位置留在原地。**代价：新对象 `ugc_id` 会变。**这是"API 改动位置并持久化"目前唯一可靠路径。
4. **回执时序**：`ugc_save_finish` 回调存在但**服务端较慢（约 15–30s+）**，轮询别过早放弃；之前 `timeout` 多是轮询太早。
5. **⚠️ 保存会重置基线（2026-09-19 实测，务必遵守）**：`save_map` 完成后，**未进入本次保存变更集**的已落位对象会被**摘除登记集合**（`all_ugc_uid_set` 移除、`del_unit_uids` 加入、位置退回哨兵位 `[0,-65536,0]`）；**保存后再摆的新对象同样会被摘除**。且被摘除的实体仍在组件表中，会参与下一次保存 ⇒ 重进后可能出现**同位置重复件**（实测 4 个类型各 2 份）。
   ⇒ 作业纪律：**每批摆完立即保存；末次保存后不要再摆放；保存前用 `map_info` 的 type_distribution 查重**。
6. **`save_map_poll` 可能收不到 ack**：重进/换会话后回执观察器可能不触发（`timeout`），此时以 `save_dirty`（`change_units_length=0`）为已上送判据（实测口径一致）。
7. **被摘除登记的残骸可救回**：补一次原生确认 `ui._normal_create_units([unit], pos)` 使其重新进入登记集合，再 `delete_objects` 正常删除。参考实现 `builds/verify_unusable/salvage.py`（实测清掉 82 个，含一个全局组件副本）。

> ⚠️ 运行时不要自行包裹 `g_unit.send_battle_event` / `room.ugc_save_finish`（会破坏原生调用并可能卡住编辑器退出）。需要观察回执请用上面现成轮询接口。

---

## 六、完整 schema 与工具描述

每个工具的参数类型、必填项、enum 取值等完整 `inputSchema`，无需翻阅本表，直接：

```bash
curl -s http://127.0.0.1:17699/api/tools > tools.json
# 或
python cli/fxhttp.py tools
```

返回的 `tools[]` 每项含 `name / description / inputSchema`，即权威契约。