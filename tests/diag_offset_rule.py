"""对照实验：摆放多个 offset 特征不同的构件，读回真实偏移，反推真实规律。

库里的 center（独立模型读到的本地包围盒中心）是否等于实体上
(bounding_center(0) - position)？逐个比对。
"""
import json
import os
import sys
import time

ROOT = r"<工具根>"
sys.path.insert(0, os.path.join(ROOT, "cli"))
from mcpcall import call              # noqa: E402
from place_solver import solve, get   # noqa: E402

CASES = [
    (3010,  [200.0, 2.0, 900.0]),
    (3081,  [400.0, 2.0, 900.0]),
    (6093,  [600.0, 2.0, 900.0]),
    (3004,  [800.0, 2.0, 900.0]),
    (61062, [1000.0, 2.0, 900.0]),
]
MODE = "local_preview"


def readback(gid):
    code = ("c=cm._ugc_id2component[%d]\n"
            "u=c._owner\n"
            "m=u.model\n"
            "bc=m.bounding_center(0)\n"
            "bb=m.bounding_box\n"
            "p3=u.get_position3()\n"
            "__result__={'bc':[round(bc.x,4),round(bc.y,4),round(bc.z,4)],"
            "'bb':[round(bb.x,4),round(bb.y,4),round(bb.z,4)],"
            "'pos':[round(p3.x,4),round(p3.y,4),round(p3.z,4)]}\n" % gid)
    r = call("fuxiang_eval", {"code": code})
    return r.get("result", r) if isinstance(r, dict) else {}


print("%-8s %-26s %-26s %-24s %s" % ("item", "库 center(=本地)", "实测偏移(bc-pos)", "scale", "bb"))
for iid, tgt in CASES:
    rec = get(iid)
    res = solve(iid, base=tgt)
    rid = "rule-%d" % iid
    r = call("fuxiang_place_item", {
        "item_id": iid, "position": res["position"], "rotation": [0, 0, 0],
        "scale": res["scale"], "mode": MODE, "request_id": rid})
    dl = time.time() + 60
    while isinstance(r, dict) and r.get("status") in ("pending", "send_unknown", "overdue") \
            and time.time() < dl:
        time.sleep(0.5)
        r = call("fuxiang_place_poll", {"request_id": rid})
    gid = r.get("ugc_id") if isinstance(r, dict) else None
    if not gid:
        print("%-8s place FAILED: %s" % (iid, json.dumps(r, ensure_ascii=False)[:120]))
        continue
    rb = readback(gid)
    try:
        pos = rb["pos"]
        bc = rb["bc"]
        off = [round(bc[i] - pos[i], 4) for i in range(3)]
        print("%-8s %-26s %-26s %-24s %s" % (
            iid, [round(x, 3) for x in rec["center"]], off,
            [round(x, 3) for x in res["scale"]], [round(x, 3) for x in rb["bb"]]))
    except Exception as e:
        print("%-8s readback FAILED %s : %s" % (iid, e, json.dumps(rb, ensure_ascii=False)[:140]))
    try:
        call("fuxiang_remove_item", {"ugc_id": gid})
    except Exception as e:
        print("   cleanup warn: %s" % str(e)[:110])
    time.sleep(0.4)
print("done")
