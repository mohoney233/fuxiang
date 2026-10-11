# 几何拼图可复用搭建方案

适用：在《第五人格》随笔浮想编辑器内，把一张图片转换为可保存的几何色块拼图。使用现有 `tools/fuxiang` 桥接；所有构件必须服务端落位。本文基于 [结城理工程](../../projects/jiechengli_mosaic/README.md) 的实际完成记录，整理于 2026-09-24。用户已确认该工程持久化成功；这是用户反馈，并非本次重新查询游戏所得。

入口：[给下一位智能体的提示词](AGENT_PROMPT.md)、[配置样例](project.config.example.json)、[检查点样例](checkpoint.example.json)。两个 JSON 是设计模板，**尚未接入通用执行器，不能直接执行**。

## 1. 已验证的路线与适用边界

原图 → 自适应矩形分块 → 实机灰阶校色 → 小批试拼 → 串行服务端摆放 → 上色 → 每 50 件保存 → 正交截图对比 → 用户退出重进验证。

优先选 301001 方块，把它压成薄片。正面无缝排列时，几何形状简单、坐标易计算、颜色可追溯。本次没有使用图片墙、线条或字符来充当图像；203013 图片墙不受服务端支持。图像复杂度集中在颜色和轮廓，自适应矩形能把件数集中给眼睛、头发等高细节区域。

这里的还原是保持画幅比例和构图的几何近似，不是原图每个像素都放一件，也不是无损复制。采样网格与物件数量是两个独立参数。

| 本次参数 | 实际结果 |
|---|---|
| 原图 | 3845 × 2160 |
| 采样网格 / 件数 | 1024 × 576 / 14,500 个自适应矩形 |
| 世界画幅 / 厚度 | 384.5 × 216 / 0.1 |
| 布局 | XY 平面，x −192.25…192.25，y 10…226，z 中心 0 |
| 构件 / 材质 | 301001；默认运行时材质 h55_ugc_geometry_box01 |
| 保存 | 每 50 件；颜色写入每次最多 20 件 |
| 正式组件计数 | map_info：113 → 14,613；GEOMETRY：14,500 |
| 编辑器计数 / 占用 | 103 / 193 → 14,603 / 14,693 |
| 引擎对原图误差 | RGB MAE 5.73/255；PSNR 29.01 dB |
| 保存与持久化 | 两个 dirty 缓冲均为 0；用户已确认持久化成功 |

主批次在首批 50 件之后处理 14,450 件，约 94 分钟；不含前期试材质、校色和方案准备。下一张图耗时应以其小批实测估算。

下次建议首批 20–50 件验图，稳定后约每 1000 件、关键区域完成或参数变化时验图。**1000 是截图间隔，不是保存间隔**。本次留存了首批与最终截图，不应把建议节奏写成已经执行的历史事实。

## 2. 开工：新工程、新会话、新基线

先只读检查工作区说明与当前工具实现。新建 `projects/<项目名>/`，保留原图、配置、计划、回执、证据。不要覆盖旧工程，也不要直接运行旧工程的 host.py。

PowerShell 环境与初始检查依次执行：

```powershell
$PY = '<你的-python.exe-完整路径>'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location '<工具根>'
& $PY cli\fxcall.py ping
& $PY cli\fxedit.py status '{}'
& $PY cli\fxedit.py map_info '{}'
& $PY cli\fxedit.py edit_targets '{"limit":0}'
& $PY cli\fxshot.py sweep map
```

逐条检查回执，不能无条件连跑。记录 pid、editor_core、server_session.connected、anchor_info、total_components、type_distribution、编辑器组件数与占用。要求 connected=true，editor_core 非空且 component_manager=true。空编辑器需用户先进入；失联按第 10 节停手。ping 不通时仅按现有安装说明重注入一次，仍不通则停手。

地图自带天空、雾、LUT、时间、出生点、锚点和 UI 等全局组件不动。当前容量、可建造边界、地面和机位都重新读取，不能沿用本次的 113、103/193 或历史会话字符串。非空地图必须区分现有内容和新工程对象。

