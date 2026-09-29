#!/usr/bin/env python3
"""Collect and audit raw Prometheus evidence for one frozen Weir acceptance run.

No query_range, interpolated points, background polling or load cancellation.
"""
import argparse
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

RESOURCE = ('container_cpu_usage_seconds_total', 'container_memory_working_set_bytes',
            'container_memory_rss', 'container_memory_usage_bytes', 'container_oom_events_total',
            'container_start_time_seconds', 'container_last_seen')
APPLICATION = ('up', 'weir_node_ready', 'weir_rpc_completions_total', 'weir_store_executions_total',
               'weir_store_records_total', 'weir_store_pending_entries', 'weir_store_pending_entries_limit',
               'weir_store_active_executions', 'weir_store_window_limit',
               'weir_store_queue_wait_seconds_count', 'weir_store_queue_wait_seconds_sum',
               'weir_store_execution_seconds_count', 'weir_store_execution_seconds_sum',
               'process_resident_memory_bytes', 'go_memstats_heap_alloc_bytes')
IDENTITY = ('kube_pod_info', 'kube_pod_container_info')
GAP, AGE, SKEW = 90, 120, 30


def sha(body):
    return hashlib.sha256(body).hexdigest()


def save(path, body):
    with path.open('xb') as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def save_json(path, value):
    save(path, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode())


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be an explicit string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return parsed.timestamp()


def reject_constant(value):
    raise ValueError('nonfinite JSON number: ' + value)


def integer(value, field):
    if type(value) is not int or value < 0:
        raise ValueError('expected nonnegative integer: ' + field)
    return value


def duration_ns(value):
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', value)
    if not match or not any(match.groups()):
        raise ValueError('frozen duration must use whole h/m/s units')
    hours, minutes, seconds = (int(v or 0) for v in match.groups())
    duration = (hours * 3600 + minutes * 60 + seconds) * 1000000000
    if not 0 < duration <= 25 * 3600 * 1000000000:
        raise ValueError('frozen duration outside acceptance bound')
    return duration


def read_evidence(path, files, lines=False):
    if path is None:
        raise ValueError('required evidence file missing')
    raw = path.read_bytes()
    files[str(path)] = {'sha256': sha(raw), 'bytes': len(raw)}
    if lines:
        if not raw.endswith(b'\n'):
            raise ValueError('incomplete JSONL final line')
        return [json.loads(line, parse_constant=reject_constant) for line in raw.splitlines()]
    return json.loads(raw, parse_constant=reject_constant)



def read_config(path):
    raw = path.read_bytes()
    cfg = json.loads(raw, parse_constant=reject_constant)
    if not re.fullmatch(r'weir-soak-[a-z0-9-]+', cfg['run']):
        raise ValueError('invalid run identity')
    for field in ('run_config_sha256', 'tool_sha256'):
        if not re.fullmatch('[a-f0-9]{64}', cfg[field]):
            raise ValueError('invalid ' + field)
    if cfg['tool_sha256'] != sha(Path(__file__).read_bytes()):
        raise ValueError('frozen audit tool hash differs')
    source = cfg['prometheus']
    url = urlsplit(source['url'])
    if url.scheme not in ('http', 'https') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Prometheus URL must be an explicit credential-free HTTP endpoint')
    if not source['uid'] or not source['namespace'] or not source['pod'] or not cfg['namespace_uid'] or not cfg['owner']:
        raise ValueError('source/namespace identity missing')
    if not cfg['load_job_uid'] or not cfg['observer']['job_uid']:
        raise ValueError('frozen Job identity missing')
    for field in ('pod', 'uid', 'node', 'pod_ip', 'container', 'container_id', 'image_id'):
        if not cfg['observer'][field]:
            raise ValueError('frozen observer identity missing')
    entries = cfg['containers']
    if len(entries) != 6 or len({e['uid'] for e in entries}) != 6:
        raise ValueError('freeze exactly three Weir, two backend and one waiting load Pod')
    if sum(e['role'] == 'weir' for e in entries) != 3 or sum(e['role'] == 'load' for e in entries) != 1:
        raise ValueError('invalid frozen roles')
    for e in entries:
        for key in ('pod', 'uid', 'container', 'container_id', 'image_id', 'node', 'pod_ip', 'cadvisor_id'):
            if not e.get(key):
                raise ValueError('missing container identity ' + key)
        cid = e['container_id'].split('://')[-1]
        if cid not in e['cadvisor_id'] or e['uid'].replace('-', '_') not in e['cadvisor_id'].replace('-', '_'):
            raise ValueError('cgroup does not bind Pod UID and container ID')
        integer(e['memory_limit_bytes'], 'memory limit')
        if type(e['cpu_limit']) not in (int, float) or not math.isfinite(e['cpu_limit']) or e['cpu_limit'] <= 0 or e['memory_limit_bytes'] <= 0:
            raise ValueError('freeze positive resource limits from PodSpec')
        if e['role'] == 'weir' and (e['instance'] != e['pod_ip'] + ':7449' or e['stores'] != ['mongo', 'search']):
            raise ValueError('unexpected Weir endpoint or stores')
    return cfg, raw


