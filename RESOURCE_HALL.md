# 资源大厅：资源定位

> 只读定位层。**不下载、不上传、不分享、不创建、不删除、不切换页签**。
> 实机断言见 `tests/mcp_interface_result.json`，原始快照见 `registry/resource_hall_OFFICIAL.json`，
> 探针脚本见 `probe/zz_rh*.py`。

---

## 一、结论

**能找到。官方页签实测 376 条组合物体，已全量导出。**

| 问题 | 结论 |
|---|---|
| 「资源大厅」是什么 | 编辑器里的**组合（Composite）库**，页签为「官方推荐 / 组合分享 / 我的组合」 |
| 资源清单在哪 | **列表页 UI** `ui.UIBattleUGCPart.UIBattleUGCResourceHall.UIBattleUGCResourceHall` 的 `_cur_cards` |
| 一条资源是什么 | **组合记录**：`full_doc` 为完整记录，`combo_data = {root, child[...], motion[...]}` |
| 官方有多少 | **376 条**（实测，全部由官方账号发布） |
| 能否用接口取到 | 能。7 个工具：`fuxiang_resource_hall` / `_ui` / `_cards` / `_item` / `_items` / `_verify` / `_export` |
| 限制 | 卡片清单只存在于**大厅打开期间**；未打开时 `open=false` |

### ⚠️ 一处重要修正

`UGCEditCompositeLibraryPart._shared_library` **不是**大厅全量清单 ——
它只保存**已下载/已分享**的组合（实测只有 1 条，即已下载的「欧式沙发1」）。
**全量资源清单在 UI 的 `_cur_cards`。** 两层要分清：

```
列表页 UI  UIBattleUGCResourceHall                 ← 资源清单（_cur_cards，376 条）
   │  点击卡片 → request_open_resource_hall_info(combo_id)
   ▼
详情页 UI  UIBattleUGCResourceHallInfo
   │  下载 → 落到 part 层
   ▼
数据 part  UGCEditCompositeLibraryPart
             ├── _local_library   我的组合        get_library(1)
             └── _shared_library  已下载/已分享    get_library(2)
```

---

## 二、分类、排序、标签（实机取值）

三个页签（`_get_category_group()` 实测，含中文名）：

| child_id | 中文名 | 含义 | 可用排序 |
|---|---|---|---|
| 20001 | **官方推荐** | 官方 PGC 组合 | 推荐(0)、最热(2) |
| 20002 | **组合分享** | 玩家分享 | 推荐(0)、最新(1)、最热(2) |
| 20003 | **我的组合** | 自己保存/下载 | 最近下载(3)、最近使用(4) |

排序值来自 `UGC_COMPOSITE_HALL_SORT`：`RECOMMEND=0 LATEST=1 HOTTEST=2 RECENT_DL=3 RECENT_USE=4`。

十个标签（`_get_tag_list()` / `DM.ugc_combo_tag`，含中文名与官方条目数）：

| id | 名称 | 条数 | id | 名称 | 条数 |
|---|---|---|---|---|---|
| 1000 | 全部 | 376 | 5 | 场景建筑 | 34 |
| 1 | 花草植被 | 15 | 6 | 功能器具 | 40 |
| 2 | 环境装饰 | 47 | 7 | 奇幻氛围 | 48 |
| 3 | 生活用品 | 49 | 8 | 生物 | 17 |
| 4 | 室内装饰 | 110 | 99 | 其他 | 16 |

排行榜 key（官方 / 组合分享页签的数据来源）：

```
rank_ugc_combo_hot / rank_ugc_combo_new / rank_ugc_combo_rec
rank_ugc_combo_official_hot / rank_ugc_combo_official_rec
```

---

## 三、卡片结构与加载路径

`_cur_cards` 每张卡片：

