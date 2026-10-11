# 地图封面（COVER）

> 给「随笔浮想」的**地图封面**加的编辑能力：把一张图片（电脑里的、游戏相册目录里的、
> 或工具链自己出的引擎截图）写成地图封面，按原生链路上传到网易封面 CDN，再写进地图记录。
> 共 **5 个 op**（注册在 `bridge/fuxiang_edit.py` 的 `OPS`，名字全部以 `cover_` 开头；
> 本文件末节有清单）。命令行入口 `cli/fxcover.py`，游戏侧实现 `bridge/fuxiang_cover.py`。
>
> 2026-09-30 实机逆向自 dwrg.exe（`.nxs` 无源码，结论全部来自 `dis` 字节码 + 钩子抓到的真实
> 调用序列）。探针：`probe/cover_scan*.py`、`probe/cover_dis1..11*.py`（字节码落在同名
> `probe/cover_dis*/` 目录）、`probe/cover_hook_install.py`（钩子）+ `probe/cover_drive*.py`
> （按原生链路实机跑一遍）。

## 0. 一句话结论

封面**不是组件属性**，是挂在 room 上的四个字段；玩家点「确认」其实只有两行：

```text
room.scene_photo_path[i]   封面图相对 doc_dir 的路径，最多 4 张，0 号是主封面
room.scene_photo_md5[i]    与上面同下标的文件 md5（小写）
room.ugc_room_cover        服务器回读的已存封面（进图时 init_scene 填）
room.scene_photo_upload_flag  上传中标记
```

```text
UIBattleUGCPhotoCheck.on_btn_confirm(btn)
    room.set_photo_path(self._photo_path, force_index=self.select_photo_index)
    room.set_photo_md5(self._cur_photo_md5, force_index=self.select_photo_index)
```

要把封面变成别人看得到的那张，还得走发布流程里的**上传 + 落库**两步（本模块照抄这两步，
但**不发布**）：

```text
UIBattleUGCMapUploading.upload_cover_photo()
  sharing_type = 'UGC_MAP_COVER_PHOTO'
  Avatar.upload_img_to_filepicker(sharing_type, rel_path, md5=…, other_info=
        {'ugc_key', 'cover_photo_idx', 'extraInfo':{origin_id, role_id, sharing_type,
                                                   center_host_num}})
    → Avatar.get_filepicker_token(extra_info)      服务端发 token
    → FilePickerProxy.upload_img_to_filepicker(token, server_url, img_name)   HTTP POST
    → Avatar.after_upload_photo(info, sharing_type, share_url, img_name)      带回 URL
Avatar.set_ugc_map_cover_photo(ugc_key, [url…])    ← 真正的落库 RPC
```

⚠️ **`after_upload_photo` 的 UGC_MAP_COVER_PHOTO 那一支把 URL 交给
`g_ui_mgr.get_ui('UIBattleUGCMapUploading').save_uploaded_cover_photo_url(…)`**，而
`get_ui` 对未注册的名字返回 **`Swallow` 哨兵**（不是 None）——所以**发布面板没打开时这个
URL 会被静默吞掉**。本模块不依赖那个面板：自己包一层 `after_upload_photo` 观察回执，
再直接调 `set_ugc_map_cover_photo`。

## 1. 定位到的东西

| 目标 | 位置 |
|---|---|
| 封面数据 | `room.scene_photo_path` / `scene_photo_md5`（`WorldBattleUGCBase`） |
| 服务器回读的封面 | `room.ugc_room_cover`（`init_scene` 里填，客户端只读） |
| 封面槽 UI | 视图 `UIBattleUGCMapRelease`（4 格 `node_group_image_list`，文案 `n/4`） |
| 原生"选封面"入口 | `UIBattleUGCMapRelease.on_btn_photo` → `show_ui('UIUGCTakePhotoNew')` |
| 拍照出图 | `UITakePhotoNewBase.do_begin_shot` → 引擎 `scene.save_screen_to_file_ex` 写 `<doc_dir>/res/ui/live/<md5(time)>.jpg` |
| 防篡改 | `screen_capture_cb` 用 `FilePickerProxy.get_file_md5(abs)` 复核，不一致 `log_custom_tamper_photo` 并中止 |
| 确认面板 | `UIBattleUGCPhotoCheck`（`from_ugc_edit` 分支；参数 `photo_path/md5/select_photo_index/ugc_uuid/photo_token`） |
| 上传通道 | `Avatar.upload_img_to_filepicker` + `FilePickerProxy`（`get_ugc_cover_save_url`） |
| 落库 RPC | `Avatar.set_ugc_map_cover_photo(uuid, photo_lst)` → `call_server_method` |
| 图片目录 | `game3d.get_doc_dir()` = `<游戏根>/Documents`，原生子目录 `res/ui/live/` |
| 「保存到相册」落点（PC） | `SDKManager.save_image_to_album` 的 `is_win32` 分支：`shutil.copyfile` 到 `~\Documents\IdentityV\<basename>`（目录自动建；UI 传的 `target_file_name` 在这条分支没用）。移动端才走 `saveAlbum` / `game3d.save_image_to_gallery` |
| 封面体积上限 | 实测 `FilePickerProxy.get_file_size_limit('UGC_MAP_COVER_PHOTO')` = `[0, 12374016]`（≈12MB） |

