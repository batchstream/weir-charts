"""Validate rendered config and serving against an explicitly selected Weir binary.

No backend connections or existing Secret files are read. The only live process
is an isolated no-Store node on loopback ephemeral ports.
"""
import argparse
import copy
import http.client
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCALE_STORES = 50
SCALE_REPLICAS = 100
SCALE_MIN_REPLICAS = 50


def render(values, release="weir"):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(values, handle)
        handle.flush()
        command = ["helm", "template", release, str(ROOT / "charts/weir"),
                   "-f", str(ROOT / "tests/values.yaml"), "-f", handle.name]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    return resources


def validate_config(binary, resources, directory):
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    (directory / "weir.yaml").write_text(config["data"]["weir.yaml"])
    args = [str(directory / Path(arg).name) if arg.startswith("/etc/weir/") else arg
            for arg in container["args"]]
    command = [binary, "check", *args[1:]]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    if result.stdout.strip() != "configuration valid":
        raise RuntimeError("unexpected configuration validation response")
    return container, args


def batching_policy():
    """Return valid values for every batching key."""
    policy = {"max_operations": 64, "max_bytes": "2MiB", "max_wait": "0s", "max_in_flight": 8}
    return policy


def check_scale(binary, directory):
    """Validate a group with many Stores and replicas."""
    stores = []
    for index in range(SCALE_STORES):
        store = {"name": "store" + str(index), "mongodb": {"uri": "mongodb://127.0.0.1:27017/?directConnection=true"}}
        if index % 2:
            store = {"name": "store" + str(index), "search": {"url": "http://127.0.0.1:9200"}}
        stores.append(store)
    values = {"stores": stores, "replicaCount": SCALE_REPLICAS}
    resources = render(values)
    validate_config(binary, resources, directory)
    metadata = {"timezone": "UTC", "start": "0 8 * * *", "end": "0 18 * * *",
                "desiredReplicas": str(SCALE_REPLICAS)}
    trigger = {"type": "cron", "metadata": metadata}
    autoscaling = {"enabled": True, "minReplicaCount": SCALE_MIN_REPLICAS, "maxReplicaCount": SCALE_REPLICAS,
                   "triggers": [trigger]}
    values["autoscaling"] = autoscaling
    scaled = render(values)
    validate_config(binary, scaled, directory)


def reserve_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener


def isolate_runtime(resources, app_port, diagnostic_port):
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    document = yaml.safe_load(config["data"]["weir.yaml"])
    generated = {"application": "[::]:" + str(app_port), "diagnostics": "[::]:" + str(diagnostic_port)}
    if document["stores"] or document["listeners"] != generated:
        raise RuntimeError("runtime fixture must have no Stores and only the generated listeners")
    # Static checks cover the generated wildcard listeners. Never expose the live
    # no-Store fixture outside loopback, even on a developer's network.
    document["listeners"] = {"application": "127.0.0.1:" + str(app_port),
                             "diagnostics": "127.0.0.1:" + str(diagnostic_port)}
    config["data"]["weir.yaml"] = yaml.safe_dump(document)


def healthy(container, probe):
    """Run one rendered httpGet probe on its named container port over loopback, bypassing any proxy."""
    ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:" + str(ports[probe["httpGet"]["port"]]) + probe["httpGet"]["path"]
    try:
        with opener.open(url, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError):
        return False


