**Local Kubernetes functional qualification, 2026-10-12 (Asia/Shanghai)**

This run uses a new, isolated Kind cluster and a task-owned MongoDB replica set.
It qualifies the deployment changes in this PR with a specific candidate image.
It does not complete the historical 24-hour acceptance gate, production capacity
qualification or approval of the final release image. The release image must be
qualified separately before updating the Chart's source/version pin.

| Component | Recorded identity |
| --- | --- |
| Chart | Base `1b31642` plus this PR; version `0.5.0`, source/appVersion pin unchanged |
| Weir runtime | `d5ad652e30ccf381678b911816059fe36faff955`, clean commit, Go `1.27.1`, Linux arm64 |
| OCI index | `sha256:2b5b1f6962fdacb9924b7c95a10a5e3b16e8fc9f7b59da6fcd1d58b8ac551e96` |
| arm64 manifest | `sha256:52ca569f79fa3cdc475990b0bc8e6f19a38ae3b4a734e5e48254d44f60aa3a6d` |
| Cluster | Kind `0.33.0`, Kubernetes `1.37.0`, one control-plane and two workers |
| Kind node index | `sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5` |
| CNI | Kind's `kindnetd:v20260820-69b56db7`, enforcement verified with live TCP attempts |
| MongoDB | Locked native macOS arm64 `8.0.32`, separate port `27029`, replica set `weir_chart` |
| Business runner | `batchstream/weir-tests` commit `0a4636f99c76e7e3aecd01695941a6ec5ea808a0`, published SDK `v0.10.0`, protocol `v0.8.0` |
| `weir-bench` SHA256 | `f931a2676f4379070aa0e14e1afb464cabb3012d0f85ac357234ff7c8a115e2d` |
| `weir-lab` SHA256 | `f14a8198aed268fd3e545f25f5c1d40748bfcc6ed5d519cc894be729066a8e7a` |

The native MongoDB archive is the same locked `8.0.32` used by the service
qualification, SHA256
`f81cb258434d548dca7244d599c82eb339043d8dedd0b1b807870c9d263117f2`.
Its data directory, port, replica set and workload databases are separate from
the service fault-injection tests. Pod connections use the task-owned host
endpoint `192.168.65.254:27029` with `directConnection=true`; the replica-set
member advertises host loopback, which must not be treated as a Pod address.

Three real failures determined the changes:

1. The third Pod stayed Pending because topology spread counted the tainted
   control-plane as an eligible hostname. The three-replica example now sets
   `nodeTaintsPolicy: Honor`. Three replicas then scheduled over the two workers
   with hostname skew one. This follows Kubernetes' documented
   [node-taint inclusion policy](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/).
2. SDK discovery of advertised `*.namespace.svc` failed. Weir's bounded DNS
   resolver treats qualified names as absolute, so those addresses do not use
   Pod search suffixes. Examples, client fixtures and install notes now use
   complete `*.namespace.svc.cluster.local` names. A custom cluster domain
   requires replacing `cluster.local` in the configuration and client endpoint.
3. Immediate replacement of all cached Pod addresses caused business failures
   during rolling upgrade: the first 4,000-operation Weir round had 1,806
   successes, 1,098 errors and 1,096 indeterminate results, with connection
   refusals to old Pod IPs. A subsequent round succeeded. The Chart now waits
   `minReadySeconds: 15` for each replacement and gives terminating Pods a
   native kubelet `preStop.sleep` of 15 seconds before SIGTERM, with 45 seconds
   total termination grace. No shell or Lua worker process is introduced.
   The [Pod termination sequence](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination)
   excludes terminating endpoints from ready discovery while this serving
   delay lets persistent clients refresh. Operators must retain adequate
   refresh/shutdown windows for their SDK and application settings.

The [cluster](../tests/acceptance/local-kind/cluster.yaml),
[values](../tests/acceptance/local-kind/values.yaml) and
[client Pods](../tests/acceptance/local-kind/clients.yaml) are retained. All
cluster commands selected the owned kubeconfig/context and namespace. The
configuration enables peer gossip and restricted metrics, uses three replicas
and a PDB allowing one unavailable Pod, and limits each Pod to 1 CPU / 512 MiB
for this bounded functional check.