def selectors(cfg):
    # Explicit names retain wrong-UID/cgroup responses for rejection instead of hiding them.
    source = cfg['prometheus']
    source_query = '{__name__="kube_pod_info",namespace=' + json.dumps(source['namespace']) + ',pod=' + json.dumps(source['pod']) + '}'
    result = [source_query]
    for entry in cfg['containers']:
        labels = {'namespace': cfg['namespace'], 'pod': entry['pod'], 'container': entry['container']}
        suffix = ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
        result.append('{__name__=~' + json.dumps('|'.join(RESOURCE)) + ',' + suffix + '}')
        identity = '{__name__=~' + json.dumps('|'.join(IDENTITY)) + ',namespace=' + json.dumps(cfg['namespace']) + ',pod=' + json.dumps(entry['pod']) + '}'
        result.append(identity)
        if entry['role'] == 'weir':
            labels = {'job': 'pod', 'pod': entry['pod'], 'instance': entry['instance']}
            suffix = ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
            result.append('{__name__=~' + json.dumps('|'.join(APPLICATION)) + ',' + suffix + '}')
    return result


def collect(cfg, options):
    output = options.output
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'freeze.json', options.config.read_bytes())
    receipt = {'phase': options.phase, 'start': options.start, 'end': options.end,
               'freeze_sha256': sha(options.config.read_bytes()), 'source': cfg['prometheus'], 'requests': []}
    try:
        end = options.start
        while end < options.end:
            begin, end = end, min(end + 900, options.end)
            for selector in selectors(cfg):
                query = selector + '[' + str(math.ceil(end - begin + AGE) + 1) + 's]'
                params = {'query': query, 'time': repr(end), 'timeout': '20s'}
                name = 'raw-%05d.json' % len(receipt['requests'])
                request = {'file': name, 'params': params, 'requested_at': time.time()}
                receipt['requests'].append(request)
                try:
                    with urlopen(cfg['prometheus']['url'].rstrip('/') + '/api/v1/query?' + urlencode(params), timeout=25) as response:
                        body = response.read(32 * 1024 * 1024 + 1)
                    if len(body) > 32 * 1024 * 1024:
                        raise ValueError('raw response exceeds bound')
                    save(output / name, body)
                    request.update(sha256=sha(body), bytes=len(body))
                except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
                    request['error'] = type(exc).__name__
                    if isinstance(exc, HTTPError):
                        request['http_status'] = exc.code
                        exc.close()
                    receipt['state'] = 'pending'
                    return False
        receipt['state'] = 'collected'
        return True
    finally:
        save_json(output / 'receipt.json', receipt)


