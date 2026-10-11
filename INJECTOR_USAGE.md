# 「随笔浮想」桥接注入器使用手册（fuxiang-inject）

> 本文档面向《第五人格》「随笔浮想」编辑器桥接注入器：**如何安装桥、如何在新游戏版本更新后重新生成导出表、如何在不再手动提权的情况下复用桥**。
> 配套：桥接脚本见 `bridge/fuxiang_bridge.py`；MCP/HTTP 调用见 [MCP_USAGE.md](MCP_USAGE.md) / [API.md](API.md)。

---

## 一、原理一句话

`fuxiang-inject` 把一段 Python 引导代码注入 `dwrg.exe`：游戏主引擎 `neox_engine.dll` 导出了完整的 CPython C-API，注入器用
`PyGILState_Ensure → PyRun_SimpleString → PyGILState_Release` 三个导出函数在游戏进程内执行 `fuxiang_bridge.py`，从而在游戏主线程里启动一个常驻桥接。

关键依赖：**`research/tools_extract/neox_exports.txt` 里的函数 RVA（相对虚拟地址）必须与当前 `neox_engine.dll` 一致**。
如果引擎更新而导出表没跟着更新，注入器会算出错误的函数地址 → 一注入就 `0xc0000005`（访问违规）→ 游戏闪退。

**防崩闸门（2026-10-03 起，install / raw 注入前自动执行）**：注入器先把导出表与**磁盘上的 DLL** 的 PE 导出目录逐项核对
（离线只读，不碰游戏进程）。关键 3 函数（`PyGILState_Ensure` / `PyRun_SimpleString` / `PyGILState_Release`）缺一或
RVA 失配 ⇒ **拒绝注入**（退出码 9），把"引擎更新后一装就闪退"变成"干净拒绝 + 指路重建导出表"。为什么这么较真：
崩溃本身比闪退更贵——原生崩溃若触发游戏的崩溃上报，dump 里的进程内存会把桥的模块名/路径一起带上去
（见「防崩与残留边界」一节）。非关键条目漂移只给 ⚠️ 告警不拦截（不影响注入安全的地址已核对过）。

```text
fuxiang-inject.exe install
  → 校验导出表 vs 当前 neox_engine.dll（关键 3 项失配 = 拒绝，退出码 9）
  → OpenProcess(dwrg) → VirtualAllocEx 两份内存（代码 + stub）
  → CreateRemoteThread → PyGILState_Ensure / PyRun_SimpleString / PyGILState_Release
  → 执行 bridge/fuxiang_bridge.py → 桥接线程接管 ipc/ 目录
  → 线程返回后**立即释放**两份远程内存（正常路径不留原生残留）
```

---

## 二、常规安装桥（每次手动提权）

```powershell
# 前置：游戏已启动并进入「随笔浮想」编辑器。
# 以管理员身份运行（会弹一次 UAC）：
& '<工具根>\injector\bin\Debug\net8.0-windows\fuxiang-inject.exe' install
```

成功标志：退出码 0，输出 `注入完成: ok`、`桥接已就绪 ✓`；同时 `ipc/bridge_state.json` 的 `pid` **等于当前 dwrg 进程 pid**、`age` 很小、`alive=true`；`ipc/bridge.log` 出现一行 `bridge installed v0.1.0 pid=<pid> (threads on main)`。

> 注：`(threads on main)` 表示桥接线程改到游戏主线程启动——这是崩溃修复的一部分，避免在注入线程上创建 CPython 线程导致闪退。

---

## 三、装桥入口脚本：直接安装 + 按需提权（推荐；计划任务已退役）

> **2026-10-03：计划任务方案已删除。** 实测定案：装桥必须提权（Medium 完整性下 `OpenProcess` 报错 5，
> 见 `builds/_medium_test.log`），所以任务想省的 UAC 实际省不掉；而 `schtasks /Create` 的默认设置带
> `DisallowStartIfOnBatteries=True` —— **电池供电下触发永远静默停在 Queued**（"从来没生效"的根因），
> 加上 `MultipleInstances=IgnoreNew`，卡住的实例还会永久堵住后续触发。结论：与其让提权**不可见地**排队，
> 不如让它**可见地**只在该发生时发生。

