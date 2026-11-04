# Weir Helm charts

[![CI](https://github.com/batchstream/weir-charts/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/batchstream/weir-charts/actions/workflows/ci.yaml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Helm chart that deploys [Weir](https://github.com/batchstream/weir), a synchronous
MongoDB and Search data plane, on Kubernetes.

## Overview

### Terms

| Term | Meaning |
| --- | --- |
| Store | A named backend connection, declared in `stores`. |
| Search | Elasticsearch or OpenSearch. |
| Store group | One Helm release. A group can host several Stores and is released and scaled independently. |
| Replica | One Pod running one Weir process. Every replica of a group hosts the same Stores. Replicas share nothing. |

### Responsibilities

| Provided by | Items |
| --- | --- |
| The chart | Deployment, headless Service, ConfigMap, NetworkPolicy. Optional: metrics Service, ServiceMonitor, PrometheusRule, Grafana dashboard, PodDisruptionBudget, KEDA ScaledObject. |
| You | Backends, collections, indices, credentials and backups. |

### Pinned versions

| Item | Version |
| --- | --- |
| Chart | `0.20.0-rc.1` |
| Server source | [commit `5107986b882d`](https://github.com/batchstream/weir/commit/5107986b882da60751403c7e05bbbebdb12b3cdb) |
| Server image | `ghcr.io/batchstream/weir@sha256:b5d949cefb0ba01e18cff2b5210cef954bc387c20cffa87784466e793e0e088e` |
| Go SDK | [weir-go](https://github.com/batchstream/weir-go) v0.23.0 |

`Chart.yaml` records the server source and `values.yaml` pins the image. Both the
validation and release workflows install that exact source with `go install`.

## Requirements

| Item | Requirement |
| --- | --- |
| Helm | 3.17 or later. CI tests 3.17.0. |
| Kubernetes | 1.30 or later |
| Cluster capacity | Each Pod requests and limits 2 CPU and 3 GiB by default. A rollout runs one extra Pod. |
| MongoDB | A nonsharded replica set with the client `bulkWrite` command (MongoDB 8.0 or later). |
| Search | Elasticsearch or OpenSearch. |
| Collections and indices | Must exist before typed reads and mutations. |
| Network | The application port uses plaintext HTTP/2. Use an isolated network and a CNI that enforces NetworkPolicy. |
| Optional | Prometheus Operator for monitoring. KEDA for autoscaling. |
| Development | Go 1.27.3 and Python 3, to run the chart tests. |

## Quick start

This installs one Store group with one MongoDB Store in the namespace
`application`.

**Step 1.** Get the chart.

```sh
git clone https://github.com/batchstream/weir-charts.git
cd weir-charts
```

**Step 2.** Check `examples/mongo.yaml`. It expects MongoDB in the namespace
`application`, with its primary at `mongo:27017` on Pods labeled `app: mongo`, and
no authentication. Edit the file if yours differ. For authentication, see
[Credentials](docs/configuration.md#credentials).

```yaml
stores:
- name: mongo
  mongodb:
    uri: mongodb://mongo:27017/?directConnection=true
networkPolicy:
  egress:
  - to:
    - podSelector:
        matchLabels:
          app: mongo
    ports:
    - protocol: TCP
      port: 27017
```

This example uses MongoDB direct mode, which has no failover. For production, use
a replica-set URI. See
[MongoDB connection modes](https://github.com/batchstream/weir/blob/main/docs/configuration.md#mongodb).

**Step 3.** Install the release. Replace `your-context` with your cluster context.

```sh
helm upgrade --install mongo ./charts/weir --namespace application \
  --kube-context your-context -f examples/mongo.yaml --atomic --wait --timeout 5m
```

Helm reports `STATUS: deployed` and prints the SDK address. With `--atomic`, Helm
removes a failed install after the timeout. Omit `--atomic` to keep the Pods for
inspection.

**Step 4.** Allow your client Pods to connect. They must run in the same namespace
and carry this label on the Pod template.

```yaml
spec:
  template:
    metadata:
      labels:
        weir-client: "true"
```

**Step 5.** Connect with the SDK, using the address that Helm prints. Keep the
trailing dot.

```go
client, err := weir.New("mongo-weir.application.svc.cluster.local.:7447")
```

To try it, run the program from the
[Go SDK quick start](https://github.com/batchstream/weir-go#quick-start) in that Pod
with this address. The program uses the collection `weir_example.records`, which
must exist first.

## Documentation

| Guide | Contents |
| --- | --- |
| [Configuration](docs/configuration.md) | Store groups, client connections, generated configuration and credentials |
| [Monitoring](docs/monitoring.md) | Metrics, labels, alerts and the Grafana dashboard |
| [Autoscaling](docs/autoscaling.md) | KEDA and Prometheus setup |
| [Operations](docs/operations.md) | Network, rolling updates, capacity, shutdown and probes |
| [Development](docs/development.md) | Chart tests, the upgrade test and releases |

| Example | Shows |
| --- | --- |
| [mongo.yaml](examples/mongo.yaml) | A group with a MongoDB Store |
| [search.yaml](examples/search.yaml) | A group with a Search Store |
| [credential-files.yaml](examples/credential-files.yaml) | Backend credentials from a mounted Secret |
| [three-replicas.yaml](examples/three-replicas.yaml) | Three replicas spread across nodes. Update its selector for your release. |
| [prometheus-alerts.yaml](examples/prometheus-alerts.yaml) | ServiceMonitor, alerts and dashboard |
| [prometheus-autoscaling.yaml](examples/prometheus-autoscaling.yaml) | KEDA autoscaling on Weir's own CPU |

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
GOBIN="$PWD/.bin" go install github.com/batchstream/weir/cmd/weir@SOURCE_REVISION
make test PYTHON=.venv/bin/python WEIR_BIN="$PWD/.bin/weir"
```

`SOURCE_REVISION` is the `weir.batchstream.io/source-revision` annotation in
`Chart.yaml`. See [Development](docs/development.md) for packaging and the upgrade
test.

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security reports: [SECURITY.md](SECURITY.md)

## Related repositories

| Repository | Purpose |
| --- | --- |
| [weir](https://github.com/batchstream/weir) | Server |
| [weir-go](https://github.com/batchstream/weir-go) | Go client SDK |
| [weir-protocol](https://github.com/batchstream/weir-protocol) | Public gRPC schema and validation |
| **[weir-charts](https://github.com/batchstream/weir-charts)** | Helm chart (this repository) |
| [weir-tests](https://github.com/batchstream/weir-tests) | System tests and benchmarks |

## License

MIT. See [LICENSE](LICENSE).
