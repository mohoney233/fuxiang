# 随笔浮想 · 桥接控制台

《第五人格》UGC 编辑器「随笔浮想」的**自制辅助工具**：一个 WinUI 3 桌面控制台，
把编辑器的备份恢复、剧情、事件、队伍、封面、截图等常用操作做成图形界面，
通过文件级 IPC 与游戏内桥接通信，面向不熟悉命令行的创作者。

> ⚠️ 启动时会弹出「使用须知」风险确认；工具为注入式实现，使用带来的账号风险由使用者自行承担。

## 功能一览

| 页面 | 能力 |
| --- | --- |
| **首页** | 桥接状态三态引导（游戏未开 / 需重连 / 一切就绪）；快捷按钮：保存地图、备份地图、拍多角度截图、更换封面；当前地图概览；多桥接（多开游戏）切换；「详细信息」折叠区内含「安装 / 重注入桥接」与「重建导出表（游戏更新后）」 |
| **备份与恢复** | 备份 = 完整存档（不动地图）；恢复走「预览改动（安全）→ 确认执行」两步，支持断点续做 |
| **剧情** | 剧情树浏览；导出 / 导入（默认预览，确认后才写入）；节点编辑收在折叠区 |
| **事件** | 事件导出 / 导入（默认预览）；DSL 双向转换；物件标签生成与检查 |
| **队伍** | 队伍配置人话呈现；添加队员槽位（创建即带坐标）；删除槽位 |
| **封面** | 选图 → 自动裁 16:9 压缩 → 上传应用；服务器回读确认 |
| **截图** | 俯视图 / 多角度 5 张 / 平面图；图库与大图预览 |
| **AI 提示词** | 25 条面向 AI 助手的提示词库（按场景分组折叠、一键复制），含教 AI 认识工具链、安全操作规约等 |
| **关于** | 作者信息与使用声明（个人学习研究制作、禁止商用、不得付费倒卖） |
| **高级（开发者）** | 任意 edit op 调用、eval 逃生舱、71 条命令手册（按功能域分组折叠，点击自动填入） |

所有会改动地图的操作完成后都会弹「别忘了保存地图」提醒，可一键走真实保存链路。

## 环境要求

- Windows 10 19041 及以上（x64）
- [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)（仅构建时需要；分发包内的注入器与控制台均已自包含，运行时无需安装）
- Python 3.8+（在 `PATH` 中即可；也可在 `console/settings.json` 的 `"Python"` 字段填完整路径）
- 游戏内桥接组件（源码在本仓库 `bridge/` 与 `injector/`，见下文「游戏内桥接与注入器（研究）」）

## 构建与运行

双击仓库根目录的 `启动控制台.cmd`：缺构建产物会自动 `dotnet build`，然后拉起 exe。
需要装桥 / 重注入的权限说明见根目录《请以管理员身份启动控制台.txt》。

手动构建：

```powershell
dotnet build console/fuxiang-console.csproj -c Debug -p:Platform=x64
console\bin\x64\Debug\net8.0-windows10.0.19041.0\fuxiang-console.exe
```

## 命令行客户端

`cli/fxapi.py` 是与游戏内桥通信的唯一真源（文件 IPC），控制台内部也走它：

```bash
python cli/fxapi.py status {}
python cli/fxapi.py catalog_find "{\"query\":\"桌子\",\"limit\":3}"
python cli/fxapi.py save_dirty {}
```

stdout 直接输出业务 JSON；加 `--envelope` 可查看完整回执信封。

想让 AI 助手（Claude / ChatGPT / Codex 等）直接操作这套工具链，见
**《如何用ai接入该工具链》**；更多接口细节见 [API.md](API.md) 与 [MCP_USAGE.md](MCP_USAGE.md)。

## 游戏更新后：重建导出表

游戏大版本更新会让导出表过期，装桥会被防崩闸门拒绝（退出码 9）。三条重建路线任选：

1. **控制台一键**：首页 →「详细信息」→「重建导出表（游戏更新后）」；
2. **CLI**：`python cli/fxexports.py build <游戏根>\neox_engine.dll`（旧表自动备份 `.bak`）；
3. **Node 脚本**：`node research/tools_extract/pe_exports.js <游戏根>\neox_engine.dll neox_exports.txt`。

