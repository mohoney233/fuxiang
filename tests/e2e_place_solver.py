#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验收：place_solver 算出的 position 是否真的让几何中心落在目标点。

流程：solve → place(local_preview) → poll → 读回 model.bounding_center(0) → 比对 → 清理

用法:
    python tests/e2e_place_solver.py [item_id] [x,y,z]
"""
import json
import os
import sys
import time

ROOT = r"<工具根>"
sys.path.insert(0, os.path.join(ROOT, "cli"))
from mcpcall import call              # noqa: E402
from place_solver import solve, get   # noqa: E402

ITEM = int(sys.argv[1]) if len(sys.argv) > 1 else 3004
TARGET = ([float(v) for v in sys.argv[2].split(",")] if len(sys.argv) > 2
          else [300.0, 2.0, 900.0])
RID = "e2e-solver-%d" % ITEM


def readback(gid):
    code = ("c=cm._ugc_id2component[%d]\n"
            "u=c._owner\n"
            "m=u.model\n"
            "bc=m.bounding_center(0)\n"
            "p3=u.get_position3()\n"
            "__result__={'world_center':[round(bc.x,4),round(bc.y,4),round(bc.z,4)],"
            "'position':[round(p3.x,4),round(p3.y,4),round(p3.z,4)]}\n" % gid)
    return call("fuxiang_eval", {"code": code})


def main():
    rec = get(ITEM)
    print("构件 %s %s" % (ITEM, rec["name_cn"]))
    print("  原生 size=%s  half=%s  center=%s" % (rec["size"], rec["half"], rec["center"]))

    res = solve(ITEM, base=TARGET)
    print("目标底面中心 %s" % TARGET)
    print("求解 position=%s scale=%s" % (res["position"], res["scale"]))
    naive = [rec["center"][i] * res["scale"][i] for i in range(3)]
    print("  若不校正锚点，几何体会偏移 %s" % [round(v, 3) for v in naive])
    for n in res["notes"]:
        print("  note: %s" % n)

    st = call("fuxiang_status", {})
    print("status pid=%s ok=%s" % (st.get("pid"), st.get("ok", True)))

    r = call("fuxiang_place_item", {
        "item_id": ITEM,
        "position": res["position"],
        "rotation": res["rotation"],
        "scale": res["scale"],
        "mode": "server",
        "request_id": RID,
    })
    print("place -> %s" % json.dumps(r, ensure_ascii=False)[:220])
    deadline = time.time() + 90
    while isinstance(r, dict) and r.get("status") in ("pending", "send_unknown", "overdue") \
            and time.time() < deadline:
        time.sleep(0.5)
        r = call("fuxiang_place_poll", {"request_id": RID})
    print("poll  -> %s" % json.dumps(r, ensure_ascii=False)[:220])

    gid = r.get("ugc_id") if isinstance(r, dict) else None
    if not gid:
        print("!! 未取得 ugc_id，无法回读")
        return 1

    rb = readback(gid)
    print("回读  -> %s" % json.dumps(rb, ensure_ascii=False))
    inner = rb.get("result", rb) if isinstance(rb, dict) else {}
    wc = inner.get("world_center")
    if not wc:
        print("!! 回读缺 world_center")
        return 1

    # 判据一：position → 几何中心的偏移，应等于 center_local × scale（无旋转）
    pos = inner.get("position")
    act_off = [wc[i] - pos[i] for i in range(3)]
    exp_off = [rec["center"][i] * res["scale"][i] for i in range(3)]
    err_off = max(abs(act_off[i] - exp_off[i]) for i in range(3))
    # 判据二：几何中心应落在 目标底面中心 + 半高
    exp = [TARGET[0], TARGET[1] + rec["half"][1] * res["scale"][1], TARGET[2]]
    err = max(abs(wc[i] - exp[i]) for i in range(3))
    print()
    print("position       %s" % pos)
    print("预期偏移(→几何中心) %s" % [round(v, 4) for v in exp_off])
    print("实测偏移             %s" % [round(v, 4) for v in act_off])
    print("期望几何中心 %s" % [round(v, 4) for v in exp])
    print("实测几何中心 %s" % wc)
    print(">>> 偏移误差 %.4f | 落点误差 %.4f  %s"
          % (err_off, err, "PASS" if (err < 0.05 and err_off < 0.05) else "FAIL"))

    d = call("fuxiang_edit_delete", {"ugc_id": gid})
    print("清理 -> %s" % json.dumps(d, ensure_ascii=False)[:200])
    return 0 if (err < 0.05 and err_off < 0.05) else 1


if __name__ == "__main__":
    sys.exit(main())
