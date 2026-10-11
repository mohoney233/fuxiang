# 界面编辑器（UI 编辑）

> 给「随笔浮想」的**界面编辑器**加的一层编辑能力：定位面板与控件、在画布上**新增控件**、
> 在控件上**绑事件**、改属性、删除、落库前自检。共 14 个 op（真源
> `python cli/consts.py` 的 `edit_ops`，本文件末节有清单）。命令行入口 `cli/fxui.py`。
>
> 2026-09-23 实机逆向自 dwrg.exe（`.nxs` 无源码，全部结论来自 `dis` 字节码 + 活实例回读；
> 探针留在 `probe/ui_hunt*.py`，`probe/ui_hunt*_view.txt` 是可读化的产物）。

## 0. 一句话结论

界面编辑器 = 另一套视图 `UIBattleUGCCustomUI`（19 个子 part 分工协作），放置出来的控件是
**组件类型 200000（UI_CUSTOM，类 `UGCUiCustomComponent`）**，事件绑定是往属性袋的
`20000016605`（点击）/`16606`（松手）/`16607`（长按）槽位写 `[事件名, 1286]`，
其中 **1286 是字段定义里的默认值，不是序号**。

⚠️ **全图界面控件共用同一个 `UgcGlobalUnit`**（实机：30 件、跨 27 个分类，全挂在
`uid 1000313` 上，尽管 `unit.is_single_component` 仍写着 `True`）。控件"是哪件"只能问
`room.get_component_item_id(ugc_id)` —— `unit.ugc_type` 只是**第一件**的 item_id。
各自那份袋能分别进报文，靠保存通道逐件切换：`_get_change_uid()` 对
`change_units['component']` 里的每个 ugc_id 先 `ui.change_component_id(ugc_id)`
再读单元级 `_get_ugc_info(unit, is_save=True)['params']`。而那份镜像的属性段**等于该控件
事件袋里 info 非空的项**（逐件比对一致），所以回读直接算袋即可（`_save_shape`），不必去动
编辑器状态 —— 这一条最初被我判成"一控件一单元"、又被判成"回读必须切镜像"，都是实机加件后
才纠正的。

## 1. 定位到的东西

| 目标 | 位置 |
|---|---|
| 视图实例 | `GK.g_ui_mgr` 的 `vars()['ui_dict']['UIBattleUGCCustomUI']`，owner 是 `UIBattleUGCEdit` |
| 是否打开 | `ui._get_is_show_custom_ui_view()` |
| 子面板 | `cus.sub_part_dict`（19 个，`ui_parts` 列分工） |
| 控件数据 | `cm._component_type2ugc_ids[200000]` → `cm._ugc_id2component[ugc_id]` |
| 原生技能/操作位 | 组件类型 200001（UI_NATIVE，实测一张图 48 个，共用 `unit 1000057`） |
| 可放置目录 | `GK.DM.ugc_item_data` 中 `component_id == 200000` 的 2108 项（图片/按钮/功能/角色四组） |
| 事件注册表 | `cm._ugc_params_events_key['event']`（与场景事件同一张表） |

⚠️ 取实例不要用 `g_ui_mgr.get_ui(name)`：未注册时它返回 **`Swallow` 哨兵而不是 None**，
会被误当成"实例存在"（本仓库反复踩过，见 README「维护注意」）。

### 19 个子 part（`ui_parts` 的 `role` 字段就是这张表）

数据层 · 公共逻辑(建 2D 节点) · 画布主体 · 画布窗口 · 操作手柄(拖拽/缩放/旋转) ·
框选复制 · 左上工具条(返回/设置/保存/撤销/替换) · 左下区 · 左侧页签 · 底部删除条 ·
右侧「控件」目录页 · 右侧「属性」页 · 右侧「更多属性」页 · **右侧「事件」页** ·
右侧「颜色」页 · 右侧「锚点」页 · 右侧「进入按钮」页 · 右侧「删除区」 · 右侧页签管理器。

事件绑定在原生 UI 里由 `UIBattleUGCCustomUIRightEventPart`（`bind_master`/`bind_params`）完成；
控件的增删由调色板视图 `UIBattleUGCRightComponentView._handle_2d_end` 与底部
`_on_click_del_node` 触发。

