# Contributing to the Weir Helm chart

Focused bug fixes, documentation improvements, tests and feature proposals are
welcome. Open an [issue](https://github.com/batchstream/weir-charts/issues/new/choose) first for a change
that depends on new [server](https://github.com/batchstream/weir) behavior.

## Report a problem

Use the issue forms and include the chart version, the Weir image digest, the
Kubernetes and Helm versions, the relevant values, the expected result and a minimal
reproduction. Remove credentials and private data from logs and values.

For vulnerabilities, follow [SECURITY.md](SECURITY.md).

## Work locally

Install Helm, Python 3 and the Go version the chart's workflows use, then follow
[Develop and release](README.md#develop-and-release):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
GOBIN="$PWD/.bin" go install github.com/batchstream/weir/cmd/weir@SOURCE_REVISION
make test PYTHON=.venv/bin/python WEIR_BIN="$PWD/.bin/weir"
```

`SOURCE_REVISION` is the `weir.batchstream.io/source-revision` annotation in
`Chart.yaml`. With `promtool` on `PATH` or in `PROMTOOL`, `make test` also checks the
rendered alerts and the autoscaling query; without it, that test is skipped.

## Prepare a pull request

- Keep the change focused and explain the observed problem and resulting behavior.
- Add a regression test when fixing behavior.
- Update `values.schema.json`, the [examples](examples) and the README when values change.
- When pinning a new server, pin it in [weir-tests](https://github.com/batchstream/weir-tests) first, then change the source annotation, `appVersion`, image digest, workflow pins, the README's references and the values in `tests/test_chart.py` together.
