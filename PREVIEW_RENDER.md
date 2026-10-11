# 预览渲染（引擎导出全局场景样貌）— 可行性分析

> 结论基于 **2026-09-16 对 dwrg.exe 的实机只读/受控探针**，
> 不是文档推测。所有接口签名取自运行中引擎的 `__doc__`，所有结论附日志或产物证据。
> 探针脚本见 `probe/zz_render1.py` ~ `zz_render11.py`，产物见 `preview/evidence_engine_capture.png`。
>
> **实现状态：路线 A 与路线 C 已落地。** 见文末 §8。

---

## 0. 结论速览

| 路线 | 内容 | 可行性 | 证据 |
|---|---|---|---|
| **A. 引擎镜头快照** | 程序化摆相机 → 引擎渲染 → 直接落 PNG | ✅ **已实证跑通** | 产出 1920×1200 真实场景 PNG，日志零报错 |
| **B. 离屏 RT 单张全局图** | `scene.render(rt_id)` + 纹理回读 | ❌ **本机渲染后端明确拒绝** | 引擎两条 ERROR/WARNING：非 render frame + 回读未实现 |
| **C. 纯数据 2D 俯视平面图** | 读组件变换，离线画俯视平面图 | ✅ 零风险、可离线 | 数据源已具备（`fuxiang_edit_targets`） |

**边界澄清（重要）**：

- 「**不用人工进编辑器操作**」→ ✅ 可达成。相机摆位、取景、出图全自动。
- 「**完全不启动游戏**」→ ❌ 不可达成。地图保存是服务端权威，UGC 物件是**客户端运行时构建的 unit**，
  场景不在本地任何文件中（`room.scene_path` 指向的是**空模板**场景
  `scene/scene_battle/h55_hospital2_ugc_customize_empty/...`，物件是运行时挂上去的）。
  所以目标应表述为「**无人值守的自动化预览**」，而非「离线渲染」。

---

## 1. 决定可行性的三个架构事实

这三点是本次探针最重要的发现，直接划定了能力边界。

### 1.1 UGC 物件不是 `world.model`，而是实体 unit

| 探针 | 结果 |
|---|---|
| `scene.get_bounding()` | `(±3.4e38, …)` — **空包围盒**，说明场景里没有注册任何 model |
| `scene.get_models_in_sight()` | `0` |
| `scene.get_all_visible_models()` | `0` |

编辑器里明明有 100+ 个物件，但引擎场景的模型表是空的。
因为物件走的是 `room.create_unit_with_info` → **unit + component** 体系，挂在 `room.unit_mgr`，
不是挂在 `scene` 的 model 表上。

**推论**：
- 不能用 `get_bounding()` 自动求"全图范围"，必须**自己遍历组件算 AABB**（数据现成）。
- 但这也**不影响渲染** —— 引擎照样把它们画出来（证据图里能看到桌子、地下室盖板）。

### 1.2 客户端渲染后端是 MobileEngine，能力被裁剪

日志原文：

```text
[18:12:07.487] [RENDER] Texture Readback not implemented!
[18:12:07.487] [RENDER] Unsupported API on MobileEngine
```

**推论**：任何依赖 **GPU→CPU 像素回读**的方案（离屏 RT 读像素、深度图导出、素材烘焙）在本客户端**不成立**。
出图只能走 **引擎自己写文件** 的通道（`save_*_to_file`）。

### 1.3 桥接的执行上下文不是 render frame

日志原文：

```text
[18:11:29.275] Should only call Scene::Render in render frame
```

桥接回调跑在游戏主线程的 tick 回调里，**不是**渲染帧内。引擎对 `Scene::Render` 做了调用点校验。

**推论**：手动"渲染到贴图"这条路被引擎主动封死；必须让**引擎自己的帧循环**去画，
即"把相机摆好 → 等引擎画 → 抓引擎画好的那一帧"。

---

## 2. 能力盘点（全部实机实测）

