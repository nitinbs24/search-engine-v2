COMPOSE = docker compose --env-file .env -f infrastructure/docker-compose.yml

.PHONY: help up down test smoke

help: ## List targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

.env:
	cp .env.example .env

up: .env ## Start the stack and wait until every service is healthy
	$(COMPOSE) up -d --wait
	$(COMPOSE) run --rm minio-init

down: ## Stop the stack (volumes are kept)
	$(COMPOSE) down

test: ## Run all unit tests
	@echo "no test suites yet (first one arrives with T3)"

smoke: ## Upload a file and find it through the API
	bash scripts/smoke.sh
