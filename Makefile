# GPU=1 gives the model server the GPU (needs the NVIDIA Container Toolkit).
COMPOSE = docker compose --env-file .env -f infrastructure/docker-compose.yml $(if $(filter 1,$(GPU)),-f infrastructure/docker-compose.gpu.yml)

# Each directory is its own pytest run: module names repeat across workers.
PY_TESTS = workers/model-server workers/ingestion workers/search infrastructure/opensearch infrastructure/k8s

.PHONY: help up down test smoke integration eval

help: ## List targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

.env:
	cp .env.example .env

up: .env ## Start the stack and wait until every service is healthy (GPU=1 for the GPU)
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (volumes are kept)
	$(COMPOSE) down

test: ## Run all unit tests
	docker run --rm -v "$(CURDIR)":/src -v se2-pip-cache:/root/.cache/pip -w /src python:3.11-slim sh -c '\
		pip install -q --root-user-action=ignore -r requirements-dev.txt && \
		mkdir -p workers/search/proto_gen && export PYTHONPATH=/src/workers/search/proto_gen && \
		python -m grpc_tools.protoc -I shared/proto --python_out=$$PYTHONPATH --grpc_python_out=$$PYTHONPATH search.proto && \
		for d in $(PY_TESTS); do echo "== $$d"; (cd $$d && python -m pytest -q -p no:cacheprovider) || exit 1; done'
	docker run --rm -v "$(CURDIR)/frontend/lib":/src:ro node:20-alpine node --test /src
	docker build -q --target test -f backend/Dockerfile . >/dev/null && echo "== backend: go vet and go test passed"

smoke: ## Upload a file and find it through the API (TARGET=minikube for the cluster)
	bash scripts/smoke.sh

integration: ## Run the end-to-end cases against the running stack
	docker run --rm --network host --env-file .env -v "$(CURDIR)":/src -v se2-pip-cache:/root/.cache/pip -w /src python:3.11-slim sh -c '\
		pip install -q --root-user-action=ignore -r requirements-dev.txt && \
		python -m pytest -q -p no:cacheprovider tests/integration'

eval: ## Upload the evaluation corpus, print recall@5 and MRR, remove the corpus
	python3 eval/run.py
