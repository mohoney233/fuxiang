#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览渲染的主机侧编排与绘图。

分工：
  * 游戏侧（bridge/fuxiang_preview.py）只做短操作：摆相机、提交出图、还原相机。
  * 本模块跑在 MCP 服务器进程里，负责**跨帧等待**、**文件轮询**与**离线绘图**。
    这些等待绝不能放进游戏主线程 —— 项目纪律：游戏主线程只做短操作。

路线 A（引擎镜头快照）：pose → 等 2 帧 → fire → 轮询落盘 → 下一个视角
路线 C（2D 俯视平面图）：取组件数据 → 用 Pillow 在主机侧绘制

⚠️ 不创建相机、不销毁相机、不创建/释放 RenderTarget、不回读像素。
   这四件事在本客户端会分别触发「非 render frame」报错、回读未实现、以及进程崩溃。
"""
import json
import os
import time

# 2026-09-22：按 __file__ 推导（本模块在主机侧运行，__file__ 一定可用）
PREVIEW_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview")

#: 摆位与出图之间的等待。引擎在摆位后的下一帧才用新相机绘制，
#: 实测 0.35~0.5 秒（60fps 下约 21~30 帧）足够稳定。
FRAME_WAIT = 0.45

#: 出图文件轮询超时。引擎异步落盘，实测滞后 1~2 秒。
FILE_WAIT = 12.0

#: 主机侧可等待的取景参数（都会原样传给游戏侧）
FRAME_KEYS = ("fit", "margin", "fov", "aspect", "elevation", "dist_mul", "span_mul",
              "sentinel_abs", "y_sentinel_abs", "include_groups")


def _abs_path(path, default_name):
    if not path:
        path = os.path.join(PREVIEW_DIR, default_name)
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _wait_file(path, timeout=FILE_WAIT):
    """轮询等待引擎把图片写完。返回 (ok, bytes, waited)。"""
    t0 = time.time()
    last = -1
    stable = 0
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 0 and size == last:
                stable += 1
                if stable >= 2:          # 连续两次大小一致，视为写完
                    return True, size, round(time.time() - t0, 2)
            else:
                stable = 0
            last = size
        time.sleep(0.12)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True, os.path.getsize(path), round(time.time() - t0, 2)
    return False, 0, round(time.time() - t0, 2)


def _frame_args(args):
    return {k: args[k] for k in FRAME_KEYS if k in args and args[k] is not None}


def plan(call_edit, args):
    """只算不拍：包围盒 + 视角列表。"""
    return call_edit("preview_plan", _frame_args(args) | {"views": args.get("views")}, timeout=30)


def state(call_edit, args):
    return call_edit("preview_state", {}, timeout=30)


def restore(call_edit, args):
    return call_edit("preview_restore", {"clear": bool(args.get("clear", True))}, timeout=30)


def _sys_avail_mb():
    """主机侧系统可用物理内存（MB），拿不到返回 None。

    背景：2026-09-19 17:05 连续第二张出图时 dwrg.exe 原生崩溃（UniSDK
    CrashDump/00960/native.dmp），崩溃时全系统可用内存仅 552MB——核显（Intel
    Graphics / D3D11）+ 21 小时长会话下，渲染崩溃与内存压力强相关。出图前把
    余量报给调用方，低时附 warning，让「先保存后截图」的顺序决策有数据可依。
    """
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:
        pass
    return None


MEM_WARN_MB = 1024


def shot(call_edit, args):
    """拍一个视角：摆位 → 跨帧等待 → 出图 → 轮询落盘。

    返回 dict，含 path / bytes / view / waited / accepted / pose。
    """
    view = args.get("view")
    if not view:
        raise ValueError("缺少 view（视角名或视角对象；取自 fuxiang_preview_plan 的 views）")
    wait = float(args.get("wait") if args.get("wait") is not None else FRAME_WAIT)
    file_wait = float(args.get("timeout") if args.get("timeout") is not None else FILE_WAIT)

    pose_args = _frame_args(args)
    pose_args["view"] = view
    if args.get("force_backup"):
        pose_args["force_backup"] = True
    pose = call_edit("preview_pose", pose_args, timeout=30)

    time.sleep(wait)

    name = view if isinstance(view, str) else (view.get("name") or "view")
    out = _abs_path(args.get("out"),
                    "%s_%s.png" % (args.get("prefix") or "preview", name))
    avail = _sys_avail_mb()
    fired = call_edit("preview_fire", {"path": out, "format": 3}, timeout=30)
    ok, size, waited = _wait_file(out, file_wait)

    result = {"view": name, "path": out, "ok": bool(ok), "bytes": size,
              "waited": waited, "accepted": bool(fired.get("accepted")),
              "pose": {"position": pose.get("applied", {}).get("position"),
                       "mode": pose.get("mode")}}
    if avail is not None:
        result["sys_avail_mb"] = avail
        if avail < MEM_WARN_MB:
            result["warning"] = ("系统可用内存仅 %dMB（<%dMB）。核显长会话下出图崩溃风险高"
                                 "（2026-09-19 实机崩过一次）：确保地图已保存，"
                                 "多张截图之间考虑重启游戏进程后再补拍。" % (avail, MEM_WARN_MB))
    if not ok:
        result["error"] = ("引擎已受理但文件未落盘（等待 %.1fs）。"
                           "检查路径可写性，或先确认游戏窗口未被最小化。" % file_wait)
    if args.get("restore"):
        result["restore"] = restore(call_edit, {})
    return result


def sweep(call_edit, args):
    """一键拍全套：规划 → 逐视角出图 → 默认还原相机。"""
    fit = args.get("fit") or "content"
    plan_args = _frame_args(args)
    plan_args["fit"] = fit
    if args.get("views"):
        plan_args["views"] = args["views"]
    planned = call_edit("preview_plan", plan_args, timeout=30)
    if not planned.get("ok"):
        raise RuntimeError("取景规划失败：%s" % json.dumps(planned, ensure_ascii=False)[:500])

    out_dir = os.path.abspath(args.get("out_dir")
                              or os.path.join(PREVIEW_DIR, "shots"))
    os.makedirs(out_dir, exist_ok=True)
    prefix = args.get("prefix") or "preview"
    stamp = time.strftime("%Y%m%d_%H%M%S")

    shots = []
    errors = []
    for view in planned["views"]:
        if view.get("mode") == "keep" and not args.get("include_current"):
            continue
        one = dict(args)
        one["view"] = view
        one["out"] = os.path.join(out_dir, "%s_%s_%s.png" % (prefix, view["name"], stamp))
        one["restore"] = False
        try:
            shots.append(shot(call_edit, one))
        except Exception as exc:
            errors.append({"view": view.get("name"), "error": str(exc)[:400]})

    restored = None
    if args.get("restore", True):
        try:
            restored = restore(call_edit, {})
        except Exception as exc:
            restored = {"ok": False, "error": str(exc)[:300]}

    manifest = {
        "ok": not errors,
        "fit": planned.get("fit"),
        "bounds": planned.get("bounds"),
        "content_count": planned.get("content_count"),
        "boundary_count": planned.get("boundary_count"),
        "non_spatial_count": planned.get("non_spatial_count"),
        "out_dir": out_dir,
        "shot_count": len(shots),
        "shots": shots,
        "errors": errors,
        "restored": restored,
    }
    manifest_path = os.path.join(out_dir, "%s_%s_manifest.json" % (prefix, stamp))
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    manifest["manifest_path"] = manifest_path
    return manifest


# --------------------------------------------------------------------------
# 路线 C：2D 俯视平面图（主机侧绘制）
# --------------------------------------------------------------------------
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

#: 标记配色（浅色底 + 深色描边，适配浅色主题）。
#: 按游戏侧下发的 item_group 分组，角色类单独识别（component_class 含 Civilian）。
_GROUP_STYLE = {
    "scene": ("道具 / 场景", "#E6F1FB", "#378ADD"),
    "global": ("全局组件", "#FAEEDA", "#BA7517"),
    "ui": ("界面组件", "#F1EFE8", "#B4B2A9"),
    "restrict": ("限制区域", "#FCEBEB", "#E24B4A"),
}
_ROLE_STYLE = ("角色", "#FBEAF0", "#D4537E")
_BOUNDARY_STYLE = ("地图边界锚点", "#FFFFFF", "#BA7517")
_OTHER_STYLE = ("其它", "#EEEDFE", "#7F77DD")


def _style_for(obj):
    if obj.get("role") == "boundary":
        return _BOUNDARY_STYLE
    cls = obj.get("component_class") or ""
    if "Civilian" in cls or "Character" in cls or "Player" in cls:
        return _ROLE_STYLE
    return _GROUP_STYLE.get(obj.get("item_group") or "", _OTHER_STYLE)


def _font(size):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _nice_step(span, target_lines=10):
    raw = span / max(target_lines, 1)
    mag = 10 ** int(_floor_log10(raw)) if raw > 0 else 1
    for mult in (1, 2, 5, 10):
        if raw <= mag * mult:
            return mag * mult
    return mag * 10


def _floor_log10(x):
    import math
    return math.floor(math.log10(x)) if x > 0 else 0


def layout(call_edit, args):
    """出 2D 俯视平面图 PNG。数据取自游戏侧，绘制在本进程完成。"""
    data = call_edit("preview_layout", _frame_args(args), timeout=60)
    if not data.get("ok"):
        raise RuntimeError("取布局数据失败：%s" % json.dumps(data, ensure_ascii=False)[:400])

    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise RuntimeError("本机缺少 Pillow，无法绘制平面图：%s" % exc)

    height = int(args.get("height") or 0)
    panel_w = int(args.get("panel_width") or 520)
    pad = 60
    header_h = 96
    legend_h = 36
    plot_top = header_h + legend_h

    objects = data.get("objects") or []
    boundary = data.get("boundary") or []
    non_spatial = data.get("non_spatial") or []

    # 取景范围：默认含地图边界锚点；fit=content 时只看内容
    fit = (args.get("fit") or "union").lower()
    if fit == "content":
        pts = list(objects)
    elif fit == "map":
        pts = list(boundary)
    else:
        pts = list(objects) + list(boundary)
    if not pts:
        raise RuntimeError("没有可绘制的对象（fit=%s）" % fit)

    xs = [p["position"][0] for p in pts]
    zs = [p["position"][2] for p in pts]
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)
    span_x = max(max_x - min_x, 1.0)
    span_z = max(max_z - min_z, 1.0)

    # 画布尺寸：默认跟随内容长宽比，避免大片留白；也可用 width/height 显式指定
    if not height:
        ratio = span_z / max(span_x, 1e-6)
        height = int(min(max(900, ratio * 620 + plot_top + pad * 2), 2100))
    avail_h = height - plot_top - pad * 2
    if args.get("width"):
        width = int(args["width"])
    else:
        s0 = avail_h / span_z
        width = int(min(max(span_x * s0 + pad * 2 + panel_w, 1500), 2900))

    plot_w = width - panel_w
    scale = min((plot_w - pad * 2) / span_x, avail_h / span_z)
    plot_h = height - plot_top - pad
    ox = pad + (plot_w - pad * 2 - span_x * scale) / 2.0
    oy = plot_top + pad + (avail_h - span_z * scale) / 2.0

    def to_px(x, z):
        # 与引擎正俯视一致：+X 向右，+Z 向上
        return (ox + (x - min_x) * scale,
                oy + (max_z - z) * scale)

    img = Image.new("RGB", (width, height), "#FFFFFF")
    d = ImageDraw.Draw(img)
    f_title = _font(26)
    f_body = _font(17)
    f_small = _font(14)

    # ---- 表头 ----
    d.rectangle([0, 0, width, header_h], fill="#F1EFE8")
    d.text((pad, 20), "随笔浮想 地图俯视平面图", font=f_title, fill="#2C2C2A")
    d.text((pad, 56), "空间物件 %d 个 · 地图边界锚点 %d 个 · 非空间组件 %d 个"
           % (len(objects), len(boundary), len(non_spatial)),
           font=f_body, fill="#5F5E5A")
    d.text((pad, 76), "X [%.1f, %.1f]   Z [%.1f, %.1f]   %.3f px/单位"
           % (min_x, max_x, min_z, max_z, scale),
           font=f_small, fill="#888780")
    d.line([0, header_h, width, header_h], fill="#B4B2A9", width=1)

    # ---- 分类图例（独立条带，避免压住网格标注）----
    d.rectangle([0, header_h, width - panel_w, plot_top], fill="#FAFAF8")
    d.line([0, plot_top, width - panel_w, plot_top], fill="#E4E2DC", width=1)
    lx, ly = pad, header_h + 10
    drawn = []
    for obj in (objects + boundary):
        style = _style_for(obj)
        if style[0] in drawn:
            continue
        drawn.append(style[0])
        _, fill, stroke = style
        d.rectangle([lx, ly, lx + 16, ly + 16], fill=fill, outline=stroke)
        d.text((lx + 22, ly - 1), style[0], font=f_small, fill="#444441")
        try:
            lx += 22 + int(d.textlength(style[0], font=f_small)) + 26
        except Exception:
            lx += 110
    if not drawn:
        d.text((pad, ly - 1), "（无空间物件）", font=f_small, fill="#888780")

    # ---- 坐标网格 ----
    step = _nice_step(max(span_x, span_z))
    gx = (int(min_x // step) - 1) * step
    while gx <= max_x + step:
        px, _ = to_px(gx, min_z)
        if pad <= px <= width - panel_w - pad:
            d.line([px, plot_top, px, height], fill="#E4E2DC", width=1)
            d.text((px + 3, plot_top + 4), "%g" % gx, font=f_small, fill="#888780")
        gx += step
    gz = (int(min_z // step) - 1) * step
    while gz <= max_z + step:
        _, py = to_px(min_x, gz)
        if plot_top <= py <= height - pad:
            d.line([0, py, width - panel_w, py], fill="#E4E2DC", width=1)
            d.text((4, py - 18), "%g" % gz, font=f_small, fill="#888780")
        gz += step

    # ---- 地图边界（由 4 个边界锚点围成的四边形）----
    if boundary:
        bxs = [b["position"][0] for b in boundary]
        bzs = [b["position"][2] for b in boundary]
        p1 = to_px(min(bxs), min(bzs))
        p2 = to_px(max(bxs), max(bzs))
        d.rectangle([min(p1[0], p2[0]), min(p1[1], p2[1]),
                     max(p1[0], p2[0]), max(p1[1], p2[1])],
                    outline="#BA7517", width=2)
        d.text((min(p1[0], p2[0]) + 6, min(p1[1], p2[1]) + 6),
               "地图边界 %.0f × %.0f" % (max(bxs) - min(bxs), max(bzs) - min(bzs)),
               font=f_small, fill="#854F0B")

    # ---- 物件标记 ----
    # 用屏幕像素定尺寸而不是世界单位：地图尺度与道具尺度差两个数量级，
    # 按世界单位画会小到看不见。相对大小仍由物件的 scale 体现。
    base_px = max(min(plot_w, avail_h) / 40.0, 10.0)
    for obj in objects:
        x, z = obj["position"][0], obj["position"][2]
        cx, cy = to_px(x, z)
        scale_v = obj.get("scale") or [1.0, 1.0, 1.0]
        try:
            sv = max((abs(float(scale_v[0])) + abs(float(scale_v[2]))) / 2.0, 0.2)
        except Exception:
            sv = 1.0
        half = max(base_px * min(sv, 2.5), 8.0)
        yaw = 0.0
        rot = obj.get("rotation") or [0.0, 0.0, 0.0]
        try:
            yaw = float(rot[1])
        except Exception:
            yaw = 0.0

        _, fill, stroke = _style_for(obj)
        # 俯视图里绕 Y 轴旋转；+Z 向上 → 屏幕角取负
        import math as _m
        rad = _m.radians(-yaw)
        ca, sa = _m.cos(rad), _m.sin(rad)
        corners = []
        for dx, dz in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            lx, lz = dx * half, dz * half
            corners.append((cx + (lx * ca - lz * sa), cy - (lx * sa + lz * ca)))
        d.polygon(corners, fill=fill, outline=stroke)
        # 朝向指示：物体正面（局部 -Z 方向）旋转后的指向
        fx = cx + half * 1.7 * sa
        fy = cy + half * 1.7 * ca
        d.line([cx, cy, fx, fy], fill=stroke, width=2)

        label = "#%s %s" % (obj.get("ugc_id"), obj.get("name_cn") or obj.get("item_id"))
        d.text((cx + half + 6, cy - 9), label, font=f_small, fill="#2C2C2A")

    # ---- 右侧明细面板 ----
    panel_x = width - panel_w
    d.rectangle([panel_x, 0, width, height], fill="#FAFAF8")
    d.line([panel_x, 0, panel_x, height], fill="#B4B2A9", width=1)

    y = 24
    d.text((panel_x + 20, y), "空间物件 (%d)" % len(objects), font=f_body, fill="#2C2C2A")
    y += 30
    for idx, obj in enumerate(objects):
        if y > height - 150:
            d.text((panel_x + 20, y), "... 其余 %d 个省略" % (len(objects) - idx),
                   font=f_small, fill="#888780")
            break
        name = obj.get("name_cn") or str(obj.get("item_id"))
        d.text((panel_x + 20, y), "#%s" % obj.get("ugc_id"), font=f_small, fill="#444441")
        d.text((panel_x + 76, y), name[:16], font=f_small, fill="#2C2C2A")
        p = obj["position"]
        d.text((panel_x + 20, y + 18),
               "  (%.1f, %.1f, %.1f)  旋转 %.0f°  缩放 %.2f"
               % (p[0], p[1], p[2],
                  (obj.get("rotation") or [0, 0, 0])[1] or 0,
                  ((obj.get("scale") or [1, 1, 1])[0] or 1)),
               font=f_small, fill="#5F5E5A")
        y += 40

    y += 12
    d.line([panel_x + 20, y, width - 20, y], fill="#E4E2DC", width=1)
    y += 16
    d.text((panel_x + 20, y), "非空间组件 (%d)" % len(non_spatial),
           font=f_body, fill="#2C2C2A")
    y += 28
    names = {}
    for ns in non_spatial:
        key = ns.get("name_cn") or str(ns.get("item_id"))
        names[key] = names.get(key, 0) + 1
    for key, cnt in list(names.items())[:18]:
        if y > height - 40:
            break
        text = "%s%s" % (key, "" if cnt == 1 else " ×%d" % cnt)
        d.text((panel_x + 20, y), text[:22], font=f_small, fill="#5F5E5A")
        y += 20

    out = _abs_path(args.get("out"), "layout_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    img.save(out, "PNG")

    report = dict(data)
    report.update({"png": out, "png_bytes": os.path.getsize(out),
                   "canvas": [width, height], "scale_px_per_unit": round(scale, 4)})
    report_path = out[:-4] + ".json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    report["report_path"] = report_path
    return report
