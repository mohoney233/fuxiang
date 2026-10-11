#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""place_object.py — 用 add_unit/place 管线在指定位置放一个物件，并轮询确认。

用法：
  python tests/place_object.py <item_id> '<x,y,z>' [request_id]
例：
  python tests/place_object.py 202014 600,2,80 p_test_1
"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../cli")
import mcpcall  # noqa: E402

ITEM = sys.argv[1] if len(sys.argv) > 1 else "202014"
POS = [float(v) for v in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["600", "2", "80"])]
REQ = sys.argv[3] if len(sys.argv) > 3 else ("place_%d" % int(time.time() * 1000))


def main():
    args = {"item_id": ITEM, "position": POS, "request_id": REQ, "mode": "server"}
    print("place:", json.dumps(mcpcall.call("fuxiang_place_item", args, 1800), ensure_ascii=False))
    for i in range(12):
        time.sleep(1.0)
        r = mcpcall.call("fuxiang_place_poll", {"request_id": REQ}, 30)
        st = r.get("status") if isinstance(r, dict) else r
        print("poll%d: %s %s" % (i, st, json.dumps(r, ensure_ascii=False)[:200]))
        if st == "placed":
            print("PLACED_OK")
            return 0
        if st in ("ambiguous", "conflict", "finalize_failed"):
            return 1
    print("NOT_CONFIRMED")
    return 1


if __name__ == "__main__":
    sys.exit(main())