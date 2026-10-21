"""Offline contracts for the owned single-node EKS functional entry."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('functional_owned', ROOT / 'tests/acceptance/functional-owned.py')
ENTRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRY)


class FunctionalOwnedTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.testdata').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.testdata')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.run = {'namespace': 'weir-accept-run12345', 'owner': 'run12345'}
        self.cfg = {'runner_source': 'a' * 40, 'runner_sha256': 'b' * 64}
        self.proofs = {name: {'image': 'example/' + name + '@sha256:' + 'c' * 64,
                              'platform_digest': 'sha256:' + 'd' * 64, 'source': 'e' * 40}
                       for name in ('candidate', 'mongo', 'search', 'runner')}
        self.options = {'cfg': self.cfg, 'run': self.run, 'proofs': self.proofs,
                        'identity': {'namespace_uid': '11111111-1111-1111-1111-111111111111',
                                     'pod_name': 'weir-abc-def', 'pod_uid': '22222222-2222-2222-2222-222222222222'}}

    def config_report(self):
        config = ENTRY.input_config(self.options)
        checks = []
        for kind in ('resolve_open', 'sdk_close'):
            check = {'kind': kind, 'seed': 1, 'passed': True, 'error_code': 0}
            checks.append(check)
        for backend in ('mongo', 'search'):
            for kind in ('prepare', 'driver_close', 'cleanup_verified', *ENTRY.CHECKS):
                seed = 1 if kind in ENTRY.CHECKS else 0
                check = {'kind': kind, 'backend': backend, 'seed': seed, 'passed': True,
                         'error_code': 3 if kind == 'cancel_admission' else 0}
                checks.append(check)
        identity_keys = ('server_source', 'server_oci', 'namespace', 'namespace_uid', 'pod_name', 'pod_uid', 'run_owner')
        report = {'schema': 1, 'mode': 'functional', 'passed': True, 'error_code': 0, 'seed_count': 1,
                  'caller_asserted_provenance': {key: config[key] for key in identity_keys},
                  'runner_build': {'target': 'linux/arm64', 'injected_source': self.cfg['runner_source'],
                                   'vcs_revision': self.cfg['runner_source'], 'vcs_modified': False,
                                   'sdk': 'v0.2.0', 'protocol': 'v0.2.0'},
                  'owned_dataset_base': config['owned_dataset_prefix'] + 'f' * 16, 'checks': checks}
        result = {'report': report, 'input': config, 'cfg': self.cfg}
        return result

    def test_report_requires_exact_all_backend_checks_cleanup_build_and_identity(self):
        valid = self.config_report()
        ENTRY.validate_report(valid)
        for mutation in ('missing', 'duplicate', 'unknown', 'identity', 'source', 'target', 'modified', 'cleanup', 'mode'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(valid)
                report = bad['report']
                if mutation == 'missing':
                    report['checks'].pop()
                elif mutation == 'duplicate':
                    report['checks'].append(report['checks'][0])
                elif mutation == 'unknown':
                    report['checks'][0]['mutation_unknown'] = True
                elif mutation == 'identity':
                    report['caller_asserted_provenance']['namespace_uid'] = 'different'
                elif mutation == 'source':
                    report['runner_build']['vcs_revision'] = 'f' * 40
                elif mutation == 'target':
                    report['runner_build']['target'] = 'linux/amd64'
                elif mutation == 'modified':
                    report['runner_build']['vcs_modified'] = True
                elif mutation == 'cleanup':
                    report['checks'][-1]['passed'] = False
                else:
                    report['mode'] = 'build'
                with self.assertRaises(ValueError):
                    ENTRY.validate_report(bad)

    def test_unknown_vcs_requires_separate_checksummed_frozen_build_receipt(self):
        options = self.config_report()
        build = options['report']['runner_build']
        build['vcs_revision'] = 'unknown'
        del build['vcs_modified']
        receipt = {'source_revision': self.cfg['runner_source'], 'sha256': self.cfg['runner_sha256'],
                   'target': 'linux/arm64', 'source_tree_sha256': 'c' * 64}
        path = self.directory / 'runner-build.json'
        raw = json.dumps(receipt).encode()
        path.write_bytes(raw)
        self.cfg['runner_build'] = str(path)
        self.cfg['runner_build_sha256'] = hashlib.sha256(raw).hexdigest()
        ENTRY.validate_report(options)
        path.write_text('{}')
        with self.assertRaises(ValueError):
            ENTRY.validate_report(options)

    def test_actual_pod_identity_imageid_and_native_placement_are_checked(self):
        cluster = ENTRY.Cluster(self.run)
        pods = []
        for role in ('weir', 'mongo', 'search', 'runner'):
            labels = {ENTRY.OWNER_LABEL: self.run['owner'], 'app': role}
            if role == 'weir':
                labels['app.kubernetes.io/instance'] = 'functional'
            image = self.proofs['candidate' if role == 'weir' else role]['image']
            if role == 'weir':
                image = image.split('@')[0] + '@' + self.proofs['candidate']['platform_digest']
            pod = {'metadata': {'name': role, 'uid': role + '-uid', 'labels': labels},
                   'spec': {'nodeName': 'checked-node', 'nodeSelector': {'type': 'sts-arm', 'kubernetes.io/arch': 'arm64'},
                            'containers': [{'image': image}]},
                   'status': {'containerStatuses': [{'ready': True, 'restartCount': 0, 'containerID': role,
                                                     'imageID': 'example@sha256:' + 'd' * 64}]}}
            pods.append(pod)
        options = {'cluster': cluster, 'proofs': self.proofs, 'node': 'checked-node'}
        listing = {'items': pods}
        with mock.patch.object(cluster, 'owned'), mock.patch.object(cluster, 'get', return_value=listing):
            baseline = ENTRY.observe(options)
            pods[1]['spec']['nodeSelector']['kubernetes.io/arch'] = 'amd64'
            with self.assertRaisesRegex(ValueError, 'outside checked ARM node'):
                ENTRY.observe(options)
            pods[1]['spec']['nodeSelector']['kubernetes.io/arch'] = 'arm64'
            options['baseline'] = baseline
            pods[0]['metadata']['uid'] = 'replacement'
            with self.assertRaises(ValueError):
                ENTRY.observe(options)
            pods[0]['metadata']['uid'] = 'weir-uid'
            pods[0]['status']['containerStatuses'][0]['imageID'] = 'example@sha256:' + 'f' * 64
            with self.assertRaises(ValueError):
                ENTRY.observe(options)

    def test_real_mongo_runtime_index_requires_verified_arm_config_and_exact_selected_pin(self):
        index = 'sha256:d0d926f94df099bff534b7ee5b5986458131a22489dfff8664509af0c1e2ca9c'
        config = 'sha256:00eb3c882069f9bfb1f36e1fa9ece2b5f0f28448059b3f5d38e815fae14fd16d'
        manifest = 'sha256:65e9d974435d072874cf08922f26ecdf7d45b54662ce632509750b5c23ba5c5f'
        proof = {'image': ENTRY.MONGO, 'platform_digest': manifest, 'config_digest': config}
        status = {'imageID': 'docker.io/library/mongo@' + index, 'image': config}
        options = {'proof': proof, 'status': status, 'selected_image': ENTRY.MONGO, 'expected_image': ENTRY.MONGO}
        ENTRY.runtime_image(options)
        for mutation in ('config', 'index', 'missing_config', 'selected_pin', 'suffix'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(options)
                if mutation == 'config':
                    bad['status']['image'] = 'sha256:' + 'f' * 64
                elif mutation == 'index':
                    bad['status']['imageID'] = 'docker.io/library/mongo@sha256:' + 'f' * 64
                elif mutation == 'missing_config':
                    del bad['proof']['config_digest']
                elif mutation == 'selected_pin':
                    bad['selected_image'] = 'mongo:8.0.32'
                else:
                    bad['status']['imageID'] += '-suffix'
                with self.assertRaises(ValueError):
                    ENTRY.runtime_image(bad)
        status['imageID'] = 'docker.io/library/mongo@' + manifest
        ENTRY.runtime_image(options)

    def test_fixture_and_runner_pods_have_approved_bounds_no_pvc_or_rbac(self):
        items = ENTRY.documents(self.options)
        self.assertEqual({item['kind'] for item in items}, {'ResourceQuota', 'LimitRange', 'Service', 'Pod', 'Job'})
        cpu = memory = 0
        for item in items:
            self.assertEqual(item['metadata']['labels'][ENTRY.OWNER_LABEL], self.run['owner'])
            if item['kind'] == 'Service':
                self.assertEqual(item['spec']['type'], 'ClusterIP')
            if item['kind'] not in ('Pod', 'Job'):
                continue
            spec = item['spec'] if item['kind'] == 'Pod' else item['spec']['template']['spec']
            self.assertFalse(spec['automountServiceAccountToken'])
            expected_selector = {'type': 'sts-arm', 'kubernetes.io/arch': 'arm64'}
            self.assertEqual(spec['nodeSelector'], expected_selector)
            self.assertTrue(all('emptyDir' in v and 'sizeLimit' in v['emptyDir'] for v in spec['volumes']))
            for container in spec['containers']:
                self.assertEqual(container['resources']['requests'], container['resources']['limits'])
                cpu += ENTRY.quantity(container['resources']['limits']['cpu'], 'cpu')
                memory += ENTRY.quantity(container['resources']['limits']['memory'], 'memory')
        self.assertEqual(cpu + 1, 2.75)  # Includes the independently rendered Weir Pod.
        self.assertLessEqual(memory + 1024 ** 3, 6 * 1024 ** 3)
        job = next(item for item in items if item['kind'] == 'Job')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 660)
        script = job['spec']['template']['spec']['containers'][0]['command'][2]
        self.assertIn('/work/program -input /work/config.json', script)
        self.assertIn('sha256sum -c -', script)
        self.assertIn('seq 1 300', script)
        self.assertNotIn('persistentVolumeClaim', json.dumps(items))

    def test_actual_helm_render_uses_candidate_owned_routes_without_production_changes(self):
        options = dict(self.options, destination=self.directory / 'prepared')
        with mock.patch.object(ENTRY, 'validate_config', return_value=self.proofs), mock.patch.object(ENTRY.Cluster, 'kubectl', side_effect=AssertionError('offline contacted cluster')):
            prepared = ENTRY.prepare(options)
        deployment = next(item for item in prepared['chart'] if item['kind'] == 'Deployment')
        spec = deployment['spec']['template']['spec']
        self.assertEqual(spec['containers'][0]['image'], 'ghcr.io/batchstream/weir@' + self.proofs['candidate']['platform_digest'])
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(spec['nodeSelector']['type'], 'sts-arm')
        self.assertNotIn('NetworkPolicy', [item['kind'] for item in prepared['items']])
        config = next(item for item in prepared['chart'] if item['kind'] == 'ConfigMap')
        self.assertIn('mongo.' + prepared['run']['namespace'], config['data']['routes.yaml'])
        self.assertIn('search.' + prepared['run']['namespace'], config['data']['routes.yaml'])
        controls = json.loads((options['destination'] / 'controls.json').read_text())
        self.assertFalse(controls['executed'])
        self.assertRegex(prepared['run']['owner'], r'^run[0-9a-f]{12}$')

    def test_handoff_input_is_exact_fifteen_fields_namespace_owned(self):
        config = ENTRY.input_config(self.options)
        self.assertEqual(len(config), 15)
        self.assertTrue(config['allow_remote'])
        self.assertEqual(config['server_oci'], self.proofs['candidate']['platform_digest'])
        self.assertTrue(all('.' + self.run['namespace'] + '.svc.cluster.local' in config[key] for key in ('mongo_uri', 'search_url')))
        self.assertEqual(config['owned_dataset_prefix'], 'weiraccept_run12345_')

    def test_namespace_cleanup_uses_uid_precondition_and_checks_owner(self):
        cluster = ENTRY.Cluster(self.run)
        cluster.uid = self.options['identity']['namespace_uid']
        namespace = {'metadata': {'uid': cluster.uid, 'labels': {ENTRY.OWNER_LABEL: self.run['owner']}}}
        with mock.patch.object(cluster, 'get', return_value=namespace), mock.patch.object(cluster, 'kubectl', return_value=b'') as call:
            cluster.cleanup()
        delete = call.call_args_list[0]
        self.assertEqual(delete.args[0][:2], ['delete', '--raw'])
        expected_preconditions = {'uid': cluster.uid}
        self.assertEqual(json.loads(delete.kwargs['input'])['preconditions'], expected_preconditions)
        namespace['metadata']['uid'] = 'replacement'
        with mock.patch.object(cluster, 'get', return_value=namespace), mock.patch.object(cluster, 'kubectl') as call:
            with self.assertRaises(ValueError):
                cluster.cleanup()
            call.assert_not_called()

    def test_local_oci_proof_rejects_wrong_arch_and_mismatched_content(self):
        config = {'architecture': 'arm64', 'os': 'linux'}
        raw = json.dumps(config).encode()
        manifest = {'config': {'digest': ENTRY.digest(raw)}}
        manifest_raw = json.dumps(manifest).encode()
        index = {'manifests': [{'platform': {'architecture': 'arm64', 'os': 'linux'}, 'digest': ENTRY.digest(manifest_raw)}]}
        index_raw = json.dumps(index).encode()
        for name, data in (('config', raw), ('manifest', manifest_raw), ('index', index_raw)):
            (self.directory / name).write_bytes(data)
        proof = {name: str(self.directory / name) for name in ('config', 'manifest', 'index')}
        proof['image'] = 'example/native@' + ENTRY.digest(index_raw)
        checked = ENTRY.image_proof(proof)
        self.assertEqual(checked['platform_digest'], ENTRY.digest(manifest_raw))
        self.assertEqual(checked['config_digest'], ENTRY.digest(raw))
        (self.directory / 'config').write_text('{"architecture":"amd64","os":"linux"}')
        with self.assertRaises(ValueError):
            ENTRY.image_proof(proof)
        # Even a fully hash-linked chain cannot authorize a non-ARM config.
        raw = (self.directory / 'config').read_bytes()
        manifest['config']['digest'] = ENTRY.digest(raw)
        manifest_raw = json.dumps(manifest).encode()
        index['manifests'][0]['digest'] = ENTRY.digest(manifest_raw)
        index_raw = json.dumps(index).encode()
        (self.directory / 'manifest').write_bytes(manifest_raw)
        (self.directory / 'index').write_bytes(index_raw)
        proof['image'] = 'example/native@' + ENTRY.digest(index_raw)
        with self.assertRaisesRegex(ValueError, 'native Linux ARM image required'):
            ENTRY.image_proof(proof)

    def distribution(self):
        source = 'e' * 40
        oci = {'index': 'sha256:' + 'a' * 64, 'images': {'arm64': {'manifest': 'sha256:' + 'b' * 64, 'binary_sha256': 'c' * 64}}}
        binary = {'linux-arm64': 'c' * 64}
        delivery = {'source': source, 'images': {'weir': {'oci': oci, 'binaries': binary}}}
        receipt = {'source': source, 'oci': oci, 'binaries': binary, 'archives': {}}
        members = {'delivery.json': json.dumps(delivery).encode(), 'weir-first/receipt.json': json.dumps(receipt).encode()}
        checksum = ''.join(hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in members.items())
        path = self.directory / 'distribution.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, raw in members.items():
                archive.writestr(name, raw)
            archive.writestr('SHA256SUMS', checksum)
        cfg = {'distribution_bundle': str(path), 'distribution_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
               'server_source': source, 'candidate': {'image': 'ghcr.io/batchstream/weir@' + oci['index'],
                                                     'platform_digest': oci['images']['arm64']['manifest']}}
        return cfg

    def test_retained_distribution_binds_source_index_platform_and_checksum(self):
        cfg = self.distribution()
        self.assertEqual(ENTRY.candidate_proof(cfg)['source'], cfg['server_source'])
        for field in ('server_source', 'distribution_sha256'):
            bad = copy.deepcopy(cfg)
            bad[field] = 'f' * (40 if field == 'server_source' else 64)
            with self.assertRaises(ValueError):
                ENTRY.candidate_proof(bad)
        cfg['candidate']['platform_digest'] = 'sha256:' + 'f' * 64
        with self.assertRaises(ValueError):
            ENTRY.candidate_proof(cfg)

    def test_candidate_remains_manifest_only_even_with_caller_supplied_config_digest(self):
        cfg = self.distribution()
        caller_config = 'sha256:' + 'f' * 64
        cfg['candidate']['config_digest'] = caller_config
        cfg['candidate']['source'] = 'caller-controlled'
        proof = ENTRY.candidate_proof(cfg)
        self.assertEqual(set(proof), {'image', 'platform_digest', 'source'})
        self.assertEqual(proof['source'], cfg['server_source'])
        selected = proof['image'].split('@')[0] + '@' + proof['platform_digest']
        status = {'imageID': proof['image'], 'image': caller_config}
        options = {'proof': proof, 'status': status, 'selected_image': selected, 'expected_image': selected}
        with self.assertRaisesRegex(ValueError, 'deployed platform imageID mismatch'):
            ENTRY.runtime_image(options)
        # Defense in depth: even a poisoned proof cannot use index fallback
        # when the Pod selected a different (platform-manifest) reference.
        proof['config_digest'] = caller_config
        with self.assertRaisesRegex(ValueError, 'deployed platform imageID mismatch'):
            ENTRY.runtime_image(options)
        status['imageID'] = selected
        ENTRY.runtime_image(options)

    def test_headroom_accounts_for_requests_usage_pods_and_native_node(self):
        node = {'metadata': {'name': 'arm-node', 'uid': 'node-uid',
                             'labels': {'kubernetes.io/arch': 'arm64', 'kubernetes.io/hostname': 'different-arm-hostname'}},
                'spec': {}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                                      'allocatable': {'cpu': '8', 'memory': '16Gi', 'pods': '58'}}}
        pod = {'spec': {'nodeName': 'arm-node', 'containers': [{'resources': {'requests': {'cpu': '1', 'memory': '1Gi'}}}]}}
        metrics = {'items': [{'metadata': {'name': 'arm-node'}, 'usage': {'cpu': '1', 'memory': '1Gi'}}]}
        cluster = ENTRY.Cluster(self.run)
        def get(args):
            result = {'items': [node] if args[0] == 'nodes' else [pod]}
            return result
        def kubectl(args):
            result = ENTRY.CONTEXT.encode() if args[0] == 'config' else json.dumps(metrics).encode()
            return result
        with mock.patch.object(cluster, 'get', side_effect=get), mock.patch.object(cluster, 'kubectl', side_effect=kubectl):
            checked = ENTRY.headroom(cluster)
            self.assertEqual(checked['node'], 'arm-node')
            self.assertEqual(checked['hostname'], 'different-arm-hostname')
            del node['metadata']['labels']['kubernetes.io/hostname']
            with self.assertRaises(ValueError):
                ENTRY.headroom(cluster)
            node['metadata']['labels']['kubernetes.io/hostname'] = ''
            with self.assertRaises(ValueError):
                ENTRY.headroom(cluster)
            node['metadata']['labels']['kubernetes.io/hostname'] = 'different-arm-hostname'
            metrics['items'][0]['usage']['cpu'] = '7'
            with self.assertRaises(ValueError):
                ENTRY.headroom(cluster)
            metrics['items'][0]['usage']['cpu'] = '1'
            pod['spec']['containers'][0]['resources']['requests']['cpu'] = '7'
            with self.assertRaises(ValueError):
                ENTRY.headroom(cluster)

    def test_uploaded_checksum_precedes_every_program_invocation_and_rejects_replacement(self):
        binary = self.directory / 'runner'
        original = b'original validated runner bytes'
        binary.write_bytes(original)
        cfg = {'runner_binary': str(binary), 'runner_sha256': hashlib.sha256(original).hexdigest()}
        cluster = mock.Mock()
        uploaded = {}
        events = []
        def remote(options):
            argv = options['argv']
            events.append(argv)
            if 'input' in options:
                uploaded[argv[-1].split('/work/')[-1]] = options['input']
                return b''
            if argv[0] == 'sha256sum':
                checksum = hashlib.sha256(uploaded['program']).hexdigest()
                return (checksum + '  /work/program\n').encode()
            if argv[0] == '/work/program':
                return b'version report'
            return b'aarch64\n'
        cluster.exec.side_effect = remote
        runner_input = {}
        handoff = {'cluster': cluster, 'pod': 'runner', 'cfg': cfg, 'input': runner_input,
                   'handoff_deadline': ENTRY.time.monotonic() + 255}
        # The local file is replaced after the earlier trusted hash was computed.
        binary.write_bytes(b'replaced since initial validation')
        with self.assertRaisesRegex(ValueError, 'uploaded runner checksum mismatch'):
            ENTRY.upload_runner(handoff)
        self.assertFalse(any(argv[0] == '/work/program' for argv in events))
        self.assertFalse(any('/work/ready' in ' '.join(argv) for argv in events))
        binary.write_bytes(original)
        events.clear()
        self.assertEqual(ENTRY.upload_runner(handoff), b'version report')
        checksum_index = next(i for i, argv in enumerate(events) if argv[0] == 'sha256sum')
        program_index = next(i for i, argv in enumerate(events) if argv[0] == '/work/program')
        self.assertLess(checksum_index, program_index)

    def test_handoff_exec_caps_both_budgets_after_ownership_and_rejects_expired_ready(self):
        cluster = ENTRY.Cluster(self.run)
        opts = {'pod': 'runner', 'argv': ['cat'], 'input': b'binary', 'timeout': 120, 'handoff_deadline': 255}
        with mock.patch.object(cluster, 'owned'), mock.patch.object(cluster, 'kubectl', return_value=b'ok') as call, mock.patch.object(ENTRY.time, 'monotonic', side_effect=[10, 12, 13]):
            self.assertEqual(cluster.exec(opts), b'ok')
            self.assertEqual(call.call_args.kwargs['timeout'], 120)
            self.assertIn('--request-timeout=120s', call.call_args.args[0])
        opts['handoff_deadline'] = 20
        with mock.patch.object(cluster, 'owned'), mock.patch.object(cluster, 'kubectl', return_value=b'ok') as call, mock.patch.object(ENTRY.time, 'monotonic', side_effect=[10, 19.875, 19.9]):
            self.assertEqual(cluster.exec(opts), b'ok')
            self.assertEqual(call.call_args.kwargs['timeout'], .125)
            self.assertIn('--request-timeout=0.125s', call.call_args.args[0])
        opts['argv'] = ['/bin/bash', '-c', 'test -f /work/ready || : > /work/ready']
        with mock.patch.object(cluster, 'owned') as owned, mock.patch.object(cluster, 'kubectl') as call, mock.patch.object(ENTRY.time, 'monotonic', return_value=21):
            with self.assertRaisesRegex(ValueError, 'handoff deadline exceeded'):
                cluster.exec(opts)
            owned.assert_not_called()
            call.assert_not_called()
        with mock.patch.object(cluster, 'owned'), mock.patch.object(cluster, 'kubectl') as call, mock.patch.object(ENTRY.time, 'monotonic', side_effect=[10, 21]):
            with self.assertRaisesRegex(ValueError, 'handoff deadline exceeded'):
                cluster.exec(opts)
            call.assert_not_called()
        with mock.patch.object(cluster, 'owned'), mock.patch.object(cluster, 'kubectl', return_value=b'ok'), mock.patch.object(ENTRY.time, 'monotonic', side_effect=[10, 12, 21]):
            with self.assertRaisesRegex(ValueError, 'handoff deadline exceeded'):
                cluster.exec(opts)

    def test_handoff_timeout_retains_bounded_failure_metadata_without_payload_or_retry(self):
        binary = self.directory / 'runner'
        payload = b'private input payload not to serialize'
        binary.write_bytes(payload)
        cfg = {'runner_binary': str(binary), 'runner_sha256': hashlib.sha256(payload).hexdigest()}
        cluster = mock.Mock()
        cluster.exec.side_effect = ENTRY.subprocess.TimeoutExpired('command not serialized', 120, output=b'o' * 5000, stderr=b'e' * 5000)
        runner_input = {}
        options = {'cluster': cluster, 'pod': 'runner', 'cfg': cfg, 'input': runner_input,
                   'handoff_deadline': 255, 'destination': self.directory}
        with mock.patch.object(ENTRY.time, 'monotonic', return_value=10):
            with self.assertRaises(ENTRY.subprocess.TimeoutExpired):
                ENTRY.upload_runner(options)
        self.assertEqual(cluster.exec.call_count, 1)
        self.assertEqual(cluster.exec.call_args.args[0]['timeout'], 120)
        metadata = json.loads((self.directory / 'handoff-failure.json').read_text())
        self.assertEqual(metadata['failure_kind'], 'process_timeout')
        self.assertEqual(metadata['timeout_classification'], 'confirmed')
        self.assertIsNone(metadata['returncode'])
        self.assertEqual(metadata['input_length'], len(payload))
        self.assertEqual(metadata['actual_transferred_bytes'], 'unknown')
        self.assertEqual(len(metadata['stdout']), 4096)
        self.assertEqual(len(metadata['stderr']), 4096)
        self.assertNotIn(payload.decode(), json.dumps(metadata))
        self.assertNotIn('command not serialized', json.dumps(metadata))

    def test_nonzero_upload_retains_failure_without_claiming_timeout_or_retrying(self):
        binary = self.directory / 'runner'
        payload = b'input bytes excluded from failure evidence'
        binary.write_bytes(payload)
        cfg = {'runner_binary': str(binary), 'runner_sha256': hashlib.sha256(payload).hexdigest()}
        error = ENTRY.subprocess.CalledProcessError(17, 'command excluded', output=b'o' * 5000, stderr=b'failure' * 1000)
        cluster = mock.Mock()
        cluster.exec.side_effect = error
        runner_input = {}
        options = {'cluster': cluster, 'pod': 'runner', 'cfg': cfg, 'input': runner_input,
                   'handoff_deadline': 255, 'destination': self.directory}
        with mock.patch.object(ENTRY.time, 'monotonic', return_value=10):
            with self.assertRaises(ENTRY.subprocess.CalledProcessError) as caught:
                ENTRY.upload_runner(options)
        self.assertIs(caught.exception, error)
        self.assertEqual(cluster.exec.call_count, 1)
        metadata = json.loads((self.directory / 'handoff-failure.json').read_text())
        self.assertEqual(metadata['failure_kind'], 'command_failure')
        self.assertEqual(metadata['timeout_classification'], 'unknown')
        self.assertEqual(metadata['returncode'], 17)
        self.assertEqual(metadata['input_length'], len(payload))
        self.assertEqual(metadata['actual_transferred_bytes'], 'unknown')
        self.assertEqual(len(metadata['stdout']), 4096)
        self.assertEqual(len(metadata['stderr']), 4096)
        self.assertNotIn(payload.decode(), json.dumps(metadata))
        self.assertNotIn('command excluded', json.dumps(metadata))

    def test_ready_publication_timeout_uses_same_bounded_handoff_evidence(self):
        cluster = mock.Mock()
        error = ENTRY.subprocess.TimeoutExpired('ready argv excluded', 4, output=b'o' * 5000, stderr=b'e' * 5000)
        cluster.exec.side_effect = error
        options = {'pod': 'runner', 'argv': ['/bin/bash', '-c', 'set -euC; : > /work/ready'],
                   'handoff_deadline': 14, 'destination': self.directory}
        with mock.patch.object(ENTRY.time, 'monotonic', return_value=10):
            with self.assertRaises(ENTRY.subprocess.TimeoutExpired) as caught:
                ENTRY.handoff_exec(cluster, options)
        self.assertIs(caught.exception, error)
        self.assertEqual(cluster.exec.call_count, 1)
        self.assertEqual(cluster.exec.call_args.args[0]['timeout'], 4)
        metadata = json.loads((self.directory / 'handoff-failure.json').read_text())
        self.assertEqual(metadata['failure_kind'], 'process_timeout')
        self.assertEqual(metadata['timeout_classification'], 'confirmed')
        self.assertEqual(metadata['input_length'], 0)
        self.assertEqual(len(metadata['stdout']), 4096)
        self.assertEqual(len(metadata['stderr']), 4096)
        self.assertNotIn('ready argv excluded', json.dumps(metadata))

    def test_execution_pins_actual_hostname_not_node_api_name(self):
        items = ENTRY.documents(self.options)
        selector = {'type': 'sts-arm', 'kubernetes.io/arch': 'arm64'}
        deployment = {'kind': 'Deployment', 'spec': {'template': {'spec': {'nodeSelector': selector}}}}
        items.append(deployment)
        cluster = mock.Mock()
        cluster.uid = None
        created = {'metadata': {'uid': 'owned-namespace-uid'}}
        cluster.kubectl.return_value = json.dumps(created).encode()
        listing = {'items': []}
        cluster.get.return_value = listing
        cluster.scoped.side_effect = ValueError('stop after selector assignment')
        checked = {'node': 'api-node-name', 'hostname': 'actual-hostname-label'}
        options = dict(self.options, items=items, chart=[deployment], destination=self.directory)
        with mock.patch.object(ENTRY, 'Cluster', return_value=cluster), mock.patch.object(ENTRY, 'headroom', return_value=checked):
            with self.assertRaisesRegex(ValueError, 'stop after selector assignment'):
                ENTRY.execute(options)
        for item in items:
            if item['kind'] == 'Pod':
                spec = item['spec']
            elif item['kind'] in ('Deployment', 'Job'):
                spec = item['spec']['template']['spec']
            else:
                continue
            self.assertEqual(spec['nodeSelector']['kubernetes.io/hostname'], checked['hostname'])
            self.assertNotEqual(spec['nodeSelector']['kubernetes.io/hostname'], checked['node'])

    def test_existing_regular_file_and_wrong_native_binary_are_rejected(self):
        path = self.directory / 'binary'
        path.write_bytes(b'not-linux-arm')
        cfg = {'server_source': 'a' * 40, 'runner_source': 'b' * 40,
               'runner_binary': str(path), 'runner_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        with self.assertRaisesRegex(ValueError, 'AArch64'):
            ENTRY.validate_config(cfg)
        link = self.directory / 'symlink'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            ENTRY.regular(link)


if __name__ == '__main__':
    unittest.main()