## 2. 控件的数据模型

```text
UgcGlobalUnit(uid=1000313, ugc_type=5201002, is_single_component=True)   ← 全图控件共用一个
  ├ UGCUiCustomComponent(_ugc_id=362, COMPONENT_TYPE=200000)   ← 玩家手放的按钮
  └ UGCUiCustomComponent(_ugc_id=363, COMPONENT_TYPE=200000)   ← 工具 ui_add 建的按钮
      component_info = {'component_id': 200000,
                        'events': [{'type': <property_id>, 'info': [...]}, ...]}
```

* 保存报文由 `ui._get_change_uid()` 组装：对 `change_units['component']` 里每个 ugc_id
  `change_component_id(uid)` → `_get_ugc_info(unit, is_save=True)['params']`，
  形如 `[三个 -999 变换占位, 该控件事件袋里 info 非空的项]`；**前 3 项是占位**，所以事件引用
  三元组里的 `index` = 属性袋下标 + 3（`PARAMS_HEADER`）。
  ⇒ 回读不必去动编辑器状态：`_save_shape(comp)` 直接按"袋里 info 非空"算，实测与切换后读到
  的镜像逐件一致。要真按保存通道的写法核一遍就调
  `ui_verify {"with_mirror_probe":true}`（默认关：它会临时改"当前主组件"的指向，
  实测不入 `change_units`、不标脏地图）。
* ⚠️ 控件单元**不在** `ui.all_ugc_uid_set`（那是场景物件的登记集合）。数值层的
  `find_target` 因此会拦下控件写入 —— `ui_set` / `ui_bind` 内部已固定传
  `allow_unregistered=true`，直接用 `edit_props` 的调用方要自己传。
* ⚠️ 200001（UI_NATIVE）一个单元挂 48 个组件，属于"共用单元"，其单元级镜像只有主组件
  那份（见 EDIT_OPS.md §9.5 与 `event_bind 写错组件` 的教训）—— 本模块**只列不改**它。

### 属性表（`ui_schema` 的输出即这张表，运行时长出来的，不写死）

别名 ← property_id ← `ITEM_PARAM_2_EVENT_TYPE` → event_type → `EVENT_TYPE_2_METHOD_NAME` 方法。
面板文案 / 方向 / 默认值形态来自 `GK.DM.ugc_item_property[200000]`。

| 别名 | property_id | event | 应用方法 | 面板文案 | 值形态 |
|---|---|---|---|---|---|
| `pos` | 20000010001 | POS(1) | `_set_ui_pos` | — | `[x, y]` 画布 2D |
| `rot` | 20000010004 | ROT(2) | `_set_ui_rot` | — | 角度 |
| `height` / `width` | 20000010007 / 10008 | HEIGHT(4)/WIDTH(5) | `_set_ui_height`/`_set_ui_width` | — | `[像素]` |
| `alpha` | 20000010009 | ALPHA(6) | `_set_ui_alpha` | — | 数值 |
| `name` | 20000016501 | SKILL_NAME(18) | `_set_skill_name` | 控件名 | `[文本]` |
| `visible` | 20000016502 | VISIBLE(3) | `_set_init_ui_visible` | — | 布尔形态 |
| `show_object_type` / `show_camp` / `show_class` | 20000016503/16504/16505 | 21/22/23 | `_set_show_*` | — | 枚举 |
| `add_color` / `ui_color` | 20000016506 / 16507 | 26 / 27 | `_set_add_color`/`_set_ui_color` | — | 双层颜色值 |
| `only_send_ui` / `interaction_layer` | 20000016601 / 16602 | 19 / 20 | `_set_only_send_ui`/`_set_interaction_layer` | — | 布尔 / 数值 |
| **`on_show`** | 20000016603 | SHOW_NODE_EVENT(16) | `_show_node` | 使控件显示 | `[事件名]`（receiver） |
| **`on_hide`** | 20000016604 | HIDE_NODE_EVENT(17) | `_hide_node` | 使控件隐藏 | `[事件名]`（receiver） |
| **`on_click`** | 20000016605 | DOWN_SEND_EVENT(7) | `_down_send` | 点击 | `[事件名, 1286]`（sender） |
| **`on_release`** | 20000016606 | UP_SEND_EVNET(8) | `_up_send` | 松手 | `[事件名, 1286]` |
| **`on_long_press`** | 20000016607 | PRESS_SEND_EVENT(9) | `_press_send` | 长按 | `[事件名, 1286]` |
| `press_time` | 20000016608 | PRESS_SEND_TIME(10) | `_press_send_time` | — | 数值 |
| `press_enable` | 20000016609 | PRESS_ENABLE(28) | `_set_press_enable` | — | 布尔形态 |
| `bind_anchor` | 20000016809 | BIND_ANCHOR(11) | `_set_bind_anchor` | — | 锚点参数 |
| `content` / `font_*` | 20000017001..17006 | 12..15, 24, 25 | `_set_content` / `_set_font_*` / `_set_font_*_align` | — | 文本 / 枚举 / 数值 / 颜色 |

