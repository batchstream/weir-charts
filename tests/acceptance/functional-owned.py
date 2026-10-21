#!/usr/bin/env python3
"""Prepare, or explicitly execute, one owned ARM EKS functional acceptance run."""
import argparse
import collections
import fcntl
import hashlib
import json
import re
import secrets
import subprocess
import time
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = 'arn:aws:eks:us-west-1:956540890581:cluster/data-team'
OWNER_LABEL = 'weir.batchstream.io/owner'
MONGO = 'mongo@sha256:d0d926f94df099bff534b7ee5b5986458131a22489dfff8664509af0c1e2ca9c'
SEARCH = 'docker.elastic.co/elasticsearch/elasticsearch@sha256:e98f9c3b09beb2fbb9eaf667d602df3f0e00bd3644138b8458dc17ba1a675595'
DIGEST = r'sha256:[0-9a-f]{64}'
EMPTY = {}
CHECKS = ('missing_read', 'put', 'lua', 'typed_read', 'lua_rejected',
          'typed_read_after_rejection', 'native_count', 'cancel_admission', 'independent_oracle')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), 'explicit regular file required: ' + str(path))
    require(not any(part.lower().startswith(('.env', 'credentials')) or part.lower() in ('.ssh', '.kube', 'secrets') or
                    part.lower().endswith(('.pem', '.key')) for part in path.parts), 'private file forbidden')
    return path


def load_json(path):
    data = json.loads(regular(path).read_text())
    return data


def digest(raw):
    value = 'sha256:' + hashlib.sha256(raw).hexdigest()
    return value


def image_proof(proof):
    """Validate local registry bytes, not a caller-written source/digest assertion."""
    index_raw = regular(proof['index']).read_bytes()
    manifest_raw = regular(proof['manifest']).read_bytes()
    config_raw = regular(proof['config']).read_bytes()
    index, manifest, config = (json.loads(raw) for raw in (index_raw, manifest_raw, config_raw))
    require(re.fullmatch(r'[^\s@]+@' + DIGEST, proof['image']), 'immutable image required')
    require(proof['image'].split('@')[1] == digest(index_raw), 'OCI index checksum mismatch')
    children = [item for item in index['manifests'] if item.get('platform') ==
                {'architecture': 'arm64', 'os': 'linux'} or
                (item.get('platform', EMPTY).get('architecture') == 'arm64' and
                 item.get('platform', EMPTY).get('os') == 'linux' and
                 item.get('platform', EMPTY).get('variant', 'v8') == 'v8')]
    require(len(children) == 1 and children[0]['digest'] == digest(manifest_raw), 'unique ARM manifest required')
    require(manifest['config']['digest'] == digest(config_raw), 'OCI config checksum mismatch')
    require(config.get('architecture') == 'arm64' and config.get('os') == 'linux', 'native Linux ARM image required')
    result = {'image': proof['image'], 'platform_digest': digest(manifest_raw), 'config_digest': digest(config_raw),
              'source': config.get('config', EMPTY).get('Labels', EMPTY).get('org.opencontainers.image.revision')}
    return result


def candidate_proof(cfg):
    """Check a retained verified-distribution ZIP; never extract or execute members."""
    bundle = regular(cfg['distribution_bundle'])
    require(re.fullmatch('[0-9a-f]{64}', cfg['distribution_sha256']), 'trusted distribution SHA256 required')
    require(hashlib.sha256(bundle.read_bytes()).hexdigest() == cfg['distribution_sha256'], 'distribution ZIP checksum mismatch')
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and len(names) <= 32, 'duplicate/oversized distribution inventory')
        require(all(not n.startswith('/') and '..' not in Path(n).parts for n in names), 'unsafe distribution member')
        require(sum(info.file_size for info in archive.infolist()) <= 128 * 1024 ** 2, 'distribution members oversized')
        checksums = {}
        for line in archive.read('SHA256SUMS').decode().splitlines():
            checksum, name = line.split('  ', 1)
            require(re.fullmatch('[0-9a-f]{64}', checksum) and name not in checksums, 'invalid member checksum inventory')
            checksums[name] = checksum
        require(set(checksums) == set(names) - {'SHA256SUMS'}, 'incomplete member checksum inventory')
        for name, checksum in checksums.items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == checksum, 'distribution member checksum mismatch')
        delivery = json.loads(archive.read('delivery.json'))
        receipt = json.loads(archive.read('weir-first/receipt.json'))
        image = delivery['images']['weir']
        require(delivery['source'] == receipt['source'] == cfg['server_source'], 'distribution source mismatch')
        require(image['oci'] == receipt['oci'] and image['binaries'] == receipt['binaries'], 'distribution receipt mismatch')
        require(image['oci']['images']['arm64']['binary_sha256'] == receipt['binaries']['linux-arm64'], 'ARM binary receipt mismatch')
        for name, checksum in receipt['archives'].items():
            require(checksums.get('weir-first/' + name) == checksum, 'binary archive receipt mismatch')
        require(cfg['candidate']['image'] == 'ghcr.io/batchstream/weir@' + image['oci']['index'] and
                cfg['candidate']['platform_digest'] == image['oci']['images']['arm64']['manifest'], 'candidate mapping mismatch')
    result = {'image': cfg['candidate']['image'], 'platform_digest': cfg['candidate']['platform_digest'],
              'source': cfg['server_source']}
    return result


