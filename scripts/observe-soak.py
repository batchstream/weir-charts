"""Observe fixed acceptance Pods without reading Secrets or changing resources."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time


def kubernetes(args, resource):
    command = ["kubectl", "--context", args.context, "--request-timeout=20s", "-n", args.namespace, *resource]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def snapshot(args):
    listing = kubernetes(args, ["get", "pods", "-o", "json"])
    tracked = []
    for pod in listing["items"]:
        labels = pod["metadata"].get("labels", {})
        selected = labels.get("app.kubernetes.io/instance") == args.release or labels.get("app") in ("mongo", "elasticsearch")
        if not selected:
            continue
        statuses = pod["status"].get("containerStatuses", [])
        state = {
            "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "node": pod["spec"].get("nodeName"), "phase": pod["status"]["phase"],
            "containers": [{"name": item["name"], "ready": item["ready"], "restarts": item["restartCount"], "imageID": item.get("imageID"), "state": item["state"], "lastState": item.get("lastState", {})} for item in statuses],
        }
        tracked.append(state)
    job = kubernetes(args, ["get", "job", args.job, "-o", "json"])
    metrics_path = "/apis/metrics.k8s.io/v1beta1/namespaces/" + args.namespace + "/pods"
    metrics = kubernetes(args, ["get", "--raw", metrics_path])
    record = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pods": tracked, "jobUID": job["metadata"]["uid"], "jobStatus": job.get("status", {}),
        "usage": metrics["items"],
    }
    return record


def invariant(record, expected_pods, expected_job, expected_count):
    pods = record["pods"]
    if len(pods) != expected_count:
        raise RuntimeError("unexpected tracked Pod count")
    if {pod["uid"] for pod in pods} != expected_pods or record["jobUID"] != expected_job:
        raise RuntimeError("Pod or Job identity changed during the fixed run")
    for pod in pods:
        if pod["phase"] != "Running" or not pod["containers"]:
            raise RuntimeError("tracked Pod stopped running: " + pod["name"])
        for container in pod["containers"]:
            if not container["ready"] or container["restarts"] != 0:
                raise RuntimeError("readiness/restart invariant failed: " + pod["name"])
    if record["jobStatus"].get("failed", 0):
        raise RuntimeError("load Job failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--release", default="weir")
    parser.add_argument("--replicas", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=90000)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 1 or args.interval < 1 or args.replicas < 1:
        parser.error("positive duration, interval, and replica count required")
    deadline = time.monotonic() + args.seconds
    with args.output.open("x") as output:
        baseline = snapshot(args)
        expected_pods = {pod["uid"] for pod in baseline["pods"]}
        expected_job = baseline["jobUID"]
        current = baseline
        while True:
            output.write(json.dumps(current, separators=(",", ":")) + "\n")
            output.flush()
            invariant(current, expected_pods, expected_job, args.replicas + 2)
            if current["jobStatus"].get("succeeded", 0) == 1:
                print("Observation completed: fixed identities, zero restarts, successful load Job")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("observation deadline reached before load Job completion")
            time.sleep(min(args.interval, remaining))
            current = snapshot(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("Observation failed: " + str(error), file=sys.stderr)
        sys.exit(1)
