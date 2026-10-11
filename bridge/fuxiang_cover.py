# -*- coding: utf-8 -*-
"""地图封面（cover）编辑层 —— 把原生"选封面 → 上传 → 落库"链路包成 5 个 op。

原生链路（2026-09-30 实机取证：静态反汇编 `probe/cover_dis*.py` + 钩子日志
`probe/cover_hook_install.py` / `ipc/cover_hook.log`，全程只读或走原生方法）
--------------------------------------------------------------------
数据落在 **room（`WorldBattleUGCEdit`）** 上，不在任何组件袋里：

    room.scene_photo_path[]   封面图的**相对 doc_dir** 路径，最多 4 张，0 号是主封面
    room.scene_photo_md5[]    与上面同下标的文件 md5（小写）
    room.ugc_room_cover       服务器回读的已存封面（URL 或 URL 列表，进图时 init_scene 填）
    room.scene_photo_upload_flag  上传中标记

写入者（原生）：

    UIBattleUGCMapRelease.on_btn_photo → show_ui('UIUGCTakePhotoNew')
      → try_begin_shot → do_begin_shot → 引擎 save_screen_to_file_ex 写
        <doc_dir>/res/ui/live/<md5(time).jpg>
      → do_after_shot(result, abs_path, rel_path, MD5_UPPER)
      → screen_capture_cb: FilePickerProxy.get_file_md5(abs) 复核（防篡改，不一致就
        log_custom_tamper_photo 并中止）→ Avatar.request_photo_token(md5)
      → _show_custom_photo_ui → show_ui('UIBattleUGCPhotoCheck',
              args={photo_path, md5, select_photo_index, ugc_uuid, photo_token})
    UIBattleUGCPhotoCheck.on_btn_confirm(btn)        ← 「确认」只有这两行
        room.set_photo_path(self._photo_path, force_index=self.select_photo_index)
        room.set_photo_md5(self._cur_photo_md5, force_index=self.select_photo_index)
        ⇒ force_index=None 时是 append，给了下标就是改写那一个槽位

上传与落库（原生，发布流程里）：

    UIBattleUGCMapUploading.upload_cover_photo()
      sharing_type = 'UGC_MAP_COVER_PHOTO'   (ComuKeys.SHARING_IMG_TYPE，实测就是字符串本身)
      map_id       = Avatar.get_map_verify_id(room.ugc_key)
      other_info   = {'ugc_key':…, 'cover_photo_idx':idx,
                      'extraInfo': {'origin_id':room.ugc_key, 'role_id':Avatar.unique_id,
                                    'sharing_type':sharing_type,
                                    'center_host_num':ServerInfo.ServerData().get_center_host_num()}}
      Avatar.upload_img_to_filepicker(sharing_type, rel_path, md5=rel_md5, other_info=other_info)
        → Avatar.get_filepicker_token(extra_info)（服务端发 token）
        → FilePickerProxy.upload_img_to_filepicker(token, server_url, img_name)（HTTP POST）
        → Avatar.after_upload_photo(info, sharing_type, share_url, img_name)
           UGC_MAP_COVER_PHOTO 那一支把 share_url 交给
           g_ui_mgr.get_ui('UIBattleUGCMapUploading').save_uploaded_cover_photo_url(...)
           —— **发布面板没打开时这个 URL 就被丢掉**（get_ui 返回 Swallow 哨兵，调用被吞）
    Avatar.set_ugc_map_cover_photo(uuid, photo_lst)   ← 真正的落库 RPC
        call_server_method('set_ugc_map_cover_photo', uuid=…, photo_lst=[url, …])
    之后才是 do_map_release(…)（发布；本模块**不碰**）

所以本模块的做法是照抄上面这几步，但**不发布**：用 `set_photo_path/md5` 写槽位，用
`Avatar.upload_img_to_filepicker` 上传，自己接住 `after_upload_photo` 的 share_url
（游戏内只有发布面板会接），再用 `Avatar.set_ugc_map_cover_photo` 落库。

已知边界
--------
* 图片必须落在 `doc_dir` 下（原生路径是 `res/ui/live/*.jpg`）；上传时客户端按
  `doc_dir + img_name` 读文件，绝对路径不接受（`real_path` 那条支路是给别的通道用的）。
* 封面尺寸上限（实测 `FilePickerProxy.get_file_size_limit('UGC_MAP_COVER_PHOTO')`）
  = `[0, 12374016]`（约 12MB）；原生截图是 1920×1080 JPG。
* `photo_lst` 的顺序就是槽位顺序（下标 = cover_photo_idx），主封面在 0。
* 落库判据仍是**重进地图后 `room.ugc_room_cover` 有值**，所以写接口一律返回
  `persistence_verified: false`。
"""
import sys
import time

