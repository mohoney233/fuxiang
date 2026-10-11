# 随笔浮想编辑器 MCP 接口使用手册

> 《第五人格》「随笔浮想」编辑器 MCP（Model Context Protocol）的使用入门与逐接口实操指南。
> 面向要实际调用编辑器能力的智能体与开发者。配套文档：功能全貌见 [README.md](README.md)、
> 组件/物体全量定位见 [CATALOG.md](CATALOG.md)、数值修改见 [EDIT_OPS.md](EDIT_OPS.md)、
> HTTP 版接口见 [API.md](API.md)。

---

## 一、MCP 是什么（本项目的形态）

本项目把"MCP"实现为一个 **stdio JSON-RPC 服务器**（[mcp/server.py](mcp/server.py)）：

```text
智能体/MCP客户端 ──tools/list / tools/call──→ mcp/server.py ──ipc/cmd + ipc/out──→
  游戏内 fuxiang_bridge ──主线程→ fuxiang_edit API 层 → 编辑器 / 组件管理器 / 场景实体
```

- 智能体通过 `tools/list` 拿到全部工具的 `name/description/inputSchema`（当前 **66 个**，
  真源 `python cli/consts.py`；2026-09-22 校正，此前写 52 为过期值），
  再用 `tools/call` 调用。
- 游戏侧 API 层每次调用都会 `reload`（热更新），新增接口无需重启 MCP 服务器；仅需重启 MCP 才能发现"新工具"。
- 同一分发层同时被 HTTP 版（[API.md](API.md)）复用；MCP 面向智能体，HTTP 面向脚本，两者等价。

---

## 二、前置条件

1. **游戏启动并进入「随笔浮想」编辑器**（dwrg.exe 已运行且已打开 UGC 编辑器）。
2. **桥接已安装**：每次游戏进程重启后，以**管理员身份**运行一次：

```powershell
& '<工具根>\injector\bin\Debug\net8.0-windows\fuxiang-inject.exe' install
```

3. **Python 运行环境**：`<你的-python.exe-完整路径>`
   （系统 `python.exe` 是 WindowsApps 占位程序，勿用）。

验证桥是否在线：`ipc/bridge_state.json` 里 `now` 若距当前超过约 10 秒即视为失联。

---

## 三、启动 MCP 服务器

MCP 是 stdio 服务器，由**客户端拉起**即可，通常不需要手动常驻：

```powershell
& '<你的-python.exe-完整路径>' -u '<工具根>\mcp\server.py'
```

### 3.1 客户端注册（配置示例）

参考 [mcp_config.example.json](mcp_config.example.json)：

```json
{
  "mcpServers": {
    "fuxiang-ugc-editor": {
      "command": "<python-完整路径>",
      "args": ["-u", "<工具根>\\mcp\\server.py"]
    }
  }
}
```

### 3.2 不依赖完整 MCP 客户端的调用方式

| 脚本 | 用途 |
|---|---|
| `python cli/mcpcall.py <tool> ['{"json":args}']` | 一次 stdio 会话调**一个工具**（最接近智能体的调用方式） |
| `python cli/fxedit.py <op> ['{"json":args}']` | 直达游戏内 edit op（reload+dispatch，验证新增 op 用，无需重启 MCP） |
| `python cli/fxcall.py ping\|info\|eval ...` | 桥接层 op（ping/info/modules/introspect）；`eval` 与 edit 层同预置（见第六节第 17 条） |
| `python cli/fxrun.py probe/xxx.py` | 把本地 .py 脚本内容送入游戏内执行（结果写 `.out.json`） |
| `python cli/fxhttp.py call <tool> ...` | 走 HTTP 版（需先启 http_server） |
| `python cli/fxsweep.py [--older-than 60] [--dry-run]` | 归拢 `ipc/out` 累积的桥接回执（`shutil.move` 改名进时间戳目录，**不删除**、跳过在途回执） |

> 回执生命周期：`fxedit` / `fxcall` / `fxlong` 现在**一律保留** `ipc/out/<cid>.json`（不删 = 不触发
> 宿主 safe-delete 钩子，见第六节第 15 条）。要清理用 `cli/fxsweep.py`，攒着也不影响正确性。

