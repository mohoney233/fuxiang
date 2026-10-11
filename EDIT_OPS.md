# 可编辑物体的数值修改（位置 / 旋转 / 缩放 / 属性）

> 本模块**只改写已有对象**的数值，不创建、不删除、不摆放。
> 摆放与删除逻辑仍在 `fuxiang_scene` / `fuxiang_placement`，本文不涉及、也未改动。
> 实机断言见 `tests/mcp_interface_result.json`，探针见 `probe/zz_edtest*.py`。

---

## 一、能改什么

| 能力 | 对象范围 | 接口 |
|---|---|---|
| **位置 / 旋转 / 缩放** | 支持 UGC 变换接口的物件实体 | `fuxiang_edit_transform` |
| **组件属性数值** | 有属性 schema 的组件 | `fuxiang_edit_props` |
| **从场景删除** | 编辑器已登记对象（含组合级联） | `fuxiang_edit_delete` |
| 查询可改对象与原因 | 全部组件 | `fuxiang_edit_targets` |
| 查单个对象的变换 + 属性 + 属性定义表 | 全部组件 | `fuxiang_edit_target` |
| 回看本会话改动 | — | `fuxiang_edit_history` |

实测（本机地图）：**166 个对象，69 个可编辑**（2026-09-22 实测）
⚠️ 本行原写「119 个对象，70 个可编辑」，是 119 对象时期的旧值，已作废；
可编辑数随地图变动，请用 `cli/fxapi.py edit_targets '{"limit":0}'` 实测自己地图。

---

## 二、两种数值的模型

### 1. 变换（位置/旋转/缩放）

存在**实体 unit** 上，不在组件里。写入必须同时更新三处，否则渲染、碰撞、逻辑位置会不一致：

```python
unit.set_ugc_size(math3d.vector(*scale), None, old_size)   # 缩放（含碰撞尺寸）
unit.update_col_ugc_size()
unit.set_ugc_rot(math3d.euler_to_matrix(math3d.vector(*rot) * (pi/180)))  # 旋转（度 → 弧度）
unit.set_position(math3d.vector(*pos))       # 渲染 + 碰撞位置
unit.set_ugc_position(math3d.vector(*pos))   # 逻辑位置（服务器保存读的是这个）
```

再调 `edit.reg_change(ui, unit, comp)` 把它放进编辑器的**保存变更集**
（= 原生 `ui._add_change_unit(unit.uid)` + 把**本组件**补进 `change_units['component']`；
共用单元时只调原生那句会登记到"当前"那个组件上，见 §9.5）。

⚠️ 只有普通 UGC 物件实体（`ugc_dir` / `ugc_size` / `set_ugc_*` 齐备）能这么改。
角色类实体（`MyCivilianUnit` 等）走另一套变换，接口会**明确拒绝**而不是报错。
> 2026-09-19 补充澄清：`MyCivilianUnit`/`BoxUnit`/`CrowUnit` 等角色与交互实体**其实具备全部
> 变换 setter**（set_ugc_size/set_ugc_rot/set_position/set_ugc_position），只是**没有
> `ugc_dir`/`ugc_size` 读属性**。所以：① 摆放侧已改容错（读不到旧尺寸用 (1,1,1)），
> 摆放不再失败；② **改动侧**仍被护栏主动拒绝，原因是无法回读校验变换（不是实体不支持）。
> 若要改这类对象的变换，用 `edit_transform` 的 `persist_via_recreate=true`（删旧摆新）。

### 2. 组件属性（`component_info['events']`）

每个组件带一个属性袋：

```python
component.component_info == {
    'component_id': 101001,
    'events': [
        {'type': 10100101001, 'info': ['TID_BATTLE_UGC_TEAM_9001']},   # 阵营
        {'type': 10100101002, 'info': ['TID_UGC_CLASSIFY_101']},       # 职业
    ],
}
```

- `events[i]['type']` 是 **property_id**（不是 event_type）。
- `info` 是值（多为 list，少数标量）。
- 属性定义在 `GK.DM.ugc_item_property[component_id]`，层次为
  `mode_id -> class_id -> {item_id: property_def}`；`property_def` 含
  `property_id / property_type / property_params / tab_id / extra_params_1`。

**property_id → 应用方法**的映射链（组件类级常量）：

```
ITEM_PARAM_2_EVENT_TYPE[property_id] → event_type
EVENT_TYPE_2_METHOD_NAME[event_type] → 应用方法名（如 _init_camp / _set_collision）
```

例（UGCCivilianComponent）：`10100101001 → 13 → _init_camp`；`10100101002 → 14 → _init_class`。
例（UGCObstacleComponent）：`1 → _set_collision`，`7 → _change_direction`，`8 → _set_lod`。

> 这两张表的**值可能是标量也可能是 list**（不同组件类定义不一致），
> 查表前必须确认可哈希 —— 已踩过 `TypeError: unhashable type: 'list'`。

### 3. `property_type` 编码（归纳，非官方文档）

| 编码 | 含义 | 例 |
|---|---|---|
| 30001 | 开关/枚举，`property_params` 形如 `{TID,默认}` | `10000002001` |
| 50001 | 枚举选择，`extra_params_1` 带引擎联动提示 | `10000002002` |
| 20011 | 数值/档位 `{TID,min,max,步进}` | `10000002003` |
| 20001 | 数值 `{TID,个数,min,max,上限}` | `20391202001` |
| 70001 | 引用选择 `{序号,上下文组件id}` | `20391210001` |
| -1/-2/-3/-4 | 内建变换类：POS / REVOLVE / SCALE / SIZE | `20391210001~4` |

---

## 三、写入路径（原生三步，非自造）

```
1) comp.update_component_info(new_events)          # 袋赋值 + _owner.apply_ugc_info() 单元级同步
                                                   # + apply_components_client 引擎应用（三步一气）
2) edit.reg_change(ui, unit, comp)                 # 进编辑器保存变更集（共用单元时带上本组件）
```
> ⚠️ 旧版这里写的是 `init_ugc_info(only_save=True)` + 手动 `apply_components_client` 两步；
> `init_ugc_info(..., True)` 在 `UGCBaseComponent.py:452` 直接 return，两者都不做，
> 改属性不要用它（摆放时新建空袋才用）。

第 2 步与游戏的增量机制同源 —— 服务器下发属性增量时走的是
`cm.add_last_update_components(params)` → `cm.do_last_update_components()` → 组件
`do_last_update(info)`，而 `do_last_update` 的实现正是
`apply_components_client(info['events'])` + 对 `info['keys']` 逐个
`set_property(name, getattr(self, name))`。
本模块复用了同一条应用器，没有另起一套。

可选 `sync_keys`：对指定内部属性名再跑一次 `set_property`，触发
`client_property_change_cb_map` 里的回调（属性面板同步用）。

---

## 四、安全护栏（写入前必过）

1. 只允许改**编辑器已登记**的对象：在 `cm._ugc_id2component` 且 unit 存活
   （`unit_mgr.get_unit` 校验同一对象）且在 `ui.all_ugc_uid_set` 中。
2. 拒绝**待删除**对象（`room.del_unit_uids['unit']`）。
3. 变换前校验实体**支持 UGC 变换接口**；角色类等直接拒绝并说明原因。
4. 属性写入前校验每个 `property_id` **属于该组件类型的 schema**；
   未知名直接拒绝（要强行写需显式 `allow_unknown_property=true`）。
5. 写入后**回读校验**；不一致则**自动回滚**到写入前的值，并如实返回回滚结果。
6. `dry_run=true` 只算不写。
7. 每次改动写一条**审计记录**（挂 room，最多 100 条），可用 `fuxiang_edit_history` 回看。

---

## 五、实机验证（本机，全部带复原）

测试对象：`ugc_id=10`（`UGCMotionPointComponent`，遗留物件，原位 `[0, -65536, 0]`）。

| 步骤 | 结果 |
|---|---|
| `edit_transform(dry_run)` | 返回 `would_set`，**未落盘**（回读仍为原值）✔ |
| `edit_transform(position=[626,0,-100])` | `ok:true`，回读一致，`registered_in_change_set:true` ✔ |
| 复原回 `[0,-65536,0]` | `ok:true`，回读一致 ✔ |
| `edit_props(dry_run)` | 返回 `would_write: [{'type': 20391210001, 'info': [1,2,3]}]` ✔ |
| `edit_props(props={'20391210001': [1,2,3]})` | `ok:true`；属性袋 `[] → [{'type':20391210001,'info':[1,2,3]}]`，回读一致 ✔ |
| `edit_props(remove=['20391210001'])` | `ok:true`；属性袋回到 `[]` ✔ |
| 未知属性 `99999999999` | **被拒绝**：「属性不在该组件类型的 schema 中」✔ |
| 角色类 `ugc_id=1` 改变换 | **被拒绝**：「实体类型 MyCivilianUnit 不支持 UGC 变换接口」✔ |

`fuxiang_edit_history` 记录了上述 3 次真实写入（2 次变换 + 1 次属性），含前后值。

---

## 六、接口清单

| 工具 | 用途 |
|---|---|
| `fuxiang_edit_targets` | 列对象 + 可编辑性判定（含受限原因），可按 `component_type` / `item_id` 过滤 |
| `fuxiang_edit_target` | 单对象：变换 + 属性袋；`with_schema=true` 联表属性定义（含当前值/event_type/应用方法） |
| `fuxiang_edit_transform` | 改位置/旋转/缩放；`dry_run` 预览 |
| `fuxiang_edit_props` | 改属性（`props` 写入 / `remove` 清除）；`dry_run` 预览 |
| `fuxiang_edit_history` | 本会话改动记录 |

游戏侧 op：`edit_targets` / `edit_target` / `edit_transform` / `edit_props` / `edit_history`。

### 典型流程

```
fuxiang_edit_targets()                                  # 找可编辑对象
fuxiang_edit_target(ugc_id=116, with_schema=true)       # 看它的变换与属性定义
fuxiang_edit_transform(ugc_id=116, position=[630,0,-125], dry_run=true)   # 预览
fuxiang_edit_transform(ugc_id=116, position=[630,0,-125])                 # 写入
fuxiang_edit_props(ugc_id=116, props={"10000002001": [1]})                # 改属性
fuxiang_edit_history()                                  # 回看
```

---

## 七、已知限制

1. **持久化仍在游戏侧**。本模块只保证「值已写入本地对象并登记进保存变更集」，
   返回里一律 `persistence_verified: false`。是否落库需在**游戏内保存 + 退出重进**后核实
   （地图保存是服务端权威，接口不绕过）。
2. **属性应用的完备性按类型而异**。变换路径已完整验证；属性路径对
   `203912`（MotionPoint）与 `100001`（GEOMETRY 的材质/颜色/透明度/碰撞）的写入/回读/持久化
   均已实测通过，**其它组件类型未逐一验证**。图片上传类属性仍未纳入。
