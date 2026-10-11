# 剧情编辑（STORY / DIALOGUE / CHOICE）

`tools/fuxiang/` 新增的剧情编辑能力：**读改编辑器里的剧情**，并**导出成文件**、**从文件导入回去**。
2026-09-21 在「稻妻"还原」地图上全流程实机验证通过。

> ## ✅ 3 处内部矛盾的裁决结果（2026-09-22 实测 + 线级验证，修复已落地）
>
> **矛盾 1「节点删除是否可信」→ A 说法（删除不可信）**正确**；B 说法（回基线=删成功）是**假阳性**。
> 裁决手段：写测试（`cli/story_write_test.py`）+ 重进 + 插桩 `GK.g_unit.send_battle_event` 抓保存报文。
>
> 关键结论：**保存时真正上送删除的是 `del_unit_uids['component']`（报文字段 `del_uid.component`），
> 不是 `ui._del_unit()`。** `_del_unit()` 只做本地移除，且对全局组件会**把标记写到存活的兄弟节点上**：
>
> | 实测项 | 数据 |
> |---|---|
> | 线级报文 | `'del_uid': {'unit': [], 'component': [167,168,169,170]}`（恰好目标、无正式剧情） |
> | 误标复现 | 袋子清空 → 删 A(targets=[167,168]) → 袋子变成 **[169,170]**（另一条存活剧情） |
> | 本地记账（不可信） | 占用 565→562、组件数 123→123、登记 75→75、`dropped_count=0` ⇒ 看着"精确回基线" |
> | 重进真相 | A **复活**（没删掉）、B **消失**（被误删）、C 消失、D 存活、4 个正式剧情全部完好 |
>
> ⇒ 三条量化事实：
> 1. **1 个剧情 = 占用值 +3**（含原生连带的首对白），同一会话内可逆。
> 2. 剧情节点**不占组件预算、不进登记集合**：`ui_component_nums` 恒 123、`all_ugc_uid_set` 恒 75。
>    故组件数/登记集合/登记数**都不是**判据，**占用值 + `story_tree`** 才是；
>    `map_info.type_distribution` 含残骸，同样不可作判据。
> 3. ⛔ **判据必须看 `del_unit_uids['component']`**（或重进），本地记账回基线没有任何证明力。
>    **修复已实现**：`fuxiang_story._reconcile_del_flags()` 在删除后把袋子校正为"恰好只有 targets"
>    （只碰剧情族的 `'component'` 桶，绝不动共用的 `'unit'` 桶）。详见 §8.6。
>    对已"复活"的节点，**重复调 `story_delete` 即可修复**。
>
> **矛盾 2「重进终验」→ 已通过（2026-09-22，两轮重进）**：4 个正式剧情 / 18 对白 / 11 选项
> **全部完好、无重复件**（类型分布逐项一致）；`ui` 记账回到最初基线（123 / 562 / 75）。
> ⚠️ 不沿用 §8.1 的「2026-09-21 通过」，以 2026-09-22 这两次为准。
> 另注：**退出编辑器会自动保存**（用户确认），所以"我手动 save 过"并不等于"只有那一次保存生效"。
>
> **矛盾 3「与 EDIT_OPS 的口径差」→ 以 `python cli/consts.py` 为准**（AST 直读源码，无需实测）。

## 1. 数据模型（实机反汇编确认）

编辑器里没有"一份剧情数据"，剧情是三种 UGC 逻辑组件的组合：

| 类型 | 编号 | 实现类 | 作用 |
|---|---|---|---|
| STORY 剧情 | 10007001 | `UGCStoryComponent` | 容器：名称 / 生效范围 / 可退出 / 跳转目标… |
| DIALOGUE 对话 | 10007002 | `UGCDialogueComponent` | 节点：`story_ugc_id` 指向所属剧情；`sort_id` 定序；`content` 是正文 |
| CHOICE 选项 | 10007003 | `UGCChoiceComponent` | 挂在对话下：`dialogue_ugc_id`；`jump_story` / `jump_dialogue` 决定跳转 |

三者都挂在 `UgcGlobalUnit` 上（**无实体、不占占用值**），数据存在
`component.component_info['events']` 属性袋里，形如 `{type: property_id, info: 值}`，
值一律是**单元素 list**：文本 `["对白"]`、数值 `[1]`、颜色 `[["A0AED6"]]`。