def runner_proof(cfg):
    raw = regular(cfg['runner_build']).read_bytes()
    require(re.fullmatch('[0-9a-f]{64}', cfg['runner_build_sha256']) and
            hashlib.sha256(raw).hexdigest() == cfg['runner_build_sha256'], 'runner build receipt checksum mismatch')
    receipt = json.loads(raw)
    require(receipt['source_revision'] == cfg['runner_source'] and receipt['sha256'] == cfg['runner_sha256'] and
            receipt['target'] == 'linux/arm64' and re.fullmatch('[0-9a-f]{64}', receipt['source_tree_sha256']),
            'runner build receipt identity mismatch')
    return receipt


def validate_config(cfg):
    require(re.fullmatch('[0-9a-f]{40}', cfg['server_source']), 'full candidate source required')
    require(re.fullmatch('[0-9a-f]{40}', cfg['runner_source']), 'full runner source required')
    require(re.fullmatch('[0-9a-f]{64}', cfg['runner_sha256']), 'runner SHA256 required')
    binary = regular(cfg['runner_binary']).read_bytes()
    require(hashlib.sha256(binary).hexdigest() == cfg['runner_sha256'], 'runner binary checksum mismatch')
    require(len(binary) >= 20 and binary[:6] == b'\x7fELF\x02\x01' and binary[18:20] == b'\xb7\x00',
            'runner must be an AArch64 little-endian ELF64 binary')
    runner_proof(cfg)
    proofs = {name: image_proof(cfg[name]) for name in ('mongo', 'search', 'carrier')}
    proofs['candidate'] = candidate_proof(cfg)
    require(proofs['mongo']['image'] == MONGO and proofs['search']['image'] == SEARCH, 'fixture index pin mismatch')
    # Explicit carrier input, restricted to the same proven native fixture image.
    require(proofs['carrier']['image'] == SEARCH, 'carrier must use the pinned native Search image')
    proofs['runner'] = proofs.pop('carrier')
    return proofs


def bounds(cpu, memory):
    values = {'requests': {'cpu': cpu, 'memory': memory}, 'limits': {'cpu': cpu, 'memory': memory}}
    return values


def metadata(name, run):
    value = {'name': name, 'namespace': run['namespace'], 'labels': {OWNER_LABEL: run['owner']}}
    return value


def pod_spec(container):
    value = {'automountServiceAccountToken': False, 'enableServiceLinks': False,
             'nodeSelector': {'type': 'sts-arm', 'kubernetes.io/arch': 'arm64'},
             'restartPolicy': 'Never', 'terminationGracePeriodSeconds': 10,
             'securityContext': {'seccompProfile': {'type': 'RuntimeDefault'}},
             'containers': [container], 'volumes': [{'name': 'data', 'emptyDir': {'sizeLimit': '2Gi'}},
                                                  {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}
    return value


def fixture(options):
    name, run, proofs = options['name'], options['run'], options['proofs']
    mongo = name == 'mongo'
    container = {'name': name, 'image': proofs[name]['image'],
                 'resources': bounds('500m' if mongo else '1', '1Gi' if mongo else '1536Mi'),
                 'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                 'volumeMounts': [{'name': 'data', 'mountPath': '/data/db' if mongo else '/usr/share/elasticsearch/data'},
                                  {'name': 'tmp', 'mountPath': '/tmp'}]}
    if mongo:
        # Bypass the root/chown entrypoint; fsGroup owns only this new emptyDir.
        container.update(command=['mongod'], args=['--replSet', 'rs0', '--bind_ip_all', '--wiredTigerCacheSizeGB', '0.25'])
        container['readinessProbe'] = {'exec': {'command': ['mongosh', '--quiet', '--eval', 'quit(db.adminCommand({ping:1}).ok ? 0 : 1)']}, 'periodSeconds': 5, 'timeoutSeconds': 3}
    else:
        container['env'] = [{'name': key, 'value': value} for key, value in
                            {'discovery.type': 'single-node', 'xpack.security.enabled': 'false',
                             'ES_JAVA_OPTS': '-Xms512m -Xmx512m', 'node.store.allow_mmap': 'false'}.items()]
        container['readinessProbe'] = {'httpGet': {'path': '/_cluster/health', 'port': 9200}, 'periodSeconds': 5, 'timeoutSeconds': 3}
    spec = pod_spec(container)
    spec['securityContext'].update(runAsNonRoot=True, runAsUser=999 if mongo else 1000, runAsGroup=999 if mongo else 1000, fsGroup=999 if mongo else 1000)
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': metadata(name, run), 'spec': spec}
    pod['metadata']['labels']['app'] = name
    port = 27017 if mongo else 9200
    service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(name, run),
               'spec': {'type': 'ClusterIP', 'selector': {'app': name, OWNER_LABEL: run['owner']},
                        'ports': [{'port': port, 'targetPort': port}]}}
    result = [service, pod]
    return result


