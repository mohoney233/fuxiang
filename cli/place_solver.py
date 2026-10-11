#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""place_solver — 摆放参数求解器：由「目标位置/目标尺寸」反解正确的 position 与 ugc_size。

为什么需要
----------
1698 个构件里 **68.6% 的模型原点不在几何中心**，偏移量中位数 9.57 世界单位
（约等于一个方块 301001 的边长），P90 达 45.27，最大 277。
若把目标点直接当作 `position` 摆放，物体会整体错位 `center × ugc_size`
—— 这就是历次搭建「错位」的根因。

公式
----
    世界几何范围 = position + rot(本地锚点 × scale)
    ⇒ 要让本地锚点 A（缩放后）落在世界点 T：
        position = T − M · (A × scale)
      · 锚点 = 几何中心：A = center
      · 锚点 = 底面中心：A = center − (0, half.y, 0)

旋转约定（已用 x90 / y90 / z90 / (30,45,60) / (-29.29,0,0) 实测反解，误差 0.000000）
    引擎 math3d.euler_to_matrix 为**行向量约定**，轴序 **xzy**：
        M = (Rx(rx) · Rz(rz) · Ry(ry))ᵀ
        世界偏移 = 本地偏移 @ M        （v' = v · M）

用法
----
    python cli/place_solver.py show 301001
    python cli/place_solver.py calc 10214 --center 580,20,-170 --size 200,30,80
    python cli/place_solver.py calc 10215 --base 580,0,-170 --rot 0,90,0 --height 60
    python cli/place_solver.py verify