COVER_SHARING_TYPE = "UGC_MAP_COVER_PHOTO"
MAX_COVER_SLOTS = 4          # 发布面板显示 "n/4"
RELEASE_VIEW_NAME = "UIBattleUGCMapRelease"
LIVE_SUBDIR = "res/ui/live"  # 原生截图/封面目录（相对 doc_dir）


def _scene(edit):
    return edit.smart_reload("fuxiang_scene")


def _room(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != "WorldBattleUGCEdit":
        raise ValueError("当前不在随笔浮想编辑器")
    return room


def _gk(edit):
    return _room(edit).create_unit_with_info.__globals__["GK"]


def _ui_dict(edit):
    """当前创建出来的视图。⚠️ 别用 g_ui_mgr.get_ui()：未注册时它返回 Swallow 哨兵。"""
    gui = getattr(_gk(edit), "g_ui_mgr", None)
    return (vars(gui).get("ui_dict") or {}) if gui is not None else {}


def _view(edit, name):
    v = _ui_dict(edit).get(name)
    return v if type(v).__name__ == name else None


def _avatar(edit):
    av = getattr(_gk(edit), "g_avatar", None)
    if av is None:
        raise ValueError("拿不到 g_avatar（登录态？）")
    return av


def _doc_dir():
    import game3d
    return str(game3d.get_doc_dir()).replace("\\", "/").rstrip("/")


def _to_rel(edit, path):
    """把绝对/相对路径统一成**相对 doc_dir** 的形态（上传按这个读文件）。

    ⚠️ 只认"在 doc_dir 下"这一件事：Windows 的 `C:/...` 不以 `/` 开头，光判前缀会把
    盘外绝对路径当相对路径拼到 doc_dir 后面（离线用例 test_path_outside_doc_dir_rejected
    抓到过）。
    """
    import os
    import re
    doc = _doc_dir()
    p = str(path).replace("\\", "/").strip()
    if p.lower().startswith((doc + "/").lower()):
        return p[len(doc) + 1:], doc, True
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p) or os.path.isabs(p):
        return None, doc, False
    return p, doc, True


def _slots(edit):
    room = _room(edit)
    paths = list(getattr(room, "scene_photo_path", []) or [])
    md5s = list(getattr(room, "scene_photo_md5", []) or [])
    doc = _doc_dir()
    out = []
    for i in range(max(len(paths), len(md5s))):
        rel = paths[i] if i < len(paths) else None
        md5 = md5s[i] if i < len(md5s) else None
        item = {"index": i, "photo_path": rel, "md5": md5,
                "is_main_cover": i == 0}
        if rel:
            abs_p = "%s/%s" % (doc, rel)
            try:
                import os
                st = os.stat(abs_p)
                item["abs_path"] = abs_p
                item["exists"] = True
                item["bytes"] = int(st.st_size)
            except Exception:
                item["abs_path"] = abs_p
                item["exists"] = False
        out.append(item)
    return out


def _refresh_release(edit):
    """让发布面板把新封面显示出来（原生刷新函数，不是自己画）。"""
    rel = _view(edit, RELEASE_VIEW_NAME)
    if rel is None:
        return {"release_view": False,
                "note": "%s 没创建，跳过面板刷新（数据已写，不影响落库）" % RELEASE_VIEW_NAME}
    done = []
    for n in ("update_photo_state", "load_save_img_photo"):
        f = getattr(rel, n, None)
        if callable(f):
            try:
                f()
                done.append(n)
            except Exception as exc:
                return {"release_view": True, "refresh_failed": "%s: %s" % (type(exc).__name__, exc)}
    return {"release_view": True, "refreshed": done}


