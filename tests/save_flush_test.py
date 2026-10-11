#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""save_flush_test.py — 用『脏缓冲排空』验证主动保存接口是否真正上送服务端。

原理：游戏只在改动集被服务端受理并处理后，才会清空 ui.change_units / cm.save_component_info。
流程：移动几何体(制造脏) → 读脏状态 → fuxiang_save_map → 等待 → 再读脏状态。
若保存后被清空 => 改动已上送服务端（持久化信号，不依赖退出重进）。

用法：python tests/save_flush_test.py  [ugc_id] [x y z] [wait_sec]
默认移动 ugc_id=114 到 [-450,0,120]。
"""
import sys, os, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../cli")
import mcpcall  # noqa: E402

UGC_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 114
POS = [float(v) for v in sys.argv[2:5]] if len(sys.argv) >= 5 else [-450.0, 0.0, 120.0]
WAIT = float(sys.argv[5]) if len(sys.argv) > 5 else 6.0


def dirty():
    return mcpcall.call("fuxiang_save_dirty", {}, 60)


def main():
    def short(d):
        return {k: v for k, v in d.items() if k in
                ("ok", "change_units_length", "change_units_per_key",
                 "save_events_length", "is_save_component_info")}

    print("[1] 移动 ugc_id=%s -> %s" % (UGC_ID, POS))
    mv = mcpcall.call("fuxiang_edit_transform", {"ugc_id": UGC_ID, "position": POS}, 60)
    print("    move:", json.dumps(mv, ensure_ascii=False)[:400])

    d1 = dirty()
    print("[2] 保存前脏状态:", json.dumps(short(d1), ensure_ascii=False))
    dirty_before = (d1.get("change_units_length") or 0) + (d1.get("save_events_length") or 0)

    print("[3] 发起 fuxiang_save_map ...")
    st = mcpcall.call("fuxiang_save_map", {"request_id": "flush_test"}, 1800)
    print("    submit:", json.dumps(st, ensure_ascii=False))

    time.sleep(WAIT)
    d2 = dirty()
    print("[4] 保存后脏状态:", json.dumps(short(d2), ensure_ascii=False))
    dirty_after = (d2.get("change_units_length") or 0) + (d2.get("save_events_length") or 0)

    print("=" * 60)
    if dirty_before > 0 and dirty_after == 0:
        print("判定：保存前有改动(%d) → 保存后被清空(0) => 改动已上送服务端并受理（持久化信号成立）✔"
              % dirty_before)
        return 0
    if dirty_before == 0:
        print("判定：保存前就是干净的(0)，没有待保存改动可上送；本次无法验证（需先制造改动）")
        return 2
    print("判定：保存前有改动(%d) 但保存后仍剩(%d) => 服务端尚未受理/未排空，保存可能未真正落库 ✘"
          % (dirty_before, dirty_after))
    return 1


if __name__ == "__main__":
    sys.exit(main())