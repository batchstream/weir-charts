import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prom_evidence', ROOT / 'scripts/prometheus-evidence.py')
PROM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROM)


def fixture():
    entry = dict(pod='weir', uid='pod-uid', container='weir', container_id='containerd://abc',
                 image_id='image@sha256:abc', node='worker', pod_ip='10.0.0.1',
                 cadvisor_id='/podpod_uid/cri-abc', role='weir', instance='10.0.0.1:7449',
                 stores=['mongo', 'search'], cpu_limit=2, memory_limit_bytes=1000)
    series = []
    for name in PROM.RESOURCE:
        labels = dict(__name__=name, namespace='owned', pod='weir', container='weir', id=entry['cadvisor_id'],
                      name='abc', image=entry['image_id'], instance='worker', job='cadvisor')
        values = []
        for point in range(990, 1126, 15):
            value = point / 10 if name == 'container_cpu_usage_seconds_total' else 10
            if name == 'container_last_seen': value = point
            if name == 'container_start_time_seconds': value = 900
            if name == 'container_oom_events_total': value = 0
            values.append((point, value))
        series.append((labels, values))
    pod_info = dict(__name__='kube_pod_info', namespace='owned', pod='weir', uid='pod-uid', node='worker', pod_ip=entry['pod_ip'])
    container_info = dict(__name__='kube_pod_container_info', namespace='owned', pod='weir', uid='pod-uid', container='weir', container_id=entry['container_id'], image_id=entry['image_id'])
    series += [(pod_info, [(1000, 1)]), (container_info, [(1000, 1)])]
    for name in PROM.APPLICATION:
        labels = dict(__name__=name, pod='weir', instance=entry['instance'], job='pod')
        variants = [labels]
        if name.startswith('weir_store_'):
            variants = [dict(labels, store=store, kind='record') for store in ('mongo', 'search')]
        if name == 'weir_rpc_completions_total':
            variants = [dict(labels, method=method, status='ok') for method in ('Read', 'Mutate', 'Bulk')]
        for variant in variants:
            values = [(point, point if name.endswith(('_total', '_sum', '_count')) else 1) for point in range(990, 1126, 15)]
            series.append((variant, values))
    options = SimpleNamespace(start=1000, end=1120, phase='postrun')
    return entry, series, options