示例（调单个工具）：

```powershell
& $py tools/fuxiang/cli/mcpcall.py fuxiang_list_items '{"query":"桌子","limit":3,"include_hidden":true}'
```

---

## 四、66 个接口全景（MCP 工具）

`tools/list` 会返回全部工具的 `inputSchema`（权威契约）。下表按功能归类，`⚠️` 为有副作用的写操作或高危：

| 归类 | 工具（`tools/call` 的 name） |
|---|---|
| 状态/地图 | `fuxiang_status` `fuxiang_map_info` |
| 类型/物件编目 | `fuxiang_list_types` `fuxiang_list_items` `fuxiang_find` `fuxiang_catalog` `fuxiang_catalog_items` `fuxiang_catalog_verify` `fuxiang_component_detail` `fuxiang_component_schema` `fuxiang_categories` `fuxiang_enums` `fuxiang_catalog_export` |
| 摆放/实体 ⚠️ | `fuxiang_plan_item`（只读） `fuxiang_place_item` `fuxiang_place_poll` `fuxiang_remove_item` `fuxiang_set_transform` `fuxiang_get_component` `fuxiang_list_components` |
| 低层组件 ⚠️ | `fuxiang_create_component` `fuxiang_remove_component` `fuxiang_repair_index` `fuxiang_pending_saves`(只读) |
| 参数/事件 ⚠️ | `fuxiang_get_params`(只读) `fuxiang_set_params` `fuxiang_create_event` `fuxiang_save` |
| 主动保存 ⚠️ | `fuxiang_save_map` `fuxiang_save_map_poll` `fuxiang_save_dirty`(只读) |
| 数值修改 ⚠️ | `fuxiang_edit_targets`(只读) `fuxiang_edit_target`(只读) `fuxiang_edit_transform` `fuxiang_edit_props` `fuxiang_edit_history`(只读) |
| 删除 ⚠️ | `fuxiang_edit_delete_preview`(只读) `fuxiang_edit_delete` |
| 资源大厅 | `fuxiang_resource_hall` `fuxiang_resource_hall_items` `fuxiang_resource_hall_item` `fuxiang_resource_hall_verify` `fuxiang_resource_hall_ui` `fuxiang_resource_hall_cards` `fuxiang_resource_hall_export` |
| 组合下载 ⚠️ | `fuxiang_combo_download_status`(只读) `fuxiang_combo_download` `fuxiang_combo_download_poll` |
| 预览渲染 | `fuxiang_preview_plan` `fuxiang_preview_shot` `fuxiang_preview_sweep` `fuxiang_preview_layout` `fuxiang_preview_restore` `fuxiang_preview_state` |
| 高级逃生舱 | `fuxiang_eval` |

---

## 五、典型使用流程（实操）

### 5.1 通用开场（每次新会话先做）

```text
fuxiang_status          # 确认编辑器核心/组件管理器可用
fuxiang_map_info        # 当前地图概况
```

### 5.2 定位"想放什么"（只读，推荐顺序）

```text
fuxiang_catalog                     # 先看总量与边界（类型数/可创建数/物件数）
fuxiang_catalog_items(limit=0)      # 需要全量清单时一次取回（约 3963 项）
fuxiang_categories                  # 需要按编辑器左栏分类定位
fuxiang_find(item_id=…/query=…)     # 定点确认某个物件/组件
fuxiang_component_detail(component_type=…)  # 定点确认某个组件类型的可创建性
fuxiang_catalog_verify              # 交付/报告需要机器可核验证据时
```

> ⚠️ 同名多物件（如"盖板"有 202014 与 202009 两条）务必用 `component_type` 或 `catalog_hidden` 消歧，**不能凭模型路径判断**（[CATALOG.md](CATALOG.md) 第四节）。`item_id` 与 `component_type` 是两套命名空间。

### 5.3 摆放一个物件（异步，标准流程）

