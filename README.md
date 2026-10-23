# Weir Helm charts

Deploy [Weir](https://github.com/batchstream/weir), a synchronous MongoDB and Search data plane, on Kubernetes. Helm 3.17+ and Kubernetes 1.30+ are required. Chart `0.10.0-rc.2` targets server source [commit `3f3db884e4a3`](https://github.com/batchstream/weir/commit/3f3db884e4a39f9d6556c6b7b2af4385afdfae9b). `Chart.yaml` records that source contract and `values.yaml` pins the immutable image `ghcr.io/batchstream/weir@sha256:3144fcb210fb7ab323bebf358473adce78baf1cee88ac46c30df8b36f533440d`; both validation and release workflows build that exact source.

One Helm release is one Store group. Every replica hosts the same Store definitions; a group can host several Stores, and each group is released and scaled independently. Replicas share nothing with each other. Each Pod runs one Weir process with Lua inside that process. Backends, collections, indices, credentials and backups are provisioned separately.

## Store groups

Declare this group's Stores directly in `stores`:

```yaml
stores:
  - name: primary
    backend:
      mongodb:
        uri: mongodb://mongo:27017/?directConnection=true
  - name: search
    backend:
      search:
        url: http://search:9200
```

Database, collection and index targets come from request URIs; Chart configuration declares the backend connection for each Store. Multiple replicas within a group use identical Stores and settings. A request for a Store the group does not host fails with NotFound.

Install [examples/mongo.yaml](examples/mongo.yaml) and [examples/search.yaml](examples/search.yaml) as separate groups in the same namespace:

```sh
helm upgrade --install mongo ./charts/weir --namespace application \
  --kube-context your-context -f examples/mongo.yaml --atomic --wait --timeout 5m
helm upgrade --install search ./charts/weir --namespace application \
  --kube-context your-context -f examples/search.yaml --atomic --wait --timeout 5m
```

The Chart generates `node.yaml`, the listener port and diagnostics access. Clients connect with `weir.New` to the group's headless Service, `<release>-weir.<namespace>.svc:<service.port>`. It resolves to every Ready Pod, and the SDK spreads business RPCs across them. Weir ends each client connection after about five minutes; the SDK then re-resolves the name, so Pods added by a scale-out or rollout receive calls within that window. Generated `[::]` listeners accept IPv4 and IPv6 on the supported Go runtime. Group identity is `namespace/release`, so metric labels remain stable when `fullnameOverride` changes. The Service selects only its release. Allow clients to reach each group's application port.

## Process and credentials

`nodeConfig` accepts process policy: `lua`, `transport`, `overload.memory` and `diagnostics.timeout`. Its schema rejects unknown keys, and the selected Weir binary validates the values. The Chart owns `lifecycle`, `listeners`, `diagnostics.address` and `diagnostics.allow_intranet`; attempts to override them through `nodeConfig` fail before installation. The top-level `lifecycle` deployment profile supplies integer seconds for both the generated Weir configuration and Kubernetes budgets. Application and diagnostics ports must be distinct. The default client NetworkPolicy uses the named `grpc` port, so it follows `service.port` automatically. Weir detects process/container memory limits automatically; set Kubernetes resource limits for actual Pod capacity.

Store `queue.max_operations/max_bytes` bound waiting work, and `batching.max_operations/max_bytes` bound physical batches. Dispatch releases queue capacity. Business execution concurrency follows the caller context. Process Lua and transport settings apply to every Store in this group.

Adapter settings stay in `backend.mongodb` or `backend.search`. MongoDB exposes `max_connecting`, `metadata_cache_entries`, `retry_attempts` and `retry_backoff` directly; Search exposes `retry_attempts` and `idle_timeout` directly. The Chart passes supplied values through without filling server defaults. Its backend schema rejects unknown fields, and the selected Weir binary validates adapter semantics.

Helm retains values in release history. Inline backend credentials and URI userinfo are rejected. MongoDB and Search use `backend.authentication.username_file/password_file`; mount an externally managed Secret read-only using `extraVolumes/extraVolumeMounts`. MongoDB requires SCRAM-SHA-256, authSource and TLS in the URI when authenticating. Both adapters can use `backend.tls.ca_file`, or system trust when it is absent.

Alternatively, set `storesSecret` to an externally provisioned Secret containing **only `routes.yaml`**, with the upstream `stores` list. It is mutually exclusive with a non-empty inline `stores`. `node.yaml` always comes from the Chart's ConfigMap. The projected `/etc/weir` mount combines the generated node and selected routes read-only; the Chart never reads or renders the existing Secret. See [examples/existing-secret.yaml](examples/existing-secret.yaml). Version the Secret or change `storesRevision` to roll Pods after a coordinated update; CA/auth file changes require the same restart.

Validate the generated `node.yaml` and selected `routes.yaml` with `weir check --config ... --routes ...` from the pinned Weir source before deployment. Credential-file validation reads those supplied files and requires their authorization. ConfigMap updates automatically change the Pod checksum.

## Prometheus and KEDA

Enable `metrics.enabled` and authorize scraper sources in `metrics.ingress`. The Chart opens the diagnostics listener for metrics and creates a separate metrics Service. With default NetworkPolicy, metrics enablement requires explicit scraper rules. Annotations can support an existing scraper; `metrics.serviceMonitor.enabled` creates a ServiceMonitor when Prometheus Operator is already installed. Its selector matches exactly this release's metrics Service, and Prometheus discovers each Pod endpoint separately. `metrics.serviceMonitor.labels` must match your Prometheus instance's monitor selector; Chart identity/component labels cannot be overridden.

ServiceMonitor target relabeling supplies `namespace` and `weir_group="namespace/release"`. Native Weir metrics also carry `store` where applicable. With a custom scraper, supply the same stable labels and avoid duplicate scrape jobs. Scrape all group replicas; querying only the ordinary metrics ClusterIP would sample one Pod.

Overlay [examples/prometheus-autoscaling.yaml](examples/prometheus-autoscaling.yaml) on the `mongo` release in namespace `application`. Adjust the namespace, release identity, Prometheus endpoint and scraper labels for your deployment. Its query aggregates this exact group's queued operations and active executions into one value; `AverageValue` lets HPA divide that total across replicas. The threshold is a workload target per replica, not a built-in execution limit; choose it from measured backend and application capacity.

The example guards its aggregate with `and on() (min(up{namespace="application",weir_group="application/mongo"}) == 1)`. A failed discovered scrape target or no discovered targets produces an empty result; `ignoreNullValues: "false"` then leaves KEDA in error instead of treating an incomplete lower workload as a valid scale-down signal. The HTTP timeout is 10 seconds, and no `or vector(0)` supplies a false zero. This guard covers the targets Prometheus has discovered; it cannot prove that scraper configuration includes every group Pod. Before enabling autoscaling, compare the per-Pod targets with all running/Ready group replicas and verify the labels and both gauges are present; repeat that coverage check after scale and rollout changes. Configure an existing `TriggerAuthentication`/`ClusterTriggerAuthentication` through `authenticationRef` if needed. No controller credentials are rendered or read by this Chart. KEDA's operator, metrics server and relevant network rules must already allow its Prometheus query. See [the Prometheus scaler contract](https://keda.sh/docs/2.20/scalers/prometheus/) and [ScaledObject specification](https://keda.sh/docs/2.20/reference/scaledobject-spec/).

`autoscaling.triggers` accepts native KEDA triggers, including Prometheus and other supported scalers. Prometheus triggers require metrics to be enabled; CRDs/controllers are external prerequisites. `pollingInterval`, `cooldownPeriod` and `scaleDown` configure polling and HPA scale-down behavior. The cooldown setting concerns KEDA activation/scale-to-zero; ordinary reduction between running replica counts is controlled by HPA behavior. Minimum replicas must stay at least one: SDK connections cannot wake a group with zero replicas. A PDB requires at least two minimum replicas. When autoscaling is enabled, the Deployment omits `replicas` so future Helm upgrades do not reset the controller's scale. Verify the ScaledObject becomes Ready and HPA reaches its minimum before admitting production traffic; Helm readiness alone does not establish controller readiness. Disabling KEDA resumes the configured `replicaCount`.

## Network and operations

Application traffic uses plaintext HTTP/2 inside a trusted, isolated network. Default NetworkPolicy permits same-namespace clients labeled `weir-client: "true"` to call the application port, plus DNS egress. Its named `grpc` rule follows `service.port` automatically (default 7447). Backend egress remains explicit. Verify the CNI enforces both allowed and denied traffic; additive allow-all policies can bypass restrictions.

One replica has replacement downtime. For three replicas use [examples/three-replicas.yaml](examples/three-replicas.yaml); update its release-specific spread selector. Its `nodeTaintsPolicy: Honor` excludes untolerated nodes from the spread calculation. The optional PDB limits voluntary disruptions and does not establish physical fault tolerance by itself.

Kubernetes resource names and DNS endpoints follow their native format limits. Size groups and replicas from measured process, network and backend capacity, including starting and terminating instances.

Each default Pod requests and limits 2 CPU / 3 GiB. Budget starting, running and terminating processes and backend connection demand together. Kubernetes does not impose a hard bound on concurrent terminating Pods; zero-surge rollout does not eliminate old remote work after connection loss. Large workloads need measured admission/latency and backend capacity verification.

The Deployment waits `minReadySeconds: 15` after each replacement becomes Ready. The startup failure threshold is `ceil(lifecycle.startupTimeoutSeconds / 10) + 1`, using Kubernetes' native 10-second probe period. `progressDeadlineSeconds` is an explicit Deployment rollout budget (Chart default 180 seconds), including time for scheduling and image pulls. It must strictly exceed `minReadySeconds` plus the startup failure threshold times the longer of the native probe period and the kubelet probe timeout. Insufficient budgets fail validation; the Chart never silently changes the configured deadline. Deployment timing fields retain Kubernetes' int32 bounds. Minimum readiness time leaves space for clients to re-resolve the headless Service while old replicas remain.

Before SIGTERM, kubelet's `preStop.sleep: 15` lets terminating Pod addresses leave Ready headless DNS without another shell/worker process. Weir then withdraws readiness and drains admitted work within `lifecycle.shutdownTimeoutSeconds` (Chart default 5). Each Store then receives one `lifecycle.cleanupTimeoutSeconds` budget (Chart default 2) to cancel remaining work, end backend sessions and close connections; Stores clean up in parallel. With `terminationGracePeriodSeconds: 0`, the Pod grace is derived as preStop plus shutdown timeout plus cleanup timeout plus one second for final process exit (23 seconds by default). That extra second is the smallest integer slack above the bounded lifecycle phases; reserve more measured exit overhead with a positive override, which must cover the full derived budget. Lifecycle values are positive integer seconds and are converted to Weir duration strings without parsing another configuration format. The native sleep hook requires Kubernetes 1.30+'s default PodLifecycleSleepAction feature. Disabling the hook removes its retirement protection.

Probes execute `/weir probe ready|live` against loopback `diagnosticsPort` with the deadline in `probeTimeoutMilliseconds` (default 750). Their kubelet timeout rounds that deadline up to whole seconds and adds one second to let the command exit; the Deployment progress budget also covers that deadline. Readiness describes lifecycle; validate real business operations separately. A sent mutation without a terminal response has an unknown outcome and requires reconciliation before retrying.

Readiness and liveness use Kubernetes' native 10-second probe period and three consecutive failures. Startup also uses the native period, with its threshold derived above. Deployment revision retention uses Kubernetes' native default. These defaults are documented in the [probe API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#Probe) and [Deployment API](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#clean-up-policy).

Changes to Store membership or backend identity require a coordinated cutover. An ordinary rolling update temporarily runs old and new definitions in one group and does not provide a transactional migration. Stop or redirect affected clients, drain the old replicas, apply the reviewed configuration/image pair and verify actual data operations before resuming.

## Develop and release

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
make package PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
# Optional real PromQL regression, using a separately installed official promtool:
promtool test rules tests/prometheus/autoscaling-query.test.yaml
```

`make test` lints the Chart, renders every example and validates the generated configuration with the selected Weir binary, including a scale check with many Stores and replicas and an isolated loopback-only serve/probe/shutdown run. It opens no backend connections, reads no existing Secrets and does not exercise CNI enforcement, Prometheus collection or KEDA scaling in a real cluster; the optional promtool command checks the autoscaling query against its fixture.

A reviewed `weir-VERSION` tag publishes the immutable OCI Chart and GitHub archive with SHA256SUMS after preflight. Source annotation, image digest and CI source pins must identify the same server revision.
