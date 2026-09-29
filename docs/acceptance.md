# Acceptance evidence

Date: 2026-09-29. This is an evidence record, not an overall production qualification declaration.

## Chart checks

Helm 3.17.0 and Python/PyYAML 6.0.2 pass strict lint for default and Mongo example
values, template rendering, and ten Python behavior/negative test groups plus Go observer race tests. Tests cover
restricted containers, external Secret handling, config checksum rollouts,
readiness/liveness, digest selection, deny-by-default network destinations, PDB,
Recreate, metrics opt-in, invalid values, release publication errors, and soak
observer failure handling. GitHub CI repeats these checks and packages the chart.
Release preflight requires a reviewed main ancestor and explicit authenticated
HTTP 404 evidence that the GitHub and OCI versions are unused. Auth/network errors
fail closed.

## Real cluster, initial product image

The dedicated acceptance namespace runs on EKS Kubernetes v1.36.3-eks-cb19647,
native Linux arm64, kernel 6.12.77. Three Weir replicas each request/limit 2 CPU /
1 GiB with a 768 MiB process budget and Local concurrency 2. All three became Ready
on three distinct EC2 workers, with zero container restarts in the successful
revision. Ordinary scheduling and the cluster's existing scaling resolved capacity;
no node pool, autoscaler, or existing application was changed.

Initial immutable image:
`ghcr.io/batchstream/weir@sha256:aee0c24fd5a0edbe81d335522e2741b34ca267f8246c893e15d6ef0097c18bb4`,
source `278264db2f9617ad583c6b56d19b8aaf5943e771`.
Temporary backends are MongoDB 8.0.32, one-member replica set with a pre-created
collection, and Elasticsearch 8.19.22, one shard/no replicas, pre-created index,
`action.auto_create_index=false`. Their disposable emptyDir data is only a test
fixture; it does not demonstrate production database durability or failover.

Server-side dry-run and Helm installation passed. SDK head `88dc043` exercised all
five RPCs against both real backends with the race detector: Mongo 3.93 seconds,
Search 4.10 seconds, total test process 9.64 seconds. It covered duplicate Create,
Read, Put/Replace, atomic expressions, ordered mixed Bulk, bounded Scan, Native,
and Delete followed by missing Read. Test-owned records were cleaned up.

The chart scaled 3 -> 1 successfully, then Helm rollback restored 3 Ready replicas.
A separate checksum-triggered configuration rollout changed process memory budget
768 -> 640 MiB while keeping resource limits constant; rollback restores the prior
config/image pair. These checks establish static config/lifecycle behavior, not a
zero-interruption guarantee for long-lived streams or cross-version compatibility.

A paused Elasticsearch server JVM produced an explicit UNAVAILABLE Read failure
in about 3.11 seconds while Weir's readiness probe remained successful. Resuming
the JVM restored a successful Read in about 1.16 seconds, without restarting Weir.
The first fault-fixture attempt paused PID 1 (`tini`) rather than the server JVM,
so the backend still answered; it was resumed and that attempt is not qualification
evidence. The verified server JVM PID was then used. No acknowledged data was lost
or uncertain mutation replayed in this read-only recovery check.

The first install also exposed two fixture problems: a temporary explicit hostname
restriction became unschedulable as shared workload allocation changed, and the
Search fixture omitted the required `action.auto_create_index=false`. The original
failed Helm revision and startup failures remain in the local receipt/history;
removing that artificial scheduling restriction and fixing only the test backend
produced the successful revision. These failures are not rewritten as passes.

## Stable release and persistent paired validation

The namespace upgraded to Weir v0.1.0, source
`be155e053f94cc7a6f3e8ce524f639b46f75be16`, image digest
`sha256:93810bbfb9eb720d0d42296a3e856f60e1d94bdb22279555ad94a8fc79c0f7df`.
All three replicas became Ready and the metrics Service was scraped successfully.
The SDK v0.1.0 commit `327974c756da1c6e7188fc7db53916cd36d45605` passed the
real five-RPC lifecycle on both backends (Mongo 4.13 s, Search 4.20 s, race test
process 10.293 s).

