import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(values=None, success=True):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(values or {}, handle)
        handle.flush()
        result = subprocess.run(
            ["helm", "template", "weir", str(ROOT / "charts/weir"), "-f", handle.name],
            capture_output=True, text=True, check=False,
        )
    if not success:
        return result
    if result.returncode:
        raise AssertionError(result.stderr)
    return {item["kind"]: item for item in yaml.safe_load_all(result.stdout) if item}


class ChartTests(unittest.TestCase):
    def test_defaults_protect_process_and_external_configuration(self):
        resources = render()
        pod = resources["Deployment"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["enableServiceLinks"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertNotIn("Secret", resources)
        self.assertNotIn("ConfigMap", resources)
        self.assertNotIn("ServiceAccount", resources)
        self.assertEqual(resources["Service"]["spec"]["type"], "ClusterIP")
        self.assertEqual(pod["volumes"][0]["secret"]["secretName"], "weir-config")
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            command = container[name]["exec"]["command"]
            self.assertEqual(command[0], "/weir")
            self.assertEqual(command[-1], "127.0.0.1:7449")
        self.assertIn("@sha256:", container["image"])
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 10)

    def test_network_policy_default_denies_backend_and_unlabeled_clients(self):
        policy = render()["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(len(policy["egress"]), 1)
        expected_sources = [{"podSelector": {"matchLabels": {"weir-client": "true"}}}]
        self.assertEqual(policy["ingress"][0]["from"], expected_sources)
        self.assertEqual([p["port"] for p in policy["egress"][0]["ports"]], [53, 53])

    def test_configuration_rolls_and_secret_is_never_rendered(self):
        first = {"config": {"existingSecret": "", "data": {"memory_mib": 768}}}
        second = {"config": {"existingSecret": "", "data": {"memory_mib": 512}}}
        a, b = render(first), render(second)
        self.assertEqual(json.loads(a["ConfigMap"]["data"]["node.json"])["memory_mib"], 768)
        self.assertNotEqual(a["Deployment"]["spec"]["template"]["metadata"]["annotations"], b["Deployment"]["spec"]["template"]["metadata"]["annotations"])
        secret_values = {"config": {"existingSecret": "approved-v2", "revision": "v2"}}
        secret = render(secret_values)
        self.assertNotIn("ConfigMap", secret)
        self.assertNotIn("Secret", secret)

    def test_three_replicas_and_optional_peer(self):
        values = {"replicaCount": 3, "podDisruptionBudget": {"enabled": True}, "peer": {"enabled": True}}
        resources = render(values)
        self.assertEqual(resources["Deployment"]["spec"]["replicas"], 3)
        expected_rolling = {"maxSurge": 0, "maxUnavailable": 1}
        self.assertEqual(resources["Deployment"]["spec"]["strategy"]["rollingUpdate"], expected_rolling)
        self.assertEqual(resources["PodDisruptionBudget"]["spec"]["maxUnavailable"], 1)
        self.assertEqual([p["name"] for p in resources["Service"]["spec"]["ports"]], ["grpc", "peer"])

    def test_metrics_requires_listener_opt_in_and_selected_scrapers(self):
        rule = {"from": [{"podSelector": {"matchLabels": {"app": "prometheus"}}}], "ports": [{"protocol": "TCP", "port": 7449}]}
        values = {"metrics": {"enabled": True, "ingress": [rule]}, "networkPolicy": {"ingress": []}}
        resources = render(values)
        self.assertEqual(resources["NetworkPolicy"]["spec"]["ingress"], [rule])
        ports = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]["ports"]
        metrics_port = {"name": "metrics", "containerPort": 7449}
        self.assertIn(metrics_port, ports)
        values["config"] = {"existingSecret": "", "data": {"diagnostics": "127.0.0.1:7449"}}
        self.assertNotEqual(render(values, success=False).returncode, 0)
        values["config"]["data"] = {"diagnostics": "0.0.0.0:7449", "diagnostics_allow_intranet": True}
        self.assertIn("ConfigMap", render(values))

    def test_recreate_has_no_incompatible_rolling_settings(self):
        values = {"strategy": {"type": "Recreate"}}
        strategy = render(values)["Deployment"]["spec"]["strategy"]
        expected = {"type": "Recreate"}
        self.assertEqual(strategy, expected)

    def test_acceptance_annotations_use_existing_pod_discovery(self):
        overlay = yaml.safe_load((ROOT / 'tests/acceptance/prometheus-values.yaml').read_text())
        overlay['podAnnotations']['cluster-autoscaler.kubernetes.io/safe-to-evict'] = 'false'
        resources = render(overlay)
        annotations = resources['Deployment']['spec']['template']['metadata']['annotations']
        self.assertEqual(annotations['prometheus.io/scrape'], 'true')
        self.assertEqual(annotations['prometheus.io/port'], '7449')
        self.assertEqual(annotations['prometheus.io/path'], '/metrics')
        self.assertEqual(annotations['cluster-autoscaler.kubernetes.io/safe-to-evict'], 'false')

    def test_invalid_values_fail_before_cluster_mutation(self):
        for invalid in (
            {"metrics": {"enabled": True}}, {"replicaCount": 0}, {"containerPort": 70000},
            {"terminationGracePeriodSeconds": 4}, {"unknownSetting": True},
            {"image": {"digest": "sha256:bad"}},
            {"config": {"key": "../secret"}},
            {"config": {"existingSecret": ""}},
            {"config": {"data": {"services": []}}},
            {"podDisruptionBudget": {"enabled": True}},
        ):
            with self.subTest(invalid=invalid):
                self.assertNotEqual(render(invalid, success=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
