#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_editor_map — 在**编辑器上下文**中抓取地图全部组件的完整数据。

与 `scan_scene_models.py` 的区别：
  * 走 `cm._ugc_id2component`（编辑器），因此**能直接拿到 item_id**，
    无需从模型路径反推；还可读取旋转、缩放、以及组件事件（材质/颜色）；
  * 仅当编辑器已打开某地图时可用（`fuxiang_status` 的 `component_manager` 为 true）。

用法:
    python cli/scan_editor_map.py <输出名> [--batch 200] [--with-events]
"""
import json
import os
import sys
import time
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(ROOT, "bridge").replace("\\", "/")
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit          # noqa: E402
from fxapi import find_layer as _find_layer  # noqa: E402  # 剥壳真源

CODE = '''import sys, json
sys.path.insert(0, {BRIDGE})
import fuxiang_edit as _fe
_L = list(cm._ugc_id2component.keys())
res = []
for gid in _L[{START}:{END}]:
    comp = cm._ugc_id2component.get(gid)
    if comp is None:
        continue
    u = getattr(comp, '_owner', None)
    if u is None:
        continue
    r = {'g': gid}
    try:
        r['i'] = getattr(u, 'ugc_type', None)
    except Exception:
        pass
    try:
        p = u.get_position3()
        r['p'] = [round(p.x, 2), round(p.y, 2), round(p.z, 2)]
    except Exception:
        pass
    try:
        d = u.ugc_dir
        r['r'] = [round(d.x, 2), round(d.y, 2), round(d.z, 2)]
    except Exception:
        pass
    try:
        s = u.ugc_size
        r['s'] = [round(s.x, 4), round(s.y, 4), round(s.z, 4)]
    except Exception:
        pass
    EVENTS = {EVENTS}
    if EVENTS:
        try:
            ci = getattr(comp, 'component_info', None) or {}
            # ⚠️ 2026-09-23 修复：此前直接把 events 嵌在第 5 层回传，撞上桥接层
            # `_safe()` 深度 >4 的截断，属性全变成 '<deep>'（材质/颜色读不到）。
            # 正确做法：在游戏侧先压平成 JSON 字符串再回传，CLI 侧 loads。
            # 优先严格 dumps（完整保真，含颜色这类双层值）；撞上 taggeddict
            # 不可序列化时回退 _jdump。与 cli/snapshot.py 同一手法。
            try:
                _s = json.dumps(ci, ensure_ascii=False)
            except Exception:
                _s = _fe._jdump(ci)
            # ⛔ 必须把整个 events 作为**字符串**挂在 item 上：item 本身已经在
            # 回传结构的第 2 层，再嵌 dict/list 就到第 5~6 层，照样被截成 '<deep>'。
            r['ej'] = _s
        except Exception:
            pass
    res.append(r)
__result__ = {'total': len(_L), 'start': {START}, 'items': res}
'''


def unwrap(node, key):
    """找含 key 的那一层并返回该层；剥壳规则见 cli/fxapi.py::find_layer（唯一真源）。"""
    return _find_layer(node, key)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    name = args[0] if args else 'editor_map'
    batch = 200
    if '--batch' in sys.argv:
        batch = int(sys.argv[sys.argv.index('--batch') + 1])
    with_events = '--with-events' in sys.argv
    out_path = os.path.join(ROOT, 'registry', name + '.json')

    items = []
    start = 0
    total = None
    while True:
        code = (CODE.replace('{BRIDGE}', repr(BRIDGE_DIR))
                    .replace('{START}', str(start))
                    .replace('{END}', str(start + batch))
                    .replace('{EVENTS}', 'True' if with_events else 'False'))
        d = call_edit('eval', {'code': code}, timeout=180)
        got = unwrap(d, 'items')
        if not got:
            print('批 start=%d 无返回，中止' % start)
            break
        total = got['total']
        for it in got['items']:
            # 游戏侧压平成字符串回传的 events，在这里还原成原来的 'e' 结构
            raw = it.pop('ej', None)
            if raw:
                try:
                    ev = json.loads(raw).get('events')
                    if ev:
                        it['e'] = [{'t': x.get('type'), 'v': x.get('info')}
                                   for x in ev]
                except Exception:
                    pass
        items.extend(got['items'])
        print('  已抓 %d / %d' % (len(items), total))
        start += batch
        if start >= total:
            break
        time.sleep(0.2)

    if total is None:
        print('抓取失败')
        return 1
    doc = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'source': 'cm._ugc_id2component（编辑器上下文）',
        'total': total,
        'captured': len(items),
        'fields': {'g': 'ugc_id', 'i': 'item_id', 'p': '位置', 'r': '旋转(度)',
                   's': 'ugc_size 缩放', 'e': '组件事件(材质/颜色)'},
        'items': items,
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print('已写入 %s（%d 条，%.1f KB）'
          % (out_path, len(items), os.path.getsize(out_path) / 1024.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
