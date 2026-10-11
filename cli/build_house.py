#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_house — 用参数化模板生成并摆放建筑。

用法:
    python cli/build_house.py --preset cabin --at 600,5,1200                 # 只生成清单
    python cli/build_house.py --preset cabin --at 600,5,1200 --place         # 生成并摆放
    python cli/build_house.py --width 160 --depth 120 --height 80 --place
    python cli/build_house.py --preset hall --seal                           # 含封顶天花板

定位说明（已实测，见 PLACEMENT.md）
    几何体 301001 的实测偏移 δ=(0, 5×scale, 0)，其原点在**底面中心**且 δ 随 scale 等比，
    因此 `position ≡ base`（包围盒底面中心）对任意缩放成立 —— 直接摆即可，无需额外校正。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
# 2026-09-22 通道收敛：原走 MCP stdio（每条调用新起 server.py 子进程），
# 现改用 cli/fxapi.py 直连裸 IPC。op 名去掉 fuxiang_ 前缀，返回值已剥壳到业务值
# （eval 的 result 层也已剥掉，故 _read_geom 不再 .get("result")）。
from fxapi import call                            # noqa: E402
import building_template as BT                    # noqa: E402


def _read_geom(gid):
    """读回构件的「世界 AABB 底面中心」与「世界几何中心」。"""
    code = ("c=cm._ugc_id2component[%d]\n"
            "u=c._owner\n"
            "m=u.model\n"
            "bc=m.bounding_center(0)\n"
            "bs=m.bounding_size(0)\n"
            "__result__={'base':[round(bc.x,4),round(bc.y-bs.y,4),round(bc.z,4)],"
            "'center':[round(bc.x,4),round(bc.y,4),round(bc.z,4)]}\n" % gid)
    r = call("eval", {"code": code})
    return r if isinstance(r, dict) else None


def place_all(parts, mode="local_preview", timeout=60.0, fix_rotated=True):
    ok = fail = fixed = 0
    results = []
    n = len(parts)
    for i, p in enumerate(parts):
        rid = "bt-%d-%d" % (int(time.time() * 1000) % 1000000, i)
        args = {
            "item_id": p["item_id"],
            "position": p["at"],
            "rotation": p["rotation"],
            "scale": p["scale"],
            "mode": mode,
            "request_id": rid,
        }
        r = call("place_item", args)
        dl = time.time() + timeout
        while isinstance(r, dict) and r.get("status") in ("pending", "send_unknown", "overdue") \
                and time.time() < dl:
            time.sleep(0.4)
            r = call("place_poll", {"request_id": rid})
        st = r.get("status") if isinstance(r, dict) else "?"
        gid = r.get("ugc_id") if isinstance(r, dict) else None
        # 以「拿到 ugc_id」为成功判据：poll 返回的字段位置/取值随路径不同，
        # 但只要有 ugc_id 就说明实体已创建（实测 15/15 均如此）。
        if gid:
            ok += 1
            st = st or "placed"
        else:
            fail += 1

        # 闭环校正的适用条件：
        #   a) 带旋转的构件 —— `position ≡ base` 只对轴对齐成立；
        #   b) **非几何体构件（真实构件）** —— 其静态 center 未必等于实体偏移。
        # 目标锚点：旋转件用**几何中心**（斜置后"底面方向"已变）；
        #          无旋转件用**AABB 底面中心**（模板的 at 按落地语义给）。
        is_geom = 301000 <= int(p["item_id"]) <= 301999
        has_rot = any(abs(x) > 1e-9 for x in p["rotation"])
        if gid and fix_rotated and (has_rot or not is_geom):
            # 目标锚点：只有绕 **x/z 轴倾斜** 的构件（如屋面斜板）才用几何中心 ——
            # 斜置后"底面方向"已变，AABB 底面中心不再对应落地位置。
            # 绕 y 轴旋转的墙**不算倾斜**，仍用 AABB 底面中心
            # （实测：误用中心会让沿 z 的墙整体下沉半个墙高 25.9）。
            tilt = abs(p["rotation"][0]) > 1e-9 or abs(p["rotation"][2]) > 1e-9
            want = "center" if tilt else "base"
            got = _read_geom(gid)
            cur = got.get(want) if got else None
            if cur:
                d = [p["at"][j] - cur[j] for j in range(3)]
                if max(abs(x) for x in d) > 0.05:
                    newpos = [round(args["position"][j] + d[j], 4) for j in range(3)]
                    call("set_transform", {
                        "ugc_id": gid, "position": newpos,
                        "rotation": p["rotation"], "scale": p["scale"]})
                    time.sleep(0.35)
                    got2 = _read_geom(gid)
                    c2 = got2.get(want) if got2 else None
                    err = (max(abs(p["at"][j] - c2[j]) for j in range(3))
                           if c2 else -1)
                    print("      ↳ 闭环(%s) Δ=%s → 残差 %.4f"
                          % (want, [round(v, 2) for v in d], err))
                    fixed += 1
        results.append({"name": p["name"], "status": st, "ugc_id": gid})
        print("   [%2d/%d] %-22s %-8s ugc_id=%s" % (i + 1, n, p["name"], st, gid))
    if fixed:
        print("   （其中 %d 个旋转构件已闭环修正）" % fixed)
    return ok, fail, results


