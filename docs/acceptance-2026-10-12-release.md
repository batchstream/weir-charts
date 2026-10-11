**Published Weir v0.2.0 image qualification, 2026-10-12 (Asia/Shanghai)**

Chart 0.6.0 pins the published Weir v0.2.0 multi-architecture image to its
verified source contract. This is a fresh final-image run using the local,
task-owned Kind cluster and separate MongoDB replica set. The
[candidate qualification](acceptance-2026-10-12-local-kind.md) retains earlier
failures and candidate-image checks; none of its operation totals are used here.
The [machine-readable evidence](acceptance-2026-10-12-release-evidence.json)
records exact Pod/container identities, operation results and clean exits.

| Component | Recorded identity |
| --- | --- |
| Chart | Source-directory Chart `0.6.0`, base `43313a89fca9d7af7940e8291f12e37ff209a72b` plus this PR |
| Weir release/source | [v0.2.0](https://github.com/batchstream/weir/releases/tag/v0.2.0), `8b8973894c4670f0e4173d223d08544727bc2ae9` |
| Published OCI index | `sha256:ea12776d93739a9decb5464df51b13219fa34e4ccb10c8a8b2fd8d3f95fe5fbf` |
| Linux arm64 child | `sha256:86ec9226eb09be2649c530d0bfa05d744c6a442ba78278c238263d58faf742a1` |
| Actual running `/weir` SHA256 | `e60d44e7f8f1237ca959b5d10d1b0fe54ec6eb9d6483bbee0a0104cad8fde4ca` on all three Pods |
| Public SDK | `v0.10.1`, source `d196d47e6174c207418abe9a32b15edb4cb05c0e`, protocol `v0.8.0` |
| Client SHA256 | `b36c9605061c3dab61c75ee893fee527d838f7a0c5633091cf4c83c7b7fe4cd4` |
| Runner source | `batchstream/weir-tests` `0a4636f99c76e7e3aecd01695941a6ec5ea808a0`; copied Go files unchanged, only public SDK require updated |
| Cluster/backend | Kind `0.33.0`, Kubernetes `1.37.0`, enforced kindnet policies; native locked MongoDB `8.0.32`, dedicated port `27029`, replica set `weir_chart` |

Anonymous registry checks, with Docker credentials explicitly disabled, confirmed
that both the version tag and full-source tag refer to the same published index.
The complete OCI archive was exported and loaded into the owned Kind nodes.
The release's executable version is `local-8b8973894c4670f0e4173d223d08544727bc2ae9`, with full linked
revision `8b8973894c4670f0e4173d223d08544727bc2ae9`, `clean-commit`, `dirty=false`; `v0.2.0` is its release
and registry alias. The Chart preserves `appVersion = source[:12]` and records
the full source revision in a managed Pod annotation.

Kind's archive import initially reported an OCI layout wrapper as kubelet
`imageID`: `docker.io/library/import-2026-10-11@sha256:9ad572d450088090d294600906d6a897e52cff8b80ad76c235f15159aa04ca40`.
This wrapper points to the exact published index, which points to the arm64
child and config `sha256:f996a44d185b316d1e40bf9a1405bac615d19b6e88bb32e981037f4dcc2f2a00`.
On all three nodes, every descriptor and all 14 layer hashes were checked;
decompressed layer diff IDs matched the published config. Actual running
overlay snapshot parents matched rootfs chain
`sha256:1a9291c17ca2da8ece8e35742a7d2842fffb99ae094aed6edcaf28e4fa29f781`.
The running binary hashes and linked source also matched the published delivery
receipt. Original import statuses and descriptor bytes were retained. Existing
aliases were never removed or rewritten to hide the import identity; additional
anonymous pulls of the exact arm64 child succeeded. Final Pod imageIDs are
recorded exactly as returned rather than assumed to equal the child digest.

One public SDK process, PID `29`, ran continuously from
`2026-10-11T21:36:46.421211+00:00` through `2026-10-11T21:41:46.421211+00:00` for 300 seconds,
with four concurrent workers and 64 owned documents. Its 2,785
warmup operations were followed by **279,287 successful timed
operations**: 139,010 reads and 140,277 Lua mutations,
with **zero errors, UNKNOWN results, timeouts or unattempted operations**.
The process was never reopened, and mutations were never replayed.

The application seed was the full Service FQDN
`weir-weir.weir-chart-test.svc.cluster.local:7447`. An actual Helm upgrade
changed the config revision and replaced all three Pods; rollback restored the
published baseline revision and replaced all three again. All upgrade Pods were
ready and all three old Pods removed by window second
56.640; rollback completed by
second 108.523. The watch captured all six old
containers exiting `0 / Completed` within the traffic window. Traffic continued
through the remaining window after both rollouts.

After the SDK exited successfully, an independent native MongoDB audit checked
all 64 persisted documents against the SDK's exact expected revision vector.
IDs, fixture IDs, the four-field schema and 128-byte padding were unchanged;
every revision retained BSON `int32`. All checks passed.

| Final Pod | Pod IP | Application execute OK | Peer sync OK | Directory |
| --- | --- | ---: | ---: | ---: |
| weir-weir-9fbfb9484-5t2kr | 10.244.2.24 | 68126 | 329 | 3 |
| weir-weir-9fbfb9484-7qts7 | 10.244.1.22 | 74355 | 341 | 3 |
| weir-weir-9fbfb9484-mxdmg | 10.244.1.23 | 61899 | 283 | 3 |

Each final Pod was Ready with zero restarts. Application/metrics access was
actually allowed for the labelled client and denied for the unlabelled client.
Application clients were denied peer access; the peer-labelled client could
reach the peer port and was denied the application port. The application
Service and full headless DNS worked, with all three ready Pod IPs represented.
Both direct ready/live probes passed for each Pod. Runtime inspection during Lua
traffic confirmed one Weir process, no child processes, UID/GID 65532, active
seccomp, no effective capabilities or privilege escalation and a read-only root.

`make package` passed four strict lint overlays, 32 Python tests, the official
published native binary's rendered configuration/serve/probe/shutdown contract,
and the observer's Go race tests. CI/release source pins equal the Chart's full
source annotation; tests lock the appVersion and managed Pod annotation contract.

Raw logs and inputs remain at
`/Users/liran/Projects/liran/go/weir-charts/.testdata/qualification/2026-10-12/final-published-image/index.md`. The compact raw archive is
`final-published-image-raw.tar.gz`, SHA256 `d376b622ca72543836485153fc7361a6f140813a34a9024b4ace6243ea7d0a9a`,
with 89 individually hashed files. The complete anonymous OCI archive is
retained separately with its own checksum. Historical failed/partial windows
remain in the candidate evidence directory.

This qualifies the published **server image deployed from the reviewed Chart
source directory**. Public OCI Chart installation is a separate check after
Chart 0.6.0 publication. The owned cluster/backend remain available for it.
This bounded local check does not complete the historical 24-hour gate or
production capacity qualification. Pod workload execution was Linux arm64;
the server release pipeline separately checked native amd64 startup. MongoDB
ran natively because the local Docker Linux kernel rejected the locked MongoDB
8.0 image. Search is covered by separate server qualification, and plaintext
transport requires a trusted isolated network. No production resources were
contacted.