3. **角色类对象不能改变换**（只能改属性）。若要改角色，需另找该实体类型的变换接口。
4. 属性值的**合法域**目前不在接口层强校验。GEOMETRY 的材质/颜色/透明度/碰撞编码已于
   2026-09-18 完全解码（见上文「实测补充」）；其余类型写入前建议先 `dry_run`。
5. 与摆放接口的并发：写入期间不建议同时手动拖动同一对象。

### 实测补充（2026-09-18，几何组件 100001：编码语义 + 视觉 + 持久化闭环）

本轮通过 `apply_components_client` 逐层反汇编 + 实机写入 + 引擎截图 + 保存重进，
把四个视觉类属性的**编码语义**全部坐实（证据：`tests/mat_test/`）。

#### 应用链（反汇编实证）

`apply_components_client(events)` 对每个 `{'type': property_id, 'info': [...]}` 取 **`info[0]`** 分发：

| property_id | 语义 | event_type | 应用方法（info[0] 传入） | 值格式（**引擎层真实格式**） |
|---|---|---|---|---|
| `10000110005` | 材质 | 1 | `_client_set_material` → `unit.set_model_materials` → `utils.change_mtg` | **mtg 路径字符串**，如 `scene\model\h55_ugc\h55_ugc_mtg\h55_ugc_mtg_brick01.mtg` |
| `10000110006` | 颜色 | 2 | `_client_set_color` → `caculate_usable_colors` → `unit.set_model_colors` → `UtilsRender.hex2rgba` | **`[[hex]*N]`**——外层是 info 包装，内层每槽一个 **6/8 位十六进制字符串（无 `#`）**，如 `[["FF0000","FF0000"]]` |
| `10000110007` | 透明度 | 3 | `_client_set_alpha` | `[0~100]`（百分比，0=不透明） |
| `10000102001` | 碰撞 | 4 | `_client_apply_collision`（内部 `switch == 1` 比较） | `[0]`（无碰撞）/ `[1]`（有碰撞，默认） |

关键细节：

1. **材质值是路径，不是数字 id**。schema 里 `property_params={8001~8004;...}` 是 **UI 面板下拉域**；
   实测传 `"8010"` **静默不生效**（引擎不解析 id，保持旧材质）。合法路径全集 =
   常量表 `room.ugc_const_data_mgr.get_const_data(ComuKeys.UGC_EDIT_CONST_KEY.MATERIAL)`
   （**163 项，键=mtg 路径，值=(槽位类型 `mat1`~`mat4`, 附加参数)**）。
2. **颜色槽位数由材质决定**：`caculate_usable_colors` 按 `mat1`~`mat4` 把颜色列表截断/补齐到 1~4 槽。
   默认材质=1 槽（只有第一槽生效）。`hex2rgba` 对非法值**静默回退白色**，不报错。
   传裸 int（如 `[7020]` 或 `[7020]*4`）会在 `len(int)` 处 `TypeError`（色号 7001~7035 同样只是 UI 域）。
3. **保存格式约束（修正旧结论）**：`cm.get_save_material_info` 对材质值做 `.split()`——
   材质值必须是**字符串**（传 int 列表会让 `_save_change` 抛 `AttributeError`）；但字符串内容应是
   **mtg 路径**而非 `"8001"` 这种 id。
4. **`edit_props` 传参形态速查**（`_as_info` 归一化 + 回读比对）：

```python
# 材质（字符串包一层 list）
edit_props(ugc_id=116, props={"10000110005": ["scene\\model\\h55_ugc\\h55_ugc_mtg\\h55_ugc_mtg_brick01.mtg"]})
# 颜色（[[hex]*4]，嵌套两层）
edit_props(ugc_id=114, props={"10000110006": [["FF0000", "FF0000", "FF0000", "FF0000"]]})
# 透明度 / 碰撞（标量包一层 list）
edit_props(ugc_id=116, props={"10000110007": [80], "10000102001": [0]})
```

   ⚠️ 传法不对时护栏会报「属性回读不一致」并**自动回滚**（不是引擎拒绝）——先对照上表检查形态。

### 7.1 读运行时材质：`material_group` 不是序列（用 `fuxiang_material_info`）

引擎的 `model.all_materials` 返回 `render.material_group`，它**没有 `__iter__` / `__len__` /
`__getitem__`**，所以 `list(model.all_materials)` 必抛
`TypeError: 'render.material_group' object is not iterable`（2026-09-20 实机）。正确取法是
`for i in range(mg.count): mg.get_at(i)`，槽位类型 `render.material`。

实测可用字段与陷阱：
- 有信息量的是 `slot.get_technique_name()`（如 `shader/ugc/ugc_basic.nfx`）、
  `slot.transparent_mode` / `slot.alpha`，以及 **`model.get_material_group_data()`**
  ——它返回**原生 mtg 名**（如 `h55_ugc_geometry_box01`），不是结构体。
- `slot.filename` 与 `slot.shader` **常是空串**（别按名字找，会误判成"没有材质"）；
  `model.get_sub_material(0)` 实测返回空串。
- 属性袋（`10000110005`）与运行时可能不一致：例如 ugc 10343 属性写
  `h55_ugc_mtg_faguang01.mtg` 而 `group_data` 是 `h55_ugc_mtg_floor03`。
  **不一致 ≠ 渲染失败**，只是"该 mtg 未在这个会话的实体上生效"的线索，必须截图确认。

现成接口（只读，单批 ≤50，自动标出 `mismatch`）：

```bash
python cli/fxedit.py material_info '{"ugc_ids":[10342,10343]}'
```

MCP 侧同名工具 `fuxiang_material_info`。

#### 视觉 + 持久化闭环（本轮实证）

| 步骤 | 结果 |
|---|---|
| 写入后实拍（`tests/mat_test/t*_after.png`） | 114 白→**正红**（FF0000）；116 白→**砖纹**（brick01 路径）；透明度 0/50/80 三档肉眼可辨 ✔ |
| 裸 id `"8010"` 对照 | 视觉无变化（静默保持旧材质）——坐实「必须传路径」 ✔ |
| 主动保存 `fuxiang_save_map` | 脏缓冲 `change_units` 3→0（已上送服务端） ✔ |
| 用户退出重进同一地图 | 属性袋回读逐项一致：颜色/材质路径/透明度/碰撞**全部保留**；重进后实拍红色与砖纹依旧 ✔ |

即：**这四个属性走 `edit_props` 原生三步写入 → 保存 → 重进，持久化闭环成立**（引擎渲染层真实生效，
不只是属性袋文本）。证据图：`tests/mat_test/t114_persisted.png`、`t116_persisted.png`。

---

## 八、从场景删除（破坏性操作）

### 机制（逆向 + 实机验证）

游戏删除按钮的完整链路是 `UIBattleUGCEdit._del_unit()`：

```
1. operate_units = self._get_operate_units()      # 从多选状态 ui.choose_uid_list 取
2. if not operate_units: return
3. _check_del_anchor_unit(operate_units)          # 锚点保护
4. operate_units = _check_group_units(...)        # 展开「成组」对象
5. _on_del_player_num_check(...)                  # 角色数量校验
6. play_ugc_se('delet')                           # 音效
   → 内部走到 _do_normal_del_commands(operate_units)
     → 清理照片数据 → 七道校验 → 组合级联 → UGCGroupDelCommandObjCommandReverse
     → _do_command(command)  # 撤销栈 + 占用刷新 + 组件数刷新 + 变更登记
     → 逐个 on_del_unit_setup_mgr(uid)
```

**关键：它从 `ui.choose_uid_list`（多选状态）取对象**。所以接口的做法是：
把目标 uid 写进 `choose_uid_list` → 调 `ui._del_unit()` → **恢复原状态**。
直接调 `_do_normal_del_commands(units)` 而不设置选中状态会**静默无效**（无异常、无效果），
这是本轮踩过的坑。

### 接口

| 工具 | 用途 |
|---|---|
| `fuxiang_edit_delete_preview` | 只读：将删清单、组合级联预估、被拦目标及原因、当前占用/组件数 |
| `fuxiang_edit_delete` | 执行删除（支持 `dry_run`、批量 ≤50、`skip_rejected`） |

护栏：只删编辑器已登记对象（历史遗留物需显式 `allow_unregistered=true`）、拒绝待删除、
**不支持按 item_id/位置模糊圈选**、部分不可删时默认整体拒绝、删除后回读校验、审计留痕。

### 回读判据（重要）

删除后以**编辑器记账**为准：
- 占用值下降（`get_current_occupancy`）
- 组件数下降（`get_all_ugc_component_nums`）
- 对象移出 `all_ugc_uid_set`

**不要用 `unit_mgr.get_unit()` 或组件索引判断**——它们的清理由引擎延迟完成，
删除后立即查仍可能返回对象（实测如此），不代表失败。

### 实测记录（两次）

| 对象 | 结果 |
|---|---|
| `ugc_id=10`（运动点） | **被原生保护拦下**：占用/组件数无变化，回读如实报 `ok=false, still_remaining` —— 运动点不可经此路径删除 ✔ |
| `ugc_id=114`（原点遗留障碍物） | 通过 `choose_uid_list` 路由后**删除成功** ✔ |
| `ugc_id=130`（现场摆放的测试桌 10148） | **删除成功**：占用 264→254（-10 正是该物件占用值）、组件数 119→118、移出登记集合 ✔ |

第三条即「摆放→删除」往返，场景净变化为零。

### 已知限制

1. **运动点（203912）不可经此路径删除**（原生保护）。它们是地图的结构性组件
   （通常挂在 y=-65536 未落位哨兵位），本来就不该删。
2. **持久化仍需游戏内保存**；删除会进撤销栈，游戏内 Ctrl+Z 可撤。
3. 批量一次最多 50 个；删除组合父件会级联子件（原生命令层自动处理）。
4. 删除前**务必先 `fuxiang_edit_delete_preview`**——这是破坏性操作。

---

## 九、运动器（MOTION, 203911）——解读与接口调用（2026-09-18 实机闭环）

「运动器」= 组件类型 **203911**（`UGCMotionComponent`，目录名「运动器」，基础分类，可创建）。
它挂在 **UgcGlobalUnit 全局逻辑单元**上（无实体模型，编辑器列表可见），是路径/旋转运动的**控制器**；
编辑器里另有一组 **运动点 203912**（`UGCMotionPointComponent`）只是 UI 层摆路径点的辅助标记，
运行时不直接参与（路径点数据存在运动器自己的属性里）。

### 9.1 属性编码（运动器 203911 自身）

