#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_model_sizes — 批量扫描构件模型的原生包围盒，生成「建筑尺寸库」。

原理
----
`world.model(model_path, scene)` 可以**脱离 UGC 实体**直接创建模型对象；
读取 `bounding_box`（半尺寸）与 `bounding_center` 后立即 `destroy()`。
实测单个耗时 1.5~12ms，全程不在地图里创建任何实体，因此不污染地图数据。

⚠️ 语义（已用三重交叉验证确认）
- `bounding_box` = **半尺寸**（half-extent）。全尺寸 = ×2。
  证据：方块 301001 读出 (5,5,5)，而其 bounding_center.y = 5 = 半高 →
  模型自 y=0 延伸到 y=10，与「GEOMETRY 原生 10×10×10、size 传 17.43 实际 174 高」吻合。
- 摆放换算：`scale = 目标尺寸 / 全尺寸`。
- `bounding_center` = 几何中心相对模型原点的偏移（模型原点通常在底面中心）。

用法
----
  python cli/scan_model_sizes.py --limit 20        # 试跑
  python cli/scan_model_sizes.py                   # 全量核心构件
  python cli/scan_model_sizes.py --all-scene       # 扩大到全部 scene 物件
"""
import os
import sys
import json
import time
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit  # noqa: E402
from fxapi import find_layer as _find_layer  # noqa: E402  # 剥壳真源

ITEMS_JSON = os.path.join(ROOT, "registry", "items.json")
OUT_JSON = os.path.join(ROOT, "registry", "model_sizes.json")

CORE_CATS = {
    ("环境", "墙体"),
    ("环境", "建筑"),
    ("环境", "地板"),
    ("环境", "地形"),
    ("环境", "门"),
    ("环境", "窗户"),
    ("物件", "几何体"),
}

# 陈设/植被类：环境组里的自然物与杂物（`物件` 组整体都算陈设，见 in_furnish）
FURNISH_ENV_CATS = {
    ("环境", "植物"),
    ("环境", "树木"),
    ("环境", "动物"),
    ("环境", "杂物"),
}


def _cats(v):
    return (v.get("category_group_cn"), v.get("category_cn"))


def in_core(v):
    return _cats(v) in CORE_CATS


def in_furnish(v):
    """家具/装饰/植物类。

    `物件` 整组（家具·柜类·座椅·桌子·壁挂·箱盒·桶类·灯具·设备·载具·食物·字符·装饰·物件·其他）
    都属可摆放陈设，因此整组收录；几何体已归 building，此处排除。
    另加环境组的植物/树木/动物/杂物。
    """
    cg, c = _cats(v)
    if cg == "物件" and c != "几何体":
        return True
    return (cg, c) in FURNISH_ENV_CATS

GAME_CODE = (
    "import world, time\n"
    "sc = world.get_active_scene()\n"
    "targets = {TARGETS}\n"
    "res = []\n"
    "t0 = time.time()\n"
    "for iid, path in targets:\n"
    "    it = {'item_id': iid}\n"
    "    m = None\n"
    "    try:\n"
    "        m = world.model(path, sc)\n"
    "        bb = m.bounding_box\n"
    "        bc = m.bounding_center(0)\n"
    "        it['hx'] = bb.x\n"
    "        it['hy'] = bb.y\n"
    "        it['hz'] = bb.z\n"
    "        it['cx'] = bc.x\n"
    "        it['cy'] = bc.y\n"
    "        it['cz'] = bc.z\n"
    "    except Exception as e:\n"
    "        it['err'] = str(e)[:120]\n"
    "    finally:\n"
    "        if m is not None:\n"
    "            try:\n"
    "                m.destroy()\n"
    "            except Exception:\n"
    "                pass\n"
    "    res.append(it)\n"
    "__result__ = {'ms': round((time.time()-t0)*1000.0, 1), 'items': res}\n"
)


def select_targets(preset):
    """preset: core | furnish | all"""
    with open(ITEMS_JSON, "r", encoding="utf-8") as f:
        d = json.load(f)
    rows = []
    for v in d.values():
        if v.get("item_type_group") != "scene":
            continue
        ok = False
        if preset == "core":
            ok = in_core(v)
        elif preset == "furnish":
            ok = in_furnish(v)
        else:
            ok = in_core(v) or in_furnish(v)
        if not ok:
            continue
        path = v.get("model_path")
        if not path:
            continue
        rows.append(v)
    rows.sort(key=lambda x: x["item_id"])
    return d, rows


def unwrap(node):
    """逐层剥掉 ok/value/result 外壳，直到找到含 items 的那一层。

    实测 fxedit.call_edit('eval', ...) 返回的是带 ok/value 外壳的完整响应
    （形如 {'ok':True,'value':{'result':{...}}}），而不是文档注释所说的内层 result，
    所以这里不写死层数，逐层探测。规则见 cli/fxapi.py::find_layer（唯一真源）。
    """
    return _find_layer(node, "items")


def scan_batch(pairs, timeout):
    code = GAME_CODE.replace("{TARGETS}", repr([tuple(p) for p in pairs]))
    return unwrap(call_edit("eval", {"code": code}, timeout=timeout))


def main():
    args = sys.argv[1:]
    limit = None
    batch = 50
    resume = "--resume" in args
    preset = "core"
    if "--all-scene" in args:
        preset = "all"
    if "--preset" in args:
        preset = args[args.index("--preset") + 1]
    if preset not in ("core", "furnish", "all"):
        print("--preset 只能是 core / furnish / all")
        return 2
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--batch" in args:
        batch = int(args[args.index("--batch") + 1])

    d, rows = select_targets(preset)
    if limit:
        rows = rows[:limit]
    total = len(rows)
    label = {"core": "核心建筑构件", "furnish": "家具/装饰/植物类", "all": "建筑+陈设全量"}[preset]
    print("范围: %s  命中 %d 个构件" % (label, total))

    cache = {}
    if resume and os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for it in prev.get("items", []):
                if it.get("size"):
                    cache[it["item_id"]] = it
            print("续跑: 已缓存 %d 条" % len(cache))
        except Exception as e:
            print("缓存读取失败，忽略: %s" % e)

    results = dict(cache)
    failed = []
    todo = [r for r in rows if r["item_id"] not in cache]
    n_batch = (len(todo) + batch - 1) // batch
    print("待扫 %d 个，分 %d 批（每批 %d）" % (len(todo), n_batch, batch))

    for bi in range(n_batch):
        chunk = todo[bi * batch:(bi + 1) * batch]
        pairs = [(c["item_id"], c["model_path"]) for c in chunk]
        got = None
        for attempt in range(3):
            got = scan_batch(pairs, timeout=120.0)
            if got and got.get("items"):
                break
            print("  批 %d 第 %d 次无返回，重试" % (bi + 1, attempt + 1))
            time.sleep(1.0)
        if not got or not got.get("items"):
            failed.extend([c["item_id"] for c in chunk])
            print("  批 %d 失败" % (bi + 1))
            continue

        by_id = {}
        for it in got["items"]:
            by_id[it["item_id"]] = it
        for c in chunk:
            raw = by_id.get(c["item_id"])
            rec = {
                "item_id": c["item_id"],
                "name_cn": c.get("name_cn"),
                "category_group_cn": c.get("category_group_cn"),
                "category_cn": c.get("category_cn"),
                "component_type": c.get("component_type"),
                "model_path": c.get("model_path"),
                "occupancy_value": c.get("occupancy_value"),
                "zoom_limit": c.get("zoom_limit"),
            }
            if raw and "hx" in raw:
                half = [raw["hx"], raw["hy"], raw["hz"]]
                center = [raw["cx"], raw["cy"], raw["cz"]]
                rec["half"] = [round(x, 4) for x in half]
                rec["size"] = [round(x * 2.0, 4) for x in half]
                rec["center"] = [round(x, 4) for x in center]
            else:
                rec["error"] = (raw or {}).get("err", "no-data")
            results[c["item_id"]] = rec

        ok_n = sum(1 for c in chunk if "size" in results.get(c["item_id"], {}))
        print("  批 %d/%d 完成  ok=%d/%d  %sms"
              % (bi + 1, n_batch, ok_n, len(chunk), got.get("ms")))
        time.sleep(0.3)

    ordered = []
    for r in rows:
        rec = results.get(r["item_id"])
        if rec is None:
            continue
        rec = dict(rec)
        rec["scope"] = "building" if in_core(r) else "furnishing"
        ordered.append(rec)
    with_sizes = [r for r in ordered if "size" in r]
    no_size = [r for r in ordered if "size" not in r]

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "registry/items.json + world.model.bounding_box（游戏内实测）",
        "scope": preset,
        "scope_counts": {k: sum(1 for r in ordered if r.get("scope") == k)
                         for k in ("building", "furnishing")},
        "semantics": {
            "half": "bounding_box，半尺寸（half-extent）",
            "size": "实际全尺寸，= half × 2",
            "center": "bounding_center(0)，几何中心相对模型原点的偏移",
            "scale_rule": "摆放时 scale = 目标尺寸 / size 的对应分量",
            "note": "方法为 world.model(path,scene) 直接建模型读包围盒后 destroy，不创建任何 UGC 实体",
        },
        "count": len(ordered),
        "count_with_size": len(with_sizes),
        "count_failed": len(no_size),
        "failed_ids": [r["item_id"] for r in no_size],
        "items": ordered,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("已写入 %s" % OUT_JSON)
    print("成功 %d / 失败 %d" % (len(with_sizes), len(no_size)))
    if failed:
        print("整批失败的批次数含 %d 个物件" % len(failed))
    return 0 if not no_size else 1


if __name__ == "__main__":
    sys.exit(main())
