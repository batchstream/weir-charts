# Monitoring

How to collect metrics and enable alerts and the dashboard.

## Enable metrics

Enable `metrics.enabled` and authorize scraper sources in `metrics.ingress`. The diagnostics listener is always bound for the probes; `metrics.enabled` adds the NetworkPolicy ingress for scrapers and a separate metrics Service. Reference the diagnostics port by its `metrics` name in ingress rules so a changed `diagnosticsPort` keeps matching. With default NetworkPolicy, metrics enablement requires explicit scraper rules. Annotations can support an existing scraper; `metrics.serviceMonitor.enabled` creates a ServiceMonitor when Prometheus Operator is already installed. Its selector matches exactly this release's metrics Service, and Prometheus discovers each Pod endpoint separately. `metrics.serviceMonitor.labels` must match your Prometheus instance's monitor selector; Chart identity/component labels cannot be overridden.

## Labels and scrapers

ServiceMonitor target relabeling supplies `namespace` and `weir_group="namespace/release"`. Native Weir metrics also carry `store` where applicable. With a custom scraper, supply the same stable labels and avoid duplicate scrape jobs. Scrape all group replicas; querying only the ordinary metrics ClusterIP would sample one Pod.

## Alerts and dashboard

`metrics.prometheusRule.enabled` adds a PrometheusRule with this group's alerts, and `metrics.grafanaDashboard.enabled` a ConfigMap holding a Grafana dashboard for the group. Both require `metrics.enabled`; their `labels` must match your Prometheus rule selector and Grafana's dashboard sidecar. Every expression selects the group by `namespace` and `weir_group`, so a custom scraper must supply those labels. [examples/prometheus-alerts.yaml](../examples/prometheus-alerts.yaml) enables the ServiceMonitor, alerts and dashboard together. `WeirGroupUnavailable` is critical: Prometheus sees no Ready replica of the group, which also covers failed scrapes. The other alerts are warnings: a replica Prometheus cannot scrape, sustained memory pressure, admission or Store rejections other than draining, unknown mutation outcomes, more than 5% of records ending with a failure, more than 5% of Native calls ending without a complete response, a p99 queue wait above one second and stalled RPCs. Each alert links to its runbook in Weir's [operations guide](https://github.com/batchstream/weir/blob/main/docs/operations.md#alerts).
