# 随笔浮想编辑器：组件与物体的全量定位

> 本文只描述**只读定位层**（枚举 / 寻址 / 判据）。摆放、删除、变换等操作逻辑
> 见 `README.md` 与 `HANDOVER_FIX.md`，本文不涉及，也不修改它们。
>
> 生成依据：dwrg.exe 实机（桥接在线，编辑器已进入），全部结论来自游戏进程内的权威表，
> 不依赖静态猜测。逐条证据见 `tests/mcp_interface_result.json` 与 `probe/` 下的探针输出。

---

## 一、结论摘要

| 维度 | 数量 | 说明 |
|---|---|---|
| 物件目录（`ugc_item_data`） | **3963** | 编辑器「全部对象」面板的唯一数据源 |
| 组件类型（四表并集） | **105** | 类型注册表 ∪ 实现类 ∪ 目录 `component_id` ∪ 属性表 |
| 其中可创建（登记表 + 有实现类） | **97** | `creatable=true` 的权威判据 |
| 目录中有编号但无实现类 | **8** | 只能枚举，不能创建，见第四节 |
| 有实现类但目录中无物件 | **1** | `203002 SLOW_REGION`，见第四节 |
| 编辑器分类（二级） | **83** | `ugc_item_tab` 展平，带中文名 |
| 属性取值枚举 | **33** | `common_cs.ugc.UGCEnums` |
| 原生列表隐藏记录（`is_visible`） | **43** | 永远不进「全部对象」 |
| 当前编辑模式下被分类屏蔽 | **2109** | 仅影响列表显示，不影响寻址 |

**可达性结论**：`fuxiang_catalog_verify` 返回 `complete=true` —
**3963/3963 个物件、105/105 个组件类型均可被接口逐个定位**。
「可定位」与「可创建」是两件事，后者由 `creatable` 单独给出（97/105）。

---

## 二、数据来源（全部为游戏权威表）

| 用途 | 来源 |
|---|---|
| 物件目录 | `GK.DM.ugc_item_data`（3963 项，值为 `taggeddict`） |
| 分类树 | `GK.DM.ugc_item_tab`（一级 11 组 → 二级 83 类） |
| 组件类型名 | `UGCComponentManager._component_type2str`（97 项，由 `ComuKeys.UGC_COMPONENT_TYPE` 初始化） |
| 类型 → 实现类 | `sys.modules` 中 `…components.unit_ugc.*` 的 `COMPONENT_TYPE` |
| 属性 schema | `GK.DM.ugc_item_property`（98 项）/ `ugc_control_panel`（4 项） |
| 中文文案 | `GK.DM.all_tid_data`；名称索引取自列表视图的 `search_item_dict` |
| 列表可见性 | `ui.component_view._core`（`UIBattleUGCEditComponentViewCore`）的 `ignore_tab_id` / `ignore_tap_order_sec` |
| 活体组件 | `cm._ugc_id2component`、`cm._component_type2ugc_ids`、`cm._global_components` |

---

## 三、三条已独立复核的关键语义

### 1. `item['is_visible']` 为真 = 不在编辑器列表显示

原生变量名即 `is_hide`，为真直接 `continue`。三处同款判断：

- `UIBattleUGCComponentManagerView.tick_get_all_components`（汇编行 370–372）
- 同类的 `_gen_search_data`（1617–1619）
- 同类的 `_init_content_items_default_state`（882–884）

因此 `README.md` 0.6.1 的更正成立，且 43 条隐藏记录的清单已可枚举
（`fuxiang_catalog_items(visibility="hidden")`）。

### 2. `item['tap_order_sec']` 就是二级分类的 `child_id`

`UGCComponentListViewCore._init_data` 由 `ugc_item_tab` 逐项取 `child_id` 建
`ignore_tap_order_sec`，`tick_get_all_components` 再按物件的 `tap_order_sec` 匹配展示集合。
据此把每个物件挂到编辑器自己的分类树上，不需要猜名字。

### 3. 列表显示有两层过滤，都不影响接口寻址

| 层 | 条件 | 本机结果 |
|---|---|---|
| 物件自身 | `is_visible` 为真 | 43 项 |
| 编辑模式 | 分类命中 `ignore_tap_order_sec`（本机 `show_mode_list=[1]`，`ignore_tab_id=[51,52,53,54,100,200]`） | 另 2109 项 |

**注意**：2109 项在当前模式下不在左栏显示（UI 组件 `item_type=3` 与全局规则类为主），
但它们依然能被 `fuxiang_find` / `fuxiang_catalog_items` 取到，也依然可被摆放接口处理。
此前文档没有记录这一层，容易把「列表里看不到」误判成「接口拿不到」。

---

## 四、三类边界（都可枚举，逐条说明）

### 1. 目录中有编号、但无实现类（8 个）——可枚举，不可创建

`201005 201006 202020 203910 10012001 10012002 10012003 10012099`

- 不在 `_component_type2str`，也没有 `COMPONENT_TYPE` 对应的类 → `cm._create_component` 返回 `None`。
- 其中 `10012001/2/3/99` 在 `ugc_control_panel` 有记录，是**操控面板占位项**，不是场景物件。
- `202020` 另有 `ugc_item_property` 记录，属历史残留。

### 2. 有实现类、但目录里没有物件（1 个）——可按类型寻址，无法按 item_id 摆放

