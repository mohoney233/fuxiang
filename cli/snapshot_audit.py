#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线体检：一张快照到底能被还原多少（不连游戏）。

用法: python cli/snapshot_audit.py <快照.json>
"""
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from snapshot_restore import (classify_components, component_id_of,  # noqa: E402
                              place_spec, event_bind_plan, _dangling_owners,
                              dangling_advice)


def story_counts(snap):
    """快照剧情段的 (剧情, 对话, 选项) 件数。

    ⚠️ `snap["story"]` 是**文档 dict**（键有 format/version/map/stories/…），
    直接 `len()` 会把键数当剧情数报出来（实机印成"剧情 7"，其实是 1 剧情 + 1 对话）。
    """
    st = snap.get("story") or {}
    if not isinstance(st, dict):
        return (0, 0, 0)
    stories = st.get("stories") or []
    dlg = len(st.get("orphan_dialogues") or [])
    cho = len(st.get("orphan_choices") or [])
    for s in stories:
        for d in (s.get("dialogues") or []):
            dlg += 1
            cho += len(d.get("choices") or [])
    return (len(stories), dlg, cho)


def main(path):
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    comps = snap.get("components") or []
    n_story, n_dlg, n_choice = story_counts(snap)
    print("快照：%s（%s）" % (snap.get("map", {}).get("name"), snap.get("captured_at")))
    print("组件 %d ｜ 事件 %d ｜ 剧情 %d（对话 %d / 选项 %d）"
          % (len(comps),
             len((snap.get("events") or {}).get("events") or []),
             n_story, n_dlg, n_choice))

    to_place, skipped = classify_components(comps)
    print("\n== 可摆 %d / 跳过 %d ==" % (len(to_place), len(skipped)))

    # 跳过明细：reason x type
    by_reason = Counter()
    detail = defaultdict(Counter)
    for s in skipped:
        r = s["reason"].split("（")[0]
        by_reason[r] += 1
        detail[r][(s["component_type"], s["i"])] += 1
    for r, n in by_reason.most_common():
        print("\n[%s] %d" % (r, n))
        for (ct, item), k in detail[r].most_common(12):
            print("    type=%-10s item=%-10s x%d" % (ct, item, k))

    # 可摆明细：按类型
    print("\n== 可摆构成 ==")
    c = Counter(str(component_id_of(x)) for x in to_place)
    for t, n in c.most_common(15):
        print("    type=%-10s x%d" % (t, n))

    # 包围盒
    xs, ys, zs = [], [], []
    for x in to_place:
        p = place_spec(x)["position"]
        xs.append(p[0]); ys.append(p[1]); zs.append(p[2])
    if xs:
        print("\n== 场景包围盒（可摆件）==")
        print("    x %.1f .. %.1f   跨度 %.1f" % (min(xs), max(xs), max(xs) - min(xs)))
        print("    y %.1f .. %.1f   跨度 %.1f" % (min(ys), max(ys), max(ys) - min(ys)))
        print("    z %.1f .. %.1f   跨度 %.1f" % (min(zs), max(zs), max(zs) - min(zs)))

    # 属性覆盖面：可摆件里有多少带非空 ci
    with_ci = sum(1 for x in to_place if (x.get("ci") or ""))
    print("\n== 属性 ==")
    print("    可摆件中带 component_info 的 %d / %d" % (with_ci, len(to_place)))

    # 事件
    ev = snap.get("events") or {}
    refs = sum(len(e.get("refs") or []) for e in (ev.get("events") or []))
    placed_old = {x.get("g") for x in to_place}
    fake_map = {g: g for g in placed_old}
    actions, dangling = event_bind_plan(ev, fake_map,
                                        comp_index={c.get("g"): c for c in comps})
    print("\n== 事件 ==")
    print("    事件 %d 条，引用 %d 处 → 可绑（物件）%d，悬空 %d"
          % (len(ev.get("events") or []), refs, len(actions), len(dangling)))
    if dangling:
        olds = {d["old_ugc_id"] for d in dangling if d.get("old_ugc_id") is not None}
        print("    悬空指向：%s" % _dangling_owners(snap, olds))
        print("    ↳ 悬空原因分报（出口各不相同，别一律当剧情节点处理）：")
        for key, n, hint in dangling_advice(dangling,
                                            {s["g"]: s["reason"] for s in skipped}):
            print("        %s ×%d → %s" % (key, n, hint))
    srcs = {}
    for a in actions:
        srcs[a.get("info_source")] = srcs.get(a.get("info_source"), 0) + 1
    if srcs:
        print("    槽位取值来源：%s" % srcs)

    # 交付闸门：**复用 snapshot.py 那一条判据**，不在这里再写一套。
    # 本仓库已经栽过两次"两处各算一遍、后来口径漂移"（悬空原因、剧情计数）。
    # 这一段存在的理由：audit 只看"能摆多少"，看不见待提交删除的身份、UI_NATIVE 整层缺口、
    # 玩法设置没采到这类"能跑但搬不像"的问题 ⇒ 印出来一切正常，收口时才发现不对。
    try:
        import snapshot as _sn
        v = _sn.validate_snapshot(snap)
        flagged = [i for i in v["items"] if i.get("level") != "ok"]
        print("\n== 交付闸门（与 snapshot.py check 同源）==")
        for i in flagged:
            print("    %-8s %s" % (i.get("level"), i.get("msg")))
        if not flagged:
            print("    ✓ %d 项全过（明细：python cli/snapshot.py check %s）"
                  % (len(v["items"]), path))
        print("    结论：%s" % ("可以交付还原" if v["ok"] else "有阻塞项，先修再交付"))
    except Exception as exc:
        print("\n== 交付闸门 == 计算失败：%s" % str(exc)[:140])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python cli/snapshot_audit.py <快照.json>")
        sys.exit(1)
    main(sys.argv[1])