| 接口 | 引擎文档签名 | 实测结果 |
|---|---|---|
| `scene.capture_scene_snapshot_to_file` | `(file_name, format=3, flags=0, pixel_format=61)` → bool | ✅ **返回 True，约 1~2s 后落盘 1920×1200 PNG** |
| `render.save_screen_to_file` | `(file_name, format=IFF_TGA, flags, priority, callback, async_capture)` → bool | 未测（同族备用，`IFF_PNG=3`） |
| `render.save_screen_to_file_ex` | 同上 + `dest_w/dest_h/uv_*` 裁剪缩放 | 未测（**可用于只截场景区、裁掉 UI**） |
| `scene.create_camera` | `(active)` → `world.camera` | ✅ 返回 `camera_2779/2780/2781` |
| `camera.set_ortho` | `(width, height, min_z, max_z)` | ✅ 调用成功 |
| `camera.set_placement` | `(pos, forward, up)` | ✅ 矩阵回读正确：位置 `(630, 308, -95)`、前向 `-Y`、上向 `+Z` |
| `camera.transformation` | 读矩阵 | ✅ 可反解 pos/forward/up，**可用来备份与还原** |
| `scene.active_camera` | 属性 | ✅ **可读且可写**（赋值后生效） |
| `scene.set_as_main_camera` | `(name)` | ❌ 只接受**预设相机名**，无法接管动态相机 |
| `scene.render` / `render_once` | `(rt_id, viewport)` | ❌ `Should only call Scene::Render in render frame` |
| `render.create_render_target_surface` | → rt id | ⚠️ 日志报 **obsoleted**（引擎建议改用 `create_render_target`） |
| `render.create_render_target` | `(dest_texture, …, depth_format, …, color)` → 1-254 | ⚠️ 返回大整数 id（如 `1745053136208`），**与文档 1-254 不符**，语义待定 |
| `render.texture.create_empty` | `(w,h,format,render_target,use_mip)` | ✅ 返回 `<py_unmanaged_tex_001852>`，`size` 属性 = `(768,768)` |
| `texture.get_data` | `(readonly=False)` — **加锁** | ❌ `Texture Readback not implemented!`，返回全 0 |
| `texture.save` | `(filename, format)` | ⚠️ 返回 None 且不落盘（因 RT 从未被写入） |
| `camera.destroy` | — | ⚠️ 调用返回 True，但**随后约 1.5s 进程崩溃**（见 §4） |
| `GK.g_render_hook_mgr` | `RenderHookMgr.register/pause/resume` | 存在，未深挖（**后续可作"帧内执行"的突破口**） |
| `world` / `render` / `neox.world2` / `math3d` | 模块 import | ✅ 全部可导入（`render` 有 927 个成员） |

---

## 3. 三条路线逐条评估

### 路线 A：引擎镜头快照 ✅ 推荐主线

**原理**：不碰渲染管线，只**摆相机**，让引擎自己画，然后抓它画好的帧。

**已实证的最小闭环**（本次真实执行，非推演）：

```python
sc = room.scene
sc.capture_scene_snapshot_to_file(r'...\preview\shot.png')   # format 默认 3 = IFF_PNG
# → True；1~2s 后文件出现：1920×1200 PNG，455 KB，内容为真实 UGC 地图
```

产物即 `preview/evidence_engine_capture.png`：可见桌子、地下室盖板、地形、天空盒 —— **真实场景渲染，非黑屏**。

**全局取景的算法**（不需要 `get_bounding()`）：

```
1) 遍历编辑器已登记组件  →  每个 unit 的 pos / rot / scale / item_id
2) 求 AABB：center = (min+max)/2，radius = |max-min|/2
3) 生成视角：
   - 正俯视（正交）：set_ortho(span, span, 1, H) + set_placement(center + (0,H,0), -Y, +Z)
   - 四角斜 45°（透视）：距离 d = radius / tan(fov_y/2) * 1.15
4) 逐视角：摆位 → 等 2 帧 → 出图
```

