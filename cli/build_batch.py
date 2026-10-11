#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_batch — 批量摆放流水线（清单 → 逐件摆放 → 分批保存 → 查重校验）。

把 2026-09-19 实测得到的几条硬结论固化下来，避免每条批量脚本都重踩一遍：

* **用 `cli/fxlong.call_edit`**（不删 `ipc/out` 回执）：`cli/fxedit` 过去每次调用都
  `os.remove` 一个回执文件，几十次以上会累计撞上宿主 safe-delete 批量阈值
  （50/turn）→ 进程被**静默终止**（exit 1、无 traceback）。2026-09-19 起 fxedit 也
  已改为保留回执，本流水线继续用 fxlong（剥壳与抛错语义不变）。
* **`place_item` 返回后立即 `place_poll`**：实测 10/10 首次查询即 `placed`
  （中位 180ms）。旧文档建议的「固定 sleep 0.5s」占单次耗时的 55%，纯属白等。
  只有真拿到 `pending` 才按响应里的 `poll_after_ms` 退避。
* **`request_id` 由 (run_id, index) 稳定派生**：同一清单重跑**天然幂等**
  （命中桥接端 settled 留痕，直接返回原定论，不重发也不重建），因此断点续做
  与失败重试都是安全的。要刻意重摆一遍时用 `--new-run`。
* **每 `--batch` 件就保存一次**：`save_map` 会摘除「未进入该次提交」的已落位对象，
  攒到最后一次性保存会丢件（且它们仍留在组件表里、下次保存会被再次提交 → 重复件）。
* **保存后用 `save_dirty` 的 `dropped_count` 校验**：`save_map_poll` 的 ack
  实测 60s 都收不到，不能作为判据；`save_dirty` 里同时带脏缓冲长度与登记集合差异。

清单格式（JSON，两种都接受）：

    {"name": "villa", "items": [ {...}, ... ]}      # 或直接给数组

每项字段：`item_id`（必填）、`position`（必填，[x,y,z]）、
`rotation`（可选，默认 [0,0,0]）、`scale`（可选，默认 [1,1,1]）、
`tag`（可选，便于对账）、`mode`（可选，默认 server）。

用法：

    python cli/build_batch.py plan.json --dry-run        # 只校验清单
    python cli/build_batch.py plan.json --limit 20       # 试跑前 20 件
    python cli/build_batch.py plan.json --batch 50       # 正式跑，每 50 件保存一次
    python cli/build_batch.py plan.json                  # 再跑一次 = 断点续做（幂等）
    python cli/build_batch.py plan.json --new-run        # 强制全新 run（重摆一遍）
