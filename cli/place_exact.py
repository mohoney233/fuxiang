#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""place_exact — 精确摆放：让构件的「世界包围盒底面中心」落在指定点。

为什么不用理论公式
------------------
库里的 `center`（= 静态加载模型的 bounding_center）**不等于**实体实例化后的偏移。
实测 5 个构件中 2 个不符（6093 的 x/z 归零、3004 只剩原值 1/22），
且与 `model_path` / `src_model_path` 的静态值都不一致 ——
**实体几何中心目前无法从静态数据预测**。

因此改用**闭环**：不猜，直接测。

    1) 按目标摆一次（有缓存 δ 则一次到位）
    2) 读回实际世界 AABB：bounding_center(0) / bounding_size(0)
    3) δ = AABB中心 − position  ← 构件固有偏移，**与位置无关**（已实测）
    4) 用 δ 反算 position 并修正，同时把 δ 缓存到 registry/place_offsets.json
       ⇒ 之后同一构件同类缩放可一次到位

用法
----
    python cli/place_exact.py 3004 --base 800,2,900
    python cli/place_exact.py 10215 --base 580,0,-170 --rot 0,90,0 --height 60 --keep
    python cli/place_exact.py 3004 --base 800,2,900 --calibrate   # 只标定不保留
    python cli/place_exact.py --list-offsets                      # 看已标定构件
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
# 2026-09-22 通道收敛：原走 MCP stdio，现改 cli/fxapi.py 直连裸 IPC。
# 返回值已剥壳到业务值（eval 的 result 层也已剥掉）。
from fxapi import call                                                # noqa: E402
from place_solver import get, solve, load_offsets, save_offset        # noqa: E402

AABB_CODE = (
    "c=cm._ugc_id2component[%d]\n"
    "u=c._owner\n"
    "m=u.model\n"
    "bc=m.bounding_center(0)\n"
    "bs=m.bounding_size(0)\n"
    "p3=u.get_position3()\n"
    "__result__={'bc':[round(bc.x,4),round(bc.y,4),round(bc.z,4)],"
    "'bs':[round(bs.x,4),round(bs.y,4),round(bs.z,4)],"
    "'pos':[round(p3.x,4),round(p3.y,4),round(p3.z,4)]}\n"
)


def read_aabb(gid):
    r = call("eval", {"code": AABB_CODE % gid})
    return r if isinstance(r, dict) else {}


def place_and_poll(item_id, position, rotation, scale, rid, mode="local_preview", wait=60.0):
    r = call("place_item", {
        "item_id": item_id, "position": position, "rotation": rotation,
        "scale": scale, "mode": mode, "request_id": rid})
    dl = time.time() + wait
    while isinstance(r, dict) and r.get("status") in ("pending", "send_unknown", "overdue") \
            and time.time() < dl:
        time.sleep(0.5)
        r = call("place_poll", {"request_id": rid})
    return r


def cached_delta(item_id, scale):
    """取该构件在给定 scale 下的 δ（用 unit_delta 按比例换算）。"""
    e = load_offsets().get(str(int(item_id)))
    if not e:
        return None
    ud = e.get("unit_delta")
    if not ud:
        ud = [e["delta"][i] / (e["scale"][i] or 1.0) for i in range(3)]
    return [ud[i] * (scale[i] or 1.0) for i in range(3)]


