"""Explicit live test: create one local object, verify it, always remove it.

Run only with --run-live. Never saves/publishes a map.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = '''
import hashlib, json
rows = sorted((str(k), id(v), str(getattr(v, '_owner_uid', None))) for k,v in cm._ugc_id2component.items())
types = sorted((str(k), sorted(str(x) for x in v)) for k,v in cm._component_type2ugc_ids.items() if v)
__result__ = {'count': len(rows), 'digest': hashlib.sha256(json.dumps([rows,types]).encode()).hexdigest()}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if not args.run_live:
        parser.error('requires --run-live; creates then removes a temporary client-local item')
    proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'mcp/server.py')],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8')
    report = {}
    seq = 0
    def rpc(method, params):
        nonlocal seq
        seq += 1
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': seq, 'method': method,
                                    'params': params}) + '\n')
        proc.stdin.flush()
        response = json.loads(proc.stdout.readline())
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response['result']
    def call(name, arguments=None):
        result = rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        content = result['content'][0]['text']
        if result.get('isError'):
            raise RuntimeError(content)
        return json.loads(content)
    created = None
    try:
        report['initialize'] = rpc('initialize', {'protocolVersion': '2024-11-05',
                                                  'capabilities': {}, 'clientInfo': {'name': 'scene-smoke', 'version': '1'}})
        report['tool_count'] = len(rpc('tools/list', {})['tools'])
        report['status'] = call('fuxiang_status')
        report['before'] = call('fuxiang_eval', {'code': SNAPSHOT})['result']
        requested = {'item_id': 202009, 'position': [100, 20, 100],
                     'rotation': [0, 30, 0], 'scale': [1.1, 1.2, 1.3]}
        report['plan'] = call('fuxiang_plan_item', requested)
        report['placed'] = call('fuxiang_place_item', dict(requested, mode='local_preview'))
        created = report['placed']['ugc_id']
        report['readback'] = call('fuxiang_get_component', {'ugc_id': created})
        report['transformed'] = call('fuxiang_set_transform', {
            'ugc_id': created, 'position': [-100, 25, 120],
            'rotation': [0, 60, 0], 'scale': [.8, .9, 1.2]})
        report['transform_readback'] = call('fuxiang_get_component', {'ugc_id': created})
        assert report['transform_readback']['position'] == [-100, 25, 120]
        report['during'] = call('fuxiang_eval', {'code': SNAPSHOT})['result']
        assert report['during']['count'] == report['before']['count'] + 1
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        try:
            if created is not None:
                report['removed'] = call('fuxiang_remove_item', {'ugc_id': created})
            report['after'] = call('fuxiang_eval', {'code': SNAPSHOT})['result']
            report['original_components_unchanged'] = report.get('before') == report['after']
        except Exception as exc:
            report['cleanup_error'] = str(exc)
        proc.stdin.close()
        proc.wait(timeout=5)
        report['server_stderr'] = proc.stderr.read()
        report['passed'] = (not report.get('error') and not report.get('cleanup_error') and
                            report.get('original_components_unchanged') is True)
        path = ROOT / 'tests/live_scene_result.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
