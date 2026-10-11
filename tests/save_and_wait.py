#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""save_and_wait.py — 发起主动保存并轮询回执直到 saved/timeout。

用法：
  python tests/save_and_wait.py [request_id] [wait_sec_per_poll] [max_polls]
"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../cli")
import mcpcall  # noqa: E402

REQUEST_ID = sys.argv[1] if len(sys.argv) > 1 else "save_%d" % int(time.time() * 1000)
POLL_EVERY = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
MAX_POLLS = int(sys.argv[3]) if len(sys.argv) > 3 else 8


def main():
    print("submit:", json.dumps(mcpcall.call("fuxiang_save_map", {"request_id": REQUEST_ID}, 1800),
                                ensure_ascii=False))
    for i in range(MAX_POLLS):
        time.sleep(POLL_EVERY)
        r = mcpcall.call("fuxiang_save_map_poll", {"request_id": REQUEST_ID, "timeout": max(POLL_EVERY * 3, 30)}, 60)
        st = r.get("status")
        print("poll%d: status=%s elapsed=%s receipts=%d"
              % (i, st, r.get("elapsed"), len(r.get("receipts") or [])))
        if st in ("saved", "timeout", "no_active_request"):
            print("final:", json.dumps(r, ensure_ascii=False, indent=1)[:1500])
            return 0 if st == "saved" else 1
    print("final(pending/timeout):", json.dumps(r, ensure_ascii=False)[:1000])
    return 1


if __name__ == "__main__":
    sys.exit(main())