## 2. 命令速查

```powershell
$py = '<你的-python.exe-完整路径>'
& $py tools/fuxiang/cli/fxcover.py state                 # 槽位 / 服务器封面 / 面板状态
& $py tools/fuxiang/cli/fxcover.py from-file D:\x\图.png  # 一条龙：落图→写槽→上传→落库
& $py tools/fuxiang/cli/fxcover.py set   <路径> [--index 0] [--dry-run]
& $py tools/fuxiang/cli/fxcover.py upload [--index 0] [--dry-run]
& $py tools/fuxiang/cli/fxcover.py poll   [--index 0] [--timeout 150]
& $py tools/fuxiang/cli/fxcover.py apply  [--url URL ...] [--dry-run]
```

`set` 的参数既可以是**电脑上任意位置的图片**（CLI 会用 Pillow 中心裁 16:9、缩到 1920×1080、
存 jpg 落到 `<doc_dir>/res/ui/live/<md5>.jpg`），也可以是**已经在图片目录里的相对路径**
（游戏相册/引擎截图，原样用，不重编码）。

op 也能直接透传：`cli/fxedit.py cover_state '{}'`。

## 3. 典型流程（含持久化核实）

1. `state` 看清当前槽位与 `ugc_room_cover`。
2. `set <图片> --index 0 --dry-run` 预演（回显将要写的 path/md5/bytes 与改前状态）。
3. `set <图片> --index 0` → 走原生 `set_photo_path/set_photo_md5(force_index=0)`，
   并调原生 `update_photo_state` / `load_save_img_photo` 让发布面板显示出来。
4. `upload --index 0` → 原生 `Avatar.upload_img_to_filepicker('UGC_MAP_COVER_PHOTO', …)`。
5. `poll --index 0` → 拿到 `share_url`（实测 0.4s 回包；没回就继续等，别重发上传）。
6. `apply` → `Avatar.set_ugc_map_cover_photo(ugc_key, [url])`。省略 `--url` 就用本会话回执。
7. **终审：退出编辑器再重进**，`state` 里 `ugc_room_cover` 应变成那个 URL。
   工具一律返回 `persistence_verified: false`，只有第 7 步算数。

⚠️ 退出编辑器会**自动保存**（README「保存」节）。封面字段不在 `change_units` 里（实测写封面
后 `save_dirty.change_units_length` 仍为 0），所以它不靠地图保存上送，靠的是第 6 步那条 RPC。

## 4. 2026-09-30 实机闭环（那张 14998 组件的《春岚》）

| 步骤 | 结果 |
|---|---|
| 钩子跑原生链路：`rel.on_btn_photo` → `try_begin_shot` | 引擎写出 `res/ui/live/6ed6443a…jpg`（1920×1080，257956 B）；`get_file_md5` = `fda67f7f…`；`request_photo_token` 回 `photo_token`；弹出 `UIBattleUGCPhotoCheck` |
| `PhotoCheck.on_btn_confirm` | `scene_photo_path=['res/ui/live/6ed6443a….jpg']`、`scene_photo_md5=['fda67f7f…']`；发布面板随即 `fill_cover_img` + `switch_group_node_state('nml')` |
| `set res/ui/live/e4b81af2…jpg --index 0`（相册里那张成品插画） | 写入并回读一致，md5 现算 `93726446…`，218339 B |
| `upload --index 0` | 0.4s 后回执 `share_url = http://h55-ugc-mapshow.fp.ps.netease.com/file/6abce619d4ced355b9c4a1baxHJ2r8G407` |
| 校验 CDN | `curl` 该 URL：HTTP 200、218339 B、`image/jpeg`，**md5 与本地文件逐字节一致** |
| `apply` | `set_ugc_map_cover_photo('6ab9151ee6ebe25f903e4458', [url])` 已上送 |
| 重进终审 | **未做**（要人工退出重进）；`ugc_room_cover` 目前仍是进图时读到的 `null` |

