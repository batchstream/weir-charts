PYTHON ?= python3
WEIR_BIN ?= weir
.PHONY: test lint contract package
lint:
	helm lint charts/weir --strict -f tests/values.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/mongo.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/search.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/mongo.yaml -f examples/prometheus-autoscaling.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/mongo.yaml -f examples/prometheus-alerts.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f examples/mongo.yaml -f examples/credential-files.yaml
	helm lint charts/weir --strict -f tests/values.yaml -f tests/three-replica-values.yaml
contract:
	$(PYTHON) scripts/check-contract.py --binary $(WEIR_BIN)
test: lint contract
	$(PYTHON) -m unittest discover -s tests -v
package: test
	mkdir -p dist
	helm package charts/weir --destination dist