| property_id | event_type | 应用方法 | 语义 | 实测默认 |
|---|---|---|---|---|
| 20391102001 | 1 | `_set_name` | 名称 | `"运动器1"` |
| 20391102002 | 2 | `_set_type` | 运动类型：**1641=PATH 路径 / 1642=ROTATION 旋转** | `"1641"` |
| 20391102003 | 3 | `_set_init_move` | 初始是否运动 | False |
| 20391102004 | 4 | `_begin_move` | 开始运动（触发） | — |
| 20391102005 | 5 | `_end_move` | 停止运动 | — |
| 20391102006 | 6 | `_set_move_time` | **全程总时长（秒）** | 9999.0 |
| 20391102007 | 18 | `_set_move_type` | 计量方式：**1711=TIME / 1712=COUNT** | `"1711"` |
| 20391102008 | 19 | `_set_move_count` | 次数（COUNT 模式） | 10.0 |
| 20391102009/10 | 20/21 | `_reset_move`/`_pause_resume` | 复位 / 暂停-继续（1721=RESUME/1722=PAUSE） | — |
| 20391118801 | 7 | `_set_path_type` | 路径方式：**1651=SCROLL**（其余 1652 RETURN/1653 REPEAT/1654 RECIPROCATE 往返/1655 ONEWAY 单程） | `"1651"` |
| 20391118802~4 | 8/9/10 | `path_back`/起终点等待 | 往返与起终点停留 | 0 |
| 20391118904~8 | 12~16 | 旋转参数 | 旋转类型/单程时长/等待/整圈时长/等待 | — |
| **20391119001** | 17 | `_set_point_info` | **路径点表** | `[]` |

**路径点条目格式**（`_get_now_value` 插值逻辑反汇编 + 原生摆点回读实测）：每项 =
`[bt, [x,y,z], [dir_x,dir_y,dir_z], [sx,sy,sz]]`（**4 字段**），
但**属性袋里的值要把点列表再包一层**：`info = [[点, 点, ...]]`；
运行时 `comp._point_info` 才是不包层的 `[点, 点, ...]`。
> ⚠️ 2026-09-22 两次修正：① 旧版把点写成 4 字段但**漏了外层那层 list**，
> 导致直写 `20391119001` 永远存不住（`check_value_type_valid` 判非法换回默认 `[[]]`）；
> ② 中途我按 `_get_now_value` 里出现的 `et` 误加成 5 字段，原生实测点就是 4 字段，
> `et` 是插值期算出来的、不入袋。权威形状与修法见 §9.5 末尾。
> 原生样本每点 `bt` 是同一个常数（如全 3.0），不是"累计到达时刻"。
⚠️ 2026-09-27 原生 REPEAT 实测修正：条目第一项是**本段耗时**，构建器逐段累加为运行时时间线。4 个 1 秒点的周期是 4 秒；3700 个点可组成 154.1667 秒周期。不要把累计时刻直接写入属性袋。
绑定时引擎自动把**对象当前位置**作为 0 时刻首点追加到表头。全 0 会让时间线塌缩（total_time=0，不动）。
插值器按 `pos/rot/scale` 三通道 lerp，`rot` 为欧拉角增量。

2026-09-27 保存重进及原生试玩验证：PATH/REPEAT 枚举写整数 `[1641]` / `[1653]`，初始运动开关写整数 `[1]` / `[0]`，计量方式写 `[1711]`。不要依赖布尔值：后续 660 线段实测中 `[false]` 重进被恢复为默认 `[1]`，整数 `[0]` 才正确保留。配置、对象绑定都保存后，点击原生试玩即可自动开始，无须 `motion_preview` 或外部逐帧更新。`edit_props` 已改为无截断复制持久化属性，防止修改名称等无关属性时把长路径截为 40 点。少量运动器证据见 `projects/shunran_native_test/`；真实片段的组件预算、启动偏差和观影视角验证见 `projects/shunran_clip_test/`，不代表已验证发布后的其他玩家。

### 9.2 对象侧绑定（持久化载体）

被驱动对象（如 GEOMETRY 100001）的属性 **`10000118701`**（et14 `_set_motion(motion, children)`）：
属性袋格式 `{'type': 10000118701, 'info': [[运动器ugc_id, ...]]}`（info[0] = 运动器 ugc_id 列表）。
⚠️ `apply_components_client` **不分发** et14（无该分支）——写袋后需手动触发绑定（见 9.3）；
重载后由 `_auto_regist_motion_mgr` 读袋向 `iUGCMotionComponentManagerPart` 注册。

### 9.3 编辑模式驱动链（反汇编 + 实机验证）

1. `comp._set_motion([运动器ugc_id...], children)` —— 仅设 `_motion/_motion_chilren` 内存值
2. `unit.preview_motion_component(True, {'motions': [...], 'motion_chilren': [...]})`
   —— **编辑模式的正规入口**：内部 `clear → bind_unit(unit, idx) → create_motion_component(init_info)
   → unit_mgr.add_unit_tick(unit)`（把单元注册进引擎 tick 循环）
3. `m.begin()` —— preview 创建的 runtime `begin_time=None`，必须显式开始
4. 引擎每帧 tick → `get_vaule(game_time)` 产生 `pos/rot/scale` **增量** →
   `unit.update_motion_dict` 应用（`set_position` 渲染位；**不写 `set_ugc_position` 逻辑位**）

⚠️ 三个坑（全部踩过）：
- `u.position` 读数是**逻辑位缓存**，运动中读它恒为起点——**实机位置以渲染/截图为准**；
- `_bind_unit_motion`（deferred init 路径）在编辑房会因 `set_motion_chilren` 缺失而抛异常
  （绑定其实已完成），绕过法是直接走 `preview_motion_component`；
- `_check_detal_time` 在 `detal > move_time` 时 clamp 并调 `end()`（begin_time 置 None），
  周期结束后需重新 `begin()`（SCROLL 模式不自动循环）。

### 9.4 接口调用

新增游戏侧 op **`motion_preview`**（`fuxiang_edit.py` OPS 表）：

> ⚠️ 2026-09-22 更正：此处原写「MCP 工具名 `fuxiang_motion_preview`」，**该 MCP 工具并不存在**
> （`mcp/server.py` 的 66 个 tool 中没有它，op 有、tool 未注册）。调用请用 CLI：
> `python cli/fxedit.py motion_preview '<json>'` 或 `python cli/fxapi.py motion_preview '<json>'`。

```jsonc
// 绑定 + 配置路径 + 开始运动
{"ugc_id": 114, "motion_ugc_ids": [117], "flag": true,
 "move_time": 6.0,
 "point_info": [[2.0, [-510,50,150], [0,0,0], [1,1,1]],
                [2.0, [-464,50,190], [0,0,0], [1,1,1]],
                [2.0, [-420,50,150], [0,0,0], [1,1,1]]],
 "persist": true}          // persist=true 时把绑定写入 10000118701 属性袋
// 停止：{"ugc_id": 114, "motion_ugc_ids": [117], "flag": false}
```

实机结果（本地图）：红方块沿 3 点路径 6 秒走完、带朝向旋转、终点停住；
连拍证据 `tests/mat_test/motion_run_t1/t2/t3.png`、`op_motion_flight.png`、`motion_check.png`。

### 9.5 持久化（会话内已做，待重进复核）

