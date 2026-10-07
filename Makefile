PYTHON ?= python3
GO ?= go
WEIR_BIN ?= weir
.PHONY: test lint contract package
lint:
	helm lint charts/weir --strict -f tests/values.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/mongo.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/search.yaml
contract:
	$(PYTHON) scripts/check-contract.py --binary $(WEIR_BIN)
test: lint contract
	$(PYTHON) -m unittest discover -s tests -v
	$(GO) test -race scripts/observe-soak.go scripts/observe-soak_test.go
package: test
	mkdir -p dist
	helm package charts/weir --destination dist