```text
fuxiang_plan_item(item_id=202014, position=[628,2,-60], rotation=[0,25,0], scale=0.8)  # 先校验（不改场景）
fuxiang_place_item(item_id=202014, position=[628,2,-60], rotation=[0,25,0], scale=0.8,
                   request_id="layout-01")     # 发起，立即返回 request_id
fuxiang_place_poll(request_id="layout-01")     # 立即查；实测首次即 placed
```

要点：
- 轮询用 `place_poll`，**在 MCP 客户端侧等待**，不能阻塞游戏主线程。
  ⚡ **不要固定 sleep**：2026-09-19 实测（10/10 样本，place→placed 中位 180ms），
  `place_item` 返回后**立即** `place_poll` 通常就是 `placed`；旧的「间隔 0.5s」建议会让每次
  摆放白等（实测占单次耗时 55%）。正确做法：**先立即查一次**，返回 `pending` 时再按响应里的
  `poll_after_ms` 退避（首查建议 0，仍 pending 后为 150ms）。
- 重试必须沿用同一 `request_id` 与相同参数；一个会话只允许一个未完成请求。
- `mode=local_preview` 才创建可被 `fuxiang_remove_item` 清理的本地预览；服务端物件须在游戏内删除保存。
- 任何工具返回 `placed` 都只代表**编辑器已登记**，`persistence_verified=false`；真实保存需**游戏内保存 + 重进**核实。

### 5.4 改已有对象的数值（变换/属性）

```text
fuxiang_edit_targets()                                     # 找可编辑对象与判定
fuxiang_edit_target(ugc_id=116, with_schema=true)          # 看它的变换与属性定义
fuxiang_edit_transform(ugc_id=116, position=[630,0,-125], dry_run=true)  # 先预览
fuxiang_edit_transform(ugc_id=116, position=[630,0,-125])                # 写入
fuxiang_edit_props(ugc_id=116, props={"10000002001": [1]})               # 改属性
fuxiang_edit_history()                                     # 回看（前后值）
```

要点：角色类对象不能改变换（会明确拒绝）；写入自动回读校验、不一致**自动回滚**；属性 id 必须属于该类型的 schema。

> ⚠️ **位置持久化坑（2026-09-18 实测）**：就地 `edit_transform` 改位置**不会持久化**（服务端读的是拖拽时的实时位置同步，重载会被旧值拉回）。要持久化位置，用 `persist_via_recreate=true` 走「删除→重摆」的 add_unit 管线（可持久化，但新对象 `ugc_id` 会变）：
> ```text
> fuxiang_edit_transform(ugc_id=116, position=[640,2,-110], persist_via_recreate=true)  # 删除+重摆，可持久化
> ```

### 5.5 主动保存（服务端）

服务器是保存权威；API 只提交改动集，真实持久化以「保存后退出重进」为准。

```text
fuxiang_map_info                                    # 看当前地图
fuxiang_edit_transform(..., persist_via_recreate=true)   # 需要持久化的位置移动
fuxiang_save_dirty()                                # 保存前：change_units 应非空(脏)
fuxiang_save_map(request_id="layout-01")            # 发起服务端保存(异步)
fuxiang_save_map_poll(request_id="layout-01")       # 轮询回执(save可能~20s+才回,别放弃太早)
fuxiang_save_dirty()                                # 保存后：change_units 被清空=已上送
```

验证提示：`save_dirty` 的脏缓冲排空可在不退出重进的情况下证明"改动已上送服务端"；真实持久化仍需保存后退出重进核对。

### 5.6 删除对象（破坏性，务必先预览）

```text
fuxiang_edit_delete_preview(ugc_id=130)    # 只读：将删清单/组合级联/被拦目标
fuxiang_edit_delete(ugc_id=130)            # 执行
```

要点：运动点（组件 203912）不可经此路径删除；删除走撤销栈（游戏内 Ctrl+Z 可撤）；批量 ≤50。

### 5.6 资源大厅 + 组合下载