每批写入前再次 status，比较 pid 与 editor_core；任一变化立即停手。用户退出重进后旧 ugc_id 会全部失效，旧进度只作历史证据。

## 3. 选件与校色：先确认材质，再拟合颜色

工具分工：

```powershell
& $PY cli\fxedit.py catalog_items '{"component_type":100001,"limit":0}'
& $PY cli\fxedit.py catalog_schema '{"component_type":100001}'
```

`catalog_schema` 是 CLI 的正确 op，描述属性面板；不能用 MCP 命令名替代。运行时材质用 `material_info`，参数是本会话已落位的非空 ugc_ids 数组。尺寸和枢轴查 `registry/model_sizes.json`，特殊形状可参考 `cli/place_exact.py`。

先用 20 个灰阶薄片做校色板，串行服务端放置，记录 ugc_id 和 owner_uid。建议输入级别：0、12、24、36、48、60、72、84、96、108、120、132、144、156、168、180、198、216、236、255。上色、保存后固定机位截图，采样色块内部，避开边缘。

颜色唯一写入渠道：属性 `10000110006`，值为 `[["RRGGBB","RRGGBB","RRGGBB","RRGGBB"]]`，不带 #。纯图像关闭碰撞：`10000102001: [0]`。只操作 place_poll=placed 的已登记对象。

如果尝试材质，`10000110005` 必须传路径字符串数组，不能传数字材质 ID。先用 material_info 对比属性袋与运行时，再看实机截图。本次试过的发光材质路径实际落到其他材质并出现紫色渐变，已弃用；另一个材质名称有 mismatch，但视觉与默认材质相同。因此名称 mismatch 是排查线索，不能单独证明效果失败或成功。

最终采用默认材质，不给正式件写显式材质路径。对每个通道建立“写入灰阶 → 截图输出值”的曲线，目标颜色经反向分段线性插值生成实际写入 RGB。新拟合需检查单调性、重复采样值和端点裁切；超出输出范围的目标只能裁切，不能宣称完全还原。本次白色最多约为 (224,219,221)，仅适用于当时地图与机位，不能作为全局固定 LUT。

测试件用显式 ID 调 delete_preview，再 delete_objects，单批不超过 50。核对删除前后的组件/占用计数以及 owner_uid，保存删除并确认 dirty 归零，建立正式新基线。delete_preview 不接受空数组来查询整图；整图核对使用 map_info / save_dirty / edit_history。

`dropped_uids` 对应 owner_uid，不应直接与 ugc_id 比较。只有逐项核对确实是本次主动删除件，才能放行该 dropped_count。索引可能延迟清理；本次曾出现索引仍为 133，但权威组件数/占用已回到 103/193。此案例不构成以后忽略数量差异的许可。

## 4. 离线分块与坐标公式

推荐用 Pillow + numpy，保持原图比例，先缩到采样网格。初始化若干矩形，对每块计算加权 RGB 平方误差，尝试水平/垂直分割；用优先队列反复选择误差下降最大的分割，直到达到件数预算。高权重区域保留更细节；本次面部/头发权重 2、眼睛 4、嘴部 3，其坐标只适合本图。换图必须重新划定重点区域。

颜色取块内加权均值；目标 RGB 和校色后的写入 HEX 均保存。限制过长窄条，保证最小一格与最大世界尺寸。件数预算需以当前地图容量、已有组件及预留余量决定，不能无条件用 14,500。

设网格宽高 Gx/Gy，世界画幅 W/H，左边界 xmin、底边 ybase、薄片中心 zc、厚度 d。图像坐标向下，矩形为半开区间 `[x0,y0,x1,y1]`：

```text
dx = W / Gx; dy = H / Gy
position = [xmin + (x0+x1)*dx/2, ybase + (Gy-y1)*dy, zc]
rotation = [0,0,0]
scale = [(x1-x0)*dx/10, (y1-y0)*dy/10, d/10]
```

