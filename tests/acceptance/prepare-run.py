#!/usr/bin/env python3
"""Render one owned namespace acceptance run; does not contact a cluster."""
import argparse
import hashlib
import json
import re
from pathlib import Path

CARRIER = 'docker.elastic.co/elasticsearch/elasticsearch@sha256:c2a3ed5f968be6d59c960aa0c60cfdaee667b6bc8211142021a41d0e85b43237'


def render(cfg):
    for key in ('namespace', 'run', 'owner', 'pvc', 'node', 'architecture', 'serverRevision', 'imageDigest', 'chartVersion', 'sdkRevision', 'runnerSHA256', 'observerSHA256', 'evidenceToolSHA256', 'prometheusURL', 'prometheusSourceUID'):
        if not cfg.get(key) or 'REPLACE' in cfg[key]:
            raise ValueError('set ' + key + ' before rendering')
    for key in ('namespace', 'run', 'pvc'):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?', cfg[key]):
            raise ValueError('invalid Kubernetes name: ' + key)
    for key in ('runnerSHA256', 'observerSHA256', 'evidenceToolSHA256'):
        if not re.fullmatch(r'[0-9a-f]{64}', cfg[key]):
            raise ValueError('invalid binary hash: ' + key)
    if cfg.get('resourceEvidenceSource') != 'prometheus':
        raise ValueError('explicit Prometheus evidence source required')
    config_sha = hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    run = cfg['run']
    if not run.startswith('weir-soak-'):
        raise ValueError('run must use the SDK-owned weir-soak- prefix')
    labels = {'weir.batchstream.io/owner': cfg['owner']}
    namespace = cfg['namespace']
    service_account = 'weir-soak-observer'
    metadata = {'name': service_account, 'namespace': namespace, 'labels': labels}
    resources = [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': metadata},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': metadata,
         'rules': [{'apiGroups': [group], 'resources': [resource], 'verbs': ['get', 'list']} for group, resource in (('', 'pods'), ('batch', 'jobs'))]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': metadata,
         'subjects': [{'kind': 'ServiceAccount', 'name': service_account, 'namespace': namespace}],
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': service_account}},
    ]
    report = '/results/' + run + '.jsonl'
    observation = '/results/' + run + '-observations.jsonl'
    exit_status = '/results/' + run + '-exit.json'
    targets = cfg.get('targets', [])
    if targets and cfg.get('address'):
        raise ValueError('set only one of targets or address')
    if targets and (len(targets) != 3 or len(set(targets)) != 3):
        raise ValueError('fixed three-Pod qualification needs three distinct targets')
    destination = ['-targets', ','.join(targets)] if targets else ['-address', cfg['address']]
    args = destination + ['-mongo-resource', cfg['mongoResource'], '-search-resource', cfg['searchResource'], '-run-id', run,
            '-duration', cfg['duration'], '-workers', '6', '-cycles-per-second', '5', '-max-p99', '500ms',
            '-server-revision', cfg['serverRevision'], '-image-digest', cfg['imageDigest'], '-chart-version', cfg['chartVersion'], '-sdk-revision', cfg['sdkRevision'],
            '-observer-status', observation + '.status.json', '-observer-heartbeat', observation + '.ready']
    load_script = '''set -uC
for attempt in $(seq 1 300); do
  if test -f /runner/ready && test -f "$HEARTBEAT" && test -f "$PROM_READY" && test -f "$PROM_FREEZE"; then break; fi
  sleep 1
done
if ! test -f /runner/ready || ! test -f "$HEARTBEAT" || ! test -f "$PROM_READY" || ! test -f "$PROM_FREEZE"; then echo "load startup deadline exceeded"; exit 1; fi
proof=()
while IFS= read -r item; do proof+=("$item"); done < "$PROM_READY"
if test "${#proof[@]}" -ne 5 || test "${proof[0]}" != "$RUN_ID" || test "${proof[1]}" != "$RUN_CONFIG_SHA256" || test "${proof[2]}" != "$POD_UID"; then echo "Prom preflight identity mismatch"; exit 1; fi
if ! [[ "${proof[4]}" =~ ^[0-9]{10}$ ]]; then echo "invalid Prom preflight time"; exit 1; fi
age=$(( $(date +%s) - ${proof[4]} ))
if test "$age" -lt 0 || test "$age" -gt 120; then echo "Prom preflight expired"; exit 1; fi
checksum=$(sha256sum "$PROM_FREEZE") || exit 1
if test "${proof[3]}" != "${checksum%% *}"; then echo "Prom freeze checksum mismatch"; exit 1; fi
printf '%s  /runner/program\\n' "$BINARY_SHA256" | sha256sum -c - || exit 1
/runner/program "$@" > "$REPORT" 2> "$REPORT.stderr"
run_status=$?
sync "$REPORT" "$REPORT.stderr" || exit 1
printf '{"exit_code":%s}\\n' "$run_status" > "$EXIT_STATUS.tmp" || exit 1
sync "$EXIT_STATUS.tmp" || exit 1
ln "$EXIT_STATUS.tmp" "$EXIT_STATUS" || exit 1
sync /results || exit 1
exit "$run_status"
'''
    observer_script = '''set -u
for attempt in $(seq 1 300); do
  if test -f /runner/ready; then
    printf '%s  /runner/program\\n' "$BINARY_SHA256" | sha256sum -c - || exit 1
    exec /runner/program "$@"
  fi
  sleep 1
done
echo "observer startup deadline exceeded"
exit 1
'''
    observer_args = ['-namespace', namespace, '-job', run, '-release', cfg.get('release', 'weir'), '-output', observation,
                     '-interval', cfg.get('interval', '1m'), '-duration', cfg['observerDuration'],
                     '-report', report, '-exit-status', exit_status, '-run-id', run, '-load-duration', cfg['duration']]
    result = {'resources': {'apiVersion': 'v1', 'kind': 'List', 'items': resources}}
    for observer in (False, True):
        name = run + '-observer' if observer else run
        environment = {'BINARY_SHA256': cfg['observerSHA256' if observer else 'runnerSHA256']}
        if not observer:
            environment.update({'REPORT': report, 'EXIT_STATUS': exit_status, 'HEARTBEAT': observation + '.ready', 'PROM_READY': '/results/' + run + '-prom-ready', 'PROM_FREEZE': '/results/' + run + '-prom-freeze.json', 'RUN_ID': run, 'RUN_CONFIG_SHA256': config_sha})
        container = {
            'name': 'observer' if observer else 'load', 'image': CARRIER,
            'command': ['/bin/bash', '-c', observer_script if observer else load_script, '--'],
            'args': observer_args if observer else args,
            'env': [{'name': key, 'value': value} for key, value in environment.items()],
            'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
            'resources': {'requests': {'cpu': '100m' if observer else '1', 'memory': '128Mi' if observer else '512Mi'}, 'limits': {'cpu': '200m' if observer else '1', 'memory': '128Mi' if observer else '512Mi'}},
            'volumeMounts': [{'name': volume, 'mountPath': '/' + volume} for volume in ('runner', 'results', 'tmp')],
        }
        if not observer:
            container['env'].append({'name': 'POD_UID', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.uid'}}})
        spec = {
            'restartPolicy': 'Never', 'automountServiceAccountToken': observer, 'enableServiceLinks': False,
            'terminationGracePeriodSeconds': 30, 'nodeSelector': {'kubernetes.io/hostname': cfg['node'], 'kubernetes.io/arch': cfg['architecture']},
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [container],
            'volumes': [{'name': 'runner', 'emptyDir': {'sizeLimit': '64Mi'}}, {'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}}, {'name': 'results', 'persistentVolumeClaim': {'claimName': cfg['pvc']}}],
        }
        if observer:
            spec['serviceAccountName'] = service_account
        pod_labels = dict(labels, app='weir-observer' if observer else 'weir-soak')
        pod_labels['weir-client'] = 'true'
        job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': namespace, 'labels': labels},
               'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': cfg['deadlineSeconds'], 'template': {'metadata': {'labels': pod_labels, 'annotations': {'cluster-autoscaler.kubernetes.io/safe-to-evict': 'false'}}, 'spec': spec}}}
        result['observer' if observer else 'load'] = job
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('destination', type=Path)
    options = parser.parse_args()
    rendered = render(json.loads(options.config.read_text()))
    options.destination.mkdir(parents=True, exist_ok=True)
    for name, value in rendered.items():
        with (options.destination / (name + '.json')).open('x') as output:
            json.dump(value, output, indent=2)