RUN_SCRIPT = '''set -euC
for attempt in $(seq 1 300); do
  if test -f /work/ready; then break; fi
  sleep 1
done
test -f /work/ready
printf '%s  /work/program\\n' "$BINARY_SHA256" | sha256sum -c -
test -f /work/config.json && test ! -L /work/config.json
/work/program -version > /work/build.json
/work/program -input /work/config.json > /work/report.json 2> /work/runner.stderr &
child=$!
set +e
wait "$child"
status=$?
set -e
printf '%s\\n' "$status" > /work/exit.txt
# Keep this exact Pod/emptyDir alive for bounded host export, including failures.
for attempt in $(seq 1 180); do
  if test -f /work/exported; then exit "$status"; fi
  sleep 1
done
exit 124
'''


def runner_job(options):
    run, cfg, proofs = options['run'], options['cfg'], options['proofs']
    container = {'name': 'runner', 'image': proofs['runner']['image'],
                 'command': ['/bin/bash', '-c', RUN_SCRIPT],
                 'env': [{'name': 'BINARY_SHA256', 'value': cfg['runner_sha256']}],
                 'resources': bounds('250m', '256Mi'),
                 'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                     'capabilities': {'drop': ['ALL']}},
                 'volumeMounts': [{'name': 'data', 'mountPath': '/work'}, {'name': 'tmp', 'mountPath': '/tmp'}]}
    spec = pod_spec(container)
    spec['securityContext'].update(runAsNonRoot=True, runAsUser=1000, runAsGroup=1000, fsGroup=1000)
    spec['volumes'][0]['emptyDir']['sizeLimit'] = '128Mi'
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata('runner', run),
           'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 660,
                    'template': {'metadata': {'labels': {OWNER_LABEL: run['owner'], 'app': 'runner'}}, 'spec': spec}}}
    return job


def chart_values(options):
    run, proofs = options['run'], options['proofs']
    namespace = run['namespace']
    values = {'fullnameOverride': 'weir', 'replicaCount': 1, 'image': {'digest': proofs['candidate']['platform_digest']},
              'resources': bounds('1', '1Gi'), 'nodeSelector': {'type': 'sts-arm', 'kubernetes.io/arch': 'arm64'},
              'networkPolicy': {'enabled': False}, 'mesh': {'enabled': False},
              'podAnnotations': {'weir.batchstream.io/acceptance-source': proofs['candidate']['source']},
              'stores': [{'name': 'mongo', 'backend': {'mongodb': {'uri': 'mongodb://mongo.' + namespace + '.svc.cluster.local:27017/?replicaSet=rs0'}}},
                         {'name': 'search', 'backend': {'search': {'url': 'http://search.' + namespace + '.svc.cluster.local:9200'}}}]}
    return values


def documents(options):
    run = options['run']
    quota = {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': metadata('functional', run),
             'spec': {'hard': {'requests.cpu': '4', 'limits.cpu': '4', 'requests.memory': '6Gi',
                               'limits.memory': '6Gi', 'pods': '10', 'persistentvolumeclaims': '0',
                               'services.loadbalancers': '0', 'services.nodeports': '0'}}}
    limit = {'apiVersion': 'v1', 'kind': 'LimitRange', 'metadata': metadata('functional', run),
             'spec': {'limits': [{'type': 'Container', 'default': {'cpu': '1', 'memory': '1536Mi'},
                                  'defaultRequest': {'cpu': '250m', 'memory': '256Mi'},
                                  'max': {'cpu': '1', 'memory': '1536Mi'}}]}}
    items = [quota, limit]
    for name in ('mongo', 'search'):
        opts = dict(options, name=name)
        items.extend(fixture(opts))
    items.append(runner_job(options))
    return items