```text
fuxiang_resource_hall                      # 看大厅结构（官方/分享/我的；排序/标签）
fuxiang_resource_hall_cards(query="桌", limit=0)   # 当前页签资源卡片（官方页签约 376 条）
fuxiang_resource_hall_item(combo_id=65552)          # 拆解某个组合本体 combo_data

fuxiang_combo_download_status()            # 先看库是否满（上限 200）
fuxiang_combo_download(share_code="uyWnjPBf")       # ⚠️ 服务器写、无回滚、单条、异步
fuxiang_combo_download_poll()              # 轮询：downloaded / pending / unknown
```

### 5.7 预览出图（不想手动进编辑器看场景）

```text
fuxiang_preview_plan(fit="content")        # 包围盒 + 推荐视角
fuxiang_preview_sweep(fit="content")       # 一键拍全套（自动还原相机）+ manifest.json
fuxiang_preview_layout(fit="union")        # 另出一张 2D 俯视平面图
```

> ⚠️ 四硬禁止：`scene.render/render_once`（非 render frame）、`texture.get_data` 像素回读、以及帧在途时的 `camera.destroy`/`release_render_target` —— 违反会**崩进程**。

### 5.8 排障与自愈

```text
fuxiang_repair_index      # 增/删后组件数异常时，重登记脱离索引的存活组件
fuxiang_pending_saves     # 本地事件重放队列（诊断，不是保存回执）
fuxiang_preview_state     # 预览相机/备份/出图可用性
fuxiang_eval              # 逃生舱：进程内执行代码；预置 core/cm/em + jdump/plain，结果赋给 __result__
```

---

## 六、调用约定与常见坑

1. **永远以 `fuxiang_status` 开头**；桥失联时报错并提示重新 `fuxiang-inject install`。
2. **异步接口禁止轮询阻塞游戏主线程**：`place_*` / `combo_download_*` 在客户端侧定时查询。
3. **幂等键**：`place_item` 用 `request_id`，下载用单条 `share_code`；重试沿用原键与相同参数。
4. **`persistence_verified` 恒为 `false`**：接口只管"写入本地对象并登记变更"，保存重进后才可核实。
5. **不要凭模型路径判断物种类别**，用 `component_type` / `catalog_hidden`（CATALOG.md 第四节）。
6. **列表不可见 ≠ 接口拿不到**：原生隐藏（43 条）与当前模式分类屏蔽（2109 条）都不影响寻址与摆放。
7. **热重载**：游戏侧 API 每次调用 reload，新增 op 用 `fxedit.py` 直接验证，不必重启 MCP。
8. **不触碰**：不要 `gc.get_objects()`（曾触发 MemoryError）、不用 `save_component_info` 插入合成 add_unit 事件。
9. **只读与写**：写操作集中在"摆放/实体、低层组件、参数/事件、数值修改、删除、组合下载"；表格中已标 `⚠️`。
10. **persistence 的最终裁决在服务端**：地图保存是服务端权威，接口不绕过。
11. **就地 `edit_transform` 的位置不持久化**：服务端存的是拖拽时的实时同步位置，API 只改本地 → 重载被拉回。需持久化位置 → `persist_via_recreate=true`（删除→重摆，可持久化但 `ugc_id` 会变）。
12. **保存回执偏慢**：`fuxiang_save_map_poll` 往往要 ~20–30s 才收到 `ugc_save_finish`，别过早判超时；可用 `fuxiang_save_dirty` 的脏缓冲排空做会话内确认。
13. **不要在运行时包裹 `g_unit.send_battle_event` / `room.ugc_save_finish`**：会破坏原生调用并可能卡住编辑器退出（曾实测踩坑）；确需观察回执用现成的 `save_map_poll`。
14. **eval 里不要对引擎数据用 `json.dumps`**：注册表（`GK.DM.*`）返回的是 C 扩展类型 `taggeddict`，MRO 只有 `(taggeddict, object)`、**不继承 dict**，`json.dumps` 直接抛 `TypeError: Object of type taggeddict is not JSON serializable`（2026-09-19 建模师实机命中）。
    正确写法：`__result__ = jdump({...})`，或干脆 `__result__ = {...}`（接口出口会自动做同样的转换）。
    ⚠️ `json.dumps(..., default=str)` 是**假修复**：不报错，但嵌套记录会被压成 `"{'TID':...}"` 这类 repr 字符串，读回来的值是坏的。
