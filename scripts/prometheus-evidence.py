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
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def read_config(path):
    raw = path.read_bytes()
    cfg = json.loads(raw)
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
        if e['cpu_limit'] <= 0 or e['memory_limit_bytes'] <= 0:
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


def merge_raw(directory, receipt):
    merged = {}
    for request in receipt['requests']:
        body = (directory / request['file']).read_bytes()
        if sha(body) != request['sha256'] or len(body) != request['bytes']:
            raise ValueError('raw response checksum mismatch')
        result = json.loads(body)
        if result.get('status') != 'success' or result.get('warnings') or result.get('infos') or result['data']['resultType'] != 'matrix':
            raise ValueError('incomplete or non-matrix raw response')
        for series in result['data']['result']:
            labels = series['metric']
            key = tuple(sorted(labels.items()))
            values = merged.setdefault(key, {})
            previous = -math.inf
            for point, raw in series['values']:
                value = float(raw)
                if not math.isfinite(point) or not math.isfinite(value) or point <= previous or value < 0:
                    raise ValueError('invalid/non-increasing raw sample')
                previous = point
                if point in values and values[point] != value:
                    raise ValueError('conflicting duplicate raw timestamp')
                values[point] = value
    result = [(dict(key), sorted(values.items())) for key, values in merged.items()]
    return result


def coverage(values, bounds):
    start, end = bounds
    points = [p for p in values if start - AGE <= p[0] <= end]
    if len(points) < 2:
        raise ValueError('missing raw series/insufficient samples')
    before = [p for p in points if p[0] <= start]
    inside = [p for p in points if p[0] >= start]
    used = (before[-1:] + inside) if before else inside
    used = sorted(set(used))
    if not used:
        raise ValueError('no samples in observation window')
    gaps = [max(0, used[0][0] - start), end - used[-1][0]]
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
                if scrape < bounds[0]:
                    continue
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
            summary['delta'] = counter(values)
            during = [p for p in values if options.start <= p[0] <= options.end]
            delta = counter(during) if len(during) >= 2 else 0
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


def verify_lifecycle(cfg, options):
    if options.phase == 'preflight':
        return
    if not options.observations or not options.observer_status or not options.load_report:
        raise ValueError('postrun needs complete observer evidence')
    if json.loads(options.observer_status.read_text()).get('passed') is not True:
        raise ValueError('observer did not pass')
    load = [json.loads(line) for line in options.load_report.read_text().splitlines()]
    first, last = load[0], load[-1]
    if first.get('kind') != 'start' or last.get('kind') != 'passed' or first.get('run_id') != cfg['run'] or last.get('run_id') != cfg['run']:
        raise ValueError('runner does not prove frozen run passed')
    if abs(timestamp(first['started_utc']) - options.start) > 0.001 or abs(timestamp(last['utc']) - options.end) > 0.001:
        raise ValueError('Prom window must equal actual runner start/end')
    if last['elapsed_ns'] < first['duration_ns'] or last['failures'] or last['unknown'] or first['workers'] != 6 or first['cycles_per_second'] != 5 or first['max_p99_ns'] != 500000000:
        raise ValueError('runner duration/error/load gates failed')
    if last['cycles'] < first['duration_ns'] / 1e9 * 30 * .98:
        raise ValueError('runner coverage below 98 percent')
    for progress in load:
        if progress.get('kind') in ('progress', 'passed') and (progress['cycle_p99_upper_ns'] > 500000000 or progress['interval_p99_upper_ns'] > 500000000 or progress['failures'] or progress['unknown']):
            raise ValueError('runner interval gate failed')
    baseline = None
    times = []
    job_uid = None
    for line in options.observations.read_text().splitlines():
        record = json.loads(line)
        if 'pods' not in record:
            continue
        if job_uid is None:
            job_uid = record['job']['metadata']['uid']
        if not job_uid or record['job']['metadata']['uid'] != job_uid or record['job']['status'].get('failed', 0):
            raise ValueError('observer Job identity/failure differs')
        observed = timestamp(record['utc'])
        times.append(observed)
        pods = record['pods'] + record['loadPods']
        if baseline is None:
            baseline = record
        for entry in cfg['containers']:
            matching = [p for p in pods if p['metadata']['name'] == entry['pod']]
            if len(matching) != 1:
                raise ValueError('observer lacks frozen Pod')
            pod = matching[0]
            if pod['metadata']['uid'] != entry['uid'] or pod['spec']['nodeName'] != entry['node'] or pod['status']['podIP'] != entry['pod_ip']:
                raise ValueError('observer Pod identity differs')
            if pod['status']['phase'] != 'Running' and not (entry['role'] == 'load' and pod['status']['phase'] == 'Succeeded'):
                raise ValueError('observer Pod lifecycle failed')
            containers = [c for c in pod['status']['containerStatuses'] if c['name'] == entry['container']]
            if len(containers) != 1 or containers[0]['containerID'] != entry['container_id'] or containers[0]['imageID'] != entry['image_id'] or containers[0]['restartCount'] != 0:
                raise ValueError('observer container identity/restart differs')
            if entry['role'] != 'load' and not containers[0]['ready']:
                raise ValueError('observer service container unready')
    if baseline and baseline['job']['metadata']['name'] != cfg['run']:
        raise ValueError('observer Job differs from run')
    if not times or times[0] > options.start or times[-1] < options.end or any(b <= a or b - a > GAP for a, b in zip(times, times[1:])):
        raise ValueError('observer does not cover full run with fixed identities')