An initial paired fixture used a run ID without the SDK-required `weir-soak-`
prefix. The runner rejected it before any RPC, and the observer recorded failure.
The renderer now rejects that input before creating a Job; the failed reports
and terminal statuses remain preserved.

The corrected Service-based paired run completed 5,394 cycles in 180.056798 s,
with cycle p99 upper bound 50 ms and zero errors/UNKNOWN. The load exited zero,
and the independent observer validated the durable final report and recorded
success. Every sample retained CPU/memory data and fixed Pod/container identities.
However, its six Service connections delivered business traffic to only two of
three replicas: the third Pod's completion counter did not increase. This proves
the paired handshake/recording and Service behavior, **not** three-Pod load coverage.
Final three-Pod qualification uses SDK v0.1.1's fixed-target runner, six workers,
and one Mongo plus one Search worker on each of three frozen Pod IPs. The original
Service-based evidence remains separate.

The results PVC survived ordinary removal of its original node. After attachment
on another worker, the initial calibration JSONL SHA256 still matched its prior
local export: `dca2992ca10e2c90ae8aa5f5b4550f8b8d3ecb80c69678df0f4b4d2cad9b48df`.
This establishes persistence of the acceptance record across that event, not
production backend durability.

## Network boundary and remaining evidence

The AWS VPC CNI agent was installed with enforcement disabled. A controller-owned
Job with an explicit deny-all egress policy still reached the test backend. This
is a failed isolation gate. The reviewed shared-CNI plan and rollback limitations
are recorded in [network-policy-enforcement.md](network-policy-enforcement.md).
The user explicitly declined modifying shared CNI for this task; this gate remains failed and the plan is retained without execution. A namespace alone
is not isolation. The task's policies now deny ingress by default and separately
allow selected clients/Weir to the necessary ports, ready for an enforced retest.

The release OCI archive must be fetched and
installed from its published location. The 24-hour load run starts only after the
final versions and configuration are fixed and fault/rollout checks have ended.
A zero-retry Job writes JSONL and exit status to a dedicated 1 GiB gp3 result PVC.
`scripts/observe-soak.go` runs independently in the cluster and records each minute's exact Pod/Job identities, container image IDs, restart counts, readiness, Kubernetes CPU/memory usage and Weir metrics to the persistent volume. It fails on unexpected replacement, restart, readiness loss, Job failure, scrape/API errors or an excessive observation gap. The SDK watchdog cancels load if the observer fails or its heartbeat stops. Final success additionally verifies the durable runner `passed` record, exact run ID/duration, zero errors/UNKNOWN and exit code zero. Its output path must be new. See [the reproducible run templates](../tests/acceptance/README.md). The initial local Python sampler was calibration support only. The observer targets this fixed three-Weir/two-backend
acceptance layout, not arbitrary production workloads.

The initial three-minute calibration completed 5,394 cycles in 180.060 seconds, about 89.91 main RPC/second, with zero failures and UNKNOWN outcomes. Its cycle p99 histogram upper bound was 50 ms. Peak sampled Weir CPU was 0.0841 cores and memory 11.06 MiB; the Elasticsearch fixture peaked at 946.1 MiB. This supports freezing the selected 90 RPC/second, cycle p99 <= 500 ms, and 98% completion thresholds for the formal run; it does not establish maximum capacity.

After calibration ended, ordinary shared node scale-down replaced one Weir Pod and made a hostname-pinned results reader unschedulable. The reader is allowed to reschedule in the PVC zone; only this task's fixed-run Pods use `cluster-autoscaler.kubernetes.io/safe-to-evict: false` before taking a new baseline. An initial observer carrier timed out waiting for its binary and never executed the observer; it is preserved as fixture setup history, not a product pass or failure. All final binaries and hashes are prepared before the next paired run.

No short test establishes a 70%-capacity soak,
all-platform qualification, multi-member database failover, or a completed 24-hour
run. Those gates remain explicit in upstream production qualification records.
