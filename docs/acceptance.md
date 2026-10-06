# Acceptance evidence

This historical report covers the Chart 0.1.x/server 0.1.x contract. It does not
qualify Chart 0.2.0, its split YAML configuration, Store discovery topology, or a
new image. Those require a separately recorded real-cluster acceptance run.

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

The final targeted paired run passed on SDK v0.1.1, source
`8f3462ef65d0ff587d58b489b6a8b48183531362`, with the same stable server image.
Three Weir Pods ran on three distinct ON_DEMAND workers, each at 2 CPU / 1 GiB.
The task selected existing compatible capacity through its own Pod selector; the
existing cluster autoscaler expanded an existing node group under its existing
limits. No node group, ASG, taint or shared autoscaler configuration was changed.
Earlier node shutdown/replacement was observed, but its initiating cause was not
established; using ON_DEMAND does not guarantee that nodes will never be replaced.

The run started at 2026-09-29T02:28:22.508225981Z and passed after 180.040095302 s:
5,394 cycles, 10,788 verified mutations, 10,788 verified reads, six stream checks,
zero failures/UNKNOWN, and cycle p99 histogram upper bound 20 ms. Each Pod had one
Mongo and one Search worker; their RPC completion deltas were 5,406 / 5,406 / 5,400.
Eight durable observer samples covered the baseline through terminal completion,
with unchanged UIDs/image IDs, zero restarts, complete fresh per-container usage
and successful metrics scrapes. The load status was exit zero, both Jobs succeeded,
and the independent observer recorded `passed: true` at 02:31:51.877200368Z.

The SDK runner binary SHA256 was
`a5c63081cfdef682e7d2f194f1f256b0190f7b0a377e2929117ef1fa916d93d5`;
the observer binary SHA256 was
`01ea665b28887527cefb780cab03fa4b374537a37415a372eabd73d7824055fa`.
All 23 historical and current result files were exported using hard-link
dereferencing and verified against checksums generated on the PVC. An earlier
plain `kubectl cp` export lost hard-linked status payloads; that export is obsolete
and the verified replacement retains the actual terminal evidence.

The results PVC survived ordinary removal of its original node. After attachment
on another worker, the initial calibration JSONL SHA256 still matched its prior
local export: `dca2992ca10e2c90ae8aa5f5b4550f8b8d3ecb80c69678df0f4b4d2cad9b48df`.
This establishes persistence of the acceptance record across that event, not
production backend durability.

## First formal 24-hour attempt: failed

The first formal run used server v0.1.0, SDK v0.1.1 and Chart 0.1.0 with the
fixed three-Pod targets above. It began at 2026-09-29T02:41:25.977370518Z and
failed at 04:03:26.977973834Z after 4,921.000606088 seconds, well short of 24 hours.
The observer's Pod usage metrics API request returned HTTP 503. It persisted a
failed status and exited one; the SDK watchdog then canceled the load. The final
load report contains 147,624 completed cycles, 295,248 verified mutations and
295,248 verified reads, 162 stream checks, cycle p99 upper bound 20 ms, seven
failures (one observer failure and six canceled workers), and zero UNKNOWN.
Both original Jobs failed with exit one and zero restarts; neither was retried.

Shared metrics-server had one replica. Cluster events show its old Pod stopping
and losing readiness while a replacement started just before the failed request.
A later replacement became Ready and the API answered successfully during the
investigation. The duration and recovery time of the first outage, and the cause
of the Pod replacements, were not established. This does not prove that retrying
within the existing sampling budget would have recovered the failed run.

All 82 pre-failure load progress records reported zero failures/UNKNOWN and cycle
p99 at most 20 ms. The 82 complete observer samples ended one minute before the
failure, with a maximum preceding sample gap of 60.05817 seconds. The three Weir
Pods retained their original identities and were Ready during investigation;
that snapshot does not prove uninterrupted data-plane availability after
observation stopped. All seven result files were exported with hard-link
dereferencing, matched against independent PVC checksums, and fully parsed,
including both failed terminal statuses. The original failed evidence remains.

The first correction, independently reviewed and merged in PR #2 at source
`0861d8d45a9e7a46c321042b3566f84470d608e9`, allowed explicit usage API HTTP 503
recovery only within the existing sampling window. Its observer binary SHA256 was
`aec6f884a2875208eca3c55ada9b7b3bd1747e902c4ac042821fa8b9ae43f64b`.
The deployed Chart remained 0.1.0; its old release tag does not contain this
separately built acceptance tool. A new three-minute pair passed with 5,394 cycles
in 180.037728265 seconds, cycle p99 at most 20 ms, zero failures/UNKNOWN, both Jobs
successful, five complete observer samples and business activity on all three
Pods and both backends. This did not establish a full-day result.

## Second formal attempt: failed; complete-sample contract approved

R2 started at 2026-09-29T06:08:33.980176152Z and failed at
06:10:33.981678716Z after 120.001503529 seconds. The final report contains
3,594 cycles, 7,188 verified writes and reads, two stream checks, cycle p99 at most
20 ms, seven failures and zero UNKNOWN. Both Jobs exited one with zero restarts.
The observer rejected required backend usage with the old combined
`missing or stale Pod usage` diagnostic. Only two complete samples were persisted;
the rejected usage response was not retained. Missing data, container-count
mismatch and staleness cannot be distinguished retrospectively. No HTTP 503 retry
was recorded in this run.