def main():
    ap = argparse.ArgumentParser(description="闭环精确摆放")
    ap.add_argument("item_id", type=int, nargs="?")
    ap.add_argument("--base", type=str, help="目标「包围盒底面中心」x,y,z")
    ap.add_argument("--rot", type=str, default="0,0,0")
    ap.add_argument("--size", type=str, help="目标全尺寸 x,y,z")
    ap.add_argument("--height", type=float, help="目标高度（等比缩放）")
    ap.add_argument("--keep", action="store_true", help="保留物件（默认保留；--calibrate 时忽略）")
    ap.add_argument("--calibrate", action="store_true", help="只标定 δ，随后删除物件")
    ap.add_argument("--remeasure", action="store_true", help="忽略缓存，强制重新测量")
    ap.add_argument("--mode", default="local_preview", choices=["local_preview", "server"])
    ap.add_argument("--list-offsets", action="store_true")
    a = ap.parse_args()

    if a.list_offsets:
        print(json.dumps(load_offsets(), ensure_ascii=False, indent=1))
        return 0
    if not a.item_id or not a.base:
        ap.print_help()
        return 2

    base = [float(v) for v in a.base.split(",")]
    rot = [float(v) for v in a.rot.split(",")]
    rec = get(a.item_id)
    theory = solve(a.item_id,
                   base=base, rotation=rot,
                   size=[float(v) for v in a.size.split(",")] if a.size else None,
                   height=a.height)
    scale = theory["scale"]

    delta = None if a.remeasure else cached_delta(a.item_id, scale)
    if delta:
        pos = [round(base[0] - delta[0], 4),
               round(base[1] - (delta[1] - rec["half"][1] * scale[1]), 4),
               round(base[2] - delta[2], 4)]
        print("[cache] 命中已标定 δ=%s → 一次到位" % [round(x, 4) for x in delta])
    else:
        pos = theory["position"]
        print("[theory] 无缓存，用理论 position=%s 起摆" % pos)

    rid = "exact-%d-%d" % (a.item_id, int(time.time()) % 100000)
    r = place_and_poll(a.item_id, pos, rot, scale, rid, a.mode)
    gid = r.get("ugc_id") if isinstance(r, dict) else None
    if not gid:
        print("!! 摆放失败: %s" % json.dumps(r, ensure_ascii=False)[:200])
        return 1

    aabb = read_aabb(gid)
    bc, bs, pos_actual = aabb["bc"], aabb["bs"], aabb["pos"]
    base_center = [bc[0], bc[1] - bs[1], bc[2]]
    delta_meas = [round(bc[i] - pos_actual[i], 4) for i in range(3)]
    err0 = max(abs(base_center[i] - base[i]) for i in range(3))
    print("  读回 bc=%s bs=%s → AABB底面中心=%s（误差 %.4f）"
          % (bc, bs, [round(v, 4) for v in base_center], err0))

    fixed = [round(base[0] - delta_meas[0], 4),
             round(base[1] - (delta_meas[1] - bs[1]), 4),
             round(base[2] - delta_meas[2], 4)]
    if max(abs(fixed[i] - pos_actual[i]) for i in range(3)) > 0.01:
        sr = call("set_transform", {"ugc_id": gid, "position": fixed,
                                            "rotation": rot, "scale": scale})
        if isinstance(sr, dict) and sr.get("ok") is False:
            print("  set_transform 失败: %s" % json.dumps(sr, ensure_ascii=False)[:160])
        time.sleep(0.5)
        aabb = read_aabb(gid)
        bc, bs = aabb["bc"], aabb["bs"]
        base_center = [bc[0], bc[1] - bs[1], bc[2]]
    err1 = max(abs(base_center[i] - base[i]) for i in range(3))
    print("  修正后 AABB底面中心=%s" % [round(v, 4) for v in base_center])
    print(">>> 最终误差 %.4f  %s" % (err1, "PASS" if err1 < 0.05 else "FAIL"))

    save_offset(a.item_id, delta_meas, scale, rec.get("name_cn"))
    print("  已缓存 δ=%s (scale=%s) → registry/place_offsets.json"
          % ([round(x, 4) for x in delta_meas], scale))
    print("  ugc_id=%s  position=%s" % (gid, fixed))

    if a.calibrate:
        call("remove_item", {"ugc_id": gid})
        print("  已删除（标定模式）")
    return 0 if err1 < 0.05 else 1


if __name__ == "__main__":
    sys.exit(main())