**编辑器「剧情」面板读的是另一张表**：`UGCStoryComponentManagerPart`（取法
`room.get_ugc_edit_part(iUGCStoryComponentManagerPart)`）上的
`story_component_sort_dict` / `dialogue_component_sort_dict` / `choice_component_sort_dict`
（`ugc_id → 同剧情内的 sort 下标`）+ `story_component_dict`。**节点不在这些表里就不"可见"**。
配额也在这里：`max_story_num=99`、`max_dialogue_num=99`、`max_choice_num=3`（一条对话最多 3 个选项）。

父子关系与顺序：

```text
STORY(816)
 ├── DIALOGUE(817, story_id=816, sort_id=1, content="…")
 │    └── CHOICE(818, dialogue_id=817, sort_id=1, content="…", jump_dialogue=819)
 └── DIALOGUE(819, story_id=816, sort_id=2, content="…")
```

⚠️ 同一类型的多个节点**共用同一个 UgcGlobalUnit**（实测：9 条对话的 owner_uid 都是 1000073）。
单元级 `ui._get_ugc_info(unit)` 的 `params` 只装得下**主组件**（`unit.ugc_component`，只有一个），
所以它**不能**当逐节点的回读判据（见 §2）。保存报文不受此影响：`_get_change_uid` 对
`change_units['component']` 里每个 uid 单独出参，实测 17 个节点 = 17 份互不相同的 params
（证据 `probe/_change_component_id.json`）。
实测删除其中 1 个节点，同单元上的其它节点不受影响（编辑器组件数各 -1），
但删除走的是原生命令层、不可部分撤销，动手前请先 dry_run。

## 2. 写入一个字段的三步法（缺一不可）

```python
comp.update_component_info(events)   # 1. 原生更新器：组件 events 赋值 +
                                     #    _owner.apply_ugc_info() 单元级同步 +
                                     #    引擎实时应用（与属性面板同路径）
comp._init_xxx(value)                # 2. 让当前客户端状态生效
                                     #    （_init_* 不在 apply_components_client 的分发表里，
                                     #     只改袋子不会生效）
                                     #    ⚠️ 只对**初始化器**调用；事件槽字段的 spec[2] 是
                                     #    事件处理器（_start_play/_on_touched/…），不能调用
ui._add_change_unit(unit.uid)        # 3. 登记进保存变更集
```

`bridge/fuxiang_story.py::_apply_fields()` 已封装这三步：**组件属性袋回读是硬校验**
（不一致才回滚），单元级只做提示——同类节点共享全局单元，镜像里本就是主组件的数据，
拿它当硬判据会把正确写入误回滚（2026-09-21 实测：两条 dialogue#1 因此变空节点）。

⚠️ **事件槽字段绝不在编辑会话里调用其"方法"**：`FIELDS` 里 `start_play_event` /
`on_touched_event` 等槽位的第三个元素是**事件处理器**名，不是 setter。实测
`_start_play()` 会走 `room.enable_story()` → 编辑器 room（`WorldBattleUGCEdit`）没有该方法
→ `AttributeError` 连累整次字段写入回滚；`_on_touched()` 则等于真的点了一次选项。
`EVENT_SLOT_PIDS` 里的槽位一律只写属性袋（引用登记照旧）。

⚠️ **不要用 `comp.init_ugc_info(info, True)`**（2026-09-21 反汇编定位的重大坑）：
它只改组件内存，从不调 `_owner.apply_ugc_info()`；而 save_change 只发 change_uid，
组件数据由**单元级** `ui._get_ugc_info(unit)` 提供。用它的结果是保存落库的一直是
服务端旧数据 —— 这正是第一版剧情内容重进即失的根因。

⚠️ **服务端创建的组件确认后必须摘除待删除标记**（同日第二个根因）：
`_add_unit_server` 同步时会把新组件 ugc_id 放进 `room.del_unit_uids['component']`
（待确认态），而原生命令层确认只清 'unit' 桶。不清的话，保存时
`_get_change_uid → get_del_unit_uids` 会把这些组件**当删除上送**（插桩实证
`del_uid.component=[353,354,355]`），服务端创建后又收到删除 → 落库为空。
修复：确认后调 `room.change_del_unit_uids(False, ugc_id, 'component')`
（已固化进 `fuxiang_placement._confirm_global_component`）。

⚠️ **原生模板同构**：面板新建节点会预置整套模板槽位（大多 `disable_delete=true`，
见 `TEMPLATE_SLOTS`，从手动创建的「剧情1」解剖固化）。API 建的节点由
`_complete_template()` 补齐成同构；选项的 content 还会镜像写进
「选项被点击」(1000700340003) 事件槽 —— 也是原生行为。

