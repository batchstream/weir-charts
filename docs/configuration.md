# Configuration

How to declare Store groups, connect clients and supply credentials.

## Store groups

One Helm release is one Store group. Every replica hosts the same Store definitions; a group can host several Stores, and each group is released and scaled independently. Replicas share nothing with each other. Each Pod runs one Weir process with Lua inside that process. Backends, collections, indices, credentials and backups are provisioned separately.

### Stores

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

Each entry uses Weir's own Store schema: `name`, exactly one of `mongodb.uri` or `search.url`, optional file-based `authentication`, optional `tls.ca_file` and optional `batching` (`max_operations`, `max_bytes`, `max_wait`, `max_in_flight`, `min_in_flight`). Database, collection and index targets come from request URIs; Chart configuration declares the backend connection for each Store. Multiple replicas within a group use identical Stores and settings. A request for a Store the group does not host fails with NotFound.

### Install several groups

Install [examples/mongo.yaml](../examples/mongo.yaml) and [examples/search.yaml](../examples/search.yaml) as separate groups in the same namespace:

```sh
helm upgrade --install mongo ./charts/weir --namespace application \
  --kube-context your-context -f examples/mongo.yaml --atomic --wait --timeout 5m
helm upgrade --install search ./charts/weir --namespace application \
  --kube-context your-context -f examples/search.yaml --atomic --wait --timeout 5m
```

## Client connections

Clients use [weir-go](https://github.com/batchstream/weir-go) v0.23.1, which speaks this server's protocol, and connect with `weir.New` to the group's headless Service by its full name with a trailing dot, `<release>-weir.<namespace>.svc.cluster.local.:<service.port>`, with the cluster's own domain if it is not `cluster.local`; without the dot every lookup first tries the Pod's DNS search domains. The name resolves to every Ready Pod, and the SDK spreads business RPCs across them. The SDK looks the name up every 5 seconds, so Pods added by a scale-out or rollout take their share of calls within seconds of becoming Ready, and a terminating Pod stops receiving calls once it leaves DNS, before SIGTERM. It also pings a replica it has heard nothing from for 10 seconds and leaves one that does not answer; this server accepts those pings, so upgrade the group before its clients. Generated `[::]` listeners accept IPv4 and IPv6 on the supported Go runtime. Group identity is `namespace/release`, so metric labels remain stable when `fullnameOverride` changes. The Service selects only its release. Allow clients to reach each group's application port.

## Generated configuration

The Chart generates `weir.yaml` with the listeners, `shutdown_timeout` and `stores`. The Chart renders one `weir.yaml` into its ConfigMap and mounts it read-only at `/etc/weir`. Its schema rejects unknown keys; the selected Weir binary validates backend semantics. Weir fixes transport, memory-pressure, Lua, queue and retry policy internally, so only listeners, the shutdown drain and Stores are configurable. Weir sets its Go memory limit to 90% of its container's cgroup v2 `memory.max` and refuses new work as live memory nears it, so keep a memory limit in `resources`. Without a limit, or on cgroup v1 nodes, Weir does not shed load.

## Credentials

Helm retains values in release history. Inline backend credentials are rejected, and so is any `@` in `mongodb.uri` or `search.url`, since it can only carry userinfo. MongoDB and Search use `authentication.username_file/password_file`; mount an externally managed Secret read-only using `extraVolumes/extraVolumeMounts` and reference it by absolute path, since relative paths resolve from `/etc/weir`; [examples/credential-files.yaml](../examples/credential-files.yaml) shows the layout. MongoDB requires SCRAM-SHA-256, authSource and TLS in the URI when authenticating. Both adapters can use `tls.ca_file`, or system trust when it is absent. Credential and CA files are read at startup; roll the Pods after rotating them, for example by changing a `podAnnotations` entry.

## Validate before deployment

Validate the generated `weir.yaml` with `weir check --config ...` from the pinned Weir source before deployment. Credential-file validation reads those supplied files and requires their authorization. ConfigMap updates automatically change the Pod checksum.
