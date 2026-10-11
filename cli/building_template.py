#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""building_template — 参数化建筑模板：由尺寸参数生成可摆放的构件清单。

设计原则（均来自实机实测，见 PLACEMENT.md / SCENE_REFERENCE.md）
--------------------------------------------------------------
1. **只用几何体**。实测 6 个样本（含官方模板「林间小屋」）一致表明：
   几何体占比 81.5%~99.5%，连官方模板都只用 4 个建筑构件。
   原因是几何体原点规则、基准尺寸整齐、可自由非等比缩放。
2. **定位统一用「包围盒底面中心」**，与 `place_exact.py` 的 `--base` 锚点完全一致，
   摆放时直接走闭环校正，不受模型原点偏移影响。
3. **尺寸 → ugc_size**：GEOMETRY 基础体原生全尺寸为 10，
   故 `scale = size / 10`（`size` 即构件实际世界尺寸）。

坐标约定：y 向上；`-z` 为正面（朝向前方）。
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_PATH = os.path.join(ROOT, "registry", "model_sizes.json")

GEOM = 301001        # 方块
ROUND_BOX = 301025   # 圆角矩形方块（官方模板用它做装饰/门窗套）
CYL = 301006         # 圆柱
CIRC = 301002        # 球体
TRI = 301009         # 等边三棱柱
GEOM_NATIVE = 10.0   # 几何体基础体的通用原生边长（仅作兜底）

DEFAULT_MATERIAL = 8001     # 白墙（模板里的通用材质序号）

_LIB = None


