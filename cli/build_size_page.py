#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_size_page — 把 registry/model_sizes.json 渲染成自包含的查阅页。

产物: registry/model_sizes.html（数据内联，可离线打开）
用法: python cli/build_size_page.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "registry", "model_sizes.json")
OUT = os.path.join(ROOT, "registry", "model_sizes.html")

ROW = """{"id":%(id)s,"n":%(n)s,"c":%(c)s,"s":%(s)s,"h":%(h)s,"ct":%(ct)s,
"occ":%(occ)s,"zl":%(zl)s,"ctp":%(ctp)s,"sc":%(sc)s,"mp":%(mp)s}"""


def build():
    with open(SRC, "r", encoding="utf-8") as f:
        d = json.load(f)
    rows = []
    for r in d["items"]:
        if "size" not in r:
            continue
        rows.append(ROW % {
            "id": r["item_id"],
            "n": json.dumps(r.get("name_cn"), ensure_ascii=False),
            "c": json.dumps(r.get("category_cn"), ensure_ascii=False),
            "s": json.dumps(r["size"]),
            "h": json.dumps(r["half"]),
            "ct": json.dumps(r.get("center")),
            "occ": json.dumps(r.get("occupancy_value")),
            "zl": json.dumps(r.get("zoom_limit")),
            "ctp": json.dumps(r.get("component_type")),
            "sc": json.dumps(r.get("scope")),
            "mp": json.dumps(r.get("model_path"), ensure_ascii=False),
        })
    data_js = "[\n" + ",\n".join(rows) + "\n]"

    html = TEMPLATE.replace("/*__DATA__*/", data_js)
    html = html.replace("__COUNT__", str(len(rows)))
    html = html.replace("__GEN__", d.get("generated_at", ""))
    sc = d.get("scope_counts") or {}
    html = html.replace("__SCOPE__", "建筑 %d · 陈设 %d" % (sc.get("building", 0), sc.get("furnishing", 0)))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已写入 %s  (%d 条, %.1f KB)" % (OUT, len(rows), len(html) / 1024.0))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>随笔浮想 · 建筑构件尺寸库</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --line:#e3e6ea; --text:#1d2129; --muted:#86909c;
  --brand:#2b5cff; --chip:#eef1f6; --ok:#0f9d58; --warn:#b26a00;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:18px 22px 12px;background:var(--panel);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
h1{margin:0 0 3px;font-size:18px;font-weight:600}
.sub{color:var(--muted);font-size:12px}
.tools{display:flex;gap:10px;flex-wrap:wrap;margin-top:11px;align-items:center}
input,select{padding:6px 9px;border:1px solid var(--line);border-radius:7px;
  background:#fff;color:var(--text);font-size:13px;font-family:inherit}
input#q{width:230px}
.tag{background:var(--chip);color:var(--muted);border-radius:6px;
  padding:3px 8px;font-size:12px}
main{padding:14px 22px 96px}
table{width:100%;border-collapse:collapse;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);
  font-size:13px;white-space:nowrap}
th{background:#fafbfc;color:var(--muted);font-weight:500;cursor:pointer;
  user-select:none;position:sticky;top:0}
