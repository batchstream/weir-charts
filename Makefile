PYTHON ?= python3
GO ?= go
.PHONY: test lint package
lint:
	helm lint charts/weir --strict
	helm lint charts/weir --strict -f examples/mongo.yaml
test: lint
	$(PYTHON) -m unittest discover -s tests -v
	$(GO) test -race scripts/observe-soak.go scripts/observe-soak_test.go
package: test
	mkdir -p dist
	helm package charts/weir --destination dist