## 3. 创建节点：必须走原生 `add_unit_story` 命令

**不能**用通用摆放（`fuxiang_scene.place`）建剧情节点。摆放走的是服务端 `add_unit`，
它只把组件挂到该类型**既有**的全局单元上，不会走剧情面板那套
「建实体 + `UGCStoryObjCommand` + 登记进 `*_component_sort_dict`」的确认流程 ⇒
建出来的节点在面板注册表里缺登记，保存通道也不是剧情的那条。
（2026-09-21 走过这段弯路：17 个节点全挤在 1000072/73/74 三个单元上，后改用原生路径重建。）

原生入口在 `UGCStoryComponentManagerPart`（引擎自己的拼写是 `stroy`）：

| 建什么 | 调用 | 服务端 create_info |
|---|---|---|
| 剧情 | `part.create_stroy_component()` | `[剧情, 该剧情的首条对白]` 一次两条 |
| 对话 | `part.create_dialogue_component(story_sort_idx)` | `{component_id, self_idx, parent_idx}` |
| 选项 | `part.create_choice_component(story_sort_idx, dialogue_sort_idx)` | `{component_id, self_idx, parent_idx, brother_idx}` |

三者都发 `GK.g_unit.send_battle_event(UGC, {'uid':…, 'key': 'add_unit_story', 'create_info': […]})`，
服务端建单元+组件，客户端回调 `create_*_component_callback` 把节点登进面板表
⇒ **建完立刻在编辑器「剧情」面板可见，无需重进**。参数是**面板 sort 下标**，不是 ugc_id；
`fuxiang_story._panel_sort_idx()` 负责 ugc_id → 下标的换算，所以创建对话/选项时
fields 里必须给 `story_id` / `dialogue_id`（导入链会自动注入）。

**游戏主线程内禁止等待**，所以仍是 `begin` / `poll` 两步；轮询的判据不是请求号回执，
而是**面板表差集**（`_native_create_poll`）：新出现的 uid 就是本次建的节点。
建剧情时连带出来的那条首对白一并返回（`auto_dialogue_ugc_id`），导入链把它**复用**为文件的
`dialogue#1`（`state['alias']`），避免多出一个空节点。

```bash
python cli/story.py create dialogue -f story_id=133 -f sort_id=2 -f content=第二句
```

实测中位延迟 < 1s（首次轮询通常已 placed）；轮询结果按请求缓存，重复查询不会重复创建。

## 4. 命令行

```bash
PY="<你的-python.exe-完整路径>"

python cli/story.py tree [--compact]                    # 看剧情树
python cli/story.py fields [story|dialogue|choice]      # 看可写字段与允许值
python cli/story.py set <ugc_id> -f name=xxx [-f ...] [--dry-run]
python cli/story.py create <story|dialogue|choice> -f k=v ...
python cli/story.py delete <id[,id...]> [--cascade] [--force] [--dry-run]
python cli/story.py export [--out 路径.json|.md]        # 默认 projects/story/<地图>_<时间>.<fmt>
python cli/story.py import <路径> [--apply] [--prune]   # 默认 dry_run
python cli/story.py resume                              # 续跑被中断的导入（进度在编辑器会话里）
```

Windows PowerShell 请用 `& $PY cli\story.py ...`（bash 用 `"$PY" cli/story.py ...`）。

## 5. MCP 工具

| 工具 | 作用 |
|---|---|
| `fuxiang_story_fields` | 字段字典（语义名 / property_id / 允许值） |
| `fuxiang_story_tree` | 读剧情树（可 compact / include_deleted） |
| `fuxiang_story_create` / `fuxiang_story_create_poll` | 创建节点（异步 begin/poll，placed 自动落字段） |
| `fuxiang_story_set` | 改字段（含 dry_run、回读校验、失败回滚） |
| `fuxiang_story_delete` | 删节点（`cascade` 连带子节点；需 `allow_unregistered=true`） |
| `fuxiang_story_export` | 导出 json / md |
| `fuxiang_story_import` | 比对 → 计划；`dry_run=false` 时执行 update 并发起第一个 create |
| `fuxiang_story_import_next` | 推进创建链直到 `done=true` |

## 6. 文件格式

### JSON（权威格式，推荐）

