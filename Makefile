# StranglerRoom operations.
#
# PY may be overridden, e.g. `make test PY=python3`.
PY ?= .venv/bin/python

.DEFAULT_GOAL := help

.PHONY: help install switch-up switch-down targets provision agents agents-stop \
        test test-integration e2e demo cinema stop clean-state

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
	bash scripts/agents.sh stop

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
	bash scripts/targets.sh stop
	+$(MAKE) switch-down

clean-state: ## Stop agents+targets, then remove runtime state (keeps .state/switch.env).
	bash scripts/agents.sh stop
	bash scripts/targets.sh stop
	mkdir -p .state
	find .state -mindepth 1 -maxdepth 1 ! -name switch.env -exec rm -rf {} +
