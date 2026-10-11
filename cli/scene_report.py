#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene_report — 分析 registry/ 下抓取的场景数据，输出结构规律。

用法:
    python cli/scene_report.py church_scene
"""
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = sys.argv[1] if len(sys.argv) > 1 else "church_scene"
PATH = os.path.join(ROOT, "registry", NAME + ".json")

GEOM_KW = ("geometry", "box", "sphere", "cylinder", "cone", "triangle", "ring", "drop")
PLANT_KW = ("plant", "flower", "tree", "grass", "bloom", "botany", "iris", "leaf",
            "shrub", "vine", "moss", "fern", "wood", "bush", "tree")
BUILD_KW = ("wall", "building", "floor", "roof", "window", "door", "school", "house",
            "stair", "pillar", "column", "passageway", "shop", "fence", "gate",
            "bridge", "tower", "pavilion", "tingzi", "dibiao")
DECOR_KW = ("light", "lamp", "prop", "statue", "umbrella", "lantern", "deco",
            "antique", "pendant", "flag", "banner", "chair", "table", "block")
CHAR_KW = ("survivor", "hunter", "npc", "banshee", "butcher", "ghost", "player")


def classify(name):
    n = name.lower()
    for kws, tag in ((GEOM_KW, "几何体"), (CHAR_KW, "角色"),
                     (PLANT_KW, "植物"), (BUILD_KW, "建筑"), (DECOR_KW, "装饰")):
        if any(k in n for k in kws):
            return tag
    return "其它"


def load(clip=None):
    """clip=None 时只剔除哨兵与无路径项，**不按坐标裁剪**。

    ⚠️ 不要硬编码地图边界：不同地图尺寸不同（教堂图边界 ±1440/±1120，
    另一张图 x/z 达 2720）。硬编码阈值曾把整张作品滤掉（805 → 只剩 29）。
    """
    with open(PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    its = [r for r in d["items"]
           if r.get("p") and abs(r["p"][1] + 65536) > 1 and r.get("f")]
    for r in its:
        r["_m"] = r["f"].replace("\\", "/").split("/")[-1]
    if clip:
        its = [r for r in its if abs(r["p"][0]) < clip and abs(r["p"][2]) < clip]
    return d, its


def main():
    d, its = load()
    print("= 场景 %s  |  抓取 %d  有效 %d  |  不同模型 %d"
          % (NAME, d["captured"], len(its), len(set(r["_m"] for r in its))))
    print()

    # ---- 1. 模型清单
    cm = Counter(r["_m"] for r in its)
    print("## 模型清单（按用量）")
    for k, v in cm.most_common():
        if v < 2:
            continue
        print("   %-42s %5d  %5.1f%%" % (k, v, 100.0 * v / len(its)))

    # ---- 1b. 构件类型构成 → 场景性质判定
    tagc = Counter(classify(r["_m"]) for r in its)
    print()
    print("## 构件类型构成")
    for t, n in tagc.most_common():
        print("   %-5s %5d  %5.1f%%  %s"
              % (t, n, 100.0 * n / len(its), "#" * int(46.0 * n / len(its))))
    vert = max(r["p"][1] for r in its) - min(r["p"][1] for r in its)
    pr = tagc.get("植物", 0) / float(len(its))
    br = tagc.get("建筑", 0) / float(len(its))
    gr = tagc.get("几何体", 0) / float(len(its))
    print()
    print("   竖向跨度 %.1f   植物 %.1f%%   建筑 %.1f%%   几何体 %.1f%%"
          % (vert, pr * 100, br * 100, gr * 100))
    if br >= 0.25 and vert > 60:
        kind = "**建筑型**（建筑构件占比高 + 竖向有结构）→ 适合学建筑"
    elif pr >= 0.35:
        kind = "**景观型**（植物主导 + 竖向扁平）→ 适合学绿化布置，学建筑价值有限"
    elif gr >= 0.5:
        kind = "**几何体拼贴型** → 可学模数与阵列手法，但教不了建筑构件用法"
    else:
        kind = "**混合/其它型** → 建议人工看图判断"
    print("   ⇒ 场景性质：%s" % kind)

    # ---- 2. 主要构件的缩放分布（模数线索）
    print()
    print("## 缩放 sig（主要构件 TOP8）")
    for name, _ in cm.most_common(8):
        sc = Counter(tuple(r["s"]) for r in its if r["_m"] == name and r.get("s"))
        top = sc.most_common(8)
        print("   %s  (共 %d，不同缩放 %d)" % (name, sum(sc.values()), len(sc)))
        for k, v in top:
            print("        s=%-26s x%-5d  实际尺寸≈%s"
                  % (k, v, [round(x * 10, 2) for x in k]))
        if len(sc) > 8:
            print("        … 其余 %d 种" % (len(sc) - 8))

    # ---- 3. 高度分层
    print()
    print("## 高度分布（每 10 单位一档，仅列 ≥20 构件的档）")
    hb = Counter(int(r["p"][1] // 10) * 10 for r in its)
    for k in sorted(hb):
        if hb[k] >= 20:
            print("      y %5d~%-5d  %5d  %s" % (k, k + 10, hb[k], "#" * min(60, hb[k] // 8)))

    # ---- 4. 平面范围（用分位数区分「全范围」与「作品主体」）
    xs = [r["p"][0] for r in its]
    zs = [r["p"][2] for r in its]

    def q(v, p):
        s = sorted(v)
        return s[max(0, min(len(s) - 1, int(len(s) * p)))]

    print()
    print("## 平面范围")
    print("   全范围        x %.1f ~ %.1f    z %.1f ~ %.1f"
          % (min(xs), max(xs), min(zs), max(zs)))
    print("   主体(P2~P98)  x %.1f ~ %.1f    z %.1f ~ %.1f"
          % (q(xs, .02), q(xs, .98), q(zs, .02), q(zs, .98)))
    cx = (q(xs, .02) + q(xs, .98)) / 2.0
    cz = (q(zs, .02) + q(zs, .98)) / 2.0
    print("   主体中心 ≈ (%.1f, %.1f)" % (cx, cz))
    left = sum(1 for x in xs if x < cx)
    right = len(xs) - left
    print("   以 x=%.1f 分左右: %d / %d  → %s"
          % (cx, left, right,
             "近似左右对称" if abs(left - right) < 0.15 * len(its) else "不对称"))

    # ---- 5. x/z 轴向上的构件聚集（开间线索）
    print()
    print("## x 轴聚集（开间/轴线线索，取 |x|>0 且成组）")
    cxp = Counter(round(r["p"][0] / 5.0) * 5 for r in its)
    hot = [(k, v) for k, v in sorted(cxp.items()) if v >= 25]
    print("   %d 个热点轴线（每 5 单位聚合，≥25 构件）：" % len(hot))
    line = "   "
    for k, v in hot[:40]:
        line += " %s:%d" % (int(k), v)
    print(line)


if __name__ == "__main__":
    main()
