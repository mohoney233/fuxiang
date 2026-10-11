#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regview — 把 registry/*.json 渲染成单文件可检索索引页（离线可用）。

用法:
  python cli/regview.py                # 读 registry/，写 registry/index.html
  python cli/regview.py <out.html>

数据全部来自 fuxiang_catalog 的导出，本脚本不接触游戏进程。
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(ROOT, "registry")

PLACE_LABEL = {"verified": "已实测", "unsupported": "不可摆放", "unknown": "未实测"}
GROUP_LABEL = {"scene": "场景物件", "global": "全局/功能组件",
               "ui": "UI 组件", "restrict": "限制区域", "unknown": "未知"}


def load(name):
    p = os.path.join(REG, name)
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


HTML = u"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>随笔浮想 · 组件与物体全量索引</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --line:#e3e6ea; --text:#1d2129;
  --muted:#86909c; --ok:#0f9d58; --bad:#d93025; --warn:#b26a00;
  --brand:#2b5cff; --chip:#eef1f6;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:20px 24px 14px;background:var(--panel);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
h1{margin:0 0 4px;font-size:19px;letter-spacing:.2px}
.sub{color:var(--muted);font-size:12.5px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.stat{background:var(--chip);border-radius:8px;padding:6px 11px;font-size:12.5px}
.stat b{font-size:15px;margin-right:4px;color:var(--brand)}
nav{display:flex;gap:6px;margin-top:14px;flex-wrap:wrap}
nav button{border:1px solid var(--line);background:var(--panel);color:var(--text);
  padding:6px 14px;border-radius:7px;cursor:pointer;font-size:13px}
nav button.on{background:var(--brand);border-color:var(--brand);color:#fff}
main{padding:16px 24px 60px}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
input[type=text],select{border:1px solid var(--line);background:var(--panel);
  border-radius:7px;padding:7px 11px;font-size:13px;color:var(--text);min-width:180px}
input[type=text]{min-width:280px}
.hint{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12.5px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);
  vertical-align:top;white-space:nowrap}
th{background:#f0f2f5;position:sticky;top:0;font-weight:600;z-index:5;cursor:pointer}
tbody tr:hover{background:#fafbfc}
td.wrap{white-space:normal;max-width:420px;color:#4e5969}
.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok);font-weight:600}
.bad{color:var(--bad);font-weight:600}
.warn{color:var(--warn);font-weight:600}
.chip{display:inline-block;background:var(--chip);border-radius:5px;
  padding:1px 6px;font-size:11.5px;color:#4e5969}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.pager{display:flex;gap:8px;align-items:center;margin-top:10px;color:var(--muted);font-size:12.5px}
.pager button{border:1px solid var(--line);background:var(--panel);border-radius:6px;
  padding:4px 11px;cursor:pointer;color:var(--text)}
footer{color:var(--muted);font-size:12px;padding:0 24px 30px}
</style>
</head>
<body>
<header>
  <h1>随笔浮想 · 组件与物体全量索引</h1>
  <div class="sub">数据源：游戏进程内 <span class="mono">_component_type2str</span> /
    <span class="mono">ugc_item_data</span> /
    <span class="mono">ugc_item_property</span> /
    <span class="mono">ugc_control_panel</span> /
    <span class="mono">ugc_item_tab</span> /
    <span class="mono">UGCEnums</span> ｜ 版本 __VERSION__</div>
  <div class="stats" id="stats"></div>
  <nav id="nav"></nav>
</header>
<main>
  <div class="tools">
    <input type="text" id="q" placeholder="搜索：编号 / 中文名 / 英文名 / 类名 / 模型路径">
    <select id="f1"></select>
    <select id="f2"></select>
    <span class="hint" id="cnt"></span>
  </div>
  <div id="view"></div>
  <div class="pager" id="pager"></div>
</main>
<footer>本页为离线静态快照，由 <span class="mono">cli/regview.py</span> 生成；
  现场数据请以 MCP 工具 <span class="mono">fuxiang_find</span> 为准。</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
var D = JSON.parse(document.getElementById('data').textContent);
var TABS = ['组件类型','物件目录','分类树','枚举','不可创建编号'];
var tab = 0, page = 0, SIZE = 60;
var PAGED = [0, 1];   // 行数大的页签做分页

function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

var STATS = [
  ['组件类型', D.counts.component_types_total],
  ['可创建', D.counts.component_types_creatable],
  ['不可创建', D.counts.component_types_without_class.length],
  ['物件', D.counts.items_total],
  ['隐藏记录', D.counts.items_hidden],
  ['分类', D.counts.categories_total],
  ['枚举', D.counts.enums_total]
];
document.getElementById('stats').innerHTML = STATS.map(function(s){
  return '<div class="stat"><b>'+s[1]+'</b>'+s[0]+'</div>';}).join('');

var nav = document.getElementById('nav');
TABS.forEach(function(t,i){
  var b = document.createElement('button');
  b.textContent = t; b.className = i===0?'on':'';
  b.onclick = function(){ tab=i; page=0; renderNav(); render(); };
  nav.appendChild(b);
});
function renderNav(){
  [].forEach.call(nav.children, function(b,i){ b.className = i===tab?'on':''; });
}

function compRows(){
  return Object.keys(D.components).map(function(k){ return D.components[k]; });
}
function itemRows(){
  return Object.keys(D.items).map(function(k){ return D.items[k]; });
}

var COLS = {
  0: ['编号','英文名','中文名','实现类','已登记','可创建','目录物件','场景中','摆放','属性表','不可创建原因'],
  1: ['item_id','中文名','分类','分组','部件类型','组件类型','占用','模型','列表可见'],
  2: ['分类ID','分组','分类名','物件数','显示模式'],
  3: ['枚举名','取值'],
  4: ['编号','中文名','类别','说明','目录物件']
};

var FILTERS = {
  0: [{k:'creatable', label:'可创建', opts:[['','全部'],['1','可创建'],['0','不可创建']]},
      {k:'place', label:'摆放', opts:[['','全部'],['verified','已实测'],['unsupported','不可摆放'],['unknown','未实测']]}],
  1: [{k:'group', label:'分组', opts:[['','全部分组'],['scene','场景物件'],['global','全局/功能组件'],['ui','UI组件'],['restrict','限制区域']]},
      {k:'vis', label:'可见性', opts:[['','全部'],['1','在对象列表'],['0','隐藏/被屏蔽']]}],
  2: [], 3: [],
  4: [{k:'kind', label:'类别', opts:[['','全部'],['mode_control_panel','按模式定义的操控面板'],['schema_without_implementation','有属性表无实现类'],['hidden_catalog_record','隐藏目录记录'],['no_implementation','无实现类']]}]
};

function renderTools(){
  var f1 = document.getElementById('f1'), f2 = document.getElementById('f2');
  var cfg = FILTERS[tab] || [];
  function fill(sel, c){
    if(!c){ sel.style.display='none'; return; }
    sel.style.display=''; sel.innerHTML = c.opts.map(function(o){
      return '<option value="'+o[0]+'" data-k="'+c.k+'">'+o[1]+'</option>';}).join('');
  }
  fill(f1, cfg[0]); fill(f2, cfg[1]);
}
function filterValues(){
  var out = {};
  [document.getElementById('f1'), document.getElementById('f2')].forEach(function(s){
    if(s.style.display==='none') return;
    var opt = s.options[s.selectedIndex];
    if(opt && opt.value) out[opt.getAttribute('data-k')] = opt.value;
  });
  return out;
}

function matches(row, q, f){
  if(q){
    var blob = JSON.stringify(row).toLowerCase();
    if(blob.indexOf(q) < 0) return false;
  }
  if(f.creatable !== undefined && String(row.creatable?1:0) !== f.creatable) return false;
  if(f.place && row.place_support !== f.place) return false;
  if(f.group && row.item_type_group !== f.group) return false;
  if(f.vis !== undefined){
    if(f.vis === '1' && !row.object_list_eligible) return false;
    if(f.vis === '0' && row.object_list_eligible) return false;
  }
  if(f.kind && row.not_creatable_kind !== f.kind) return false;
  return true;
}

function cellHtml(v, col){
  if(v === undefined || v === null || v === '') return '<span class="hint">—</span>';
  if(col === '可创建' || col === '已登记'){
    return v === true ? '<span class="ok">是</span>'
         : v === false ? '<span class="bad">否</span>' : '<span class="hint">?</span>';
  }
  if(col === '摆放'){
    var cls = v==='verified'?'ok':(v==='unsupported'?'bad':'warn');
    return '<span class="'+cls+'">'+(PLACE[v]||v)+'</span>';
  }
  if(col === '属性表') return v ? '<span class="ok">有</span>' : '<span class="hint">—</span>';
  if(col === '列表可见') return v ? '<span class="ok">是</span>' : '<span class="chip">否</span>';
  if(col === '取值'){
    return '<span class="mono">'+Object.keys(v).map(function(k){
      return k+'='+v[k];}).join('  ')+'</span>';
  }
  if(typeof v === 'object') return '<span class="mono">'+esc(JSON.stringify(v))+'</span>';
  return esc(v);
}

var PLACE = __PLACE__;
var KIND = __KIND__;

function rowsFor(){
  if(tab===0){
    return compRows().map(function(c){ return {
      '编号': c.component_type, '英文名': c.name, '中文名': c.name_cn,
      '实现类': c.class, '已登记': c.registered, '可创建': c.creatable,
      '目录物件': c.catalog_item_count, '场景中': c.live_count,
      '摆放': c.place_support, '属性表': c.property_schema,
      '不可创建原因': c.not_creatable_kind ? (KIND[c.not_creatable_kind]||c.not_creatable_kind) : null,
      creatable: c.creatable, place_support: c.place_support,
      not_creatable_kind: c.not_creatable_kind,
      not_creatable_note: c.not_creatable_note, component_type: c.component_type,
      catalog_item_ids: c.catalog_item_ids, class: c.class,
      _raw: c
    };});}
  if(tab===1){
    return itemRows().map(function(it){ return {
      'item_id': it.item_id, '中文名': it.name_cn,
      '分类': it.category_cn, '分组': it.category_group_cn,
      '部件类型': GROUP[it.item_type_group] || it.item_type_group,
      '组件类型': it.component_type, '占用': it.occupancy_value,
      '模型': it.model_path, '列表可见': it.object_list_eligible,
      item_type_group: it.item_type_group,
      object_list_eligible: it.object_list_eligible, _raw: it
    };});}
  if(tab===2){
    var cnt = {};
    itemRows().forEach(function(it){
      if(it.category_id!=null) cnt[it.category_id]=(cnt[it.category_id]||0)+1; });
    return Object.keys(D.categories).map(function(k){ var c = D.categories[k];
      return {'分类ID': c.category_id, '分组': c.group_cn, '分类名': c.name_cn,
              '物件数': cnt[c.category_id]||0, '显示模式': c.show_mode};});}
  if(tab===3){
    return Object.keys(D.enums).map(function(k){
      return {'枚举名': k, '取值': D.enums[k]};});}
  var out = [];
  compRows().forEach(function(c){
    if(c.creatable) return;
    out.push({'编号': c.component_type, '中文名': c.name_cn,
      '类别': KIND[c.not_creatable_kind]||c.not_creatable_kind,
      '说明': c.not_creatable_note, '目录物件': c.catalog_item_count,
      not_creatable_kind: c.not_creatable_kind});
  });
  return out;
}
var GROUP = __GROUP__;

function render(){
  var q = document.getElementById('q').value.trim().toLowerCase();
  var f = filterValues();
  var rows = rowsFor().filter(function(r){ return matches(r, q, f); });
  var total = rows.length;
  var maxPage = Math.max(0, Math.ceil(total/SIZE)-1);
  if(page > maxPage) page = maxPage;
  var cols = COLS[tab];
  var view = document.getElementById('view');
  if(!total){
    view.innerHTML = '<div class="hint">没有匹配的记录。</div>';
    document.getElementById('cnt').textContent = '0 条';
    document.getElementById('pager').innerHTML = '';
    return;
  }
  var slice = PAGED.indexOf(tab) >= 0 ? rows.slice(page*SIZE, page*SIZE+SIZE) : rows;
  var html = '<table><thead><tr>' + cols.map(function(c){
    return '<th>'+c+'</th>';}).join('') + '</tr></thead><tbody>';
  slice.forEach(function(r){
    html += '<tr>' + cols.map(function(c){
      return '<td>' + cellHtml(r[c], c) + '</td>';}).join('') + '</tr>';
  });
  html += '</tbody></table>';
  view.innerHTML = html;
  document.getElementById('cnt').textContent =
    '匹配 ' + total + ' 条 / 共 ' + rowsFor().length + ' 条';
  var pager = document.getElementById('pager');
  if(PAGED.indexOf(tab) >= 0 && total > SIZE){
    pager.innerHTML = '<button id="pv">上一页</button><span>第 '+(page+1)+' / '+(maxPage+1)+
      ' 页</span><button id="nx">下一页</button>';
    document.getElementById('pv').onclick = function(){ if(page>0){page--;render();} };
    document.getElementById('nx').onclick = function(){ if(page<maxPage){page++;render();} };
  } else { pager.innerHTML = ''; }
}

document.getElementById('q').oninput = function(){ page=0; render(); };
document.getElementById('f1').onchange = function(){ page=0; render(); };
document.getElementById('f2').onchange = function(){ page=0; render(); };
renderTools(); render();
</script>
</body>
</html>
"""


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REG, "index.html")
    comps = load("component_types.json")
    items = load("items.json")
    enums = load("enums.json")
    summary = load("summary.json")
    try:
        cats = load("categories.json")
    except Exception:
        # 旧导出没有分类表时按物件反推（会漏掉空分类）
        cats = {}
        for it in items.values():
            cid = it.get("category_id")
            if cid is None or str(cid) in cats:
                continue
            cats[str(cid)] = {"category_id": cid,
                              "group_cn": it.get("category_group_cn"),
                              "name_cn": it.get("category_cn")}

    payload = {
        "version": summary.get("version"),
        "counts": summary.get("counts"),
        "components": comps,
        "items": items,
        "enums": enums,
        "categories": cats,
    }
    payload["counts"]["categories_total"] = len(cats)

    doc = (HTML
           .replace("__VERSION__", esc(payload.get("version")))
           .replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                           separators=(",", ":")))
           .replace("__PLACE__", json.dumps(PLACE_LABEL, ensure_ascii=False))
           .replace("__GROUP__", json.dumps(GROUP_LABEL, ensure_ascii=False))
           .replace("__KIND__", json.dumps({
               "mode_control_panel": "按模式定义的操控面板",
               "schema_without_implementation": "有属性表无实现类",
               "hidden_catalog_record": "隐藏目录记录",
               "no_implementation": "无实现类"}, ensure_ascii=False)))
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print("written: %s (%d bytes)" % (out, os.path.getsize(out)))
    print("components=%d items=%d categories=%d enums=%d"
          % (len(comps), len(items), len(cats), len(enums)))


if __name__ == "__main__":
    main()