301001 原生 10×10×10，枢轴在底面中心，所以 position.y 是块底边；不能再加半块高。世界 H=W×原图高/原图宽，保证原始画幅比例；网格因整数取整可有极小比例差。

单块目标尺寸建议 ≤100（scale ≤10），过大可能发生只渲染一半的缺陷。本次最小 scale=0.01；新工程需重新核对属性限制，避免网格太密导致小于允许缩放。

离线检查：每个网格位置恰好覆盖一次、无空洞/重叠、所有 mode=server、坐标和缩放合法、件数满足容量、颜色格式合法。先输出 planned.png 和离线并排图，检查脸部与轮廓再冻结完整计划。

plan.json 每件至少保存 item_id、position、rotation、scale、tag、mode，同时保存 grid_rect、target_rgb、color 以便工程追溯。manifest.json 保留相同几何与颜色，并补充实际 placed 回执映射。

## 5. 正式执行：build_batch + 工程侧护栏

先 dry-run，不写引擎：

```powershell
# $plan 为新工程完整 plan.json 的绝对路径
& $PY cli\build_batch.py $plan --dry-run --stop-on-error
```

批量摆放仍使用 build_batch，带 `--batch 50 --stop-on-error`。但不能只运行裸命令就认为完整任务已完成：当前工具会规范化条目，**不会自动执行 plan 里的 color 字段**。需参照本次工程 host.py 在工程侧组织上色、严格保存检查和故障退出；不得修改 cli/、mcp/ 或桥接核心脚本。实现前只读核对当前版本，避免照搬过期行为。

每批必须按以下顺序串行执行：

1. status 核对会话与 connected；确认无上一笔未收尾的保存请求。
2. build_batch 逐件 place_item，随即 place_poll 同 request_id，直到 placed；始终仅一笔在途摆放。
3. 写进度中的 i/tag/request_id/ugc_id/owner_uid（可取得时），并记录原始回执。
4. 本批至多 50 件全部落位后，分组最多 20 件，用 eval 循环调用 FE.op_edit_props 上色及关闭碰撞。逐项检查 ok=true，任一失败停手。
5. 保存前核对 map_info 与类型计数相对基线的增量；再 save_map，记录 request_id，轮询该笔 save_map_poll 并检查 save_dirty。
6. 必须在 180 秒内确认 change_units_length 与 save_events_length 都为 0，核对 dropped_uids 和保存后数量。记录保存检查点，才进入下一批。
7. 到截图节点时，先确认已经保存、无未处理写入，再截图检查。

主机等待可以短暂停顿；禁止在游戏主线程 eval 内 sleep 或轮询。推荐工程脚本通过 `from fxlong import call_edit` 调用并保留回执。

保存回执 timeout 且两个 dirty 缓冲为 0，按已上送处理，不能误说服务端已明确确认回执。上一笔保存观察请求仍在途时继续收尾，不能发第二笔；没有在途时也不要强行 poll 已经结束的请求。保存超时且 dirty 不归零，执行停手规则。

### 当前 build_batch 的重要陷阱

- `--limit` 在计算 run_id 前截断清单。先 `--limit 50` 再跑完整计划会产生不同 run_id，可能重摆前 50 件。试跑应使用同一完整计划、同一进度，在已保存的首批检查点主动退出；或使用互不重叠的独立批次清单并维护映射。本次采用前者。
- run_id 的规范化内容不含颜色。另存原图、配置和完整计划的 SHA256，防止几何不变但颜色改了仍被当作同一任务。
- placed 进度仅证明落位，不能证明已上色或已保存。续作必须核对颜色日志和保存检查点，不能盲目跳过全部 placed 条目。
- 当前原始工具把 send_unknown 列在 BAD 中并设轮询次数上限；用户要求它与 pending/overdue 一样继续轮询原 request_id。项目侧编排必须落实这一语义，不能生成新请求替代。
- 当前原始工具可能捕获异常后执行尾部保存，这与“硬故障后不再保存”冲突。本次工程在调用边界记录原错后用终止路径退出整个任务，确保不会落入尾部保存。只是防止错误后的额外写入，不是绕过错误继续工作。
- 退出码 0 不等于上色和保存都完成。必须检查原始回执、dirty、数量和项目检查点。

