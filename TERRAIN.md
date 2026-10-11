# 地形复刻研究

> 结论日期：2026-09-23 ｜ 全部结论为实机探测所得（地图「樱花残羽艾玛的地图」）
> 目标：搞清楚「地形」在这套 UGC 编辑器里到底是什么、能不能导出、能不能复刻。

---

## 0. 结论先行

**UGC 编辑器没有地形雕刻能力。** 地形不是可编辑数据，而是「**场景框架**」的一部分——
由**地图设置字段 14**（下拉三选一 `4001 / 4002 / 4003`，当前图 = `4002`）决定。

所以：

| 想复刻的东西 | 能不能 | 怎么做 |
|---|---|---|
| 地形底子（场景框架） | ✅ 能 | 快照里已有 `map.framework_id`（= setting 14），还原时 `set_map_setting_data(14, "4002")`（**已验证可写**） |
| 地形起伏的高度数据 | ⚠️ 能读、不能原生写 | `room.get_pos3_height(vector)` 采样成高度场；要"造"出起伏只能用几何体拼 |
| 地形雕刻（刷高刷低） | ❌ 不存在 | 编辑器没有这个功能，无 API 也无 UI |

一句话：**复刻地形 = 复刻那个框架 id**；真有起伏需要等效复刻时，用「采样高度场 + 几何体拼装」。

---

## 1. 证据（逐条实测）

| # | 探测 | 结果 |
|---|---|---|
| 1 | 编辑器内核的全部 part（13 个） | 只有 component_info / direct_data / photo_mgr / **map_edit_data** / **map_setting** / co_creator_* / nav_path / checkpoint_route / select_unit / composite_library / debug —— **没有任何 terrain part** |
| 2 | `sys.modules` 里 UGC 界面模块（541 个） | 过滤 terrain / ground / height / land / mountain → **0 个** |
| 3 | `room` 上的地形相关属性 | 只有**查询**类：`get_pos3_height` / `get_detour_height` / `get_near_pos3_height` / `has_terrain_walls` / `no_terrain_scene`；**没有 set/编辑类** |
| 4 | `iUGCEditMapEditDataPart` | `FIELD_MAP_EDIT_DATA = "map_edit_data"`；`get_map_edit_data()` = `{}`，`_get_room_map_edit_data()` = `{}`，room 上**根本没有这个属性** ⇒ 该通道在本图无数据 |
| 5 | `room.no_terrain_scene` | **= 1**（当前图是无地形场景）；`has_terrain_walls = 0` |
| 6 | 高度采样（5×5 网格，±800） | `get_pos3_height` 全部返回 `None` —— 与 no_terrain_scene=1 一致 |
| 7 | `GK.DM.ugc_setting_config[14]` | `{'id':14, 'type':8, 'item_params':'4001,4002,4003', 'default_value':'4002', 'title':'TID_UGC_SETTING_TITLE_14', 'desc':'TID_BATTLE_UGC_SETTING_FRAME_SETTING_FRAME_8'}` ⇒ **下拉型（type=8），三个候选值** |
| 8 | `UGCEditMapSettingPart` 的方法 | `set_map_setting_data(key, value, source='') -> bool`<br>`submit_map_setting_draft(data: Dict, source='') -> bool`<br>`save_map_basic_info(name, desc, tags, sub_tags, topics, source='') -> bool` |
| 9 | **幂等写入验证** | `set_map_setting_data(14, "4002")` → 返回 **True**，回读仍 `4002`，**脏缓冲仍为 0** ⇒ 写通道可用且幂等 |

注：`map_edit_data` 这个通道仍然存在且**可写**（`set_map_setting_data` 的兄弟接口 `set_map_edit_data`），
但本图它始终是空的。若将来官方开放地形编辑，数据就会落在这里，快照已经存了它。

---

## 2. 三档复刻方案

### 方案 A · 同框架复刻（推荐，已可实施）

地形底子由框架 id 决定 ⇒ 目标图用同一个框架，地形/场景底子就一致。

