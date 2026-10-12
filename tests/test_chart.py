import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(values=None, success=True, release="weir", namespace="default"):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(values or {}, handle)
        handle.flush()
        command = ["helm", "template", release, str(ROOT / "charts/weir"), "--namespace", namespace,
                   "-f", str(ROOT / "tests/values.yaml"), "-f", handle.name]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    if not success:
        return result
    if result.returncode:
        raise AssertionError(result.stderr)
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    resources = {item["kind"]: item for item in documents}
    resources["Services"] = {item["metadata"]["name"]: item for item in documents if item["kind"] == "Service"}
    return resources


def process_values(stall="30s"):
    values = {"nodeConfig": {"transport": {"timeouts": {"stall": stall}}}}
    return values


class ChartTests(unittest.TestCase):
    def test_defaults_protect_one_process_and_generate_configuration(self):
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
        self.assertNotIn("ServiceAccount", resources)
        self.assertEqual(len(pod["containers"]), 1)
        sources = pod["volumes"][0]["projected"]["sources"]
        self.assertEqual(sources[0]["configMap"]["name"], "weir-weir")
        self.assertEqual(container["args"], ["serve", "--config", "/etc/weir/node.yaml", "--routes", "/etc/weir/routes.yaml"])
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            command = container[name]["exec"]["command"]
            self.assertEqual(command[:2], ["/weir", "probe"])
            self.assertEqual(command[-1], "127.0.0.1:7449")
        self.assertIn("@sha256:", container["image"])
        node = yaml.safe_load(resources["ConfigMap"]["data"]["node.yaml"])
        self.assertEqual(node["discovery"]["group"], "default/weir")
        self.assertEqual(node["discovery"]["advertise"], ["weir-weir-headless.default.svc.cluster.local:7447"])
        self.assertEqual(node["discovery"]["seeds"], ["weir-weir-mesh.default.svc.cluster.local:7448"])
        self.assertEqual(node["listeners"]["application"], "[::]:7447")
        self.assertEqual(node["diagnostics"]["address"], "127.0.0.1:7449")
        self.assertEqual(yaml.safe_load(resources["ConfigMap"]["data"]["routes.yaml"])["stores"], [])

    def test_group_services_and_mesh_seeds_have_distinct_selectors(self):
        first = render(release="mongo", namespace="application")
        second = render(release="search", namespace="application")
        for release, resources in (("mongo", first), ("search", second)):
            ordinary = resources["Services"][release + "-weir"]["spec"]
            headless = resources["Services"][release + "-weir-headless"]["spec"]
            seed = resources["Services"][release + "-weir-mesh"]["spec"]
            self.assertEqual(ordinary["selector"]["app.kubernetes.io/instance"], release)
            self.assertEqual(headless["selector"], ordinary["selector"])
            self.assertEqual(headless["clusterIP"], "None")
            self.assertEqual([p["name"] for p in ordinary["ports"]], ["grpc"])
            self.assertEqual(seed["type"], "ClusterIP")
            self.assertNotIn("app.kubernetes.io/instance", seed["selector"])
            self.assertEqual(seed["selector"]["weir.batchstream.io/mesh"], "weir")
            self.assertEqual([p["name"] for p in seed["ports"]], ["grpc", "peer"])
            labels = resources["Deployment"]["spec"]["template"]["metadata"]["labels"]
            self.assertEqual(labels["weir.batchstream.io/mesh"], "weir")
        self.assertNotEqual(first["Services"].keys(), second["Services"].keys())

    def test_standalone_has_no_mesh_membership_or_peer_access(self):
        values = {"mesh": {"enabled": False}}
        resources = render(values)
        pod = resources["Deployment"]["spec"]["template"]
        self.assertNotIn("weir.batchstream.io/mesh", pod["metadata"]["labels"])
        self.assertNotIn("weir-weir-mesh", resources["Services"])
        self.assertNotIn("env", pod["spec"]["containers"][0])
        node = yaml.safe_load(resources["ConfigMap"]["data"]["node.yaml"])
        self.assertNotIn("peer", node["listeners"])
        self.assertNotIn("seeds", node["discovery"])
        self.assertEqual(len(resources["NetworkPolicy"]["spec"]["egress"]), 1)

    def test_network_policy_automatically_isolates_mesh_named_peer_ports(self):
        values = {"mesh": {"name": "private", "port": 7558}, "service": {"port": 7557}}
        resources = render(values)
        policy = resources["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(len(policy["egress"]), 2)
        peer_ingress = policy["ingress"][-1]
        peer_egress = policy["egress"][-1]
        selector = peer_ingress["from"][0]["podSelector"]["matchLabels"]
        self.assertEqual(selector["weir.batchstream.io/mesh"], "private")
        self.assertNotIn("namespaceSelector", peer_ingress["from"][0])
        self.assertEqual(peer_egress["to"][0]["podSelector"]["matchLabels"], selector)
        self.assertEqual(peer_ingress["ports"][0]["port"], "peer")
        self.assertEqual(peer_egress["ports"][0]["port"], "peer")
        self.assertEqual(resources["Services"]["weir-weir-mesh"]["spec"]["ports"][1]["targetPort"], "peer")
        self.assertEqual(policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["weir-client"], "true")

    def test_configuration_rolls_and_external_secret_supplies_only_routes(self):
        first, second = process_values("30s"), process_values("20s")
        a, b = render(first), render(second)
        node = yaml.safe_load(a["ConfigMap"]["data"]["node.yaml"])
        self.assertEqual(node["transport"]["timeouts"]["stall"], "30s")
        checksum_a = a["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        checksum_b = b["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        self.assertNotEqual(checksum_a, checksum_b)
        secret_values = {"storesSecret": "approved-v2", "storesRevision": "v2"}
        secret = render(secret_values)
        self.assertNotIn("routes.yaml", secret["ConfigMap"]["data"])
        self.assertIn("node.yaml", secret["ConfigMap"]["data"])
        sources = secret["Deployment"]["spec"]["template"]["spec"]["volumes"][0]["projected"]["sources"]
        self.assertEqual(sources[0]["configMap"]["items"][0]["key"], "node.yaml")
        self.assertEqual(sources[1]["secret"]["name"], "approved-v2")
        self.assertEqual(len(sources[1]["secret"]["items"]), 1)
        self.assertEqual(sources[1]["secret"]["items"][0]["key"], "routes.yaml")
        self.assertEqual(secret["Deployment"]["spec"]["template"]["metadata"]["annotations"]["weir.batchstream.io/stores-revision"], "v2")
        store_values = {"stores": [{"name": "mongo", "backend": {"mongodb": {"uri": "mongodb://mongo:27017"}}}]}
        changed = render(store_values)
        self.assertNotEqual(checksum_a, changed["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"])
        self.assertNotIn("Secret", secret)

    def test_derived_identity_and_addresses_follow_ports_domain_and_overrides(self):
        values = {"fullnameOverride": "custom", "clusterDomain": "cluster.internal", "service": {"port": 7557},
                  "mesh": {"port": 7558, "seeds": ["other.ns.svc.cluster.internal:7558"]}, "diagnostics": {"port": 7559}}
        resources = render(values, release="payments", namespace="backend")
        node = yaml.safe_load(resources["ConfigMap"]["data"]["node.yaml"])
        self.assertEqual(node["discovery"]["group"], "backend/payments")
        self.assertEqual(node["discovery"]["advertise"], ["custom-headless.backend.svc.cluster.internal:7557"])
        self.assertEqual(node["discovery"]["seeds"], ["custom-mesh.backend.svc.cluster.internal:7558", "other.ns.svc.cluster.internal:7558"])
        self.assertEqual(node["listeners"]["application"], "[::]:7557")
        self.assertEqual(node["listeners"]["peer"], "[::]:7558")
        pod = resources["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["env"][1]["value"], "[$(POD_IP)]:7558")
        self.assertEqual(pod["containers"][0]["readinessProbe"]["exec"]["command"][-1], "127.0.0.1:7559")

    def test_three_replicas_keep_retirement_and_disruption_guards(self):
        values = {"replicaCount": 3, "podDisruptionBudget": {"enabled": True}}
        resources = render(values)
        deployment = resources["Deployment"]["spec"]
        self.assertEqual(deployment["replicas"], 3)
        self.assertEqual(deployment["minReadySeconds"], 15)
        pod = deployment["template"]["spec"]
        self.assertEqual(pod["terminationGracePeriodSeconds"], 45)
        self.assertEqual(pod["containers"][0]["lifecycle"]["preStop"]["sleep"]["seconds"], 15)
        self.assertEqual(deployment["strategy"]["rollingUpdate"]["maxSurge"], 0)
        self.assertEqual(deployment["strategy"]["rollingUpdate"]["maxUnavailable"], 1)
        self.assertEqual(resources["PodDisruptionBudget"]["spec"]["maxUnavailable"], 1)

    def test_metrics_generate_listener_and_one_per_pod_monitor(self):
        values = yaml.safe_load((ROOT / "examples/prometheus-autoscaling.yaml").read_text())
        resources = render(values, release="mongo", namespace="application")
        node = yaml.safe_load(resources["ConfigMap"]["data"]["node.yaml"])
        self.assertEqual(node["diagnostics"]["address"], "[::]:7449")
        self.assertTrue(node["diagnostics"]["allow_intranet"])
        monitor = resources["ServiceMonitor"]["spec"]
        selector = monitor["selector"]["matchLabels"]
        selected = [service for service in resources["Services"].values()
                    if all(service["metadata"]["labels"].get(k) == v for k, v in selector.items())]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["metadata"]["name"], "mongo-weir-metrics")
        endpoint = monitor["endpoints"][0]
        self.assertEqual(endpoint["port"], "metrics")
        self.assertEqual(endpoint["path"], "/metrics")
        labels = {rule["targetLabel"]: rule["replacement"] for rule in endpoint["relabelings"]}
        self.assertEqual(labels["namespace"], "application")
        self.assertEqual(labels["weir_group"], "application/mongo")
        self.assertEqual(labels["weir_mesh"], "weir")
        self.assertEqual(monitor["namespaceSelector"]["matchNames"], ["application"])
        self.assertEqual(resources["ServiceMonitor"]["metadata"]["labels"]["release"], "monitoring")

    def test_keda_controls_replica_count_and_preserves_standard_triggers(self):
        values = yaml.safe_load((ROOT / "examples/prometheus-autoscaling.yaml").read_text())
        values["autoscaling"]["triggers"][0]["authenticationRef"] = {"name": "external-auth"}
        resources = render(values, release="mongo", namespace="application")
        self.assertNotIn("replicas", resources["Deployment"]["spec"])
        scaled = resources["ScaledObject"]["spec"]
        self.assertEqual(scaled["scaleTargetRef"]["name"], "mongo-weir")
        self.assertEqual(scaled["minReplicaCount"], 2)
        self.assertEqual(scaled["maxReplicaCount"], 10)
        trigger = scaled["triggers"][0]
        self.assertEqual(trigger, values["autoscaling"]["triggers"][0])
        self.assertEqual(trigger["metricType"], "AverageValue")
        self.assertEqual(trigger["metadata"]["ignoreNullValues"], "false")
        self.assertIn('namespace="application",weir_group="application/mongo"', trigger["metadata"]["query"])
        self.assertNotIn("vector(0)", trigger["metadata"]["query"])
        self.assertEqual(scaled["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleDown"]["policies"][0]["value"], 1)
        cron = {"type": "cron", "metadata": {"timezone": "UTC", "start": "0 8 * * *", "end": "0 18 * * *", "desiredReplicas": "3"}}
        other = {"autoscaling": {"enabled": True, "triggers": [cron]}}
        self.assertEqual(render(other)["ScaledObject"]["spec"]["triggers"][0], cron)

    def test_promtool_regression_uses_the_actual_autoscaling_example_query(self):
        example = yaml.safe_load((ROOT / "examples/prometheus-autoscaling.yaml").read_text())
        query = example["autoscaling"]["triggers"][0]["metadata"]["query"]
        fixture = yaml.safe_load((ROOT / "tests/prometheus/autoscaling-query.test.yaml").read_text())
        for scenario in fixture["tests"]:
            for expression in scenario["promql_expr_test"]:
                self.assertEqual(expression["expr"], query)

    def test_inline_auth_userinfo_and_writable_mounts_are_rejected(self):
        for adapter, endpoint in (("mongodb", {"uri": "mongodb://mongo:27017"}), ("search", {"url": "https://search:9200"})):
            backend = {adapter: endpoint, "authentication": {"username": "test-user", "password": "test-password"}}
            invalid = {"stores": [{"name": adapter, "backend": backend}]}
            result = render(invalid, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("stores cannot contain backend credentials", result.stderr)
        for adapter, field, uri in (("mongodb", "uri", "mongodb://user:password@mongo:27017"), ("search", "url", "https://user:password@search:9200")):
            invalid = {"stores": [{"name": "store", "backend": {adapter: {field: uri}}}]}
            self.assertIn("userinfo is unsupported", render(invalid, success=False).stderr)
        allowed = {"stores": [{"name": "mongo", "backend": {"mongodb": {"uri": "mongodb://mongo:27017/?appName=weir@example"}}}]}
        self.assertIn("weir@example", render(allowed)["ConfigMap"]["data"]["routes.yaml"])
        invalid = {"extraVolumeMounts": [{"name": "extra", "mountPath": "/extra", "readOnly": False}]}
        self.assertNotEqual(render(invalid, success=False).returncode, 0)

    def test_removed_authentication_layouts_fail_before_rendering(self):
        backends = [
            {"mongodb": {"uri": "mongodb://mongo:27017", "username": "test-user", "password": "test-password"}},
            {"mongodb": {"uri": "mongodb://mongo:27017", "username_file": "/auth/username", "password_file": "/auth/password"}},
            {"search": {"url": "https://search:9200", "connection": {}}},
            {"search": {"url": "https://search:9200", "connection": None}},
        ]
        for field in ("username", "password", "username_file", "password_file"):
            for value in ("", None):
                backends.append({"mongodb": {"uri": "mongodb://mongo:27017", field: value}})
        for backend in backends:
            invalid = {"stores": [{"name": "store", "backend": backend}]}
            result = render(invalid, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("unsupported; use backend.authentication", result.stderr)

    def test_common_file_authentication_is_rendered_for_both_backends(self):
        stores = []
        for adapter, endpoint in (("mongodb", {"uri": "mongodb://mongo:27017/?authSource=admin&authMechanism=SCRAM-SHA-256&tls=true"}),
                                  ("search", {"url": "https://search:9200"})):
            backend = {adapter: endpoint, "authentication": {"username_file": "/auth/username", "password_file": "/auth/password"},
                       "tls": {"ca_file": "/auth/ca.crt"}}
            stores.append({"name": adapter, "backend": backend})
        values = {"stores": stores}
        routes = yaml.safe_load(render(values)["ConfigMap"]["data"]["routes.yaml"])
        self.assertEqual(routes["stores"], stores)

    def test_three_replica_overlay_excludes_untolerated_nodes_from_spread(self):
        values = yaml.safe_load((ROOT / "examples/three-replicas.yaml").read_text())
        constraints = render(values)["Deployment"]["spec"]["template"]["spec"]["topologySpreadConstraints"][0]
        self.assertEqual(constraints["nodeTaintsPolicy"], "Honor")
        self.assertEqual(constraints["minDomains"], 2)

    def test_recreate_and_disabled_pre_stop_are_rendered_cleanly(self):
        values = {"strategy": {"type": "Recreate"}, "preStopDelaySeconds": 0}
        deployment = render(values)["Deployment"]["spec"]
        self.assertNotIn("rollingUpdate", deployment["strategy"])
        self.assertNotIn("lifecycle", deployment["template"]["spec"]["containers"][0])

    def test_acceptance_annotations_preserve_existing_pod_discovery(self):
        values = yaml.safe_load((ROOT / "tests/acceptance/local-kind/values.yaml").read_text())
        values.update(yaml.safe_load((ROOT / "tests/acceptance/prometheus-values.yaml").read_text()))
        annotations = render(values)["Deployment"]["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["prometheus.io/path"], "/metrics")

    def test_ci_builds_the_recorded_source_contract(self):
        chart = yaml.safe_load((ROOT / "charts/weir/Chart.yaml").read_text())
        revision = chart["annotations"]["weir.batchstream.io/source-revision"]
        self.assertEqual(len(revision), 40)
        self.assertEqual(chart["appVersion"], revision[:12])
        self.assertEqual(render()["Deployment"]["spec"]["template"]["metadata"]["annotations"]["weir.batchstream.io/source-revision"], revision)
        for name in ("ci.yaml", "release.yaml"):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            steps = next(iter(workflow["jobs"].values()))["steps"]
            source = next(step for step in steps if step.get("with", {}).get("repository") == "batchstream/weir")
            self.assertEqual(source["with"]["ref"], revision)

    def test_invalid_values_fail_before_cluster_mutation(self):
        store = {"name": "mongo", "backend": {"mongodb": {"uri": "mongodb://mongo:27017"}}}
        trigger = {"type": "cron", "metadata": {"timezone": "UTC"}}
        cases = [
            {"metrics": {"enabled": True}}, {"metrics": {"serviceMonitor": {"enabled": True}}},
            {"metrics": {"serviceMonitor": {"labels": {"app.kubernetes.io/instance": "other"}}}},
            {"replicaCount": 0}, {"replicaCount": 32}, {"unknownSetting": True},
            {"minReadySeconds": -1}, {"minReadySeconds": 180}, {"terminationGracePeriodSeconds": 20},
            {"preStopDelaySeconds": -1}, {"image": {"digest": "sha256:bad"}}, {"image": {"digest": ""}},
            {"podAnnotations": {"weir.batchstream.io/source-revision": "unverified"}},
            {"config": {"existingSecret": "old"}}, {"peer": {"enabled": True}},
            {"nodeConfig": {"listeners": {}}}, {"nodeConfig": {"discovery": {}}},
            {"nodeConfig": {"diagnostics": {"address": "127.0.0.1:1234"}}},
            {"nodeConfig": {"diagnostics": {"allow_intranet": True}}},
            {"mesh": {"name": "INVALID"}}, {"mesh": {"enabled": False, "seeds": ["other:7448"]}},
            {"stores": [store, store]}, {"stores": [store] * 17}, {"stores": [store], "storesSecret": "external"},
            {"autoscaling": {"enabled": True}}, {"autoscaling": {"minReplicaCount": 0}},
            {"autoscaling": {"maxReplicaCount": 32}},
            {"autoscaling": {"enabled": True, "minReplicaCount": 3, "maxReplicaCount": 2, "triggers": [trigger]}},
            {"podDisruptionBudget": {"enabled": True}},
            {"podDisruptionBudget": {"enabled": True}, "autoscaling": {"enabled": True, "minReplicaCount": 1, "triggers": [trigger]}},
            {"service": {"port": 7449}}, {"mesh": {"port": 7447}}, {"mesh": {"port": 7449}},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                result = render(invalid, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
