# Development

How to test the chart and publish a version.

## Test and package

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
GOBIN="$PWD/.bin" go install github.com/batchstream/weir/cmd/weir@SOURCE_REVISION
make test PYTHON=.venv/bin/python WEIR_BIN="$PWD/.bin/weir"
make package PYTHON=.venv/bin/python WEIR_BIN="$PWD/.bin/weir"
```

`SOURCE_REVISION` is the `weir.batchstream.io/source-revision` annotation in `Chart.yaml`. `make test` lints the Chart, renders every example and validates the generated configuration with the selected Weir binary, including a scale check with many Stores and replicas and an isolated loopback-only serve, HTTP probe and shutdown run. With an official `promtool` on `PATH` or named by `PROMTOOL`, it also runs the rendered alerts and the autoscaling query against their PromQL fixtures; CI installs a checksum-verified promtool. It opens no backend connections, reads no existing Secrets and does not exercise CNI enforcement, Prometheus collection or KEDA scaling in a real cluster.

## Upgrade test

CI also runs `scripts/kind-upgrade.sh` on every change. In a kind cluster of its own, under load from `weir-soak` with `-no-errors` on both the current and the previous SDK, it upgrades a release from the Chart of the previous server through the Chart a change replaces to the new one, back and forward again at one replica, replaces the replica with one that starts 30 seconds late, scales to three replicas, takes the three back and forward again and scales back down. The change fails if a rollout does not complete, a caller saw any error or an outcome did not hold. While the previous SDK is v0.22.0 its client skips held streams, which that version fails in a rollout. Every rollout setting stays the Chart's default, and the test measures no throughput. The job fails if the weir-tests revision it builds the clients from pins other servers than these two Charts, so pin a new server there first. To run it locally, build `weir-soak` from [weir-tests](https://github.com/batchstream/weir-tests) for the cluster's platform and check out the commit to upgrade from; the script uses a kubeconfig of its own and ignores your current context and Helm settings. A second binary, built with the previous SDK and its protocol version as in the workflow, is optional; `PREVIOUS_PROFILES` sets its profiles, and `THROUGH_CHECKOUT` a Chart to pass through:

```sh
scripts/kind-upgrade.sh FROM_CHECKOUT . path/to/weir-soak [path/to/previous-weir-soak]
```

## Release

A reviewed `weir-VERSION` tag on `main` publishes the OCI Chart and a GitHub release with the archive and SHA256SUMS. The workflow refuses a version whose OCI Chart already exists and creates the release before pushing, so a rerun never overwrites a published Chart. If the push fails after the release exists, verify the release archive against its SHA256SUMS and `helm push` it by hand. Source annotation, image digest and CI source pins must identify the same server revision.