def check_runtime(binary, directory):
    # Keep both reservations open until immediately before startup.
    application = reserve_port()
    diagnostics = reserve_port()
    app_port = application.getsockname()[1]
    diagnostic_port = diagnostics.getsockname()[1]
    values = {"service": {"port": app_port}, "diagnosticsPort": diagnostic_port}
    resources = render(values)
    validate_config(binary, resources, directory)
    isolate_runtime(resources, app_port, diagnostic_port)
    container, args = validate_config(binary, resources, directory)
    application.close()
    diagnostics.close()
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen([binary, *args], stdout=output, stderr=output)
        try:
            print("Isolated no-Store runtime PID " + str(process.pid) + ", loopback ports "
                  + str(app_port) + "/" + str(diagnostic_port), flush=True)
            deadline = time.monotonic() + 10
            while not healthy(container, container["startupProbe"]):
                if process.poll() is not None or time.monotonic() >= deadline:
                    output.seek(0)
                    raise RuntimeError("rendered serve/startup probe failed: " + output.read())
                time.sleep(0.05)
            for name in ("readinessProbe", "livenessProbe"):
                if not healthy(container, container[name]):
                    raise RuntimeError("rendered " + name + " failed")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise RuntimeError("Weir did not drain after SIGTERM")
            for port in (app_port, diagnostic_port):
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("127.0.0.1", port))
            print("Isolated runtime PID " + str(process.pid) + " exited " + str(exit_code), flush=True)
            print("Isolated runtime loopback ports released: " + str(app_port) + "/" + str(diagnostic_port), flush=True)
        if exit_code != 0:
            output.seek(0)
            raise RuntimeError("Weir shutdown failed: " + output.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    options = parser.parse_args()
    binary = shutil.which(options.binary)
    if binary is None:
        parser.error("--binary must identify a built Weir executable")
    binary = str(Path(binary).resolve())
    fixtures = [("weir", yaml.safe_load((ROOT / "examples/mongo.yaml").read_text())),
                ("search", yaml.safe_load((ROOT / "examples/search.yaml").read_text())),
                ("weir", yaml.safe_load((ROOT / "tests/three-replica-values.yaml").read_text()))]
    with tempfile.TemporaryDirectory(prefix="weir-chart-contract-") as temporary:
        directory = Path(temporary)
        for release, values in fixtures:
            resources = render(values, release)
            validate_config(binary, resources, directory)
            custom = copy.deepcopy(values)
            custom.update(fullnameOverride="custom", shutdownTimeoutSeconds=9)
            for store in custom["stores"]:
                store["batching"] = batching_policy()
            validate_config(binary, render(custom, release), directory)
            replicas = copy.deepcopy(values)
            replicas.update(yaml.safe_load((ROOT / "examples/three-replicas.yaml").read_text()))
            validate_config(binary, render(replicas, release), directory)
        # Credential files resolve relative to the generated configuration file.
        files = copy.deepcopy(fixtures[0][1])
        store = files["stores"][0]
        store["mongodb"]["uri"] = ("mongodb://mongo:27017/?directConnection=true"
                                   "&authMechanism=SCRAM-SHA-256&authSource=admin&tls=true")
        store["authentication"] = {"username_file": "test-user.txt", "password_file": "test-password.txt"}
        (directory / "test-user.txt").write_text("test-user\n")
        (directory / "test-password.txt").write_text("test-password\n")
        validate_config(binary, render(files), directory)
        files = copy.deepcopy(fixtures[1][1])
        store = files["stores"][0]
        store["search"]["url"] = "https://search:9200"
        store["authentication"] = {"username_file": "test-user.txt", "password_file": "test-password.txt"}
        validate_config(binary, render(files, "search"), directory)
        # check reads the referenced files rather than trusting their names.
        store["authentication"]["password_file"] = "missing-password.txt"
        try:
            validate_config(binary, render(files, "search"), directory)
        except subprocess.CalledProcessError as error:
            if "credential file unavailable" not in error.stderr:
                raise RuntimeError("missing credential file rejected for another reason: "
                                   + error.stderr) from error
        else:
            raise RuntimeError("missing credential file unexpectedly accepted")
        # Server validation owns backend semantics beyond the Chart's schema.
        invalid = copy.deepcopy(fixtures[0][1])
        invalid["stores"][0]["mongodb"]["uri"] = "mongodb://mongo/?directConnection=true"
        resources = render(invalid)
        try:
            validate_config(binary, resources, directory)
        except subprocess.CalledProcessError as error:
            if "explicit valid host and port" not in error.stderr:
                raise RuntimeError("invalid Store configuration rejected for another reason: "
                                   + error.stderr) from error
        else:
            raise RuntimeError("invalid Store configuration unexpectedly accepted")
        check_scale(binary, directory)
        check_runtime(binary, directory)
    print("Contract checks passed: MongoDB/Search configurations, batching, scale, "
          "credential files and serve/probes/shutdown")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr or str(error)) from error