- 快照侧：`map.framework_id`（setting 14）+ 整份 `map.map_setting`
- 还原侧：`set_map_setting_data(14, <framework_id>)`，或整份 `submit_map_setting_draft(data)`
- ✅ 写通道已验证（返回 True、幂等、不产生脏数据）
- ⚠️ **待验证**：运行中改框架是否会**立即**换场景（可能要重进地图才生效）。
  这个实验要改一个真实字段值到不同值，**应该在测试图上做**，不在你正在编辑的图上做。

### 方案 B · 高度场采样 + 几何体拼装（等效复刻起伏）

当两张图框架不同、或需要把 A 图的起伏搬到 B 图时：

1. **采样**：`room.get_pos3_height(math3d.vector(x, 9999.0, z))` 按网格步长 S 采样，得到高度场。
   已实现：`python cli/snapshot.py export --terrain 100`（写入快照的 `terrain` 段）。
2. **拼装**：每格一块 GEOMETRY（301xxx）。
   - GEOMETRY 原生尺寸 10³（`bounding_box=5`）⇒ 实际尺寸 = `ugc_size × 10`
   - 一块覆盖 S×S：`ugc_size = (S/10, H/10, S/10)`
   - 块中心：`y = (H_base + h)/2`（H_base 取全场最低点，避免悬空）
   - ⛔ 单块尺寸 >100 会有渲染缺陷 ⇒ **步长 S ≤ 100**

| 步长 | 覆盖 2240×2240 的块数 | 占组件预算(15000) |
|---|---|---|
| 100 | 23×23 ≈ 529 | 3.5% |
| 50 | 45×45 ≈ 2 025 | 13.5% |
| 25 | 90×90 ≈ 8 100 | 54%（不建议） |

⚠️ 这是**近似**：台阶状、非平滑，且要占组件预算。只在确有需要时用。

### 方案 C · 人工选框架（最稳，零风险）

快照只记下 `framework_id`，新建地图时在界面上手工选同一个框架。
适合"我要在新图里复刻这张图的场景底子"这类一次性操作。

---

## 3. 已实现的东西

- `cli/snapshot.py export --terrain <步长>`：采样高度场写入快照 `terrain` 段
  （`meta` = 范围/步长/有效点数/no_terrain_scene；`grid` = 二维高度数组）
  - ⛔ 采样网格**必须压成 JSON 字符串回传**：`_safe` 对 list 只留前 200 项，
    一张 23×23 = 529 点的网格直接回传会被静默截断
  - 实测：步长 200 → 12×12 = 144 点，**有效高度 0**（因为本图 `no_terrain_scene=1`，符合预期）
- 快照 `map.framework_id`：直接取 `map_setting["14"]`，不必再解码
- 快照一直都在存 `map.map_setting`（整份）与 `map_edit_data`

## 4. 还没验证 / 做不了

| 项 | 状态 | 说明 |
|---|---|---|
| 改框架是否立即换场景 | ⚠️ 待验证 | 需在一张测试图上把 14 改成 4001/4003 观察（**不能在你正在编辑的图上试**） |
| 有地形的图上采样是否有值 | ⚠️ 待验证 | 本图 `no_terrain_scene=1`，`get_pos3_height` 全 None。需要在有地形的模板图上复验 |
| 地形网格能否直接导出 | ❌ 做不到 | `world.get_all_model()` 在本上下文报 `SystemError`；且地形不是 UGC 组件，不在快照体系内 |
| 原生地形雕刻 | ❌ 不存在 | 无 part、无 UI、无写接口 |

## 5. 一个必须知道的安全事实（本次顺带发现）

删除物件后，`room.del_unit_uids` 里可能残留待删除标记。本次删除试摆件（ugc_id 177）后，
袋子里留了 `{'unit': [1000100]}`（uid 1000100 = 已删的 177，位置已是哨兵 `[0,-65536,0]`）。

- ✅ **未误标到存活物件**（已逐个核对 uid → 组件 → 位置）
- ⛔ **不要手动清这个标记**：`change_del_unit_uids(False, uid, 'unit')` 虽然存在，
  但清掉删除标记会让已删的件在下次保存后**复活**（产出重复件）——这是踩过的坑。
- 正确做法：正常保存一次（或退出时的自动保存），让删除真正上送，袋子随之清空；
  之后 `snapshot export` 的告警会消失。
- 判据：**清理前先核对袋子里的 uid 到底指向谁**，别只看"袋子非空"就动手。