def command(argv, **kwargs):
    result = subprocess.run(argv, check=True, capture_output=True, timeout=kwargs.pop('timeout', 45), **kwargs)
    return result.stdout


class Cluster:
    def __init__(self, run):
        self.run = run
        self.uid = None

    def kubectl(self, args, **kwargs):
        argv = ['kubectl', '--context', CONTEXT, '--request-timeout=30s', *args]
        return command(argv, **kwargs)

    def get(self, args):
        result = json.loads(self.kubectl(['get', *args, '-o', 'json']))
        return result

    def owned(self):
        namespace = self.get(['namespace', self.run['namespace']])
        require(namespace['metadata']['uid'] == self.uid and
                namespace['metadata']['labels'].get(OWNER_LABEL) == self.run['owner'], 'namespace ownership changed')

    def scoped(self, args, **kwargs):
        self.owned()
        return self.kubectl(['-n', self.run['namespace'], *args], **kwargs)

    def create(self, item):
        raw = json.dumps(item).encode()
        return self.scoped(['create', '-f', '-'], input=raw)

    def exec(self, options):
        args = ['exec', '-i', options['pod'], '--', *options['argv']]
        deadline = options.get('handoff_deadline')
        if deadline is None:
            return self.scoped(args, input=options.get('input'), timeout=options.get('timeout', 45))
        require(time.monotonic() < deadline, 'runner handoff deadline exceeded')
        # The 255-second handoff window reserves the full existing 45-second
        # ownership rail within the shell's unchanged 300-second ready gate.
        self.owned()
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'runner handoff deadline exceeded')
        budget = min(options.get('timeout', 45), remaining)
        request_timeout = '--request-timeout=' + str(budget) + 's'
        raw = self.kubectl([request_timeout, '-n', self.run['namespace'], *args],
                           input=options.get('input'), timeout=budget)
        require(time.monotonic() < deadline, 'runner handoff deadline exceeded')
        return raw

    def cleanup(self):
        self.owned()
        # API UID precondition closes the get/delete race; no name-only delete.
        deletion = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': self.uid}}
        self.kubectl(['delete', '--raw', '/api/v1/namespaces/' + self.run['namespace'], '-f', '-'], input=json.dumps(deletion).encode())
        self.kubectl(['wait', '--for=delete', 'namespace/' + self.run['namespace'], '--timeout=180s'], timeout=190)


def quantity(value, resource):
    units = {'m': .001, 'Ki': 1024, 'Mi': 1024 ** 2, 'Gi': 1024 ** 3, 'Ti': 1024 ** 4, 'n': 1e-9, 'u': 1e-6}
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)', str(value))
    require(match is not None and (not match[2] or match[2] in units), 'unsupported ' + resource + ' quantity')
    amount = float(match[1]) * units.get(match[2], 1)
    return amount