def merge_raw(directory, receipt, files=None):
    merged = {}
    for request in receipt['requests']:
        body = (directory / request['file']).read_bytes()
        if sha(body) != request['sha256'] or len(body) != request['bytes']:
            raise ValueError('raw response checksum mismatch')
        if files is not None:
            files[str(directory / request['file'])] = {'sha256': sha(body), 'bytes': len(body)}
        result = json.loads(body, parse_constant=reject_constant)
        if result.get('status') != 'success' or result.get('warnings') or result.get('infos') or result['data']['resultType'] != 'matrix':
            raise ValueError('incomplete or non-matrix raw response')
        for series in result['data']['result']:
            labels = series['metric']
            key = tuple(sorted(labels.items()))
            values = merged.setdefault(key, {})
            previous = -math.inf
            for point, raw in series['values']:
                if not math.isfinite(point) or point <= previous:
                    raise ValueError('invalid/non-increasing raw sample')
                previous = point
                if not receipt['start'] <= point <= receipt['end']:
                    continue
                value = float(raw)
                if not math.isfinite(value) or value < 0:
                    raise ValueError('invalid raw sample inside audit window')
                if point in values and values[point] != value:
                    raise ValueError('conflicting duplicate raw timestamp')
                values[point] = value
    result = [(dict(key), sorted(values.items())) for key, values in merged.items() if values]
    return result


def coverage(values, bounds):
    start, end = bounds
    used = [p for p in values if start <= p[0] <= end]
    if len(used) < 2:
        raise ValueError('missing raw series/insufficient samples')
    gaps = [used[0][0] - start, end - used[-1][0]]
    gaps += [b[0] - a[0] for a, b in zip(used, used[1:])]
    if max(gaps) > GAP:
        raise ValueError('raw sample gap or boundary exceeds 90 seconds')
    return used, max(gaps)


def trend(values):
    vals = [v for _, v in values]
    midpoint = max(1, len(vals) // 2)
    result = {'first': vals[0], 'last': vals[-1], 'min': min(vals), 'max': max(vals),
              'first_half_mean': sum(vals[:midpoint]) / midpoint,
              'last_half_mean': sum(vals[-midpoint:]) / midpoint}
    return result


def counter(values):
    if any(b[1] < a[1] for a, b in zip(values, values[1:])):
        raise ValueError('counter reset: do not combine lifetimes or use rate to conceal it')
    return values[-1][1] - values[0][1]


def audit_container(entry, series, options):
    bounds = (options.start, options.end)
    selected = {}
    identities = []
    for labels, values in series:
        if labels.get('pod') != entry['pod']:
            continue
        name = labels['__name__']
        if name in IDENTITY:
            identities.append(labels)
            if labels.get('uid') != entry['uid']:
                raise ValueError('KSM Pod UID differs')
            if name == 'kube_pod_info' and (labels.get('node') != entry['node'] or labels.get('pod_ip') != entry['pod_ip']):
                raise ValueError('KSM Pod address/node differs')
            if name == 'kube_pod_container_info' and (labels.get('container') != entry['container'] or labels.get('container_id') != entry['container_id'] or labels.get('image_id') != entry['image_id']):
                raise ValueError('KSM container identity differs')
        if name not in RESOURCE:
            continue
        if labels.get('container') != entry['container'] or labels.get('id') != entry['cadvisor_id'] or labels.get('name') != entry['container_id'].split('://')[-1] or labels.get('instance') != entry['node'] or labels.get('image') != entry['image_id']:
            raise ValueError('cAdvisor identity differs from frozen container')
        if name in selected:
            raise ValueError('duplicate cAdvisor scrape source')
        selected[name] = coverage(values, bounds)[0]
    if set(selected) != set(RESOURCE):
        raise ValueError('missing mandatory cAdvisor resource series')
    seen = selected['container_last_seen']
    if any(abs(t - v) > SKEW for t, v in seen):
        raise ValueError('exporter last_seen clock differs by over 30 seconds')
    summaries = {}
    for name, values in selected.items():
        summary = trend(values)
        summary['max_gap_seconds'] = coverage(values, bounds)[1]
        if name in RESOURCE[:4]:
            timestamps = [t for t, _ in values]
            ages = []
            for scrape, observed in seen:
                index = bisect_right(timestamps, observed + SKEW) - 1
                # Choose the latest actual statistics no later than exporter time.
                past = bisect_right(timestamps, observed) - 1
                if past >= 0:
                    index = past
                if index < 0:
                    raise ValueError('no resource sample at exporter observation')
                age = observed - timestamps[index]
                if age > AGE or age < -SKEW:
                    raise ValueError('resource source age exceeds 120 seconds or future tolerance')
                ages.append(age)
            if not ages:
                raise ValueError('no exporter observations inside run')
            summary['max_source_age_seconds'] = max(ages)
        summaries[name] = summary
    cpu = selected['container_cpu_usage_seconds_total']
    counter(cpu)
    cpu_rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(cpu, cpu[1:])]
    summaries['cpu_cores'] = {'peak': max(cpu_rates), 'limit': entry['cpu_limit'], 'peak_headroom': entry['cpu_limit'] - max(cpu_rates)}
    memory_peak = max(v for _, v in selected['container_memory_working_set_bytes'])
    summaries['memory_headroom_bytes'] = entry['memory_limit_bytes'] - memory_peak
    if memory_peak > entry['memory_limit_bytes']:
        raise ValueError('working set exceeds frozen memory limit')
    if len({v for _, v in selected['container_start_time_seconds']}) != 1:
        raise ValueError('container start time changed')
    if any(v != 0 for _, v in selected['container_oom_events_total']):
        raise ValueError('OOM event present')
    if options.phase == 'preflight' and {item['__name__'] for item in identities} != set(IDENTITY):
        raise ValueError('preflight needs live KSM UID and container association')
    if not identities:
        summaries['identity_note'] = 'KSM absent; exact frozen cgroup UID/container and lifecycle evidence remain required'
    return summaries