class PrometheusTests(unittest.TestCase):
    def test_raw_identity_resources_and_business(self):
        entry, series, options = fixture()
        resources = PROM.audit_container(entry, series, options)
        self.assertAlmostEqual(resources['cpu_cores']['peak'], .1)
        self.assertEqual(resources['memory_headroom_bytes'], 990)
        application = PROM.audit_application(entry, series, options)
        self.assertTrue(application)
        self.assertTrue(all(s['max_gap_seconds'] <= 90 for s in application))

    def test_raw_gaps_boundaries_resets_and_identity_fail(self):
        for mode in ('gap', 'start', 'end', 'uid', 'container', 'duplicate', 'missing', 'reset', 'oom', 'future', 'stale', 'up', 'app-missing', 'no-increment', 'backend-family', 'rpc-error', 'queue-overflow', 'target-missing'):
            with self.subTest(mode=mode):
                entry, series, options = fixture()
                if mode == 'gap': series[0][1][:] = [(990, 1), (1100, 2)]
                if mode == 'start': series[0][1][:] = [(1100, 1), (1115, 2)]
                if mode == 'end': series[0][1][:] = [(990, 1), (1005, 2)]
                if mode == 'uid': series[0][0]['id'] = '/other-uid/cri-abc'
                if mode == 'container': series[0][0]['name'] = 'other'
                if mode == 'duplicate': series.append((dict(series[0][0], job='duplicate'), series[0][1]))
                if mode == 'missing': del series[0]
                if mode == 'reset': series[0][1][-2] = (1110, 0)
                if mode == 'oom': series[4][1][4] = (1050, 1)
                if mode == 'future': series[6][1][4] = (1050, 1200)
                if mode == 'stale':
                    # Genuine exporter observation values expose source age; no query evaluation timestamps.
                    options.end = 1240
                    series[6][1][:] = [(t, t) for t in range(990, 1246, 15)]
                if mode == 'up':
                    next(v for l, v in series if l['__name__'] == 'up')[4] = (1050, 0)
                if mode == 'app-missing': series[:] = [(l, v) for l, v in series if l['__name__'] != 'process_resident_memory_bytes']
                if mode == 'no-increment':
                    for labels, values in series:
                        if labels['__name__'] == 'weir_store_executions_total': values[:] = [(t, 1) for t, _ in values]
                if mode == 'backend-family': series[:] = [(l, v) for l, v in series if not (l['__name__'] == 'weir_store_pending_entries' and l.get('store') == 'search')]
                if mode == 'rpc-error':
                    labels = dict(__name__='weir_rpc_completions_total', pod='weir', instance=entry['instance'], job='pod', status='non_ok', method='Read')
                    series.append((labels, [(t, t) for t in range(990, 1126, 15)]))
                if mode == 'queue-overflow':
                    next(v for l, v in series if l['__name__'] == 'weir_store_pending_entries')[4] = (1050, 2)
                if mode == 'target-missing': series[:] = [(l, v) for l, v in series if l['__name__'] not in PROM.APPLICATION]
                with self.assertRaises(ValueError):
                    PROM.audit_container(entry, series, options)
                    PROM.audit_application(entry, series, options)

    def test_preflight_requires_identity_and_does_not_require_new_traffic(self):
        entry, series, options = fixture()
        options.phase = 'preflight'
        for labels, values in series:
            if labels['__name__'].endswith(('_total', '_sum', '_count')): values[:] = [(t, 0) for t, _ in values]
        PROM.audit_application(entry, series, options)
        without_ksm = [(l, v) for l, v in series if l['__name__'] not in PROM.IDENTITY]
        with self.assertRaises(ValueError): PROM.audit_container(entry, without_ksm, options)
        options.phase = 'postrun'
        self.assertIn('identity_note', PROM.audit_container(entry, without_ksm, options))

    def test_overlap_exact_dedup_conflicts_and_non_matrix_rejected(self):
        entry, series, _ = fixture()
        labels, values = series[0]
        matrix = dict(status='success', data=dict(resultType='matrix', result=[dict(metric=labels, values=values)]))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            receipt = dict(requests=[])
            for index in range(2):
                body = json.dumps(matrix).encode()
                path = directory / str(index)
                path.write_bytes(body)
                receipt['requests'].append(dict(file=path.name, sha256=PROM.sha(body), bytes=len(body)))
            self.assertEqual(len(PROM.merge_raw(directory, receipt)[0][1]), len(values))
            matrix['data']['result'][0]['values'] = [[values[0][0], 0]]
            body = json.dumps(matrix).encode()
            (directory / '1').write_bytes(body)
            receipt['requests'][1].update(sha256=PROM.sha(body), bytes=len(body))
            with self.assertRaises(ValueError): PROM.merge_raw(directory, receipt)
            matrix['data']['resultType'] = 'vector'
            body = json.dumps(matrix).encode()
            (directory / '1').write_bytes(body)
            receipt['requests'][1].update(sha256=PROM.sha(body), bytes=len(body))
            with self.assertRaises(ValueError): PROM.merge_raw(directory, receipt)

    def test_http_raw_collection_and_unavailable_is_pending(self):
        requests = []
        response_status = [200]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(response_status[0]); self.end_headers()
                self.wfile.write(b'{"status":"success","data":{"resultType":"matrix","result":[]}}')
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            entry, _, _ = fixture()
            source = dict(url='http://127.0.0.1:' + str(server.server_port), uid='prom-uid', namespace='monitoring', pod='prometheus-0')
            cfg = dict(namespace='owned', prometheus=source, containers=[entry])
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); config = root / 'freeze.json'; config.write_text(json.dumps(cfg))
                options = SimpleNamespace(output=root/'ok', config=config, phase='preflight', start=1000, end=1120)
                self.assertTrue(PROM.collect(cfg, options))
                self.assertEqual(len(requests), 4)
                self.assertTrue(all('/api/v1/query?' in r and '/query_range' not in r for r in requests))
                self.assertTrue(all(parse_qs(urlsplit(r).query)['query'][0].endswith('[241s]') for r in requests))
                response_status[0] = 503
                options.output = root/'pending'
                self.assertFalse(PROM.collect(cfg, options))
                receipt = json.loads((options.output/'receipt.json').read_text())
                self.assertEqual(receipt['state'], 'pending')
                self.assertEqual(receipt['requests'][-1]['http_status'], 503)
                self.assertFalse((options.output/'ready').exists())
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_complete_offline_audit_binds_runner_observer_and_freeze(self):
        original, _, base_options = fixture()
        entries, all_series = [], []
        for index in range(6):
            entry, series, _ = fixture()
            entry.update(pod='pod-'+str(index), uid='uid-'+str(index), role='weir' if index < 3 else ('load' if index == 5 else 'backend'))
            entries.append(entry)
            for labels, values in series:
                if entry['role'] != 'weir' and labels['__name__'] in PROM.APPLICATION: continue
                labels['pod'] = entry['pod']
                if 'uid' in labels: labels['uid'] = entry['uid']
                all_series.append(dict(metric=labels, values=values))
        source = dict(url='http://unused', namespace='monitoring', pod='prom', uid='source-uid')
        labels = dict(__name__='kube_pod_info', namespace='monitoring', pod='prom', uid='source-uid')
        all_series.append(dict(metric=labels, values=[[1000, '1']]))
        cfg = dict(run='weir-soak-test', namespace='owned', prometheus=source, containers=entries)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory/'freeze'; config.write_text(json.dumps(cfg))
            raw = json.dumps(dict(status='success', data=dict(resultType='matrix', result=all_series))).encode()
            (directory/'raw.json').write_bytes(raw)
            request = dict(file='raw.json', sha256=PROM.sha(raw), bytes=len(raw))
            receipt = dict(state='collected', freeze_sha256=PROM.sha(config.read_bytes()), phase='postrun', start=1000, end=1120, requests=[request])
            (directory/'receipt.json').write_text(json.dumps(receipt))
            records = []
            for point in (990, 1050, 1125):
                pods = []
                for entry in entries:
                    status = dict(name=entry['container'], containerID=entry['container_id'], imageID=entry['image_id'], restartCount=0, ready=True)
                    pods.append(dict(metadata=dict(name=entry['pod'], uid=entry['uid']), spec=dict(nodeName=entry['node']), status=dict(phase='Running', podIP=entry['pod_ip'], containerStatuses=[status])))
                records.append(dict(utc=PROM.datetime.fromtimestamp(point, PROM.timezone.utc).isoformat(), pods=pods[:5], loadPods=pods[5:], job=dict(metadata=dict(name=cfg['run'], uid='job-id'), status=dict(active=1))))
            observations = directory/'observations'; observations.write_text('\n'.join(json.dumps(r) for r in records))
            status_file = directory/'status'; status_file.write_text('{"passed":true}')
            start = dict(kind='start', run_id=cfg['run'], started_utc=PROM.datetime.fromtimestamp(1000, PROM.timezone.utc).isoformat(), duration_ns=120000000000, workers=6, cycles_per_second=5, max_p99_ns=500000000)
            last = dict(kind='passed', run_id=cfg['run'], utc=PROM.datetime.fromtimestamp(1120, PROM.timezone.utc).isoformat(), elapsed_ns=120000000000, cycles=3600, failures=0, unknown=0, cycle_p99_upper_ns=20000000, interval_p99_upper_ns=20000000)
            load = directory/'load'; load.write_text(json.dumps(start)+'\n'+json.dumps(last))
            options = SimpleNamespace(start=1000, end=1120, phase='postrun', config=config, output=directory, observations=observations, observer_status=status_file, load_report=load)
            self.assertEqual(PROM.audit(cfg, options)['state'], 'passed')
            records[1]['pods'][0]['metadata']['uid'] = 'replacement'
            observations.write_text('\n'.join(json.dumps(r) for r in records))
            with self.assertRaisesRegex(ValueError, 'identity differs'): PROM.audit(cfg, options)
            receipt['freeze_sha256'] = 'wrong'
            (directory/'receipt.json').write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, 'frozen audit window'): PROM.audit(cfg, options)