```json
{
  "format": "fuxiang-story",
  "version": 1,
  "exported_at": "2026-09-21T03:24:47",
  "map": {"name": "稻妻\"还原"},
  "stories": [
    {"ugc_id": 824, "name": "序章", "enable_quit": 1,
     "dialogues": [
       {"ugc_id": 825, "story_id": 824, "sort_id": 1, "talker_type": 1224,
        "content": "第一句",
        "choices": [
          {"ugc_id": 827, "dialogue_id": 825, "sort_id": 1, "jump_type": 1227,
           "jump_dialogue": 825, "content": "回到开头", "enable": 1}
        ]}
     ]}
  ],
  "orphan_dialogues": [], "orphan_choices": []
}
```

`ugc_id: null` 表示"新建"。导入按 `ugc_id` 匹配（缺失即创建），逐字段 diff 出
create / update / delete / unchanged 四张清单。

### Markdown（方便人读写，同样可回读）

```markdown
# 剧情导出 — 稻妻"还原

> format: fuxiang-story / version: 1 / exported_at: 2026-09-21T03:24:47

## 1. 剧情：序章

- ugc_id: 824
- enable_quit: 1

### 1.1 对话（ugc_id=825, sort=1）

- story_id: 824
- talker_type: 1224

> 第一句

- 选项(ugc_id=827, sort=1) → 对话 825: 回到开头
    - jump_type: 1227
    - enable: 1
```

规则：`## N. 剧情：名称` 开一段剧情；`### N.M 对话（ugc_id=…, sort=…）` 开一条对话；
`> 文本` 是对话正文；`- 选项(ugc_id=…, sort=…)[→ 对话/剧情 id]: 文本` 是选项，
其下缩进 4 空格的 `- k: v` 是该选项的其它字段。`ugc_id` 写 `None` 即新建。

⚠️ 手写 md 时如果没保留 `ugc_id`，导入会认为是"新建"而不是"修改"，可能产生重复节点。
推荐流程：**先导出（带 ugc_id）→ 在导出稿上改 → 再导入**。

## 7. 字段表

事件类槽位（`start_play_event` / `end_play_event` / `quit_play_event` /
`start_event` / `end_event` / `on_touched_event`）不在这三张表里，
方向与 property_id 见 §11。

### story（剧情）

| 语义名 | property_id | 类型 | 说明 |
|---|---|---|---|
| `name` | 1000700114703 | text | 剧情名称 |
| `enable_quit` | 1000700114709 | int | 允许退出 0/1 |
| `effect_type` | 1000700114706 | int | 生效类型 1051/1052/1053 |
| `effect_camp` | 1000700114707 | str | 生效阵营 |
| `effect_class` | 1000700114708 | str | 生效角色 |
| `effect_count` | 1000700114710 | int | 可触发次数 |
| `only_trigger_self` | 1000700114711 | int | 仅自己触发 |
| `only_enable_host` | 1000700114716 | int | 仅房主生效 |
| `hide_player_units` | 1000700114717 | int | 隐藏玩家单位 |
| `jump_type` | 1000700114712 | int | 跳转类型 1511/1226 |
| `jump_target` | 1000700114713 | int | 跳转目标（剧情 ugc_id） |
| `voice_config` | 1000700114714 | str | 语音配置 |

### dialogue（对话）

| 语义名 | property_id | 类型 | 说明 |
|---|---|---|---|
| `story_id` | 1000700240001 | int | 所属剧情 ugc_id |
| `sort_id` | 1000700240002 | int | 对话顺序 |
| `talker_type` | 1000700240003 | int | 说话人类型 1223/1224/1225/1541 |
| `npc` | 1000700240004 | str | 说话人 NPC |
| `custom` | 1000700240005 | text | 自定义说话人 |
| `content` | 1000700240006 | text | 对话内容（上限 50 字） |
| `perform_type` | 1000700240101 | int | 演出类型 1229~1232 |
| `show_quit` | 1000700240007 | int | 显示退出 |
| `text_type` / `text_size` / `text_color` | 1000700217201~3 | int/int/color | 文本样式 |
| `h_align` / `v_align` | 1000700217204~5 | int | 对齐 1521~1523 / 1531~1533 |

### choice（选项）

| 语义名 | property_id | 类型 | 说明 |
|---|---|---|---|
| `dialogue_id` | 1000700340001 | int | 所属对话 ugc_id |
| `sort_id` | 1000700340002 | int | 选项顺序（1~4） |
| `jump_type` | 1000700340004 | int | 跳转类型 1226/1227/1228/1741 |
| `jump_story` | 1000700340005 | int | 跳转剧情 ugc_id |
| `jump_dialogue` | 1000700340006 | int | 跳转对话 ugc_id |
| `content` | 1000700340007 | text | 选项内容（上限 20 字） |
| `enable` | 1000700340008 | int | 是否启用 |