"""
import os
import sys
import json
import math
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_PATH = os.path.join(ROOT, "registry", "model_sizes.json")
XFORM_PROBE = os.path.join(ROOT, "probe", "lib_xform_probe.out.json")

_CACHE = {}


# ---------------------------------------------------------------- 库
def load_library(refresh=False):
    if refresh or "items" not in _CACHE:
        with open(LIB_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        _CACHE["items"] = {r["item_id"]: r for r in d["items"] if "size" in r}
        _CACHE["meta"] = d
    return _CACHE["items"]


def get(item_id, lib=None):
    lib = lib or load_library()
    rec = lib.get(int(item_id))
    if rec is None:
        raise KeyError("item_id %s 不在尺寸库中（可用 --preset all 重新采集）" % item_id)
    return rec


# ---------------------------------------------------------------- 旋转
def _rx(t):
    c, s = math.cos(t), math.sin(t)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _mul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _transpose(A):
    return [[A[j][i] for j in range(3)] for i in range(3)]


def euler_matrix(rx, ry, rz):
    """复现引擎 math3d.euler_to_matrix（输入为度）。轴序 xzy，返回转置形式。"""
    return _transpose(_mul(_rx(math.radians(rx)),
                           _mul(_rz(math.radians(rz)), _ry(math.radians(ry)))))


def rotate(v, M):
    """行向量约定旋转：v @ M。"""
    return [sum(v[k] * M[k][j] for k in range(3)) for j in range(3)]


def inv_rotate(w, M):
    """行向量约定的逆旋转：w @ Mᵀ（用于从世界偏移反推本地偏移）。"""
    return rotate(w, _transpose(M))


# ---------------------------------------------------------------- 求解
def solve(item_id, center=None, base=None, size=None, ugc_size=None,
          rotation=None, lib=None, height=None):
    """求解摆放参数。

    center  : 希望**几何中心**落在的世界坐标 (x, y, z)
    base    : 希望**底面中心**落在的世界坐标 (x, y, z)
    size    : 目标实际全尺寸 (x, y, z)，会换算成 ugc_size
    height  : 只给目标高度（等价于 size 的 y），x/z 保持原比例
    ugc_size: 直接指定缩放倍率（与 size/height 互斥）
    rotation: 欧拉角（度），轴序同引擎 (x, y, z)

    返回 dict：position / scale / rotation / anchor / native_size / target_size ...
    """
    rec = get(item_id, lib)
    half = list(rec["half"])
    cl = list(rec["center"])
    native = list(rec["size"])
    notes = []

    anchors = [a for a in (center, base) if a is not None]
    if len(anchors) != 1:
        raise ValueError("center 与 base 必须且只能给一个")
    anchor = "center" if center is not None else "base"
    T = list(center if center is not None else base)

    # ---- 缩放
    if ugc_size is not None:
        scale = [float(x) for x in ugc_size]
    elif size is not None:
        scale = [float(size[i]) / native[i] if native[i] else 1.0 for i in range(3)]
    elif height is not None:
        if not native[1]:
            raise ValueError("该构件原生高度为 0（平面件），无法按高度定标")
        k = float(height) / native[1]
        scale = [k, k, k]
        notes.append("按高度等比缩放 k=%.4f" % k)
    else:
        scale = [1.0, 1.0, 1.0]

    # ---- 本地锚点（缩放前）
    if anchor == "base":
        local = [cl[0], cl[1] - half[1], cl[2]]
        if abs(half[1]) < 1e-6:
            notes.append("平面构件（half.y=0），base 与 center 锚点等价")
    else:
        local = cl

    # ---- 缩放 → 旋转 → 反推 position
    scaled = [local[i] * scale[i] for i in range(3)]
    rot = [float(x) for x in (rotation or [0, 0, 0])]
    M = euler_matrix(*rot) if any(abs(x) > 1e-9 for x in rot) else None
    world_off = rotate(scaled, M) if M else scaled
    position = [round(T[i] - world_off[i], 4) for i in range(3)]

    # ---- 组装的最终实际尺寸 & 校验信息
    target_size = [round(native[i] * scale[i], 4) for i in range(3)]
    if any(abs(s - 1.0) > 1e-9 for s in scale):
        zl = rec.get("zoom_limit")
        if zl and any(not (zl[0] <= scale[i] <= zl[1]) for i in range(3)):
            notes.append("⚠️ 缩放 %s 超出该构件允许区间 %s" % (scale, zl))
    if max(half) >= 0 and abs(cl[0]) + abs(cl[2]) > 1e-6:
        notes.append("原点水平偏移 center=(%.2f, %.2f)，已按锚点校正" % (cl[0], cl[2]))

    return {
        "item_id": rec["item_id"],
        "name_cn": rec.get("name_cn"),
        "anchor": anchor,
        "target": [round(v, 4) for v in T],
        "position": position,
        "scale": [round(s, 6) for s in scale],
        "rotation": rot,
        "native_size": native,
        "target_size": target_size,
        "half": half,
        "center_local": cl,
        "world_offset": [round(v, 4) for v in world_off],
        "notes": notes,
    }


# ---------------------------------------------------------------- 实测偏移缓存
OFFSET_PATH = os.path.join(ROOT, "registry", "place_offsets.json")


def load_offsets():
    if "offsets" not in _CACHE:
        try:
            with open(OFFSET_PATH, "r", encoding="utf-8") as f:
                _CACHE["offsets"] = json.load(f)
        except Exception:
            _CACHE["offsets"] = {}
    return _CACHE["offsets"]


def save_offset(item_id, delta, scale, name=None, source="closed-loop"):
    """记录某构件实测的「世界 AABB 中心 − position」偏移。

    delta  : 实测偏移（世界单位，已含 scale）
    scale  : 当时的 ugc_size，便于换算单位缩放偏移
    """
    off = load_offsets()
    off[str(int(item_id))] = {
        "delta": [round(float(x), 6) for x in delta],
        "scale": [round(float(x), 6) for x in (scale or [1, 1, 1])],
        "unit_delta": [round(float(delta[i]) / (scale[i] or 1.0), 6) for i in range(3)],
        "name_cn": name,
        "source": source,
    }
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        json.dump(off, f, ensure_ascii=False, indent=1)
    return off[str(int(item_id))]


def base_position(item_id, base, scale=None, delta=None):
    """让「世界 AABB 的底面中心」精确落在 base 的 position。

    实测关系（2026-09-17 闭环验证，误差 0.0000）：
        AABB 中心 bc = position + δ        （δ 与位置无关，是构件固有偏移）
        AABB 半高   = half.y × scale.y
        ⇒ position = base − (δ.x, δ.y − half.y×scale.y, δ.z)

    ⚠️ 为什么要用它：δ **不等于** 库里的 `center × scale`。
    实测 5 个构件中 2 个不符（6093 的 x/z 归零、3004 只剩 1/22），
    且与 `model_path`/`src_model_path` 静态加载值都不一致 ——
    实体实例化后的几何中心目前无法从静态数据预测，故改用实测 δ。
    """
    rec = get(item_id)
    sc = [float(x) for x in (scale or [1, 1, 1])]
    if delta is None:
        e = load_offsets().get(str(int(item_id)))
        if e is None:
            return None
        delta = e["delta"]
    return [round(base[0] - delta[0], 4),
            round(base[1] - (delta[1] - rec["half"][1] * sc[1]), 4),
            round(base[2] - delta[2], 4)]


# ---------------------------------------------------------------- 验证
def verify(verbose=True):
    """用地图内实体的实测 bounding_center(0) 检验公式。

    预测： world_center = position + M · (center_local × scale)
    实测： 引擎给出的 model.bounding_center(0)

    ⚠️ **覆盖范围很窄，不要当作广泛验证**：静态采集 200 个实体时命中的只有
    **4 种** item_id，其中 **84 个是方块 301001**（x/z 偏移恰为 0，**天然无法证伪**）。
    ⚠️ 本函数只说明「在上述样本上相符」，**不代表** `center` 可用于预测任意构件的
    实体偏移 —— 该做法已被对照实验证伪（见 PLACEMENT.md 第二节）。
    **最终定位请用 `cli/place_exact.py` 的闭环校正。**
    """
    with open(XFORM_PROBE, "r", encoding="utf-8") as f:
        rows = json.load(f)["value"]["result"]
    lib = load_library()
    ok = bad = skip = 0
    worst = (0.0, None)
    for e in rows:
        iid = e.get("item_id")
        rec = lib.get(iid)
        if rec is None:
            skip += 1
            continue
        cl = rec["center"]
        scale = e["size"]
        rot = e["dir"]
        M = euler_matrix(*rot) if any(abs(x) > 1e-9 for x in rot) else None
        scaled = [cl[i] * scale[i] for i in range(3)]
        off = rotate(scaled, M) if M else scaled
        pred = [e["pos"][i] + off[i] for i in range(3)]
        act = e["world_center"]
        err = max(abs(pred[i] - act[i]) for i in range(3))
        if err < 0.02:
            ok += 1
        else:
            bad += 1
            if verbose:
                print("  MISMATCH ugc_id=%s item=%s pred=%s act=%s err=%.4f"
                      % (e["ugc_id"], iid, [round(x, 3) for x in pred], act, err))
        if err > worst[0]:
            worst = (err, e["ugc_id"])
    if verbose:
        print("验证样本 %d（库内命中 %d，跳过 %d）" % (len(rows), ok + bad, skip))
        print("  一致 %d / 不一致 %d   最大误差 %.6f (ugc_id=%s)" % (ok, bad, worst[0], worst[1]))
        rot_n = sum(1 for e in rows if any(abs(x) > 1e-9 for x in e["dir"]))
        print("  其中含旋转样本 %d 个" % rot_n)
    return ok, bad


# ---------------------------------------------------------------- CLI
def _parse_vec(s):
    return [float(x) for x in s.replace(" ", "").split(",")]


def main():
    p = argparse.ArgumentParser(description="摆放参数求解器")
    sub = p.add_subparsers(dest="cmd")

    s1 = sub.add_parser("show", help="查看构件规格")
    s1.add_argument("item_id", type=int)

    s2 = sub.add_parser("calc", help="求解摆放参数")
    s2.add_argument("item_id", type=int)
    s2.add_argument("--center", type=str, help="目标几何中心 x,y,z")
    s2.add_argument("--base", type=str, help="目标底面中心 x,y,z")
    s2.add_argument("--size", type=str, help="目标全尺寸 x,y,z")
    s2.add_argument("--height", type=float, help="目标高度（等比缩放）")
    s2.add_argument("--ugc-size", dest="ugc_size", type=str, help="直接指定缩放 x,y,z")
    s2.add_argument("--rot", type=str, default="0,0,0", help="欧拉角 x,y,z（度）")

    s3 = sub.add_parser("verify", help="用实机实体数据验证公式")

    a = p.parse_args()
    if a.cmd == "show":
        r = get(a.item_id)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif a.cmd == "calc":
        res = solve(a.item_id,
                    center=_parse_vec(a.center) if a.center else None,
                    base=_parse_vec(a.base) if a.base else None,
                    size=_parse_vec(a.size) if a.size else None,
                    ugc_size=_parse_vec(a.ugc_size) if a.ugc_size else None,
                    rotation=_parse_vec(a.rot),
                    height=a.height)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.cmd == "verify":
        ok, bad = verify()
        return 0 if bad == 0 else 1
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
