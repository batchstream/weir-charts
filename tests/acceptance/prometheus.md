# Existing Prometheus evidence

This is a single acceptance workflow, not a monitoring service. The standard-library
`scripts/prometheus-evidence.py` reads an existing credential-free HTTP Prometheus
endpoint (or an explicitly opened local port-forward); it does not install or
configure anything and never runs in the business cancellation path. Its output
contains internal identities; keep raw output local, not in the public repository.

## Freeze and startup

1. Merge `prometheus-values.yaml` over the owned deployment values. Retain the
   existing 7449 intranet diagnostic listener and restricted scraper ingress.
   Verify all three discovered targets are UP, unique, use the intended Pod UID
   and `PodIP:7449/metrics`. Applying Pod template annotations rolls Pods; record
   the replacement identities. Do not change shared Prometheus or metrics-server.
2. Fill `run.example.json` with source/binary versions and `resourceEvidenceSource:
   prometheus`. Render and create the owned waiting load/observer Jobs. Upload
   verified binaries and start the lifecycle observer. The load still cannot run:
   it also waits for a Prometheus preflight marker. This permits obtaining the
   actual load UID and container ID before any business traffic.
3. Fill `prometheus-freeze.example.json` from the new Pod/Job snapshot and actual
   raw series/discovered target labels. Freeze all six containers (3 Weir,
   MongoDB, Elasticsearch, waiting load): UID, name, node, IP, container ID, image
   ID, exact cgroup ID and PodSpec CPU/memory limits. Preserve that Pod snapshot.
   Freeze source Prometheus namespace/Pod/UID and the **exact** script SHA256.
   Source UID is checked against historical KSM info; no shared configuration is
   read. A different source identity requires a new explicit freeze.
4. `run_config_sha256` uses the renderer's canonical JSON bytes:

   ```python
   hashlib.sha256(json.dumps(run_config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
   ```

5. Allow at least 60 seconds of real Prometheus samples, then run a recent preflight:

   ```sh
   python3 scripts/prometheus-evidence.py collect --phase preflight \
     --config /owned/run-prom-freeze.json --start START_UTC --end RECENT_END_UTC \
     --output /owned/run-preflight
   ```

   End must be within 30 seconds of invocation. The tool checks raw coverage,
   cgroup/UID/container identity, fresh source statistics, required application
   families, UP/readiness and capacity bounds. Server v0.1.0 eagerly initializes
   RPC method/status and store outcome/kind counters and histograms
   (`internal/server/metrics.go`, `internal/store/metrics.go`): zeros are valid;
   missing required families are not a legitimate lazy-initialization case for
   this frozen version. Preflight never requires positive business deltas.
6. Publish verified `freeze.json` and `ready` from that successful output to the
   **new** PVC paths `/results/RUN-prom-freeze.json` and `/results/RUN-prom-ready`,
   using exclusive temporary files, fsync and non-overwriting links. Never
   overwrite an existing run's files. The five-line ready marker contains run ID,
   canonical run-config SHA, actual load UID, exact freeze-file SHA, publication
   Unix time. The shell validates all bindings and a 120-second marker lifetime
   before the runner starts. That lifetime is a new preflight guard, not the
   historical resource-age threshold. Observer and Prometheus do not wait on one
   another's terminal success.

## Raw collection and audit

The load runner still owns real monotonic duration, cycle/interval p99, coverage,
confirmed operations, errors and UNKNOWN. The lifecycle observer still samples
Kubernetes every minute with its original 90-second deadline and a 150-second
runner watchdog. It never reads metrics.k8s.io or application metrics. Its new
records retain container ID, node/IP and resource declarations for later binding.

After load and observer terminate successfully, export their immutable full files
with hard links dereferenced, compare every PVC/local SHA and parse every JSONL.
Use the runner's actual `started_utc` and last `utc`, not Job creation or planned
end. Run promptly while the existing Prometheus still retains the complete period:

