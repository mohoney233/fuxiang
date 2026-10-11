#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 接口层端到端验证：证明编辑器里的组件与物体确实「可通过接口被调用」。

与 live_scene_smoke.py 的差别：那些脚本直连 ``cli/fxcall.py`` 走 ipc 目录，
只验证了游戏侧 API；本脚本启动**真正的 MCP stdio 服务器**，按 MCP 协议
（initialize / tools/list / tools/call）发起调用，验证对外暴露的接口层。

用法：
    python tests/mcp_interface_check.py            # 冒烟：工具清单 + 关键调用
    python tests/mcp_interface_check.py --sweep    # 追加全量扫描（105 类型 + 抽样物件）
    python tests/mcp_interface_check.py --sweep --sample-items 120

退出码 0 表示全部断言通过。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "mcp", "server.py")
DEFAULT_PY = r"<你的-python.exe-完整路径>"

CATALOG_TOOLS = [
    "fuxiang_catalog", "fuxiang_find", "fuxiang_component_detail",
    "fuxiang_component_schema", "fuxiang_enums", "fuxiang_categories",
    "fuxiang_catalog_export", "fuxiang_catalog_items", "fuxiang_catalog_verify",
]

RESOURCE_HALL_TOOLS = [
    "fuxiang_resource_hall", "fuxiang_resource_hall_items",
    "fuxiang_resource_hall_item", "fuxiang_resource_hall_verify",
    "fuxiang_resource_hall_ui", "fuxiang_resource_hall_cards",
    "fuxiang_resource_hall_export",
]

EDIT_TOOLS = [
    "fuxiang_edit_targets", "fuxiang_edit_target", "fuxiang_edit_transform",
    "fuxiang_edit_props", "fuxiang_edit_history",
]

PREVIEW_TOOLS = [
    "fuxiang_preview_plan", "fuxiang_preview_shot", "fuxiang_preview_sweep",
    "fuxiang_preview_layout", "fuxiang_preview_restore", "fuxiang_preview_state",
]


