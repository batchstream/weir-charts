import importlib.util
import json
import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare', ROOT / 'tests/acceptance/prepare-run.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AcceptanceTests(unittest.TestCase):
    def test_run_is_frozen_non_retrying_and_namespace_scoped(self):
        cfg = json.loads((ROOT / 'tests/acceptance/run.example.json').read_text())
        cfg.update(node='owned-node', serverRevision='a' * 40, imageDigest='sha256:' + 'b' * 64,
                   sdkRevision='c' * 40, runnerSHA256='d' * 64, observerSHA256='e' * 64, evidenceToolSHA256='f' * 64, prometheusSourceUID='prom-uid')
        rendered = MODULE.render(cfg)
        for key in ('load', 'observer'):
            job = rendered[key]
            self.assertEqual(job['spec']['backoffLimit'], 0)
            self.assertEqual(job['spec']['template']['spec']['restartPolicy'], 'Never')
            self.assertEqual(job['metadata']['namespace'], cfg['namespace'])
            self.assertEqual(job['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'], cfg['node'])
        load = rendered['load']['spec']['template']['spec']
        observer = rendered['observer']['spec']['template']['spec']
        self.assertFalse(load['automountServiceAccountToken'])
        self.assertTrue(observer['automountServiceAccountToken'])
        script = load['containers'][0]['command'][2]
        self.assertLess(script.index('test -f "$HEARTBEAT"'), script.index('> "$REPORT"'))
        self.assertIn('-observer-status', load['containers'][0]['args'])
        for resource in rendered['resources']['items']:
            self.assertNotIn(resource['kind'], ('ClusterRole', 'ClusterRoleBinding', 'Secret'))
        self.assertIn('RUN_CONFIG_SHA256', script)
        self.assertIn('POD_UID', script)
        self.assertIn('PROM_FREEZE', script)
        self.assertLess(script.index('Prom preflight identity mismatch'), script.index('> "$REPORT"'))
        rules = rendered['resources']['items'][1]['rules']
        self.assertEqual({group for rule in rules for group in rule['apiGroups']}, {'', 'batch'})
        self.assertNotIn('-metrics=true', observer['containers'][0]['args'])
        self.assertEqual({item for rule in rules for item in rule['resources']}, {'pods', 'jobs'})
        self.assertEqual({item for rule in rules for item in rule['verbs']}, {'get', 'list'})
        cfg['targets'] = ['10.0.0.1:7447', '10.0.0.2:7447', '10.0.0.3:7447']
        with self.assertRaises(ValueError):
            MODULE.render(cfg)
        del cfg['address']
        targeted = MODULE.render(cfg)['load']['spec']['template']['spec']['containers'][0]['args']
        self.assertEqual(targeted[:2], ['-targets', ','.join(cfg['targets'])])
        self.assertNotIn('-address', targeted)
        cfg['run'] = 'invalid-run-prefix'
        with self.assertRaises(ValueError):
            MODULE.render(cfg)
        cfg['run'] = 'weir-soak-run'
        cfg['runnerSHA256'] = 'REPLACE_WITH_HASH'
        with self.assertRaises(ValueError):
            MODULE.render(cfg)

    def test_prometheus_ready_binds_run_config_pod_and_freeze(self):
        cfg = json.loads((ROOT / 'tests/acceptance/run.example.json').read_text())
        cfg.update(node='owned-node', serverRevision='a'*40, imageDigest='sha256:'+'b'*64, sdkRevision='c'*40,
                   runnerSHA256='d'*64, observerSHA256='e'*64, evidenceToolSHA256='f'*64, prometheusSourceUID='prom-uid')
        load = MODULE.render(cfg)['load']['spec']['template']['spec']['containers'][0]
        # Execute the real validation block, before the binary or report can run.
        script = load['command'][2]
        check = script[script.index('proof=()'):script.index("printf '%s  /runner/program")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            freeze = root / 'freeze'; freeze.write_text('frozen identities')
            ready = root / 'ready'
            env = dict(os.environ, PROM_READY=str(ready), PROM_FREEZE=str(freeze), RUN_ID=cfg['run'],
                       RUN_CONFIG_SHA256='config-hash', POD_UID='actual-load-uid')
            valid = [cfg['run'], 'config-hash', 'actual-load-uid', hashlib.sha256(freeze.read_bytes()).hexdigest(), str(int(time.time()))]
            for index in range(6):
                fields = valid.copy()
                if index < 5: fields[index] = 'wrong'
                ready.write_text('\n'.join(fields)+'\n')
                run = subprocess.run(['bash', '-c', 'set -u\n'+check], env=env, capture_output=True)
                self.assertEqual(run.returncode == 0, index == 5)
            valid[4] = str(int(time.time())-121)
            ready.write_text('\n'.join(valid)+'\n')
            run = subprocess.run(['bash', '-c', check], env=env, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