def audit_application(entry, series, options):
    required = set(APPLICATION)
    summaries, present, stores, records, rpcs = [], set(), {}, {}, set()
    families = {}
    up = None
    selected = []
    for labels, values in series:
        name = labels['__name__']
        if labels.get('pod') != entry['pod'] or name not in required:
            continue
        if labels.get('job') != 'pod' or labels.get('instance') != entry['instance']:
            raise ValueError('application target differs')
        semantic = tuple((k, v) for k, v in sorted(labels.items()) if k not in ('job', 'instance', 'pod'))
        if any(key == semantic for key, _, _ in selected):
            raise ValueError('duplicate application scrape source')
        values, gap = coverage(values, (options.start, options.end))
        selected.append((semantic, labels, values))
        present.add(name)
        if name.startswith('weir_store_'):
            families.setdefault(name, set()).add(labels.get('store'))
        if name == 'up':
            up = values
        if name in ('up', 'weir_node_ready') and any(v != 1 for _, v in values):
            raise ValueError('scrape or Weir readiness not continuously up')
        summary = {'labels': labels, 'trend': trend(values), 'max_gap_seconds': gap}
        if name.endswith(('_total', '_count', '_sum')):
            delta = counter(values)
            summary['delta'] = delta
            if name == 'weir_rpc_completions_total':
                if labels.get('status') != 'ok' and delta != 0:
                    raise ValueError('non-OK application RPC increment')
                if labels.get('status') == 'ok' and delta > 0:
                    rpcs.add(labels.get('method'))
            if name == 'weir_store_executions_total' and labels.get('kind') == 'record':
                stores[labels.get('store')] = delta
            if name == 'weir_store_records_total':
                records[labels.get('store')] = records.get(labels.get('store'), 0) + delta
        summaries.append(summary)
    if present != required:
        raise ValueError('missing application series: ' + ','.join(sorted(required - present)))
    if any(stores_seen != set(entry['stores']) for stores_seen in families.values()):
        raise ValueError('missing backend series family')
    for _, labels, values in selected:
        up_times = [t for t, _ in up]
        for point, _ in values:
            index = bisect_right(up_times, point) - 1
            if index < 0 or point - up_times[index] > SKEW:
                raise ValueError('application sample lacks matching successful scrape time')
    for store in entry['stores']:
        for used_name, limit_name in (('weir_store_pending_entries', 'weir_store_pending_entries_limit'), ('weir_store_active_executions', 'weir_store_window_limit')):
            used_series = [values for _, labels, values in selected if labels['__name__'] == used_name and labels.get('store') == store]
            limit_series = [values for _, labels, values in selected if labels['__name__'] == limit_name and labels.get('store') == store]
            if len(used_series) != 1 or len(limit_series) != 1:
                raise ValueError('ambiguous queue/execution capacity')
            limits = dict(limit_series[0])
            if any(point not in limits or value > limits[point] for point, value in used_series[0]):
                raise ValueError('queue/execution exceeds same-scrape capacity')
    if options.phase == 'postrun' and (set(stores) != set(entry['stores']) or min(stores.values()) <= 0 or set(records) != set(entry['stores']) or min(records.values()) <= 0 or not {'Read', 'Mutate', 'Bulk'}.issubset(rpcs)):
        raise ValueError('missing per-Pod backend/RPC execution increment')
    return summaries


