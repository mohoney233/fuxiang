#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fxcover — 地图封面（cover）命令行。

用法（图片可以来自电脑任意位置，也可以直接是游戏相册/截图目录里的文件）：

  python cli/fxcover.py state                        # 看封面槽位与服务器回读的封面
  python cli/fxcover.py from-file D:/x/图.png [--index 0]   # 一条龙：落图→写槽→上传→落库
  python cli/fxcover.py set   D:/x/图.png [--index 0] [--dry-run]
  python cli/fxcover.py upload [--index 0] [--dry-run]
  python cli/fxcover.py poll   [--index 0] [--timeout 90]
  python cli/fxcover.py apply  [--url URL ...] [--dry-run]

分步等价于 from-file 依次执行 set → upload → poll → apply。
链路与边界见 COVER.md；游戏侧实现见 bridge/fuxiang_cover.py。
"""
import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fxapi import call  # noqa: E402

LIVE_SUBDIR = os.path.join("res", "ui", "live")
COVER_MAX_BYTES = 12374016        # 实测 get_file_size_limit('UGC_MAP_COVER_PHOTO')
TARGET_WH = (1920, 1080)          # 原生截图尺寸，封面按它出


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _state():
    return call("cover_state", {})


def _stage(src, doc_dir, write=True):
    """把一张电脑里的图片变成游戏可用的封面 JPG：落到 <doc_dir>/res/ui/live/<md5>.jpg。

    已经是 jpg 且尺寸/体积合适就直接拷；否则用 Pillow 裁成 16:9、缩到 1920×1080 再存 jpg
    （原生封面就是这个形态，服务端审核也按图片格式收）。
    write=False 时只在内存里算出目标路径/字节数/md5，不落盘（给 --dry-run 用）。
    """
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise SystemExit("文件不存在: %s" % src)
    dst_dir = os.path.join(doc_dir, LIVE_SUBDIR.replace(os.sep, "/")).replace("\\", "/")
    if write:
        os.makedirs(dst_dir, exist_ok=True)

    ext = os.path.splitext(src)[1].lower()
    size = os.path.getsize(src)
    data = None
    if ext == ".jpg" and size <= COVER_MAX_BYTES:
        data = open(src, "rb").read()
    else:
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image is None:
            if ext not in (".jpg", ".jpeg"):
                raise SystemExit("没装 Pillow，且源文件不是 jpg；请先自己转成 jpg 再来")
            data = open(src, "rb").read()
        else:
            im = Image.open(src)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            tw, th = TARGET_WH
            # 中心裁到目标比例，避免拉伸
            target_ratio = tw / float(th)
            if w / float(h) > target_ratio:
                nw = int(h * target_ratio)
                im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            else:
                nh = int(w / target_ratio)
                im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
            im = im.resize((tw, th))
            if im.mode != "RGB":
                im = im.convert("RGB")
            import io
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            data = buf.getvalue()
    if len(data) > COVER_MAX_BYTES:
        raise SystemExit("转出来的 jpg 仍有 %d 字节，超过上限 %d" % (len(data), COVER_MAX_BYTES))
    md5 = hashlib.md5(data).hexdigest()
    name = "%s.jpg" % md5
    dst = "%s/%s" % (doc_dir.replace("\\", "/").rstrip("/"),
                     (LIVE_SUBDIR + "/" + name).replace(os.sep, "/"))
    if write:
        with open(dst, "wb") as f:
            f.write(data)
    return {"rel_path": (LIVE_SUBDIR + "/" + name).replace(os.sep, "/"),
            "abs_path": dst, "bytes": len(data), "md5": md5,
            "written": bool(write),
            "converted": ext != ".jpg" or size != len(data)}


def _set(src, index, dry_run):
    st = _state()
    if not st.get("ok"):
        raise SystemExit(json.dumps(st, ensure_ascii=False))
    doc = st["doc_dir"]
    doc_prefix = doc.replace("\\", "/").rstrip("/") + "/"
    if os.path.isfile(src) and not os.path.abspath(src).replace("\\", "/").lower(
            ).startswith(doc_prefix.lower()):
        staged = _stage(src, doc, write=not dry_run)
        if dry_run:
            # 预演不写盘，所以游戏侧"文件不存在"是必然的；把计划报出来就够判断了
            _out({"ok": True, "dry_run": True, "would_stage": staged,
                  "index": index,
                  "note": "源图在 doc_dir 外：真跑会先落这张 jpg 再写封面槽。"
                          "已经算出目标路径/字节数/md5。"})
            return
        print("[stage] %s (%d bytes, md5=%s)" % (staged["rel_path"], staged["bytes"],
                                                staged["md5"]))
        path = staged["rel_path"]
    elif os.path.isfile(src):
        path = os.path.abspath(src).replace("\\", "/")[len(doc_prefix):]
    else:
        path = src            # 已经是 doc_dir 下的相对路径（比如游戏相册里现成的图）
    return call("cover_set_slot", {"path": path, "index": index, "dry_run": bool(dry_run)})


def cmd_state(_a):
    _out(_state())


def cmd_set(a):
    _out(_set(a.src, a.index, a.dry_run))


def cmd_upload(a):
    _out(call("cover_upload", {"index": a.index, "dry_run": bool(a.dry_run)}))


def _poll(index, timeout):
    since = time.time() - 1
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = call("cover_upload_poll", {"index": index, "since": since})
        if isinstance(last, dict) and last.get("status") in ("uploaded", "no_capture"):
            return last
        time.sleep(1.0)
    return last


def cmd_poll(a):
    _out(_poll(a.index, a.timeout))


def cmd_apply(a):
    urls = a.url or None
    _out(call("cover_apply", {"urls": urls, "dry_run": bool(a.dry_run)}))


def cmd_from_file(a):
    doc = _state().get("doc_dir")
    if not doc:
        raise SystemExit("拿不到 doc_dir（桥接没跑？先进编辑器）")
    if os.path.isfile(a.src):
        staged = _stage(a.src, doc)
        print("[stage] %s (%d bytes, md5=%s)" % (staged["rel_path"], staged["bytes"], staged["md5"]))
        path = staged["rel_path"]
    else:
        path = a.src
    r = call("cover_set_slot", {"path": path, "index": a.index})
    print("[set]   %s" % json.dumps(r, ensure_ascii=False)[:400])
    if not (isinstance(r, dict) and r.get("ok")):
        raise SystemExit("写槽位失败，停止")
    since = time.time() - 1
    u = call("cover_upload", {"index": a.index})
    print("[upload] %s" % json.dumps(u, ensure_ascii=False)[:400])
    if not (isinstance(u, dict) and u.get("ok")):
        raise SystemExit("上传发起失败，停止")
    got = _poll(a.index, a.timeout)
    print("[poll]  %s" % json.dumps(got, ensure_ascii=False)[:500])
    if not (isinstance(got, dict) and got.get("status") == "uploaded"):
        raise SystemExit("没拿到上传回执 URL；封面槽位已写好，等回包后可单独跑 apply")
    r2 = call("cover_apply", {"urls": [got["share_url"]], "since": since})
    print("[apply] %s" % json.dumps(r2, ensure_ascii=False)[:500])
    print("[state] %s" % json.dumps(_state().get("slots"), ensure_ascii=False)[:400])


def main():
    p = argparse.ArgumentParser(prog="fxcover", description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("state").set_defaults(func=cmd_state)
    for name in ("set",):
        sp = sub.add_parser(name)
        sp.add_argument("src")
        sp.add_argument("--index", type=int, default=0)
        sp.add_argument("--dry-run", action="store_true")
        sp.set_defaults(func=cmd_set)
    sp = sub.add_parser("upload")
    sp.add_argument("--index", type=int, default=0)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_upload)
    sp = sub.add_parser("poll")
    sp.add_argument("--index", type=int, default=0)
    sp.add_argument("--timeout", type=float, default=90.0)
    sp.set_defaults(func=cmd_poll)
    sp = sub.add_parser("apply")
    sp.add_argument("--url", action="append")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_apply)
    sp = sub.add_parser("from-file")
    sp.add_argument("src")
    sp.add_argument("--index", type=int, default=0)
    sp.add_argument("--timeout", type=float, default=90.0)
    sp.set_defaults(func=cmd_from_file)
    a = p.parse_args()
    if not getattr(a, "func", None):
        p.print_help()
        return 2
    a.func(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