提供现成脚本：[`fuxiang_bridge_install.ps1`](fuxiang_bridge_install.ps1)。

```powershell
# 提权终端里跑 = 全程无提示；普通终端里跑 = 仅在 OpenProcess 被拒时弹一次 UAC
powershell -NoProfile -ExecutionPolicy Bypass -File '<工具根>\fuxiang_bridge_install.ps1'
```

脚本行为：
1. 直接运行 `fuxiang-inject.exe install`（新注入器自带导出表防崩闸门，闸门拒绝 = 退出码 9，脚本直接给重建导出表的指引）。
2. 仅当注入器报 `OpenProcess 失败`（退出码 5，即当前终端权限不够）→ 自提权重跑一次（**一次 UAC**），输出落 `ipc/_install_elevated.log`。
3. 判收不看 exit code 一个数字：`bridge_state.json` 的 `pid` 必须等于**当前** dwrg 进程 pid、心跳 ≤5s —— 打印 `bridge ready OK`（exit 0）。
   （旧脚本在这里的显示有坑：把 3970s 的陈旧心跳印成 `alive=True`。）

等价的手动命令（供了解/自行管控）：

```text
# 提权终端（或弹一次 UAC 后）：
fuxiang-inject.exe install
```

> 说明：装桥发生在**每次游戏重启后**（桥随进程消失），所以普通终端下每局一次 UAC 点击是这条路的全部成本；
> 在提权终端里工作则零提示。曾考虑修复计划任务的电池限制（`Set-ScheduledTaskSettings`），
> 但"静默排队 + 失败不可见"正是原方案的核心缺陷，故直接退役。

---

## 四、游戏版本更新 / 替换引擎后：重新生成导出表（重点）

`neox_engine.dll` 一旦被更新或替换，导出表 `research/tools_extract/neox_exports.txt` 必须重新生成，否则装桥必闪退（详见第一节）。

### 4.1 什么时候要做

- 游戏大版本更新后；
- 替换 / 回滚了 `neox_engine.dll`（例如恢复 `neox_engine.dll.bak`）；
- 首次遇到"一装桥就 `0xc0000005` 闪退"。

### 4.2 重新生成（三条路线，按需选一）

**方式 A：图形控制台一键（推荐，无需 Node.js）**
桥接控制台首页 → 展开「详细信息」→ 点 **「重建导出表（游戏更新后）」**。
自动探测运行中游戏的 `neox_engine.dll`（探测不到可手动选文件）→ 重建 → 自动用注入器核对，全程看任务日志。旧表自动备份 `.bak`。

**方式 B：CLI 脚本（无需 Node.js）**

```powershell
& '<python>' '<工具根>\cli\fxexports.py' build '<游戏根>\neox_engine.dll'   # 重建（旧表自动备份 .bak）
& '<python>' '<工具根>\cli\fxexports.py' verify '<游戏根>\neox_engine.dll'  # 核对（关键 3 项失配 = 退出码 9）
```

`cli/fxexports.py` 是纯标准库实现，与方式 C 的 Node 脚本、注入器闸门三方解析逻辑与判定口径一致。

**方式 C：Node 脚本（原有路线）**

```powershell
cd '<工具根>\research\tools_extract'
node --version          # 需要 Node.js
# 旧表会自动/手动备份（建议保留，便于对照）
Copy-Item neox_exports.txt neox_exports.txt.bak -Force
# 用当前 neox_engine.dll 重新生成
node pe_exports.js '<游戏根>/neox_engine.dll' neox_exports.txt
```

### 4.3 核对关键函数 RVA 已变化且与引擎一致