# ---------------------------------------------------------------------------
# 上传回执：自己接住 after_upload_photo 的 share_url（游戏里只有发布面板会接）
# ---------------------------------------------------------------------------
def _capture_state(room):
    st = getattr(room, "_fx_cover_replies", None)
    if st is None:
        st = []
        room._fx_cover_replies = st
    return st


def _install_capture(edit, av):
    """包一层 Avatar.after_upload_photo（幂等）。只观察，不改行为。"""
    room = _room(edit)
    _capture_state(room)
    if getattr(av, "_fx_cover_hooked", False):
        return {"already_installed": True}
    orig = av.after_upload_photo

    def hooked(*a, **kw):
        rec = {"ts": time.time(),
               "sharing_type": repr(a[1] if len(a) > 1 else kw.get("sharing_type")).strip("'\""),
               "share_url": (a[2] if len(a) > 2 else kw.get("share_url")),
               "img_name": (a[3] if len(a) > 3 else kw.get("img_name")),
               "other_info": None}
        try:
            info = a[0] if a else kw.get("info")
            oi = info.get("other_info") if hasattr(info, "get") else None
            if oi is not None:
                rec["other_info"] = {"ugc_key": oi.get("ugc_key"),
                                     "cover_photo_idx": oi.get("cover_photo_idx")}
        except Exception:
            pass
        try:
            _capture_state(room).append(rec)
        except Exception:
            pass
        return orig(*a, **kw)

    av.after_upload_photo = hooked
    av._fx_cover_hooked = True
    return {"already_installed": False}