枚举取值的**中文含义尚未从客户端表里解出**（`all_tid_data` 查不到这些编号），
上表只给出实测到的合法取值集合；不确定时以游戏内属性面板为准。

## 8. 已知边界

1. **持久化必须保存**：所有写入只进保存变更集，需在游戏内保存（并在重进后核实）。
2. **删除留残骸**：`ui._del_unit()` 后 `cm._ugc_id2component` 仍保留条目（Ctrl+Z 撤销依赖它，
   不要清理）。本模块用会话内名单跳过它们；`include_deleted=true` 可以看到。
3. **创建串行**：服务端同时只受理一个在途摆放请求，批量创建必须 begin→poll→begin…
   （`cli/story.py import --apply` 与 `story_import_next` 已封装这条链）。
4. **索引残骸会让 `map_info` 的组件数偏高**：删除后 `type_distribution` 仍会统计到残骸，
   判定删除成败请看编辑器记账（`ui.get_all_ugc_component_nums()` / 占用值 / 登记集合）。
5. 同类型节点共用全局单元，删除是原生命令层操作，**先 dry_run 再执行**。

6. ✅ **删除的真相与修复（2026-09-22 定位完成、已修、线级验证）**

   **结论：保存时真正上送删除的通道是 `room.del_unit_uids['component']`（报文字段 `del_uid.component`），
   而不是 `ui._del_unit()`。** `_del_unit()` 只负责本地移除 + 进撤销栈。

   线级证据（插桩 `GK.g_unit.send_battle_event`，抓 evt=2006 的 `save_change` 报文）：
   ```
   'del_uid': {'unit': [], 'component': [167, 168, 169, 170]}   # 恰好是目标，无任何正式剧情
   'change_uid': {}
   ```

   **误标现象（两次独立复现）**：`_del_unit()` 对全局组件会把标记写到**存活的兄弟节点**上——
   ```
   袋子清空 → 删 A(targets=[167,168]) → 袋子变成 [169,170]（另一条存活剧情 D）
   ```
   后果（重进实测，与袋子内容 4/4 一致）：

   | 节点 | 删前状态 | 在袋子里？ | 重进结果 |
   |---|---|---|---|
   | ZZ_TEST_A | 已删（本地记账 -3、移出登记集合） | 否 | **复活** ❌ 没删掉 |
   | ZZ_TEST_B | 存活 | 是（被误标） | **消失** ❌ 误删 |
   | ZZ_TEST_C | 已删 | 是 | 消失（巧合正确） |
   | ZZ_TEST_D | 存活 | 否 | 存活 ✓ |
   | 4 个正式剧情 / 18 对白 / 11 选项 | — | 否 | **全部完好、无重复件** ✓ |

   ⇒ "本地记账回基线"是**假阳性**：它只反映本地移除，与落库无关。

   **✅ 修复后的重进复验（2026-09-22，第二轮）**：4 个 ZZ_TEST 测试节点**全部消失**，
   4 个正式剧情 / 18 对白 / 11 选项完好；`ui` 记账 组件数 123 / 占用 **562** / 登记 75
   与最初基线**逐位一致**，`type_distribution` 逐项一致（本次无残骸），待删除袋**空**，孤儿 0/0。
   ⇒ 删除修复端到端成立。

   **修复（已实现）**：`fuxiang_story._reconcile_del_flags()`；`op_story_delete` 在原生命令层之后把袋子
   校正成「恰好只有 targets」，且**只碰剧情族组件**的 `'component'` 桶：
   * 摘误标：`(bag ∩ 存活) - targets`
   * 补标记：`targets - bag`（服务端删除的唯一有效通道）
   * ⛔ 绝不动 `'unit'` 桶：全部 story 共用单元 uid **1000073**、全部 dialogue 共用 **1000074**（实测），
     标 unit 等于整族删除。

   **由此获得的能力**：对"已本地删除但重进复活"的节点，**重复调 `story_delete` 即可修复**——
   原生命令层会以「没有可删除的对象」拒绝，但标记校正照常执行（`ok=false` 且 `del_flags` 有值即此情形）。

   **遗留观察（占用值的口径）**：占用值反映的是**含本地待删除态**的本地口径，会立刻扣掉
   "本地已删但尚未落库"的节点，因此可能**低于**基线。实测对照：
   * 有 3 个"本地已删未落库"节点的会话读到 **553**；
   * 重进后（无一处在途删除）读回 **562**，与最初基线逐位一致。
   ⇒ 判据以**重进后的读数**为准；"本地删了就掉、但重进又回来"正是 §上文那张表的机制。
   （此解释由两次读数推得，未做单独验证。）

