# Persistent acceptance runs

These opt-in fixtures operate only in an explicitly selected, task-owned namespace. They are not chart dependencies. Supply isolated MongoDB replica-set and Elasticsearch indices, a three-replica Weir installation, working metrics-server, diagnostics metrics access, and a 1 GiB persistent results volume. Temporary backend data in the qualification run uses `emptyDir`; it does not qualify backend durability or backups.

Build the SDK's `examples/soak` at the recorded commit and `scripts/observe-soak.go` at this repository's recorded commit, using the cluster architecture and `CGO_ENABLED=0`. Record both SHA256 values before creating Jobs. Copy `run.example.json` and set the fixed identities, endpoint, unique run name, owner, namespace, results PVC and its node. The load and observer share that node because a ReadWriteOnce PVC cannot attach on different nodes. The Weir replicas remain spread across workers.

```sh
python3 tests/acceptance/prepare-run.py run.json rendered-run
kubectl --context "$CONTEXT" apply --dry-run=server -f rendered-run/resources.json
kubectl --context "$CONTEXT" create --dry-run=server -f rendered-run/load.json -f rendered-run/observer.json
kubectl --context "$CONTEXT" apply -f rendered-run/resources.json
kubectl --context "$CONTEXT" create -f rendered-run/load.json -f rendered-run/observer.json
```

The renderer refuses an existing output file and uses `backoffLimit: 0`, `restartPolicy: Never`, bounded startup and overall deadlines, fixed binary hashes, and an owner label. Its pinned Elasticsearch image is only a shell/tar carrier; neither Job starts Elasticsearch. The load Job has no Kubernetes token. The observer has only namespace-scoped `get/list` access to Pods, Jobs and Pod metrics. It reads its newly projected token internally for normal Kubernetes authentication and never records it.

Within five minutes, upload the prepared binaries into the corresponding Pods as `/runner/program` using `kubectl cp`, set them executable, verify `sha256sum`, and create `/runner/ready` in each Pod. Wait approximately 30 seconds for metrics-server to sample the load Pod before starting the observer. The load shell waits for both its local ready file and the observer's persistent ready heartbeat **before opening its report**. Never reuse a run name or existing report path.

The observer freezes Pod/Job UIDs and container image IDs, requires all service containers Ready with zero restarts, scrapes every Weir `/metrics`, and records namespace Pod CPU/memory metrics each minute with an fsync. It fails immediately on API/scrape errors or identity/readiness changes. Its monotonic observation interval allows at most 30 seconds of scheduling overrun. The SDK watchdog cancels the run if the observer reports failure, exits prematurely, or stops updating its heartbeat for 150 seconds. Unknown writes are counted and never replayed or cleaned up automatically.

Success requires all of: the load Job succeeded with exit code zero; its last durable JSONL record is `passed` for the exact run ID and duration with zero failures/UNKNOWN; and the observer Job succeeded with a durable `passed: true` status after validating that same load evidence. A successful Job alone is insufficient. Record the cycle latency histogram's upper bound as **cycle p99**, not per-RPC p99. The six workers at five cycles/second target about 90 main RPC/second; require at least 98% of planned cycles.

Export every run's reports, stderr, observation JSONL, terminal statuses, Job/Pod identities, binary hashes, image/source/chart identities and the failed-attempt history through a read-only mount of the PVC. Verify local checksums before removing only task-owned resources. Namespace deletion also removes PVCs with a Delete reclaim policy, so results must be exported first. Keep shared CNI and unrelated workloads unchanged unless separately authorized; a policy object without tested enforcement does not satisfy the network gate.
