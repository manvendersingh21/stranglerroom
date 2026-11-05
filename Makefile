# StranglerRoom operations.
#
# PY may be overridden, e.g. `make test PY=python3`.
PY ?= .venv/bin/python

.DEFAULT_GOAL := help

.PHONY: help install switch-up switch-down targets provision agents agents-stop \
        test test-integration e2e demo cinema stop clean-state isolate isolate-verify isolate-off

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create .venv with uv and install the pinned dependencies.
	test -d .venv || uv venv .venv
	uv pip install --python .venv/bin/python -r requirements.txt

switch-up: ## Start the Switch + Mattermost stack.
	bash scripts/bootstrap_switch.sh up

switch-down: ## Stop the Switch + Mattermost stack.
	bash scripts/bootstrap_switch.sh stop

targets: ## Start the legacy, modern and proxy targets.
	PYTHON=$(PY) bash scripts/targets.sh start

provision: ## Register agents and create/reuse the migration room.
	PYTHONPATH=. $(PY) -m agents.provision

agents: ## Start the three agents.
	PYTHON=$(PY) bash scripts/agents.sh start

agents-stop: ## Stop the three agents.
	PYTHON=$(PY) bash scripts/agents.sh stop

test: ## Run the fast test suite (non-integration).
	$(PY) -m pytest -m 'not integration'

test-integration: ## Run the live Switch integration tests.
	$(PY) -m pytest -m integration

e2e: ## Run the end-to-end demo from a fresh state.
	$(PY) scripts/run_e2e_demo.py --fresh

demo: ## Full demo: Switch, fresh targets, provision, agents, e2e (in order).
	+$(MAKE) switch-up
	PYTHON=$(PY) bash scripts/targets.sh start --fresh
	+$(MAKE) provision
	+$(MAKE) agents
	+$(MAKE) e2e

cinema: ## Run the demo with cinematic pacing from a fresh state.
	$(PY) scripts/run_e2e_demo.py --cinema --fresh

stop: ## Stop agents, targets and Switch.
	+$(MAKE) agents-stop
	PYTHON=$(PY) bash scripts/targets.sh stop
	+$(MAKE) switch-down

clean-state: ## Stop agents+targets, then remove runtime state (keeps switch.env and connection.env).
	PYTHON=$(PY) bash scripts/agents.sh stop
	PYTHON=$(PY) bash scripts/targets.sh stop
	PYTHONPATH=. $(PY) -m agents.isolation clean
	mkdir -p "$${STRANGLER_STATE_DIR:-.state}"
	find "$${STRANGLER_STATE_DIR:-.state}" -mindepth 1 -maxdepth 1 ! -name switch.env ! -name connection.env -exec rm -rf {} +

isolate: ## Run every component as its own OS user from /srv/stranglerroom (root; docs/ISOLATION.md).
	PYTHONPATH=. $(PY) -m agents.isolation setup

isolate-verify: ## Check users, service area, access matrix, smoke imports, secret exposure, live processes.
	PYTHONPATH=. $(PY) -m agents.isolation verify

isolate-off: ## Turn isolation off (remove the marker); `python -m agents.isolation disable --purge` also deletes users.
	PYTHONPATH=. $(PY) -m agents.isolation disable