- 绑定：114 袋 `10000118701=[[117]]`（走 op_edit_props 原生管线 + 保存，脏缓冲 1→0）✅ **已实测跨重进保留**
- 运动器配置：117 袋 `20391119001=[路径点]`、`20391102006=[6.0]` —— 117 是全局组件、
  **不在编辑器登记集合**，`edit_props` 护栏会拒绝。解法：写 `update_component_info(events)`
  后调 **`ui._add_change_unit(全局unit.uid)`**。`motion_preview` 的 `persist=true` 已固化这条链
  （对象袋 + 运动器袋 + 变更集登记）。
  ⚠️ **2026-09-22 实机 A/B 推翻了我自己前一版的判断**。曾据反汇编（`UGCBaseComponent.py:444-453`
  `only_save` 分支赋值后即 return、不跑 `_owner.apply_ugc_info()`）断言旧写法「静默不落库」，
  **错了**。对照实验（同一个袋、同一个属性 `20391102006`，脚本 `probe/_mt_ab.py`）：
  `init_ugc_info(..., True)` 与 `update_component_info(...)` 之后，单元级镜像**都**跟到了新值。
  真正机制在 `probe/_mt_readthrough.py` 量出来：`ui._get_ugc_info(unit)['params']` 里那些 param
  **对象就是主组件袋里的同一批 dict**（`is` 比较为 True、原地改袋镜像立刻跟随）。所以——
  * 组件**独占**一个 unit（运动器 203911、全局规则 10001001、胜利条件 10002001 都是）：
    两种写法都能进报文，`init_ugc_info` 并不漏落库；
  * 组件与兄弟**共享** unit（实测 1000073 挂 4 个 STORY、1000074 挂 18 个对话、
    1000075 挂 11 个选项、1000061 挂 48 个 UI_NATIVE）：镜像只有主组件那一份，
    改非主组件的袋不动参数表 ⇒ 只有走 `update_component_info`（跑 apply_ugc_info）才推得进去。
    **这才是 2026-09-21 剧情内容重进即失的真实成因**，与本次运动器无关。
  ✅ 复验结果（本图新建运动器 170 + 几何体载体 171，`motion_preview persist=true`）：
  保存报文 `evt=2006 save_change` 里 `change_uid['component'][0][170]` 带
  `20391102006=[5.0]`、`20391119001=[3 个路径点]`，载体在 `change_uid['unit'][0][1000109]`
  带 `10000118701=[[170]]`；`del_uid` 两桶皆空、`change_units` 归 0、`dropped_count=0`。
  仍保留 `update_component_info` 写法，但理由改成：保留原生 component_info 容器、
  顺带 `apply_components_client` 让配置实时生效、与共享单元组件的写法统一。
  **落库缺一不可的是 `ui._add_change_unit(unit.uid)`。**
  ⚠️ 通道分叉：全局组件走 `change_uid['component'][<ugc_id>]`，普通对象走
  `change_uid['unit'][<unit_uid>]`；判据要按这个去报文里找，别看脏缓冲数量。
  🔴 **2026-09-22 重进终审结果（推翻"报文里有=存得下"）**：同一张图退出重进后按内容核对——
  时长 `20391102006=[5.0]` ✅ 留住了、载体绑定 `10000118701=[[169]]` ✅ 留住了，
  但**路径点 `20391119001` 回读变成 `[[]]`、组件内部 `_point_info=[]` 全丢**，
  而保存报文里明明带着那 3 个点（`[[2.0,[660,16.5,-115],[0,0,0],[1,1,1]], ...]`）。
  ⇒ **客户端发出的报文含嵌套数组，不代表服务端存得下**；扁平标量（`[5.0]`）能过，
  嵌套 list-of-list 过不去。另排查掉一个可能：路径点**不是**存在运动点 203912 组件里
  （本图 50 个 203912 的袋全空、位置恒为 y=-65536，纯 UI 标记，与本文 §9 开头一致）。
  尚未定位到正确写法（待试：全 float 叶子 + 补 t=0 首点；或改走原生摆点流程）。
  🔴 **2026-09-22 追加定位（本地直跑校验器，不用再烧重进周期）**：
  `UGCMotionComponent.check_value_type_valid(cls, events, params)` 对 `20391119001`
  **无论喂什么形状都返回它的默认值** —— 试过 4 元组、5 元组
  `[bt,[x,y,z],[dx,dy,dz],[sx,sy,sz],et]`（`_get_now_value` 里的权威 5 字段形状，
  本文 §9.1 的格式表漏了末尾的 `et`）、全 int、扁平 10 标量、dict 列表、`[]`，
  全部被替换成默认；只有字面量 `[[]]` 能"通过"。⇒ **直写属性袋这条路本身存不住路径点**，
  不是编码形状问题。且该函数有共享状态副作用（连续调用后返回的默认值嵌套会自增深：
  `[[]]` → `[[[]]]`），别拿它当纯函数反复调。
  另两条实测约束：① 多个运动器**共用一个 UgcGlobalUnit**（5 个都在 unit 1000094 上），
  单元级镜像只留最后写入那一个组件的值；② 写袋后 `_edit_refresh_motion_point`
  → `UGCMotionPointComponent.apply_motion_point` 会因形状不符抛
  `TypeError: object of type 'float' has no len()`，且**不会**创建运动点组件
  （203912 数量不变、50 个全在未落位 y=-65536），所以运动点也不是这次的载体。
  下一步要用**原生摆点**（编辑器里手摆一条路径）回读真实落库编码，别再猜形状。
  ✅ **2026-09-22 已定位并修好（原生回读一次到位）**：用户在编辑器里原生摆了一条路径后回读，
  权威形状是 **`info = [[点, 点, ...]]` —— 点列表外面还要再包一层 list**，
  每个点仍是 4 字段 `[bt, [x,y,z], [dx,dy,dz], [sx,sy,sz]]`；
  运行时 `comp._point_info` 用的是**不包层**的那一层（`[点, 点, ...]`）。
  工具链此前直写不包层的 `[点, 点]` ⇒ 被 `check_value_type_valid` 判非法换回默认 `[[]]`、
  并在 `_edit_refresh_motion_point` 抛 `TypeError: object of type 'float' has no len()`。
  修法：`fuxiang_edit_ops.py` 的 persist 分支把 `20391119001` 写成 `[[点,...]]`
  （`_set_point_info` 那侧保持不包层）。改后本地跑校验器 `kept=true`、袋内形状与原生同构。
  ⚠️ **2026-09-22 补：共用单元的登记缺陷（已修，工具链通用入口 `edit.reg_change`）**
  —— 这条不止影响运动器：`room.single_unit_mgr.check_unit_single(unit)` 为真时
  `get_single_unit_key(unit)` 只返回**该单元"当前"那一个**组件的 ugc_id，
  于是原生 `ui._add_change_unit(unit.uid)` 登记不到本次真正改动的那个组件，形状修对了也不上送。
  实测两组：6 个运动器共用 unit 1000094（写 169 → 登记到 176）；
  **4 条剧情共用 unit 1000073（改 119 → 原生登记到 137）**，对话/选项同理。
  修法：新增 `fuxiang_edit.reg_change(ui, unit, comp)` —— 先调原生 `_add_change_unit`，
  再把**本组件**的 `_ugc_id` 补进 `ui.ugc_params['change_units']['component']`；
  `op_edit_props` / `op_edit_transform` / `op_motion_preview(persist)` / `story_set` 全部改走它。
  离线回归锁在 `tests/test_reg_change.py`（含 set / list / dict 三种桶形态）。
  ⚠️ 桶的真实类型是 **`set`**（实机 `type()` 读数：`component={119,137,169,174}`、
  `unit={1000073,1000093,1000095}`；原生 `_add_change_unit` 就是 `setdefault(key, set())` + `.add()`）。
  曾有会话断言它是 `dict[component_id]=unit_uid` 并要求改写 —— **不成立**，别再改。
  验证不发包即可做：`ui._get_change_uid()`（只构建不发送）里 component 桶
  **按组件各取一份 params**（实测 119/137/169/174 四条 md5 互不相同），
  所以补登记后"非当前"节点也真能上送；修之前该桶只有"当前"那一个。
  ⇒ 顺带纠正两处我自己的错判：① 曾读截断输出以为"整单元共用一份 payload"；
  ② 曾以为剧情逐条 `story_set` 恰好落在"当前"上所以无碍 —— 实际上只有**最后一次**写入的节点
  登记得上，一次改多条剧情/多个运动器时前面的都会静默不落库。
  批量改同一单元下多个组件的场景，统一按上面补登记最稳妥。
  ✅✅ **2026-09-22 端到端闭环（工具链自己写的路径点确实落库）**：形状 + 登记两处都修好之后，
  用 `motion_preview persist=true` 往运动器 169 写 `20391119001=[[3 个点]]` 与
  `20391102006=[8.0]`，保存 → 退出 → 重进后回读 **3 个点与 8.0 全在**。
  同一单元、同一次保存里的对照：修好之前写的 171/172/173/174 回读全是 `[[]]`（0 个点），
  而用户原生摆的 176 有 6 个点 —— 差异只在「点列表有没有包外层 list」和
  「有没有用 `reg_change` 登记本组件」，两个缺陷各自都足以让路径点丢失。
  ⚠️ 本段此前写的「路径点请当作不持久化处理」是**只修好一半时的错判**，现已推翻并作废。
  判"是不是真重进"别只看 ugc_id 变不变（这一轮 id 就没变）：看 room 上的会话级痕迹
  —— `vars(room)` 里 `_fuxiang_save_obs` / `_fuxiang_edit_history` 是否消失、
  `change_units` 是否只剩载入自带的 `unit:[1000073,1000093]`。
- **附带发现**：运动结束后保存地图，会把对象**当时的渲染位**经位置同步通道存成新落位
  （重进后逻辑位=路径终点附近 [-432,51,159]，不再是起点）——动画即"重新落位"工具。
- ⚠️ 运行时状态（motion runtime/tick 注册）是会话内的；重载后不自动复跑（unit_motions=0），
  需再次调 `motion_preview` 重启（运动器配置已持久化后只传 ugc_id+motion_ugc_ids 即可）。

---

## 十、事件系统（2026-09-18 实证闭环：手动建"你好"事件 + 前后快照 diff + 接口全链）

### 10.1 数据架构

- **事件注册表**：`cm._ugc_params_events_key['event'][事件键] = {'sort_id': n, 'info': [(ugc_id, params下标, info下标), ...]}`。
  `info` = **引用三元组**（哪些组件参数槽位持有该事件键）；`info=None` = 已删除态。
  `cm.get_save_event_info()` 只收集 info 非空的键、按 sort_id 排序 → 保存载荷。
  同一注册表还有其他 child_key（'camp'/'class' = 平民阵营/职业的引用域），机制通用。
  ⚠️ 读写这一项要分清三个接口（2026-09-23 实测）：
  | 接口 | 语义 |
  |---|---|
  | `change_ugc_params_events_key(key, infos)` | **追加**。过滤后的短列表（含 `[]`）不删任何东西；只对 `None`（删除态）有效 |
  | `modify_ugc_params_events_key(key, infos)` | **替换**。要"摘掉某条引用"只能用它 |
  | `change_ugc_params_events_key(key, None)` | 删除整个事件 |
  统一入口：`fuxiang_edit_ops._set_event_refs()`（优先 `modify_`，无则退回 `change_`，并去重保序）。
- **事件键** = 用户输入的名字字符串（如 "你好"，UI 经 TID 校验）。
- **绑定** = 把事件键字符串写进组件参数槽：`params[index]['info'][params_idx] = 事件键`。
  槽位空值形态 `info=None`；绑定后如 `['你好', 0.0, 0.0, 0.0]`（接收类字段带 3 个数值附加位）。
- **已知事件类字段（实测）**：`10000103004` = GEOMETRY 事件接收字段（info[0]=键）；
  `20200303001` = 道具箱(202003) 事件发送字段（info[0]=键，info[1]=动作参数 1286）。
  发送/接收对应属性面板控件 `UGCPropertyItemLogicSendEvent / LogicSelectEvent`。
- **创建** = 命令层 `UGCEventAddCommand({'owner': ui, 'type': True, 'is_shared': True,
  'type_info': {'event_key': 名字, 'child_key': 'event'}})` →
  `UGCGroupCommandReverse({...})` → `ui._do_command`（进撤销栈，与 UI 同链路）。
  ⚠️ 这两个命令类都是**单 dict 位置参数**，不是 kwargs。

#### ⭐ 参数表结构不变量（2026-09-23 实机定位，改事件代码前必读）

```
unit params[0..2]  = 变换三元组（位置 / 旋转 / 缩放；逻辑组件填 -999 占位）
unit params[3:]    = **该组件自己的**事件袋（= component_info['events'] 的那批 dict）
```

⇒ 引用三元组 `(ugc_id, index, params_idx)` 里的 `index` 是**单元级**下标，
**组件本地下标 = `index - 3`**（常量 `PARAMS_HEADER`）。

⛔ **绝不能用 `ui._get_ugc_info(unit)['params']` 去解析或写事件槽位**：
一个 unit 可以挂**多个同类组件**（实测 4 个 CHOICE 全挂 `unit 1000306`、
2 个 STORY 挂 `unit 1000304`、运动器 6 个共用一个 unit），此时它返回的是
**主组件**的那一份袋 ——

| 用途 | 错误后果 |
|---|---|
| 读（`event_list` 的 `property_id` / `current_value`） | 读到**兄弟组件**的值。实测「选项1」(ugc 351) 的槽位被报成 `['拆开信件']`（那是 357 的） |
| 写（`event_bind` / `event_unbind`） | 把事件键写到**兄弟组件**身上。实测会把 357 的「拆开信件」覆盖成「选项1」 |

**正确写法**：定位到目标组件 → 取 `comp.component_info['events']` →
按 `own_index = index - 3` 读写 → `comp.update_component_info(own)` →
`edit.reg_change(ui, unit, comp)`（共用单元时必须它，只调原生 `_add_change_unit`
会登记到"当前"那个组件上）。

**自检**：`event_list` 的每条引用都带 `own_index` / `property_id` / `key_ok` /
`resolved_from`，顶层 `bad_refs` 汇总 `key_ok=false` 的陈旧/错位引用。

⚠️ **`key_ok=true` 只说明「这个槽里放着这个键」，不能说明「放对了字段」** ——
slot 顺序是节点相关的，用不可移植的 `index` 定位时会**落到别的字段**上，而那个字段的槽
里**恰好**可能是同名的键 ⇒ 自检全绿、实际写错。回对必须**逐条比 `property_id`**
（见 `snapshot_restore.verify_event_refs`，2026-09-23 就是靠它把「看不出错」的 3 处
失败点名出来的）。

### 10.2 接口 op（均已实机验证）

| op | 参数 | 说明 |
|---|---|---|
| `event_list` | — | 全部事件 + 引用解析（`own_index`/字段/当前值/`key_ok`）+ `bad_refs` + 保存口径 |
| `event_create` | `name` | 命令层新建（进撤销栈） |
| `event_delete` | `event_key` | 清空所有引用槽位 + 注册表 info=None |
| `event_bind` | `ugc_id, event_key, property_id`（或 `index` / `own_index`）| 写**目标组件自身**的参数槽（缺失槽自动按事件字段形态创建）+ 注册表登记 |
| `event_unbind` | `ugc_id, event_key` | 槽位恢复 None + 注册表**替换**为该引用的补集 |