详见 [INJECTOR_USAGE.md](INJECTOR_USAGE.md) 第四节。

## 游戏内桥接与注入器（研究）

控制台与 CLI 通过文件 IPC 与运行在**游戏进程内**的桥接模块通信。本仓库同时提供这部分逆向源码，
仅作技术研究与学习交流之用：

- `bridge/` — 游戏内桥接模块（Python）：挂接编辑器运行时，向上暴露 op / eval 接口
- `injector/` — 桥接注入器（C#/.NET）：将桥接模块载入游戏进程，带导出表防崩闸门
- `fuxiang_bridge_install.ps1` — 装桥脚本：可见提权、装后校验（心跳 + pid 比对）

整条链路：`console / cli (fxapi.py) → 文件 IPC（ipc/cmd、ipc/out、bridge_state.json）→ 游戏内 bridge → 编辑器`

代码行为清单、杀软报毒原因与分发包完整性校验见**《关于代码安全性研究报告》**与 `SHA256SUMS.txt`；
预编译分发包（`fuxiang-toolkit-0.10.0-20261011-r4.zip`）在 [Releases](https://github.com/mohoney233/fuxiang/releases) 页面。

## 目录结构

```
启动控制台.cmd              一键构建 + 启动（仓库根目录）
请以管理员身份启动控制台.txt  权限说明
console/                   WinUI 3 桌面控制台
  Pages/                   十个功能页（XAML + code-behind）
  Services/                工具根定位、op/eval 调用、多桥接、长任务编排、导出表重建
  app.ico                  应用图标
cli/                       文件 IPC 客户端与控制台长任务脚本
  fxapi.py                 与游戏内桥通信的唯一真源
  fxexports.py             引擎导出表重建 / 核对（纯标准库）
  snapshot*.py             备份 / 恢复
  story.py                 剧情导出 / 导入 / 编辑
  event_*.py               事件导出 / 导入 / DSL / 标签
  fxcover.py / fxshot.py   封面 / 截图
bridge/                    游戏内桥接模块源码（Python，研究用途）
injector/                  桥接注入器源码（C#/.NET，研究用途）
mcp/                       MCP stdio 服务器（默认停用）与预览出图模块
research/tools_extract/    引擎导出表快照 + 重建脚本（游戏更新后用）
registry/                  编目参考数据（组件类型 / 物件清单 / 模型尺寸 / 资源大厅快照）
fuxiang_bridge_install.ps1 装桥脚本（可见提权 + 装后校验）
```

仓库根目录还附一组使用手册（[API.md](API.md)、[PLACEMENT.md](PLACEMENT.md)、
[STORY_EDIT.md](STORY_EDIT.md)、[COVER.md](COVER.md)、[SNAPSHOT.md](SNAPSHOT.md)、
[CATALOG.md](CATALOG.md) 等），分发包内同样收录。

## 如何用 AI 接入

见 **[如何用ai接入该工具链.md](如何用ai接入该工具链.md)**：把 Claude / ChatGPT / Codex 等
AI 助手接入 CLI、MCP 或控制台提示词库，让 AI 替你摆图、备份、写剧情。

## 致谢

- 控制台应用图标来自于 **小红书 @调之味上口下口口** 老师创作。

## 联系方式

- Bilibili：[@残雪Ethereal](https://space.bilibili.com/432133610)
- 邮箱：mohoney0@qq.com

## 免责声明

1. 本项目为个人自制的非官方工具，与网易及《第五人格》官方无任何关联，不代表官方立场。
2. 本项目仅供学习交流与个人创作使用，请勿用于商业用途，不得付费倒卖；如果你是付费获取的该应用，请联系作者。
3. 使用本项目产生的一切风险与后果（包括但不限于账号功能受限、数据丢失）由使用者自行承担。
4. 本仓库包含的注入器与游戏内桥接源码仅为技术研究与学习交流而公开，请勿将其用于违反游戏协议或其他违规用途。
5. 如本项目涉及侵犯相关方权益，请通过上方联系方式告知，我将及时处理。
