# Weir Helm charts

Deploy [Weir](https://github.com/batchstream/weir), a synchronous MongoDB
and Search data plane, on Kubernetes. Helm 3.17+ and Kubernetes 1.30+ are required.
Chart 0.3.0 targets the source contract recorded in Chart.yaml's
`weir.batchstream.io/source-revision` annotation. Older configuration using manual memory, concurrency and business timeout
fields must be updated for this contract.

Each release runs one replica group, with one Weir process per Pod and Lua inside
that process. Backends, collections, indices, backups and storage are provisioned
separately. Application and peer traffic use plaintext HTTP/2 inside a trusted,
isolated network. Historical cluster evidence is retained in
[docs/acceptance.md](docs/acceptance.md); it does not qualify this new Chart version.

## Install

Build or obtain an image compatible with the recorded source contract, then record
its immutable digest in your reviewed values. `image.digest` is required. This
Chart does not assume that a new image has been published. Template tests use a
clearly marked dummy digest in `tests/values.yaml`; that fixture is unsuitable
for installation.

Provision an external Secret with **both** `node.yaml` and `routes.yaml`, and set
`config.existingSecret` to its name. `node.yaml` uses the upstream basic YAML
schema; `routes.yaml` contains `stores` with `mongodb` or `search` backend settings.
The deployment starts:

```sh
/weir serve --config /etc/weir/node.yaml --routes /etc/weir/routes.yaml
```

The application listener must bind `0.0.0.0:<service.port>`, and diagnostics must
bind `127.0.0.1:<diagnostics.port>`. Weir detects the visible container, host and
process memory limits automatically; there is no process `memory` configuration.
Set Kubernetes resource limits for the actual Pod capacity. Each Store exposes
`batch_queue.max_operations` and `batch_queue.max_bytes` for waiting work, plus
`max_batch_operations` and `max_batch_bytes` for physical batches. Dispatch returns
queue capacity immediately. Business execution follows the client context without
a Weir concurrency or connection cap. For peer synchronization, enable `peer.enabled`, bind `0.0.0.0:<peer.port>` and select
`discovery.peer_address_env: WEIR_PEER_ADDRESS`. The Pod supplies this variable
through its public Pod IP and peer port, with brackets that work for IPv4 and IPv6.
The examples use IPv4 wildcard listeners; for an IPv6 Pod network, bind application
and peer listeners to `[::]:<port>` instead. All replicas of a release must share the
same `discovery.group`, Store definitions and business advertisement.

Backend database/collection/index targets come from request URIs, rather than
Chart routing configuration. Authenticated MongoDB uses `mongodb.username_file`
and `mongodb.password_file`, with explicit SCRAM-SHA-256, authSource and TLS in
the MongoDB URI; Search uses the same fields under
`search.connection`. Mount their externally managed Secret read-only at the
referenced paths. Search HTTPS can use an explicitly mounted `ca_file`; otherwise
it uses system trust. [examples/existing-secret.yaml](examples/existing-secret.yaml)
shows external config, auth and CA mounts.

For non-secret configuration, set `config.existingSecret: ""` and provide
`config.data.node` and `config.data.routes`. Helm writes the same two YAML files
into a ConfigMap. Inline backend credentials are rejected in this mode because
Helm retains values in release history. Validate supplied config using the
compatible binary's `weir check --config node.yaml --routes routes.yaml` before
installation; checking credential-file fields reads those explicitly supplied
files and requires their authorization. The Chart never reads an existing Secret.

```sh
helm upgrade --install weir ./charts/weir \
  --namespace application --kube-context your-context \
  -f your-reviewed-values.yaml --atomic --wait --timeout 5m
```

Configure precise network ingress and egress destinations. The default permits
same-namespace Pods labeled `weir-client: "true"` to call port 7447, and DNS egress
to kube-system/kube-dns. Backend and peer egress require explicit rules. The CNI
must enforce NetworkPolicy; verify allowed and denied clients before promotion.
Additive allow-all policies can defeat those restrictions. Update policy ports
when changing listener ports.

## Store discovery

Every release exposes an ordinary `<release>-weir` Service for SDK initialization
and peer bootstrap, plus `<release>-weir-headless` for direct business discovery.
Advertise the latter's DNS in `discovery.advertise`: clients resolve it to the
ready group Pod IPs and send business RPCs directly to the Store owner.

For several groups, [examples/shared-seed.yaml](examples/shared-seed.yaml) defines
an ordinary `weir-seed` Service that selects all Weir groups in the `application`
namespace. Its application port is the SDK initialization endpoint; its peer port
is the common bootstrap seed. It intentionally uses the common Chart name label
without a release-instance label. Adjust it if changing `nameOverride` or ports.
[examples/mongo.yaml](examples/mongo.yaml) assumes release `weir`, and
[examples/search.yaml](examples/search.yaml) assumes release `search`; both point
to this shared seed but advertise their own group headless DNS. Allow selected
Weir peer ingress/egress across groups, as those examples show. A Store name must
belong to exactly one group; conflicting ownership is a discovery error.

For a single group, point `discovery.seeds` to its ordinary release Service instead
of provisioning the shared seed. For a standalone node, omit peer synchronization
while retaining a reachable business advertisement for its local Stores. A
no-Store discovery node can use `routes.yaml` containing `stores: []`.

## Operations

One replica has replacement downtime. For three replicas layer
`examples/three-replicas.yaml` onto your reviewed configuration. Its topology label
selector assumes release `weir`; adjust it for other release names. It requires
at least two eligible worker hostnames. The optional PDB limits voluntary
disruptions, and does not establish physical fault tolerance by itself.

Each default Pod requests and limits 2 CPU / 3 GiB. Observe backend concurrency
and connection demand on every starting, running and terminating Pod. A revision can have three old
and three new processes while termination completes; reserve up to
12 CPU / 18 GiB and allow for twice the steady backend connection demand.
Zero-surge rolling updates do not bound terminating processes or remote work after connection loss.

SIGTERM withdraws readiness and drains admitted work within the application's
5-second cap. The Chart grants 15 seconds for termination and kubelet overhead.
Probes execute `/weir probe ready|live --address 127.0.0.1:7449`. Readiness describes
lifecycle; qualify real Read/Mutate/Scan/Native operations independently. A sent
mutation without a terminal reply has an unknown outcome and requires explicit
reconciliation before retrying.

Configuration is static. Version external Secrets and change
`config.existingSecret`, or update `config.revision` after an approved Secret
change. ConfigMap changes automatically roll Pods via a checksum covering both
files. CA/auth file changes need the same controlled restart. Verify old processes
have exited before beginning another revision. Roll back only to a reviewed,
compatible image/config pair. Weir needs no application PVC.

```sh
kubectl --context your-context -n application rollout status deployment/weir-weir
kubectl --context your-context -n application get endpointslice \
  -l kubernetes.io/service-name=weir-weir-headless
```

For Prometheus, enable `metrics.enabled`, authorize scraper sources and port 7449
in `metrics.ingress`, and configure `diagnostics.address: 0.0.0.0:7449` plus
`diagnostics.allow_intranet: true`. For IPv6 scraper access use `[::]:7449`
instead. ConfigMap mode checks that listener contract;
external Secret mode requires the same pre-install validation. The separate
`<release>-weir-metrics` Service exposes diagnostics `/metrics` and fixed health
handlers. `metrics.annotations` can select an existing scraper.

## Develop and release

Build the recorded Weir source commit as an explicit validator; then run:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
make package PYTHON=.venv/bin/python WEIR_BIN=/absolute/path/to/weir
```

Strict lint and template tests use the template-only image fixture. The contract
check invokes the selected binary against actual Helm-rendered MongoDB/Search
configuration, three-replica overlays, generated credential-file fixtures and an
invalid legacy config. It then starts a no-Store node, executes the rendered
startup/readiness/liveness probes and verifies bounded SIGTERM shutdown. It makes
no backend connections and reads no existing Secrets. CI builds the fixed source
revision before running the same checks; it packages the Chart as an artifact.

A reviewed `weir-0.3.0` tag can publish the immutable OCI Chart and GitHub archive
with SHA256SUMS after release preflight. Updating the server contract requires
updating the Chart source annotation and CI source pin together, rerunning these
checks and qualifying the intended immutable image. Real install/upgrade/rollback,
backend, discovery and sustained-load evidence belong in a new acceptance report.
The [persistent acceptance fixtures](tests/acceptance/README.md) retain historical
v0.1.x context and require adaptation for a current SDK/server pair.
