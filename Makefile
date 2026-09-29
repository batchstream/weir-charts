PYTHON ?= python3
.PHONY: test lint package
lint:
	helm lint charts/weir --strict
	helm lint charts/weir --strict -f examples/mongo.yaml
test: lint
	$(PYTHON) -m unittest discover -s tests -v
package: test
	mkdir -p dist
	helm package charts/weir --destination dist