另有 9 个 `property_type` 为 `110005/110006/110011/190002/30001` 的字段没有 event_type
（`ui_schema.unmapped_properties`，编辑器自用/仅展示），写它们没有应用方法 ⇒ 无效。

⛔ 五个**事件槽**只能用 `ui_bind`：除写槽位外还要在事件注册表里登记反向引用
（`op_event_bind` 已负责），`ui_set` 明确拒写它们。

## 3. 新增控件的原生链路（工具照抄，不发明）

```text
玩家从调色板拖放：UIBattleUGCRightComponentView._handle_2d_end(main_edit_ui, end_pos, True)
  ├─ main_edit_ui.get_create_unit() / get_create_components()   ← 服务端建件后的待确认袋
  ├─ _remove_node_2d(comp)                                      ← 清拖拽预览节点
  ├─ update_params = {refresh_occupancy, refresh_component_nums,
  │                   refresh_ui_custom, select_ugc_id}
  └─ main_edit_ui.on_check_ui_component_limit([comp], end_pos, update_params)
        ├─ comp._init_client_first_default_params()
        ├─ ui_component_detector_chain.detect(...)              ← 占用值校验，超预算可能弹窗
        └─ on_check_success:
             materializer_part.materialize_component(comp)
             _get_create_ui_components_commands(...)：
                 ui._add_change_unit(unit.uid) + ui.change_component_id(ugc_id)
                 + UGCUIObjCommand(view='UIBattleUGCCustomUI', type=False)
                 + UGCObjChangeCommand(type=True) + UGCOccupancyChangeCommand(type=True)
                 + 把 end_pos.x/.y 写进 POS(20000010001) 槽位
             UGCGroupUICompCommandReverse → iUIBattleUGCCustomUICommandControl.do_command
```

工具侧实现：`bridge/fuxiang_placement.py::_confirm_ui_component`（在 `place_poll` 里
按「组件类型 200000 **且** 界面编辑器在前视图」分流；视图关着时退回既有的
`_confirm_global_component` 通用登记路径 —— 历史上 200000 的图标类件就是这么验过的）。

删除：`ui.on_del_ui_component([ugc_id], last_update_params)` →
`_get_del_ui_components_commands` → `UGCGroupUICompCommandReverse` → `do_command`；
真正上送的删除通道仍是 `room.del_unit_uids['component']`。

### 两个必须记住的坑

1. **视图没打开 = 静默空操作**。`_get_create_ui_components_commands` 与
   `_get_del_ui_components_commands` 开头都是
   `if not self._get_is_show_custom_ui_view(): return []`。表现为"调用成功但控件没出现"，
   而且它仍留在待删除袋里 ⇒ 保存时被当**删除**上送。故 `ui_add`/`ui_set`/`ui_bind`/`ui_delete`
   一律先检查 `view_open`，不满足直接报错。
2. **坐标是画布 2D**。`end_pos` 只取 `.x`/`.y`（设计尺寸 1334×750，原点左下；实测玩家的
   按钮在 `[451.6, 625.2]`）。`ui_add --pos` 传第三个数字会被 `_pos2` 拒收，别拿场景坐标来。

## 4. 命令速查