```powershell
# 期望看到三行，且 PyGILState_Ensure / PyRun_SimpleString / PyGILState_Release 都是非 MISSING：
node pe_exports.js '<游戏根>/neox_engine.dll'
# 或用 CLI 核对（纯标准库，退出码与注入器闸门同口径：0=通过 / 9=拒绝）：
& '<python>' '<工具根>\cli\fxexports.py' verify '<游戏根>\neox_engine.dll'
# 或用注入器离线核对（不碰游戏，全表 6600+ 项逐条比；DLL 路径必须显式给出，
# 或设 FUXIANG_GAME_DIR；install 时会自动取目标进程模块路径，无需设置）：
& '...\fuxiang-inject.exe' verify-exports '<游戏根>\neox_engine.dll'
# 也可自定路径做离线测试（如用篡改过的表验证闸门）：
& '...\fuxiang-inject.exe' verify-exports '<游戏根>\neox_engine.dll' '别的导出表.txt'
```

判断是否成功：`verify-exports` 打印 `结论：关键导出全部匹配，可以安全注入 ✓`（退出码 0）。
`install` / `raw` 现在会**自动先跑这道闸**：关键项失配时输出 `⛔ 导出表校验失败，拒绝注入` 并退出码 9，
而不是像旧版那样直接把游戏搞崩。

### 4.4 举例（本次修复对照）

| 函数 | 旧表(9/16) | 当前引擎(9/17) | 说明 |
|---|---|---|---|
| `PyGILState_Ensure` | `0x3aa8620` | `0x3a185e0` | 已变 |
| `PyRun_SimpleString` | `0x3ac86e0` | `0x3a386a0` | 已变，旧表会闪退 |
| `PyGILState_Release` | `0x3aa86e0` | `0x3a186a0` | 已变 |

引擎更新但未重建导出表时，注入线程退出码为 `0xc0000005`（访问违规），游戏随之闪退——本次正是因此。
（2026-10-03 起：`install` / `raw` 会先被防崩闸门拦下，见第一节；表 4.4 这种"旧表对上新引擎"的情况**不会再走到注入那一步**。）

---

## 4.5 防崩与残留边界（2026-10-03 审计后加固）

注入链路的"被取证暴露面"分析与现状，改了什么、还剩什么：

**已消除的两处：**

1. **RVA 过期 = 必崩 → 现在是干净拒绝**。崩溃的代价不只是闪退：原生崩溃若触发游戏自带的崩溃上报，
   dump 里的进程内存会把 `fuxiang_*` 模块名、路径字符串一起带给厂商。防崩闸门（`verify-exports`，
   install/raw 自动执行）把这个最常见的崩溃源在 `OpenProcess` 之前掐掉。
2. **注入等待超时不再拆活线程脚下的内存**。旧实现 `WaitForSingleObject` 超时后仍会 `VirtualFreeEx`
   两块远程内存——若线程只是卡得久（还在 `PyRun_SimpleString` 里），这就是自己制造的访问违规。
   现在超时路径**不释放**（泄漏两小页，重启游戏即回收），只在确认线程已退出后才释放。

**正常路径本就不留原生残留**：stub 与代码两块远程内存在注入线程返回后立即释放；不加载任何 DLL；
桥的轮询/心跳线程跑在引擎自带 CPython 的线程上。**已知残留边界**（暂不处理，成本高收益低）：
桥以 Python 模块常驻（`fuxiang_bridge` / `fuxiang_edit` / … 的代码对象、`__file__` 路径、文档字符串），
一次原生崩溃的 dump 或一次深度内存扫描仍能看到它们——所以防崩（闸门 + 桥内 `BaseException`
全兜底 + eval 守护）才是这条线上最有效的事。

---