class McpClient:
    def __init__(self, python_exe):
        self.proc = subprocess.Popen(
            [python_exe, "-u", SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=ROOT, text=True, encoding="utf-8", bufsize=1)
        self._id = 0

    def call(self, method, params=None, timeout=180.0):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()[:2000]
            raise RuntimeError("MCP 服务器无响应（可能已退出）：%s" % err)
        return json.loads(line)

    def tool(self, name, args=None, timeout=180.0):
        return self.call("tools/call", {"name": name, "arguments": args or {}}, timeout)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def payload(resp):
    """取 tools/call 的文本内容并解析为 JSON。"""
    if resp.get("result", {}).get("isError"):
        txt = resp["result"]["content"][0]["text"]
        return None, txt
    txt = resp["result"]["content"][0]["text"]
    try:
        return json.loads(txt), None
    except Exception:
        return None, txt[:500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=DEFAULT_PY if os.path.exists(DEFAULT_PY) else sys.executable)
    ap.add_argument("--sweep", action="store_true", help="追加全量扫描")
    ap.add_argument("--sample-items", type=int, default=120,
                    help="全量扫描时抽样的物件数（0 表示不抽样）")
    ap.add_argument("--out", default=os.path.join(HERE, "mcp_interface_result.json"))
    args = ap.parse_args()

    report = {"python": args.python, "started": time.time(), "checks": [], "failures": []}

    def check(name, ok, detail=None):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            report["failures"].append({"check": name, "detail": detail})
        print(("  [OK]   " if ok else "  [FAIL] ") + name +
              ("" if ok else "  -> %s" % (detail,)))

    cli = McpClient(args.python)
    try:
        # 1) 握手
        init = cli.call("initialize", {"protocolVersion": "2024-11-05",
                                       "capabilities": {}, "clientInfo": {"name": "check"}})
        info = init.get("result", {}).get("serverInfo", {})
        report["server_info"] = info
        check("initialize 握手", info.get("name") == "fuxiang-ugc-editor", info)
        print("  服务器: %s v%s" % (info.get("name"), info.get("version")))

        # 2) 工具清单
        tlist = cli.call("tools/list")
        names = [t["name"] for t in tlist["result"]["tools"]]
        report["tool_count"] = len(names)
        report["tools"] = names
        missing = [n for n in CATALOG_TOOLS + RESOURCE_HALL_TOOLS + EDIT_TOOLS
                   + PREVIEW_TOOLS if n not in names]
        check("tools/list 暴露全部 catalog / 资源大厅 / 编辑 / 预览工具", not missing,
              "缺失: %s" % missing)
        print("  工具总数: %d" % len(names))

        # 3) 连接状态
        st, err = payload(cli.tool("fuxiang_status"))
        check("fuxiang_status 连通", bool(st and st.get("editor_core")), err or st)

        # 4) 全量注册表概览
        cat, err = payload(cli.tool("fuxiang_catalog"))
        counts = (cat or {}).get("counts", {})
        report["catalog_counts"] = counts
        check("fuxiang_catalog 概览",
              counts.get("items_total") == 3963 and counts.get("component_types_total") == 105,
              counts)
        print("  物件 %s 项 / 组件类型 %s 种 / 可创建 %s" % (
            counts.get("items_total"), counts.get("component_types_total"),
            counts.get("component_types_creatable")))

        # 5) 全量自检
        ver, err = payload(cli.tool("fuxiang_catalog_verify"))
        report["verify"] = ver
        check("fuxiang_catalog_verify 全量可达", bool(ver and ver.get("complete")), err or ver)
        if ver:
            print("  可达: 物件 %s/%s  组件类型 %s/%s" % (
                ver["reachable"]["items"], ver["items_total"],
                ver["reachable"]["component_types"], ver["component_types_total"]))
            # 「可达」按**进程内已加载的 UGC*Component 实现类**判定，因此与本次
            # 游戏会话的历史操作有关：刚重启、没做过任何创建/摆放时，实现类尚未
            # 被按需 import，可达数会明显低于 105。物件侧（3963）不受影响。
            unreachable = ver.get("component_types_unreachable") or []
            total_unreachable = ver.get("component_types_unreachable_count", len(unreachable))
            reasons = {}
            for u in unreachable:
                reasons[u.get("reason")] = reasons.get(u.get("reason"), 0) + 1
            if reasons:
                print("  组件类型不可达 %d 个，原因分布（按返回的前 %d 条统计）: %s"
                      % (total_unreachable, len(unreachable), reasons))
                print("  提示：'已登记但无实现类' 通常是本会话尚未 import 对应实现类所致；"
                      "在游戏里创建/摆放一次相关物件后可复现完整可达。"
                      "物件侧可达性与本现象无关。")

        # 6) 枚举：全量（只取计数与首行，避免传输 2.5MB）
        it, err = payload(cli.tool("fuxiang_catalog_items", {"limit": 1}))
        check("fuxiang_catalog_items 可枚举全部物件",
              bool(it) and it.get("matched") == 3963, err or (it or {}).get("matched"))

        for vis, expect in (("hidden", 43), ("ui_hidden", None), ("listed", None)):
            r, err = payload(cli.tool("fuxiang_catalog_items",
                                      {"limit": 1, "visibility": vis}))
            ok = bool(r)
            if ok and expect is not None:
                ok = r.get("matched") == expect
            check("visibility=%s 过滤" % vis, ok, err or (r or {}).get("matched"))
            if r and vis == "ui_hidden":
                print("  当前编辑模式下因分类屏蔽而不显示: %s 项" % r.get("matched"))
                report["ui_hidden_count"] = r.get("matched")

        # 7) 单物件定位（含隐藏记录的同名区分）
        one, err = payload(cli.tool("fuxiang_find", {"item_id": 202014}))
        m = (one or {}).get("matches") or []
        check("fuxiang_find item_id=202014",
              len(m) == 1 and m[0]["item"]["component_type"] == 202014, err or m)
        hid, err = payload(cli.tool("fuxiang_find", {"item_id": 202009}))
        m2 = (hid or {}).get("matches") or []
        check("fuxiang_find item_id=202009（隐藏记录）",
              len(m2) == 1 and m2[0]["item"]["catalog_hidden"] is True, err or m2)

        # 8) 孤儿组件类型：有实现类但目录无物件
        orp, err = payload(cli.tool("fuxiang_component_detail", {"component_type": 203002}))
        report["slow_region"] = orp
        # 类是否「当前已加载」会随懒加载波动，故只断言稳定身份：登记 + 名 + 目录无物件
        check("fuxiang_component_detail 203002 (SLOW_REGION)",
              bool(orp) and orp.get("name") == "SLOW_REGION" and orp.get("registered") is True
              and orp.get("catalog_item_count") == 0, err or orp)
        if orp:
            print("  203002 %s / class=%s (loaded=%s) / creatable=%s / 目录物件=%s" % (
                orp.get("name"), orp.get("class"), orp.get("class_resolvable"),
                orp.get("creatable"), orp.get("catalog_item_count")))

        # 9) 无实现类的目录编号
        bad, err = payload(cli.tool("fuxiang_component_detail", {"component_type": 201005}))
        check("fuxiang_component_detail 201005（无实现类）",
              bool(bad) and bad.get("creatable") is False, err or bad)

        # 10) 分类树
        cats, err = payload(cli.tool("fuxiang_categories"))
        report["category_count"] = (cats or {}).get("total")
        check("fuxiang_categories 分类树",
              bool(cats) and cats.get("total", 0) >= 80, err or (cats or {}).get("total"))

        # 11) 属性 schema 与枚举
        sch, err = payload(cli.tool("fuxiang_component_schema", {"component_type": 202014}))
        check("fuxiang_component_schema 202014", bool(sch and sch.get("available")), err or sch)
        en, err = payload(cli.tool("fuxiang_enums"))
        report["enum_count"] = (en or {}).get("total")
        check("fuxiang_enums 取值域", bool(en) and en.get("total", 0) >= 30,
              err or (en or {}).get("total"))

        # 12) 类型清单（必须含 105 种）
        lt, err = payload(cli.tool("fuxiang_list_types", {"limit": 0}))
        types = (lt or {}).get("types") or []
        report["list_types_total"] = (lt or {}).get("total")
        check("fuxiang_list_types 覆盖全部类型",
              len(types) == 105, err or "返回 %d 条" % len(types))

        # 13) 资源大厅（组合库）
        hi, err = payload(cli.tool("fuxiang_resource_hall"))
        report["resource_hall"] = {
            k: (hi or {}).get(k) for k in
            ("hall_categories", "hall_sorts", "library_types", "libraries", "loaded")}
        check("fuxiang_resource_hall 大厅结构与库状态",
              bool(hi) and (hi.get("hall_categories") or {}).get("OFFICIAL") == 20001
              and (hi.get("library_types") or {}).get("SHARED") == 2,
              err or hi)
        if hi:
            print("  大厅分类 %s / 排序 %s" % (
                (hi.get("hall_categories") or {}), (hi.get("hall_sorts") or {})))

        rh, err = payload(cli.tool("fuxiang_resource_hall_items", {"limit": 0}))
        rows = (rh or {}).get("items") or []
        report["resource_hall_items"] = {"total": (rh or {}).get("total"),
                                         "official": (rh or {}).get("official_count")}
        check("fuxiang_resource_hall_items 可枚举组合记录",
              bool(rh) and rh.get("total") == len(rows), err or rh)
        if rows:
            print("  组合记录 %d 条（官方 %s 条）：%s" % (
                rh.get("total"), rh.get("official_count"),
                ", ".join("%s=%s" % (r.get("combo_id"), r.get("name_cn") or r.get("name"))
                          for r in rows[:3])))

        # 单条详情：用清单里的真实 ID 回读（服务端可能更换，故不写死）
        if rows:
            cid = rows[0]["combo_id"]
            one, err = payload(cli.tool("fuxiang_resource_hall_item", {"combo_id": cid}))
            ok = bool(one and one.get("found") and one.get("combo_id") == cid)
            check("fuxiang_resource_hall_item 按 combo_id 回读 %s" % cid, ok, err or one)
            cd = (one or {}).get("combo_data") or {}
            report["resource_hall_item"] = {
                "combo_id": cid, "name_cn": (one or {}).get("name_cn"),
                "official": (one or {}).get("is_official_pgc"),
                "child_count": ((one or {}).get("combo_data_parts") or {}).get("child"),
                "combo_data_keys": sorted(cd.keys()),
                "combo_data_available": (one or {}).get("combo_data_available")}
            # combo_data 只在「已下载」后才有；只有元数据是合法状态，接口须显式说明
            parts = (one or {}).get("combo_data_parts")
            check("fuxiang_resource_hall_item 说明 combo_data 可用性",
                  bool(one) and (bool(parts) or one.get("combo_data_available") is False),
                  err or (one or {}).get("combo_data_hint"))
            if one:
                print("  %s (%s) 官方=%s 子物件=%s combo_data=%s" % (
                    cid, one.get("name_cn"), one.get("is_official_pgc"),
                    (one.get("combo_data_parts") or {}).get("child"),
                    sorted(cd.keys()) or ("不可用" if one.get("combo_data_available") is False
                                          else "未知")))

        miss, err = payload(cli.tool("fuxiang_resource_hall_item", {"combo_id": "0"}))
        check("fuxiang_resource_hall_item 未知 ID 返回 found=false",
              bool(miss) and miss.get("found") is False, err or miss)

        hv, err = payload(cli.tool("fuxiang_resource_hall_verify"))
        report["resource_hall_verify"] = hv
        check("fuxiang_resource_hall_verify 记录结构自检",
              bool(hv) and hv.get("complete") is True, err or hv)

        # 14) 资源大厅【实况】（需要游戏内已打开大厅）
        hui, err = payload(cli.tool("fuxiang_resource_hall_ui"))
        report["resource_hall_ui"] = hui
        if hui and hui.get("open"):
            tax = hui.get("taxonomy") or {}
            cats = {c.get("child_id"): c.get("name_cn") for c in (tax.get("categories") or [])}
            report["resource_hall_categories"] = cats
            check("fuxiang_resource_hall_ui 三个页签含中文名",
                  cats.get(20001) == "官方推荐" and cats.get(20002) == "组合分享"
                  and cats.get(20003) == "我的组合", err or cats)
            print("  页签: %s / 当前 %s(%s) 卡片 %s 张" % (
                cats, hui.get("state", {}).get("category_name"),
                hui.get("state", {}).get("sort_name"), hui.get("card_count")))

            hc, err = payload(cli.tool("fuxiang_resource_hall_cards", {"limit": 0}))
            cards = (hc or {}).get("cards") or []
            report["resource_hall_cards"] = {
                "open": (hc or {}).get("open"), "total": (hc or {}).get("total"),
                "category": (hc or {}).get("category_name"),
                "sample_names": [c.get("name") for c in cards[:8]]}
            check("fuxiang_resource_hall_cards 枚举当前页签资源",
                  bool(hc) and hc.get("total") == len(cards) and hc.get("total", 0) > 0,
                  err or hc)
            if cards:
                c0 = cards[0]
                check("卡片字段完整（ID/中文名/分享码/标签/作者）",
                      all(c0.get(k) is not None for k in
                          ("combo_id", "name", "share_code", "tag", "author", "occupy_num")),
                      c0)
                print("  资源 %d 条，示例：%s" % (
                    hc.get("total"), "、".join(str(c.get("name")) for c in cards[:6])))

            # 中文关键字搜索
            q, err = payload(cli.tool("fuxiang_resource_hall_cards",
                                      {"query": "桌", "limit": 5}))
            check("fuxiang_resource_hall_cards 中文关键字搜索",
                  bool(q) and q.get("total", 0) > 0, err or (q or {}).get("total"))
            if q and q.get("cards"):
                print("  搜索「桌」命中 %d 条，如 %s" % (
                    q.get("total"), "、".join(str(c.get("name")) for c in q["cards"][:4])))

            ex, err = payload(cli.tool("fuxiang_resource_hall_export"))
            report["resource_hall_export"] = ex
            check("fuxiang_resource_hall_export 导出快照",
                  bool(ex) and ex.get("count") == (hc or {}).get("total"), err or ex)
            if ex:
                print("  快照: %s (%s 条, %s 字节)" % (
                    ex.get("exported"), ex.get("count"), ex.get("bytes")))
        else:
            check("fuxiang_resource_hall_ui（大厅未打开则跳过实况检查）", True,
                  "open=false，需在游戏内打开资源大厅")
            print("  提示：资源大厅当前未打开，已跳过实况清单检查")

        # 15) 可编辑对象的数值修改（默认只读：清单 / 详情 / dry_run / 护栏）
        ets, err = payload(cli.tool("fuxiang_edit_targets", {"limit": 0}))
        rows = (ets or {}).get("targets") or []
        report["edit_targets"] = {"total": (ets or {}).get("total"),
                                  "editable": (ets or {}).get("editable"),
                                  "blocked": (ets or {}).get("blocked")}
        check("fuxiang_edit_targets 列出对象与可编辑性",
              bool(ets) and ets.get("total", 0) > 0, err or ets)
        if ets:
            print("  对象 %s 个，可编辑 %s 个，受限 %s 个" % (
                ets.get("total"), ets.get("editable"), ets.get("blocked")))

        editable = [r for r in rows if r.get("editable") and r.get("transform", {}).get("position")]
        if editable:
            tgt = editable[0]
            uid = tgt["ugc_id"]
            one, err = payload(cli.tool("fuxiang_edit_target",
                                       {"ugc_id": uid, "with_schema": True}))
            sch = (one or {}).get("schema") or {}
            report["edit_target"] = {"ugc_id": uid, "found": (one or {}).get("found"),
                                     "schema_property_count": sch.get("property_count")}
            check("fuxiang_edit_target 返回变换+属性+schema",
                  bool(one) and one.get("found") and (sch.get("property_count") or 0) > 0,
                  err or one)
            if one:
                print("  ugc_id=%s 组件 %s，属性定义 %s 项（已设 %s 项）" % (
                    uid, (one.get("target") or {}).get("component_type"),
                    sch.get("property_count"), sch.get("set_count")))

            pos = tgt["transform"]["position"]
            moved = [float(pos[0]) + 5.0, float(pos[1]), float(pos[2])]
            dr, err = payload(cli.tool("fuxiang_edit_transform",
                                       {"ugc_id": uid, "position": moved, "dry_run": True}))
            after, _ = payload(cli.tool("fuxiang_edit_target", {"ugc_id": uid}))
            now = ((after or {}).get("target") or {}).get("transform", {}).get("position")
            check("fuxiang_edit_transform dry_run 不落盘",
                  bool(dr) and dr.get("dry_run") is True and now == pos,
                  err or {"dry": dr, "now": now})

            first_pid = ((sch.get("properties") or [{}])[0] or {}).get("property_id")
            if first_pid:
                # 护栏：拒绝时接口会返回 error，MCP 侧表现为 isError=true（文本不是 JSON）
                gd, gerr = payload(cli.tool("fuxiang_edit_props",
                                            {"ugc_id": uid,
                                             "props": {"99999999999": [1]}}))
                text = (gerr or json.dumps(gd, ensure_ascii=False)) if (gerr or gd) else ""
                check("fuxiang_edit_props 拒绝未知属性（护栏）",
                      "不在该组件类型的 schema 中" in (text or ""), text[:200])
        else:
            check("（无可编辑对象则跳过编辑写入检查）", True, "无 editable 目标")

        # 角色类不能改变换（护栏）
        roles = [r for r in rows if not r.get("editable")
                 and any("不支持 UGC 变换接口" in str(x) for x in (r.get("blocked_by") or []))]
        if roles:
            rr, rerr = payload(cli.tool("fuxiang_edit_transform",
                                        {"ugc_id": roles[0]["ugc_id"],
                                         "position": [0, 0, 0]}))
            text = (rerr or json.dumps(rr, ensure_ascii=False)) if (rerr or rr) else ""
            check("角色类改变换被明确拒绝（护栏）",
                  "不支持 UGC 变换接口" in (text or ""), text[:200])

        hh, err = payload(cli.tool("fuxiang_edit_history", {"limit": 5}))
        report["edit_history"] = hh
        check("fuxiang_edit_history 可读", bool(hh) and "count" in hh, err or hh)

        # 11d) 预览渲染（路线 A 引擎出图 / 路线 C 平面图）
        pst, err = payload(cli.tool("fuxiang_preview_state"))
        report["preview_state"] = pst
        cam_before = (pst or {}).get("camera", {})
        check("fuxiang_preview_state 读到相机与出图接口",
              bool(pst and pst.get("has_cam") and pst.get("has_capture_api")
                   and cam_before.get("ok")),
              err or pst)
        print("  相机: %s 投影=%s 出图接口=%s" % (
            cam_before.get("position"),
            "透视" if cam_before.get("perspective") else "正交",
            (pst or {}).get("has_capture_api")))

        ppl, err = payload(cli.tool("fuxiang_preview_plan",
                                    {"fit": "content", "views": ["top", "iso_ne"]}))
        report["preview_plan"] = ppl
        ok_plan = bool(ppl and ppl.get("ok") and ppl.get("views")
                       and ppl.get("content_count", 0) > 0)
        check("fuxiang_preview_plan 出包围盒与视角列表", ok_plan, err or ppl)
        if ok_plan:
            print("  空间物件 %s / 边界锚点 %s / 非空间组件 %s；视角 %s" % (
                ppl.get("content_count"), ppl.get("boundary_count"),
                ppl.get("non_spatial_count"), [v["name"] for v in ppl["views"]]))
            b = ppl.get("bounds") or {}
            check("取景范围未被 y 哨兵（-65536）污染",
                  bool(b) and abs((b.get("min") or [0, 0, 0])[1]) < 1000, b)

        lay, err = payload(cli.tool("fuxiang_preview_layout",
                                    {"fit": "content",
                                     "out": os.path.join(ROOT, "preview",
                                                         "layout_selfcheck.png")}))
        report["preview_layout"] = {k: v for k, v in (lay or {}).items()
                                    if k not in ("objects", "boundary", "non_spatial")}
        check("fuxiang_preview_layout 生成平面图 PNG",
              bool(lay and lay.get("png") and os.path.exists(lay["png"])
                   and os.path.getsize(lay["png"]) > 10000),
              err or report["preview_layout"])

        shot, err = payload(cli.tool("fuxiang_preview_shot",
                                     {"view": "iso_ne", "fit": "content",
                                      "restore": True,
                                      "out": os.path.join(ROOT, "preview",
                                                          "shot_selfcheck.png")}))
        report["preview_shot"] = shot
        check("fuxiang_preview_shot 引擎渲染落盘 PNG",
              bool(shot and shot.get("ok") and shot.get("bytes", 0) > 10000),
              err or shot)
        check("fuxiang_preview_shot 后相机已还原",
              bool(shot and (shot.get("restore") or {}).get("verified")),
              (shot or {}).get("restore"))

        pst2, _e2 = payload(cli.tool("fuxiang_preview_state"))
        cam_after = (pst2 or {}).get("camera", {})
        report["camera_unchanged"] = (
            cam_before.get("position") == cam_after.get("position") and
            cam_before.get("forward") == cam_after.get("forward"))
        check("整套预览流程未改动编辑器相机", report["camera_unchanged"],
              {"before": cam_before.get("position"), "after": cam_after.get("position")})

        # ---- 全量扫描 ----
        if args.sweep:
            print("\n[全量扫描]")
            # 12a) 逐个 component_type 走接口
            all_types = sorted(int(t.get("component_type", t.get("type"))) for t in types)
            missing_types, t0 = [], time.time()
            for t in all_types:
                r, err = payload(cli.tool("fuxiang_find", {"component_type": t}))
                if not r or not r.get("matches"):
                    missing_types.append({"component_type": t, "err": err})
            report["type_sweep"] = {
                "requested": len(all_types), "missing": missing_types,
                "seconds": round(time.time() - t0, 1)}
            check("组件类型全量扫描：%d 种均可经接口定位" % len(all_types),
                  not missing_types, missing_types[:5])

            # 12b) 均匀抽样物件走接口（每页取 1 条，步进 step）
            n = args.sample_items
            total = (ver or {}).get("items_total", 3963)
            if n > 0:
                step = max(1, total // n)
                offsets = list(range(0, total, step))
                missing_items, t0 = [], time.time()
                for off in offsets:
                    r, err = payload(cli.tool("fuxiang_catalog_items",
                                              {"limit": 1, "offset": off}))
                    rows = (r or {}).get("items") or []
                    if not rows:
                        missing_items.append({"offset": off, "err": err or "空页"})
                        continue
                    iid = rows[0]["item_id"]
                    f, ferr = payload(cli.tool("fuxiang_find", {"item_id": iid}))
                    if not f or not f.get("matches"):
                        missing_items.append({"item_id": iid, "err": ferr})
                report["item_sweep"] = {
                    "requested": len(offsets), "step": step, "missing": missing_items,
                    "seconds": round(time.time() - t0, 1)}
                check("物件均匀抽样扫描：%d 项均可经接口定位" % len(offsets),
                      not missing_items, missing_items[:5])
    finally:
        cli.close()

    report["finished"] = time.time()
    report["ok"] = not report["failures"]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n%s  通过 %d / %d 项检查；报告: %s" % (
        "PASS" if report["ok"] else "FAIL",
        len(report["checks"]) - len(report["failures"]), len(report["checks"]), args.out))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