th:hover{color:var(--brand)}
tbody tr{cursor:pointer}
tbody tr:hover{background:#f3f6ff}
tbody tr.sel{background:#e8efff}
td.num{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.dim{color:var(--brand);font-weight:500}
.flat{color:var(--warn)}
.wrap{max-height:none}
#calc{position:fixed;left:0;right:0;bottom:0;background:var(--panel);
  border-top:1px solid var(--line);padding:11px 22px;display:none;
  box-shadow:0 -2px 12px rgba(0,0,0,.06)}
#calc.on{display:block}
#calc .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
#calc b{font-size:14px}
#calc .out{font-family:ui-monospace,Consolas,monospace;font-size:13px;
  background:var(--chip);padding:5px 10px;border-radius:7px}
#calc input{width:74px}
.hint{color:var(--muted);font-size:12px}
button{padding:6px 12px;border:1px solid var(--line);border-radius:7px;
  background:#fff;cursor:pointer;font-size:13px;font-family:inherit}
button:hover{border-color:var(--brand);color:var(--brand)}
</style>
</head>
<body>
<header>
  <h1>随笔浮想 · 建筑构件尺寸库</h1>
  <div class="sub">__COUNT__ 个构件（__SCOPE__） · 取自游戏内模型原生包围盒 · 生成于 __GEN__</div>
  <div class="tools">
    <input id="q" placeholder="搜索名称或编号…">
    <select id="scope">
      <option value="">全部范围</option>
      <option value="building">建筑构件</option>
      <option value="furnishing">家具·装饰·植物</option>
    </select>
    <select id="cat"><option value="">全部分类</option></select>
    <select id="sort">
      <option value="id">按编号</option>
      <option value="sizeY">按高度</option>
      <option value="sizeMax">按最大边</option>
      <option value="sizeX">按 X</option>
      <option value="sizeZ">按 Z</option>
      <option value="occ">按占用值</option>
    </select>
    <label class="tag"><input type="checkbox" id="flatOnly"> 只看平面构件</label>
    <span class="tag" id="cnt"></span>
  </div>
</header>
<main>
  <table>
    <thead><tr>
      <th data-k="id">编号</th><th data-k="n">名称</th><th data-k="c">分类</th>
      <th data-k="sizeX">实际尺寸 X×Y×Z</th><th>半尺寸</th>
      <th>中心偏移</th><th data-k="occ">占用值</th><th>缩放上限</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
</main>
<div id="calc">
  <div class="row">
    <b id="cname">—</b>
    <span class="hint">目标尺寸</span>
    <input id="tX" placeholder="X"> <input id="tY" placeholder="Y"> <input id="tZ" placeholder="Z">
    <span class="hint">→ 摆放时传 ugc_size</span>
    <span class="out" id="cout">—</span>
    <button id="cfill">用实际尺寸填入</button>
    <button id="cclose">关闭</button>
  </div>
</div>
<script>
const DATA = /*__DATA__*/;
const tb = document.getElementById('tb'), q = document.getElementById('q');
const cat = document.getElementById('cat'), sort = document.getElementById('sort');
const scope = document.getElementById('scope');
const cnt = document.getElementById('cnt'), flatOnly = document.getElementById('flatOnly');
const calc = document.getElementById('calc'), cname = document.getElementById('cname');
const cout = document.getElementById('cout');
let cur = null, sortKey = 'id', sortDir = 1;

[...new Set(DATA.map(d => d.c).filter(Boolean))].sort().forEach(c => {
  const o = document.createElement('option'); o.value = c; o.textContent = c; cat.appendChild(o);
});
const f = n => (Math.round(n * 100) / 100).toString();
const isFlat = d => Math.abs(d.s[1]) < 1e-6;

function view() {
  const kw = q.value.trim().toLowerCase(), c = cat.value, sc = scope.value;
  let r = DATA.filter(d => (!c || d.c === c) && (!sc || d.sc === sc)
    && (!kw || String(d.id).includes(kw) || (d.n || '').toLowerCase().includes(kw)));
  if (flatOnly.checked) r = r.filter(isFlat);
  const key = d => {
    if (sortKey === 'id') return d.id;
    if (sortKey === 'n') return d.n || '';
    if (sortKey === 'c') return d.c || '';
    if (sortKey === 'occ') return parseFloat(d.occ) || 0;
    if (sortKey === 'sizeX') return d.s[0];
    if (sortKey === 'sizeY') return d.s[1];
    if (sortKey === 'sizeZ') return d.s[2];
    return Math.max(...d.s.map(Math.abs));
  };
  r.sort((a, b) => {
    const x = key(a), y = key(b);
    if (typeof x === 'string') return x.localeCompare(y) * sortDir;
    return (x - y) * sortDir;
  });
  return r;
}

function render() {
  const r = view();
  cnt.textContent = r.length + ' / ' + DATA.length;
  const h = [];
  for (const d of r) {
    const flat = isFlat(d) ? ' flat' : '';
    h.push('<tr data-id="' + d.id + '" class="' + (cur && cur.id === d.id ? 'sel' : '') + '">'
      + '<td class="num">' + d.id + '</td><td>' + (d.n || '—') + '</td>'
      + '<td>' + (d.c || '—') + '</td>'
      + '<td class="num dim' + flat + '">' + f(d.s[0]) + ' × ' + f(d.s[1]) + ' × ' + f(d.s[2]) + '</td>'
      + '<td class="num">' + f(d.h[0]) + ' × ' + f(d.h[1]) + ' × ' + f(d.h[2]) + '</td>'
      + '<td class="num">' + d.ct.map(f).join(', ') + '</td>'
      + '<td class="num">' + (d.occ || '—') + '</td>'
      + '<td class="num">' + (d.zl ? d.zl[0] + '~' + d.zl[1] : '—') + '</td></tr>');
  }
  tb.innerHTML = h.join('');
}

function recalc() {
  if (!cur) return;
  const t = ['tX', 'tY', 'tZ'].map(i => parseFloat(document.getElementById(i).value));
  if (t.every(v => isNaN(v))) { cout.textContent = '—'; return; }
  const out = [0, 1, 2].map(i => isNaN(t[i]) || !cur.s[i] ? null : t[i] / cur.s[i]);
  cout.textContent = out.map(v => v === null ? '—' : f(v)).join(' , ');
}

tb.addEventListener('click', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  const d = DATA.find(x => x.id == tr.dataset.id);
  cur = d;
  cname.textContent = d.n + ' (' + d.id + ')  实际尺寸 ' + d.s.map(f).join(' × ');
  ['tX', 'tY', 'tZ'].forEach(i => document.getElementById(i).value = '');
  cout.textContent = '—';
  calc.classList.add('on');
  render();
});
document.getElementById('cfill').onclick = () => {
  if (!cur) return;
  ['tX', 'tY', 'tZ'].forEach((i, k) => document.getElementById(i).value = f(cur.s[k]));
  recalc();
};
document.getElementById('cclose').onclick = () => { calc.classList.remove('on'); cur = null; render(); };
['tX', 'tY', 'tZ'].forEach(i => document.getElementById(i).addEventListener('input', recalc));
q.addEventListener('input', render);
cat.addEventListener('change', render);
scope.addEventListener('change', render);
flatOnly.addEventListener('change', render);
sort.addEventListener('change', () => { sortKey = sort.value; render(); });
document.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = 1; }
  render();
});
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