**注意**：`set_ortho` 的正交"正交"是靠相机矩阵实现，本次已验证 `transformation` 矩阵正确。

**风险**：
- 摆位后必须**精确还原**（用 `transformation` 备份）。
- **不要 destroy 相机**（见 §4 崩溃）。
- 建议**复用 `room.cam` 这一个相机**来回摆，而非新建 —— 新建 + 销毁正是崩溃路径。

### 路线 B：离屏 RT 单张全局图 ❌ 本机不可行

**原理**：新建 RenderTarget，把相机输出绑上去，渲染一次，然后存图 —— 一次拿到全图、不打扰玩家视野。

**实测结论：被引擎双重封死。**

| 步骤 | 结果 |
|---|---|
| `texture.create_empty(768,768,A8R8G8B8,render_target=True)` | ✅ 成功 |
| `create_render_target_surface(...)` | ⚠️ 成功但**已废弃**；`create_render_target` 返回大整数 id |
| `camera.set_output_rt(rt_id)` | ✅ 调用成功 |
| `scene.render(rt_id, viewport)` / `render_once` | ❌ `Should only call Scene::Render in render frame` |
| 把该相机设为 `active_camera`，等 3 秒 | ⚠️ 相机确实接管了（`active_camera == camera_2781`），但 |
| `texture.get_data(True)` 回读 | ❌ `Texture Readback not implemented!`，409600 像素**全 0** |
| `texture.save(png)` | ⚠️ 返回 None，**不落盘** |

**结论**：路线 B 在本客户端**不可行**，且是**不可绕过的引擎级限制**（非权限、非配置问题）。
唯一保留的可能性是 `GK.g_render_hook_mgr`（RenderHookMgr）—— 若能把渲染调用塞进 render frame，
`scene.render` 的报错或可消除；但**像素回读未实现**这条仍是硬墙，除非改用"RT 绑相机 + 引擎自己画 + 抓帧"，
那就等价于路线 A 了。**不建议投入。**

### 路线 C：纯数据 2D 俯视平面图 ✅ 推荐补充

**原理**：完全不进渲染管线，只用已有数据画示意图。

- 数据源：`cm._ugc_id2component` → `unit.get_position3()` / `ugc_size` / `ugc_type`（= item_id）
  - 已经被 `fuxiang_edit_targets` / `fuxiang_edit_target` 封装好
- 名称：`GK.DM.ugc_item_data`（3963 项目录）
- 出图：Pillow / matplotlib 画 4096×4096 俯视平面图，每个物件按其 bbox + 旋转画矩形，标注编号与名称

**优点**：一次看到全图（不受单个镜头视野限制）、可标注、可算间距/重叠、**零崩溃风险**、可离线批量跑。
**缺点**：不是"游戏里那样"的渲染图，是示意布局图。

---

## 4. 本次踩到的坑（务必写进实现护栏）

### 4.1 崩溃事件

```text
[18:13:03.138] [RENDER] Texture Readback not implemented!
[18:13:03.149] [RENDER] Unsupported API on MobileEngine
[18:13:04.889] [SCRIPT] crash_event_sync_callback event_type == 8,
               file_path=<用户目录>\AppData\Local\UniSDK\CrashDump\1.8.1\h55\31264
```

**进程 dwrg.exe (pid 31264) 已退出**，崩溃转储目录已创建（内容为空，SDK 未落盘）。

**归因**：崩溃发生在 RT 实验的最后一步 —— `camera.destroy()` + `release_render_target()` 之后约 1.5 秒。
此时该相机刚被设为活动相机、且绑定着刚被释放的 RT，**引擎帧在途仍在引用它**。
属于"销毁了帧在途还在用的资源"这一经典悬垂引用。

**护栏**：预览功能**不创建、不销毁相机、不创建、不释放 RT** —— 只用 `room.cam` 来回摆位。