```sh
kind create cluster --name weir-chart-qualification \
  --kubeconfig /tmp/weir-chart-qualification-20261012/kubeconfig \
  --config tests/acceptance/local-kind/cluster.yaml

helm upgrade --install weir charts/weir --namespace weir-chart-test \
  --kubeconfig /tmp/weir-chart-qualification-20261012/kubeconfig \
  --kube-context kind-weir-chart-qualification \
  -f tests/acceptance/local-kind/values.yaml \
  --set image.repository=docker.io/library/weir-qualification \
  --set image.digest=sha256:2b5b1f6962fdacb9924b7c95a10a5e3b16e8fc9f7b59da6fcd1d58b8ac551e96 \
  --set image.pullPolicy=Never --atomic --wait --timeout 3m
```

Before installation, the owned namespace, isolated MongoDB, client Pods and
candidate image were prepared. The dual-architecture OCI archive was imported
with `kind load image-archive`; each node's containerd image reference was also
tagged by the recorded immutable index. The runtime's `/weir version` output
confirmed the source commit on all three replicas. The Chart retains its old
supported-source annotation until the separate reviewed release-pin PR.

For each final Pod, direct `/weir probe ready` and `live` passed, readiness was
true and restart count was zero. The application Service and full headless DNS
worked; DNS contained all three ready Pod IPs. The allowed client reached each
application port and metrics endpoint, while the unlabelled client was denied.
The application client was denied the peer port; the peer-labelled client was
allowed the peer port and denied the application port. Application metrics
showed a three-node directory and real peer `sync_directory` completions.
Actual containerd runtime inspection confirmed UID/GID 65532, no capabilities,
`noNewPrivileges`, read-only root and active seccomp. Every Weir main process
had no child processes while Lua business requests were running.

The existing `weir-bench` first verified two pairs of 200 operations against
direct MongoDB and SDK paths, including independent persistence checks, with
zero errors or indeterminate outcomes. A rollout with readiness pacing then
passed both 5,000-operation Weir rounds and both direct rounds. A first
180-second Lua run passed all 163,356 timed requests, but its last old container
exited three seconds after the traffic window; it was retained as incomplete
rollout-window evidence and rerun with a longer window.

The decisive run reuses `weir-lab client-worker`; no duplicate business runner
was added. One process opens the SDK once, performs a three-second warmup and
then sends continuous reads and Lua mutations for 300 seconds, with four
workers, 64 partitioned records, 128-byte padding and a 50% requested write mix.
Each request has a five-second deadline. Workers stop on the first error, and
business requests are never retried or replayed. The coordinator upgrades,
waits for all three old Pods to disappear, then rolls back to the preceding
revision and again waits for all replaced Pods to disappear. Both replacements
must complete before the timed window ends.

Final counts, phase timestamps, six clean terminations, per-Pod business
counters and frozen runtime identities are recorded in
[the evidence summary](acceptance-2026-10-12-local-kind-evidence.json).
PID 386 completed 222,595 timed requests: 110,850 reads and 111,745 Lua
mutations, with zero errors, timeouts or UNKNOWN outcomes. The timed window
was `2026-10-11T20:56:17.995595Z` to `21:01:17.995595Z`; both replacements
and all six clean old-container exits completed by approximately `20:58:08Z`.
Independent native MongoDB postflight checks every final document against the
runner's expected revision vector, exact fields, ID, padding and BSON int32
revision type. Probe and policy checks alone are not business acceptance.

`make package` passed four strict Helm lint configurations, rendered MongoDB and
Search configuration checks using a real Weir binary, serve/probe/shutdown
contract checks, 32 Python tests and Go race tests for the existing observer.
The source and release workflows still validate their recorded supported
source; no release was produced by this functional PR.

Raw successful and failed attempts, reused runner inputs, coordination/watch
events, postflight and runtime checks are exported under the local ignored
`.testdata/qualification/2026-10-12/candidate-local-kind/` directory with a SHA256
manifest and a readable index. This export
excludes kubeconfig, backend data and binaries. The evidence summary records
the archive checksum. The temporary cluster and MongoDB are retained for the
separate final published-image qualification.

The local MongoDB container could not start under Docker's LinuxKit kernel
`7.0.12`: locked MongoDB `8.0.32` rejected the kernel compatibility check
([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). The native,
same-version isolated replica set supplied real backend operations instead.
An early selectorless-Service backend fixture also failed CNI egress matching
after port translation; the direct task-owned host endpoint avoided that
fixture ambiguity. The failed setup evidence is preserved.

All nodes share one local Docker host, so this does not establish independent
failure domains, node-loss tolerance, production resource capacity, backends'
durability/backups, TLS or external-secret operation. Search configuration is
contract-checked here; live Elasticsearch/OpenSearch behavior belongs to the
separate service qualification. No 24-hour load window, complete historical
Prometheus acceptance audit or production deployment was run. These limits
remain explicit even after the functional tests and release-image checks pass.