```python
{
  'combo_id':        '66568',            # 组合 ID
  'name':            '水桶',              # 已解析的中文名
  'cover_url':       'https://…png',     # 封面
  'combo_share_code':'uvqiCa7U',         # 分享码
  'occupy_num':      284,                # 占用值
  'is_recommend':    False,              # 是否推荐
  'is_downloaded':   False,              # 是否已下载
  'is_selected':     False,              # 管理模式下是否勾选
  'multi_select_mode': False,
  'full_doc': {                          # 完整组合记录（UGC_COMBO_KEY 子集）
      '_id', 'author', 'tag', 'desc', 'dl_cnt', 'cover', 'combo_permission',
      'combo_share_code', 'create_ts', 'last_ts', 'publish_ts', 'gm_ver',
      'name', 'occupy_num', 'photo_extra'
  },
}
```

**加载路径**（UI 自身方法；本次只逆向记录，未调用）：

- 官方 / 组合分享：`_async_load_rank`（异步生成器，榜驱动）
- 我的组合：`_load_mine_recent` → `part.request_mine_recent(typ)`，排序映射 `_MINE_SORT_TO_TYP`
- 搜索：`_async_search` / `_build_mine_search_candidates`
- 分类与状态持久化：`_resolve_init_category` / `_get_saved_filter` / `_save_cur_category`
- 官方已读标记：`_is_official_read` / `_mark_official_read`（对应事件 `COMPOSITE_OFFICIAL_READ_CHANGED`）
- 下载后同步：`_set_card_downloaded` / `_sync_items_data_downloaded` / `_collect_downloaded_id_set`
- 管理（删除）：`_enter_manage_mode` / `_do_delete`

---

## 四、接口清单

| 工具 | 用途 |
|---|---|
| `fuxiang_resource_hall` | 结构常量：页签 / 排序 / 标签 / 记录字段表 / 两个 part 库状态 / 下发通道 |
| `fuxiang_resource_hall_ui` | **实况**：当前页签·排序·标签·搜索词·管理模式，三个页签中文名与可选排序，卡片数（可带清单） |
| `fuxiang_resource_hall_cards` | **资源清单**：当前页签的卡片，支持中文搜索 / 标签 / 只看已下载 / 只看推荐 / 分页，`limit=0` 取全部 |
| `fuxiang_resource_hall_item` | 按 `combo_id` / 分享码取单条，拆解 `combo_data`（root/child/motion + 每个物件的参数） |
| `fuxiang_resource_hall_items` | part 层：我的 / 已下载 组合记录 |
| `fuxiang_resource_hall_verify` | part 层记录自检 |
| `fuxiang_resource_hall_export` | 把当前页签清单导出为 JSON 快照到 `registry/` |

游戏侧 op：`hall_info` / `hall_ui` / `hall_cards` / `hall_item` / `hall_items` / `hall_verify` / `hall_export`。

### 典型调用

```
fuxiang_resource_hall_ui                                   # 看当前页签与页签名
fuxiang_resource_hall_cards(limit=0)                       # 取当前页签全部资源
fuxiang_resource_hall_cards(query="桌")                     # 中文搜索
fuxiang_resource_hall_cards(tag=4, limit=0)                # 按标签
fuxiang_resource_hall_item(combo_id=66568, with_param_detail=true)
fuxiang_resource_hall_export                               # 落盘快照
```

**读取其它页签**：在游戏内点击「组合分享」或「我的组合」，再调用 `fuxiang_resource_hall_cards` 即可。

---

## 五、实测数据（两个页签都已抓取）

### 页签 1：官方推荐（20001）— 376 条

```
基本
  条数        376（combo_id 全部唯一）
  发布账号     胡桃今天吃什么 (uid 184421208)   ← 官方发布号
  排序         RECOMMEND(0)   标签 全部(1000)
  已下载       1（317053 欧式沙发1）
  标签分布     室内装饰 110 / 生活用品 49 / 奇幻氛围 48 / 环境装饰 47 /
              功能器具 40 / 场景建筑 34 / 生物 17 / 其他 16 / 花草植被 15

样例
  66568  水桶        分享码 uvqiCa7U  占用 284  下载 41178  标签 生活用品
  66636  货架1       分享码 u3w5fA0k  占用  46  下载 12008  标签 环境装饰
  317053 欧式沙发1   分享码 H4JGWf41x 占用 170  下载 92919  标签 室内装饰（已下载）
  …快照 registry/resource_hall_OFFICIAL.json（376 条，519 KB）
```