def _lib():
    global _LIB
    if _LIB is None:
        with open(LIB_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        _LIB = {r["item_id"]: r for r in d["items"] if "size" in r}
    return _LIB


def native_size(item_id):
    """查该构件的**原生全尺寸**；查不到时退回 (10,10,10)。

    坐标换算必须用它：`scale = 目标尺寸 ÷ 原生尺寸`。
    ⚠️ 不要假设所有几何体都是 10³ —— 301009 是 8.66×10×7.5、
    301024 是 11.004×1.004×5.502，硬编码 10 会让这类构件尺寸算错。
    """
    r = _lib().get(int(item_id))
    if not r:
        return [GEOM_NATIVE] * 3
    return [float(x) or GEOM_NATIVE for x in r["size"]]


def brick(name, at, size, rot=(0, 0, 0), anchor="base", item=GEOM,
          material=DEFAULT_MATERIAL, color=None, stage="shell"):
    """生成一个构件规格。

    at     : 定位点。`anchor="base"` 时是包围盒**底面中心**；
             `anchor="center"` 时是**几何中心**（旋转件必须用这个）。
    size   : 实际尺寸 (宽, 高, 深)，单位与世界坐标一致
    rot    : 欧拉角（度），轴序同引擎 xzy

    ⚠️ 为什么旋转件必须用 `anchor="center"`：
    "底面中心"在旋转后语义就变了（板一斜，底面方向不再是 -y），
    若仍按底面中心对齐，闭环修正会把板整体抬高（实测屋顶浮空、两坡不接）。
    """
    nat = native_size(item)
    return {
        "name": name,
        "item_id": item,
        "anchor": anchor,
        "at": [round(float(v), 4) for v in at],
        "size": [round(float(v), 4) for v in size],
        "scale": [round(float(size[i]) / nat[i], 6) for i in range(3)],
        "rotation": [float(v) for v in rot],
        "material": material,
        "color": color,
        "stage": stage,
    }


def room(width, depth, height, thickness=8.0, floor=True,
         door=None, windows=None, stage="shell", frames=True, y0=None):
    """一个空房间：可选地板 + 四面墙（可选门洞、窗洞）。

    y0: 墙的起始高度。默认等于 `thickness`（自建地板时）；
        若地板由外部提供（真实构件套装），需显式传地板厚度。
    """
    out = []
    T, W, D, H = thickness, width, depth, height
    cx, cz = 0.0, 0.0
    y0 = T if y0 is None else y0

    if floor:
        out.append(brick("floor", (cx, 0.0, cz), (W, T, D),
                         material=8008, stage=stage))

    y0 = T                                  # 墙从地板顶面起
    # -- 前/后墙（沿 x 方向展开），z = ∓D/2
    for side, z in (("front", -D / 2 + T / 2), ("back", D / 2 - T / 2)):
        out.extend(_wall_with_openings(
            "wall-" + side, axis="x", span=W, thick=T, height=H, y0=y0,
            fixed=z, center=(cx, cz), side=side,
            door=(door if door and door.get("side") == side else None),
            wins=[w for w in (windows or []) if w.get("side") == side],
            stage=stage, frames=frames))
    # -- 左/右墙（沿 z 方向展开），x = ∓W/2
    for side, x in (("left", -W / 2 + T / 2), ("right", W / 2 - T / 2)):
        out.extend(_wall_with_openings(
            "wall-" + side, axis="z", span=D, thick=T, height=H, y0=y0,
            fixed=x, center=(cx, cz), side=side,
            door=(door if door and door.get("side") == side else None),
            wins=[w for w in (windows or []) if w.get("side") == side],
            stage=stage, frames=frames))
    return out


def _frame_ring(name, axis, a, b, ya, yb, thick, y0, fixed, center,
                fw=4.0, fout=2.5, bottom=True, stage="trim"):
    """一处洞口的门窗套：左右竖框 + 上框（+ 可选下框），用圆角矩形方块并略凸出墙面。

    官方模板「林间小屋」里用了 172 个 301025 圆角矩形方块做这类收边，此处沿用同一构件。
    """
    out = []
    t = thick + fout
    w = b - a
    h = yb - ya
    if w < 2 * fw or h < 2 * fw:
        return out

    def put(nm, along_c, up0, s_along, s_up):
        if axis == "x":
            out.append(brick(nm, (center[0] + along_c, y0 + up0, fixed),
                             (s_along, s_up, t), item=ROUND_BOX,
                             material=8062, stage=stage))
        else:
            out.append(brick(nm, (fixed, y0 + up0, center[1] + along_c),
                             (t, s_up, s_along), item=ROUND_BOX,
                             material=8062, stage=stage))

    put(name + "-L", a + fw / 2.0, ya, fw, h)
    put(name + "-R", b - fw / 2.0, ya, fw, h)
    put(name + "-T", (a + b) / 2.0, yb - fw, w, fw)
    if bottom:
        put(name + "-B", (a + b) / 2.0, ya, w, fw)
    return out


def _wall_with_openings(name, axis, span, thick, height, y0, fixed, center,
                        side, door, wins, stage, frames=True):
    """把一面墙拆成若干矩形片段，为门/窗留洞。"""
    out = []
    cuts = []
    if door:
        dw = min(door.get("width", 24.0), span * 0.8)
        dh = min(door.get("height", height * 0.75), height * 0.95)
        off = door.get("offset", 0.0)
        cuts.append((off - dw / 2, off + dw / 2, 0.0, dh, True))
    for w in wins:
        ww = min(w.get("width", 30.0), span * 0.7)
        wh = min(w.get("height", 30.0), height * 0.7)
        sill = w.get("sill", height * 0.35)
        off = w.get("offset", 0.0)
        cuts.append((off - ww / 2, off + ww / 2, sill, sill + wh, False))
    cuts.sort(key=lambda c: c[0])

    def piece(nm, a, b, ya, yb):
        if b - a <= 0.01 or yb - ya <= 0.01:
            return
        s = b - a
        yb0 = y0 + ya
        hh = yb - ya
        mid = (a + b) / 2.0
        if axis == "x":
            out.append(brick(nm, (center[0] + mid, yb0, fixed), (s, hh, thick),
                             stage=stage))
        else:
            out.append(brick(nm, (fixed, yb0, center[1] + mid), (thick, hh, s),
                             stage=stage))

    cursor = -span / 2.0
    for i, (a, b, ya, yb, _isdoor) in enumerate(cuts):
        a = max(a, -span / 2.0)
        b = min(b, span / 2.0)
        if a > cursor:
            piece("%s-%d" % (name, i), cursor, a, 0.0, height)
        # 洞口上方的门楣 / 窗楣
        if yb < height:
            piece("%s-head-%d" % (name, i), a, b, yb, height)
        # 窗台下方
        if ya > 0:
            piece("%s-sill-%d" % (name, i), a, b, 0.0, ya)
        cursor = max(cursor, b)
    if cursor < span / 2.0:
        piece("%s-end" % name, cursor, span / 2.0, 0.0, height)

    # 门窗套（门不加下框，避免出现门槛）
    if frames:
        for i, (a, b, ya, yb, isdoor) in enumerate(cuts):
            aa = max(a, -span / 2.0)
            bb = min(b, span / 2.0)
            out.extend(_frame_ring("%s-fr%d" % (name, i), axis, aa, bb, ya, yb,
                                   thick, y0, fixed, center,
                                   bottom=not isdoor))
    return out


def roof_flat(width, depth, y, thickness=6.0, overhang=6.0, stage="roof"):
    return [brick("roof-flat", (0.0, y, 0.0),
                  (width + 2 * overhang, thickness, depth + 2 * overhang),
                  material=8014, stage=stage)]


def roof_gable(width, depth, y, rise, thickness=5.0, overhang=6.0,
               angle=None, stage="roof", max_piece=100.0):
    """双坡屋顶：两块倾斜板，**以几何中心定位**。

    檐口在 (z=±half, y)，屋脊在 (z=0, y+rise)。

    ⚠️ 超过 `max_piece` 时**沿 x 分块**。实测几何体**单块缩放过大时渲染不完整**：
    真实构件小屋屋顶宽 372（scale≈37）时只渲染出一半，缩到 186（scale≈18.6）仍不行；
    拆到每块 ≤60（scale ≤6）后正常。
    ⇒ 单块尺寸建议控制在 **≈100 以内（scale ≤10）**，这也是官方/玩家作品
    "小块拼装"习惯的由来。
    """
    import math
    half = depth / 2.0 + overhang
    if angle is None:
        angle = math.degrees(math.atan2(rise, half))
    slab_len = math.sqrt(half * half + rise * rise)
    total_w = width + 2 * overhang
    n = max(1, int(math.ceil(total_w / max_piece)))
    pw = total_w / n
    out = []
    for sgn, tag in ((-1, "north"), (1, "south")):
        for i in range(n):
            ox = (i - (n - 1) / 2.0) * pw
            out.append(brick("roof-%s-%d" % (tag, i),
                             (ox, y + rise / 2.0, sgn * half / 2.0),
                             (pw, thickness, slab_len),
                             rot=(sgn * angle, 0, 0),
                             anchor="center",
                             material=8014, stage=stage))
    return out


def gable_walls(width, depth, y, rise, thickness=6.0, steps=8, overhang=6.0,
                stage="roof"):
    """双坡顶的山墙：±x 两端用阶梯矩形填充「墙顶 ↔ 斜板下沿」之间的三角形。

    ⚠️ 形状是**屋脊最高、两端最低**（双坡）；初版误写成"从 0 递增到 rise"，
    那是**单坡**的形状，导致山墙两端高出屋面、露出一排阶梯（实拍发现）。

    ⚠️ 高度必须按**屋顶板的斜率**算（板跨 `depth+2*overhang`，山墙只跨 `depth`），
    否则山墙比屋顶陡、在屋脊端穿出板面。
    """
    half = depth / 2.0
    span_half = half + overhang

    def rh(z):
        """屋顶下沿在 z 处的相对高度（屋脊 z=0 处为 rise，檐口为 0）。"""
        return rise * (1.0 - abs(z) / span_half)

    out = []
    for sx in (-1, 1):
        x = sx * (width / 2.0 - thickness / 2.0)
        for i in range(steps):
            z0 = -half + depth * i / float(steps)
            z1 = -half + depth * (i + 1) / float(steps)
            h = max(rh(z0), rh(z1))          # 取该段最高处，略穿入板内被板遮住
            if h < 1.5:
                continue
            out.append(brick("gable-%s-%d" % ("l" if sx < 0 else "r", i),
                             (x, y, (z0 + z1) / 2.0),
                             (thickness, h, z1 - z0),
                             material=DEFAULT_MATERIAL, stage=stage))
    return out


def roof_shed(width, depth, y, rise, thickness=5.0, overhang=6.0,
              angle=None, stage="roof"):
    """单坡屋顶：一整块斜板，从 −z 檐口（低）升到 +z 屋脊（高），几何中心定位。

    ⚠️ 角度必须取负：行向量约定下绕 x 轴**正向**旋转会让 +z 端**下降**
    （实拍验证：正角度得到的是"掀开的盖子"，板反向悬空）。
    """
    import math
    span = depth + 2 * overhang
    if angle is None:
        angle = math.degrees(math.atan2(rise, span))
    slab_len = math.sqrt(span * span + rise * rise)
    return [brick("roof-shed", (0.0, y + rise / 2.0, 0.0),
                  (width + 2 * overhang, thickness, slab_len),
                  rot=(-angle, 0, 0), anchor="center",
                  material=8014, stage=stage)]


def shed_walls(width, depth, y, rise, thickness=6.0, steps=8, overhang=6.0,
               stage="roof"):
    """单坡顶的山墙：±x 两端用阶梯矩形填充「墙顶 ↔ 斜板下沿」之间的空隙。

    ⚠️ 高度必须按**屋顶板的斜率**算（板跨 `depth+2*overhang`，山墙只跨 `depth`）：
    若直接把 rise 按 depth 分摊，山墙会比屋顶更陡、在屋脊端**穿出板面**
    （实拍验证：初版山墙在屋顶上方露出一整排阶梯）。
    """
    half = depth / 2.0
    span = depth + 2 * overhang

    def rh(z):
        """屋顶中心线在 z 处的相对高度。"""
        return rise * (z + span / 2.0) / span

    out = []
    for sx in (-1, 1):
        x = sx * (width / 2.0 - thickness / 2.0)
        for i in range(steps):
            z0 = -half + depth * i / float(steps)
            z1 = -half + depth * (i + 1) / float(steps)
            h = rh(z1)                 # 取该段高端，略微穿入板内被板遮住
            if h < 1.5:
                continue
            out.append(brick("shedwall-%s-%d" % ("l" if sx < 0 else "r", i),
                             (x, y, (z0 + z1) / 2.0),
                             (thickness, h, z1 - z0),
                             material=DEFAULT_MATERIAL, stage=stage))
    return out


def cabin(width=120.0, depth=100.0, height=70.0, thickness=8.0,
          roof="gable", roof_rise=None, overhang=8.0,
          door_width=26.0, door_height=52.0, windows=2,
          base=(0.0, 0.0, 0.0), frames=True):
    """一间完整小屋（地板 + 四墙 + 门 + 若干窗 + 门窗套 + 屋顶）。

    roof: "gable" 双坡（含山墙）/ "flat" 平顶 / "shed" 单坡
    frames: 是否加门窗套（用 301025 圆角矩形方块，官方模板同款做法）
    """
    wins = []
    if windows >= 1:
        wins.append({"side": "front", "width": 30.0, "height": 26.0,
                     "sill": 22.0, "offset": -width * 0.3})
    if windows >= 2:
        wins.append({"side": "front", "width": 30.0, "height": 26.0,
                     "sill": 22.0, "offset": width * 0.3})
    if windows >= 3:
        wins.append({"side": "left", "width": 28.0, "height": 26.0,
                     "sill": 22.0})
    parts = room(width, depth, height, thickness=thickness, frames=frames,
                 door={"side": "front", "width": door_width,
                       "height": door_height, "offset": 0.0},
                 windows=wins)
    roof_y = thickness + height
    if roof == "flat":
        parts += roof_flat(width, depth, roof_y, overhang=overhang)
    elif roof == "shed":
        rise = roof_rise if roof_rise else depth * 0.45
        parts += roof_shed(width, depth, roof_y, rise, overhang=overhang)
        parts += shed_walls(width, depth, roof_y, rise, thickness=thickness,
                            overhang=overhang)
    else:
        rise = roof_rise if roof_rise else depth * 0.32
        parts += roof_gable(width, depth, roof_y, rise, overhang=overhang)
        parts += gable_walls(width, depth, roof_y, rise, thickness=thickness,
                             overhang=overhang)

    ox, oy, oz = base
    for p in parts:
        p["at"] = [round(p["at"][0] + ox, 4),
                   round(p["at"][1] + oy, 4),
                   round(p["at"][2] + oz, 4)]
    return parts


PRESETS = {
    "cabin": dict(width=120.0, depth=100.0, height=70.0, thickness=8.0,
                  roof="gable", windows=2),
    "hut": dict(width=70.0, depth=60.0, height=48.0, thickness=6.0,
                roof="gable", windows=1, door_width=22.0, door_height=40.0),
    "hall": dict(width=220.0, depth=140.0, height=110.0, thickness=10.0,
                 roof="flat", windows=3, door_width=34.0, door_height=70.0),
}


# ============================================================ 真实构件套装
# 实机实测：真实构件的原点大多在**底面中心或几何中心**、偏移量 ≤0.6，
# 但静态 center 未必等于实体偏移（见 PLACEMENT.md），故 build_house 对
# **非几何体构件一律走闭环校正**。
REAL_KIT = {
    "floor": 10247,   # 瓷砖地板           170.16 × 1.00 × 83.03（原点=几何中心）
    "wall": 10217,    # 墙34              169.36 × 51.82 × 4.48（原点=底面中心，自带窗）
    "ceil": 92026,    # 轮船船室天花板      302.66 × 8.16 × 233.52
    "roof": 10214,    # 屋顶1             228.94 × 33.80 × 88.38（原点=底面中心）
}


def tile_area(item, x0, z0, x1, z1, y, name="tile", stage="floor"):
    """用构件 `item` 平铺覆盖 [x0,x1]×[z0,z1]；**数量按构件原生尺寸自适应**。

    真实构件不能任意缩放，所以是"用多块拼出面积"，而不是"拉大一块"。
    """
    import math
    nat = native_size(item)
    sx, sz = nat[0], nat[2]
    nx = max(1, int(math.ceil((x1 - x0) / sx - 1e-6)))
    nz = max(1, int(math.ceil((z1 - z0) / sz - 1e-6)))
    cx, cz = (x0 + x1) / 2.0, (z0 + z1) / 2.0
    out = []
    for i in range(nx):
        for j in range(nz):
            px = cx + (i - (nx - 1) / 2.0) * sx
            pz = cz + (j - (nz - 1) / 2.0) * sz
            out.append(brick("%s-%d-%d" % (name, i, j), (px, y, pz),
                             tuple(nat), item=item, stage=stage))
    return out


def wall_run(item, length, y, fixed, name="wall", along="x", stage="shell"):
    """用构件 `item` 沿 x 或 z 方向拼出长度约 `length` 的墙。

    along="x" → 墙沿 x 展开，`fixed` 为 z 坐标；
    along="z" → 墙沿 z 展开，`fixed` 为 x 坐标（构件绕 y 轴旋转 90°）。
    """
    nat = native_size(item)
    # 构件的「长度」始终是 nat[0]：沿 z 时构件绕 y 轴转 90°，长度方向随之转到 z。
    ln = nat[0]
    n = max(1, int(round(length / ln)))
    out = []
    for i in range(n):
        off = (i - (n - 1) / 2.0) * ln
        if along == "x":
            out.append(brick("%s-%d" % (name, i), (off, y, fixed),
                             tuple(nat), item=item, stage=stage))
        else:
            # ⚠️ 沿 z 的墙是**旋转 90°**，不是交换尺寸分量：
            # 交换分量会让 scale 变成 (4.48/169.36, 1, 169.36/4.48)，直接超出缩放区间 (0.1,1000)。
            out.append(brick("%s-%d" % (name, i), (fixed, y, off),
                             tuple(nat), rot=(0, 90, 0),
                             item=item, stage=stage))
    return out


def hut_real(base=(0.0, 0.0, 0.0), kit=None, roof="none", with_ceil=False,
             overhang=8.0, tiles_x=2, tiles_z=4, door_width=52.0, stage="real"):
    """用**真实建筑构件**搭一间小屋（红木墙 + 红瓷砖地板）。

    ⚠️ **默认不封顶（roof="none"）**：这是刻意的 —— 便于从上方观察内部构造，
    也便于学习真实构件的摆放方式。需要顶时用 `roof="gable"/"flat"/"real"`。

    **门洞的做法**：真实墙板 10217 是固定尺寸构件，**无法在其中挖洞**，
    因此前墙改用**两块缩放到位的墙板，中间留出 `door_width` 的空隙**。
    缩放幅度很小（约 0.85）→ 不会产生大缩放的渲染缺陷。

    尺寸由构件模数决定：开间 = 地板宽 × tiles_x、进深 = 地板深 × tiles_z、
    层高 = 墙板高 51.82。
    """
    kit = kit or REAL_KIT
    fs = native_size(kit["floor"])
    ws = native_size(kit["wall"])
    W = fs[0] * tiles_x           # 开间
    D = fs[2] * tiles_z           # 进深
    H = ws[1]                     # 层高 = 墙板高
    parts = []
    parts += tile_area(kit["floor"], -W / 2, -D / 2, W / 2, D / 2, 0.0)
    y0 = fs[1]
    zf = -(D / 2 - ws[2] / 2)                 # 前墙 z

    # 前墙：两块**缩放到位**的墙板 + 中间留出 door_width 的门洞
    seg = (W - door_width) / 2.0
    for tag, cx_ in (("L", -(door_width / 2.0 + seg / 2.0)),
                     ("R", door_width / 2.0 + seg / 2.0)):
        parts.append(brick("wall-front-" + tag, (cx_, y0, zf),
                           (seg, ws[1], ws[2]), item=kit["wall"], stage=stage))

    # 后墙（整段）
    parts += wall_run(kit["wall"], W, y0, D / 2 - ws[2] / 2,
                      name="wall-back", along="x")
    # 左右墙（整段，绕 y 轴旋转 90°）
    for tag, x in (("left", -(W / 2 - ws[2] / 2)), ("right", W / 2 - ws[2] / 2)):
        parts += wall_run(kit["wall"], D, y0, x, name="wall-" + tag, along="z")

    top = fs[1] + H                       # 墙顶高度
    if with_ceil:
        cs = native_size(kit["ceil"])
        parts += tile_area(kit["ceil"], -W / 2, -D / 2, W / 2, D / 2, top,
                           name="ceil", stage="roof")
        top += cs[1]
    if roof == "gable":
        rise = D * 0.30
        parts += roof_gable(W, D, top, rise, overhang=overhang, max_piece=60.0)
        parts += gable_walls(W, D, top, rise, thickness=max(ws[2], 8.0),
                             overhang=overhang)
    elif roof == "flat":
        parts += roof_flat(W, D, top, overhang=overhang)

    ox, oy, oz = base
    for p in parts:
        p["at"] = [round(p["at"][0] + ox, 4),
                   round(p["at"][1] + oy, 4),
                   round(p["at"][2] + oz, 4)]
    return parts


REAL_PRESETS = {
    "hut": {},
}


def hut_hybrid(base=(0.0, 0.0, 0.0), kit=None, roof="gable", width=None,
               depth=None, height=60.0, thickness=8.0,
               door_width=34.0, door_height=46.0, windows=2, frames=True,
               stage="real"):
    """**真实构件 + 几何体混合**小屋 —— **带门和窗**。

    - **地板、屋顶用真实构件**（10247 瓷砖地板 / 10214 屋顶1），**不缩放**
      → 规避"几何体大缩放渲染不完整"的缺陷。
    - **墙用几何体**：真实墙板 10217 是**固定尺寸**构件，无法在其中挖门窗洞；
      而几何体可自由切分成带洞的墙段（`_wall_with_openings`）。
    - 山墙用几何体小块填充。

    roof: "real" 真实屋顶构件 10214 / "gable" 几何体双坡 / "none"
    """
    import math
    kit = kit or REAL_KIT
    rid = kit.get("roof", 10214)
    fs = native_size(kit["floor"])
    rs = native_size(rid)
    W = float(width or fs[0])
    D = float(depth or fs[2] * 2)
    H = float(height)
    parts = []

    # 1) 地板：真实构件平铺
    parts += tile_area(kit["floor"], -W / 2, -D / 2, W / 2, D / 2, 0.0)

    # 2) 墙：几何体，带门洞与窗洞（从地板顶面起）
    wins = []
    if windows >= 1:
        wins.append({"side": "front", "width": 30.0, "height": 26.0,
                     "sill": H * 0.40, "offset": -W * 0.28})
    if windows >= 2:
        wins.append({"side": "front", "width": 30.0, "height": 26.0,
                     "sill": H * 0.40, "offset": W * 0.28})
    if windows >= 3:
        wins.append({"side": "left", "width": 28.0, "height": 26.0,
                     "sill": H * 0.40})
    parts += room(W, D, H, thickness=thickness, floor=False, y0=fs[1],
                  door={"side": "front", "width": door_width,
                        "height": door_height, "offset": 0.0},
                  windows=wins, frames=frames)

    # 3) 屋顶
    top = fs[1] + H
    if roof == "real":
        nz = max(1, int(math.ceil(D / rs[2])))
        for j in range(nz):
            z = -D / 2.0 + (j + 0.5) * (D / nz)
            parts.append(brick("roof-%d" % j, (0.0, top, z), tuple(rs),
                               item=rid, material=8014, stage="roof"))
    elif roof == "gable":
        rise = D * 0.30
        parts += roof_gable(W, D, top, rise, overhang=8.0, max_piece=60.0)
        parts += gable_walls(W, D, top, rise, thickness=thickness,
                             overhang=8.0, steps=8)

    ox, oy, oz = base
    for p in parts:
        p["at"] = [round(p["at"][0] + ox, 4),
                   round(p["at"][1] + oy, 4),
                   round(p["at"][2] + oz, 4)]
    return parts