15. **长跑脚本别在调用路径里删文件**：`ipc/out/<cid>.json` 回执过去由 `cli/fxedit.py` 每次 `os.remove`，几十次累计会撞宿主 safe-delete 批量阈值（本工作区实测 50/turn、两次被终止）→ **进程静默死亡、exit 1、无 traceback**，看起来像"只跑了几行"。2026-09-19 起 `fxedit`/`fxcall`/`fxlong` 统一保留回执，归拢交给 `cli/fxsweep.py`（改名归档）。另：长跑务必 `python -u xxx.py > run.log 2>&1`，管道下 stdout 全缓冲，被终止时输出会整块丢失。
16. **「桥接未运行（心跳 None）」≠ 桥死**：`None` 是状态文件读不到，通常是 Windows `MoveFileEx` 替换 `bridge_state.json` 时的亚毫秒缺失窗口（心跳每 1.0s 重写，瞬间自愈），也可能是手动删除后的自愈期——桥多半还活着。2026-09-19 建模师在 jp-city-0524 实机命中（dwrg pid 未变、ping 正常）。`fxedit`/`fxcall` 已内置 2.5s 读重试；见到这条先 `ping` 复核再决定是否重注入。**心跳超时（具体秒数）才是真掉线**。
17. **`eval` 有两个同名通道，预置现已统一**：桥层原始 `op_eval`（`fxcall.call('eval')` / `fxrun.py` 直达，命名空间原本只有 `__result__`）与 edit 层 `fuxiang_edit.op_eval`（`fxedit.py` / MCP `fuxiang_eval` 走 `dispatch`，预置 `core/cm/em/jdump/plain`）。2026-09-19 建模师按「`core`/`cm` 是 eval 预置变量」用 `fxcall.call('eval')` → `NameError: name 'cm' is not defined`。现 `cli/fxcall.py::call` 对 `op=='eval'` 自动拼接同一套预置（prologue 包在 `try` 里，取不到也不会弄坏传输通道），两条通道语义一致；桥层代码未改，**无需重注入**。写新代码仍优先 `fxedit.py` / MCP 工具。预置名统一由 `fuxiang_edit.eval_presets()` 提供：`core/room/cm/em/GK/jdump/plain/FS/FE`。
18. **「编辑器的退出按钮点了没反应」多半是会话已断，不是 UI 卡住**：随笔浮想的退出与保存都是**服务端中介**流程。2026-09-20 09:18 实机：网络切换后 `handle_soul_disconnect` → `Avatar.soul_server=None` → `check_can_reconnect=-1`（客户端拒绝重连），进程只剩 `Bound/CLOSE_WAIT` 套接字、无 Established，于是退出无反应——**room 与 UI 全部正常、`g_unit.room is core.room`、`ui._get_select_pos()` 也不抛**，所以 EDIT_OPS §10.6 那个 `GK.g_unit.room` 修法的判据要**先排除**。看 `fuxiang_status` 的 `server_session.connected`：为 `false` 时**停止摆放**（写进去也不可能上送），已落库的内容留在服务端，恢复网络后重启客户端重进即可。抢救性只读导出用 `cli/scan_editor_map.py <名字> --batch 200 --with-events`（走 `cm` 索引，不依赖网络）。

---

## 七、验证与测试

```powershell
# 接口层端到端（真正走 MCP stdio 协议，启动真实服务器）
& $py tools/fuxiang/tests/mcp_interface_check.py --sweep
# 结果写入 tools/fuxiang/tests/mcp_interface_result.json

# 离线回归（原生命令为模拟对象）
& $py tools/fuxiang/tests/...  # 见 README「验证」节
```

---

## 八、速查：工具 → 底层 op

同一套分发层也被 HTTP API 复用，完整 `tool ↔ op` 映射见 [API.md](API.md) 第四节。此处不再重复，注意：
预渲染系列（`fuxiang_preview_*`）由主机侧 `mcp/preview.py` 编排（跨帧等待+文件轮询），不是简单透传。

> 维护注意：MCP 服务器须重启才能发现**新增工具**；游戏内 API 每次调用热重载，不需重新注入。