`203002 SLOW_REGION`（类 `UGCSlowRegionComponent`，`creatable=true`，`catalog_item_count=0`）。

它只能通过 `component_type` 定位，编辑器左栏没有对应物件。
**易错点**：item `203002` 的中文名是「水潭」，其组件是 `TARES(100003)`，
与组件类型 `203002` 完全无关——`item_id` 与 `component_type` 是两套命名空间，可同名不同义。

### 3. 目录里没有官方中文文案（7 个）——可枚举，名字为 `null`

`15064 202033 202034 10012001 10012002 10012003 10012099`

`all_tid_data` 查不到对应 TID，游戏自身的搜索索引里键就是 TID 字面量
（如 `TID_UGC_ITEM_15064`）。这不是本接口的缺陷，游戏里同样没有可读名。

### 4. 同名多记录（11 组）

举例：`地下室盖板 → [202014, 202009]`、`灯泡 → [3056, 8085]`、`木桶3 → [8009, 6015]`。
两者可能**共用同一个模型文件却不是同一种组件**——例如：

| item_id | component_type | 表现 |
|---|---|---|
| 202014 | 202014（`BASEMENT`，地下室组件） | 正常进「地下室盖板」列表 |
| 202009 | 100000（`OBSTACLE`，普通障碍物） | `is_visible=1`，列表不显示 |

按名字搜到多条时，必须用 `component_type` 或 `catalog_hidden` 消歧，不能凭模型路径判断。

---

## 五、接口清单（MCP 工具）

工具总数 66（真源 `python cli/consts.py`；2026-09-22 校正，此前写 29 为旧口径），
其中定位相关 9 个：

| 工具 | 用途 |
|---|---|
| `fuxiang_catalog` | 全量注册表概览：类型数 / 可创建数 / 物件数 / 隐藏数 / 摆放支持矩阵 |
| `fuxiang_catalog_items` | **全量枚举物件**（`limit=0` 取回 3963 项），支持分类 / 组件类型 / 大类 / 可见性 / 关键字过滤 |
| `fuxiang_catalog_verify` | **全量可达性自检**，遍历全部物件与类型并列出全部边界项 |
| `fuxiang_find` | 统一定位：`item_id` / `component_type` / 名称关键字 / 枚举名 |
| `fuxiang_component_detail` | 单个组件类型档案（实现类、目录物件、`creatable`、摆放支持） |
| `fuxiang_component_schema` | 组件属性 schema（属性面板数据源） |
| `fuxiang_categories` | 编辑器分类树与每类物件数 |
| `fuxiang_enums` | 属性取值域（33 个枚举） |
| `fuxiang_catalog_export` | 导出注册表快照到 `registry/`，便于离线比对 |

对应的游戏侧 op：`catalog_summary` / `catalog_items` / `catalog_verify` / `catalog_find` /
`catalog_type` / `catalog_schema` / `catalog_categories` / `catalog_enums` / `catalog_export`。

### 推荐调用顺序

```
fuxiang_catalog                     # 先看总量与边界
fuxiang_catalog_items(limit=0)      # 需要全量清单时一次取回
fuxiang_categories                  # 需要按左栏分类定位时
fuxiang_find(item_id=...)           # 定点确认某个物件
fuxiang_component_detail(...)       # 定点确认某个组件类型的可创建性
fuxiang_catalog_verify              # 交付/报告需要机器可核验证据时
```

---

## 六、验证方法与证据

### 1. 接口层端到端（真正走 MCP stdio 协议）

```powershell
$py = '<你的-python.exe-完整路径>'
& $py tools/fuxiang/tests/mcp_interface_check.py            # 冒烟：17 项断言
& $py tools/fuxiang/tests/mcp_interface_check.py --sweep    # 追加全量扫描
```

该脚本启动真正的 MCP 服务器进程，按 `initialize` / `tools/list` / `tools/call` 发起调用，
与「直连 `cli/fxcall.py`」的历史脚本不同，可以直接验证对外暴露的接口层。
结果写入 `tests/mcp_interface_result.json`。

### 2. 全量可达性自检（单次调用内遍历）

`fuxiang_catalog_verify` 会遍历全部 3963 个物件与全部 105 个类型，与 `fuxiang_find`
共用同一份索引，因此通过自检等价于「每个编号都能被 `fuxiang_find` 命中」。

### 3. 离线快照

`fuxiang_catalog_export` 写出 `registry/`：`component_types.json`、`items.json`、
`enums.json`、`summary.json`（另有 `raw_*.json` 为原始表转储）。可用于版本间比对。

---

## 七、已知限制

1. **可达 ≠ 可创建**。`creatable` 为真的 97 种类型中，**82 种**已有服务端摆放的
   实机记录（2026-09-19 复核，`fuxiang_scene.SERVER_COMPONENT_TYPES`），其中
   9 种（含密码机/道具箱/NPC/大门）还过了**保存→重进**持久化闭环；
   真正不受理的只有 `203013 图片墙`。其余为本地预览白名单实测结果。
2. **列表可见性依赖编辑模式**。`ui_mode_hidden_total` 随模式变化，不是固定值。
3. **索引缓存**在 `fuxiang_catalog._CACHE`，键为 `(room, 组件数, 类型数)`；
   切图或大改结构后应传 `refresh=true`。
4. 服务端删除、复杂多实体类型、多人同步仍未验证（与 `HANDOVER_FIX.md` 一致）。