def audit(cfg, options):
    receipt = json.loads((options.output / 'receipt.json').read_text())
    if receipt.get('state') != 'collected':
        raise ValueError('Prometheus evidence pending retrieval, not passed')
    if receipt['freeze_sha256'] != sha(options.config.read_bytes()) or receipt['phase'] != options.phase or receipt['start'] != options.start or receipt['end'] != options.end:
        raise ValueError('collection does not match frozen audit window')
    series = merge_raw(options.output, receipt)
    source = cfg['prometheus']
    source_series = [(labels, values) for labels, values in series if labels.get('namespace') == source['namespace'] and labels.get('pod') == source['pod']]
    if not source_series or any(labels.get('uid') != source['uid'] for labels, _ in source_series):
        raise ValueError('Prometheus source identity cannot be verified')
    for labels, _ in series:
        if labels.get('namespace', cfg['namespace']) != cfg['namespace'] and not (labels.get('namespace') == source['namespace'] and labels.get('pod') == source['pod']):
            raise ValueError('unexpected namespace')
    verify_lifecycle(cfg, options)
    results = {}
    for entry in cfg['containers']:
        result = {'resources': audit_container(entry, series, options)}
        if entry['role'] == 'weir':
            result['application'] = audit_application(entry, series, options)
        results[entry['uid']] = result
    result = {'state': 'passed', 'phase': options.phase, 'run': cfg['run'], 'freeze_sha256': receipt['freeze_sha256'],
              'start': options.start, 'end': options.end, 'containers': results,
              'scope': 'Prometheus evidence only; load, observer and independent trend review must also pass'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'audit'))
    parser.add_argument('--phase', choices=('preflight', 'postrun'), required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--start', type=timestamp, required=True)
    parser.add_argument('--end', type=timestamp, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--load-report', type=Path)
    parser.add_argument('--observations', type=Path)
    parser.add_argument('--observer-status', type=Path)
    options = parser.parse_args()
    cfg, raw = read_config(options.config)
    if not 0 < options.end - options.start <= 25 * 3600:
        parser.error('window must be positive and at most 25 hours')
    if options.phase == 'preflight' and (options.end - options.start < 60 or abs(time.time() - options.end) > SKEW):
        parser.error('preflight needs at least 60 seconds of recent raw samples')
    if options.command == 'collect' and not collect(cfg, options):
        print('Prometheus retrieval pending; original request receipt preserved, load not cancelled')
        return 2
    try:
        result = audit(cfg, options)
        if options.phase == 'preflight' and time.time() - options.end > AGE:
            raise ValueError('preflight expired before publication')
    except (ValueError, KeyError, TypeError) as exc:
        result = {'state': 'failed', 'run': cfg['run'], 'error': str(exc)}
        save_json(options.output / 'audit.json', result)
        print(json.dumps(result))
        return 1
    save_json(options.output / 'audit.json', result)
    if options.phase == 'preflight':
        load = next(e for e in cfg['containers'] if e['role'] == 'load')
        marker = '\n'.join((cfg['run'], cfg['run_config_sha256'], load['uid'], sha(raw), str(int(time.time())))) + '\n'
        save(options.output / 'ready', marker.encode())
    print(json.dumps({'state': result['state'], 'phase': options.phase, 'run': cfg['run']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