Metrics-server was replaced again shortly before the failure, and its APIService
became Available nine seconds before the rejected sample. This is a temporal
association, not proof of a specific missing field, outage duration or root cause
of the replacement. A later successful usage query does not prove that the failed
sample was complete. All seven R2 result files were independently matched against
PVC hashes and fully parsed. Both failed runs and the successful short pairs are
preserved; no short test or later tool correction qualifies either failed run.

The approved contract for subsequent runs accepts only complete, healthy,
unexpired observations inside the same fixed sampling window. Explicit usage API
HTTP 503 and missing/stale/incomplete usage may cause complete recollection within
that window. This changes the former per-response fail-fast rule explicitly; it
does not change the 30-second startup deadline, 90-second steady sample window,
120-second usage age, 150-second watchdog or any business/24-hour success gate.
Hard identity, health, permission and protocol errors retain immediate precedence.
Each rejected attempt now records its specific reason, affected objects,
timestamps, duration and remaining budget, and never refreshes the heartbeat.
A new independently reviewed source/binary, run identity and full-duration result
are still required. No shared metrics-server, CNI or node-pool changes are part of
this correction.

## Third formal attempt and Prometheus evidence source

R3 started at 2026-09-29T07:15:12.198542099Z and failed at
07:30:42.199939615Z after 930.001398859 seconds: 27,894 cycles,
55,788 confirmed writes/reads, seven failures, zero UNKNOWN and cycle p99 at most
20 ms. Both Jobs exited one, without restart. After 15 complete observations the
observer recorded 30 explicit metrics API 503 responses, exhausted the unchanged
90-second deadline and failed; it did not renew its budget or heartbeat. The
metrics-server replacement became Ready after that deadline. All seven raw result
files were exported, individually verified against PVC hashes and fully parsed.
This run and both earlier formal failures remain failed; none is resumed or
combined with later data to manufacture 24 hours.

The existing Prometheus already collected cAdvisor data throughout the three
sampled failure windows, with actual resource timestamp gaps about 28–30 seconds.
A limited owned-Pod annotation probe confirmed that its existing pod-discovery job
also collects Weir business, queue, execution, process RSS and Go heap at 15-second
intervals through diagnostics port 7449. The temporary annotation was restored.
These limited windows and one-Pod probe are source discovery, not retrospective
qualification of any failed run. Prometheus is single-replica with bounded
retention; source metadata is not a guarantee of future complete history.

Subsequent runs use the existing Prometheus for resource and application evidence,
and the observer only for fixed Kubernetes lifecycle and durable load watchdog
state. Runtime metrics.k8s.io access and duplicate application scrapes are removed.
The new startup preflight uses actual raw samples before allowing business RPCs.
Final collection uses raw range vectors, never query_range evaluation grids,
lookback interpolation or zero filling. Original sample gaps and run boundaries
must stay within 90 seconds; resource statistics must be within 120 seconds of
the corresponding exporter observation, allowing only the existing 30-second
clock skew. This measures exporter/statistics age, not TSDB ingestion latency.

The existing lifecycle sampling and Prometheus historical sampling prove different
things. Temporary historical query failure leaves evidence pending, while a real
historical gap, identity conflict, counter reset, scrape failure, missing required
series or resource bound failure prevents a pass. Final qualification requires
load + lifecycle observer + complete Prometheus audit + independent trend review.
The business gates remain actual 24h, six workers at five cycles/second, at least
98% cycle coverage, every interval/overall cycle p99 <=500ms and zero failures or
UNKNOWN. The workload remains a stability test, not peak-capacity certification.

The reproducible annotation overlay and standalone export/audit tools do not change
the packaged Chart. Chart 0.1.0 source and the newly reviewed tool source/hash must
be recorded separately. All three Pods will roll when the Pod template annotations
are applied; a new short pair, full Prometheus evidence audit and fresh identities
must pass before any new 24-hour run. No shared metrics-server, Prometheus, CNI,
node-pool or ASG mutation is part of this change. See the [source contract and
commands](../tests/acceptance/prometheus.md).

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
`scripts/observe-soak.go` runs independently in the cluster and persists fixed
Pod/Job/container identity, lifecycle and readiness observations. It fails on
replacement, restart, readiness loss, Job/API failure or excessive observation gap.
The SDK watchdog stops load if that lifecycle observer fails or its heartbeat
stops. Prometheus history collection is independent; query failures cannot cancel
load and incomplete historical evidence cannot qualify it. Final lifecycle success
also verifies the durable runner terminal report and exit status. Each result path
is exclusive to a new run. See [the reproducible run templates](../tests/acceptance/README.md).


The initial three-minute calibration completed 5,394 cycles in 180.060 seconds, about 89.91 main RPC/second, with zero failures and UNKNOWN outcomes. Its cycle p99 histogram upper bound was 50 ms. Peak sampled Weir CPU was 0.0841 cores and memory 11.06 MiB; the Elasticsearch fixture peaked at 946.1 MiB. This supports freezing the selected 90 RPC/second, cycle p99 <= 500 ms, and 98% completion thresholds for the formal run; it does not establish maximum capacity.

After calibration ended, ordinary shared node scale-down replaced one Weir Pod and made a hostname-pinned results reader unschedulable. The reader is allowed to reschedule in the PVC zone; only this task's fixed-run Pods use `cluster-autoscaler.kubernetes.io/safe-to-evict: false` before taking a new baseline. An initial observer carrier timed out waiting for its binary and never executed the observer; it is preserved as fixture setup history, not a product pass or failure. All final binaries and hashes are prepared before the next paired run.

No short test establishes a 70%-capacity soak,
all-platform qualification, multi-member database failover, or a completed 24-hour
run. Those gates remain explicit in upstream production qualification records.