本次 host.py 的 `FUXIANG_STOP_AFTER` 是**工程自有变量**，不是工具链通用开关：按本次进程新摆数量，在整批保存后退出。下一位智能体可以借鉴此设计，但需为新项目适配和验证。

## 6. 断点、重进与修改

同会话续作：校验会话、完整计划哈希、在途请求和最近已保存检查点；优先收尾原请求。只有已核实的 saved 批次可直接跳过，placed 未 saved 的情况要保留现场并按实际回执处理。不要用 `--new-run` 解决普通续作。

重进后续作：旧 ID 与会话不能再执行。建立新会话档案，按 item_id、position、rotation、scale 匹配重新登记映射，必要时用颜色协助消歧；匹配不唯一不能凭旧序号继续。没有必要修改时，保留旧工程作为证据即可。

服务端对象就地 edit_transform 不保证重进后保持。确需位置修正使用 persist_via_recreate=true，记录新 ID 并重新 place_poll；遵守单笔在途与逐批保存。删除只对明确归属本工程的 ID，不动全局组件。末次保存后不再增加未保存物件。

## 7. 截图验收：机位、比例与实际观看

出图前保存。shot 回执 sys_avail_mb 必须 ≥1024；失败、持续不出图或内存不足按第 10 节停止。初始空图使用 fit=map，有内容后可 sweep content，正式图像比较必须使用自定义正交机位。

在主机脚本加载 `mcp/preview.py`，调用 `P.shot(call_edit, {"view": view, "out": 输出绝对路径, "restore": True})`。相机位于画布中心前方，forward=[0,0,1]，up=[0,1,0]，mode=ortho；近远裁剪覆盖画布。

设实际截图视口宽高比 A，画布世界宽高 W/H：

```text
ortho_width  = max(W, H*A)
ortho_height = max(H, W/A)
camera_center = [xmin+W/2, ybase+H/2, zc-distance]
```

这会保留完整画布并产生必要留白。按世界画幅投影计算裁切边界，保留原始 PNG 和裁切 PNG，禁止直接拉伸整张截图冒充比例对齐。本次视口 1920×1200，正交 384.5×240.3125，相机 [0,118,-200]；上下边距约 60.702 像素。换分辨率必须重新计算。

将原图与画布裁切用同一尺寸比较，输出并排图、差异图，实际打开观看。检查整图是否有孔洞/重复条带，以及眼睛、嘴部、发丝、衣服边缘等重点。分开记录“原图→计划”的分块损失和“计划→引擎”的渲染误差，不能把 MAE/PSNR 换算成所谓百分比还原度。

本次已知偏差：青白高光略暗，微小雨滴和织物纹理简化，背景渐变呈块状，发丝斜线有台阶。下一次应结合图像重点和预算选择更细划分，不承诺增加件数能解决材质亮度上限。

## 8. 工程交付与验收门槛

推荐每个新工程至少包含：

| 文件 | 内容 |
|---|---|
| project.config.json、baseline.json | 输入参数、工具版本/哈希、会话、范围、各计数器基线 |
| calibration.json | 灰阶输入/实测 RGB、材质、机位、拟合规则 |
| plan.json、manifest.json | 每块格位、几何、目标/写入色；manifest 补齐实际映射 |
| plan.progress.jsonl、audit.jsonl | placed、颜色、请求和逐批保存的原始记录 |
| checkpoint.json | 已核实保存批次、计划哈希、在途请求、会话标识 |
| planned.png、front.png、front_canvas.png | 计划预览、原始引擎图、准确裁切 |
| comparison.jpg、difference_x4.png、verification.json | 对比图、差异倍率说明、误差与目视结论 |
| final_save_evidence.json | 最终 map_info、类型分布、save_dirty、保存请求 |
| README.md、persistence_confirmation.json | 件数、参数、偏差、复现方式、用户验证来源 |

