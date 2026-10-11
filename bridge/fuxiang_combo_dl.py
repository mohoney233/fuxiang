# -*- coding: utf-8 -*-
"""随笔浮想编辑器：组合（资源大厅）下载 —— 写层，非阻塞。

⚠️ 这是**对玩家账号组合库的服务端写操作**，不是本地读取。
   请勿批量调用；本模块按「一次一条 + 发送/轮询分离」设计，避免阻塞主线程。

机制（实机逆向 + 复核）
------------------------
```
UI : UIBattleUGCEditCompositeLibraryPart.request_download_composite(combo_id, share_code)
       if self._op_fetch.running: return            # 一次只允许一个
       if core_part.is_library_full(SHARED): ...    # 库有上限
part: fetch_shared_composite(share_code)
       -> _rpc_fetch_shared_composite(share_code)
       -> core.call_server(<iUGCEditCompositeLibraryPart>, func, share_code)   # 服务端 RPC
回包: on_combo_fetch_reply(ok, res)，res 含 input / output
```

容量上限（`GK.DM.ugc_battle_global_data` 实测）：

| 键 | 值 |
|---|---|
| `ugc_max_shared_combo_num` | 200（分享/已下载库） |
| `ugc_max_local_combo_num` | 200（我的库） |
| `ugc_combo_recent_dl_limit` | 100（「最近下载」只留 100） |
| `ugc_combo_recent_use_limit` | 100 |

因此批量下载会被上限卡住，且无法用本接口回滚（删除须走游戏内的批量删除）。
本模块**不做批量**：每次只发一条，且要求显式 `share_code`。

主线程约束：只发一次 RPC 立即返回；结果由 `fuxiang_combo_download_poll` 短调用轮询。
"""
import time

POLL_HINT_MS = 500
DEFAULT_TIMEOUT = 120.0


def _room(edit):
    room = edit.get_room()
    if room is None or type(room).__name__ != "WorldBattleUGCEdit":
        raise ValueError("当前不在随笔浮想编辑器")
    return room


def _part(edit):
    core = edit.get_core()
    if core is None:
        raise ValueError("UGCEditCore 不可用")
    import importlib
    iface = importlib.import_module(
        "common_cs.ugc_edit_core.parts.composite_library.iUGCEditCompositeLibraryPart"
    ).iUGCEditCompositeLibraryPart
    part = core.get_part(iface)
    if part is None:
        raise ValueError("组合库 part 不可用")
    return part


def _lib_type(edit, name="SHARED"):
    import importlib
    ck = importlib.import_module("common_cs.ComuKeys")
    return getattr(getattr(ck, "UGC_COMPOSITE_LIBRARY_TYPE"), name, 2 if name == "SHARED" else 1)


def _limits(edit):
    try:
        room = _room(edit)
        gd = room.create_unit_with_info.__globals__["GK"].DM.ugc_battle_global_data
        return {
            "max_shared": gd.get("ugc_max_shared_combo_num"),
            "max_local": gd.get("ugc_max_local_combo_num"),
            "recent_dl": gd.get("ugc_combo_recent_dl_limit"),
            "recent_use": gd.get("ugc_combo_recent_use_limit"),
        }
    except Exception:
        return {}


def _ids(part, t):
    out = []
    try:
        for rec in (part.get_library(t) or []):
            if hasattr(rec, "get"):
                out.append(str(rec.get("_id")))
            else:
                out.append(repr(rec)[:40])
    except Exception:
        pass
    return out


def _state(room):
    return vars(room).setdefault("_fuxiang_combo_dl", {"pending": None, "log": []})