def _cover_replies(edit, since_ts=0.0):
    room = _room(edit)
    av = _avatar(edit)
    rows = [r for r in _capture_state(room)
            if r.get("sharing_type") == COVER_SHARING_TYPE and (r.get("ts") or 0) >= since_ts]
    return rows, getattr(av, "_fx_cover_hooked", False)


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------
def op_cover_state(edit, args):
    """封面实况：槽位 / 服务器回读的封面 / 发布面板开没开 / 图片目录。"""
    room = _room(edit)
    doc = _doc_dir()
    out = {"ok": True,
           "ugc_key": edit._safe(getattr(room, "ugc_key", None)),
           "doc_dir": doc,
           "photo_dir": "%s/%s" % (doc, LIVE_SUBDIR),
           "slots": _slots(edit),
           "max_slots": MAX_COVER_SLOTS,
           "ugc_room_cover": edit._safe(getattr(room, "ugc_room_cover", None)),
           "scene_photo_upload_flag": edit._safe(getattr(room, "scene_photo_upload_flag", None)),
           "release_view_open": _view(edit, RELEASE_VIEW_NAME) is not None,
           "note": "slots[i].photo_path 是相对 doc_dir 的路径；0 号是主封面。"
                   "落库判据是重进后 ugc_room_cover 有值，详见 COVER.md。"}
    try:
        out["map_verify_id"] = edit._safe(_avatar(edit).get_map_verify_id(room.ugc_key))
    except Exception as exc:
        out["map_verify_id_err"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        n = len([x for x in __import__("os").listdir("%s/%s" % (doc, LIVE_SUBDIR))])
        out["photo_dir_files"] = n
    except Exception:
        out["photo_dir_files"] = None
    return out


def op_cover_set_slot(edit, args):
    """把一张已在 doc_dir 下的图片写成封面（等价于原生"确认"那两行）。

    args: path（相对 doc_dir 或绝对路径）, index=0, dry_run
    """
    room = _room(edit)
    path = args.get("path")
    if not path:
        return {"ok": False, "error": "缺少 path（相对 doc_dir 的图片路径，如 res/ui/live/xxx.jpg）"}
    idx = int(args.get("index", 0))
    if not 0 <= idx < MAX_COVER_SLOTS:
        return {"ok": False, "error": "index 只能在 0..%d（0 号是主封面）" % (MAX_COVER_SLOTS - 1)}
    rel, doc, ok = _to_rel(edit, path)
    if not ok:
        return {"ok": False, "error": "path 必须落在 doc_dir(%s) 下；绝对路径请先拷进去" % doc}
    abs_p = "%s/%s" % (doc, rel)
    import os
    if not os.path.isfile(abs_p):
        return {"ok": False, "error": "文件不存在: %s" % abs_p}
    fpp = __import__("common_cs.FilePickerProxy", fromlist=["FilePickerProxy"]).FilePickerProxy
    try:
        # get_file_md5 签名只有 (path)：游戏里也是按 FilePickerProxy.get_file_md5(p) 直接调的，
        # 走实例会变成 (self, path) 两个实参而 TypeError。
        md5 = fpp.get_file_md5(abs_p)
    except Exception as exc:
        return {"ok": False, "error": "算 md5 失败: %s: %s" % (type(exc).__name__, exc)}
    if not md5:
        return {"ok": False, "error": "md5 为空（FilePickerProxy 未初始化？）"}
    before = _slots(edit)
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "index": idx, "photo_path": rel, "md5": md5,
                "bytes": int(os.stat(abs_p).st_size), "before": before,
                "note": "dry_run：没写。真写会调原生 room.set_photo_path/set_photo_md5(force_index=%d)" % idx}
    try:
        room.set_photo_path(rel, force_index=idx)
        room.set_photo_md5(str(md5), force_index=idx)
    except Exception as exc:
        return {"ok": False, "error": "原生 set_photo_path/md5 抛错: %s: %s"
                % (type(exc).__name__, exc)}
    after = _slots(edit)
    got = after[idx] if idx < len(after) else {}
    refresh = _refresh_release(edit)
    return {"ok": got.get("photo_path") == rel and str(got.get("md5")) == str(md5),
            "index": idx, "photo_path": rel, "md5": str(md5),
            "bytes": int(os.stat(abs_p).st_size),
            "readback": got, "before": before, "panel_refresh": refresh,
            "persistence_verified": False,
            "note": "槽位已写并刷新了面板显示；要成为别人的看到的封面，还得 cover_upload + cover_apply。"}


def op_cover_upload(edit, args):
    """按原生发布流程的写法上传封面图（不发布地图）。

    args: index=0, dry_run
    回执靠 cover_upload_poll 读（本模块自己接 after_upload_photo）。
    """
    room = _room(edit)
    idx = int(args.get("index", 0))
    slots = _slots(edit)
    if idx >= len(slots):
        return {"ok": False, "error": "槽位 %d 是空的，先 cover_set_slot" % idx}
    slot = slots[idx]
    if not slot.get("exists"):
        return {"ok": False, "error": "本地文件不存在: %s" % slot.get("abs_path")}
    limit = None
    try:
        fpp = __import__("common_cs.FilePickerProxy", fromlist=["FilePickerProxy"]).FilePickerProxy
        limit = fpp().get_file_size_limit(COVER_SHARING_TYPE)
        if limit and len(limit) >= 2 and slot.get("bytes") and slot["bytes"] > int(limit[1]):
            return {"ok": False, "error": "文件 %s 字节，超过上限 %s；先压成 JPG"
                    % (slot["bytes"], limit[1])}
    except Exception:
        pass
    av = _avatar(edit)
    from common_cs import ServerInfo
    other_info = {"ugc_key": room.ugc_key,
                  "cover_photo_idx": idx,
                  "extraInfo": {"origin_id": room.ugc_key,
                               "role_id": av.unique_id,
                               "sharing_type": COVER_SHARING_TYPE,
                               "center_host_num": ServerInfo.ServerData().get_center_host_num()}}
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "index": idx, "photo_path": slot["photo_path"],
                "md5": slot["md5"], "sharing_type": COVER_SHARING_TYPE,
                "map_verify_id": edit._safe(av.get_map_verify_id(room.ugc_key)),
                "size_limit": limit, "would_call": "Avatar.upload_img_to_filepicker",
                "other_info": other_info,
                "note": "dry_run：没发。真跑会向服务端要 token 再 HTTP POST 这个文件。"}
    capture = _install_capture(edit, av)
    t0 = time.time()
    try:
        av.upload_img_to_filepicker(COVER_SHARING_TYPE, slot["photo_path"],
                                   md5=str(slot["md5"]), other_info=other_info)
    except Exception as exc:
        return {"ok": False, "error": "upload_img_to_filepicker 抛错: %s: %s"
                % (type(exc).__name__, exc), "index": idx}
    return {"ok": True, "status": "uploading", "index": idx,
            "photo_path": slot["photo_path"], "md5": slot["md5"],
            "started_at": t0, "capture": capture,
            "note": "上传是异步的（服务端发 token + HTTP POST）。用 cover_upload_poll 取 share_url。"}


