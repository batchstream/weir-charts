# Acceptance entries

## Current single-node owned EKS functional run

`functional-owned.py` is a separate, finite external-host orchestrator for the
companion `cmd/weir-accept` contract. It does not reuse or relax the historical
24-hour soak/Prometheus gates below. It prepares offline by default. **Do not add
`--execute` until this change has passed review/CI and execution is authorized.**
It advances no production Chart version, source annotation or image default.

Use the cached Python environment containing this repository's pinned PyYAML and
an existing Helm executable. Supply one explicit regular JSON configuration:

```json
{
  "server_source": "REPLACE_FULL_CANDIDATE_SOURCE",
  "candidate": {
    "image": "ghcr.io/batchstream/weir@sha256:REPLACE_INDEX",
    "platform_digest": "sha256:REPLACE_ARM64_MANIFEST"
  },
  "distribution_bundle": "/absolute/path/verified-release.zip",
  "distribution_sha256": "REPLACE_TRUSTED_ZIP_SHA256",
  "runner_binary": "/absolute/path/weir-accept-linux-arm64",
  "runner_sha256": "REPLACE_BINARY_SHA256",
  "runner_source": "REPLACE_FULL_COMPANION_SOURCE",
  "runner_build": "/absolute/path/runner-build.json",
  "runner_build_sha256": "REPLACE_TRUSTED_BUILD_RECEIPT_SHA256",
  "mongo": {
    "image": "mongo@sha256:d0d926f94df099bff534b7ee5b5986458131a22489dfff8664509af0c1e2ca9c",
    "index": "/absolute/path/mongo-index.json",
    "manifest": "/absolute/path/mongo-arm64-manifest.json",
    "config": "/absolute/path/mongo-arm64-config.json"
  },
  "search": {
    "image": "docker.elastic.co/elasticsearch/elasticsearch@sha256:e98f9c3b09beb2fbb9eaf667d602df3f0e00bd3644138b8458dc17ba1a675595",
    "index": "/absolute/path/search-index.json",
    "manifest": "/absolute/path/search-arm64-manifest.json",
    "config": "/absolute/path/search-arm64-config.json"
  },
  "carrier": {
    "image": "docker.elastic.co/elasticsearch/elasticsearch@sha256:e98f9c3b09beb2fbb9eaf667d602df3f0e00bd3644138b8458dc17ba1a675595",
    "index": "/absolute/path/search-index.json",
    "manifest": "/absolute/path/search-arm64-manifest.json",
    "config": "/absolute/path/search-arm64-config.json"
  }
}
```

All image proof files are **exact registry response bytes**, not reserialized JSON.
The helper hashes the index against its pinned image reference, resolves exactly
one Linux ARM64 descriptor, checks the manifest and config hashes, and checks the
config architecture/OS. Search and carrier deliberately reuse the same proof
files and existing pinned Elasticsearch 8.19.22 image. The carrier overrides the
image entrypoint with `/bin/bash`; it never starts Java/Elasticsearch. Its required
stock tools are bash, seq, sleep, cat, chmod, uname and sha256sum. The binary hash,
build-only invocation and `uname -m` are checked in that actual Pod before the
functional ready marker. Missing tools or an unexpected image ID fail closed.
Mongo uses the existing pinned 8.0.32 fixture and a new single-member replica set;
this is not replica-set failover qualification.

The distribution ZIP is retained provenance, not a registry signature check: its
SHA256 must come from the independently verified delivery. The helper validates
all ZIP member checksums, matching delivery/receipt source, index, ARM manifest,
ARM binary hash and archive receipts without extracting or executing members.
The host separately verifies actual Kubernetes container image IDs against the
selected platform manifests. No registry access/download occurs in preparation.
The runner's independently retained `runner-build.json` must contain
`source_revision`, `target: "linux/arm64"`, `sha256` and `source_tree_sha256`.
Its explicit trusted checksum binds that receipt to the binary/source inputs.
It is not a claim that the helper rebuilt the source. A frozen `-buildvcs=false`
runner may report `vcs_revision: "unknown"` only with this matching receipt;
unknown VCS remains unknown, separate from its injected source assertion. When
runtime VCS metadata is present, the source must match and the tree must be clean.
The runner must report linked SDK/protocol versions and actual Linux ARM target.

