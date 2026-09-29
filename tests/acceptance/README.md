# Persistent acceptance runs

These opt-in fixtures operate only in an explicitly selected, task-owned namespace. They are not chart dependencies. Supply isolated MongoDB replica-set and Elasticsearch indices, a three-replica Weir installation, working metrics-server, diagnostics metrics access, and a 1 GiB persistent results volume. Temporary backend data in the qualification run uses `emptyDir`; it does not qualify backend durability or backups.

Build the SDK's `examples/soak` at the recorded commit and `scripts/observe-soak.go` at this repository's recorded commit, using the cluster architecture and `CGO_ENABLED=0`. Record both SHA256 values before creating Jobs. For deterministic three-Pod coverage with SDK v0.1.1+, remove `address`, set `targets` to the three fixed `PodIP:7447` strings in run configuration and save the matching Pod names, UIDs and image IDs alongside it. Six workers map round-robin to these targets, one Mongo and one Search worker per Pod. The Service-based five-RPC and rollout checks are a separate phase. Verify all three Pod RPC counters increase before accepting the load window. Copy `run.example.json` and set the fixed identities, endpoint, unique run name, owner, namespace, results PVC and its node. The load and observer share that node because a ReadWriteOnce PVC cannot attach on different nodes. The Weir replicas remain spread across workers.

```sh
python3 tests/acceptance/prepare-run.py run.json rendered-run
kubectl --context "$CONTEXT" apply --dry-run=server -f rendered-run/resources.json
kubectl --context "$CONTEXT" create --dry-run=server -f rendered-run/load.json -f rendered-run/observer.json
kubectl --context "$CONTEXT" apply -f rendered-run/resources.json
kubectl --context "$CONTEXT" create -f rendered-run/load.json -f rendered-run/observer.json
```

The renderer refuses an existing output file and uses `backoffLimit: 0`, `restartPolicy: Never`, bounded startup and overall deadlines, fixed binary hashes, and an owner label. Its pinned Elasticsearch image is only a shell/tar carrier; neither Job starts Elasticsearch. The load Job has no Kubernetes token. The observer has only namespace-scoped `get/list` access to Pods and Jobs. It reads its newly projected token internally for normal Kubernetes authentication and never records it.

Within five minutes, upload the prepared binaries into the corresponding Pods as `/runner/program` using `kubectl cp`, set them executable, verify `sha256sum`, and create `/runner/ready` in each Pod. Start the lifecycle observer, then complete the Prometheus preflight while the load shell waits. The load shell waits for both its local ready file and the observer's persistent ready heartbeat **before opening its report**. Never reuse a run name or existing report path.

The observer freezes Pod/Job/container IDs, image IDs, node and IP, requires all service containers Ready with zero restarts, and records only Kubernetes lifecycle evidence. It uses namespace-scoped Pod/Job reads; there is no metrics.k8s.io permission or direct application metrics scraping. Reads, validation, file sync and heartbeat publication still share a 90-second monotonic boundary at the default 60-second interval. The SDK watchdog remains 150 seconds. A lifecycle failure still stops the load.

Resource and application evidence comes from the existing Prometheus deployment. Merge `prometheus-values.yaml` into the owned values, preserving existing annotations and restricted metrics ingress. The existing pod-discovery job must honor scrape/port/path; no shared scrape configuration or collector is installed. See [the raw evidence procedure](prometheus.md) for exact queries, identity freeze, preflight and final audit. Prometheus query failure leaves evidence pending and never cancels the business load. Actual missing historical samples still prevent qualification.

Create the two waiting Jobs first, then freeze their actual UID/container identities. The load shell waits for both the lifecycle heartbeat and a unique Prometheus preflight marker **before opening the report or sending an RPC**. The marker binds the run ID, canonical run configuration SHA256, actual load Pod UID and full evidence freeze SHA256. Its separate 120-second publication lifetime prevents stale preflight reuse; this is a new startup guard, distinct from resource source-age validation. Wrong/old identity, expired marker or mismatched freeze aborts startup. No backward-compatible path bypasses the Prometheus gate.

Success requires the independently audited complete Prometheus resource/application window **and** all of: the load Job succeeded with exit code zero; its last durable JSONL record is `passed` for the exact run ID and duration with zero failures/UNKNOWN; and the observer Job succeeded with a durable `passed: true` status after validating that same load evidence. A successful Job alone is insufficient. Record the cycle latency histogram's upper bound as **cycle p99**, not per-RPC p99. The six workers at five cycles/second target about 90 main RPC/second; require at least 98% of planned cycles.

Export every run's reports, stderr, observation JSONL, terminal statuses, Job/Pod identities, binary hashes, image/source/chart identities and the failed-attempt history through a read-only mount of the PVC. The shell's exit status uses an atomic hard link. Plain `kubectl cp` can export hard-linked entries as empty files; use the carrier's GNU tar to dereference them, then compare every extracted file against checksums generated on the PVC:

```sh
kubectl --context "$CONTEXT" -n "$NAMESPACE" exec "$RESULT_READER" -- \
  tar --hard-dereference -cf - -C /results . > results.tar
mkdir exported-results
tar -xf results.tar -C exported-results
kubectl --context "$CONTEXT" -n "$NAMESPACE" exec "$RESULT_READER" -- \
  bash -c 'cd /results && sha256sum *' > exported-results/SHA256SUMS
(cd exported-results && shasum -a 256 -c SHA256SUMS)
```

Run this after both Jobs terminate so files are stable. Verify all local checksums before removing only task-owned resources. Namespace deletion also removes PVCs with a Delete reclaim policy, so results must be exported first. Keep shared CNI and unrelated workloads unchanged unless separately authorized; a policy object without tested enforcement does not satisfy the network gate.
