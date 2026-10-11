import json
import os
import sys
import time

ROOT = r"<工具根>"
sys.path.insert(0, os.path.join(ROOT, "cli"))
from mcpcall import call          # noqa: E402
from place_solver import get      # noqa: E402

ITEM = int(sys.argv[1]) if len(sys.argv) > 1 else 3004
rec = get(ITEM)
path = rec["model_path"]

r = call("fuxiang_place_item", {
    "item_id": ITEM, "position": [300, 2, 900], "rotation": [0, 0, 0],
    "scale": 1, "mode": "local_preview", "request_id": "diag-lod-%d" % ITEM})
gid = r.get("ugc_id")
print("placed ugc_id=%s" % gid)
time.sleep(0.8)

code = (
    "import world\n"
    "sc = world.get_active_scene()\n"
    "def vv(x):\n"
    "    return [round(x.x, 4), round(x.y, 4), round(x.z, 4)]\n"
    "c = cm._ugc_id2component[%d]\n"
    "u = c._owner\n"
    "m = u.model\n"
    "fresh = world.model(%r, sc)\n"
    "out = {\n"
    " 'entity': {'bb': vv(m.bounding_box), 'bc0': vv(m.bounding_center(0)),"
    " 'bs0': vv(m.bounding_size(0)), 'file': str(getattr(m, 'file_name', '?'))},\n"
    " 'fresh': {'bb': vv(fresh.bounding_box), 'bc0': vv(fresh.bounding_center(0)),"
    " 'bs0': vv(fresh.bounding_size(0)), 'file': str(getattr(fresh, 'file_name', '?'))},\n"
    " 'pos': vv(u.get_position3()), 'scale': vv(u.ugc_size)}\n"
    "fresh.destroy()\n"
    "__result__ = out\n"
) % (gid, path)

res = call("fuxiang_eval", {"code": code})
d = res.get("result", res) if isinstance(res, dict) else {}
if isinstance(d, dict) and "entity" in d:
    print("ENTITY bb=%s" % d["entity"]["bb"])
    print("       bc0=%s" % d["entity"]["bc0"])
    print("FRESH  bb=%s" % d["fresh"]["bb"])
    print("       bc0=%s" % d["fresh"]["bc0"])
    print("差值   bb=%s" % [round(d["entity"]["bb"][i] - d["fresh"]["bb"][i], 4) for i in range(3)])
    print("       bc0=%s" % [round(d["entity"]["bc0"][i] - d["fresh"]["bc0"][i], 4) for i in range(3)])
else:
    print(json.dumps(res, ensure_ascii=False)[:800])

d = call("fuxiang_remove_item", {"ugc_id": gid})
print("removed:", json.dumps(d, ensure_ascii=False)[:120])
