# Operations

How the chart handles traffic, rollouts, capacity, shutdown and probes.

## Network

Application traffic uses plaintext HTTP/2 inside a trusted, isolated network. Default NetworkPolicy permits same-namespace clients labeled `weir-client: "true"` to call the application port, plus DNS egress. Its named `grpc` rule follows `service.port` automatically (default 7447). Backend egress remains explicit. Verify the CNI enforces both allowed and denied traffic; additive allow-all policies can bypass restrictions.

## Rolling updates

A rolling update starts each replacement and waits for it before stopping a replica (`maxSurge: 1`, `maxUnavailable: 0`), at any replica count and under autoscaling, so an upgrade neither interrupts a single replica nor reduces capacity. A replacement that cannot start, such as a bad image or a cluster without room, leaves the old replicas serving, and the Deployment reports failure after `progressDeadlineSeconds`. A rollout needs room for one more Pod, for which a cluster autoscaler may add a node. Where a cluster or quota has none, set `strategy.rollingUpdate` to `maxSurge: 0` and `maxUnavailable: 1`: the replacement then races the old replica's shutdown, capacity drops by one during each replacement, and a single replica whose replacement is late has no server for as long as it is late, while calls fail with errors.

## Replicas and disruption

One replica still has downtime when its process or node fails. For three replicas use [examples/three-replicas.yaml](../examples/three-replicas.yaml); update its release-specific spread selector. Its `nodeTaintsPolicy: Honor` excludes untolerated nodes from the spread calculation. The optional PDB limits voluntary disruptions and does not establish physical fault tolerance by itself.

## Capacity

Kubernetes resource names and DNS endpoints follow their native format limits. Size groups and replicas from measured process, network and backend capacity, including starting and terminating instances.

Each default Pod requests and limits 2 CPU / 3 GiB. Budget starting, running and terminating processes and backend connection demand together: a rollout runs one replica more than the group's count, plus Pods still terminating. Kubernetes does not impose a hard bound on concurrent terminating Pods, and old remote work can outlast a lost connection. Large workloads need measured admission/latency and backend capacity verification.

## Rollout timing

The Deployment waits `minReadySeconds: 15` after each replacement becomes Ready. Weir bounds its own startup at 30 seconds and usually listens within a second, so the startup probe runs every second and a replica becomes Ready as soon as it starts. The first probe can land anywhere in the first period, so the probe allows 32 failures, killing a Pod that has not started 31 to 32 seconds after its container starts. `progressDeadlineSeconds` is an explicit Deployment rollout budget (Chart default 180 seconds), including time for scheduling and image pulls. It must strictly exceed `minReadySeconds` plus that 32-second startup budget. Insufficient budgets fail validation; the Chart never silently changes the configured deadline. Deployment timing fields retain Kubernetes' int32 bounds. Minimum readiness time leaves space for clients to re-resolve the headless Service while old replicas remain.

## Shutdown

Before SIGTERM, kubelet's `preStop.sleep: 15` lets terminating Pod addresses leave Ready headless DNS without another shell/worker process. Weir then withdraws readiness and drains admitted work within `shutdownTimeoutSeconds` (Chart default 5). Each Store then receives Weir's fixed 2-second cleanup budget to cancel remaining work, end backend sessions and close connections; Stores clean up in parallel. With `terminationGracePeriodSeconds: 0`, the Pod grace is derived as preStop plus shutdown timeout plus 2 seconds of cleanup plus one second for final process exit (23 seconds by default). That extra second is the smallest integer slack above the bounded lifecycle phases; reserve more measured exit overhead with a positive override, which must cover the full derived budget. `shutdownTimeoutSeconds` is a positive integer and becomes Weir's `shutdown_timeout` duration string. The native sleep hook requires Kubernetes 1.30+'s default PodLifecycleSleepAction feature. Disabling the hook removes its retirement protection.

## Probes

Kubelet probes are HTTP GETs of `/readyz` (startup and readiness) and `/livez` (liveness) on `diagnosticsPort`, which Weir always binds on `[::]`. The same port serves `/metrics`; without `metrics.enabled`, the default NetworkPolicy admits no other sources to it. Readiness describes lifecycle; validate real business operations separately. A sent mutation without a terminal response has an unknown outcome and requires reconciliation before retrying.

Readiness and liveness use Kubernetes' native 10-second probe period, one-second timeout and three consecutive failures. Startup uses the one-second period and the threshold above. Deployment revision retention uses Kubernetes' native default. These defaults are documented in the [probe API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#Probe) and [Deployment API](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#clean-up-policy).

## Store and backend changes

Changes to Store membership or backend identity require a coordinated cutover. An ordinary rolling update temporarily runs old and new definitions in one group and does not provide a transactional migration. Stop or redirect affected clients, drain the old replicas, apply the reviewed configuration/image pair and verify actual data operations before resuming.