完工前同时满足：全部对象 placed 且唯一；全部上色成功；几何覆盖无缺失；基线+新增−删除与各自计数器一致；两个 dirty 为 0；真实引擎截图已经打开检查；全部工程证据齐全。最后请用户游戏内再保存、退出重进确认，收到反馈后追加确认记录，保留原始保存证据不改写。

## 9. 如何利用这次工程

[完整计划](../../projects/jiechengli_mosaic/plan.json) 和 [manifest](../../projects/jiechengli_mosaic/manifest.json) 可直接作为同图复建的离线几何/颜色输入。新地图仍须新建工程、重新校验容量/会话/颜色，生成新的运行标识及空进度；绝不能把历史 ugc_id 用于新地图，也不能把旧 progress 复制过去作为已完成进度。

| 历史脚本 | 可借鉴内容 | 新任务必须替换/核对 |
|---|---|---|
| prepare.py | 自适应分块、坐标换算 | 图片、画幅、网格、预算、ROI、校色、工程路径 |
| calibrate.py | 校色板与采样 | 测试件 ID、材质、机位、采样位置 |
| host.py | build_batch 编排、上色/保存护栏 | 路径、会话基线、删除归属、计数器、检查点恢复 |
| evidence.py | 正交图裁切与比较 | 当前视口、画布、机位、目标件数 |
| finish.py | 汇总审计与证据 | 件数、会话、进度、输出路径 |

这些是本次项目脚本，不是可任意复制运行的通用程序。尤其 host.py 中 114–133 的已删除测试 ID、初始会话和本图计数器仅是历史事实。

## 10. 硬故障：保持现场并交接维修

以下情况立即停止，不再保存、删除、退出或重启游戏，不改工具链脚本，不用 eval 绕过错误，不反复重试失败命令（同一 request_id 最多重试一次）：

- 工具链脚本 Traceback、异常类 ok:false、桥接未运行/等待结果超时、HTTP 5xx；桥心跳异常且按规定重注入一次后仍不通。
- server_session.connected 不为 true，或 pid/editor_core 与本次基线不同。
- place_poll 为 ambiguous、conflict、finalize_failed。
- 保存超过 180 秒未能按规则确认上送，或 dirty 在 180 秒内未归零。
- 截图工具崩溃、持续不出图，或 sys_avail_mb <1024。
- 批量上色任一项 ok 不为 true。

pending/send_unknown/overdue 继续轮询同一个摆放请求；普通图片效果不佳允许迭代。环境候选路径不存在、控制台中文乱码不代表桥接坏了。save_map_poll timeout 且双 dirty 为 0 按已上送处理。当前用户明确放行过错误 op 名、空目标删除预览、已核实的删除 dropped 差异；这不豁免新的未知异常。

不循环删除 ipc 下回执；需整理时使用 cli/fxsweep.py 归档。报错交接五项必须完整，错误原文不翻译、不概括：

```text
【报错阶段】开工自检 / 摆放 / 上色 / 保存 / 出图 / 其他
【报错工具/命令】完整命令行，或脚本名 + 行号
【错误原文】原样粘贴
【已完成清单】已摆件数、已上色件数、关键 ugc_id、最后成功 request_id
【现场状态】是否保存过、当前地图计数器、是否有在途请求
```

若未来使用阀门外类型，只有客户端 plan_item.server_begin_allowed=false 不等于服务端拒绝，可依原用户规定临时扩展 SERVER_COMPONENT_TYPES 并立即还原；一条 eval 只发一笔，之后普通 place_poll 确认。本文默认 301001 已在阀门内，不需要该操作，也不尝试 203013。
