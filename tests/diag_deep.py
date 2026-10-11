import json
import os
import sys
import time

ROOT = r"<工具根>"
sys.path.insert(0, os.path.join(ROOT, "cli"))
from mcpcall import call          # noqa: E402
from place_solver import get      # noqa: E402

ITEM = 3004
rec = get(ITEM)
mp = rec["model_path"]

r = call("fuxiang_place_item", {
    "item_id": ITEM, "position": [300, 2, 900], "rotation": [0, 0, 0],
    "scale": 1, "mode": "local_preview", "request_id": "diag-deep"})
gid = r.get("ugc_id")
print("placed ugc_id=%s" % gid)
time.sleep(0.8)

probe = r'''
import world
sc = world.get_active_scene()
def vv(x):
    return [round(x.x, 4), round(x.y, 4), round(x.z, 4)]
def dump(m):
    d = {}
    for nm, fn in (('bb', lambda: m.bounding_box),
                   ('bs0', lambda: m.bounding_size(0)),
                   ('bc0', lambda: m.bounding_center(0))):
        try:
            d[nm] = vv(fn())
        except Exception as e:
            d[nm] = 'E:' + str(e)[:50]
    for i in (1, 2, 3):
        try:
            d['bc%d' % i] = vv(m.bounding_center(i))
        except Exception as e:
            d['bc%d' % i] = 'E:' + str(e)[:40]
    for i in range(3):
        try:
            d['sub%d' % i] = str(m.get_submesh_bounding_box(i))[:90]
        except Exception as e:
            d['sub%d' % i] = 'E:' + str(e)[:40]
    for a in ('file_name', 'model_file', 'name', 'res_path'):
        if hasattr(m, a):
            d['attr_' + a] = str(getattr(m, a))[:110]
    return d

out = {}
c = cm._ugc_id2component[__GID__]
u = c._owner
me = u.model
out['entity'] = dump(me)
out['entity_pos'] = vv(u.get_position3())
out['entity_size'] = vv(u.ugc_size)
out['entity_dir'] = vv(u.ugc_dir)
try:
    out['entity_socket_cnt'] = me.get_socket_count()
except Exception as e:
    out['entity_socket_cnt'] = 'E:' + str(e)[:40]
fresh = world.model(__MP__, sc)
out['fresh'] = dump(fresh)
fresh.destroy()
__result__ = out
'''
probe = probe.replace("__GID__", str(gid)).replace("__MP__", repr(mp))

res = call("fuxiang_eval", {"code": probe})
d = res.get("result", res) if isinstance(res, dict) else {}
if isinstance(d, dict) and "entity" in d:
    for k in ("entity", "fresh"):
        print("=== %s ===" % k)
        for kk, vv2 in d[k].items():
            print("   %-18s %s" % (kk, vv2))
    print("entity_pos =", d.get("entity_pos"))
    print("entity_size=", d.get("entity_size"))
    print("entity_dir =", d.get("entity_dir"))
    print("sockets    =", d.get("entity_socket_cnt"))
else:
    print(json.dumps(res, ensure_ascii=False)[:900])

call("fuxiang_remove_item", {"ugc_id": gid})
print("cleaned")