### 页签 2：组合分享（20002）— 玩家自制，已加载 500 条

```
基本
  条数        500（当前已加载；列表为滚动分批加载，滚动可继续拉取）
  作者数      228 位不同玩家           ← 与官方页签（单一发布号）形成鲜明对比
  排序         RECOMMEND(0)   标签 全部(1000)
  已下载       0
  标签分布     室内装饰 118 / 生物 105 / 花草植被 83 / 场景建筑 64 / 其他 60 /
              生活用品 32 / 环境装饰 20 / 功能器具 14 / 奇幻氛围 4

高产作者（按条目数）
  罒Time 18 / 鄧大少爷 15 / 绘入画 14 / 卷卷回 12 / 笨蛋就是笨蛋呀 11 /
  冷酷Bqueen 10 / 奶油泡芙犭苗 10 / 猫桃七 9 / 名杯佣兵 8 / 我没友玩 8 …

下载量 TOP
  157000  园丁维丽斯          52074   K1Ity
  98254   小女孩恶之源        41886   稀世辞章
  109422  约瑟夫DM            23334   笨蛋就是笨蛋呀
  111700  拱门                20326   久然字
  104950  失职的看守          15656   LosrelX
  72005   【MH】藤绕拱门（全权限可编辑） 12366  冷酷Bqueen
  98253   渔女静默之戟        10999   稀世辞章
  86451   拱门                10713   园丁发位下置
  98255   先知白              10624   稀世辞章
  131006  渔女沁凉夏日         9825   罒Time
  244950  金竖琴               7851   法粗
  73237   入殓师-加特          7808   卷卷回

样例
  1111151 无名布偶      分享码 0dn2LSVj3  占用 562  标签 生物
  98254   小女孩恶之源  分享码 mF2QHDCC   占用  60  标签 其他
  111700  拱门          分享码 qphbUvsz   占用 602  标签 花草植被
  96603   飘花          分享码 mQh8MJzW   占用 1325 标签 花草植被
  65552   小白花        分享码 uyWnjPBf   占用   1  标签 花草植被
  …快照 registry/resource_hall_SHARED.json（500 条，981 KB）
```

> 对比：官方页签是**单一发布号**的 376 条精选；组合分享页签是 **228 位玩家**的创作，
> 内容形态差异大（角色皮肤摆件、花草植被、建筑构件），高下载条目以「角色/雕塑类」为主。


单条查询（`fuxiang_resource_hall_item`）会依次查两级：

1. **part 层**（`_local_library` / `_shared_library`）→ 有 `combo_data` 本体；
2. **大厅 UI 卡片**（`_cur_cards`）→ 只有元数据（`full_doc`）。

```
hall_item(combo_id="157000")   # 组合分享页签的条目
  found: true   source: resource_hall_ui._cur_cards   library: SHARED
  name: 园丁维丽斯   author: K1Ity   标签: 生物   下载: 52074   占用: 60
  share_code: HZWYzlety
  combo_data_available: false   # 未下载 → 客户端没有本体

hall_item(combo_id="317053")   # 已下载过的条目
  found: true   source: part 的 _shared_library
  combo_data 可用（child/root 分段）
```

⚠️ **`combo_data` 只在「已下载/已使用」后才落到客户端**。要拿到某个未下载组合的本体，
需先在游戏内下载它，再用 `fuxiang_resource_hall_item` 读取。


---

## 六、下载组合（写操作）

> ⚠️ **这是对玩家账号库的服务端写操作**，不是本地读取。会真实改动账号数据，
> 且本接口**不提供回滚**（删除须在游戏内资源大厅的管理模式里做）。

### 机制

```
UI : request_download_composite(combo_id, share_code)
       if self._op_fetch.running: return          # 一次只允许一个
       if core_part.is_library_full(SHARED): ...  # 库有上限
part: fetch_shared_composite(share_code)
       -> _rpc_fetch_shared_composite(share_code)
       -> core.call_server(<iUGCEditCompositeLibraryPart>, func, share_code)   # 服务端 RPC
回包: on_combo_fetch_reply(ok, res)，res 含 input / output
```