### 4.2 三条硬禁止

| 禁止 | 原因 |
|---|---|
| `scene.render` / `render_once` | 非 render frame，引擎拒绝（ERROR 日志） |
| `texture.get_data` / `NRenderTexture.get_attachment_pixels_data` | `Texture Readback not implemented!`（MobileEngine） |
| 帧在途时 `camera.destroy` / `release_render_target` | 已导致进程崩溃 |

### 4.3 两个易踩的语义坑

- `texture.size` / `texture.surface_size` / `texture.format` 是**属性不是方法**（调用会 `'tuple' object is not callable`）。
- `create_render_target*` 返回的 id 与文档 `1-254` 不符（实测为 13 位大整数），**不要用文档假设去校验**。

---

## 5. 推荐落地方案

### 5.1 接口设计（对齐现有 MCP 风格）

| 工具 | 用途 |
|---|---|
| `fuxiang_preview_plan` | 只算不拍：返回自动取景的视角列表、AABB、预估范围、输出路径（`dry_run`） |
| `fuxiang_preview_shot` | 拍**一个**视角（相机名/正交或透视/位置/朝向），返回文件路径与体积 |
| `fuxiang_preview_sweep` | 一键拍全套：正俯视 + 四角 + 可选环拍 N 张 |
| `fuxiang_preview_layout` | 路线 C：出 2D 俯视平面图（零风险） |
| `fuxiang_preview_restore` | 显式还原相机到备份状态（保险绳） |

游戏侧 op：`preview_plan` / `preview_shot` / `preview_sweep` / `preview_layout` / `preview_restore`。

### 5.2 `fuxiang_preview_shot` 执行流程

```text
1) 备份：cam.transformation + cam.fov/aspect/z_range + sc.active_camera
2) 摆位：cam.set_placement(pos, forward, up)   （正交视角先 set_ortho）
3) 返回"已提交"，不在主线程等待
   —— 由 MCP 客户端侧 sleep 约 0.3~0.5s（跨 2 帧）
4) fuxiang_preview_shot(poll) : 调用 capture_scene_snapshot_to_file(path)
5) 轮询文件出现（本次实测滞后 1~2s）+ 体积 > 0 → 判定成功
6) 下一个视角；全部结束后还原相机到步骤 1 的备份
```

**关键约束**：主线程只提交、不 sleep、不轮询（沿用 README 既有纪律）。
「等 2 帧」通过**两次独立调用 + 客户端侧间隔**实现，而不是在游戏主线程里 sleep。

### 5.3 取景自动求解

```python
# 已有数据，无需引擎支持
targets = edit.op_edit_targets({})            # 全部对象 + 变换
pts = [t['position'] for t in targets if t.get('position')]
minv = [min(p[i] for p in pts) for i in range(3)]
maxv = [max(p[i] for p in pts) for i in range(3)]
center = [(minv[i]+maxv[i])/2 for i in range(3)]
radius = max((maxv[i]-minv[i])/2 for i in range(3))
```

- 正交俯视：`span = 2*radius*1.15`，高度取 `center.y + 300`（避免裁到 z 跨度）
- 透视斜视：`d = radius / tan(radians(fov_y)/2) * 1.2`

### 5.4 已知待优化项

1. **截图含编辑器叠加层**：证据图中可见坐标网格线与半透明角色占位。
   属于编辑器 UI/调试渲染。可选处理：`render.save_screen_to_file_ex` 用 uv 裁掉边栏；
   或探索 `scene.set_debug_visibility` / `hide_models` / UI 层开关（**需实测，勿盲调编辑器状态**）。
2. **异步落盘**：`capture_scene_snapshot_to_file` 立即返回 True，文件滞后 1~2s 出现 → 必须校验文件而非信返回值。
3. **编辑模式屏蔽的对象是否入镜**：显示层过滤（`is_visible` / `ignore_tap_order_sec`）只影响列表，
   理论上不影响渲染，**待实测确认**。