```sh
# No Kubernetes contact, resource creation or image pull:
"$PYTHON" -I tests/acceptance/functional-owned.py input.json .testdata/functional-preview

# Later, explicitly authorized execution; use a NEW destination/run:
"$PYTHON" -I tests/acceptance/functional-owned.py input.json .testdata/functional-run --execute
```

Preparation validates local evidence, creates a random owner and unique namespace
name, and runs the actual Chart through `helm template` with owned candidate
values. Generated files belong under ignored `.testdata/`, never in Git commits.
There is no arbitrary command/runner option. The fixed functional invocation is
`/work/program -input /work/config.json`; stdout is the actual JSON report.

Execution is fixed to
`arn:aws:eks:us-west-1:956540890581:cluster/data-team`. A checkout-local nonblocking
lock serializes these runs. Immediately before namespace creation, read-only
preflight checks current context, ready/untainted native ARM nodes, existing Pod
requests (conservatively including init containers/overhead), node usage and Pod
slots. One node must have the **entire** 4 CPU / 6 GiB / 10 Pod initial quota free
against both requests and usage; every created Pod is then pinned to its hostname
plus `type: sts-arm` and `kubernetes.io/arch: arm64`. This is not a distributed
reservation: other cluster users can consume headroom, so execution remains
single-operator and never changes nodes, autoscaling or shared network settings.

A new namespace is created, never adopted; its actual UID and owner label gate
all subsequent operations. Owned ResourceQuota/LimitRange enforce the initial
budget. The four Pods have matching requests/limits: Weir 1 CPU/1 GiB,
Mongo 500m/1 GiB, Search 1 CPU/1536 MiB, runner 250m/256 MiB (total 2.75 CPU/
3840 MiB). Fixture data volumes are 2 GiB each, runner handoff is 128 MiB, and
helper tmp volumes are 64 MiB each. All are bounded `emptyDir`; no PVC/PV,
Role/RoleBinding, in-cluster API client/token, CRD, LoadBalancer, NodePort or
public port is used. The Job is nonretrying with a 660-second deadline, a finite
300-second upload gate and a finite export hold. Dependencies become ready and
Mongo primary initialization completes before Weir, then the runner, are created.
The host uploads exclusive regular binary/config files into **that same runner
Pod**, not into separate supposedly shared `emptyDir`s.

Only the newly rendered owned Chart has NetworkPolicy disabled. No existing
policy is edited or enforcement assumed: **a namespace is not network isolation**.
The owned services/replica discovery addresses are constructed from that namespace;
there is no existing database/Secret/credential reuse or TLS qualification claim.

Before and after functional work, the host verifies the actual namespace UID,
all four Pod/container UIDs/IDs, approved node, readiness, zero restarts and exact
platform image IDs. The runner receives the actual Weir Pod UID and namespace
UID in its 15-field input. Exit zero is insufficient: the host requires schema 1,
functional mode, matching identity/build, the exact complete set of per-backend
checks, no unknown mutations, and both actual `cleanup_verified` checks.
Missing/duplicate checks and a build-only report cannot pass. Reports, stderr,
controlled logs, Pod/image identities and headroom evidence are exported before
cleanup. Namespace deletion rechecks owner/UID and uses a Kubernetes **UID
precondition**, then verifies deletion. `functional-result.json` is emitted only
after both functional validation and namespace deletion succeed. Any failure
remains failure, even if its owned namespace was cleaned up. A lost create response,
process kill, or blocked deletion may require operator remediation using the
recorded owner/UID; never delete a replacement namespace by name alone.

This is one-node plaintext **functional** evidence only: not HA, node loss,
Mongo failover, 24-hour soak, formal/TLS qualification or server-ledger evidence.

## Historical persistent acceptance runs

The recorded run examples and SDK soak layout belong to the historical v0.1.x
qualification. Before using them for the current Chart, qualify a current SDK/server
pair and adapt Store targets and result checks to that pair. Chart unit/contract
tests do not establish a real-cluster or 24-hour acceptance result.
The [local Kind qualification](../../docs/acceptance-2026-10-12-local-kind.md)
has separate fixtures and records its functional deployment evidence.

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
