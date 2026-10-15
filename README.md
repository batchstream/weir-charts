# Weir Helm charts

Deploy [Weir](https://github.com/batchstream/weir), a synchronous MongoDB and Search data plane, on Kubernetes. Helm 3.17+ and Kubernetes 1.30+ are required. Chart 0.9.0 introduces the integer lifecycle deployment profile and flat adapter settings. `Chart.yaml` records its server source contract and `values.yaml` pins the immutable image digest.

The unreleased working tree now uses `lifecycle.cleanup_timeout`, discovery sync policy and `probe --timeout`, which require a matching new Weir server image. The recorded 0.8.0 image digest cannot run these new Chart defaults: release the matching server image and update the image digest, Chart source identity and CI source pins together before publishing or deploying this version.

One Helm release is one Store owner group. Every replica owns the same Store definitions; a group can host several Stores. Different releases join a Weir Mesh through peer directory synchronization, and each group can be released and scaled independently. Each Pod runs one Weir process with Lua inside that process. Backends, collections, indices, credentials and backups are provisioned separately.

## Store groups

Declare this group's Stores directly in `stores`:

```yaml
mesh:
  name: application
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

Database, collection and index targets come from request URIs; Chart configuration declares the backend connection for each Store. Every Store name belongs to one owner group in a Mesh. Overlapping names in different groups produce a discovery conflict, rather than selecting a backend arbitrarily. Multiple replicas within a group use identical Stores and settings.

Install [examples/mongo.yaml](examples/mongo.yaml) and [examples/search.yaml](examples/search.yaml) as separate groups in the same namespace:

```sh
helm upgrade --install mongo ./charts/weir --namespace application \
  --kube-context your-context -f examples/mongo.yaml --atomic --wait --timeout 5m
helm upgrade --install search ./charts/weir --namespace application \
  --kube-context your-context -f examples/search.yaml --atomic --wait --timeout 5m
```

The Chart generates `node.yaml`, listener ports, diagnostics access and discovery. Group identity is `namespace/release`, so it remains stable when `fullnameOverride` changes. The advertised business endpoint is the group's `<release>-weir-headless.<namespace>.svc.<clusterDomain>:<service.port>`. The SDK resolves its Ready Pod addresses and balances business RPCs within that group; a Kubernetes ClusterIP alone does not rebalance an established gRPC connection. Set `clusterDomain` if the cluster differs from `cluster.local`. Generated `[::]` listeners accept IPv4 and IPv6 on the supported Go runtime; Pod peer advertisements use bracketed Pod IPs.

Each enabled group creates its own `<release>-weir-mesh` ordinary ClusterIP Service. It selects all enabled Pods with the same explicit `mesh.name` label in its namespace, exposing application initialization and peer bootstrap ports. Every group uses its own seed Service to discover the shared Mesh. Service names remain release-owned, so no shared Helm object needs an owner flag. Ordinary `<release>-weir` and headless business Services select only their release.

`mesh.enabled` defaults to true and `mesh.name` defaults to `weir`. Use different names to isolate Mesh membership. A Pod with `mesh.enabled: false` has no membership label or peer listener and cannot enter another group's Mesh seed Service. Additional `mesh.seeds` are appended to the generated seed; they are explicit peer endpoints. Cross-namespace Mesh synchronization requires explicit ingress and egress rules on both sides. A Mesh name is a same-namespace bootstrap/network selector, not a protocol-level authentication boundary; a deliberately configured external seed can connect directories across that boundary.

SDK initialization may use any Mesh seed Service's application endpoint. Discovery then selects the owner group's business endpoints; peer traffic synchronizes directory information and does not proxy business requests through other groups. Allow SDK clients to reach each owner group's application port.

## Process and credentials

`nodeConfig` accepts process policy: `lua`, `transport`, `overload`, optional `discovery.lease/sync_interval/sync_timeout` and `diagnostics.timeout`. The Chart owns `lifecycle`, `listeners`, discovery identity/addresses, `diagnostics.address` and `diagnostics.allow_intranet`; attempts to override them through `nodeConfig` fail before installation. The top-level `lifecycle` deployment profile supplies integer seconds for both the generated Weir configuration and Kubernetes budgets. Application, diagnostics and enabled peer ports must be distinct. The default client NetworkPolicy uses the named `grpc` port, so it follows `service.port` automatically. Weir detects process/container memory limits automatically; set Kubernetes resource limits for actual Pod capacity.

Store `queue.max_operations/max_bytes` bound waiting work, and `batching.max_operations/max_bytes` bound physical batches. Dispatch releases queue capacity. Business execution follows the caller context without a fixed Weir concurrency cap. Process Lua and transport settings apply to every Store in this group; Store scan settings bound retained fetch/publication batches.

Adapter settings stay in `backend.mongodb` or `backend.search`. MongoDB exposes `max_connecting`, `metadata_cache_entries`, `retry_attempts` and `retry_backoff` directly; Search exposes `retry_attempts`, `pit_keep_alive` and `idle_timeout` directly. The Chart passes supplied values through without filling server defaults. Its backend schema rejects unknown fields, and the selected Weir binary validates adapter semantics.

Helm retains values in release history. Inline backend credentials and URI userinfo are rejected. MongoDB and Search use `backend.authentication.username_file/password_file`; mount an externally managed Secret read-only using `extraVolumes/extraVolumeMounts`. MongoDB requires SCRAM-SHA-256, authSource and TLS in the URI when authenticating. Both adapters can use `backend.tls.ca_file`, or system trust when it is absent.

Alternatively, set `storesSecret` to an externally provisioned Secret containing **only `routes.yaml`**, with the upstream `stores` list. It is mutually exclusive with a non-empty inline `stores`. `node.yaml` always comes from the Chart's ConfigMap. The projected `/etc/weir` mount combines the generated node and selected routes read-only; the Chart never reads or renders the existing Secret. See [examples/existing-secret.yaml](examples/existing-secret.yaml). Version the Secret or change `storesRevision` to roll Pods after a coordinated update; CA/auth file changes require the same restart.

Validate the generated `node.yaml` and selected `routes.yaml` with the compatible `weir check --config ... --routes ...` command before deployment. Credential-file validation reads those supplied files and requires their authorization. ConfigMap updates automatically change the Pod checksum.

## Prometheus and KEDA

Enable `metrics.enabled` and authorize scraper sources in `metrics.ingress`. The Chart opens the diagnostics listener for metrics and creates a separate metrics Service. With default NetworkPolicy, metrics enablement requires explicit scraper rules. Annotations can support an existing scraper; `metrics.serviceMonitor.enabled` creates a ServiceMonitor when Prometheus Operator is already installed. Its selector matches exactly this release's metrics Service, and Prometheus discovers each Pod endpoint separately. `metrics.serviceMonitor.labels` must match your Prometheus instance's monitor selector; Chart identity/component labels cannot be overridden.

ServiceMonitor target relabeling supplies `namespace`, `weir_group="namespace/release"` and `weir_mesh`. Native Weir metrics also carry `store` where applicable. With a custom scraper, supply the same stable labels and avoid duplicate scrape jobs. Scrape all group replicas; querying only the ordinary metrics ClusterIP would sample one Pod.

Overlay [examples/prometheus-autoscaling.yaml](examples/prometheus-autoscaling.yaml) on the `mongo` release in namespace `application`. Adjust the namespace, release identity, Prometheus endpoint and scraper labels for your deployment. Its query aggregates this exact group's pending operations and active executions into one value; `AverageValue` lets HPA divide that total across replicas. The threshold is a workload target per replica, not a built-in execution limit; choose it from measured backend and application capacity.

The example guards its aggregate with `and on() (min(up{namespace="application",weir_group="application/mongo"}) == 1)`. A failed discovered scrape target or no discovered targets produces an empty result; `ignoreNullValues: "false"` then leaves KEDA in error instead of treating an incomplete lower workload as a valid scale-down signal. The HTTP timeout is 10 seconds, and no `or vector(0)` supplies a false zero. This guard covers the targets Prometheus has discovered; it cannot prove that scraper configuration includes every group Pod. Before enabling autoscaling, compare the per-Pod targets with all running/Ready group replicas and verify the labels and both gauges are present; repeat that coverage check after scale and rollout changes. Configure an existing `TriggerAuthentication`/`ClusterTriggerAuthentication` through `authenticationRef` if needed. No controller credentials are rendered or read by this Chart. KEDA's operator, metrics server and relevant network rules must already allow its Prometheus query. See [the Prometheus scaler contract](https://keda.sh/docs/2.20/scalers/prometheus/) and [ScaledObject specification](https://keda.sh/docs/2.20/reference/scaledobject-spec/).

`autoscaling.triggers` accepts native KEDA triggers, including Prometheus and other supported scalers. Prometheus triggers require metrics to be enabled; CRDs/controllers are external prerequisites. `pollingInterval`, `cooldownPeriod` and `scaleDown` configure polling and HPA scale-down behavior. The cooldown setting concerns KEDA activation/scale-to-zero; ordinary reduction between running replica counts is controlled by HPA behavior. Minimum replicas must stay at least one: direct SDK connections and peer discovery cannot wake a group with zero owners. A PDB requires at least two minimum replicas. When autoscaling is enabled, the Deployment omits `replicas` so future Helm upgrades do not reset the controller's scale. Verify the ScaledObject becomes Ready and HPA reaches its minimum before admitting production traffic; Helm readiness alone does not establish controller readiness. Disabling KEDA resumes the configured `replicaCount`.

## Network and operations

Application and peer traffic use plaintext HTTP/2 inside a trusted, isolated network. Default NetworkPolicy permits same-namespace clients labeled `weir-client: "true"` to call the application port, plus DNS egress. Its named `grpc` rule follows `service.port` automatically (default 7447). The Chart adds same-Mesh peer ingress/egress automatically using the destination's named `peer` port, so groups can use different peer port numbers. Backend egress and additional seed destinations remain explicit. Verify the CNI enforces both allowed and denied traffic; additive allow-all policies can bypass restrictions.

One replica has replacement downtime. For three replicas use [examples/three-replicas.yaml](examples/three-replicas.yaml); update its release-specific spread selector. Its `nodeTaintsPolicy: Honor` excludes untolerated nodes from the spread calculation. The optional PDB limits voluntary disruptions and does not establish physical fault tolerance by itself.

The Chart imposes no Store, seed or replica count ceiling. Store names follow the public protocol syntax without a Chart length quota. Kubernetes resource names and DNS endpoints still follow their native format limits. Size groups and replicas from measured process, network and backend capacity, including starting and terminating instances.

Each default Pod requests and limits 2 CPU / 3 GiB. Budget starting, running and terminating processes and backend connection demand together. Kubernetes does not impose a hard bound on concurrent terminating Pods; zero-surge rollout does not eliminate old remote work after connection loss. Large workloads need measured admission/latency and backend capacity verification.

The Deployment waits `minReadySeconds: 15` after each replacement becomes Ready. The startup probe allows `lifecycle.startupTimeoutSeconds` (Chart default 5) plus one probe period; its failure threshold is derived automatically. The progress deadline covers this probe budget and `minReadySeconds`, with a floor of 180 seconds. Values that cannot fit Kubernetes' int32 progress deadline are rejected. This leaves time for SDK discovery/DNS refresh while old replicas remain.

Before SIGTERM, kubelet's `preStop.sleep: 15` lets terminating Pod addresses leave Ready headless DNS without another shell/worker process. Weir then withdraws readiness and drains admitted work within `lifecycle.shutdownTimeoutSeconds` (Chart default 5). The following cancellation and disconnection cleanup phases each receive `lifecycle.cleanupTimeoutSeconds` (Chart default 2); this is a per-phase budget. With `terminationGracePeriodSeconds: 0`, the Pod grace is derived as preStop plus shutdown timeout plus two cleanup phases plus one second for final process exit (25 seconds by default). That extra second is the smallest integer slack above the bounded lifecycle phases; reserve more measured exit overhead with a positive override, which must cover the full derived budget. Lifecycle values are positive integer seconds and are converted to Weir duration strings without parsing another configuration format. The native sleep hook requires Kubernetes 1.30+'s default PodLifecycleSleepAction feature. Disabling the hook removes its retirement protection.

Probes execute `/weir probe ready|live` against loopback `diagnosticsPort` with the deadline in `probeTimeoutMilliseconds` (default 750). Their kubelet timeout rounds that deadline up to whole seconds and adds one second to let the command exit; the Deployment progress budget also covers that deadline. Readiness describes lifecycle; validate real business operations separately. A sent mutation without a terminal response has an unknown outcome and requires reconciliation before retrying.

Changes to Store ownership, Store membership or backend identity require a coordinated cutover. An ordinary rolling update temporarily runs old and new definitions in one group and does not provide a transactional migration. Stop or redirect affected clients, drain the old ownership, apply the reviewed configuration/image pair and verify discovery and actual data operations before resuming. Compatible process/image updates still require upgrade/rollback qualification.

## Develop and release

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
make package PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
# Optional real PromQL regression, using a separately installed official promtool:
promtool test rules tests/prometheus/autoscaling-query.test.yaml
```

CI builds the pinned Weir source contract before validating rendered configurations. Tests cover generated Store groups, Mesh selectors/policies, Secret projection, metrics/KEDA objects, source identity, authentication fixtures and an isolated no-Store serve/probe/shutdown check. The selected Weir binary also validates a generated configuration with 32 Stores, a 256-character Store name and 33 total seeds; fixed and autoscaled 128-replica manifests and extended lifecycle timers are rendered without starting those replicas. The Prometheus regression fixture is bound to the actual example query and covers healthy targets, a failed target with still-retained samples, partial stale data, recovery, unrelated groups and missing targets. Execute it with an official promtool to test PromQL semantics; the Python suite checks that every fixture expression remains the example query. That contract test opens no backend connections and reads no existing Secrets. Real Mesh discovery, CNI enforcement, per-Pod Prometheus collection and KEDA scaling require isolated cluster qualification.

A reviewed `weir-VERSION` tag publishes the immutable OCI Chart and GitHub archive with SHA256SUMS after preflight. Source annotation, image digest and CI source pins must identify the same server revision; the current recorded pins still identify Weir commit `34711a930d259488d13b9c561fe11dcf91c7770c` pending the matching 0.9.0 server image update. Historical [acceptance fixtures](tests/acceptance/README.md) and [v0.2.0 release evidence](docs/acceptance-2026-10-12-release.md) describe their recorded versions; they do not qualify the new Chart's Mesh/monitor/autoscaling behavior or continuous 24-hour production acceptance.
