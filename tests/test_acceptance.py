import importlib.util
import json
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
                   sdkRevision='c' * 40, runnerSHA256='d' * 64, observerSHA256='e' * 64)
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
        rules = rendered['resources']['items'][1]['rules']
        self.assertEqual({item for rule in rules for item in rule['resources']}, {'pods', 'jobs'})
        self.assertEqual({item for rule in rules for item in rule['verbs']}, {'get', 'list'})
        cfg['runnerSHA256'] = 'REPLACE_WITH_HASH'
        with self.assertRaises(ValueError):
            MODULE.render(cfg)
