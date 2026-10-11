"""验证「闭环校正」可行性：
   摆一次 → 读实际世界 AABB 中心 → 算偏差 → 平移 position → 复查是否落到目标。

若成立，则无需知道偏移成因，也能精确定位任意构件。
"""
import json
import os
import sys
import time

ROOT = r"<工具根>"
sys.path.insert(0, os.path.join(ROOT, "cli"))
from mcpcall import call              # noqa: E402

ITEM = int(sys.argv[1]) if len(sys.argv) > 1 else 3004
TARGET = ([float(v) for v in sys.argv[2].split(",")] if len(sys.argv) > 2
          else [800.0, 2.0, 900.0])
RID = "loop-%d" % ITEM


def read_aabb(gid):
    code = ("c=cm._ugc_id2component[%d]\n"
            "u=c._owner\n"
            "m=u.model\n"
            "bc=m.bounding_center(0)\n"
            "bs=m.bounding_size(0)\n"
            "p3=u.get_position3()\n"
            "__result__={'bc':[round(bc.x,4),round(bc.y,4),round(bc.z,4)],"
            "'bs':[round(bs.x,4),round(bs.y,4),round(bs.z,4)],"
            "'pos':[round(p3.x,4),round(p3.y,4),round(p3.z,4)]}\n" % gid)
    r = call("fuxiang_eval", {"code": code})
    return r.get("result", r) if isinstance(r, dict) else {}


print("构件 %s  目标 %s" % (ITEM, TARGET))
r = call("fuxiang_place_item", {
    "item_id": ITEM, "position": TARGET, "rotation": [0, 0, 0], "scale": 1,
    "mode": "local_preview", "request_id": RID})
dl = time.time() + 60
while isinstance(r, dict) and r.get("status") in ("pending", "send_unknown", "overdue") \
        and time.time() < dl:
    time.sleep(0.5)
    r = call("fuxiang_place_poll", {"request_id": RID})
gid = r.get("ugc_id") if isinstance(r, dict) else None
if not gid:
    print("place FAILED", json.dumps(r, ensure_ascii=False)[:180])
    sys.exit(1)
print("placed ugc_id=%s (position=目标，未做任何校正)" % gid)

a0 = read_aabb(gid)
bc0, bs0, pos0 = a0["bc"], a0["bs"], a0["pos"]
print("  读回: bc=%s bs=%s pos=%s" % (bc0, bs0, pos0))

# 让「AABB 底面中心」落到目标：底面中心 = (bc.x, bc.y - bs.y, bc.z)
base0 = [bc0[0], bc0[1] - bs0[1], bc0[2]]
delta = [TARGET[i] - base0[i] for i in range(3)]
print("  AABB 底面中心=%s  → 需要平移 %s" % ([round(v, 4) for v in base0],
                                            [round(v, 4) for v in delta]))

pos1 = [round(pos0[i] + delta[i], 4) for i in range(3)]
sr = call("fuxiang_set_transform", {"ugc_id": gid, "position": pos1,
                                    "rotation": [0, 0, 0], "scale": 1})
print("  set_transform -> %s" % json.dumps(sr, ensure_ascii=False)[:160])
time.sleep(0.6)

a1 = read_aabb(gid)
bc1, bs1 = a1["bc"], a1["bs"]
base1 = [bc1[0], bc1[1] - bs1[1], bc1[2]]
err = max(abs(base1[i] - TARGET[i]) for i in range(3))
print("  修正后 AABB 底面中心=%s" % [round(v, 4) for v in base1])
print(">>> 与目标误差 %.4f  %s" % (err, "PASS" if err < 0.05 else "FAIL"))

try:
    call("fuxiang_remove_item", {"ugc_id": gid})
    print("cleaned")
except Exception as e:
    print("cleanup warn: %s" % str(e)[:100])
