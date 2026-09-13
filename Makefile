.PHONY: dev validate contracts compiler terraform kubernetes

dev:
	python3 -m pip install -r requirements-dev.txt

contracts:
	python3 scripts/validate-workspace.py examples/workspace

compiler:
	python3 -m unittest discover -s tests -v
	python3 scripts/compile-workspace.py examples/workspace --agent account-researcher --output /tmp/model-garden-account-researcher.json

terraform:
	terraform -chdir=infra/aws fmt -check
	terraform -chdir=infra/gcp fmt -check
	terraform -chdir=infra/azure fmt -check

kubernetes:
	kubectl kustomize platform/k8s >/dev/null

validate: contracts compiler terraform kubernetes
	bash -n scripts/*.sh