⚠️ `event_bind` 的 `info` 是**整段无损写回**：给了就用原样写。所以调用方必须给**正确**的值
—— 实测教训：快照里曾存着被单元镜像污染的 `current_value`，照抄回写会写坏槽位。
取值的权威来源是组件自身事件袋（`own_index` 处那条），见 `snapshot_restore.own_slot_info`。

⚠️ **槽位定位优先级：`property_id` > `own_index` > `index`**（返回值带 `located_by`）。
`index` 是**单元级**下标，而它对应的槽位顺序是**节点相关**的（不同组件/不同节点模板槽
排布不同）⇒ 跨节点、跨图**不可移植**。2026-09-23 跨图还原就栽在这里：同一个 `index`
在源节点指 `设为可用(1000700340009)`，到目标节点却指 `enable(1000700340008)`，而槽里
恰好放着**同名**的键 ⇒ `key_ok=true`，看起来完全正常。**还原侧只下发 `property_id`。**

### 10.3 踩过的坑（都已固化进 op）

1. `update_component_info` 的切片是 **`params[3:]`**（跳过 pos/rot/scale 变换三件套）——
   传反成 `params[:3]` 会把组件其余参数**全部重置**（116 曾因此丢失颜色/材质/绑定，已按记录值修复）。
2. ⛔ **`change_ugc_params_events_key` 是「追加」语义，不是替换** —— 2026-09-23 实测：
   给它一个**过滤后的短列表**（哪怕传 `[]`）**什么都不删**，只对「删除态 info=None」有效。
   所以「解绑 = 传 `[]` 清引用」是**错的**，它一直是**静默空操作**（幽灵引用就是这么留下来的）。
   真正替换引用的接口是 **`modify_ugc_params_events_key(child, key, infos)`**（实测
   `[[358,3,0],[358,6,0]]` → 写 `[[358,6,0]]`，回读即 `[[358,6,0]]`）。
   `fuxiang_edit_ops._set_event_refs()` 已统一封装（优先 `modify_`，无则退回 `change_`），
   `event_bind` / `event_unbind` / `fuxiang_story._register_event_refs` 全部改走它，并做去重保序。
   `change_ugc_params_events_key(key, None)` 仍然 = **删除事件**（这条没变）。
3. `ui._set_event_key` 内部 `GK.unit_mgr.get_unit(uid)` 用 **unit uid**，直接传 ugc_id 会静默跳过
   —— op 内改为 ugc_id 自行解析 unit 后直写槽位。
4. 运动器 203911 类全局组件不在编辑器登记集合，`event_bind` 前置的登记校验会拒绝（设计如此）。
5. **`find_target` 的登记集合护栏有出口 `allow_unregistered=true`**：`ui.all_ugc_uid_set`
   只收「编辑器摆放出来的」物件，**骨架自带**的全局单例（天空盒/雾/LUT/胜负条件…）不在里面。
   默认拦（防误写历史遗留物），但 `set_component_info` 这类**就地覆写**调用方（快照还原的
   `--sync-global`）必须显式放行 —— 否则 6 处单例同步全失败。这类对象仍经 `unit_mgr` 校验
   为**存活**，写走 `update_component_info + edit.reg_change` 会把它补进 `change_units`，安全。

### 10.4 实机状态（待重进复核持久化）

- 事件 `你好`（sort 0）：114 接收 + 118(道具箱) 发送 —— 用户手动创建，已随保存上送。
- 事件 `接口测试事件`（sort 1）：已绑定到 116（几何体事件接收字段），`event-final-02` 保存已排空脏缓冲。
- 保存口径 `get_save_event_info() = ['你好','接口测试事件']`。

### 10.5 常用事件组件与字段（2026-09-18 实机接线验证；同日 P3-1 全量解码修正语义名）

**事件字段就在静态属性表 `DM.ugc_item_property` 里**（早前记为"组件初始化时物化、不在静态表"是
错的——那时只查了 `80001` 之外的段）。判据与形态见 §10.10。字段值形态 = `info` 列表，
**事件键的位置由字段自己的权威形态决定，不一定是 0**。

| 角色 | 组件 | 字段 property_id | 语义（类方法） | info 形态（实测） |
|---|---|---|---|---|
| 发送 | 203003 REGION | `20300311106` | 进入区域时 `_enter_region` | `[键, 事件接收者]`（1286=全部组件）|
| 发送 | 203003 REGION | `20300311107` | 离开区域时 `_leave_region` | 同上；**需 `20300311113` 离开生效次数≠0** |
| 接收 | 203003 REGION | `20300311108~11110/11114~11116` | 销毁 / 可进入 / 不可进 / 可见 / 不可见 / 恢复 | `[键]` |
| 接收 | 203003 REGION | `20300311117` / `20300311119` | 传送到坐标 `_teleport` / 旋转到方向 `_change_direction` | `[键, x, y, z]` |
| 接收 | 203004 MIST | `20300411205` | **使文字出现** `_appear`（旧文档写作"显示"）| `[键]` |
| 接收 | 203004 MIST | `20300411206` | 使文字消失 `_disappear` | `[键]` |
| 接收 | 203004 MIST | `20300411201` | 显示文字内容 `_init_content` | `['测试成功']` |
| 发送 | 202003 道具箱 | `20200303001` | **完成开箱时** `_finish`（旧文档写作"交互发送"）| `[键, 事件接收者]` |
| 接收 | 202003 道具箱 | `20200303002` | 使箱子可以被打开 `_set_available` | `[键]` |
| 发送 | 202003 道具箱 | `20200303005` | 开启该箱子后如果获取手持物 | `[手持物id列表, 键, 事件接收者]` ← **键在第 2 位** |
| 接收 | 100000/100001/202041 | `x03002`/`x03003` | 使自己变为可见 / 不可见 `_become_visible/_become_invisible` | `[键]` |
| 接收 | 100000/100001/202041 | `x03004`/`x03005` | 传送到指定坐标 / 旋转到指定方向 | `[键, x, y, z]` |
| 发送 | 202041 人数检测 | `20204111101` | 玩家角色数量达标：**区域内人数 ≥ N 且持续 T 秒时发一次** | `[玩家角色数量, 持续时间, 键]` ← **键在第 3 位** |
| 发送 | 203025 消息事件 | `20302516908` | 检测符合要求 `_match_trigger` | `[键]` |
| 接收 | 203025 消息事件 | `20302516912`/`16913` | 使检测功能生效 / 失效 | `[键]` |
| 接收 | 202014 盖板 | `20201403002`/`03003` | 使盖板变成可见 / 不可见 | `[键]` |

> 100000 障碍物 / 100001 几何体 / 202041 人数检测 共用同一套「显隐·传送·旋转」接收字段，
> 但 **event_type 编号各不相同**（如 `_become_invisible`：障碍物 5、几何体 9、人数检测 3），
> 所以 property_id 必须按组件分别登记，不能只记一个。

**字段默认值构造（权威形态来源）**：
```python
one = comp._get_property_by_id(property_id)
dv, _filters = ugc_utils.get_default_value_info(
    one, DM.ugc_dropdown_index, DM.ugc_role_filter_table, DM.ugc_tool_filter_table)
# dv 各段**顺序拼接**就是 info：
#   20300311106 → [[''], [1286]]      → ['', 1286]
#   10000103004 → [[''], [0.0]*3]     → ['', 0.0, 0.0, 0.0]
#   20204111101 → [[4], [10.0], ['']] → [4, 10.0, '']        ← 键在 2 位
# 已封装为 bridge/fuxiang_edit_ops.py::event_field_shape(comp, pid)
#   → {label, direction: sender|receiver, key_index, default_info, event_type, method}
```

**标签/枚举文案解析技巧**（`ugc_dropdown_index` 惰性表读取不稳，直接拼 TID 最可靠）：
```python
DM.all_tid_data.get("TID_BATTLE_UGC_PROSETTING_TEXT_%d" % id)     # 字段/参数标签
DM.all_tid_data.get("TID_BATTLE_UGC_PROSETTING_OPTION_%d" % id)   # 枚举选项
# 关键：100001=发送事件 100002=接收事件 100199=进入区域时 100200=离开区域时
#       100455=事件接收者 1286=全部组件 100145=使自己传送到指定坐标 100348=使自己旋转到指定方向
#       100014=可进入 100262=进入区域生效次数 100263=离开区域生效次数 100482=检测符合要求
```

**「进入区域→显示消息」完整配方（已实机落地）**：
1. `place` 区域 203003（**必须 `mode="server"`**，见 §10.7）→ `edit_transform` 放大到可走入
   （`ugc_size = 目标 ÷ 模型原生尺寸`，本模型 6.54×10.44×3.32）；`edit_props {"20300311104":[1]}` 设为可进入
2. `event_create("进入提示")`
3. `event_bind(区域ugc_id, "进入提示", property_id=20300311106, extra=[1286])`
4. place 信息提示 203004 → `edit_props {"20300411201": ["测试成功"]}` +
   `event_bind(提示ugc_id, "进入提示", property_id=20300411205, extra=[])`
5. 保存；试玩模式走入区域即显示。
   ⚠️ 事件只在**游戏/试玩**中触发，编辑器内不运行。
   ⚠️ 一个事件可挂任意多个接收方（扇出）；实测 进入提示 同时驱动
   「信息提示·使文字出现」+「障碍物·使自己变为不可见」两个接收端。

### 10.6 ⚠️ 排障：编辑器「点什么都没反应」（2026-09-18 实机定位并修复）

**症状**：编辑器内点击/拖拽放置全无反应，但游戏仍在运行、桥接心跳正常、帧率仍报 60。

**定位**：
```
log.txt 反复出现 →
  ui\UIBattleUGCPart\UIBattleUGCEdit.py  _get_select_pos  line 1055
  AttributeError: 'NoneType' object has no attribute 'scene'
```
`_get_select_pos` 第一行 `room = GK.g_unit.room`（1054），**`GK.g_unit.room` 为 None** →
`.scene` 抛异常 → 被 `UIBase.do_check_btn_clicked_too_fast` 包装器吞掉 → 表现为"按键无响应"。

**根因**：`GK.g_unit` 是编辑模式观察角色（`UgcEditCivilianUnit`，uid=0），出现**失联实例**
（`.room is None`）。常见于客户端单位重建/地图重载（重进、保存后 resync）后全局引用未刷新。

**修复**（一行，立即生效、无需重启）：
```python
GK.g_unit.room = core.room      # 把 room 引用写回失联的观察单位
# 验证：ui._get_select_pos(math3d.vector(x, y, 0)) 不再抛异常
```

**排查手法**：不要只看"卡死"表象——桥接心跳/帧率正常时，优先 **grep log.txt 的
TracebackManager 调用栈**，把异常函数定位到具体行，再用 eval 复现该函数调用拿到真实异常。

