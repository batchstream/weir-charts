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
            ["helm", "template", "weir", str(ROOT / "charts/weir"), "-f", str(ROOT / "tests/values.yaml"), "-f", handle.name],
            capture_output=True, text=True, check=False,
        )
    if not success:
        return result
    if result.returncode:
        raise AssertionError(result.stderr)
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    resources = {item["kind"]: item for item in documents}
    resources["Services"] = {item["metadata"]["name"]: item for item in documents if item["kind"] == "Service"}
    return resources


def config_values(memory="2GiB"):
    node = {"listeners": {"application": "0.0.0.0:7447"},
            "diagnostics": {"address": "127.0.0.1:7449"}, "memory": memory}
    values = {"config": {"existingSecret": "", "data": {"node": node, "routes": {"stores": []}}}}
    return values


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
        self.assertEqual(container["args"], ["serve", "--config", "/etc/weir/node.yaml", "--routes", "/etc/weir/routes.yaml"])
        headless = resources["Services"]["weir-weir-headless"]["spec"]
        self.assertEqual(headless["clusterIP"], "None")
        self.assertEqual([port["name"] for port in headless["ports"]], ["grpc"])
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            command = container[name]["exec"]["command"]
            self.assertEqual(command[:2], ["/weir", "probe"])
            self.assertEqual(command[3], "--address")
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
        first = config_values("2GiB")
        second = config_values("1GiB")
        a, b = render(first), render(second)
        self.assertEqual(yaml.safe_load(a["ConfigMap"]["data"]["node.yaml"])["memory"], "2GiB")
        self.assertEqual(yaml.safe_load(a["ConfigMap"]["data"]["routes.yaml"]), {"stores": []})
        self.assertNotEqual(a["Deployment"]["spec"]["template"]["metadata"]["annotations"], b["Deployment"]["spec"]["template"]["metadata"]["annotations"])
        secret_values = {"config": {"existingSecret": "approved-v2", "revision": "v2"}}
        secret = render(secret_values)
        self.assertEqual(secret["Deployment"]["spec"]["template"]["metadata"]["annotations"]["weir.batchstream.io/config-revision"], "v2")
        routes_changed = config_values()
        routes_changed["config"]["data"]["routes"]["stores"] = [{"name": "mongo", "mongodb": {"uri": "mongodb://mongo:27017"}}]
        changed = render(routes_changed)
        self.assertNotEqual(a["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"], changed["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"])
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
        env = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(env[0]["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
        self.assertEqual(env[1], {"name": "WEIR_PEER_ADDRESS", "value": "[$(POD_IP)]:7448"})

    def test_metrics_requires_listener_opt_in_and_selected_scrapers(self):
        rule = {"from": [{"podSelector": {"matchLabels": {"app": "prometheus"}}}], "ports": [{"protocol": "TCP", "port": 7449}]}
        values = {"metrics": {"enabled": True, "ingress": [rule]}, "networkPolicy": {"ingress": []}}
        resources = render(values)
        self.assertEqual(resources["NetworkPolicy"]["spec"]["ingress"], [rule])
        ports = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]["ports"]
        metrics_port = {"name": "metrics", "containerPort": 7449}
        self.assertIn(metrics_port, ports)
        values.update(config_values())
        self.assertNotEqual(render(values, success=False).returncode, 0)
        values["config"]["data"]["node"]["diagnostics"] = {"address": "0.0.0.0:7449", "allow_intranet": True}
        self.assertIn("ConfigMap", render(values))
        values["config"]["data"]["node"]["diagnostics"]["address"] = "[::]:7449"
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

    def test_listener_values_match_generated_ports_and_probes(self):
        for field, value in (("listeners", {"application": "0.0.0.0:7446"}),
                             ("diagnostics", {"address": "127.0.0.1:7448"})):
            invalid = config_values()
            invalid["config"]["data"]["node"][field] = value
            self.assertNotEqual(render(invalid, success=False).returncode, 0)
        invalid = config_values()
        invalid["peer"] = {"enabled": True}
        self.assertNotEqual(render(invalid, success=False).returncode, 0)
        invalid["config"]["data"]["node"]["listeners"]["peer"] = "0.0.0.0:7448"
        self.assertNotEqual(render(invalid, success=False).returncode, 0)

    def test_inline_auth_and_writable_extra_mounts_are_rejected(self):
        invalid = config_values()
        invalid["config"]["data"]["routes"]["stores"] = [{"name": "mongo", "mongodb": {"uri": "mongodb://mongo:27017", "username": "test-user", "password": "test-password"}}]
        self.assertNotEqual(render(invalid, success=False).returncode, 0)
        invalid = {"extraVolumeMounts": [{"name": "extra", "mountPath": "/extra", "readOnly": False}]}
        self.assertNotEqual(render(invalid, success=False).returncode, 0)

    def test_ci_builds_the_recorded_source_contract(self):
        chart = yaml.safe_load((ROOT / "charts/weir/Chart.yaml").read_text())
        revision = chart["annotations"]["weir.batchstream.io/source-revision"]
        self.assertEqual(len(revision), 40)
        for name in ("ci.yaml", "release.yaml"):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            steps = next(iter(workflow["jobs"].values()))["steps"]
            source = next(step for step in steps if step.get("with", {}).get("repository") == "batchstream/weir")
            self.assertEqual(source["with"]["ref"], revision)

    def test_invalid_values_fail_before_cluster_mutation(self):
        for invalid in (
            {"metrics": {"enabled": True}}, {"replicaCount": 0}, {"containerPort": 70000},
            {"terminationGracePeriodSeconds": 4}, {"unknownSetting": True},
            {"image": {"digest": "sha256:bad"}},
            {"config": {"key": "node.json"}},
            {"image": {"digest": ""}},
            {"config": {"existingSecret": ""}},
            {"config": {"data": {"services": []}}},
            {"podDisruptionBudget": {"enabled": True}},
        ):
            with self.subTest(invalid=invalid):
                self.assertNotEqual(render(invalid, success=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
