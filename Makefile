.PHONY: dev validate contracts tests terraform hermes assurance assurance-scan proof scorecard ops

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

assurance:
	python3 scripts/enterprise-evidence.py

assurance-scan:
	python3 -m pip install -r requirements-assurance.txt
	bash scripts/run-assurance-scans.sh

proof:
	@echo 'Run on a deployed host: python3 scripts/reference-proof.py --project-name <client-env> --require-voice-call --require-fallback --require-governed-action'

scorecard:
	@echo 'python3 scripts/operating-scorecard.py summary <private-scorecard.yaml>'

ops:
	@echo 'python3 scripts/operations-readiness.py <client-operations.yaml>'

validate: contracts tests terraform hermes
	python3 -m py_compile scripts/deploy-reference-voice.py scripts/reference-proof.py scripts/operating-scorecard.py scripts/operations-readiness.py platform/channels/livekit_receptionist.py
	bash -n scripts/*.sh