def headroom(cluster):
    """Read requests and usage immediately before creation; never a reservation."""
    current = cluster.kubectl(['config', 'current-context']).decode().strip()
    require(current == CONTEXT, 'current context must match approved EKS context')
    nodes = cluster.get(['nodes', '-l', 'type=sts-arm'])['items']
    pods = cluster.get(['pods', '-A'])['items']
    usage = json.loads(cluster.kubectl(['get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes']))['items']
    used = {item['metadata']['name']: item['usage'] for item in usage}
    eligible = []
    for node in nodes:
        name = node['metadata']['name']
        hostname = node['metadata']['labels'].get('kubernetes.io/hostname')
        if not hostname:
            continue
        if node['metadata']['labels'].get('kubernetes.io/arch') != 'arm64' or node['spec'].get('unschedulable'):
            continue
        if any(t.get('effect') in ('NoSchedule', 'NoExecute') for t in node['spec'].get('taints', [])):
            continue
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            continue
        allocated = {'cpu': 0, 'memory': 0, 'pods': 0}
        for pod in pods:
            if pod['spec'].get('nodeName') != name or pod.get('status', EMPTY).get('phase') in ('Succeeded', 'Failed'):
                continue
            allocated['pods'] += 1
            for resource in ('cpu', 'memory'):
                regular_total = sum(quantity(c.get('resources', EMPTY).get('requests', EMPTY).get(resource, '0'), resource) for c in pod['spec']['containers'])
                # Conservative: charge all init requests, including restartable init containers.
                init_total = sum(quantity(c.get('resources', EMPTY).get('requests', EMPTY).get(resource, '0'), resource) for c in pod['spec'].get('initContainers', []))
                allocated[resource] += regular_total + init_total + quantity(pod['spec'].get('overhead', EMPTY).get(resource, '0'), resource)
        allocatable = node['status']['allocatable']
        if name not in used:
            continue
        enough = all(quantity(allocatable[key], key) - max(allocated[key], quantity(used[name].get(key, '0'), key)) >= wanted
                     for key, wanted in (('cpu', 4), ('memory', 6 * 1024 ** 3)))
        if enough and int(allocatable['pods']) - allocated['pods'] >= 10:
            record = {'node': name, 'hostname': hostname, 'uid': node['metadata']['uid'], 'allocatable': allocatable,
                      'requested': allocated, 'usage': used[name]}
            eligible.append(record)
    require(eligible, 'no single native ARM node has initial full-quota headroom')
    # Pin every Pod to one checked node, in addition to mandatory approved type/arch.
    return eligible[0]


def input_config(options):
    run, proofs, identity = options['run'], options['proofs'], options['identity']
    suffix = '.' + run['namespace'] + '.svc.cluster.local'
    config = {'expected_arch': 'arm64', 'allow_remote': True, 'server_source': proofs['candidate']['source'],
              'server_oci': proofs['candidate']['platform_digest'], 'namespace': run['namespace'],
              'namespace_uid': identity['namespace_uid'], 'pod_name': identity['pod_name'], 'pod_uid': identity['pod_uid'],
              'run_owner': run['owner'], 'application_seeds': ['weir' + suffix + ':7447'],
              'mongo_uri': 'mongodb://mongo' + suffix + ':27017/?replicaSet=rs0',
              'search_url': 'http://search' + suffix + ':9200', 'mongo_store': 'mongo', 'search_store': 'search',
              'owned_dataset_prefix': 'weiraccept_' + run['owner'] + '_'}
    return config


def validate_build(build, cfg):
    require(build.get('target') == 'linux/arm64' and build.get('injected_source') == cfg['runner_source'] and
            build.get('sdk') not in (None, '', 'unknown', '(devel)') and
            build.get('protocol') not in (None, '', 'unknown', '(devel)'), 'runner build identity unverified')
    if build.get('vcs_revision') == cfg['runner_source']:
        require(build.get('vcs_modified') is False, 'runner VCS is not clean')
    else:
        # Frozen -buildvcs=false builds retain their independently checksummed
        # build receipt; never relabel unknown runtime VCS as verified VCS.
        require(build.get('vcs_revision') == 'unknown' and 'vcs_modified' not in build, 'runner VCS mismatch')
        runner_proof(cfg)


def validate_report(options):
    report, config, cfg = options['report'], options['input'], options['cfg']
    require(report.get('schema') == 1 and report.get('mode') == 'functional' and report.get('passed') is True and
            report.get('error_code') == 0 and report.get('seed_count') == 1, 'functional report did not pass')
    expected = {key: config[key] for key in ('server_source', 'server_oci', 'namespace', 'namespace_uid', 'pod_name', 'pod_uid', 'run_owner')}
    require(report.get('caller_asserted_provenance') == expected, 'report identity mismatch')
    validate_build(report.get('runner_build', EMPTY), cfg)
    require(re.fullmatch(re.escape(config['owned_dataset_prefix']) + '[0-9a-f]{16}', report.get('owned_dataset_base', '')), 'dataset identity mismatch')
    expected_checks = [('resolve_open', '', 1), ('sdk_close', '', 1)]
    for backend in ('mongo', 'search'):
        expected_checks.extend((kind, backend, 0) for kind in ('prepare', 'driver_close', 'cleanup_verified'))
        expected_checks.extend((kind, backend, 1) for kind in CHECKS)
    checks = report.get('checks', [])
    actual = [(c.get('kind'), c.get('backend', ''), c.get('seed', 0)) for c in checks]
    require(collections.Counter(actual) == collections.Counter(expected_checks), 'missing, duplicate or unexpected checks')
    for check in checks:
        code = 3 if check['kind'] == 'cancel_admission' else 0
        require(check.get('passed') is True and check.get('error_code') == code and not check.get('mutation_unknown'), 'failed or unknown check')


