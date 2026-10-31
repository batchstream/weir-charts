import copy
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("contract", ROOT / "scripts/check-contract.py")
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


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
    resources["ConfigMaps"] = {item["metadata"]["name"]: item for item in documents if item["kind"] == "ConfigMap"}
    resources["ConfigMap"] = next(item for item in documents if item["kind"] == "ConfigMap" and "weir.yaml" in item["data"])
    return resources


def render_notes(values, release, namespace):
    """Render NOTES.txt without a cluster by embedding it in a copied chart."""
    with tempfile.TemporaryDirectory() as directory:
        chart = Path(directory) / "weir"
        shutil.copytree(ROOT / "charts/weir", chart)
        notes = (chart / "templates/NOTES.txt").read_text()
        indented = "".join("  " + line for line in notes.splitlines(keepends=True))
        (chart / "templates/rendered-notes.yaml").write_text("notes: |\n" + indented)
        values_file = Path(directory) / "values.json"
        values_file.write_text(json.dumps(values))
        command = ["helm", "template", release, str(chart), "--namespace", namespace,
                   "--show-only", "templates/rendered-notes.yaml",
                   "-f", str(ROOT / "tests/values.yaml"), "-f", str(values_file)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    return yaml.safe_load(result.stdout)["notes"]


def promtool():
    """Return the promtool binary named by PROMTOOL or found on PATH, if any."""
    named = os.environ.get("PROMTOOL")
    if named:
        return named
    return shutil.which("promtool")


def weir_config(resources):
    return yaml.safe_load(resources["ConfigMap"]["data"]["weir.yaml"])


def strict_object_paths(schema, path=()):
    """Return the value paths of every schema object that rejects unknown keys."""
    paths = []
    if schema.get("additionalProperties") is False:
        paths.append(path)
    for name, child in schema.get("properties", {}).items():
        paths.extend(strict_object_paths(child, path + (name,)))
    if "items" in schema:
        paths.extend(strict_object_paths(schema["items"], path + ("[]",)))
    return paths


def nest(path, leaf):
    """Wrap leaf in the mappings and lists named by a strict object path."""
    value = leaf
    for key in reversed(path):
        if key == "[]":
            value = [value]
        else:
            value = {key: value}
    return value


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
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], "weir-weir")
        self.assertEqual(container["args"], ["serve", "--config", "/etc/weir/weir.yaml"])
        for name, path in (("startupProbe", "/readyz"), ("readinessProbe", "/readyz"), ("livenessProbe", "/livez")):
            self.assertEqual(container[name]["httpGet"], {"path": path, "port": "metrics"})
            self.assertNotIn("periodSeconds", container[name])
            self.assertNotIn("timeoutSeconds", container[name])
        self.assertNotIn("failureThreshold", container["readinessProbe"])
        self.assertNotIn("failureThreshold", container["livenessProbe"])
        self.assertNotIn("revisionHistoryLimit", resources["Deployment"]["spec"])
        self.assertIn("@sha256:", container["image"])
        listeners = {"application": "[::]:7447", "diagnostics": "[::]:7449"}
        self.assertEqual(weir_config(resources), {"listeners": listeners, "shutdown_timeout": "5s", "stores": []})

    def test_group_services_select_only_their_release(self):
        first = render(release="mongo", namespace="application")
        second = render(release="search", namespace="application")
        for release, resources in (("mongo", first), ("search", second)):
            self.assertEqual(set(resources["Services"]), {release + "-weir"})
            service = resources["Services"][release + "-weir"]["spec"]
            self.assertEqual(service["selector"]["app.kubernetes.io/instance"], release)
            self.assertEqual(service["clusterIP"], "None")
            self.assertEqual([p["name"] for p in service["ports"]], ["grpc"])
        self.assertNotEqual(first["Services"].keys(), second["Services"].keys())

    def test_network_policy_admits_clients_on_the_named_grpc_port_and_dns_only(self):
        values = {"service": {"port": 7557}}
        resources = render(values)
        policy = resources["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(len(policy["ingress"]), 1)
        self.assertEqual(len(policy["egress"]), 1)
        self.assertEqual(policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["weir-client"], "true")
        self.assertEqual(policy["ingress"][0]["ports"][0]["port"], "grpc")
        container_ports = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]["ports"]
        self.assertEqual(next(port["containerPort"] for port in container_ports if port["name"] == "grpc"), 7557)

    def test_configuration_changes_roll_pods(self):
        a, b = render({"shutdownTimeoutSeconds": 5}), render({"shutdownTimeoutSeconds": 9})
        self.assertEqual(weir_config(b)["shutdown_timeout"], "9s")
        checksum_a = a["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        checksum_b = b["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        self.assertNotEqual(checksum_a, checksum_b)
        store_values = {"stores": [{"name": "mongo", "mongodb": {"uri": "mongodb://mongo:27017"}}]}
        changed = render(store_values)
        self.assertNotEqual(checksum_a, changed["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"])
        self.assertNotIn("Secret", changed)

    def test_derived_identity_and_addresses_follow_ports_and_overrides(self):
        values = {"fullnameOverride": "custom", "service": {"port": 7557}, "diagnosticsPort": 7559}
        resources = render(values, release="payments", namespace="backend")
        self.assertEqual(weir_config(resources)["listeners"], {"application": "[::]:7557", "diagnostics": "[::]:7559"})
        self.assertEqual(set(resources["Services"]), {"custom"})
        notes = render_notes(values, release="payments", namespace="backend")
        self.assertIn("SDK address (weir.New), resolving to this group's Ready Pods:\ncustom.backend.svc:7557\n", notes)
        pod = resources["Deployment"]["spec"]["template"]["spec"]
        self.assertNotIn("env", pod["containers"][0])
        ports = {port["name"]: port["containerPort"] for port in pod["containers"][0]["ports"]}
        self.assertEqual(ports, {"grpc": 7557, "metrics": 7559})
        self.assertEqual(pod["containers"][0]["readinessProbe"]["httpGet"]["port"], "metrics")

    def test_three_replicas_keep_retirement_and_disruption_guards(self):
        values = {"replicaCount": 3, "podDisruptionBudget": {"enabled": True}}
        resources = render(values)
        deployment = resources["Deployment"]["spec"]
        self.assertEqual(deployment["replicas"], 3)
        self.assertEqual(deployment["minReadySeconds"], 15)
        pod = deployment["template"]["spec"]
        self.assertEqual(pod["terminationGracePeriodSeconds"], 23)
        self.assertEqual(pod["containers"][0]["lifecycle"]["preStop"]["sleep"]["seconds"], 15)
        self.assertEqual(deployment["strategy"]["rollingUpdate"]["maxSurge"], 0)
        self.assertEqual(deployment["strategy"]["rollingUpdate"]["maxUnavailable"], 1)
        self.assertEqual(resources["PodDisruptionBudget"]["spec"]["maxUnavailable"], 1)

    def test_metrics_generate_listener_and_one_per_pod_monitor(self):
        values = yaml.safe_load((ROOT / "examples/prometheus-autoscaling.yaml").read_text())
        resources = render(values, release="mongo", namespace="application")
        self.assertIn("mongo-weir-metrics", resources["Services"])
        disabled = render(dict(values, metrics={"enabled": False}, autoscaling={"enabled": False}, podDisruptionBudget={"enabled": False}), release="mongo", namespace="application")
        self.assertNotIn("mongo-weir-metrics", disabled["Services"])
        self.assertEqual(len(disabled["NetworkPolicy"]["spec"]["ingress"]), len(resources["NetworkPolicy"]["spec"]["ingress"]) - len(values["metrics"]["ingress"]))
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
        self.assertEqual(set(labels), {"namespace", "weir_group"})
        self.assertEqual(monitor["namespaceSelector"]["matchNames"], ["application"])
        self.assertEqual(resources["ServiceMonitor"]["metadata"]["labels"]["release"], "monitoring")
        ingress = resources["NetworkPolicy"]["spec"]["ingress"]
        self.assertEqual(ingress[-len(values["metrics"]["ingress"]):], values["metrics"]["ingress"])
        self.assertEqual(ingress[-1]["ports"], [{"protocol": "TCP", "port": "metrics"}])

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

    def test_many_stores_and_replicas_render(self):
        stores = []
        for index in range(CONTRACT.SCALE_STORES):
            store = {"name": "store" + str(index), "mongodb": {"uri": "mongodb://127.0.0.1:27017"}}
            stores.append(store)
        values = {"stores": stores, "replicaCount": CONTRACT.SCALE_REPLICAS}
        resources = render(values)
        self.assertEqual(weir_config(resources)["stores"], stores)
        self.assertEqual(resources["Deployment"]["spec"]["replicas"], CONTRACT.SCALE_REPLICAS)
        metadata = {"timezone": "UTC", "start": "0 8 * * *", "end": "0 18 * * *",
                    "desiredReplicas": str(CONTRACT.SCALE_REPLICAS)}
        trigger = {"type": "cron", "metadata": metadata}
        autoscaling = {"enabled": True, "minReplicaCount": CONTRACT.SCALE_MIN_REPLICAS,
                       "maxReplicaCount": CONTRACT.SCALE_REPLICAS, "triggers": [trigger]}
        scaling = {"autoscaling": autoscaling}
        scaled = render(scaling)["ScaledObject"]["spec"]
        self.assertEqual(scaled["minReplicaCount"], CONTRACT.SCALE_MIN_REPLICAS)
        self.assertEqual(scaled["maxReplicaCount"], CONTRACT.SCALE_REPLICAS)

    def test_extended_lifecycle_timers_follow_native_progress_relation(self):
        values = {"minReadySeconds": 200, "progressDeadlineSeconds": 600, "terminationGracePeriodSeconds": 600,
                  "preStopDelaySeconds": 400}
        resources = render(values)
        deployment = resources["Deployment"]["spec"]
        pod = deployment["template"]["spec"]
        self.assertEqual(deployment["minReadySeconds"], 200)
        probe = pod["containers"][0]["startupProbe"]
        startup_budget = probe["failureThreshold"] * 10
        self.assertEqual(deployment["progressDeadlineSeconds"], 600)
        self.assertEqual(pod["terminationGracePeriodSeconds"], 600)
        self.assertEqual(pod["containers"][0]["lifecycle"]["preStop"]["sleep"]["seconds"], 400)
        # Both Kubernetes Deployment timing fields are int32 and the progress
        # deadline must strictly exceed minReadySeconds.
        values["minReadySeconds"] = 2147483646 - startup_budget
        values["progressDeadlineSeconds"] = 2147483647
        self.assertEqual(render(values)["Deployment"]["spec"]["progressDeadlineSeconds"], 2147483647)
        self.assertEqual(render()["Deployment"]["spec"]["progressDeadlineSeconds"], 180)

        values["minReadySeconds"] += 1
        rejected = render(values, success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("must exceed", rejected.stderr)

        values = {"progressDeadlineSeconds": 66}
        self.assertEqual(render(values)["Deployment"]["spec"]["progressDeadlineSeconds"], 66)
        values["progressDeadlineSeconds"] = 65
        self.assertNotEqual(render(values, success=False).returncode, 0)

    def test_shutdown_timeout_drives_weir_and_pod_grace(self):
        values = {"shutdownTimeoutSeconds": 120, "preStopDelaySeconds": 30}
        resources = render(values)
        deployment = resources["Deployment"]["spec"]
        pod = deployment["template"]["spec"]
        # The first probe can land at once, so even the earliest kill must
        # follow Weir's fixed 30s startup bound with a full period to spare.
        threshold = pod["containers"][0]["startupProbe"]["failureThreshold"]
        self.assertEqual(threshold, 5)
        self.assertGreater((threshold - 1) * 10, 30)
        self.assertEqual(pod["terminationGracePeriodSeconds"], 153)
        self.assertEqual(weir_config(resources)["shutdown_timeout"], "120s")

        values["terminationGracePeriodSeconds"] = 180
        self.assertEqual(render(values)["Deployment"]["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 180)
        values["terminationGracePeriodSeconds"] = 152
        rejected = render(values, success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("at least 153", rejected.stderr)

    def test_shutdown_timeout_uses_native_integer_seconds(self):
        for seconds in (0, -5, 1.5, "5s", 9223372037):
            with self.subTest(seconds=seconds):
                values = {"shutdownTimeoutSeconds": seconds}
                rejected = render(values, success=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("shutdownTimeoutSeconds", rejected.stderr)
                self.assertEqual(rejected.stdout, "")

        values = {"shutdownTimeoutSeconds": 1000000000}
        self.assertEqual(weir_config(render(values))["shutdown_timeout"], "1000000000s")

    def test_promtool_regression_uses_the_actual_autoscaling_example_query(self):
        example = yaml.safe_load((ROOT / "examples/prometheus-autoscaling.yaml").read_text())
        query = example["autoscaling"]["triggers"][0]["metadata"]["query"]
        fixture = yaml.safe_load((ROOT / "tests/prometheus/autoscaling-query.test.yaml").read_text())
        for scenario in fixture["tests"]:
            for expression in scenario["promql_expr_test"]:
                self.assertEqual(expression["expr"], query)

    def test_alerts_and_dashboard_select_only_this_group(self):
        values = yaml.safe_load((ROOT / "examples/prometheus-alerts.yaml").read_text())
        resources = render(values, release="mongo", namespace="application")
        selector = 'namespace="application",weir_group="application/mongo"'
        rule = resources["PrometheusRule"]
        self.assertEqual(rule["metadata"]["labels"]["release"], "monitoring")
        alerts = rule["spec"]["groups"][0]["rules"]
        self.assertEqual(len({alert["alert"] for alert in alerts}), len(alerts))
        for alert in alerts:
            with self.subTest(alert=alert["alert"]):
                self.assertIn(selector, alert["expr"])
                self.assertIn(alert["labels"]["severity"], {"critical", "warning"})
                self.assertEqual(alert["labels"]["namespace"], "application")
                self.assertEqual(alert["labels"]["weir_group"], "application/mongo")
                self.assertTrue(alert["annotations"]["runbook_url"].endswith("#" + alert["alert"].lower()))
                self.assertIn("application/mongo", alert["annotations"]["summary"])
        configuration = resources["ConfigMaps"]["mongo-weir-dashboard"]
        self.assertEqual(configuration["metadata"]["labels"]["grafana_dashboard"], "1")
        dashboard = json.loads(configuration["data"]["weir.json"])
        self.assertEqual(dashboard["title"], "Weir application/mongo")
        self.assertLessEqual(len(dashboard["uid"]), 40)
        other = render(values, release="search", namespace="application")["ConfigMaps"]["search-weir-dashboard"]
        self.assertNotEqual(json.loads(other["data"]["weir.json"])["uid"], dashboard["uid"])
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                with self.subTest(panel=panel["title"], query=target["refId"]):
                    self.assertIn(selector, target["expr"])
                    self.assertNotIn("WEIR_", target["expr"])
        disabled = render({"metrics": {"enabled": False}, "networkPolicy": {"enabled": False}})
        self.assertNotIn("PrometheusRule", disabled)

    def test_promtool_checks_the_rendered_alerts_and_the_autoscaling_query(self):
        binary = promtool()
        if not binary:
            self.skipTest("promtool is not installed; CI sets PROMTOOL")
        values = yaml.safe_load((ROOT / "examples/prometheus-alerts.yaml").read_text())
        rule = render(values, release="mongo", namespace="application")["PrometheusRule"]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "alerts.test.yaml"
            shutil.copy(ROOT / "tests/prometheus/alerts.test.yaml", fixture)
            (Path(directory) / "alerts.rules.yaml").write_text(yaml.safe_dump(rule["spec"]))
            fixtures = [str(fixture), str(ROOT / "tests/prometheus/autoscaling-query.test.yaml")]
            result = subprocess.run([binary, "test", "rules", *fixtures], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        scenarios = yaml.safe_load((ROOT / "tests/prometheus/alerts.test.yaml").read_text())["tests"]
        tested = {test["alertname"] for scenario in scenarios for test in scenario.get("alert_rule_test", [])}
        self.assertEqual(tested, {alert["alert"] for alert in rule["spec"]["groups"][0]["rules"]})

    def test_inline_auth_userinfo_and_writable_mounts_are_rejected(self):
        for adapter, endpoint in (("mongodb", {"uri": "mongodb://mongo:27017"}), ("search", {"url": "https://search:9200"})):
            store = {"name": adapter, adapter: endpoint, "authentication": {"username": "test-user", "password": "test-password"}}
            invalid = {"stores": [store]}
            result = render(invalid, success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("Additional property", result.stderr)
        for adapter, field, uri in (
                ("mongodb", "uri", "mongodb://user:password@mongo:27017"),
                ("mongodb", "uri", "mongodb://admin:p#ss@mongo:27017/?directConnection=true"),
                ("mongodb", "uri", "mongodb://admin:p?ss@mongo:27017/"),
                ("mongodb", "uri", "mongodb://admin:p/ss@mongo:27017/"),
                ("mongodb", "uri", "mongodb://admin:p/?ss@mongo:27017/"),
                ("search", "url", "https://user:password@search:9200"),
                ("search", "url", "https://user:p#ss@search:9200")):
            with self.subTest(uri=uri):
                invalid = {"stores": [{"name": "store", adapter: {field: uri}}]}
                result = render(invalid, success=False)
                self.assertIn("userinfo is unsupported", result.stderr)
                self.assertNotIn("ss@", result.stderr)
        invalid = {"extraVolumeMounts": [{"name": "extra", "mountPath": "/extra", "readOnly": False}]}
        self.assertNotEqual(render(invalid, success=False).returncode, 0)

    def test_store_configuration_rejects_unknown_structure(self):
        mongo = {"uri": "mongodb://mongo:27017"}
        stores = [
            {"mongodb": {"uri": "mongodb://mongo:27017", "unexpected": True}},
            {"search": {"url": "https://search:9200", "unexpected": None}},
            {"mongodb": mongo, "unexpected": {}},
            {"mongodb": mongo, "search": {"url": "http://search:9200"}},
            {"mongodb": mongo, "batching": {"unexpected": 1}},
            {"mongodb": mongo, "authentication": {"username_file": "/auth/username"}},
            {"mongodb": mongo, "authentication": {"password_file": "/auth/password"}},
            {"mongodb": mongo, "authentication": {"username_file": "", "password_file": "/auth/password"}},
            {"mongodb": mongo, "tls": {}},
        ]
        for store in stores:
            with self.subTest(store=store):
                invalid = {"stores": [dict(store, name="store")]}
                result = render(invalid, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_batching_settings_pass_through_without_default_overrides(self):
        stores = [{"name": "mongo", "mongodb": {"uri": "mongodb://mongo:27017"}, "batching": CONTRACT.batching_policy()},
                  {"name": "search", "search": {"url": "http://search:9200"}, "batching": {"max_wait": "5ms"}}]
        values = {"stores": stores}
        self.assertEqual(weir_config(render(values))["stores"], stores)

    def test_common_file_authentication_is_rendered_for_both_backends(self):
        stores = []
        for adapter, endpoint in (("mongodb", {"uri": "mongodb://mongo:27017/?authSource=admin&authMechanism=SCRAM-SHA-256&tls=true"}),
                                  ("search", {"url": "https://search:9200"})):
            store = {"name": adapter, adapter: endpoint,
                     "authentication": {"username_file": "/auth/username", "password_file": "/auth/password"},
                     "tls": {"ca_file": "/auth/ca.crt"}}
            stores.append(store)
        values = {"stores": stores}
        self.assertEqual(weir_config(render(values))["stores"], stores)

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

    def test_pod_annotations_merge_with_chart_managed_annotations(self):
        pod_annotations = {"prometheus.io/scrape": "true", "prometheus.io/port": "7449", "prometheus.io/path": "/metrics"}
        values = {"podAnnotations": pod_annotations}
        annotations = render(values)["Deployment"]["spec"]["template"]["metadata"]["annotations"]
        for key, value in pod_annotations.items():
            self.assertEqual(annotations[key], value)
        self.assertIn("checksum/config", annotations)
        self.assertIn("weir.batchstream.io/source-revision", annotations)

    def test_runtime_fixture_is_loopback_only_and_rejects_stores(self):
        config = render()["ConfigMap"]
        resources = [copy.deepcopy(config)]
        CONTRACT.isolate_runtime(resources, 7447, 7449)
        isolated = yaml.safe_load(resources[0]["data"]["weir.yaml"])
        self.assertEqual(isolated["listeners"], {"application": "127.0.0.1:7447", "diagnostics": "127.0.0.1:7449"})
        generated = yaml.safe_load(config["data"]["weir.yaml"])
        self.assertEqual(generated["listeners"], {"application": "[::]:7447", "diagnostics": "[::]:7449"})
        for case in ("stores", "listener", "diagnostics"):
            with self.subTest(case=case):
                unsafe = copy.deepcopy(config)
                document = yaml.safe_load(unsafe["data"]["weir.yaml"])
                if case == "stores":
                    document["stores"] = [{"name": "forbidden"}]
                elif case == "listener":
                    document["listeners"]["extra"] = "127.0.0.1:7448"
                else:
                    document["listeners"]["diagnostics"] = "[::]:7450"
                unsafe["data"]["weir.yaml"] = yaml.safe_dump(document)
                resources = [unsafe]
                with self.assertRaises(RuntimeError):
                    CONTRACT.isolate_runtime(resources, 7447, 7449)

    def test_runtime_logging_failures_still_reap_child_and_check_ports(self):
        for error_type in (BrokenPipeError, OSError):
            for persistent in (False, True):
                with self.subTest(error=error_type.__name__, persistent=persistent):
                    failure = error_type("injected stdout failure")
                    print_failures = failure if persistent else [failure, None, None]
                    process = mock.Mock()
                    process.pid = 12345
                    process.poll.return_value = None
                    process.wait.return_value = 0
                    application, diagnostics = mock.Mock(), mock.Mock()
                    application.getsockname.return_value = ("127.0.0.1", 18001)
                    diagnostics.getsockname.return_value = ("127.0.0.1", 18002)
                    reservations = [application, diagnostics]
                    released = [mock.MagicMock(), mock.MagicMock()]
                    for listener in released:
                        listener.__enter__.return_value = listener
                    resources = []
                    validation = ({}, ["serve"])
                    directory = Path("/unused-mocked-runtime")
                    with (
                        mock.patch.object(CONTRACT, "reserve_port", side_effect=reservations),
                        mock.patch.object(CONTRACT, "render", return_value=resources),
                        mock.patch.object(CONTRACT, "validate_config", return_value=validation),
                        mock.patch.object(CONTRACT, "isolate_runtime"),
                        mock.patch.object(CONTRACT.tempfile, "TemporaryFile"),
                        mock.patch.object(CONTRACT.subprocess, "Popen", return_value=process) as popen,
                        mock.patch.object(CONTRACT, "healthy") as probe,
                        mock.patch.object(CONTRACT.socket, "socket", side_effect=released),
                        mock.patch("builtins.print", side_effect=print_failures),
                    ):
                        with self.assertRaises(error_type) as raised:
                            CONTRACT.check_runtime("mock-weir", directory)
                    self.assertIs(raised.exception, failure)
                    popen.assert_called_once()
                    probe.assert_not_called()
                    application.close.assert_called_once_with()
                    diagnostics.close.assert_called_once_with()
                    process.terminate.assert_called_once_with()
                    process.wait.assert_called_once_with(timeout=10)
                    process.kill.assert_not_called()
                    for listener, port in zip(released, (18001, 18002)):
                        address = ("127.0.0.1", port)
                        listener.bind.assert_called_once_with(address)
                        listener.__exit__.assert_called_once()

    def test_chart_identity_matches_pinned_image(self):
        revision = "b89ae1e5e8b073176a9f36a6bb264e3a950c41ac"
        digest = "sha256:e1f29a7747b5a4ff3a1eb033854b6da50b350cf919aa2cde000dc25463a93c2f"
        chart = yaml.safe_load((ROOT / "charts/weir/Chart.yaml").read_text())
        values = yaml.safe_load((ROOT / "charts/weir/values.yaml").read_text())
        self.assertEqual(chart["version"], "0.14.0-rc.1")
        self.assertEqual(chart["annotations"]["weir.batchstream.io/source-revision"], revision)
        self.assertEqual(values["image"]["repository"], "ghcr.io/batchstream/weir")
        self.assertEqual(values["image"]["digest"], digest)
        image = values["image"]["repository"] + "@" + digest
        readme = (ROOT / "README.md").read_text()
        self.assertIn("https://github.com/batchstream/weir/commit/" + revision, readme)
        self.assertIn("`" + image + "`", readme)
        # Unlike the general render helper, validate the actual deployable default
        # without tests/values.yaml's deliberately non-deployable image fixture.
        command = ["helm", "template", "weir", str(ROOT / "charts/weir")]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        deployment = next(item for item in yaml.safe_load_all(result.stdout) if item and item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"], image)
        self.assertEqual(deployment["metadata"]["labels"]["helm.sh/chart"], "weir-" + chart["version"])
        self.assertEqual(deployment["metadata"]["labels"]["app.kubernetes.io/version"], chart["appVersion"])

    def test_ci_installs_the_recorded_source_contract(self):
        chart = yaml.safe_load((ROOT / "charts/weir/Chart.yaml").read_text())
        revision = chart["annotations"]["weir.batchstream.io/source-revision"]
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertEqual(chart["appVersion"], revision[:12])
        self.assertEqual(render()["Deployment"]["spec"]["template"]["metadata"]["annotations"]["weir.batchstream.io/source-revision"], revision)
        for name in ("ci.yaml", "release.yaml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
                steps = next(iter(workflow["jobs"].values()))["steps"]
                commands = [step.get("run", "") for step in steps]
                install = 'GOBIN="$PWD/.bin" go install github.com/batchstream/weir/cmd/weir@' + revision
                self.assertIn(install, commands)
                make = next(command for command in commands if command.startswith("make "))
                self.assertTrue(make.endswith('WEIR_BIN="$PWD/.bin/weir"'), make)
                make_step = next(step for step in steps if step.get("run", "").startswith("make "))
                self.assertEqual(make_step["env"]["PROMTOOL"], "${{ github.workspace }}/.bin/promtool")
                self.assertTrue(any("promtool" in command and "sha256sum -c" in command for command in commands))

    def test_unknown_keys_are_rejected_in_every_strict_object(self):
        schema = json.loads((ROOT / "charts/weir/values.schema.json").read_text())
        paths = strict_object_paths(schema)
        expected = [(), ("image",), ("stores", "[]"), ("stores", "[]", "tls"), ("stores", "[]", "batching"),
                    ("autoscaling", "scaleDown", "policies", "[]")]
        for path in expected:
            self.assertIn(path, paths)
        for path in paths:
            with self.subTest(path=".".join(path)):
                unknown = {"unknownSetting": 1}
                invalid = nest(path, unknown)
                result = render(invalid, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("unknownSetting", result.stderr)

    def test_invalid_values_fail_before_cluster_mutation(self):
        store = {"name": "mongo", "mongodb": {"uri": "mongodb://mongo:27017"}}
        trigger = {"type": "cron", "metadata": {"timezone": "UTC"}}
        cases = [
            {"metrics": {"enabled": True}}, {"metrics": {"serviceMonitor": {"enabled": True}}},
            {"metrics": {"serviceMonitor": {"labels": {"app.kubernetes.io/instance": "other"}}}},
            {"metrics": {"prometheusRule": {"enabled": True}}}, {"metrics": {"grafanaDashboard": {"enabled": True}}},
            {"metrics": {"prometheusRule": {"labels": {"helm.sh/chart": "other"}}}},
            {"metrics": {"grafanaDashboard": {"labels": {"app.kubernetes.io/name": "other"}}}},
            {"replicaCount": 0},
            {"minReadySeconds": -1}, {"minReadySeconds": 2147483646}, {"minReadySeconds": 2147483647}, {"terminationGracePeriodSeconds": 20},
            {"progressDeadlineSeconds": 0}, {"progressDeadlineSeconds": 2147483648},
            {"preStopDelaySeconds": -1}, {"image": {"digest": "sha256:bad"}}, {"image": {"digest": ""}},
            {"shutdownTimeoutSeconds": 0},
            {"podAnnotations": {"weir.batchstream.io/source-revision": "unverified"}},
            {"podAnnotations": {"checksum/config": "unverified"}},
            {"stores": [store, store]},
            {"autoscaling": {"enabled": True}}, {"autoscaling": {"minReplicaCount": 0}},
            {"autoscaling": {"enabled": True, "minReplicaCount": 3, "maxReplicaCount": 2, "triggers": [trigger]}},
            {"podDisruptionBudget": {"enabled": True}},
            {"podDisruptionBudget": {"enabled": True}, "autoscaling": {"enabled": True, "minReplicaCount": 1, "triggers": [trigger]}},
            {"service": {"port": 7449}},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                result = render(invalid, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