7. ⚠️ **`save_map_poll` 是一次性消费**：读到回执后它立刻清空在途标志并返回 `status=saved`，
   此后的轮询全部回 `status=no_active_request`。调用方必须在**首个定论态**（`saved`/`timeout`/
   `no_active_request`/`no_such_request`）就退出循环，否则会把**首次回执**（含 `dropped_count`）
   覆盖成无意义的 `no_active_request`。同理，`{"timeout":0.01}` 只用来**释放在途护栏**，
   它必然立刻超时、拿不到真实回执 —— 保存是否上送要看 `save_dirty.change_units_length` 是否归 0。

## 8.1 重进终验口径（2026-09-21 首次通过；2026-09-22 复验通过）

> 2026-09-22 复验结论：**通过（两轮重进）**——4 个正式剧情 / 18 对白 / 11 选项全部完好、
> 无重复件，类型分布逐项与最初基线一致，`ui` 记账回到 123 / **562** / 75。
> ⛔ 读占用值时注意：它含**本地待删除态**，会立刻扣掉"本地已删但未落库"的节点，
> 因而可能低于基线（实测有在途删除时读到 553、重进后回到 562）——判据以重进后读数为准。
> 另：**退出编辑器会自动保存**，所以"手动 save 过一次"不等于"只有那一次保存生效"——
> 排障时要把退出时的自动保存算进去。

`probe/_after_reentry.py` 一次读全：剧情树（组件袋）+ 面板 sort 表 + 区域/提示属性袋 +
事件引用 + `in_save`。判据是**逐节点内容**而不是 ugc_id——重进后 id 会重新编号
（实测 133/135 → 130/132，区域 150 → 147）。同一份存档连续两次重进结果逐项一致
（`probe/_after_reentry_run1.json` vs `probe/_after_reentry.json`）才算稳定。
删除类变动的判据见 §8.6（`del_unit_uids['component']` 或重进）。

## 9. 实测记录（2026-09-21）

| 步骤 | 结果 |
|---|---|
| 创建 STORY / DIALOGUE / CHOICE（服务端） | 一次即 placed，ugc_id 816/817/818，`editor_registered=true` |
| 写字段（三步法） | 属性袋回读一致，登记变更集成功 |
| 导出 json / md | 1 剧情 + 1 对话 + 1 选项，两种格式均可回读 |
| 改文件后导入 | 计划正确（create 2 / update 1），执行后对话正文被改写、新对话+新选项按 sort 挂好 |
| md 手写改稿导入 | 剧情改名、选项文本/jump_type/enable 更新，其余 unchanged |
| 级联删除 + 保存 | 编辑器组件数/登记集合回到基线（805 / 757），`dropped_count=0` ⚠️ **此判据已被 2026-09-22 判定为假阳性：本地记账回基线不代表落库成功，真正的判据是 `del_unit_uids['component']` 或重进。见 §8.6** |
| 收尾 | 测试节点全部删除并保存，地图类型分布与占用值回到测试前水平（索引残骸除外） |

## 10. 2026-09-21 持久化事故复盘（两处根因，均已修复）

现象：API 创建的剧情（含触发区域）在游戏内保存后重进**全部消失**。

| # | 根因 | 机制 | 修复 |
|---|---|---|---|
| 1 | 写路径不同步单元级数据 | `init_ugc_info(info,True)` 只改组件内存；save_change 只发 change_uid，组件数据由单元级 `ui._get_ugc_info(unit)` 提供 → 落库的是服务端空默认 | 改用原生 `update_component_info(events)`（内含 `_owner.apply_ugc_info()`），`fuxiang_story._apply_fields` 与 `fuxiang_edit_ops.op_edit_props` 同步修正 |
| 2 | 新组件被当删除上送 | `_add_unit_server` 同步会把组件 ugc_id 放进 `del_unit_uids['component']`；原生命令确认只清 'unit' 桶 → 保存时 `get_del_unit_uids` 把它们当删除上送 | 确认完成后 `room.change_del_unit_uids(False, ugc_id, 'component')`（`fuxiang_placement._confirm_global_component`） |

定位手段（可复用）：给 `GK.g_unit.send_battle_event` 套包装记录 evt=2006（save_change）
的 `change_uid` / `del_uid` 报文；数据在不在报文里一目了然。