def runtime_image(options):
    proof, status = options['proof'], options['status']
    require(options['selected_image'] == options['expected_image'], 'selected Pod image mismatch')
    image_id = status.get('imageID', '')
    match = re.fullmatch(r'(?:[^\s@]+@)?(' + DIGEST + ')', image_id)
    require(match is not None, 'invalid runtime imageID')
    runtime_digest = match[1]
    manifest_match = runtime_digest == proof['platform_digest']
    # Some runtimes report the pinned index in imageID and the resolved config
    # in image. Only the independently verified ARM index/manifest/config chain
    # authorizes that pair; an index ID by itself never establishes platform.
    index_match = (options['selected_image'] == proof['image'] and
                   runtime_digest == proof['image'].split('@')[1] and
                   proof.get('config_digest') is not None and status.get('image') == proof['config_digest'])
    require(manifest_match or index_match, 'deployed platform imageID mismatch')


def observe(options):
    cluster, proofs = options['cluster'], options['proofs']
    cluster.owned()
    listing = cluster.get(['pods', '-n', cluster.run['namespace']])['items']
    records = {}
    for role in ('weir', 'mongo', 'search', 'runner'):
        pods = [p for p in listing if (p['metadata'].get('labels', EMPTY).get('app') == role or
                                     (role == 'weir' and p['metadata'].get('labels', EMPTY).get('app.kubernetes.io/instance') == 'functional'))]
        require(len(pods) == 1, 'expected exactly one ' + role + ' Pod')
        pod = pods[0]
        require(pod['metadata'].get('labels', EMPTY).get(OWNER_LABEL) == cluster.run['owner'], 'Pod owner label mismatch')
        selector = pod['spec']['nodeSelector']
        require(selector.get('type') == 'sts-arm' and selector.get('kubernetes.io/arch') == 'arm64' and
                pod['spec'].get('nodeName') == options['node'], 'Pod scheduled outside checked ARM node')
        statuses = pod.get('status', EMPTY).get('containerStatuses', [])
        require(len(statuses) == 1 and statuses[0].get('ready') and statuses[0].get('restartCount') == 0, 'Pod not ready or restarted')
        proof = proofs['candidate' if role == 'weir' else role]
        selected = pod['spec']['containers']
        require(len(selected) == 1, 'expected exactly one selected container')
        expected_image = proof['image']
        if role == 'weir':
            expected_image = proof['image'].split('@')[0] + '@' + proof['platform_digest']
        image_options = {'proof': proof, 'status': statuses[0], 'selected_image': selected[0]['image'],
                         'expected_image': expected_image}
        runtime_image(image_options)
        records[role] = {'name': pod['metadata']['name'], 'uid': pod['metadata']['uid'], 'image_id': statuses[0].get('imageID'),
                         'runtime_image': statuses[0].get('image'), 'selected_image': selected[0]['image'],
                         'node': pod['spec']['nodeName'], 'container_id': statuses[0].get('containerID')}
    baseline = options.get('baseline')
    if baseline is not None:
        require(records == baseline, 'Pod or container identity changed')
    return records


def write_json(path, value):
    with path.open('x') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def upload_runner(options):
    cluster, pod, cfg = options['cluster'], options['pod'], options['cfg']
    deadline = options['handoff_deadline']
    def handoff(opts):
        started = time.monotonic()
        require(started < deadline, 'runner handoff deadline exceeded')
        opts['handoff_deadline'] = deadline
        opts['timeout'] = min(opts.get('timeout', 45), deadline - started)
        try:
            raw = cluster.exec(opts)
        except subprocess.TimeoutExpired as error:
            destination = options.get('destination')
            if destination is not None:
                def bounded(value):
                    if isinstance(value, bytes):
                        return value[:4096].decode(errors='replace')
                    return (value or '')[:4096]
                failure = {'elapsed_seconds': time.monotonic() - started,
                           'input_length': len(opts.get('input', b'')), 'actual_transferred_bytes': 'unknown',
                           'stdout': bounded(error.stdout), 'stderr': bounded(error.stderr)}
                write_json(destination / 'handoff-timeout.json', failure)
            raise
        require(time.monotonic() < deadline, 'runner handoff deadline exceeded')
        return raw
    # Verify the bytes actually uploaded, not a previous read of this local path.
    for name, raw in (('program', regular(cfg['runner_binary']).read_bytes()),
                      ('config.json', json.dumps(options['input']).encode())):
        opts = {'pod': pod, 'argv': ['/bin/bash', '-c', 'set -euC; umask 077; cat > /work/' + name], 'input': raw,
                'timeout': 120 if name == 'program' else 45}
        handoff(opts)
    opts = {'pod': pod, 'argv': ['sha256sum', '/work/program']}
    checksum = handoff(opts).decode().split()
    require(checksum == [cfg['runner_sha256'], '/work/program'], 'uploaded runner checksum mismatch')
    opts = {'pod': pod, 'argv': ['/bin/bash', '-c', 'chmod 700 /work/program; uname -m']}
    require(handoff(opts).decode().strip() == 'aarch64', 'carrier kernel architecture mismatch')
    # No invocation of the uploaded program, even -version, precedes its hash gate.
    opts = {'pod': pod, 'argv': ['/work/program', '-version']}
    build_raw = handoff(opts)
    return build_raw