```powershell
$py = '<你的-python.exe-完整路径>'
& $py tools/fuxiang/cli/fxui.py state                     # 视图开没开、控件数、画布尺寸
& $py tools/fuxiang/cli/fxui.py parts                     # 19 个子面板的分工与方法
& $py tools/fuxiang/cli/fxui.py catalog 按钮 --limit 0    # 可放置控件目录
& $py tools/fuxiang/cli/fxui.py schema                    # 别名字典（写之前先看它）
& $py tools/fuxiang/cli/fxui.py list --native             # 已有控件（+原生操作位概要）
& $py tools/fuxiang/cli/fxui.py get 362                   # 单个控件 + 保存报文形态

& $py tools/fuxiang/cli/fxui.py add 5201002 --pos 700,300 --dry-run
& $py tools/fuxiang/cli/fxui.py add 5201002 --pos 700,300          # 自动轮询到 placed
& $py tools/fuxiang/cli/fxui.py bind 362 on_click 我的事件 --create-event
& $py tools/fuxiang/cli/fxui.py set 362 -f width=200 -f alpha=0.8
& $py tools/fuxiang/cli/fxui.py move 362 800 400
& $py tools/fuxiang/cli/fxui.py delete 362 --yes          # 默认 dry_run，--yes 才真删
& $py tools/fuxiang/cli/fxui.py verify                    # 落库前自检
```

op 名与 MCP 无关，任意 op 都能直接透传：`cli/fxedit.py ui_list '{"include_native":true}'`。

## 5. 典型流程（含持久化核实）

1. `state` 确认 `view_open: true`；不是就先在游戏里点开界面编辑器。
2. `catalog` 挑 `item_id`（中文名可用 `add 按钮1（常态）`，精确匹配才受理）。
3. `add` → 记下 `request_id`，脚本会自动轮询；`status: placed` 且
   `canvas_position` 与请求一致才算被编辑器接纳。
4. 建事件/绑事件：`bind <ugc_id> on_click <事件名> --create-event`。
5. `verify`：`ok: true` 表示每个控件都进了报文形态、事件引用双向一致、没有待删除标记。
6. **游戏内保存**（`cli/fxapi.py save_map '{}'` 或手动 Ctrl+S）。⚠️ 保存会重置基线、
   **退出编辑器会自动保存**（README「保存」节）。
7. 重进地图后 `list` 回读：按 `item_id` + `pos` + 事件名比对（`ugc_id` 会整体重编号，
   别拿 id 当判据）。工具一律返回 `persistence_verified: false`，只有第 7 步算数。

截图核对：`cli/fxshot.py`（引擎出图）；界面编辑器的画布是 2D 覆盖层，场景截图里
未必看得到控件，必要时让人工看图。

## 6. 护栏

| 护栏 | 行为 |
|---|---|
| 视图未打开 | 写操作直接 `ok:false`，并说明"原生命令会返回空" |
| `ui_set` 收到事件槽 | 拒写并指向 `ui_bind`（否则会绕开事件注册表反向登记） |
| `ui_bind` 事件不存在 | 默认报错并列出现有事件；`create_event=true` 才建（走原生命令层，进撤销栈） |
| `pos` 非 2D / 非数值 / NaN | `_pos2`·`_num` 抛错，dispatch 转成 `ok:false` |
| 别名漂移（客户端改版） | `_alias_map` 运行时比对 property_id 与应用方法名，不符即报错 |
| 属性回读不一致 | 由数值层 `op_edit_props` 负责**自动回滚**（本模块复用，不另写一套） |
| 删除 | 默认 `dry_run`；执行后核对 `del_unit_uids['component']`，未标记的列在 `not_marked` |
| 单批上限 | 删除 ≤50 个；`ui_add` 沿用摆放的「单笔在途」协议 |

## 7. 为什么不注册成 MCP 工具

2026-09-22 通道收敛后，MCP stdio 默认停用且**刻意不再增长**（`mcp/server.py` 头注释：
"新增一个 op 要改 3 处，数字/结论还会在 tool 描述里过期"）。本能力的 14 个 op 全部经
`fuxiang_edit.dispatch`，`cli/fxui.py` 与 `cli/fxedit.py <op>` 已 100% 覆盖。

