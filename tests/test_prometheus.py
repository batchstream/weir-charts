import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
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


def bundle(directory, seconds=120):
    start, end = 1000, 1000 + seconds
    entries, all_series = [], []
    for index in range(6):
        entry, series, _ = fixture()
        uid, cid, ip = 'uid-' + str(index), 'container' + str(index), '10.0.0.' + str(index + 1)
        entry.update(pod='pod-' + str(index), uid=uid, role='weir' if index < 3 else ('load' if index == 5 else 'backend'),
                     container_id='containerd://' + cid, cadvisor_id='/pod' + uid.replace('-', '_') + '/cri-' + cid,
                     pod_ip=ip, instance=ip + ':7449')
        entries.append(entry)
        for labels, _ in series:
            name = labels['__name__']
            if entry['role'] != 'weir' and name in PROM.APPLICATION:
                continue
            labels['pod'] = entry['pod']
            if 'uid' in labels: labels['uid'] = uid
            if name in PROM.RESOURCE: labels.update(id=entry['cadvisor_id'], name=cid)
            if name in PROM.APPLICATION: labels['instance'] = entry['instance']
            if name == 'kube_pod_container_info': labels['container_id'] = entry['container_id']
            if name == 'kube_pod_info': labels['pod_ip'] = ip
            values = []
            for point in range(990, end + 1, 60):
                value = 1
                if name.endswith(('_total', '_count', '_sum')): value = point
                if name == 'container_cpu_usage_seconds_total': value = point / 10
                if name == 'container_last_seen': value = point
                if name == 'container_start_time_seconds': value = 900
                if name == 'container_oom_events_total': value = 0
                values.append([point, value])
            all_series.append(dict(metric=labels, values=values))
    source = dict(url='http://unused', namespace='monitoring', pod='prom', uid='source-uid')
    labels = dict(__name__='kube_pod_info', namespace='monitoring', pod='prom', uid='source-uid')
    all_series.append(dict(metric=labels, values=[[1000, '1']]))
    observer = dict(job_uid='observer-job-id', pod='observer-pod', uid='observer-pod-id', node='worker', pod_ip='10.0.0.9', container='observer', container_id='containerd://observer', image_id='image@sha256:observer')
    tool_sha = PROM.sha((ROOT/'scripts/prometheus-evidence.py').read_bytes())
    cfg = dict(run='weir-soak-test', namespace='owned', namespace_uid='namespace-id', owner='task-owner', prometheus=source, containers=entries,
               observer=observer, load_job_uid='load-job-id', tool_sha256=tool_sha)
    run = dict(run=cfg['run'], namespace=cfg['namespace'], owner=cfg['owner'], duration=str(seconds)+'s', observerDuration=str(seconds+600)+'s', interval='1m',
               resourceEvidenceSource='prometheus', evidenceToolSHA256=tool_sha, prometheusSourceUID=source['uid'], serverRevision='a'*40, imageDigest='sha256:'+'b'*64,
               sdkRevision='c'*40, chartVersion='0.1.0', targets=[e['pod_ip']+':7447' for e in entries[:3]])
    canonical = json.dumps(run, sort_keys=True, separators=(',', ':')).encode()
    cfg['run_config_sha256'] = PROM.sha(canonical)
    run_config = directory/'run.json'; run_config.write_bytes(canonical)
    config = directory/'freeze.json'; config.write_text(json.dumps(cfg))
    raw = json.dumps(dict(status='success', data=dict(resultType='matrix', result=all_series))).encode()
    (directory/'raw.json').write_bytes(raw)
    request = dict(file='raw.json', sha256=PROM.sha(raw), bytes=len(raw))
    receipt = dict(state='collected', freeze_sha256=PROM.sha(config.read_bytes()), phase='postrun', start=start, end=end, requests=[request])
    (directory/'receipt.json').write_text(json.dumps(receipt))
    settings = dict(namespace=cfg['namespace'], job=cfg['run'], runID=cfg['run'], runConfigSHA256=cfg['run_config_sha256'], interval=60000000000,
                    loadDuration=seconds*10**9, duration=(seconds+600)*10**9, replicas=3, release='weir')
    records = [settings]
    iso = PROM.datetime.fromtimestamp
    for point in list(range(990, end, 60)) + [end+5]:
        pods = []
        for entry in entries:
            state = dict(running=dict(startedAt=iso(900, PROM.timezone.utc).isoformat()))
            phase = 'Running'
            if entry['role'] == 'load' and point > end:
                phase = 'Succeeded'
                state = dict(terminated=dict(exitCode=0, reason='Completed', startedAt=iso(900, PROM.timezone.utc).isoformat(), finishedAt=iso(end+1, PROM.timezone.utc).isoformat()))
            status = dict(name=entry['container'], containerID=entry['container_id'], imageID=entry['image_id'], restartCount=0, ready=phase=='Running', state=state)
            pods.append(dict(metadata=dict(name=entry['pod'], uid=entry['uid']), spec=dict(nodeName=entry['node']), status=dict(phase=phase, podIP=entry['pod_ip'], containerStatuses=[status])))
        job_status = dict(active=1) if point <= end else dict(succeeded=1)
        records.append(dict(utc=iso(point, PROM.timezone.utc).isoformat(), pods=pods[:5], loadPods=pods[5:], job=dict(metadata=dict(name=cfg['run'], uid=cfg['load_job_uid']), status=job_status)))
    observations = directory/'observations.jsonl'; observations.write_text(''.join(json.dumps(r)+'\n' for r in records))
    status = dict(passed=True, completed_at=iso(end+6, PROM.timezone.utc).isoformat(), runID=cfg['run'], namespace=cfg['namespace'], job=cfg['run'], runConfigSHA256=cfg['run_config_sha256'])
    status_file = directory/'status.json'; status_file.write_text(json.dumps(status))
    first = dict(kind='start', run_id=cfg['run'], started_utc=iso(start, PROM.timezone.utc).isoformat(), duration_ns=seconds*10**9, workers=6, cycles_per_second=5, max_p99_ns=500000000,
                 server_revision=run['serverRevision'], image_digest=run['imageDigest'], sdk_revision=run['sdkRevision'], chart_version=run['chartVersion'], targets=run['targets'])
    load_records = [first]
    for kind in ('owned', 'cleaned'):
        if kind == 'cleaned':
            for point in range(start+60, end, 60):
                cycles=(point-start)*30
                load_records.append(dict(kind='progress', run_id=cfg['run'], utc=iso(point, PROM.timezone.utc).isoformat(), elapsed_ns=(point-start)*10**9, cycles=cycles,
                                         verified_reads=cycles*2, verified_mutations=cycles*2, stream_checks=2, cycle_histogram=[cycles]+[0]*11, cycle_overflow=0,
                                         failures=0, unknown=0, cycle_p99_upper_ns=20000000, interval_p99_upper_ns=20000000))
        for worker in range(6):
            # SDK v0.1.1 owned/cleaned events omit their zero duration via omitempty.
            load_records.append(dict(kind=kind, worker=worker, target=run['targets'][worker%3], backend='mongo' if worker%2==0 else 'search', resource='weir://owned/records/worker-'+str(worker), sequence=0))
    last = dict(load_records[7])
    last.update(kind='passed', run_id=cfg['run'], utc=iso(end, PROM.timezone.utc).isoformat(), elapsed_ns=seconds*10**9, cycles=seconds*30,
                verified_reads=seconds*60, verified_mutations=seconds*60, cycle_histogram=[seconds*30]+[0]*11)
    load_records.append(last)
    load = directory/'load.jsonl'; load.write_text(''.join(json.dumps(r)+'\n' for r in load_records))
    load_exit = directory/'exit.json'; load_exit.write_text('{"exit_code":0}')
    items=[]
    for job_name, uid, entry, finished in ((cfg['run'], cfg['load_job_uid'], entries[-1], end+1), (cfg['run']+'-observer', observer['job_uid'], observer, end+7)):
        meta = dict(name=job_name, uid=uid, namespace=cfg['namespace'], labels={'weir.batchstream.io/owner':cfg['owner']})
        job = dict(kind='Job', metadata=meta, status=dict(succeeded=1, completionTime=iso(finished+1,PROM.timezone.utc).isoformat(), conditions=[dict(type='Complete',status='True')]))
        meta = dict(name=entry['pod'], uid=entry['uid'], namespace=cfg['namespace'], labels={'weir.batchstream.io/owner':cfg['owner']}, ownerReferences=[dict(kind='Job',uid=uid)])
        state = dict(terminated=dict(exitCode=0, reason='Completed', startedAt=iso(900,PROM.timezone.utc).isoformat(), finishedAt=iso(finished,PROM.timezone.utc).isoformat()))
        container=dict(name=entry['container'],containerID=entry['container_id'],imageID=entry['image_id'],restartCount=0,ready=False,state=state)
        pod=dict(kind='Pod',metadata=meta,spec=dict(nodeName=entry['node']),status=dict(phase='Succeeded',podIP=entry['pod_ip'],containerStatuses=[container]))
        items += [job,pod]
    snapshot=directory/'terminal.json';snapshot.write_text(json.dumps(dict(captured_at=iso(end+10,PROM.timezone.utc).isoformat(),items=items)))
    options=SimpleNamespace(start=start,end=end,phase='postrun',config=config,output=directory,run_config=run_config,observations=observations,observer_status=status_file,load_report=load,load_exit=load_exit,terminal_snapshot=snapshot)
    return cfg, options


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
            receipt = dict(requests=[], start=990, end=1125)
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

    def test_complete_audit_binds_all_bytes_and_frozen_duration(self):
        for seconds in (120, 86400):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as temporary:
                cfg, options = bundle(Path(temporary), seconds)
                PROM.read_config(options.config)
                result = PROM.audit(cfg, options)
                self.assertEqual(result['state'], 'passed')
                self.assertEqual(result['lifecycle']['required_duration_ns'], seconds * 10**9)
                self.assertEqual(result['lifecycle']['actual_elapsed_ns'], seconds * 10**9)
                for path in (options.config, options.run_config, options.load_report, options.observations, options.observer_status, options.load_exit, options.terminal_snapshot, options.output/'raw.json', options.output/'receipt.json'):
                    self.assertEqual(result['files'][str(path)], dict(sha256=PROM.sha(path.read_bytes()), bytes=path.stat().st_size))

    def test_complete_bundle_rejects_review_reproductions_and_invalid_gates(self):
        cases = ('wrong-settings-failed-tail', 'failed-observer-tail', 'wrong-status', 'nonzero-load-exit', 'nonzero-container-exit', 'still-active', 'observer-active', 'shorter-than-frozen', 'nan', 'boolean-count', 'negative-count', 'missing-field', 'wrong-freeze', 'truncated-jsonl', 'inconsistent-time', 'failed-middle', 'missing-snapshot')
        for mode in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                cfg, options = bundle(Path(temporary))
                records = [json.loads(line) for line in options.load_report.read_text().splitlines()]
                if mode == 'wrong-settings-failed-tail':
                    observed = [json.loads(line) for line in options.observations.read_text().splitlines()]
                    observed[0]['runID'] = 'weir-soak-other'
                    observed.append(dict(passed=False,error='observer deadline reached'))
                    options.observations.write_text(''.join(json.dumps(r)+'\n' for r in observed))
                elif mode == 'failed-observer-tail':
                    with options.observations.open('a') as output: output.write('{"passed":false,"error":"failed"}\n')
                elif mode == 'wrong-status':
                    status=json.loads(options.observer_status.read_text());status['runID']='weir-soak-other';options.observer_status.write_text(json.dumps(status))
                elif mode == 'nonzero-load-exit': options.load_exit.write_text('{"exit_code":1}')
                elif mode in ('nonzero-container-exit','still-active','observer-active'):
                    snapshot=json.loads(options.terminal_snapshot.read_text())
                    if mode=='nonzero-container-exit': snapshot['items'][1]['status']['containerStatuses'][0]['state']['terminated']['exitCode']=1
                    else: snapshot['items'][2 if mode=='observer-active' else 0]['status']=dict(active=1)
                    options.terminal_snapshot.write_text(json.dumps(snapshot))
                elif mode == 'shorter-than-frozen':
                    run=json.loads(options.run_config.read_text());run.update(duration='24h',observerDuration='24h10m')
                    canonical=json.dumps(run,sort_keys=True,separators=(',',':')).encode();options.run_config.write_bytes(canonical)
                    cfg['run_config_sha256']=PROM.sha(canonical);options.config.write_text(json.dumps(cfg))
                    receipt=json.loads((options.output/'receipt.json').read_text());receipt['freeze_sha256']=PROM.sha(options.config.read_bytes());(options.output/'receipt.json').write_text(json.dumps(receipt))
                    status=json.loads(options.observer_status.read_text());status['runConfigSHA256']=cfg['run_config_sha256'];options.observer_status.write_text(json.dumps(status))
                    observed=[json.loads(line) for line in options.observations.read_text().splitlines()];observed[0].update(runConfigSHA256=cfg['run_config_sha256'],loadDuration=86400*10**9,duration=87000*10**9)
                    options.observations.write_text(''.join(json.dumps(row)+'\n' for row in observed))
                elif mode == 'nan':
                    for key in ('elapsed_ns','cycles','cycle_p99_upper_ns','interval_p99_upper_ns'):records[-1][key]=float('nan')
                elif mode == 'boolean-count': records[-1]['cycles']=True
                elif mode == 'negative-count': records[-1]['cycle_p99_upper_ns']=-1
                elif mode == 'missing-field': del records[-1]['unknown']
                elif mode == 'wrong-freeze': options.run_config.write_text('{}')
                elif mode == 'inconsistent-time': records[-1]['elapsed_ns']=200000000000
                elif mode == 'failed-middle': records.insert(8,dict(kind='observer_failure',error='failed'))
                elif mode == 'missing-snapshot': options.terminal_snapshot=None
                options.load_report.write_text(''.join(json.dumps(r)+'\n' for r in records))
                if mode == 'truncated-jsonl': options.load_report.write_text(options.load_report.read_text().rstrip())
                with self.assertRaises((ValueError,KeyError,TypeError)):
                    PROM.audit(cfg, options)

    def test_sdk_owned_cleaned_optional_duration_is_strict_when_present(self):
        # Field sets from a verified successful SDK v0.1.1 report; identities are synthetic.
        fields = {'backend', 'kind', 'resource', 'sequence', 'target', 'worker'}
        for kind in ('owned', 'cleaned'):
            for duration in (None, True, -1, 0.5, '0', float('nan'), 0, 1000):
                with self.subTest(kind=kind, duration=duration), tempfile.TemporaryDirectory() as temporary:
                    cfg, options = bundle(Path(temporary))
                    records = [json.loads(line) for line in options.load_report.read_text().splitlines()]
                    record = next(row for row in records if row.get('kind') == kind)
                    self.assertEqual(set(record), fields)
                    record['duration_ns'] = duration
                    options.load_report.write_text(''.join(json.dumps(row)+'\n' for row in records))
                    if type(duration) is int and duration >= 0:
                        self.assertEqual(PROM.audit(cfg, options)['state'], 'passed')
                    else:
                        with self.assertRaises(ValueError): PROM.audit(cfg, options)
            for missing in ('worker', 'target', 'backend', 'sequence'):
                with self.subTest(kind=kind, missing=missing), tempfile.TemporaryDirectory() as temporary:
                    cfg, options = bundle(Path(temporary))
                    records = [json.loads(line) for line in options.load_report.read_text().splitlines()]
                    del next(row for row in records if row.get('kind') == kind)[missing]
                    options.load_report.write_text(''.join(json.dumps(row)+'\n' for row in records))
                    with self.assertRaises(KeyError): PROM.audit(cfg, options)

    def test_full_audit_uses_only_raw_samples_in_closed_window(self):
        modes = ('outside-identities', 'outside-nan', 'outside-reset-health', 'inside-uid', 'inside-cgroup',
                 'inside-source', 'inside-nan', 'inside-up', 'inside-memory', 'inside-queue', 'inside-reset',
                 'growth-only-outside', 'samples-only-outside')
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                cfg, options = bundle(Path(temporary))
                path = options.output/'raw.json'
                raw = json.loads(path.read_bytes())
                series = raw['data']['result']
                if mode in ('outside-identities', 'inside-uid', 'inside-cgroup', 'inside-source'):
                    name = 'container_memory_rss' if mode == 'inside-cgroup' else 'kube_pod_info'
                    source = next(row for row in series if row['metric']['__name__'] == name)
                    if mode == 'inside-source': source = series[-1]
                    extra = copy.deepcopy(source)
                    extra['metric'].update(uid='old-uid', id='old-cgroup', pod_ip='old-ip')
                    extra['values'] = [[options.start-1, '1'], [options.end+1, '1']] if mode == 'outside-identities' else [[options.start, '1']]
                    series.append(extra)
                    if mode == 'outside-identities':
                        extra = copy.deepcopy(next(row for row in series if row['metric']['__name__'] == 'kube_pod_container_info'))
                        del extra['metric']['container_id']
                        del extra['metric']['image_id']
                        extra['values'] = [[options.start-1, '1']]
                        series.append(extra)
                else:
                    for row in series:
                        name = row['metric']['__name__']
                        if name in PROM.IDENTITY: continue
                        inside = [point for point in row['values'] if options.start <= point[0] <= options.end]
                        if mode.startswith('outside-'):
                            value = 'NaN' if mode == 'outside-nan' else (0 if name in ('up','weir_node_ready') else 999999)
                            row['values'] = [[options.start-1,value]] + inside + [[options.end+1,value]]
                        elif mode == 'growth-only-outside' and name.endswith(('_total','_count','_sum')) and name in PROM.APPLICATION:
                            row['values'] = [[options.start-1,0]] + [[t,50] for t,_ in inside] + [[options.end+1,100]]
                        elif mode == 'samples-only-outside': row['values'] = [[options.start-1,1],[options.end+1,1]]
                        elif mode == 'inside-nan' and name == 'container_memory_rss': row['values'][-1][1] = 'NaN'
                        elif mode == 'inside-up' and name == 'up': row['values'][-1][1] = 0
                        elif mode == 'inside-memory' and name == 'container_memory_working_set_bytes': row['values'][-1][1] = 1001
                        elif mode == 'inside-queue' and name == 'weir_store_pending_entries': row['values'][-1][1] = 2
                        elif mode == 'inside-reset' and name == 'container_cpu_usage_seconds_total': row['values'][-1][1] = 0
                body = json.dumps(raw).encode(); path.write_bytes(body)
                receipt_path = options.output/'receipt.json'
                receipt = json.loads(receipt_path.read_bytes())
                receipt['requests'][0].update(sha256=PROM.sha(body), bytes=len(body))
                receipt_path.write_text(json.dumps(receipt))
                if mode.startswith('outside-'):
                    result = PROM.audit(cfg, options)
                    self.assertEqual(result['state'], 'passed')
                    self.assertEqual(result['files'][str(path)]['sha256'], PROM.sha(body))
                    self.assertAlmostEqual(result['containers']['uid-0']['resources']['cpu_cores']['peak'], .1)
                else:
                    with self.assertRaises(ValueError): PROM.audit(cfg, options)

    def test_window_boundaries_need_two_inside_samples_and_bounded_gaps(self):
        bounds = (1000, 1200)
        values = [(999,999), (1000,1), (1090,2), (1180,3), (1200,4), (1201,0)]
        used, gap = PROM.coverage(values, bounds)
        self.assertEqual(used, values[1:-1])
        self.assertEqual(gap, 90)
        for values in ([(999,1),(1091,2),(1180,3)], [(1000,1),(1090,2),(1201,3)], [(999,1),(1100,2),(1201,3)]):
            with self.assertRaises(ValueError): PROM.coverage(values, bounds)

    def test_historical_preflight_audit_never_publishes_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg, options = bundle(Path(temporary))
            receipt_path = options.output/'receipt.json'
            receipt = json.loads(receipt_path.read_bytes()); receipt['phase'] = 'preflight'
            receipt_path.write_text(json.dumps(receipt))
            command = [sys.executable, str(ROOT/'scripts/prometheus-evidence.py'), 'audit', '--phase', 'preflight',
                       '--config', str(options.config), '--run-config', str(options.run_config), '--start', '1970-01-01T00:16:40Z',
                       '--end', '1970-01-01T00:18:40Z', '--output', str(options.output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((options.output/'ready').exists())
            command[2] = 'collect'
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('recent raw samples', result.stderr)
            self.assertFalse((options.output/'ready').exists())