def op_cover_upload_poll(edit, args):
    """查上传回执：pending / uploaded（带 share_url）/ failed（带现场）。"""
    room = _room(edit)
    since = float(args.get("since") or 0.0)
    rows, hooked = _cover_replies(edit, since)
    idx = args.get("index")
    if idx is not None:
        rows = [r for r in rows if (r.get("other_info") or {}).get("cover_photo_idx") == int(idx)]
    ok_rows = [r for r in rows if r.get("share_url")]
    if ok_rows:
        return {"ok": True, "status": "uploaded", "replies": ok_rows,
                "share_url": ok_rows[-1]["share_url"],
                "next": "用 cover_apply 把它写进地图记录（set_ugc_map_cover_photo）"}
    if not hooked:
        return {"ok": False, "status": "no_capture",
                "error": "回执捕获没装上（先跑 cover_upload，它会自动装）"}
    return {"ok": True, "status": "pending", "replies": rows,
            "waiting_since": since,
            "note": "还没回包。服务端发 token + 上传通常几秒到几十秒，稍后再查。"}


def op_cover_apply(edit, args):
    """落库：Avatar.set_ugc_map_cover_photo(ugc_key, [url…])。

    args: urls=[…]（不给就用本会话上传回执里的 URL，按 cover_photo_idx 排）, dry_run
    """
    room = _room(edit)
    av = _avatar(edit)
    urls = args.get("urls")
    if not urls:
        rows, _ = _cover_replies(edit, float(args.get("since") or 0.0))
        by_idx = {}
        for r in rows:
            if r.get("share_url"):
                i = (r.get("other_info") or {}).get("cover_photo_idx")
                by_idx[int(i or 0)] = r["share_url"]
        if not by_idx:
            return {"ok": False, "error": "没有可用的上传回执 URL；先 cover_upload / cover_upload_poll"}
        n = max(by_idx) + 1
        urls = [by_idx.get(i) for i in range(n)]
    urls = [str(u) for u in urls]
    if not all(u.startswith("http") for u in urls):
        return {"ok": False, "error": "URL 形态不对: %s" % (urls[:2],)}
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "ugc_key": room.ugc_key, "urls": urls,
                "would_call": "Avatar.set_ugc_map_cover_photo"}
    try:
        av.set_ugc_map_cover_photo(room.ugc_key, urls)
    except Exception as exc:
        return {"ok": False, "error": "set_ugc_map_cover_photo 抛错: %s: %s"
                % (type(exc).__name__, exc), "urls": urls}
    return {"ok": True, "ugc_key": room.ugc_key, "urls": urls,
            "slots": _slots(edit), "persistence_verified": False,
            "note": "RPC 已上送。终审：退出重进后 cover_state 的 ugc_room_cover 应变成这个 URL；"
                    "封面本身与地图内容无关，不会改动物件，但**退出编辑器会自动保存**（README）。"}


OPS = {
    "cover_state": op_cover_state,
    "cover_set_slot": op_cover_set_slot,
    "cover_upload": op_cover_upload,
    "cover_upload_poll": op_cover_upload_poll,
    "cover_apply": op_cover_apply,
}

_ = sys  # 保留给 smart_reload 的路径调试
