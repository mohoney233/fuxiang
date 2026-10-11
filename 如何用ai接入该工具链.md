# 如何用 AI 接入该工具链

这套工具链天生就是给「AI 助手当手」用的：装好桥之后，AI 可以通过命令行或 MCP 直接
摆物件、改剧情、做备份、截平面图。本文给出三种接入方式，按你的使用习惯任选其一，
也可以混用。

## 零、前提（三种方式共用）

1. **装桥**：游戏进入「随笔浮想」编辑器后，双击根目录 `启动控制台.cmd`，在首页
   「详细信息」里点「安装 / 重注入桥接」（或跑 `fuxiang_bridge_install.ps1`）。
   每次游戏重启后都要重装一次。
2. **Python**：机器上有 Python 3.8+（PATH 上有 `python` 即可）。
3. **AI 能在本机执行命令**（方式 A/B），或者你手动复制粘贴提示词（方式 C）。

## 一、方式 A：AI 编码助手 + CLI（推荐，最通用）

适用：Claude Code、Codex CLI、Cursor 等能在你的电脑上**执行命令**的 AI 助手。
把下面这段话发给 AI 当开场白（或存成项目说明让 AI 自动读到）：

```text
你可以操作《第五人格》「随笔浮想」UGC 编辑器。工具链位于 <工具根>（本仓库），
游戏内桥接已装好。请遵守：

1. 所有操作通过：python cli/fxapi.py <op> '<json参数>'（在 <工具根> 下执行）。
   stdout 直接输出业务 JSON；op 失败会有非零退出码。
2. 常用 op：
   - status                    编辑器状态（先查这个确认连接）
   - list_items / catalog_find 查物件目录（先 find 再 place，不要凭空猜 item_id）
   - place_item / place_poll   摆放（place_item 返回 request_id 后立即 place_poll，
     status=placed 即成功；不要固定 sleep）
   - set_transform / edit_*    改已有对象的变换 / 属性
   - save_map / save_map_poll  真实保存（返回 saved 才算落库）
3. 铁律：
   - 保存会重置基线：每批摆完立即 save_map，末次保存后不要再摆放；
   - place_item 必须带唯一 request_id，重试沿用同一 id；
   - 不要用 eval 执行破坏性代码；删除类操作先 dry_run；
   - 持久化以「退出编辑器重进后核对」为准，接口返回不代表已存档。
4. 图形操作（备份恢复 / 封面 / 截图）优先让用户用控制台点按钮，命令行做批量与自动化。
```

之后就可以直接下任务：「在 (600, 2, -300) 附近摆一圈 8 根栏杆围个场地，摆完保存」、
「把当前地图备份一份」「导出剧情到 markdown」等。

常用入口速查：

| 命令 | 用途 |
| --- | --- |
| `python cli/fxapi.py status {}` | 桥接 / 编辑器状态 |
| `python cli/fxapi.py catalog_find '{"query":"桌子","limit":5}'` | 按名字找物件 |
| `python cli/fxapi.py place_item '{...}'` | 摆放（异步，返回 request_id） |
| `python cli/fxapi.py save_map {}` → `save_map_poll` | 真实保存并查回执 |
| `python cli/snapshot.py export out.json` | 地图快照（备份） |
| `python cli/fxshot.py sweep union` | 多角度截图 + 平面图 |
| `python cli/consts.py` | 常量真值（勿手写数字） |

## 二、方式 B：MCP 客户端（Claude Desktop / Cursor 等）

工具链自带一个 **stdio MCP 服务器**（`mcp/server.py`），把编辑器能力注册成 MCP 工具，
支持 MCP 的客户端可以直接「选工具」调用。

默认停用（防止误启动），启动前设置环境变量 `FUXIANG_ENABLE_MCP=1`。客户端配置示例
（即仓库根的 `mcp_config.example.json`，把两处占位改成你的实际路径）：

```json
{
  "mcpServers": {
    "fuxiang-ugc-editor": {
      "command": "<你的-python.exe-完整路径>",
      "args": ["-u", "<工具根>\\mcp\\server.py"]
    }
  }
}
```

- PowerShell 手动试跑：`$env:FUXIANG_ENABLE_MCP=1` 后 `python -u mcp/server.py`；
- HTTP 通道（`mcp/http_server.py`）**默认停用且不建议开启**：无鉴权且允许任意 op
  透传，等于把游戏进程内任意代码执行暴露在局域网；
- 工具清单与调用协议详见 [MCP_USAGE.md](MCP_USAGE.md)。

## 三、方式 C：控制台「AI 提示词」页（零配置）

不想配置任何东西：打开控制台 →「AI 提示词」页 → 按场景挑一条（桥接与工具链 / 地图与
备份 / 摆放与编辑 / 剧情 / 事件 / 队伍 / 封面与截图 / 综合协作，共 25 条）→ 一键复制 →
粘贴给你的 AI 助手。每条提示词自带操作规约，AI 照做即可。

## 四、安全规约（给 AI 也给你）

- **保存基线**：`save_map` 之后编辑器账本重置，之前的未保存摆放会作废——让 AI
  「小批摆放 → 保存 → 再继续」，不要一口气摆几百件才保存。
- **重进核验**：接口返回 `placed` 只代表编辑器接纳，真实持久化要在游戏里保存并
  退出重进核对；AI 无法替你做这一步，别让它宣称「已永久保存」。
- **删除三思**：剧情删除走 `fuxiang_story_delete`（带 cascade 会连删子节点），
  先让 AI 跑 dry_run 预览。
- **eval 逃生舱**：`fuxiang_eval` 是给开发调试用的，别让 AI 用它做常规操作，
  更别让它 `eval` 任何涉及网络 / 文件系统的代码。
- **账号风险**：这是注入式第三方工具，任何自动化操作都一样的风险面，AI 不会让它
  更安全也不会更危险——风险由你承担。

## 五、任务示例

| 你说 | AI 会做 |
| --- | --- |
| 「看看桥接连上没」 | `fxapi.py status`，读 pid / 心跳 |
| 「在 (628,2,-60) 放一张桌子5，放完保存」 | `catalog_find` 找 item_id → `place_item` → 立即 `place_poll` → `save_map` → `save_map_poll` |
| 「把这张图备份一下」 | `cli/snapshot.py export backups/xxx.json`（不动地图） |
| 「把剧情导出成 Markdown 给我」 | `cli/story.py` 导出接口，输出 md 文件 |
| 「游戏更新了，桥装不上」 | 跑 `python cli/fxexports.py build <游戏根>\neox_engine.dll` 重建导出表，再装桥 |
