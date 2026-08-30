# Every target here is a command this project actually needs; nothing is wrapped for the
# sake of having a wrapper. `make` on its own lists them.
.DEFAULT_GOAL := help
.PHONY: help install test test-py test-js up down logs rebuild acceptance \
        two-workers ledger reset-ledger clean

BASE ?= http://localhost:8850

help:  ## List the targets
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the venv, semora included, from the lock file
	uv sync

test: test-py test-js  ## Everything that runs without a server

test-py:  ## pytest
	uv run pytest -q

test-js:  ## The run-inspector state machine and frame reducer
	node tests/test_stream.mjs

up:  ## Build and start the console on a postgres ledger
	docker compose up -d --build
	@until curl -sf -m 2 $(BASE)/api/units >/dev/null; do sleep 1; done
	@echo "$(BASE)"

down:  ## Stop the stack, keeping the ledger
	docker compose --profile two-workers down

logs:  ## Follow the console's output
	docker compose logs -f console

rebuild: up  ## Alias for up; the image rebuilds either way

acceptance: up  ## Twelve live checks against a real model and a real ledger
	uv run python scripts/acceptance.py $(BASE)

two-workers:  ## A second worker on :8851, sharing the ledger
	docker compose --profile two-workers up -d --build
	@until curl -sf -m 2 http://localhost:8851/api/units >/dev/null; do sleep 1; done
	@echo "worker-a $(BASE)  worker-b http://localhost:8851"

ledger:  ## Open psql on the run ledger
	docker compose exec db psql -U console -d console

reset-ledger:  ## Drop the ledger volume. Parked runs and payment records go with it.
	docker compose --profile two-workers down -v

clean: reset-ledger  ## reset-ledger, plus local caches
	rm -rf .pytest_cache .ruff_cache
	find src tests -name __pycache__ -type d -exec rm -rf {} +
