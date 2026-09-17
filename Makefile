.PHONY: dev validate contracts tests terraform hermes enterprise-evidence

dev:
	python3 -m pip install -r requirements-dev.txt

contracts:
	python3 scripts/validate-workspace.py examples/workspace

tests:
	python3 -m unittest discover -s tests -v
	python3 scripts/eval-receptionist.py examples/workspace
	python3 scripts/compile-workspace.py examples/workspace --agent account-researcher --output /tmp/model-garden-account-researcher.json
	python3 scripts/compile-workspace.py examples/workspace --agent receptionist --output /tmp/model-garden-receptionist.json

terraform:
	terraform -chdir=infra/bootstrap/gcp fmt -check
	terraform -chdir=infra/bootstrap/gcp init -backend=false -input=false >/dev/null
	terraform -chdir=infra/bootstrap/gcp validate
	terraform -chdir=infra/docker-host/gcp fmt -check
	terraform -chdir=infra/docker-host/gcp init -backend=false -input=false >/dev/null
	terraform -chdir=infra/docker-host/gcp validate

hermes:
	bash scripts/smoke-hermes-runtime.sh

enterprise-evidence:
	python3 scripts/enterprise-evidence.py verify
	python3 scripts/enterprise-evidence.py pack --output /tmp/model-garden-enterprise-evidence

validate: contracts tests terraform hermes enterprise-evidence
	bash -n scripts/*.sh
