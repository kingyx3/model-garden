.PHONY: dev validate contracts terraform kubernetes

dev:
	python3 -m pip install -r requirements-dev.txt

contracts:
	python3 scripts/validate-workspace.py examples/workspace

terraform:
	terraform -chdir=infra/aws fmt -check
	terraform -chdir=infra/gcp fmt -check
	terraform -chdir=infra/azure fmt -check

kubernetes:
	kubectl kustomize platform/k8s >/dev/null

validate: contracts terraform kubernetes
	bash -n scripts/*.sh
