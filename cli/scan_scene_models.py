#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_scene_models — 抓取当前场景内全部 world.model 的路径 / 位置 / 缩放。

用途：把游戏内已加载的现成场景（如「观赏图」、他人地图）转为**结构化数据**，
作为建筑结构参考。比把封面图变成向量更有用 —— 这里拿到的是每个构件的确切坐标。

特点：
  * **不依赖编辑器上下文**（走 `scene` 的 `world.get_all_model()`，不需要 `cm`），
    因此可以在「观赏」等非编辑模式下抓取；
  * 分批 eval，可断点续抓；
  * 只读，不创建/修改任何实体。

用法:
    python cli/scan_scene_models.py [输出文件名] [--batch 800]
"""
import json
import os
import sys
import time
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from fxedit import call_edit          # noqa: E402
from fxapi import find_layer as _find_layer  # noqa: E402  # 剥壳真源

CODE = '''import world
_L = list(world.get_all_model())
res = []
for m in _L[{START}:{END}]:
    r = {}
    try:
        r['f'] = str(getattr(m, 'filename', None))
    except Exception:
        pass
    try:
        p = m.position
        r['p'] = [round(p.x, 2), round(p.y, 2), round(p.z, 2)]
    except Exception:
        pass
    try:
        s = m.scale
        r['s'] = [round(s.x, 3), round(s.y, 3), round(s.z, 3)]
    except Exception:
        pass
    try:
        r['n'] = str(getattr(m, 'name', None))
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
    name = args[0] if args else 'scene_models'
    batch = 800
    if '--batch' in sys.argv:
        batch = int(sys.argv[sys.argv.index('--batch') + 1])
    out_path = os.path.join(ROOT, 'registry', name + '.json')

    items = []
    start = 0
    total = None
    while True:
        code = CODE.replace('{START}', str(start)).replace('{END}', str(start + batch))
        d = call_edit('eval', {'code': code}, timeout=180)
        got = unwrap(d, 'items')
        if not got:
            print('批 start=%d 无返回，中止' % start)
            break
        total = got['total']
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
        'source': 'world.get_all_model() 当前场景',
        'total': total,
        'captured': len(items),
        'fields': {'f': '模型文件路径', 'p': '世界位置 [x,y,z]', 's': '缩放 [x,y,z]',
                   'n': '对象名'},
        'items': items,
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print('已写入 %s（%d 条，%.1f KB）' % (out_path, len(items), os.path.getsize(out_path) / 1024.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