## 五、故障排查速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 一装桥游戏就闪退，注入 `exit=0xc0000005` | 导出表 RVA 与当前引擎不符 | 按第四节重建 `neox_exports.txt`（新版会先被闸门拦下，退出码 9） |
| `⛔ 导出表校验失败，拒绝注入`（退出码 9） | 引擎更新后表未重建，或表文件损坏 | 这是在**保护你**：按第四节重建导出表后再装 |
| 计划任务 `FuxiangBridgeInstall` 触发后停在 Queued（电池供电下**永不启动**，`DisallowStartIfOnBatteries` 默认开；2026-10-03 实测） | `schtasks` 创建计划任务的默认电池限制 + `MultipleInstances=IgnoreNew` 卡死实例 | **已退役**：任务已删除，改用 `fuxiang_bridge_install.ps1`（直接安装 + 按需 UAC，见第三节） |
| `未找到运行中的 dwrg.exe` | 非管理员读不到目标模块 / 游戏未运行 | 用第二节或第三节的提权方式；确认真实进程已加载 `neox_engine.dll` |
| `注入完成: ok (tid=…, exit=0xc0000005)` | 同上（RVA 错） | 重建导出表 |
| `桥接未上报心跳` | 注入后桥线程未拉起 / 心跳写入失败 | 看 `ipc/_bootstrap_err.txt`、`ipc/bridge.log` |
| 心跳 `pid` 不是当前游戏 pid | 装桥时游戏已被替换 / 未针对当前进程装 | 重进编辑器后再装一次 |
| 重注入后桥仍在**旧路径**工作（心跳/命令目录不跟随新位置） | bootstrap 的重注入分支不刷新 `__file__`，模块内部按它推导全部路径（2026-10-04 已修：两个分支都刷新） | 重新构建注入器后再 install；编辑层子模块会随下一次 eval 自动从新路径重载 |
| 间歇性 `Invalid camera` 等序列化报错 | 某 op 返回了已销毁的引擎对象 | 已捕获不闪退；避免 `fuxiang_eval` 直接返回引擎对象 |

---

## 六、关于"完全免提权"注入（探索性）

能否完全不提权注入取决于游戏进程的完整性级别：

- 若游戏以**普通（Medium）完整性、同用户**运行，且未被保护，那么以 Medium 注入器 `OpenProcess` 并
  `WriteProcessMemory`/`CreateRemoteThread` 理论上是允许的（多数单机注入器即如此）。
  当前注入器之所以需要管理员，主要因为它：
  1. 用 `.Modules` 枚举 `neox_engine.dll` 基址（跨进程读模块常需更高权限）；
  2. 请求了 `PROCESS_ALL_ACCESS`，且没有降级到最小权限；
  3. 未利用 `NtQueryInformationProcess`（仅需 `PROCESS_QUERY_LIMITED_INFORMATION`）通过 PEB 读镜像基址。
- 若要真正"免提权注入"，需改造注入器：改为**最小权限句柄 + PEB 取基址 + 失败自动回退**，然后**重新编译** `fuxiang-inject`（`dotnet build`）并针对实际游戏进程完整性实测。

> 结论：**当下最稳的免重复提权方案是第三节的计划任务**（提权一次、之后静默）；"完全免提权"是可行的优化方向，但需改代码+重编译+实测，尚未落地。

---

## 七、相关路径速览

> **2026-10-03 迁移 / 10-04 位置无关化**：工具链与研究目录已整体移出游戏安装目录，现位于
> `<工具根>`。**工具根自动推导**：从注入器 exe 自身位置向上找
> `bridge/fuxiang_bridge.py`（`FUXIANG_TOOL_ROOT` 可覆盖）；install 时引擎 DLL 路径取自
> **目标进程的实际模块**（`FUXIANG_GAME_DIR` 只作离线 verify-exports/selftest 兜底）——
> 整树搬到任何目录、或游戏换盘重装，都无需改代码。下表「工具根」= 当前实际位置，「游戏根」=
> `<游戏根>`。历史文档里的旧绝对路径一律读作 `<工具根>\...`。

| 项 | 路径 |
|---|---|
| 注入器 | `<工具根>/injector/bin/Debug/net8.0-windows/fuxiang-inject.exe` |
| 注入器源码 | `<工具根>/injector/Program.cs` |
| 桥接脚本 | `<工具根>/bridge/fuxiang_bridge.py` |
| 导出表 | `<工具根>/research/tools_extract/neox_exports.txt` |
| 导出表生成器 | `<工具根>/research/tools_extract/pe_exports.js`（Node 版）或 `<工具根>/cli/fxexports.py`（标准库版，控制台按钮也走它） |
| 引擎 | `<游戏根>/neox_engine.dll`（勿忘与导出表同步） |
| 桥状态 | `<工具根>/ipc/bridge_state.json` |
| 桥日志 | `tools/fuxiang/ipc/bridge.log` |