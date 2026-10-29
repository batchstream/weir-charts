# Weir Helm charts

Deploy [Weir](https://github.com/batchstream/weir), a synchronous MongoDB and Search data plane, on Kubernetes. Helm 3.17+ and Kubernetes 1.30+ are required. Chart `0.12.0-rc.1` targets server source [commit `b0687cc450fb`](https://github.com/batchstream/weir/commit/b0687cc450fb0133a0b2e0592a5058ec797ba47f). `Chart.yaml` records that source contract and `values.yaml` pins the immutable image `ghcr.io/batchstream/weir@sha256:ff5cc59ac80a9251258d4a9bd738093e57345af16b6a20238acecd79896f9a1e`; both validation and release workflows install that exact source with `go install`.

One Helm release is one Store group. Every replica hosts the same Store definitions; a group can host several Stores, and each group is released and scaled independently. Replicas share nothing with each other. Each Pod runs one Weir process with Lua inside that process. Backends, collections, indices, credentials and backups are provisioned separately.

## Store groups

Declare this group's Stores directly in `stores`:

```yaml
stores:
  - name: primary
    mongodb:
      uri: mongodb://mongo:27017/?directConnection=true
  - name: search
    search:
      url: http://search:9200
    batching:
      max_wait: 5ms
```

Each entry uses Weir's own Store schema: `name`, exactly one of `mongodb.uri` or `search.url`, optional file-based `authentication`, optional `tls.ca_file` and optional `batching` (`max_operations`, `max_bytes`, `max_wait`, `max_in_flight`). Database, collection and index targets come from request URIs; Chart configuration declares the backend connection for each Store. Multiple replicas within a group use identical Stores and settings. A request for a Store the group does not host fails with NotFound.

Install [examples/mongo.yaml](examples/mongo.yaml) and [examples/search.yaml](examples/search.yaml) as separate groups in the same namespace:

```sh
helm upgrade --install mongo ./charts/weir --namespace application \
  --kube-context your-context -f examples/mongo.yaml --atomic --wait --timeout 5m
helm upgrade --install search ./charts/weir --namespace application \
  --kube-context your-context -f examples/search.yaml --atomic --wait --timeout 5m
```

The Chart generates `weir.yaml` with the listeners, `shutdown_timeout` and `stores`. Clients connect with `weir.New` to the group's headless Service, `<release>-weir.<namespace>.svc:<service.port>`. It resolves to every Ready Pod, and the SDK spreads business RPCs across them. Weir ends each client connection after about five minutes; the SDK then re-resolves the name, so Pods added by a scale-out or rollout receive calls within that window. Generated `[::]` listeners accept IPv4 and IPv6 on the supported Go runtime. Group identity is `namespace/release`, so metric labels remain stable when `fullnameOverride` changes. The Service selects only its release. Allow clients to reach each group's application port.

## Configuration and credentials

The Chart renders one `weir.yaml` into its ConfigMap and mounts it read-only at `/etc/weir`. Its schema rejects unknown keys; the selected Weir binary validates backend semantics. Weir fixes transport, memory-pressure, Lua, queue and retry policy internally, so only listeners, the shutdown drain and Stores are configurable. Weir sets its Go memory limit to 90% of the container memory limit and refuses new work as live memory nears it, so keep a memory limit in `resources`; without one Weir does not shed load.

Helm retains values in release history. Inline backend credentials are rejected, and so is any `@` in `mongodb.uri` or `search.url`, since it can only carry userinfo. MongoDB and Search use `authentication.username_file/password_file`; mount an externally managed Secret read-only using `extraVolumes/extraVolumeMounts` and reference it by absolute path, since relative paths resolve from `/etc/weir`; [examples/credential-files.yaml](examples/credential-files.yaml) shows the layout. MongoDB requires SCRAM-SHA-256, authSource and TLS in the URI when authenticating. Both adapters can use `tls.ca_file`, or system trust when it is absent. Credential and CA files are read at startup; roll the Pods after rotating them, for example by changing a `podAnnotations` entry.

Validate the generated `weir.yaml` with `weir check --config ...` from the pinned Weir source before deployment. Credential-file validation reads those supplied files and requires their authorization. ConfigMap updates automatically change the Pod checksum.

## Prometheus and KEDA

Enable `metrics.enabled` and authorize scraper sources in `metrics.ingress`. The diagnostics listener is always bound for the probes; `metrics.enabled` adds the NetworkPolicy ingress for scrapers and a separate metrics Service. Reference the diagnostics port by its `metrics` name in ingress rules so a changed `diagnosticsPort` keeps matching. With default NetworkPolicy, metrics enablement requires explicit scraper rules. Annotations can support an existing scraper; `metrics.serviceMonitor.enabled` creates a ServiceMonitor when Prometheus Operator is already installed. Its selector matches exactly this release's metrics Service, and Prometheus discovers each Pod endpoint separately. `metrics.serviceMonitor.labels` must match your Prometheus instance's monitor selector; Chart identity/component labels cannot be overridden.

ServiceMonitor target relabeling supplies `namespace` and `weir_group="namespace/release"`. Native Weir metrics also carry `store` where applicable. With a custom scraper, supply the same stable labels and avoid duplicate scrape jobs. Scrape all group replicas; querying only the ordinary metrics ClusterIP would sample one Pod.

Overlay [examples/prometheus-autoscaling.yaml](examples/prometheus-autoscaling.yaml) on the `mongo` release in namespace `application`. Adjust the namespace, release identity, Prometheus endpoint and scraper labels for your deployment. Its query aggregates this exact group's queued operations and active executions into one value; `AverageValue` lets HPA divide that total across replicas. The threshold is a workload target per replica, not a built-in execution limit; choose it from measured backend and application capacity.

The example guards its aggregate with `and on() (min(up{namespace="application",weir_group="application/mongo"}) == 1)`. A failed discovered scrape target or no discovered targets produces an empty result; `ignoreNullValues: "false"` then leaves KEDA in error instead of treating an incomplete lower workload as a valid scale-down signal. The HTTP timeout is 10 seconds, and no `or vector(0)` supplies a false zero. This guard covers the targets Prometheus has discovered; it cannot prove that scraper configuration includes every group Pod. Before enabling autoscaling, compare the per-Pod targets with all running/Ready group replicas and verify the labels and both gauges are present; repeat that coverage check after scale and rollout changes. Configure an existing `TriggerAuthentication`/`ClusterTriggerAuthentication` through `authenticationRef` if needed. No controller credentials are rendered or read by this Chart. KEDA's operator, metrics server and relevant network rules must already allow its Prometheus query. See [the Prometheus scaler contract](https://keda.sh/docs/2.20/scalers/prometheus/) and [ScaledObject specification](https://keda.sh/docs/2.20/reference/scaledobject-spec/).

`autoscaling.triggers` accepts native KEDA triggers, including Prometheus and other supported scalers. Prometheus triggers require metrics to be enabled; CRDs/controllers are external prerequisites. `pollingInterval`, `cooldownPeriod` and `scaleDown` configure polling and HPA scale-down behavior. The cooldown setting concerns KEDA activation/scale-to-zero; ordinary reduction between running replica counts is controlled by HPA behavior. Minimum replicas must stay at least one: SDK connections cannot wake a group with zero replicas. A PDB requires at least two minimum replicas. When autoscaling is enabled, the Deployment omits `replicas` so future Helm upgrades do not reset the controller's scale. Verify the ScaledObject becomes Ready and HPA reaches its minimum before admitting production traffic; Helm readiness alone does not establish controller readiness. Disabling KEDA resumes the configured `replicaCount`.

## Network and operations

Application traffic uses plaintext HTTP/2 inside a trusted, isolated network. Default NetworkPolicy permits same-namespace clients labeled `weir-client: "true"` to call the application port, plus DNS egress. Its named `grpc` rule follows `service.port` automatically (default 7447). Backend egress remains explicit. Verify the CNI enforces both allowed and denied traffic; additive allow-all policies can bypass restrictions.

One replica has replacement downtime. For three replicas use [examples/three-replicas.yaml](examples/three-replicas.yaml); update its release-specific spread selector. Its `nodeTaintsPolicy: Honor` excludes untolerated nodes from the spread calculation. The optional PDB limits voluntary disruptions and does not establish physical fault tolerance by itself.

Kubernetes resource names and DNS endpoints follow their native format limits. Size groups and replicas from measured process, network and backend capacity, including starting and terminating instances.

Each default Pod requests and limits 2 CPU / 3 GiB. Budget starting, running and terminating processes and backend connection demand together. Kubernetes does not impose a hard bound on concurrent terminating Pods; zero-surge rollout does not eliminate old remote work after connection loss. Large workloads need measured admission/latency and backend capacity verification.

The Deployment waits `minReadySeconds: 15` after each replacement becomes Ready. Weir bounds its own startup at 30 seconds. The first startup probe can land anywhere in the first 10-second native period, so the probe allows five failures, killing a Pod that has not started 40 to 50 seconds after its container starts. `progressDeadlineSeconds` is an explicit Deployment rollout budget (Chart default 180 seconds), including time for scheduling and image pulls. It must strictly exceed `minReadySeconds` plus that 50-second startup budget. Insufficient budgets fail validation; the Chart never silently changes the configured deadline. Deployment timing fields retain Kubernetes' int32 bounds. Minimum readiness time leaves space for clients to re-resolve the headless Service while old replicas remain.

Before SIGTERM, kubelet's `preStop.sleep: 15` lets terminating Pod addresses leave Ready headless DNS without another shell/worker process. Weir then withdraws readiness and drains admitted work within `shutdownTimeoutSeconds` (Chart default 5). Each Store then receives Weir's fixed 2-second cleanup budget to cancel remaining work, end backend sessions and close connections; Stores clean up in parallel. With `terminationGracePeriodSeconds: 0`, the Pod grace is derived as preStop plus shutdown timeout plus 2 seconds of cleanup plus one second for final process exit (23 seconds by default). That extra second is the smallest integer slack above the bounded lifecycle phases; reserve more measured exit overhead with a positive override, which must cover the full derived budget. `shutdownTimeoutSeconds` is a positive integer and becomes Weir's `shutdown_timeout` duration string. The native sleep hook requires Kubernetes 1.30+'s default PodLifecycleSleepAction feature. Disabling the hook removes its retirement protection.

Kubelet probes are HTTP GETs of `/readyz` (startup and readiness) and `/livez` (liveness) on `diagnosticsPort`, which Weir always binds on `[::]`. The same port serves `/metrics`; without `metrics.enabled`, the default NetworkPolicy admits no other sources to it. Readiness describes lifecycle; validate real business operations separately. A sent mutation without a terminal response has an unknown outcome and requires reconciliation before retrying.

Readiness and liveness use Kubernetes' native 10-second probe period, one-second timeout and three consecutive failures. Startup also uses the native period, with its threshold derived above. Deployment revision retention uses Kubernetes' native default. These defaults are documented in the [probe API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#Probe) and [Deployment API](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#clean-up-policy).

Changes to Store membership or backend identity require a coordinated cutover. An ordinary rolling update temporarily runs old and new definitions in one group and does not provide a transactional migration. Stop or redirect affected clients, drain the old replicas, apply the reviewed configuration/image pair and verify actual data operations before resuming.

## Develop and release

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
go install github.com/batchstream/weir/cmd/weir@SOURCE_REVISION
make test PYTHON=.venv/bin/python WEIR_BIN="$(go env GOPATH)/bin/weir"
make package PYTHON=.venv/bin/python WEIR_BIN="$(go env GOPATH)/bin/weir"
# Optional real PromQL regression, using a separately installed official promtool:
promtool test rules tests/prometheus/autoscaling-query.test.yaml
```

`SOURCE_REVISION` is the `weir.batchstream.io/source-revision` annotation in `Chart.yaml`. `make test` lints the Chart, renders every example and validates the generated configuration with the selected Weir binary, including a scale check with many Stores and replicas and an isolated loopback-only serve, HTTP probe and shutdown run. It opens no backend connections, reads no existing Secrets and does not exercise CNI enforcement, Prometheus collection or KEDA scaling in a real cluster; the optional promtool command checks the autoscaling query against its fixture.

A reviewed `weir-VERSION` tag on `main` publishes the OCI Chart and a GitHub release with the archive and SHA256SUMS. The release is created first, so rerunning a published version fails instead of overwriting its Chart. Source annotation, image digest and CI source pins must identify the same server revision.