def op_combo_download(edit, args):
    """发送一条组合下载请求（服务端写入）。非阻塞，结果用 poll 查。"""
    share_code = args.get("share_code")
    if not share_code or not isinstance(share_code, str):
        raise ValueError("需要 share_code（资源大厅卡片里的组合分享码）")
    if args.get("count") or args.get("share_codes") or args.get("limit"):
        raise ValueError("本接口只支持单条下载：请传 share_code，逐条调用并轮询完成"
                         "（批量会顶到库上限且无法回滚）")

    room = _room(edit)
    part = _part(edit)
    lt = _lib_type(edit, "SHARED")
    st = _state(room)

    if st.get("pending"):
        return {"status": "busy",
                "pending": st["pending"],
                "note": "上一笔下载尚未确认，请先用 fuxiang_combo_download_poll 查询。"}

    ids_before = _ids(part, lt)
    if part.is_library_full(lt):
        return {"status": "library_full",
                "library_size": len(ids_before),
                "limits": _limits(edit),
                "note": "组合库已达上限，服务器会拒绝新增；请先在游戏内删除部分组合。"}

    rec = {"share_code": share_code, "started": time.monotonic(),
           "before_ids": ids_before, "before_count": len(ids_before)}
    st["pending"] = rec
    send_error = None
    try:
        part.fetch_shared_composite(share_code)   # 发送 RPC 后立即返回
        rec["status"] = "sent"
    except Exception as exc:
        rec["status"] = "send_error"
        send_error = "%s: %s" % (type(exc).__name__, exc)
        rec["send_error"] = send_error
        st["pending"] = None
    # 注意：返回体里**不能带 "error" 键**（即使为 None）——
    # dispatch 见到 "error" 就会把整个结果判为失败，导致成功也被上报成错误。
    out = {"status": rec["status"],
           "share_code": share_code,
           "library_size_before": len(ids_before),
           "library_full": False,
           "limits": _limits(edit),
           "poll_after_ms": POLL_HINT_MS,
           "note": "请求已发出（服务端异步处理）。请间隔约 0.5 秒调用 "
                   "fuxiang_combo_download_poll 查询结果；期间不要再发新的下载。"}
    if send_error:
        out["send_error"] = send_error
        out["status"] = "send_error"
    return out


def op_combo_download_poll(edit, args):
    """查询下载结果：库中是否新增了条目。"""
    room = _room(edit)
    part = _part(edit)
    lt = _lib_type(edit, "SHARED")
    st = _state(room)
    rec = st.get("pending")
    if rec is None:
        last = (st.get("log") or [None])[-1]
        return {"status": "idle", "last": last,
                "note": "没有待确认的下载。用 fuxiang_combo_download 发起一条。"}

    ids_now = _ids(part, lt)
    new_ids = [i for i in ids_now if i not in rec["before_ids"]]
    elapsed = time.monotonic() - rec["started"]

    if new_ids:
        detail = None
        for r in (part.get_library(lt) or []):
            if hasattr(r, "get") and str(r.get("_id")) == new_ids[0]:
                detail = {
                    "combo_id": str(r.get("_id")),
                    "name": r.get("name"),
                    "share_code": r.get("combo_share_code"),
                    "has_combo_data": hasattr(r.get("combo_data"), "keys"),
                }
                break
        rec["status"] = "downloaded"
        rec["new_ids"] = new_ids
        rec["detail"] = detail
        rec.pop("started", None)
        st["pending"] = None
        st["log"].append(rec)
        return {"status": "downloaded", "elapsed_s": round(elapsed, 2),
                "new_ids": new_ids, "detail": detail,
                "library_size": len(ids_now),
                "note": "已出现在组合库中（服务端已受理）。组合本体 combo_data 可随后用 "
                        "fuxiang_resource_hall_item(combo_id=...) 读取。"}

    if elapsed >= DEFAULT_TIMEOUT:
        rec["status"] = "unknown"
        rec.pop("started", None)
        st["pending"] = None
        st["log"].append(rec)
        return {"status": "unknown", "elapsed_s": round(elapsed, 2),
                "share_code": rec["share_code"],
                "note": "等待超时，结果未知；不会自动重发。可在游戏内资源大厅确认该组合"
                        "是否已变为「已下载」。"}

    return {"status": "pending", "elapsed_s": round(elapsed, 2),
            "share_code": rec["share_code"],
            "library_size": len(ids_now), "poll_after_ms": POLL_HINT_MS,
            "note": "服务端尚未回包；请继续轮询同一笔请求，不要重复发送。"}


def op_combo_download_status(edit, args):
    """查看下载器状态与库容量（不发起下载）。"""
    part = _part(edit)
    lt_sh = _lib_type(edit, "SHARED")
    lt_lo = _lib_type(edit, "LOCAL")
    room = _room(edit)
    st = _state(room)
    gd = _limits(edit)
    return {
        "capability": {
            "part": type(part).__name__,
            "fetch_method": "fetch_shared_composite(share_code)",
            "rpc": "core.call_server(<iUGCEditCompositeLibraryPart>, ...)",
            "concurrency": "一次一条（UI 侧 _op_fetch.running 互斥；本接口另有 pending 锁）",
        },
        "limits": gd,
        "library": {
            "shared_count": len(_ids(part, lt_sh)),
            "shared_full": bool(part.is_library_full(lt_sh)),
            "local_count": len(_ids(part, lt_lo)),
            "local_full": bool(part.is_library_full(lt_lo)),
        },
        "pending": st.get("pending"),
        "history": (st.get("log") or [])[-10:],
        "note": "下载会写入玩家账号的组合库（服务端持久化），本接口不提供回滚；"
                "删除请在游戏内的资源大厅使用管理/删除。",
    }