## 5. 护栏

| 护栏 | 行为 |
|---|---|
| 槽位越界 | `index` 只允许 0..3（面板就是 `n/4`），否则直接 `ok:false` |
| 图片不在 doc_dir 下 | 拒绝：上传按 `doc_dir + img_name` 读文件，绝对路径不受理 |
| 文件不存在 / md5 算不出 | 拒绝，并说明是哪一个 |
| 超过封面体积上限 | 用原生 `get_file_size_limit('UGC_MAP_COVER_PHOTO')` 现算，超了直接拒 |
| `dry_run` | `set` / `upload` / `apply` 都在**任何写入或上送之前**返回预演 |
| 回执捕获 | 幂等包一层 `Avatar.after_upload_photo`（只观察，原调用照旧转发），只收 `UGC_MAP_COVER_PHOTO` |
| URL 形态 | `apply` 前校验每项都以 `http` 开头，避免把空串写进地图记录 |

## 6. 已知边界

* **不发布**：本模块只到 `set_ugc_map_cover_photo`。`do_map_release` /
  `submit_verify_ugc_map`（真正让地图公开）不在能力范围内，需要玩家自己在发布面板做。
* 封面槽是**本地会话数据**：只 `set` 不 `apply` 时，别人看不到，重进后 `scene_photo_path`
  会回到服务器那份（`ugc_room_cover`）。
* 原生"选封面"只有**拍照**一条路（`on_btn_photo` → 拍照界面），界面里没有"从相册选"按钮；
  本模块把"相册/电脑里的图片"直接接到 `set_photo_path/md5` 这个原生落点上。
* 上传走的是游戏自己的通道（服务端发 token + 客户端 HTTP POST），没有自造协议；
  但**回执不经过发布面板**，所以 `cover_upload_poll` 依赖那层观察钩子——桥接重装或
  avatar 实例换掉后要重新 `cover_upload`（钩子会自动再装一次）。
* `photo_lst` 的下标就是槽位顺序；只传一张时它是主封面。多槽（最多 4）需要逐个
  `upload` 后一次性 `apply --url … --url …`。
* ⚠️ **多槽写入的坑（2026-10-02 实机，dis 实锤）**：原生 `set_photo_path(rel, force_index=i)`
  在 `force_index >= len(scene_photo_path)` 时**静默丢弃**（字节码：`force_index < len` 才
  `STORE_SUBSCR`，否则直接 `RETURN_VALUE`，不报错不扩列）。列表只有 1 项时写 1..3 号槽
  全部无声失败，回读 `readback: {}` 是唯一线索。修法：先把 `scene_photo_path` /
  `scene_photo_md5` 补齐到 4 项（空串占位即可），再逐槽写——原生 UI 流程里列表总是
  先被填满，所以平时踩不到。
* 游戏内 `FilePickerProxy.get_file_md5` 是**类级函数**（签名只有 `(path)`）：按实例调会变成
  `(self, path)` 而 `TypeError`（本模块踩过，回归用例 `test_md5_is_called_class_level` 锁住）。

## 7. op 清单（5 个）

| op | 用途 |
|---|---|
| `cover_state` | 封面实况：槽位（路径/md5/在不在/字节数）、`ugc_room_cover`、面板开没开、图片目录 |
| `cover_set_slot` | 把 doc_dir 下一张图写成封面槽（等价原生"确认"）；`path` `index` `dry_run` |
| `cover_upload` | 按发布流程的原生写法上传封面图（不发布）；`index` `dry_run` |
| `cover_upload_poll` | 查上传回执 `share_url`；`index` `since` |
| `cover_apply` | 落库：`set_ugc_map_cover_photo(ugc_key, [url…])`；`urls` 省略则用本会话回执 |

离线回归：`python -m unittest tests.test_cover -v`（槽位护栏、dry_run 真只读、
md5 的类级调用、回执按 sharing_type 过滤、文档 op 数 == OPS 实际）。