## 8. 已知边界

* **只能改 UI_CUSTOM(200000)**。200001（UI_NATIVE，技能/操作位）不在画布上、且与
  48 个组件共用一个单元，属"共用单元"高危区 —— `ui_list --native` 只给概要。
* `ui_add` 走服务端建件 ⇒ 受「单笔在途」「同会话未定论请求 ≤128」约束（与摆放同一账本）。
* 占用值校验可能在游戏内弹窗（`ui_component_detector_chain`）。弹窗期间确认不会完成，
  `ui_add_poll` 会给出 `finalize_failed` 并保留现场，**不会自动重试**。
* 画布刷新走 `ui.refresh_ui_custom`（取不到只记 `canvas_refresh`，不影响数据正确性）。
* 复制粘贴/框选（`add_copy_unit_ui_custom`）、锚点绑定到原生控件、控件层与交互层的
  运行时行为（`_down_send` 触发到哪）本模块**未做端到端验证**，不在能力范围内。

## 9. 2026-09-23 实机闭环（当前那张图）

`cli/fxui.py add → bind → set → verify → 保存` 全程走通，抓包（`probe/ui_persist_hook.py`
包 `GK.g_unit.send_battle_event`）拿到的原始报文如下；证据汇总在
`tests/handover_evidence/ui_edit_live_test.json`。

| 步骤 | 结果 |
|---|---|
| `add 5201002 --pos 1100,650` | `placed`，新控件 `ugc_id=363`，`canvas_position=[1100,650]`，组件数 342→343、占用 1395→1400（该物件 `occupancy_value=5`）✓ |
| 原生确认确实跑了 | 363 的袋里有 POS；`p_area` 子节点 1→2（长出自己的 2D 节点）；尺寸由物件默认推导（`client_get_size=[122,122]`） |
| `bind 363 on_click UI工具链测试事件 --create-event` | 事件建进注册表；槽位是 `property_id_created`（新控件袋里原本没有这一槽），`own_index=14 → index=17`；`canvas_refresh.ok` |
| `set 363 -f width=140 -f height=60 -f name=工具链按钮` | 回读一致；`name`/`content` 按玩家手放的形态自动包成 `[文本]` |
| `verify` | 362/363 **各 5 段字段**、各带自己的事件、`in_change_component: true`、无待删除标记 |
| `save_map` 报文 | `change_uid.component[0] = {362: [8 段], 363: [8 段]}` ⇒ **共用单元的两个控件分别上了各自的数据**；随后 `change_units` 归 0、`dropped_count: 0` |
| 报文里 363 的内容 | `-999` 三段占位 + `POS[1100,650]` + `HEIGHT[60]` + `WIDTH[140]` + `20000016605=["UI工具链测试事件",1286]` + `20000016501=["工具链按钮"]` |
| `delete 363 --yes` + 保存 | `marked_for_delete:[363]`，报文 `del_uid.component=[363]` ✓ |

两点如实记录：

* `save_map_poll` 的回执本会话一直 `pending` 到 30s 超时（`persistence: unknown`），但
  `save_dirty` 的 `change_units_length` 已归 0 —— 按 README 口径"归 0 = 已上送"。
  **重进核实没做**（测试件按约定删掉了）；要补这步就再 `add` 一个留着，退出重进后用
  `list` 按 `item_id` + `pos` 比对。
* 删除后本地 `del_unit_uids['component']` 仍留着 363（`verify` 会报"在待删除袋里"），
  要到重进才干净 —— 这是删除通道的正常形态，不是失败。

实测过程中当场揪出并修掉两个真 bug：

1. **`op_event_bind` 不认 `dry_run`**：`ui_bind --dry-run` 把 `on_release` 真写进了玩家手放的
   按钮 362（回读还"通过"）。已改成在**任何写入之前**返回 `would_write` / `would_register`
   的预演，并回滚了那次误写（只清 16606 槽位、摘对应注册表引用，点击槽原样保留）。
   回归用例：`tests/test_ui_edit.py::TestBindDryRunIsReallyReadOnly`（假对象跑真函数，
   断言 dry_run 时 `update_component_info`/注册表/变更集三处都没动）。