### 10.7 ⚠️ 服务端摆放支持范围修正 + 测试物丢失教训（2026-09-18 实机）

**教训**：本轮用本地命令层（`scene.command_create` = `ui._normal_create_units`）创建的 3 个测试物
（消息事件/区域/信息提示）在**重进后全部消失**，连带事件引用被清空。
⇒ **只有"服务端拥有"的物件才持久化**；本地命令层创建只进本地变更集，服务器不认。

**修正**：`bridge/fuxiang_scene.py` 的 `SERVER_COMPONENT_TYPES` 原先只有 `{100000, 202014}`，
本次实测**服务端同样受理 203003 REGION 与 203004 MIST**：
```bash
python cli/fxedit.py place_item '{"item_id":203003,"position":[-430,51,200],"mode":"server","request_id":"x"}'
python cli/fxedit.py place_poll '{"request_id":"x"}'      # → status=placed，一次即成，位置正确
```
⇒ 服务端**不是**按 component_type 拒绝；旧白名单只是客户端保守策略。
需要摆放更多类型时，先按此法实测，通过即加入 `SERVER_COMPONENT_TYPES`。

**另两个坑（区域/信息提示专属）**：
1. `20300311112`（进入生效次数）**默认 0 = 永不触发**；必须设置（如 `[[999.0]]`），
   否则区域进人也不发事件。
2. `_init_content` / `_init_enter_effect_times` 等 `_init_*` 字段**不在** `apply_components_client`
   的分发白名单里：写袋后运行时属性仍是旧值，需显式调用原生方法落一次
   （`comp._init_enter_effect_times(999.0)` / `comp._init_content("测试成功")`），
   或依赖组件自身的初始化路径（重载后生效）。
3. 逻辑组件的 `unit.position` 是未落位哨兵 `[0,-65536,0]`；**真实位置读 `unit.get_position3()`**。

### 10.8 事件文件化 P1（导出/导入往返）— 2026-09-18 落地

**工具**：`cli/event_export.py`（导出）+ `cli/event_import.py`（导入/校验）。

文件结构（`projects/event_dump/map_events.json`，schema_version=1）：
```jsonc
{"meta": {...},                     // 版本/时间/事件数
 "objects": {"115": {label, item_id, component_type, position, rotation, ugc_id_at_export}},
 "events":  [{"key":"进入提示","sort_id":2,"bindings":[
     {"role":"sender","semantic":"区域.进入区域时","object":"REGION_120",
      "property_id":"20300311106","raw_value":["进入提示",1286],  // 无损，导入按此写回
      "value":{"事件键":"进入提示","接收范围":"全部组件(1286)"}}]}]}   // 人类可读
```
- **锚点 = (item_id, x, z) 容差匹配**（ugc_id 不可当身份，理由与实测见 §10.11 末条；y 仅告警）。
- 导入默认 **dry-run**（只出计划），`--apply` 才写；`--prune` 才删除文件里没有的绑定/事件。
- 写回映射：新绑定 → `event_bind(extra=raw[1:])`；值变更 → `edit_props` 写整段 `raw_value`。

**往返实测（本轮全部通过）**：

| 测试 | 文件改动 | 结果 |
|---|---|---|
| 基线 | 无 | dry-run 零变更（幂等）；linter 正确提示"接口测试事件无发送方" |
| A 新增绑定 | 给『你好』加一条（砖方块接收） | apply 后 你好 refs 2→3 ✅ |
| B 改参数 | 区域接收范围 1286→1287 | dry-run 报"更新参数"，apply 后实时值生效 ✅ |
| C 删除+清理 | 文件移除『接口测试事件』+ `--prune` | 事件删除、引用清空、**不误伤同槽位的新绑定** ✅ |

**往返测试发现并修复的两个真实 bug**（都属"会静默损坏数据"级）：
1. **`op_event_delete` 无条件清空引用槽位** → 当槽位已被**另一个**事件接管（陈旧引用）时，
   会把别的事件的绑定删掉（实测：删『接口测试事件』把砖方块上的『你好』绑定清成 `[None,0,0,0]`）。
   修复：只清"仍持有本事件键"的槽位，否则仅摘注册表引用。
2. **`op_event_unbind` 的陈旧引用**：现已在 `removed` 里显式标注 `{"stale_ref_removed": ref}`。

**linter 已有检查**：找不到锚点 / 未知字段 / 无发送方 / 无接收方 / 事件键重复 /
**陈旧引用（注册表指向的槽位已改持别的键）** / **文件内多事件争用同一槽位**。

**尚未做（P2 起）**：模板/实例化（gadget 复用）、语义 DSL（中文动作名）、对象标签持久化、
导入后自动 `save_map`、批量相对坐标重连。

### 10.9 事件文件化 P2：标签 / 语义 DSL / 机关模板（2026-09-18 全部实机验证）

**① 对象标签（稳定身份）** — `cli/event_labels.py` + `projects/event_dump/labels.json`
- 组件**没有**可用的游戏内文本字段（GEOMETRY/REGION/MIST/道具箱均无 name 类属性），故标签存**旁挂文件**：
  `{"标签名": {item_id, component_type, position, matched_ugc_id, auto?, note}}`。
- 匹配规则：同 `item_id` + `(x,z)` 容差内最近者（y 仅告警——逻辑组件 `position` 是哨兵，真实位在 `get_position3()`）。
- 命令：`dump`（生成/刷新，自动名标 `auto:true`）/ `check`（校验：OK/移动/缺失/歧义）/ `set` / `rename` / `remove`。
- 导出/导入**优先用用户命名的标签**（`auto` 标记区分），坐标仅作后备 → 移动对象后仍可解析。

**② 语义 DSL（人可读层）** — `cli/event_dsl.py`（需 PyYAML：装在本机 venv
`~/.workbuddy/binaries/python/envs/default`）
```yaml
事件:
- 事件: 进入提示
  当: {对象: 进入区域, 时机: 进入区域时, 接收范围: 全部组件}
  则: [{对象: 成功提示, 动作: 显示信息, 事件键: 进入提示}]
对象:
  成功提示: {类型: 信息提示, 位置: [...], 属性: {'20300411201': [测试成功]}, 属性说明: {'20300411201': 显示内容}}
```
- `to-dsl`（机器→DSL）/ `from-dsl`（DSL→机器，再用 `event_import.py` 导入地图）。
- 名表：时机（进入区域时/离开区域时/交互发送）、动作（显示信息/接收事件/检测触发）、枚举（全部组件…）、组件中文名；
  可用 `registry/semantic_names.json` 覆盖扩展。
- 实测：改 DSL 里 `显示内容` → `from-dsl` → `event_import --apply` → 地图内属性真的变了，且**可逆**。

**③ 机关模板** — `cli/gadget.py`（`projects/gadgets/<名>.json`）
```bash
python cli/gadget.py save 进入提示机关 --labels 进入区域,成功提示
python cli/gadget.py instantiate 进入提示机关 --at=-700,1.543,300 --apply
```
- 模板 = 对象（item_id/相对偏移/旋转/缩放/暴露属性）+ 事件（按标签引用绑定）。
- `instantiate` 默认 dry-run；`--apply` 逐对象走**服务端摆放**（`place_item mode=server` + `place_poll`），
  再 `edit_props` 写属性、**`RUNTIME_REFRESH` 显式调 `_init_*` 原生方法**（分发表不覆盖这些字段）、
  建事件（`_副本` 后缀）并按标签重连绑定、最后把新对象写进 `labels.json`。
- **幂等**：目标位置已有同 item_id 对象时**复用**而不重复摆放；事件已存在则跳过创建只补绑定。
- 实测：在 `[-700,1.5,300]` 复制出第二套「进入区域+成功提示」机关，运行时属性（进入次数 999/可进入/提示文本）全部就位，
  事件 `进入提示_副本` 绑定到 122 区域 + 123 提示，重复执行零副作用。

**本轮修掉的设计缺陷**（都是"会覆盖/污染用户改动"级）：
1. 导入时**未变化的属性也被重写** → 已改为写前比对（`_same_value`），只写真正变化项（实测 12 项里只写 1 项）。
2. `_init_*` 类字段写袋后**运行时不同步** → 模板/导入流程显式调用原生方法。
3. 已删除事件仍被导出 → 导出跳过 `in_save=False 且无引用` 的事件。
4. 自动标签压过用户命名 → `auto` 标记 + 用户命名优先。

### 10.10 事件字段语法（P3-1 全量解码，2026-09-18）

**一条规则解释全部 383 个事件字段**，不用再逐组件反汇编：

1. `property_type` 用 `;` 分段，`property_params` 用 `{}` 分段，**两段一一对齐**
   （全库 383 个 8000x 字段实测 **0 例外**；证据 `probe/p3_decode_family.out.json`）。
2. `info` 的形态 = `ugc_utils.get_default_value_info(定义)[0]` 的**各段顺序拼接**
   （源码 `default_value += default_value_info`，反汇编 `UGCBaseComponent.apply_components_client:1332`）。
3. **`80001` 段 = 事件键槽**，该段第二元素 **`1`=发送 / `2`=接收**；
   段的序号（展平后）= 事件键在 `info` 里的下标。
   ⚠️ 同族的 `80002`=阵营、`80003`=职业、`80004`=天赋页、`80005`=快捷消息、`80006`=随机事件权重，
   **都不是事件键槽**——只按 `8000x` 前缀筛会把阵营字段当事件字段。
4. 其它控制码：`50001`/`40001` 枚举 `{标签,值域,默认}`、`20001` 数字 `{标签,默认,最小,最大,单位}`、
   `20004` 滑块、`30001` 开关、`70001` **三元数值**（坐标/欧拉角，`{初值,说明TID}`→`[0.0,0.0,0.0]`）、
   `10002` 文本 `{标签,占位TID,最小,最大}`、`120001` 材质、`120002` 颜色(4 槽)、
   `200001` 运动器、`60002` 手持物、`110005/6` 分隔标题、`110012` 坐标偏移。

**规模**：98 个组件类型 / 1469 个属性定义中，**383 个字段含 8000x 段、覆盖 66 个组件类型**；
其中 `80001` 事件键字段 349 个（126 发送 / 228 接收）。
证据与可复查产物：`probe/p3_scan_event_fields.out.json`（判据校准）、
`probe/p3_decode_family.out.json`（383 字段全表）、`probe/p3_decode_comp.txt`（6 个组件逐字段中文表）。

**运行时应用白名单（决定要不要 `RUNTIME_REFRESH`）**：`apply_components_client` 只把
**11 个方法名**桥接到 `_client_*` 变体：
`_set_color / _set_material / _set_init_visible / _set_alpha / _set_collision /
_set_ui_pos / _set_ui_rot / _set_ui_height / _set_ui_width / _set_lod / _set_coordinate_offset`。
其余（全部 `_init_*`，以及 `_appear/_enter_region/_become_invisible` 等逻辑字段）
**在编辑会话内不会被应用**——写袋只改持久化数据，当前会话运行时仍是旧值。
⇒ 这也回答了旧「未解之谜」：`_init_content` / `_init_enter_effect_times` 重载后**不需要**
会话内补调用，保存+重进后引擎按属性袋自行生效；`RUNTIME_REFRESH` 只是"本会话立刻可见"的手段。

