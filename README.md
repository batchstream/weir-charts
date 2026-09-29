# Weir Helm charts

Deploy [Weir](https://github.com/batchstream/weir), a synchronous, bounded MongoDB
and Search data plane, on Kubernetes. Helm 3.17+ and Kubernetes 1.30+ are required
by the templates; real cluster qualification is recorded in `docs/acceptance.md`.

Weir is stateless. Databases, indexes, collections, replication, backups, and storage
are provisioned separately. The chart does not install a database, an operator,
cluster permissions, a public ingress, or a load balancer. Application and peer
traffic use plaintext HTTP/2 and must stay inside a trusted network boundary.

## Install

Provision a Secret named `weir-config-v1` containing `node.json` through your approved
secret management process. The file must set `application: "0.0.0.0:7447"`,
`diagnostics: "127.0.0.1:7449"`, a process `memory_mib` budget below the container
memory limit (768 for the default 1 GiB), and valid backend services/routes.
See upstream configuration documentation. Never place credentials in Helm values:
Helm retains values in release history. Non-secret test configuration can use
`config.data` with `config.existingSecret: ""`.

```sh
helm upgrade --install weir oci://ghcr.io/batchstream/charts/weir   --version 0.1.0 --namespace your-namespace --kube-context your-context   -f your-reviewed-values.yaml --atomic --wait --timeout 3m
```

For a checked-out chart, replace the archive URL with `./charts/weir`. Configure
`config.existingSecret`, precise `networkPolicy.ingress` and `networkPolicy.egress`
in your values. The default permits only same-namespace Pods labeled
`weir-client: "true"` to call port 7447 and DNS egress to kube-system/kube-dns.
It deliberately permits no backend egress until explicit destinations are supplied.
The cluster CNI **must enforce NetworkPolicy**; creating policies on a CNI with
policy support disabled provides no isolation. Verify an allowed and denied client
before admitting application traffic. Existing allow-all policies are additive.

The default image is pinned by digest; `image.digest` takes precedence over tag.
The exact release image and source commit are recorded with release evidence.
Pod startup/readiness/liveness execute `/weir -probe` against loopback; no shell,
HTTP sidecar, or diagnostic Service is required. Readiness describes lifecycle,
not database health. Verify real Read/Mutate/Bulk/Scan/Native operations with the
[Go SDK](https://github.com/batchstream/weir-go).

## Operations

One replica is the default and has replacement downtime. For three replicas use
`examples/three-replicas.yaml` with release name `weir`, or adjust its label selector
for another release name. It requires at least two eligible worker hostnames.
Topology spread across two Pods on one host does not prove physical fault tolerance.
The optional PDB only limits voluntary disruptions; it cannot prevent node failure.

Each default Pod requests and limits 2 CPU / 1 GiB. Each Local in the example has
concurrency 2 and at most C+1 locally owned connection slots. Count every Local
on every starting, running, and terminating Pod. A three-replica revision can have
three old plus three new processes while termination completes: reserve up to
12 CPU / 6 GiB and twice the steady backend connection budget if simultaneous
replacement is possible. A zero-surge rolling strategy is not a hard bound on
terminating processes or remote work continuing after lost connections.

SIGTERM withdraws readiness and drains admitted work within the application's
5-second cap. The chart grants 15 seconds for termination and kubelet overhead.
It adds no arbitrary preStop sleep. Existing streams may break during replacement;
a sent mutation without its terminal reply is UNKNOWN and must never be replayed
automatically. Explicitly reconcile uncertain effects at the application boundary.

Configuration is static. Version externally provisioned Secrets and change
`config.existingSecret`, or change `config.revision` after an approved Secret update.
A `config.data` change automatically rolls Pods via a checksum. Finish each rollout
and verify old processes have exited before another revision. Roll back to a known
compatible immutable image/config pair with `helm rollback weir REVISION`; perform
schema and backend compatibility review before rollback. Mounted CA changes also
need a controlled restart. `extraVolumes` and `extraVolumeMounts` support read-only
standard CA/config volumes; no Weir PVC is needed.

```sh
kubectl --context your-context -n your-namespace rollout status deployment/weir-weir
kubectl --context your-context -n your-namespace get endpointslice   -l kubernetes.io/service-name=weir-weir
```

`peer.enabled` adds the peer port to the Service; the supplied Weir configuration
must bind the same port and your ingress rules must permit only intended peers.
For custom application/diagnostic ports update both values and the supplied config.
Network policy ports must match the actual container listeners.

To expose bounded Prometheus metrics, set `metrics.enabled: true`, configure
`metrics.ingress` with the authorized scraper Pod/namespace selector and TCP port
7449, and set `diagnostics: "0.0.0.0:7449"` plus
`diagnostics_allow_intranet: true` in Weir's configuration. ConfigMap mode validates
these fields; an external Secret must supply them through its own review process.
The separate `<release>-weir-metrics` ClusterIP exposes `/metrics` on the diagnostics
port. The port also serves fixed health handlers; it exposes no pprof/debug APIs.
`metrics.annotations` can configure your existing scraper. NetworkPolicy remains
required; enabling the Service does not grant access to arbitrary sources.

## Develop and release

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test PYTHON=.venv/bin/python
make package PYTHON=.venv/bin/python
```

Changes reach main only through a reviewed PR. CI validates schema/negative cases,
security and rollout contracts, and creates a chart archive. After validation, a
`weir-0.1.0` tag publishes the OCI chart and a public GitHub release archive with
SHA256SUMS; its version
must match Chart.yaml. Release archives are immutable inputs for consumers.
Real install/upgrade/rollback and backend evidence belong in the acceptance report;
short tests do not establish a capacity SLO or a 24-hour soak result.

The optional fixed-layout soak observer requires explicit context, namespace, Job,
and a new output file. It is read-only and does not read Secrets or restart failed
workloads:

```sh
python3 scripts/observe-soak.py --context your-context --namespace acceptance \
  --job weir-soak-24h --output /absolute/new-run/observations.jsonl
```