4. **多视角拼接**：目前是"几张图"而非"一张全局图"。若要单张全局图，只能用路线 C。

---

## 6. 复现证据清单

| 文件 | 内容 |
|---|---|
| `preview/evidence_engine_capture.png` | **路线 A 实证产物**：1920×1200 真实 UGC 场景渲染 |
| `probe/zz_render1.py` / `_out_render1.json` | 渲染/相机入口定位（world 模块、room.scene 338 成员） |
| `probe/zz_render2.py` / `_out_render2.json` | `capture_scene_snapshot_to_file` 等 **11 个接口的引擎文档签名** |
| `probe/zz_render3.py` / `_out_render3.json` | `render` 模块 927 成员 + 格式常量 |
| `probe/zz_render4.py` / `_out_render4.json` | `save_screen_to_file*` / `create_render_target*` / `Texture` 文档 |
| `probe/zz_render5.py` / `_out_render5.json` | 相机可控面、`get_bounding` 空盒、可见模型 0 |
| `probe/zz_render6.py` ~ `zz_render11.py` | 路线 B（离屏 RT）全过程与失败点 |
| `log_inst1.txt`（18:11~18:13） | 引擎级报错原文 + 崩溃事件 |
| `<用户目录>\AppData\Local\UniSDK\CrashDump\1.8.1\h55\31264\` | 崩溃转储目录（SDK 未落盘，内容为空） |

---

## 7. 一句话建议

**做路线 A + 路线 C 的组合**：A 出「多个真实渲染视角图」作为观感预览，C 出「一张全图俯视平面图」作为布局总览。
**放弃路线 B** —— 它不是"要花功夫"，而是被引擎明确拒绝且不可绕过。
记住那条护栏：**不建相机、不毁相机、不碰 RT、不回读像素**，只用 `capture_scene_snapshot_to_file` 出图。

---

## 8. 实现落地（A + C，2026-09-16 实机通过）

### 8.1 交付物

| 文件 | 作用 |
|---|---|
| `bridge/fuxiang_preview.py` | **游戏侧**：取景分类、包围盒、视角生成、相机备份/摆位/还原、出图提交。全部为短操作。 |
| `mcp/preview.py` | **主机侧**：跨帧等待、文件轮询、2D 平面图绘制（Pillow）。等待只发生在本进程。 |
| `mcp/server.py` | 新增 6 个工具 + `handle_preview_call` 分派。 |
| `bridge/fuxiang_edit.py` | 新增 `preview_plan/pose/fire/restore/state/layout` 六个 op。 |
| `tests/preview_live.py` | 走真 MCP stdio 服务器的端到端验证。 |
| `tests/mcp_interface_check.py` | 新增 7 项预览断言。 |

### 8.2 接口

| 工具 | 说明 |
|---|---|
| `fuxiang_preview_plan` | 只算不拍：包围盒 + 视角列表 |
| `fuxiang_preview_shot` | 拍一个视角（摆位 → 等 2 帧 → 出图 → 等落盘） |
| `fuxiang_preview_sweep` | 一键拍全套 + manifest，默认还原相机 |
| `fuxiang_preview_layout` | 2D 俯视平面图 PNG（不动相机） |
| `fuxiang_preview_restore` | 相机还原 + 回读校验 |
| `fuxiang_preview_state` | 诊断 |

`fit=content | map | union`；取景参数 `margin / fov / elevation / dist_mul / span_mul / aspect`。

### 8.3 取景分类（本机实测数据）

本机地图 119 个已登记组件，分类结果：

| 分类 | 数量 | 处置 |
|---|---|---|
| 空间物件 | **7** | 参与取景（求生者(访客)、桌子5×2、地下室盖板×4） |
| 地图边界锚点 | 4 | 单独成组，`fit=map/union` 用于框整张地图 |
| 非空间组件 | **108** | 剔除（界面控件 48、运动途经点 50、全局规则类 10） |
| 读取失败 | 0 | — |

**关键修正过程**（初版曾把包围盒撑到 2560×2560 全图）：

1. `-65536` 是逻辑组件的**未落位哨兵**（运动途经点、胜利目标、坐标中心偏移等 53 个）。
2. `(0,0,0)` 上挂着 8 个 `item_type_group == "global"` 的全局组件（游戏时间、雾效调节、
   天空背景…），必须按目录分组剔除，而不是按坐标。
3. `±1440 / ±1120` 的 4 个「地图边界锚点」**不是**哨兵，是合法的可建造区域定义，保留。

三档 `fit` 在 `preview_plan` 里同时返回 `bounds_content` / `bounds_map` / `bounds`，
便于一眼看出"内容区"和"整张地图"差多少：

```
bounds_content  X [611.1, 640.1]  Z [-125.0, -37.7]    跨度 29 × 87
bounds_map      X [-1440, 1120]   Z [-1440, 1120]      跨度 2560 × 2560
```

### 8.4 实机验证结果

`tests/preview_live.py --shots --fit content`：

```
空间物件 7 · 边界锚点 4 · 非空间组件 108
top / iso_ne / iso_nw / iso_se / iso_sw  →  5/5 ok，490KB ~ 633KB，1920×1200
还原 verified = True
整套流程未改动编辑器相机 = True
总耗时 9.5 秒
```

`tests/mcp_interface_check.py` 新增 7 项预览断言**全部通过**：

- `preview_state` 读到相机与出图接口
- `preview_plan` 出包围盒与视角列表
- 取景范围未被 y 哨兵（-65536）污染
- `preview_layout` 生成平面图 PNG
- `preview_shot` 引擎渲染落盘 PNG
- `preview_shot` 后相机已还原
- 整套预览流程未改动编辑器相机

相机还原精度：`position/forward` 偏差 0，`up` 偏差 1e-4。

### 8.5 平面图（路线 C）落点

1099 行的绘制逻辑全部在主机侧，游戏进程只被读一次数据：

- 坐标网格（自适应步长，标注 X/Z 值）
- 地图边界框（取自边界锚点）
- 每个空间物件：**旋转方块 + 朝向指示线 + `#ugc_id 名称`**（尺寸按 scale，
  但用屏幕像素定标 —— 地图尺度与道具尺度差两个数量级，按世界单位画会看不见）