"""
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxlong import call_edit  # noqa: E402

BAD = ("ambiguous", "conflict", "finalize_failed", "send_unknown")
POLL_TRIES = 40
SAVE_DRAIN_TRIES = 30


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------
def _norm_item(it, i):
    """归一化一项清单。兼容 `build_house.py --out` 导出的 parts 格式
    （其 `item_id` / `scale`(=ugc_size 倍率) / `rotation` 已同名，位置键是 `at`）。

    ⚠️ 只接受 `scale` / `ugc_size` 作为缩放 —— 不接受 `size`（那是**目标世界尺寸**，
    与 ugc_size 差一个 2×bounding_box 的系数，混用会让构件尺寸全错）。
    """
    if not isinstance(it, dict):
        raise ValueError("第 %d 项不是对象" % i)
    item_id = it.get("item_id", it.get("item"))
    pos = it.get("position") or it.get("at") or it.get("pos")
    if item_id is None:
        raise ValueError("第 %d 项缺 item_id" % i)
    if not (isinstance(pos, (list, tuple)) and len(pos) == 3):
        raise ValueError("第 %d 项 position/at 必须是 [x,y,z]" % i)
    scale = it.get("scale") or it.get("ugc_size") or (1, 1, 1)
    return {
        "item_id": item_id,
        "position": [float(v) for v in pos],
        "rotation": [float(v) for v in (it.get("rotation") or it.get("rot") or (0, 0, 0))],
        "scale": [float(v) for v in scale],
        "anchor": it.get("anchor"),
        "tag": it.get("tag") or it.get("name"),
        "mode": it.get("mode"),
        "allow_unverified": it.get("allow_unverified"),
    }


def load_plan(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        items = data.get("items") or data.get("parts") or []
        name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
    elif isinstance(data, list):
        items, name = data, os.path.splitext(os.path.basename(path))[0]
    else:
        raise ValueError("清单必须是对象 {name, items} / {parts} 或数组")
    return name, [_norm_item(it, i) for i, it in enumerate(items)]


def run_id_for(items, new_run=False):
    """稳定派生 run_id：同清单重跑复用同一批 request_id → 天然幂等。"""
    if new_run:
        return "r%d" % int(time.time())
    h = hashlib.sha1(json.dumps(items, sort_keys=True, ensure_ascii=False)
                     .encode("utf-8"))
    return "h" + h.hexdigest()[:10]


def token_for(rid, i):
    return "bb_%s_%05d" % (rid, i)


# --------------------------------------------------------------------------
# 单件摆放
# --------------------------------------------------------------------------
def place_one(spec, token):
    """发出摆放并立即轮询。返回 (最后响应, status, poll 次数)。"""
    r = call_edit("place_item", {
        "item_id": spec["item_id"],
        "position": list(spec["position"]),
        "rotation": list(spec.get("rotation") or (0, 0, 0)),
        "scale": list(spec.get("scale") or (1, 1, 1)),
        "request_id": token,
        "mode": spec.get("mode") or "server",
        "allow_unverified": bool(spec.get("allow_unverified")),
    }, timeout=90) or {}
    status, tries = r.get("status"), 0
    while status not in ("placed",) and status not in BAD and tries < POLL_TRIES:
        wait = float(r.get("poll_after_ms") or 0) / 1000.0
        if wait > 0:
            time.sleep(wait)
        r = call_edit("place_poll", {"request_id": token}, timeout=90) or {}
        status = r.get("status")
        tries += 1
    return r, status, tries


# --------------------------------------------------------------------------
# 保存与校验
# --------------------------------------------------------------------------
def save_and_check(save_token):
    """发起保存并等脏缓冲排空；返回最后一次 save_dirty 结果。

    ⚠️ 必须先主动释放可能残留的保存 active：`op_save_map` 有「单笔在途」护栏，
    而本流水线**不依赖 ack**（只用脏缓冲排空判断已上送），从不走 `save_map_poll` 的
    saved 分支 —— 若不主动释放，第二次起的 `save_map` 会被护栏**直接拒绝**，
    脏缓冲永不排空，表现为每批白等 30s 超时（踩过：44 件跑到 1929ms/件）。
    """
    try:
        # timeout 极小 = 「距上次请求已过 timeout 即放弃等待并释放 active」
        call_edit("save_map_poll", {"timeout": 0.01}, timeout=30)
    except Exception:
        pass

    r = call_edit("save_map", {"request_id": save_token}, timeout=90) or {}
    if not r.get("ok"):
        return {"submitted": False, "save_error": r.get("error"),
                "note": "save_map 被拒，本批未提交"}

    d = {}
    for _ in range(SAVE_DRAIN_TRIES):
        time.sleep(0.8)
        try:
            d = call_edit("save_dirty", {}, timeout=60) or {}
        except Exception as exc:
            print("    save_dirty 读取失败：%s" % str(exc)[:120], flush=True)
            continue
        if d.get("change_units_length") == 0 and d.get("save_events_length") == 0:
            d["submitted"] = True
            return d
    d["submitted"] = False
    d["note"] = "脏缓冲未在 %d 次轮询内排空" % SAVE_DRAIN_TRIES
    return d


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="批量摆放流水线")
    ap.add_argument("plan", help="清单 JSON 路径")
    ap.add_argument("--batch", type=int, default=50, help="每多少件保存一次（默认 50）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 件（试跑）")
    ap.add_argument("--dry-run", action="store_true", help="只校验清单，不写任何东西")
    ap.add_argument("--new-run", action="store_true", help="强制全新 run（重摆一遍）")
    ap.add_argument("--stop-on-error", action="store_true", help="遇到失败立即停止")
    ap.add_argument("--progress", default=None, help="进度文件（jsonl），默认自动生成")
    a = ap.parse_args()

    name, items = load_plan(a.plan)
    if a.limit:
        items = items[:a.limit]
    rid = run_id_for(items, a.new_run)
    prog = a.progress or os.path.join(ROOT, "builds", "batch",
                                      "%s_%s.progress.jsonl" % (name, rid))
    os.makedirs(os.path.dirname(prog), exist_ok=True)

    print("清单「%s」：%d 件 ｜ run_id=%s" % (name, len(items), rid), flush=True)
    print("进度文件：%s" % prog, flush=True)
    rotated = sum(1 for it in items if any(abs(v) > 0.01 for v in it["rotation"]))
    if rotated:
        print("提示：%d 件带旋转 —— 本流水线按 position 直摆、不做闭环修正，"
              "斜置件可能有偏移（实测斜屋顶板偏差可达 ~16）。需要精确对位请用 "
              "cli/place_exact.py 或 build_house（二者内置闭环）。" % rotated, flush=True)

    if a.dry_run:
        for i, it in enumerate(items[:10]):
            print("  [%d] item=%-8s pos=%s tag=%s"
                  % (i, it["item_id"], it["position"], it.get("tag") or "-"), flush=True)
        if len(items) > 10:
            print("  ...（共 %d 件）" % len(items), flush=True)
        print("dry-run：未做任何写操作。", flush=True)
        return 0

    # 断点续做：读取已成功的记录
    done = set()
    if os.path.exists(prog):
        with open(prog, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("status") == "placed":
                    done.add(rec["i"])
        if done:
            print("断点续做：跳过已完成的 %d 件" % len(done), flush=True)

    placed = failed = skipped = unsaved = 0
    drops = []
    t0 = time.perf_counter()
    pending_save = 0

    for i, spec in enumerate(items):
        token = token_for(rid, i)
        if i in done:
            skipped += 1
            continue
        tag = spec.get("tag") or ""
        try:
            r, status, tries = place_one(spec, token)
        except Exception as exc:
            status, r, tries = "exception", {"error": str(exc)[:300]}, -1
        rec = {"i": i, "token": token, "tag": tag, "status": status,
               "poll_tries": tries, "ugc_id": (r or {}).get("ugc_id"),
               "t": round(time.time(), 3)}
        if status == "placed":
            placed += 1
            pending_save += 1
            # 只在每 10 件或异常时打印，避免刷屏
            if placed % 10 == 0:
                print("  [%4d/%d] placed=%d 失败=%d" % (i + 1, len(items), placed, failed),
                      flush=True)
        else:
            failed += 1
            rec["note"] = (r or {}).get("note") or (r or {}).get("error")
            print("  [%4d] %s → %s ｜ %s"
                  % (i + 1, tag or ("item %s" % spec["item_id"]), status,
                     str(rec.get("note"))[:150]), flush=True)
        with open(prog, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if status != "placed" and a.stop_on_error:
            print("遇到失败且指定 --stop-on-error，停止。", flush=True)
            break

        # 分批保存：避免「未进入本次提交的已落位对象」被摘除登记集合
        if pending_save >= a.batch:
            print("  —— 已摆 %d 件，保存并校验 ——" % pending_save, flush=True)
            d = save_and_check("bb_save_%d" % int(time.time()))
            dropped = d.get("dropped_count")
            if not d.get("submitted"):
                unsaved += pending_save
                print("     ⚠️ 本批保存未确认上送：%s"
                      % (d.get("save_error") or d.get("note")), flush=True)
            print("     change_units=%s dropped=%s%s"
                  % (d.get("change_units_length"), dropped,
                     ("（有对象被回滚，见 dropped_uids）" if dropped else "")), flush=True)
            if dropped:
                drops.append({"at": i, "dropped_count": dropped,
                              "dropped_uids": d.get("dropped_uids")})
            pending_save = 0

    # 收尾保存
    if pending_save:
        print("  —— 收尾：保存剩余 %d 件 ——" % pending_save, flush=True)
        d = save_and_check("bb_save_%s" % int(time.time()))
        if not d.get("submitted"):
            unsaved += pending_save
            print("     ⚠️ 收尾保存未确认上送：%s"
                  % (d.get("save_error") or d.get("note")), flush=True)
        if d.get("dropped_count"):
            drops.append({"at": "tail", "dropped_count": d.get("dropped_count"),
                          "dropped_uids": d.get("dropped_uids")})

    elapsed = time.perf_counter() - t0
    print("\n===== 批量摆放结果 =====", flush=True)
    print("清单 %s ｜ %d 件 ｜ placed %d ｜ 失败 %d ｜ 续做跳过 %d"
          % (name, len(items), placed, failed, skipped), flush=True)
    print("耗时 %.1fs（%.0f ms/件）" % (elapsed, elapsed * 1000 / max(1, placed)), flush=True)
    if drops:
        print("⚠️ 有 %d 次保存出现「被摘除登记集合」的对象，明细见进度文件。" % len(drops),
              flush=True)
    else:
        print("保存校验：未见对象被摘除登记集合。", flush=True)
    if unsaved:
        print("⚠️ 有 %d 件所在批次未确认上送（保存被拒或脏缓冲未排空），可能未持久化。"
              % unsaved, flush=True)
    print("进度：%s" % prog, flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