**接口层落点**：
- 游戏侧 `bridge/fuxiang_edit_ops.py::event_field_shape(comp, pid)` 直接按上面规则算
  `label/direction/key_index/default_info/event_type/method`；`op_event_bind` 已改用它
  （新增 `info` 整段无损入参，并在结果里回显 `field_label/direction/field_shape`）。
- 主机侧 `cli/event_export.py::FIELD_SEMANTICS`（33 个字段）、`EXPOSED_PROPS`（6 个组件）、
  `cli/event_dsl.py::FIELD_ROLE` + `FIELD_KEY_INDEX`、`cli/gadget.py::RUNTIME_REFRESH`。
- 离线回归：`tests/test_event_files.py`（8 项，锁住"键不在 0 位"与 DSL 往返无损）。


### 10.11 P3-1 新组件接线实录（2026-09-18，接口绑定→保存→重进→试玩触发 **已实机验收**）

两条新链路，全部走接口、零手工：

**A. 障碍物接收端（新组件 100000）**
```bash
python cli/fxedit.py place_item '{"item_id":3014,"position":[-430,2,215],"mode":"server","request_id":"p3a-box1"}'
python cli/fxedit.py place_poll '{"request_id":"p3a-box1"}'        # → placed, ugc_id=124
python cli/fxedit.py event_bind '{"ugc_id":124,"event_key":"进入提示","property_id":10000003003}'
# → field_label=使自己变为不可见 direction=receiver field_shape=[''] slot_value=['进入提示']
```
复用既有事件「进入提示」（区域 120 → 信息提示 121），形成**一发两收**：走入区域应同时
「弹出提示」+「箱子消失」。截图证据 `tests/mat_test/p3_A_box.png`（箱子在区域体积内）。

**B. 人数检测发送端（新组件 202041，事件键在 info[2]）**
```bash
python cli/fxedit.py place_item '{"item_id":202041,"position":[-950,1.543,400],"mode":"server","request_id":"p3b-det1"}'   # → ugc_id=125
python cli/fxedit.py edit_transform '{"ugc_id":125,"scale":[5.5046,1.1494,10.8434]}'    # 放大到可走入（与区域 120 同型号 box01）
python cli/fxedit.py place_item '{"item_id":203004,"position":[-950,53,405],"mode":"server","request_id":"p3b-mist1"}'      # → ugc_id=126
python cli/fxedit.py event_create '{"name":"人数达到"}'
python cli/fxedit.py event_bind  '{"ugc_id":125,"event_key":"人数达到","property_id":20204111101}'
# → params_idx=2（自动），slot_value=[4, 10.0, '人数达到']
python cli/fxedit.py edit_props  '{"ugc_id":125,"props":{"20204111101":[1,1.0,"人数达到"]}}'   # 1 人 / 持续 1 秒
python cli/fxedit.py edit_props  '{"ugc_id":126,"props":{"20300411201":["人数达标"]}}'
python cli/fxedit.py event_bind  '{"ugc_id":126,"event_key":"人数达到","property_id":20300411205}'
```
截图证据 `tests/mat_test/p3_B_detect.png`（半透明检测体积）。

**服务端摆放白名单再扩**：`fuxiang_scene.SERVER_COMPONENT_TYPES` 由
`{100000,202014,203003,203004}` 加到含 **202041**（实测 `_add_unit_server` 受理、`place_poll` 一次 `placed`）。
⇒ 每加一个类型前都要像这样单次实测，不要凭白名单推断。

**文件层往返**：`event_export → to-dsl → from-dsl → event_import` 对含新字段的 4 事件 10 绑定
**dry-run 零变更**（新字段全部中文解码，无 `未解码字段`）。产物
`projects/event_dump/{map_events.json,events.yaml,p3_roundtrip.json}`。

**本轮顺带修掉的两个真 bug**：
1. `cli/event_import.py::resolve_ref` 走坐标回退分支时按 2 个值解包 `match_anchor`（实际返回 3 个）
   → **绑定对象没有用户标签就抛 `ValueError: too many values to unpack`**（崩溃，非静默）。
2. `cli/event_dsl.py` 的 `to-dsl` 只输出 `params[1]`，`from-dsl` 对接收端一律写 `[键,0,0,0]`
   → **传送/旋转类字段的 x/y/z 在 DSL 往返中被丢掉**；现按参数名逐位输出/回填，
   并新增 `FIELD_KEY_INDEX` 保证事件键写回正确下标。

**✅ 用户重进 + 试玩核实（2026-09-18 23:05）—— 两条链都真触发，持久化闭环成立**
- A：走进区域 → 弹提示 **且** 黑箱子消失（一发两收，新组件 100000 的 `10000003003` 生效）。
- B：走进人数检测体积 → 弹「人数达标」，**且只触发一次**。
- 重进后读回：4 事件 9 引用全在，`20204111101` 仍是 `[1, 1.0, '人数达到']`、`10000003003` 仍是 `['进入提示']`；
  22 个标签 0 异常（位置锚点扛住重载）。
- **由"只触发一次"定死的语义**：`20204111101` 第 2 参数（`100737 时间要求`）= **需持续满足的秒数**，
  触发是**边沿触发**（进入达标状态那一刻发一次），不是每 N 秒重复发。
- **人数检测开箱即用**：125 的属性袋里除 `20204111101` 外全为 `null`（走默认值）就触发了
  ⇒ 它**不需要**区域那种 `20300311112 进入生效次数` 的前置开关（区域不设=永不触发，两者行为不同，别类推）。
- ⚠️ 修正一条旧断言：**"ugc_id 每次重载都变"不成立**——本轮重进后 124/125/126 与重载前完全相同
  （编号由保存顺序决定，地图内容不变时恰好稳定）。仍然**绝不能拿 ugc_id 当身份**（锚点用 item_id+坐标），
  但也不要再写"必然重新编号"这种过度强的说法。

---

## 十一、新增玩家角色（求生者 / 监管者）——位置·属性·事件持久化（2026-09-21 闭环）

在 `樱花残羽艾玛的地图` 内新增**两个玩家角色**（求生者 `101001` CIVILIAN /
监管者 `102001` BUTCHER），设置彼此**不同**的属性与事件，并通过「保存 → 退出重进」验证。
**最终结论（多次重进实证）：用「创建即带坐标」的角色专用创建法（`ui._add_unit_server([item_id], pos=(x,y,z))`）
创建的角色的 位置 + 属性 + 事件 全部持久化成立 **✅**；对已存在角色后改位置（set_position /
set_ugc_position + _add_change_unit + 保存）无法落库，会被服务端拉回哨兵位。**

### 11.1 角色专用创建方法（核心）——创建瞬间带落位坐标

反汇编编辑器 UI 确认：`UIBattleUGCEdit._add_unit_server` 有一个**可选 `pos` 参数**，它把落位坐标
一并发给服务端（`send_battle_event(UGC, {uid, key:'add_unit', item_id, pos})`）：

```python
# ✗ 错误：不带 pos —— 服务端创建时无落位坐标 → 角色落在哨兵位 [0,-65536,0]
ui._add_unit_server([101001])

# ✓ 正确：带 pos —— 创建瞬间携带落位坐标，重进后位置持久化
ui._add_unit_server([101001], pos=(626.0, 14.6, -140.0))
```

**为什么这是唯一可靠的 API 落位方式**：角色实体（`MyCivilianUnit`/`MyButcherUnit`）不在普通
`place_item` 白名单（`UNSUPPORTED_TYPES` 拦截）；而创建后再 `set_position` 改位置，服务端不认
这种已存在角色的"漂移"，保存重进后仍按哨兵位拉回。只有**创建那一步**带上坐标，服务端才会把它
作为角色的出生/落位点持久化。

### 11.2 角色专属落位流程（实测可用）

```python
# 1) send —— 带 pos 创建
ui._add_unit_server([102001], pos=(600.0, 14.6, -104.0))

# 2) detect —— 轮询 cm._ugc_id2component 找新 uid（ugc_type 匹配且 uid 不在 before）

# 3) confirm —— set 目标坐标 + 原生确认 + 取消隐藏 + 登记
u.set_position(v); u.set_ugc_position(v)
ui._normal_create_units([u], v)      # end_pos = 目标坐标 vector
u.hide_collision_pos(False)
ui._add_change_unit(u.uid)

# 4) edit_props 设属性 → event_create/event_bind 设事件 → save_map + poll
```

> ⚠️ 验证中确认的坑：
> - 若 confirm 时把**手爪来的 `get_position3()`**（此刻哨兵位）当 end_pos，位置会以哨兵位保存，
>   **属性/事件仍可落库，但位置重进后停在 y=-65536 未落位**（→ 后续 set_position 也无法挽救）。
> - 角色 Y 轴语义：编辑器地图对角色**没有可用 nav 高度场**（`room.get_pos3_height` 全 null），
>   角色 Y 由系统落位决定。手动拖角色时 X/Z 可动、Y 恒显示 `-10000`/中间负值 = 仍处于"未落位
>   半隐藏"（`HIDE_COL_POS=(0,-65536,0)`）。所以 API 必须通过"创建带 pos"一次到位。

### 11.3 角色属性可写（edit_props），变换不可改

角色不能用 `fuxiang_edit_transform`（护栏拒绝：「实体类型 MyCivilianUnit 不支持 UGC 变换接口」），
但**属性袋可改**（`fuxiang_edit_props` 走原生三步）。属性 id 语义见 11.5。

### 11.4 实测结果（多次重进后核对）

服务端按保存顺序**重排 ugc_id**，身份用「类型 + 位置 + 属性袋」锚定，勿用 ugc_id：

| 角色 | 重进后 ugc_id | 位置 | 属性（不相同） | 专属事件 |
|---|---|---|---|---|
| 求生者 101001（带 pos 创建） | 3 | ✅ `(626,14.6,-140)` | 恐惧上限`3000`、可治疗`800`、伤害倍率`150%`、手持物无冷却`1`、无能力`0` | 【求生者·被击倒】→`10100103011` |
| 监管者 102001（带 pos 创建） | 5 | ✅ `(600,14.6,-104)` | 初始存在感`120`、1阶`1000`、2阶`2500`、技能无冷却`1`、锁定`0` | 【监管者·攻击命中】→`10200103001` |

- **位置 + 属性 + 事件**：重进后逐项回读一致，注册表事件引用都在，**全部持久化成立** ✅。
- 旧的哨兵幽灵（当初不带 pos 创建、卡在 y=-10000 的两个角色）已用 `delete_objects` 清理，
  不留残留占位。

### 11.5 角色关键属性 id（TID 反查）

**求生者 UGCCivilianComponent(101001)**：