def verify_run_config(cfg, options, files):
    run = read_evidence(getattr(options, 'run_config', None), files)
    canonical = json.dumps(run, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if sha(canonical) != cfg['run_config_sha256']:
        raise ValueError('canonical run configuration checksum differs')
    for field in ('run', 'namespace', 'owner'):
        if run[field] != cfg[field]:
            raise ValueError('run configuration identity differs: ' + field)
    if run['resourceEvidenceSource'] != 'prometheus' or run['evidenceToolSHA256'] != cfg['tool_sha256'] or run['prometheusSourceUID'] != cfg['prometheus']['uid']:
        raise ValueError('run evidence source differs')
    duration_ns(run['duration'])
    if duration_ns(run['observerDuration']) < duration_ns(run['duration']) or duration_ns(run['interval']) > 60000000000:
        raise ValueError('invalid frozen observation duration/interval')
    return run


def verify_pod(pod, entry, terminal=False):
    if pod['metadata']['name'] != entry['pod'] or pod['metadata']['uid'] != entry['uid'] or pod['spec']['nodeName'] != entry['node'] or pod['status']['podIP'] != entry['pod_ip']:
        raise ValueError('Pod identity differs from freeze')
    statuses = pod['status']['containerStatuses']
    if len(statuses) != 1:
        raise ValueError('unexpected container identity count')
    container = statuses[0]
    if container['name'] != entry['container'] or container['containerID'] != entry['container_id'] or container['imageID'] != entry['image_id'] or integer(container['restartCount'], 'restart count') != 0:
        raise ValueError('container identity/restart differs')
    if terminal or pod['status']['phase'] == 'Succeeded':
        state = container['state']['terminated']
        if pod['status']['phase'] != 'Succeeded' or integer(state['exitCode'], 'container exit') != 0 or state['reason'] != 'Completed':
            raise ValueError('container lacks successful terminal evidence')
        finished = timestamp(state['finishedAt'])
        if timestamp(state['startedAt']) > finished:
            raise ValueError('container termination time is invalid')
        return finished
    if pod['status']['phase'] != 'Running' or container['ready'] is not True:
        raise ValueError('Pod lifecycle/readiness failed')
    return None


def verify_lifecycle(cfg, options, files, run):
    if options.phase == 'preflight':
        return {'required_duration_ns': duration_ns(run['duration'])}
    status = read_evidence(getattr(options, 'observer_status', None), files)
    load = read_evidence(getattr(options, 'load_report', None), files, True)
    observed = read_evidence(getattr(options, 'observations', None), files, True)
    exit_status = read_evidence(getattr(options, 'load_exit', None), files)
    terminal = read_evidence(getattr(options, 'terminal_snapshot', None), files)
    required = duration_ns(run['duration'])
    observer_duration = duration_ns(run['observerDuration'])
    interval = duration_ns(run['interval'])
    if integer(exit_status['exit_code'], 'load exit') != 0:
        raise ValueError('load exit is not successful')
    if status.get('passed') is not True or status.get('error'):
        raise ValueError('observer did not pass')
    binding = {'runID': cfg['run'], 'namespace': cfg['namespace'], 'job': cfg['run'], 'runConfigSHA256': cfg['run_config_sha256']}
    if any(status.get(key) != value for key, value in binding.items()):
        raise ValueError('observer status run/config differs')
    completed = timestamp(status['completed_at'])
    if len(load) < 2 or load[0].get('kind') != 'start' or load[-1].get('kind') != 'passed':
        raise ValueError('runner lacks complete start/passed report')
    first, last = load[0], load[-1]
    expected_start = {'run_id': cfg['run'], 'duration_ns': required, 'workers': 6, 'cycles_per_second': 5, 'max_p99_ns': 500000000,
                      'server_revision': run['serverRevision'], 'image_digest': run['imageDigest'], 'sdk_revision': run['sdkRevision'], 'chart_version': run['chartVersion']}
    for field, expected in expected_start.items():
        if type(expected) is int:
            integer(first[field], field)
        if first[field] != expected:
            raise ValueError('runner differs from frozen configuration: ' + field)
    start, end = timestamp(first['started_utc']), timestamp(last['utc'])
    if abs(start - options.start) > .001 or abs(end - options.end) > .001 or end <= start:
        raise ValueError('Prom window must equal complete actual runner start/end')
    targets = run['targets']
    if first['targets'] != targets or len(targets) != 3 or set(targets) != {e['pod_ip'] + ':7447' for e in cfg['containers'] if e['role'] == 'weir'}:
        raise ValueError('runner targets differ from frozen Pods')
    previous_elapsed, previous_cycles, previous_time = -1, -1, start
    owned, cleaned = set(), set()
    progress_count = 0
    for index, record in enumerate(load[1:], 1):
        kind = record.get('kind')
        if record.get('error') or record.get('unknown') not in (None, 0, False):
            raise ValueError('runner error event present')
        if kind in ('owned', 'cleaned'):
            worker = integer(record['worker'], 'worker')
            expected_backend = 'mongo' if worker % 2 == 0 else 'search'
            if worker >= 6 or record['target'] != targets[worker % 3] or record['backend'] != expected_backend:
                raise ValueError('worker event differs from frozen target')
            destination = owned if kind == 'owned' else cleaned
            if worker in destination or (kind == 'cleaned' and worker not in owned):
                raise ValueError('worker ownership/cleanup sequence is invalid')
            destination.add(worker)
            integer(record['sequence'], 'sequence')
            if 'duration_ns' in record:
                integer(record['duration_ns'], 'operation duration')
            continue
        if kind not in ('progress', 'passed') or (kind == 'passed') != (index == len(load) - 1) or record.get('run_id') != cfg['run']:
            raise ValueError('unexpected runner record or failure tail')
        for field in ('elapsed_ns', 'cycles', 'verified_mutations', 'verified_reads', 'stream_checks', 'failures', 'unknown', 'cycle_p99_upper_ns', 'interval_p99_upper_ns', 'cycle_overflow'):
            integer(record[field], field)
        if record['failures'] or record['unknown'] or record['cycle_p99_upper_ns'] > 500000000 or record['interval_p99_upper_ns'] > 500000000:
            raise ValueError('runner interval gate failed')
        histogram = record['cycle_histogram']
        if len(histogram) != 12 or sum(integer(value, 'histogram count') for value in histogram) + record['cycle_overflow'] != record['cycles']:
            raise ValueError('runner histogram/count evidence differs')
        if record['verified_mutations'] != record['cycles'] * 2 or record['verified_reads'] != record['cycles'] * 2:
            raise ValueError('runner confirmation counts differ')
        utc = timestamp(record['utc'])
        elapsed = record['elapsed_ns'] / 1e9
        if record['elapsed_ns'] < previous_elapsed or record['cycles'] < previous_cycles or utc < previous_time or abs((utc - start) - elapsed) > 1:
            raise ValueError('runner UTC/monotonic time or counters are inconsistent')
        if utc - previous_time > GAP:
            raise ValueError('runner minute report missing')
        previous_elapsed, previous_cycles, previous_time = record['elapsed_ns'], record['cycles'], utc
        if kind == 'progress':
            progress_count += 1
    if owned != set(range(6)) or cleaned != owned:
        raise ValueError('runner lacks complete owned/cleaned worker evidence')
    if last['elapsed_ns'] < required or end - start < required / 1e9 - .001 or last['elapsed_ns'] > observer_duration:
        raise ValueError('runner did not complete frozen duration')
    if last['cycles'] * 100 < required // 1000000000 * 30 * 98:
        raise ValueError('runner coverage below frozen 98 percent target')
    if len(observed) < 2:
        raise ValueError('observer settings or observations missing')
    settings = observed[0]
    binding.update(loadDuration=required, interval=interval, duration=observer_duration, replicas=3, release=run.get('release', 'weir'))
    for field, expected in binding.items():
        if type(expected) is int:
            integer(settings[field], field)
        if settings.get(field) != expected:
            raise ValueError('observer settings differ: ' + field)
    times = []
    for record in observed[1:]:
        if set(record) != {'utc', 'pods', 'loadPods', 'job'}:
            raise ValueError('unknown observer record or failed tail')
        job = record['job']
        if job['metadata']['uid'] != cfg['load_job_uid'] or job['metadata']['name'] != cfg['run'] or integer(job['status'].get('failed', 0), 'Job failed'):
            raise ValueError('observer Job identity/failure differs')
        times.append(timestamp(record['utc']))
        pods = record['pods'] + record['loadPods']
        if len(pods) != 6 or len(record['loadPods']) != 1:
            raise ValueError('observer Pod set differs')
        for entry in cfg['containers']:
            matching = [p for p in pods if p['metadata']['name'] == entry['pod']]
            if len(matching) != 1:
                raise ValueError('observer lacks frozen Pod')
            if entry['role'] != 'load' and matching[0]['status']['phase'] != 'Running':
                raise ValueError('service Pod left running lifecycle')
            finished = verify_pod(matching[0], entry)
            if finished is not None and (finished < end - 1 or finished > times[-1] + 1):
                raise ValueError('observed load termination time differs')
    if times[0] > start or times[-1] < end or any(b <= a or b - a > GAP for a, b in zip(times, times[1:])):
        raise ValueError('observer does not cover complete run')
    if not times[-1] <= completed <= times[0] + observer_duration / 1e9:
        raise ValueError('observer completion time differs')
    final = observed[-1]
    if integer(final['job']['status'].get('succeeded', 0), 'Job succeeded') != 1 or integer(final['job']['status'].get('active', 0), 'Job active') != 0:
        raise ValueError('final observed load Job is not successful')
    load_entry = next(e for e in cfg['containers'] if e['role'] == 'load')
    load_finished = verify_pod(final['loadPods'][0], load_entry, True)
    if load_finished < end - 1:
        raise ValueError('load exited before its terminal report')
    captured = timestamp(terminal['captured_at'])
    items = terminal['items']
    if len(items) != 4:
        raise ValueError('need exact independent two Job/two Pod terminal snapshot')
    for job_name, uid, entry, after in ((cfg['run'], cfg['load_job_uid'], load_entry, end), (cfg['run'] + '-observer', cfg['observer']['job_uid'], cfg['observer'], completed)):
        matches = [item for item in items if item['kind'] == 'Job' and item['metadata']['name'] == job_name]
        pod_matches = [item for item in items if item['kind'] == 'Pod' and item['metadata']['name'] == entry['pod']]
        if len(matches) != 1 or len(pod_matches) != 1:
            raise ValueError('missing terminal Job or Pod')
        job, pod = matches[0], pod_matches[0]
        for item in (job, pod):
            if item['metadata']['namespace'] != cfg['namespace'] or item['metadata']['labels'].get('weir.batchstream.io/owner') != cfg['owner']:
                raise ValueError('terminal snapshot outside frozen ownership')
        if job['metadata']['uid'] != uid or not any(ref['uid'] == uid and ref['kind'] == 'Job' for ref in pod['metadata']['ownerReferences']):
            raise ValueError('terminal snapshot Job ownership differs')
        state = job['status']
        if integer(state.get('succeeded', 0), 'Job succeeded') != 1 or integer(state.get('failed', 0), 'Job failed') or integer(state.get('active', 0), 'Job active'):
            raise ValueError('terminal Job did not succeed')
        if not any(c['type'] == 'Complete' and c['status'] == 'True' for c in state['conditions']) or any(c['type'] == 'Failed' and c['status'] == 'True' for c in state['conditions']):
            raise ValueError('terminal Job lacks Complete condition')
        finished = verify_pod(pod, entry, True)
        job_completed = timestamp(state['completionTime'])
        if finished < after - 1 or job_completed < finished - 1 or captured < max(finished, job_completed, completed):
            raise ValueError('terminal snapshot chronology differs')
    result = {'required_duration_ns': required, 'actual_elapsed_ns': last['elapsed_ns'], 'actual_start': start, 'actual_end': end,
              'run_config_sha256': cfg['run_config_sha256'], 'observer_completed_at': completed, 'terminal_captured_at': captured,
              'load_job_uid': cfg['load_job_uid'], 'observer_job_uid': cfg['observer']['job_uid'], 'progress_records': progress_count}
    return result

def audit(cfg, options):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (options.start, options.end)) or not options.start < options.end:
        raise ValueError('invalid finite audit start/end')
    files = {}
    frozen = read_evidence(options.config, files)
    if frozen != cfg:
        raise ValueError('audit config differs from frozen file')
    run = verify_run_config(cfg, options, files)
    receipt = read_evidence(options.output / 'receipt.json', files)
    if receipt.get('state') != 'collected':
        raise ValueError('Prometheus evidence pending retrieval, not passed')
    if receipt['freeze_sha256'] != files[str(options.config)]['sha256'] or receipt['phase'] != options.phase or receipt['start'] != options.start or receipt['end'] != options.end:
        raise ValueError('collection does not match frozen audit window')
    series = merge_raw(options.output, receipt, files)
    source = cfg['prometheus']
    source_series = [(labels, values) for labels, values in series if labels.get('namespace') == source['namespace'] and labels.get('pod') == source['pod']]
    if not source_series or any(labels.get('uid') != source['uid'] for labels, _ in source_series):
        raise ValueError('Prometheus source identity cannot be verified')
    for labels, _ in series:
        if labels.get('namespace', cfg['namespace']) != cfg['namespace'] and not (labels.get('namespace') == source['namespace'] and labels.get('pod') == source['pod']):
            raise ValueError('unexpected namespace')
    lifecycle = verify_lifecycle(cfg, options, files, run)
    results = {}
    for entry in cfg['containers']:
        result = {'resources': audit_container(entry, series, options)}
        if entry['role'] == 'weir':
            result['application'] = audit_application(entry, series, options)
        results[entry['uid']] = result
    result = {'state': 'passed', 'phase': options.phase, 'run': cfg['run'], 'freeze_sha256': receipt['freeze_sha256'],
              'start': options.start, 'end': options.end, 'containers': results, 'lifecycle': lifecycle, 'files': files,
              'scope': 'Frozen-duration load, terminal lifecycle and Prometheus evidence verified; independent trend review still required'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'audit'))
    parser.add_argument('--phase', choices=('preflight', 'postrun'), required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--start', type=timestamp, required=True)
    parser.add_argument('--end', type=timestamp, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-config', type=Path, required=True)
    parser.add_argument('--load-report', type=Path)
    parser.add_argument('--load-exit', type=Path)
    parser.add_argument('--terminal-snapshot', type=Path)
    parser.add_argument('--observations', type=Path)
    parser.add_argument('--observer-status', type=Path)
    options = parser.parse_args()
    cfg, raw = read_config(options.config)
    if not 0 < options.end - options.start <= 25 * 3600:
        parser.error('window must be positive and at most 25 hours')
    realtime_preflight = options.command == 'collect' and options.phase == 'preflight'
    if options.phase == 'preflight' and options.end - options.start < 60:
        parser.error('preflight needs at least 60 seconds of raw samples')
    if realtime_preflight and abs(time.time() - options.end) > SKEW:
        parser.error('preflight needs at least 60 seconds of recent raw samples')
    if options.command == 'collect' and not collect(cfg, options):
        print('Prometheus retrieval pending; original request receipt preserved, load not cancelled')
        return 2
    try:
        result = audit(cfg, options)
        if realtime_preflight and time.time() - options.end > AGE:
            raise ValueError('preflight expired before publication')
    except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
        result = {'state': 'failed', 'run': cfg['run'], 'error': str(exc)}
        save_json(options.output / 'audit.json', result)
        print(json.dumps(result))
        return 1
    save_json(options.output / 'audit.json', result)
    if realtime_preflight:
        load = next(e for e in cfg['containers'] if e['role'] == 'load')
        marker = '\n'.join((cfg['run'], cfg['run_config_sha256'], load['uid'], sha(raw), str(int(time.time())))) + '\n'
        save(options.output / 'ready', marker.encode())
    print(json.dumps({'state': result['state'], 'phase': options.phase, 'run': cfg['run']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
