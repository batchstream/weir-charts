"""Validate rendered config and CLI against an explicitly selected Weir binary.

No backend connections or existing Secret files are read. The only live process
is an isolated no-Store node using loopback diagnostics and ephemeral ports.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(values, release="weir"):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(values, handle)
        handle.flush()
        command = ["helm", "template", release, str(ROOT / "charts/weir"),
                   "-f", str(ROOT / "tests/values.yaml"), "-f", handle.name]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    return resources


def validate_config(binary, resources, directory, pod_ip="127.0.0.1"):
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for name in ("node.yaml", "routes.yaml"):
        (directory / name).write_text(config["data"][name])
    environment = dict(os.environ)
    for entry in container.get("env", []):
        if entry["name"] == "WEIR_PEER_ADDRESS":
            environment[entry["name"]] = entry["value"].replace("$(POD_IP)", pod_ip)
    args = [str(directory / Path(arg).name) if arg.startswith("/etc/weir/") else arg
            for arg in container["args"]]
    command = [binary, "check", *args[1:]]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
    if result.stdout.strip() != "configuration valid":
        raise RuntimeError("unexpected configuration validation response")
    return container, args, environment


def reserve_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener


def check_runtime(binary, directory):
    # Keep both reservations open until immediately before startup.
    application = reserve_port()
    diagnostics = reserve_port()
    app_port = application.getsockname()[1]
    diagnostic_port = diagnostics.getsockname()[1]
    node = {"listeners": {"application": "0.0.0.0:" + str(app_port)},
            "diagnostics": {"address": "127.0.0.1:" + str(diagnostic_port)}}
    data = {"node": node, "routes": {"stores": []}}
    values = {"config": {"existingSecret": "", "data": data},
              "service": {"port": app_port}, "diagnostics": {"port": diagnostic_port}}
    resources = render(values)
    container, args, environment = validate_config(binary, resources, directory)
    application.close()
    diagnostics.close()
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen([binary, *args], env=environment, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 10
            while True:
                command = [binary, *container["startupProbe"]["exec"]["command"][1:]]
                ready = subprocess.run(command, capture_output=True, text=True, timeout=3)
                if ready.returncode == 0:
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    output.seek(0)
                    raise RuntimeError("rendered serve/startup probe failed: " + output.read())
                time.sleep(0.05)
            for name in ("readinessProbe", "livenessProbe"):
                command = [binary, *container[name]["exec"]["command"][1:]]
                subprocess.run(command, capture_output=True, text=True, check=True, timeout=3)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise RuntimeError("Weir did not drain after SIGTERM")
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
                ("search", yaml.safe_load((ROOT / "examples/search.yaml").read_text()))]
    with tempfile.TemporaryDirectory(prefix="weir-chart-contract-") as temporary:
        directory = Path(temporary)
        for release, values in fixtures:
            resources = render(values, release)
            validate_config(binary, resources, directory)
            ipv6 = copy.deepcopy(values)
            for name in ("application", "peer"):
                ipv6["config"]["data"]["node"]["listeners"][name] = ipv6["config"]["data"]["node"]["listeners"][name].replace("0.0.0.0", "[::]")
            validate_config(binary, render(ipv6, release), directory, "fd00::123")
            replicas = copy.deepcopy(values)
            replicas.update(yaml.safe_load((ROOT / "examples/three-replicas.yaml").read_text()))
            validate_config(binary, render(replicas, release), directory)
        # Exercise relative credential-file resolution using only generated fixtures.
        files = copy.deepcopy(fixtures[0][1])
        mongo = files["config"]["data"]["routes"]["stores"][0]["mongodb"]
        mongo.update(uri="mongodb://mongo:27017/?directConnection=true&authMechanism=SCRAM-SHA-256&authSource=admin&tls=true",
                     username_file="test-user.txt", password_file="test-password.txt")
        (directory / "test-user.txt").write_text("test-user\n")
        (directory / "test-password.txt").write_text("test-password\n")
        validate_config(binary, render(files), directory)
        files = copy.deepcopy(fixtures[1][1])
        search = files["config"]["data"]["routes"]["stores"][0]["search"]
        search.update(url="https://search:9200", connection={"username_file": "test-user.txt", "password_file": "test-password.txt"})
        validate_config(binary, render(files, "search"), directory)
        # A real binary must reject old fields that a permissive Helm object could accept.
        old = copy.deepcopy(fixtures[0][1])
        old["config"]["data"]["node"]["memory"] = "1GiB"
        try:
            validate_config(binary, render(old), directory)
        except subprocess.CalledProcessError:
            pass
        else:
            raise RuntimeError("removed memory configuration unexpectedly accepted")
        check_runtime(binary, directory)
    print("Rendered MongoDB/Search config, credential-file fixtures, serve/probes and shutdown passed")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr or str(error)) from error