def main():
    ap = argparse.ArgumentParser(description="参数化建筑模板")
    ap.add_argument("--preset", choices=sorted(BT.PRESETS.keys()))
    ap.add_argument("--width", type=float)
    ap.add_argument("--depth", type=float)
    ap.add_argument("--height", type=float)
    ap.add_argument("--thickness", type=float)
    ap.add_argument("--roof", choices=["gable", "flat", "shed", "real"],
                    help="'real' 仅在 --kit real 下有效：用真实屋顶构件 10214")
    ap.add_argument("--windows", type=int)
    ap.add_argument("--no-frames", action="store_true", help="不加门窗套")
    ap.add_argument("--kit", choices=["geom", "real", "hybrid"], default="geom",
                    help="geom=几何体（尺寸自由）；real=真实构件（红木墙+红瓷砖地板，默认无顶）；"
                         "hybrid=地板/屋顶真实 + 墙几何体（可开门窗）")
    ap.add_argument("--with-ceil", action="store_true",
                    help="real 套装：在墙顶再加一层真实天花板（默认直接上坡屋顶）")
    ap.add_argument("--at", type=str, default="0,0,0", help="基准位置 x,y,z（底面）")
    ap.add_argument("--out", type=str, default="", help="清单输出路径（默认 registry/house_<preset>.json）")
    ap.add_argument("--place", action="store_true", help="实际摆放")
    ap.add_argument("--mode", default="local_preview", choices=["local_preview", "server"])
    a = ap.parse_args()

    base = tuple(float(v) for v in a.at.split(","))
    if a.kit == "real":
        kw = {"kit": "real"}
        # 真实构件版：**红木墙 10217 + 红瓷砖地板 10247**，默认**不封顶**（便于看内部）。
        # 门洞靠"两块缩放墙板中间留缝"实现（真实墙板是固定尺寸、无法挖洞）。
        parts = BT.hut_real(base=base, roof=a.roof or "none")
    elif a.kit == "hybrid":
        kw = {"kit": "hybrid"}
        parts = BT.hut_hybrid(base=base, roof=a.roof or "gable",
                              width=120.0, depth=100.0,
                              windows=(a.windows if a.windows is not None else 2))
    else:
        kw = dict(BT.PRESETS.get(a.preset, {})) if a.preset else {}
        for k in ("width", "depth", "height", "thickness", "roof", "windows"):
            v = getattr(a, k)
            if v is not None:
                kw[k] = v
        if a.no_frames:
            kw["frames"] = False
        parts = BT.cabin(base=base, **kw)

    out = a.out or os.path.join(ROOT, "registry",
                               "house_%s.json" % (a.preset or "custom"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"params": kw, "base": list(base), "count": len(parts),
                   "parts": parts}, f, ensure_ascii=False, indent=1)
    print("清单: %d 个构件 → %s" % (len(parts), out))
    from collections import Counter
    print("构成: %s" % dict(Counter(p["name"].rsplit("-", 1)[0] for p in parts)))

    if not a.place:
        print("（未摆放；加 --place 实际摆到地图里）")
        return 0

    ok, fail, _ = place_all(parts, mode=a.mode)
    print()
    print("摆放完成: 成功 %d / 失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