### 硬性限制（实测 `GK.DM.ugc_battle_global_data`）

| 键 | 值 | 含义 |
|---|---|---|
| `ugc_max_shared_combo_num` | **200** | 分享/已下载库上限 |
| `ugc_max_local_combo_num` | **200** | 我的库上限 |
| `ugc_combo_recent_dl_limit` | 100 | 「最近下载」只留 100 |
| `ugc_combo_recent_use_limit` | 100 | 「最近使用」只留 100 |

`is_library_full` 优先取上表配置（类内兜底常量是 100，但配置存在时按配置走）。
`g_avatar.get_ugc_local_combo_limit()` 实测也返回 200。

**并发**：一次只允许一笔在途请求（UI 侧 `_op_fetch.running`；本接口另有 `pending` 锁）。
**延迟**：服务端异步处理，实测**约 15–40 秒**才回包。

### 接口

| 工具 | 用途 |
|---|---|
| `fuxiang_combo_download_status` | 诊断：能力、库容量与占用、待确认请求、最近记录 |
| `fuxiang_combo_download` | 按 `share_code` 下载**一条**（发送后立即返回，须轮询） |
| `fuxiang_combo_download_poll` | 查询结果：`downloaded` / `pending` / `unknown` |

**只接受单条 `share_code`，不支持批量** —— 批量会顶到 200 上限且无法回滚。

### 试点记录（1 条，已完成）

```
share_code  uyWnjPBf    组合 65552「小白花」（作者 心跳回忆集，标签 花草植被，占用 1）
发送        status=sent，库大小 1
轮询        约 15–40 秒后 status=downloaded，new_ids=["65552"]
库变化      shared_ids: ["317053"] -> ["317053", "65552"]
本体        has_combo_data=true；combo_data = {child: 21, root: 3}
            首个 child: item_id=301042 / ugc_type=100001(GEOMETRY) / 6 个参数
            （位置·旋转·缩放·模型路径·颜色表 ["ffffff","f8b4fe",…]·…）
```

即 **分享码 → 服务端 RPC → 进入账号组合库 → `combo_data` 可读** 全链路已打通。

### 若要做大批量下载（未执行，需另行评估）

按实测单条 15–40 秒估算，200 条需 **50 分钟 ~ 2.2 小时**连续 RPC；
叠加库上限 200 与风控/限流风险，建议：分批（每批 ≤10）、每条等回调完成后才发下一条、
并在批次间留间隔。

---

## 七、已知限制

1. **卡片清单随 UI 生命周期**。`_cur_cards` 只在大厅打开期间存在；关闭后
   `fuxiang_resource_hall_ui` 返回 `open=false`。需要留档时用 `fuxiang_resource_hall_export`。
2. **接口不主动切换页签 / 排序**（属写操作）。要读其它页签，请在游戏内点击对应页签后重新调用。
3. `_shared_library`（part 层）只反映「已下载 / 已分享」，**不等价于**大厅清单。
4. 官方条目识别：当前页签本身就是官方推荐；单条层面可用 `author`（官方发布号）或
   `full_doc.name` 的 `TID_UGC_PGC_` 前缀辅助判断。
5. 下载 / 分享 / 上传 / 删除组合**不在本次范围**（写操作，涉及发布通道）。
6. `_get_sort_group(category)` 返回的是**排序 key 序列**（字符串），不是配置字典；
   中文名需用 `UGC_COMPOSITE_HALL_SORT` 反查。
7. **列表条数是「已加载」而非「全服总数」**。官方页签 376 条、组合分享页签当前 500 条
   （`_req_seq=2`，列表滚动会继续分批加载）。想要更多条目，在游戏内往下滚动即可。
8. **`combo_data` 本体只在下载后才在客户端**。未下载的条目只有卡片元数据；
   `fuxiang_resource_hall_item` 会明确返回 `combo_data_available: false`，不会伪造。
9. 三个页签的排序集合不同（官方 2 种、组合分享 3 种、我的 2 种），
   接口不主动切换排序；要换排序请在游戏内点选后再读。