修复后实测：创建 剧情353 + 对白354 + 选项355 → 单元级回读一致（组件袋==单元槽位）
→ 保存报文 change_uid 携带三组件完整数据、del_uid 为空。

✅ **重进终验：2026-09-22 通过**。同期用同一套插桩发现并修掉了**第三处根因**——
删除侧 `_del_unit()` 会把待删除标记写到存活的兄弟节点上，详见 **§8.6**（`_reconcile_del_flags`）。

## 11. 事件接线：区域触发剧情、选项触发事件（2026-09-21 实机落地）

字段方向一律按 §EDIT_OPS 10.10 的权威判据（`80001` 段第二元素 `1=发送 / 2=接收`）实测解出，
完整表见 `probe/_event_field_shapes.json`：

| 组件 | property_id | 中文标签 | 方向 |
|---|---|---|---|
| 区域 203003 | `20300311106` | 进入区域时 | **发送**（info=`[事件名, 接收范围]`，1286=全部组件） |
| 区域 203003 | `20300311110` | 使区域变成不可进入 | 接收 |
| 信息提示 203004 | `20300411205` | 使文字出现 | 接收 |
| 剧情 10007001 | `1000700114704` | 使剧情开始播放 | **接收** ← 触发的入口 |
| 剧情 10007001 | `1000700114705` | 剧情播放结束 | 发送 |
| 剧情 10007001 | `1000700114715` | 使剧情立刻结束 | 接收 |
| 对话 10007002 | `1000700240008` / `40009` | 开始对话 / 结束对话 | 发送 |
| 选项 10007003 | `1000700340003` | 选项被点击（发送事件） | **发送** ← 「选项触发事件」 |
| 选项 10007003 | `1000700340009` / `40010` | 使选项变为可见 / 不可见 | 接收 |

剧情侧的事件名**写文件即可**：`start_play_event` / `end_play_event` / `on_touched_event` 这些
语义名已在 `FIELDS` 里，`_apply_fields` 会自动建事件（`_ensure_event`）并把引用登记进
`cm._ugc_params_events_key`（`_register_event_refs`）——不登记的话保存后引用丢失。
物件侧（区域/信息提示）用 `event_bind(ugc_id, 事件名, property_id[, extra])`。
选项不写 `on_touched_event` 时，工具按原生行为自动建一个**与选项同名的**事件绑到 40003 槽。

一次完整落地的配方（`projects/story/雨夜伞铺.json` + `yuye_sapu_wiring.py`，2 剧情 / 9 对话 / 6 选项）：

1. 文件里给剧情写 `start_play_event`、给选项写 `on_touched_event` → `story.py import --apply`；
   跨剧情的分支就用事件做（选项发 `追问老妇人` → 另一条剧情的 `start_play_event` 收），
   本剧情内的分支用 `jump_type=1227 + jump_dialogue_ref`（文件里写对话序号，导入时解析成 ugc_id）。
2. 服务端摆放区域：`place_item {"item_id":203003,"position":<出生点>,"scale":[5.5046,1.1494,10.8434]}`
   （该模型原生 6.54×10.44×3.32，这组缩放出 36×12×36 的可走体积），
   再 `edit_props {"20300311104":[1],"20300311112":[999.0]}`（不设进入次数=永不触发）
   + `RUNTIME_REFRESH`（`cli/gadget.py::refresh_runtime`）让本会话立刻生效。
3. `event_bind(区域, "踏入灯下", 20300311106, extra=[1286])` → 与剧情 133 的接收槽接成一条链。
4. 回读判据（三条都要过）：
   * `event_list`：每个事件同时有发送方与接收方，且**只指向活节点**
     （删过的节点会留下失效引用，用 `probe/_event_ref_cleanup.py` 那套按面板表清掉）；
   * `story.py export`：逐节点内容与事件名回读一致；
   * `ui._get_change_uid()`（只组装不发送）：报文里每个组件 uid 各带自己的 params
     ——实测 17 节点 = 17 份互不相同的 payload。
   ⚠️ `event_list` 对**剧情组件**的槽位解析曾显示串位 —— 2026-09-23 已**根治**
   （当时只当成显示问题，实际**写入也会串**）：它按**单元级** params 下标解析，而
   同类节点共享单元，读到的是主组件那份。现在一律从**组件自身事件袋**解析
   （`own_index = index - 3`），并附 `key_ok` 自检。详见 EDIT_OPS.md「参数表结构不变量」。