def execute(options):
    run, cfg, proofs, destination = options['run'], options['cfg'], options['proofs'], options['destination']
    cluster = Cluster(run)
    try:
        checked = headroom(cluster)
        write_json(destination / 'headroom.json', checked)
        namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': run['namespace'], 'labels': {OWNER_LABEL: run['owner']}}}
        # create, never apply/adopt an existing namespace.
        created = json.loads(cluster.kubectl(['create', '-f', '-', '-o', 'json'], input=json.dumps(namespace).encode()))
        cluster.uid = created['metadata']['uid']
        write_json(destination / 'namespace.json', created)
        for item in options['items']:
            if item['kind'] == 'Job':
                item['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] = checked['hostname']
            elif item['kind'] == 'Pod':
                item['spec']['nodeSelector']['kubernetes.io/hostname'] = checked['hostname']
            elif item['kind'] == 'Deployment':
                item['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] = checked['hostname']
            if item not in options['chart'] and item['kind'] != 'Job':
                cluster.create(item)
        cluster.scoped(['wait', '--for=condition=Ready', 'pod/mongo', 'pod/search', '--timeout=180s'], timeout=190)
        mongo_init = 'rs.initiate({_id:"rs0",members:[{_id:0,host:"mongo.' + run['namespace'] + '.svc.cluster.local:27017"}]}); for(let i=0;i<60;i++){if(db.hello().isWritablePrimary)quit(0);sleep(1000)};quit(1)'
        opts = {'pod': 'mongo', 'argv': ['mongosh', '--quiet', '--eval', mongo_init], 'timeout': 80}
        cluster.exec(opts)
        for item in options['chart']:
            cluster.create(item)
        cluster.scoped(['wait', '--for=condition=Available', 'deployment/weir', '--timeout=180s'], timeout=190)
        handoff_deadline = time.monotonic() + 255
        for item in options['items']:
            if item['kind'] == 'Job':
                cluster.create(item)
        runner_deadline = time.monotonic() + 30
        while True:
            cluster.owned()
            runner_pods = cluster.get(['pods', '-n', run['namespace'], '-l', 'app=runner'])['items']
            if runner_pods:
                require(len(runner_pods) == 1, 'multiple runner Pods')
                runner_name = runner_pods[0]['metadata']['name']
                break
            require(time.monotonic() < runner_deadline, 'runner Pod creation deadline exceeded')
            time.sleep(1)
        cluster.scoped(['wait', '--for=condition=Ready', 'pod/' + runner_name, '--timeout=180s'], timeout=190)
        observation = {'cluster': cluster, 'proofs': proofs, 'node': checked['node']}
        baseline = observe(observation)
        write_json(destination / 'identities-before.json', baseline)
        identity = {'namespace_uid': cluster.uid, 'pod_name': baseline['weir']['name'], 'pod_uid': baseline['weir']['uid']}
        identity_options = dict(options, identity=identity)
        runner_input = input_config(identity_options)
        write_json(destination / 'runner-input.json', runner_input)
        pod = baseline['runner']['name']
        handoff = {'cluster': cluster, 'pod': pod, 'cfg': cfg, 'input': runner_input,
                   'handoff_deadline': handoff_deadline, 'destination': destination}
        build_raw = upload_runner(handoff)
        require(len(build_raw) <= 65536, 'runner build report oversized')
        build_report = json.loads(build_raw)
        require(build_report.get('mode') == 'build' and build_report.get('passed') is True, 'runner build preflight failed')
        validate_build(build_report.get('runner_build', EMPTY), cfg)
        (destination / 'build-preflight.json').write_bytes(build_raw)
        # Recheck every actual identity immediately before starting business work.
        observation['baseline'] = baseline
        observe(observation)
        opts = {'pod': pod, 'argv': ['/bin/bash', '-c', 'set -euC; : > /work/ready'],
                'handoff_deadline': handoff_deadline}
        cluster.exec(opts)
        deadline = time.monotonic() + 150
        while True:
            opts = {'pod': pod, 'argv': ['/bin/bash', '-c', 'if test -f /work/exit.txt; then cat /work/exit.txt; fi']}
            status = cluster.exec(opts).decode().strip()
            if status:
                break
            require(time.monotonic() < deadline, 'runner completion deadline exceeded')
            time.sleep(2)
        for filename in ('report.json', 'build.json', 'runner.stderr'):
            opts = {'pod': pod, 'argv': ['cat', '/work/' + filename]}
            raw = cluster.exec(opts)
            require(len(raw) <= 65536, 'runner output oversized')
            (destination / filename).write_bytes(raw)
        require(status == '0', 'runner exited unsuccessfully')
        report = load_json(destination / 'report.json')
        report_options = {'report': report, 'input': runner_input, 'cfg': cfg}
        validate_report(report_options)
        after = observe(observation)
        write_json(destination / 'identities-after.json', after)
        opts = {'pod': pod, 'argv': ['/bin/bash', '-c', 'set -euC; : > /work/exported']}
        cluster.exec(opts)
        cluster.scoped(['wait', '--for=condition=complete', 'job/runner', '--timeout=30s'], timeout=40)
    finally:
        if cluster.uid is not None:
            # Export diagnostics before deleting; never fetch Secrets or arbitrary config.
            try:
                cluster.owned()
                write_json(destination / 'pods-final.json', cluster.get(['pods', '-n', run['namespace']]))
                pods = cluster.get(['pods', '-n', run['namespace']])['items']
                for pod in pods:
                    name = pod['metadata']['name']
                    try:
                        raw = cluster.scoped(['logs', name, '--tail=200', '--limit-bytes=65536'])
                        (destination / (name + '.log')).write_bytes(raw)
                    except subprocess.SubprocessError:
                        pass
            finally:
                cluster.cleanup()
                cleanup = {'namespace': run['namespace'], 'uid': cluster.uid, 'deletion_verified': True}
                write_json(destination / 'cleanup.json', cleanup)
    result = {'passed': True, 'namespace_uid': cluster.uid, 'scope': 'single-node functional only', 'namespace_deletion_verified': True}
    write_json(destination / 'functional-result.json', result)


def prepare(options):
    cfg, destination = options['cfg'], options['destination']
    proofs = validate_config(cfg)
    owner = 'run' + secrets.token_hex(6)
    run = {'owner': owner, 'namespace': 'weir-accept-' + owner}
    destination.mkdir(parents=True, exist_ok=False)
    context = {'run': run, 'cfg': cfg, 'proofs': proofs, 'destination': destination}
    values = chart_values(context)
    write_json(destination / 'values.json', values)
    raw = command(['helm', 'template', 'functional', str(ROOT / 'charts/weir'), '--namespace', run['namespace'],
                   '-f', str(destination / 'values.json')])
    chart = list(yaml.safe_load_all(raw))
    for item in chart:
        require(item['kind'] in ('Deployment', 'Service', 'ConfigMap'), 'unexpected chart resource')
        item['metadata']['namespace'] = run['namespace']
        labels = item['metadata'].get('labels')
        if labels is None:
            labels = {}
            item['metadata']['labels'] = labels
        labels[OWNER_LABEL] = owner
        if item['kind'] == 'Deployment':
            item['spec']['template']['metadata']['labels'][OWNER_LABEL] = owner
    items = documents(context) + chart
    resources = {'apiVersion': 'v1', 'kind': 'List', 'items': items}
    write_json(destination / 'resources.json', resources)
    controls = {'run': run, 'proofs': proofs, 'runner_sha256': cfg['runner_sha256'],
                'runner_source': cfg['runner_source'], 'context': CONTEXT,
                'executed': False, 'scope': 'single-node functional only; namespace is not network isolation'}
    write_json(destination / 'controls.json', controls)
    context['items'] = items
    context['chart'] = chart
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--execute', action='store_true', help='explicitly create then remove a fresh owned EKS namespace')
    args = parser.parse_args()
    options = {'cfg': load_json(args.config), 'destination': args.destination.resolve()}
    prepared = prepare(options)
    if args.execute:
        lock_path = ROOT / '.testdata/functional-owned.lock'
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            execute(prepared)


if __name__ == '__main__':
    main()