```sh
python3 scripts/prometheus-evidence.py collect --phase postrun \
  --config /owned/run-prom-freeze.json --start ACTUAL_START --end ACTUAL_END \
  --load-report /owned/run.jsonl --observations /owned/run-observations.jsonl \
  --observer-status /owned/run-observations.jsonl.status.json --output /owned/run-prom-export
```

The same arguments with `audit` instead of `collect` replay the already saved raw
responses offline (use a copy without `audit.json`, since verdicts never overwrite).
Exit 0 means this evidence audit passed; exit 1 means invalid/incomplete evidence;
exit 2 means HTTP retrieval is pending. A pending request receipt retains the
HTTP status and completed raw files. A later explicit collection uses a **new**
output directory; retain the failed attempt. No polling, hidden retries or load
cancellation occur. Missing history is a failed gate, not a pending HTTP read.

Queries use `/api/v1/query` with literal range vectors, bounded 15-minute chunks
plus 120-second overlap. They do not use `query_range`, `rate`, `increase`,
`timestamp()` or resampling. Every raw response retains request query/time,
length, SHA and source identity. HTTP success alone is insufficient: warnings,
non-matrix results, nonfinite/negative values and non-increasing original sample
sequences are rejected. Exact overlap duplicates are deduplicated only when the
complete label identity, timestamp and value agree; conflicts and duplicate
semantic scrape sources are rejected.

Queries are explicit metric-name groups plus selectors:

- cAdvisor: namespace + Pod + container; CPU seconds, memory working set, RSS,
  usage, OOM events, container start time and last_seen. Exact cgroup UID,
  container ID/name, image and node must match the freeze.
- Application: job=pod + Pod name + fixed instance `IP:7449`; UP, node ready,
  RPC completions, executions/records, queue entries/limits, active execution/
  window limit, queue/execution histogram counts/sums, process RSS and Go heap.
  This source has no namespace/UID labels. KSM preflight plus cgroup identity and
  the observer's continuous fixed UID/container/IP/node records bind its lifetime;
  identical Pod names alone never establish identity.
- KSM Pod/container info: namespace + Pod; validate every returned UID/node/IP/
  image/container association. Preflight requires both info families. Later KSM
  gaps remain an explicitly reported cross-check limitation, not an added fatal
  continuity gate; exact cgroup and lifecycle evidence are still mandatory.

All mandatory resource/application series must cover start/end and internal
intervals with actual gaps <=90s, including the boundary from the last sample to
run end. No missing points are filled. Resource timestamp T is compared to the
same container's actual `container_last_seen` exporter observation S using the
latest original statistics at/before S; require S-T <=120s and at most 30s future
skew. last_seen's value must also agree with its original sample timestamp within
30s. This proves exporter/statistics age, **not TSDB ingestion latency**; query
evaluation time is never treated as the historical observation clock. Application
samples must align with an actual successful `up` scrape within 30s; any observed
up=0 or readiness=0 fails.

Counters cannot reset. CPU cores derive from adjacent original counter differences
and actual time differences. Reports distinguish cAdvisor working set/RSS/usage,
Weir process RSS and Go heap; preserve first/last/min/max and half-window means,
CPU peak and CPU/memory headroom. Queue/active execution cannot exceed their
same-scrape frozen capacity metrics. Every Pod must show positive in-run
Read/Mutate/Bulk and record executions/records for both backends, with no non-OK
RPC increments. Positive pre-existing counters alone do not satisfy this gate.

Final qualification is **load passed AND lifecycle observer passed AND full
Prometheus audit passed AND independent trend/remaining-margin review**. Keep the
actual 24h, six workers × five cycles/s, >=98% coverage, overall/every interval
cycle p99 <=500ms and zero business failures/UNKNOWN. A short pair uses its own
explicit duration and is only a short-pair result. Source/build hashes, all raw
receipts and files must be retained. Chart 0.1.0 is unchanged; record its source
separately from these new standalone tools. Three failed historical runs stay
failed. Shared CNI enforcement remains an unresolved production gate.
