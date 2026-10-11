"""Staged live MCP test; screenshots are captured separately from the game window.

place creates one temporary local item; remove cleans it up. The checkpoint allows
visual inspection between these steps. Never invokes save or publish.
"""
import json
from pathlib import Path
import subprocess
import sys
from live_scene_smoke import SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tests/visual_evidence/report.json'


def call(name, args=None):
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'visual-verify', 'version': '1'}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': name, 'arguments': args or {}}},
    ]
    result = subprocess.run([sys.executable, '-u', str(ROOT / 'mcp/server.py')],
                            input=''.join(json.dumps(r) + '\n' for r in requests),
                            capture_output=True, text=True, encoding='utf-8', timeout=75)
    if result.returncode:
        raise RuntimeError(result.stderr)
    reply = json.loads(result.stdout.splitlines()[-1])['result']
    text = reply['content'][0]['text']
    if reply.get('isError'):
        raise RuntimeError(text)
    return json.loads(text)


def main():
    action = sys.argv[1]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {}
    if action == 'place':
        if report.get('placed') and not report.get('removed'):
            raise RuntimeError('An earlier test item is still pending cleanup')
        report = {'before': call('fuxiang_eval', {'code': SNAPSHOT})['result']}
        REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['request'] = {'mode': 'local_preview', 'item_id': 202009, 'position': [620, 0, -60],
                             'rotation': [0, 0, 0], 'scale': 1}
        report['placed'] = call('fuxiang_place_item', report['request'])
        REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['readback'] = call('fuxiang_get_component', {'ugc_id': report['placed']['ugc_id']})
    elif action == 'inspect':
        report['readback'] = call('fuxiang_get_component', {'ugc_id': report['placed']['ugc_id']})
    elif action == 'remove':
        report['removed'] = call('fuxiang_remove_item', {'ugc_id': report['placed']['ugc_id']})
        REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['after'] = call('fuxiang_eval', {'code': SNAPSHOT})['result']
        report['original_components_unchanged'] = report['before'] == report['after']
    else:
        raise ValueError('expected place, inspect, or remove')
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