| property_id | 语义 | 值 |
|---|---|---|
| `10100101001` | 所属哪个队伍（阵营） | `["TID_BATTLE_UGC_TEAM_9001"]` |
| `10100101002` | 玩家职业 | `["TID_UGC_CLASSIFY_101"]` |
| `10100102001` | 恐惧值上限 | `[3000]`（默认 1000） |
| `10100102008` | 恐惧值达到多少时可治疗 | `[800]`（默认 500） |
| `10100102020` | 受到伤害倍率(%) | `[150]`（默认 100） |
| `10100102015` | 手持物无冷却 | `[1]` |
| `10100102002` | 「无能力」模式 | `[0]`（关） |
| `10100103011` | **被击倒后（发送事件）** | `[事件键, 1286]` |

**监管者 UGCButcherComponent(102001)**：

| property_id | 语义 | 值 |
|---|---|---|
| `10200101001`/`10200101002` | 阵营 / 职业 | TID 串 |
| `10200102002` | 初始存在感 | `[120]`（默认 0） |
| `10200102016` | 1阶所需存在感 | `[1000]` |
| `10200102017` | 2阶所需存在感 | `[2500]` |
| `10200102006` | 技能无冷却 | `[1]` |
| `10200102001` | 锁定存在感（开关） | `[0]`（关） |
| `10200103001` | **攻击命中求生者（发送事件）** | `[事件键, 1286]` |

### 11.6 操作步骤速查

```text
# 0) 前置：桥已安装、编辑器在线（fuxiang_status 的 server_session.connected=true）

# 1) 角色专用摆放：带 pos 创建（place_item 会被 UNSUPPORTED_TYPES 拦截）
ui._add_unit_server([101001], pos=(626.0, 14.6, -140.0))     # ⚠️ 必须带 pos
detect 新实体 -> set_position/set_ugc_position(目标坐标)
       -> _normal_create_units([u], 目标坐标vector)           # end_pos 用目标坐标
       -> hide_collision_pos(False) + ui._add_change_unit(u.uid)

# 2) 设置属性（edit_props 走原生三步，可写）
edit_props {ugc_id, props: {"10100102001":[3000], "10100102008":[800], ...}}

# 3) 事件：创建 + 绑定
event_create {name:"求生者·被击倒"}
event_bind  {ugc_id, event_key:"求生者·被击倒", property_id:10100103011, extra:[1286]}
# → direction=sender, slot_value=['求生者·被击倒', 1286]

# 4) 保存（单笔在途）
save_map {request_id} -> save_map_poll（回执常 20~40s，勿过早判超时）
save_dirty # 保存后 change_units 排空=已上送；重进做最终裁决
```

**要点与坑**：
1. **角色位置持久化的唯一 API 通道是「创建时带 `pos`」**；已存在角色后改位置不落库。
2. 清理卡在哨兵位的旧角色用 `delete_objects`（走游戏原生删除链路，非组合父件可直接删）。
3. 角色只能 `edit_props`、不能 `edit_transform`；位置走创建带 pos。
4. 重进会重排 ugc_id，身份用「类型+位置+属性袋」锚定，别用 ugc_id。
5. `edit_props` 写角色属性时 eval 命名空间没有 `ui` 变量，取 UI 用
   `import fuxiang_scene as FS; FS.get_edit_ui(FE)`（登记变更时尤其要这样取）。

---

## 十二、逻辑组件（计时器 / 计数器 / 或与逻辑 / 随机事件 / 消息事件）——工具已可直接使用（2026-09-21）

研究编辑器「逻辑」分类下的逻辑组件，确认工具（`place_item` + `edit_props` + `event_*`）
已能完整使用它们：**摆放、设属性、读属性、事件接线**全部走标准原生链路，无需额外桥接。

### 12.1 逻辑组件一览与 component_type

| 中文名 | component_type | class | 分类 | server 摆放 |
|---|---|---|---|---|
| **计时器** | `203005` | UGCClockComponent | 逻辑 | ✅ |
| **计数器** | `203006` | UGCCounterComponent | 逻辑 | ✅ |
| **或与逻辑** | `203012` | UGCLogicComponent | 逻辑 | ✅ |
| **随机事件** | `203021` | UGCRandomEventComponent | 逻辑 | ✅ |
| **消息事件** | `203025` | UGCChatEventComponent | 逻辑 | ⚠️ 见 12.4 |

> 相关「功能」组件同样可用：区域 `203003`、信息提示 `203004`、销毁方块 `203016`、交互按钮 `203022`、
> 禁行区域 `202042`、人数检测 `202041`（后者见 §10.11）等。

### 12.2 摆放（server 模式）

这 5 个类型在 `SERVER_COMPONENT_TYPES` / `VERIFIED_TYPES` 里（`203025` 为本轮新增，见 12.4），
直接走标准 `place_item(mode=server)` + `place_poll` 即可，返回 `placed` + `editor_registered=true`：

```text
place_item {item_id:203005, position:[700,2,700], mode:"server", request_id:"clock-01"}
place_poll {request_id:"clock-01"}    # → status=placed, ugc_id 分配
```

实测：计时器/计数器/或与逻辑/随机事件/消息事件 server 摆放全部一次 `placed`，
`component_count` / `occupancy` 相应增加，写地图后重进即可持久化。

### 12.3 属性与事件字段（已用 §10.10 规则解码）

**计时器 `203005`（UGCClockComponent）**
- 属性：`20300502001` 可看见、`20300511301` 游戏开始时计时、`20300511302` 计时方式(`{100209,1,1031~1032,1031}`)、`20300511303` 目标时间。
- 发送（方向=sender）：`20300511304` **计时达到目标时间**、`11305` 计时达到指定时间、`11310/11311/11312` 暂停/恢复/停止计时、`11315/11316` 如果计时（大于）。
- 接收（方向=receiver）：`20300511306` 使计时启动、`11307` 使计时停止、`11308` 使计时暂停、`11309` 使计时继续、`11313/11314` 接收事件。

**计数器 `203006`（UGCCounterComponent）**
- 属性：`20300611401` 初始数值、`11402` 目标数值、`11413` 目标数值类型、`11414/11415` 目标数值=某队伍/职业人数、`02001` 可看见。
- 发送：`20300611403` **计数达到目标数值**、`11404` 计数达到指定数值、`11410/11411` 如果计数（大于）、`11412` 若计数重置。
- 接收：`20300611405` 使计数重置、`11406` 使计数+1、`11407` 使计数-1、`11408/11409` 接收事件。

**或与逻辑 `203012`（UGCLogicComponent）**
- **接线靠字段 `20301212701` 的多段 info，不走标准 80001 事件槽**（`event_field_shape` 判定 direction=null，无法用 `event_bind`）。
  `default_value_info` 三段顺序拼接的形态为：`[逻辑类型, [接收事件键...], 需全部(0/1), 发送事件键]`。
  实测形态：`[1063, ["计时结束"], 1, "随机分支"]`。
- 段1 逻辑类型：`1063=与`、`1064=或`（TID `100232`）。
- 段2 接收事件模式：`100233`=接收到以下**全部**事件后 / `100234`=接收到以下**任一**事件后，`[接收事件键列表]` 放在该段首位，`100269`=再次发送是否需接收全部事件(0/1)。
- 段3 `{100001,1}`=**发送事件键**（逻辑门成立后触发）。
- 写入方式：`edit_props {ugc_id, props:{"20301212701":[逻辑类型, 接收键列表, 需全部, 发送键]}}`。

**随机事件 `203021`（UGCRandomEventComponent）**
- 属性：`20302116303`/`16307` 事件分支配置（`140003` 携带事件键）、`16305` 最多发送事件一定次数、`16302` 权重。
- 接收：`20302116301` **接收事件**（触发随机分支）。

**消息事件 `203025`（UGCChatEventComponent）**
- 属性：`20302516901` 对哪些玩家显示、`16902/16903` 队伍/职业、`16904` 检测消息类型、`16905/16906` 检测消息内容、`16907` 检测消息要求、`16911` 检测功能初始生效。
- 发送：`20302516908` **检测符合要求**。
- 接收：`20302516912/16913` 使检测功能 生效/失效。

### 12.4 ⚠️ 消息事件 server 摆放白名单（本轮已修）

`bridge/fuxiang_scene.py::SERVER_COMPONENT_TYPES` **原本没有 `203025`**（仅 `VERIFIED_TYPES` 有），
导致 `place_item(mode=server)` 抛「未列入已实测的服务端摆放白名单」。已按「先单次实测再加白」验证
（eval 临时扩展后 `_add_unit_server` 受理、`place_poll` 一次 `placed`），并把 **`203025` 永久加入
`SERVER_COMPONENT_TYPES`**。现在消息事件可直接 server 摆放（白名单 78→79 项）。

### 12.5 或与逻辑 / 随机事件的运行时生效与 RUNTIME_REFRESH

- `20301212701`（逻辑类型）与随机事件分支等 `_init_*`/逻辑字段**不在** `apply_components_client`
  的运行时分发白名单（见 §10.10）——写袋后当前会话运行时可能不立即生效，**保存+重进后引擎按属性袋生效**。
- 随机事件 `20302116303` 这类 `140003` 字段需按事件分支装配；实测写入属性袋 + 绑定接收事件后，注册表引用正确登记。

### 12.6 实测接线示例（工具全程可操作）

```text
# 1) 摆放或被编辑器中已有逻辑组件（如 162=计时器）
place_item {item_id:203005, mode:"server", ...}   # 或直接用编辑器里现有实例

# 2) 设属性（edit_props 原生三步）
edit_props {ugc_id:162, props:{"20300511302":[1031], "20300511303":[60]}}   # 倒计时 60s
edit_props {ugc_id:163, props:{"20300611401":[0], "20300611402":[10]}}      # 计数器 0→10
edit_props {ugc_id:165, props:{"20301212701":[1063]}}                       # 或与逻辑

# 3) 事件接线：计时器「计时达到目标时间」→ 事件 → 信息提示显示
event_create {name:"计时结束"}
event_bind  {ugc_id:162, event_key:"计时结束", property_id:20300511304, extra:[1286]}  # sender
event_bind  {ugc_id:152, event_key:"计时结束", property_id:20300411205, extra:[]}       # receiver(MIST)
# → slot_value=['计时结束',1286] / ['计时结束']

# 4) 保存
save_map {request_id} -> save_map_poll; save_dirty 排空=已上送
```

⚠️ 演示副作用提醒：接线时若复用已有信息提示（如 152），会**覆盖其显示内容**；演示/验证后应在
游戏内改回原值，或改用新建的接收端。

**要点**：
1. 逻辑组件与普通组件一样走 `place_item`/`edit_props`/`event_*`，工具无需追加桥接。
2. 消息事件 `203025` 此前缺 server 白名单，现已在 `SERVER_COMPONENT_TYPES`，可直接 server 摆放。
3. 逻辑字段（或与逻辑类型、随机分支、`_init_*`）写袋后当前会话可能不即时生效，保存重进后生效。
4. 事件绑定仍走 `event_create` + `event_bind`（发送/接收方向已按 §10.10 解码）。