5. `save_map` + `save_dirty`（`change_units_length=0` 即已上送；回执常超时，以此为准）。

编辑器内即时可见的证据口径：`UGCStoryComponentManagerPart` 的三张 sort 表里有节点 =
面板列表里有它。截图只能看到 3D 视口（区域体积与浮动文字在试玩态才渲染），**不能**当可见性判据。

## 12. 跨图导入：内部引用重映射（2026-09-23）

**问题**：剧情文档里存着**源图**的 ugc_id，而节点间的 `story_id` / `dialogue_id` /
`jump_dialogue` 写的就是源图 id。导入到**新图**时这些节点全是 create、id 全变 ⇒
原样写回旧 id，新节点就指向不存在的节点（悬空）。

**解法**：用**纯结构化的 path** 求交。

```
`_flatten` 给出的 path 只依赖文档自身数组顺序，与 ugc_id 无关：
  story#N ／ story#N/dialogue#N ／ story#N/dialogue#N/choice#N   （均 1-based）

旧 id --(_doc_id_to_path)--> 文档 path --(created 的 path→new id)--> 新 id
```

- `_doc_id_to_path(doc)`：离线、纯逻辑、可单测。
- `_node_fields(edit, node, created, id_path)`：父/跳转引用先重映射，
  **映射不到则保留原值** ⇒ 同图导入语义完全不变（这是最危险的回归方向，测试已锁）。
  `jump_dialogue` 最容易漏（它不在父引用那条链上）。
- `op_story_import_next` 每次回传累计 `created` + `id_map`（`{源ugc_id: 新ugc_id}`，
  已剔除 `old == new`）。

**给快照还原用的接口**：`id_map` 直接可并进总映射 → 重绑指向剧情节点的事件引用。
`snapshot_restore` 的「第一遍事件 → story → 第二遍补绑」三步即靠它。

**实测（和风街区，10 节点）**：`_doc_id_to_path` 覆盖 10/10；模拟导入到空白图后
**10 条内部引用全部换到新号段**（含 2 条 `jump_dialogue`），无残留旧 id。
映射模拟新号段 = 旧 id + 900000。

⚠️ **实机复核已完成（2026-09-23，空白目标图）**：`restore --apply --with-story`
在用户新开的空白图上跑通 —— 2 剧情 / 4 对话 / 4 选项共 10 节点全部创建，
`story_import_next` 回传 `created` 10 条、`id_map` 10 条、`import_done=true`，
`_apply_fields` 逐节点写入无告警，第二遍补绑把 6 条指向剧情节点的事件引用全部接上。
终态回对 **9/9 全对**（详见 `SNAPSHOT.md`）。

## 13. 「看不出错」的三类静默失败（2026-09-23 实机定位，改 `_apply_fields` 前必读）

这三条共同点：**本地读回来一切正常**，只有跨图/重进才暴露。已全部修好并加锁
（`tests/test_story_field_guard.py`）。

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | STORY 只补到 **2 个模板槽**（正常 13），事件补绑"装不下下标"并**静默丢失** | 源图 `edit_time=""` ⇒ `int("")` 抛 ValueError ⇒ `_apply_fields` **整节点回滚**（一个坏字段废掉整份字段写入） | `decode_value`：数值型字段的空串归 `None`；`_apply_fields` 遇空值/编码失败**跳过该字段**并回传 `skipped_empty`/`encode_bad`，**不再整节点回滚** |
| 2 | 4 条指向「所属对话 ugc_id」的**幽灵引用** | `_register_event_refs` 登记的是**组件本地下标**，少加 `PARAMS_HEADER` ⇒ 注册表里的引用指到别的字段上 | 登记 `own_index + _params_header()`；偏移量只允许 `fuxiang_story._params_header()` 一处定义（测试锁 == `edit_ops.PARAMS_HEADER` == 3） |
| 3 | 还原**静默吞掉字段写入错误**：节点建出来了但字段没写全，本地看不出区别 | `apply_story` 不看 `fields_error` | 收集逐节点 `fields_error`/`encode_bad`/`skipped_empty` 回传并打印；收尾做事件引用回对 |

⚠️ 与 §12 的重映射叠加后的**联动坑**：`_apply_fields` 回滚会让节点缺模板槽，
而缺槽的节点**事件引用补绑会失败**（找不到该 property_id 的槽）—— 于是"跨图导入重映射"
看起来没问题，事件却静默少一条。所以**回读必须比 `property_id`，不能只看引用存在与否**
（见 EDIT_OPS.md §10.1 的 `key_ok` 说明）。