- 右侧明细：物件列表（坐标/旋转/缩放）+ 非空间组件清单
- 画布长宽比自适应内容，避免大片留白

> 引擎不提供可靠模型包围盒（`bounding_info` 实测为 `None`、`col_radius` 为 `None`），
> 所以平面图是**标记式**而非真实占位。要做真实占位需离线解析 `model_path` 的模型文件。

### 8.6 仍待优化

1. 截图自带编辑器叠加层（坐标网格线、半透明角色占位）。可选 `render.save_screen_to_file_ex`
   用 uv 裁边，或探索 UI 层开关。
2. 单张正俯视在整图 `fit=map/union` 下物件只有几像素 —— 这是 2560:4 的尺度比决定的，
   需靠 `fit=content` 或 `dist_mul` 拉近。
3. 平面图字体依赖 `C:\Windows\Fonts\msyh.ttc`，非 Windows 环境会退化。

### 8.7 附带发现（与预览无关，但值得记）

新会话中 `fuxiang_catalog_verify` 的**组件类型可达数会低于 105**（本次实测 38/105，
不可达原因全部是「已登记但无实现类」）。因为该判据依赖进程内**已导入**的
`UGC*Component` 实现类，而实现类是游戏按需 import 的：刚重启、没做过创建/摆放时，
大多数实现类尚未加载。**物件侧 3963/3963 不受影响。**
`tests/mcp_interface_check.py` 已加打印说明此分布。