2. **回读把地图标脏的担心是错的**：一度以为自检必须 `change_component_id` 切换镜像，
   而那会把控件登记进 `change_units`（实测 `change_units_length` 前后都是 0 ⇒ 不入登记）。
   现改为默认直接算袋（`_save_shape`），切换式核对降级为 `ui_verify` 的
   `with_mirror_probe` 选项；本次开过一次，两件 `matches_bag` 均为 true（各 5 段字段、
   POS 分别 [451.6,625.2] / [1100,650]）。

## 10. 分类覆盖扫（27 个分类各一件）

`builds/ui_sweep/sweep.py`：从 `ui_catalog` 取每个分类的第一件，按 6×5 网格
（画布 1334×750）放一件、命名 `UI验证·<分类>`、绑 `UI验证点击_<分类>`，
前四件额外各绑一种槽（松手/长按/使控件显示/使控件隐藏）。可重复跑：
`request_id = uisweep-<分类号>` 幂等，且已存在的分类会被跳过。

2026-09-23 实机一轮：27 个分类全部 `placed`（ugc 365–391），0 件失败；
组件数 343→369、占用 1400→1530（每件 `occupancy_value=5`）；`save_map` 的
`pending_before_count {component: 27}`、保存后 `change_units_length` 归 0、`dropped_count` 0、
事件注册表 293→293（没摘掉任何登记）；`ui_verify` 对这批 0 问题（仅剩两件与本次无关的
待删除件：363 是本文 §9 的测试件、364 是玩家自己删的）。

这一轮**推翻了两处错判**（已改进代码）：

* 控件身份不能用 `unit.ugc_type`（它是"第一件"的 item_id）→ `_real_item_id()` 改问
  `room.get_component_item_id(ugc_id)`；
* 摆放状态机按"新实体 / owner.ugc_type==item_id"两种口径都认不到第 2..N 件控件
  （全图控件共用一个单元）→ `poll` 开头加专路 `_new_ui_components()`，按
  「请求后新出现的 200000 组件」整组交给原生确认。

另外两处小坑也记着：分类中文名不唯一（图片/「效果」5103 与 功能/「效果」5302 重名），
脚本按中文名建事件会让两件撞进同一个事件（实测 367 与 377 共用过 `UI验证点击_效果`），
现已改为带分类号命名；`--dry-run` 曾把真跑的回执覆盖掉，现在 dry 回执写
`receipt.dry.json`。

产物：`builds/ui_sweep/inventory.py` → `inventory.json`（保存后的实况：27 件、30 条事件、
5 种槽位、全部进报文、verify 无问题），其中 `cleanup_command` 就是清理这批验证件的
一条命令。

## 11. 探针与证据

| 文件 | 内容 |
|---|---|
| `probe/ui_hunt1..8.py` | 视图定位、19 个子 part、组件类型表、`_handle_2d_end` 调用者扫描 |
| `probe/ui_hunt9..13.py` | `on_check_ui_component_limit` / `_get_create_ui_components_commands` / 删除链路的字节码与枚举 |
| `probe/ui_hunt14.py` | 控件单元不在 `all_ugc_uid_set`、`_get_ugc_info(is_save=True)` 形态 |
| `probe/ui_hunt17..18.py` | 事件槽 `default_info=['', 1286]`、28 个属性的面板文案表 |
| `probe/ui_save_channel.py`、`probe/ui_switch_comp.py` | `_get_change_uid` 逐件 `change_component_id` 的实证（切换前后镜像 POS 互换） |
| `probe/ui_persist_hook.py` / `ui_persist_read.py` / `ui_change_uid.py` | 报文抓包与 `_get_change_uid()` 直读 |
| `probe/live_ui_*.json`、`probe/ui_*_36*.json` | 各 op 的实机返回（含玩家手放的 ugc_id=362） |
| `tests/handover_evidence/ui_edit_live_test.json` | 第 9 节闭环的原始数据汇总 |

离线回归：`python -m unittest tests.test_ui_edit -v`（别名自洽、坐标护栏、摆放状态机两处分流
都在、文档 op 数 == OPS 实际）。
