SHELL := /bin/bash

PYTHON ?= python3
NPM ?= npm
VENV_DIR ?= .venv
VENV_PYTHON := $(VENV_DIR)/bin/python
TEMP_ROOT ?= $(if $(TMPDIR),$(TMPDIR),/tmp)
WATCH_PATH ?= $(abspath $(TEMP_ROOT)/localtrace-demo)
# Colon-separated extra directories to observe read-only (never created).
WATCH_PATHS ?=
# Set to 0 to stop watching well-known local-AI model directories.
WATCH_MODEL_DIRS ?= 1
DEMO_TARGET ?= $(WATCH_PATH)/localtrace-demo-model.gguf
DEMO_SIZE_MB ?= 256
DEMO_CHUNK_MB ?= 4
DEMO_DELAY_MS ?= 100
DEMO_FSYNC_EVERY_CHUNKS ?= 1
FILE_GROWTH_ALERT_BYTES ?= 67108864
# Alert delivery: macOS Notification Center banners (NOTIFY=0 to disable)
# and an append-only JSON Lines log (default ~/Library/Logs/LocalTrace/alerts.jsonl).
NOTIFY ?= 1
ALERT_LOG_PATH ?=
# Per-owner usage scan of watched directories: rescan cadence and budgets.
USAGE_SCAN_INTERVAL_SECONDS ?= 60
USAGE_MAX_FILES ?= 200000
USAGE_MAX_SECONDS ?= 20
CAPACITY_ALERT_PERCENT ?= 90
CAPACITY_REARM_PERCENT ?=

.DEFAULT_GOAL := help

.PHONY: help setup setup-backend setup-frontend run run-backend run-frontend \
	test test-tooling test-backend test-frontend demo demo-clean alert-log

help:
	@echo "LocalTrace development commands"
	@echo "  make setup          Install backend and frontend dependencies"
	@echo "  make run            Run the API and dashboard together"
	@echo "  make test           Run backend tests and build-check the frontend"
	@echo "  make demo           Write a safe, temporary .gguf-like workload"
	@echo "  make demo-clean     Remove only the verified demo workload file"
	@echo "  make run CAPACITY_ALERT_PERCENT=75 CAPACITY_REARM_PERCENT=72"
	@echo "  make run WATCH_PATHS=/Volumes/Models:~/team-models"
	@echo "  make run WATCH_MODEL_DIRS=0   Watch only the demo path"
	@echo "  make run USAGE_SCAN_INTERVAL_SECONDS=15   Rescan owner usage more often"
	@echo "  make run NOTIFY=0             Skip Notification Center banners"
	@echo "  make alert-log                Tail the append-only JSONL alert log"

setup: setup-backend setup-frontend

setup-backend:
	$(PYTHON) -m venv "$(VENV_DIR)"
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e './backend[dev]'

setup-frontend:
	$(NPM) --prefix frontend install

# -j runs both long-lived development servers and propagates Ctrl-C to them.
run:
	@$(MAKE) --no-print-directory -j2 run-backend run-frontend

run-backend:
	LOCALTRACE_WATCH_PATH="$(WATCH_PATH)" \
	LOCALTRACE_WATCH_PATHS="$(WATCH_PATHS)" \
	LOCALTRACE_WATCH_MODEL_DIRS="$(WATCH_MODEL_DIRS)" \
	LOCALTRACE_FILE_GROWTH_ALERT_BYTES="$(FILE_GROWTH_ALERT_BYTES)" \
	LOCALTRACE_NOTIFY="$(NOTIFY)" \
	LOCALTRACE_ALERT_LOG_PATH="$(ALERT_LOG_PATH)" \
	LOCALTRACE_USAGE_SCAN_INTERVAL_SECONDS="$(USAGE_SCAN_INTERVAL_SECONDS)" \
	LOCALTRACE_USAGE_MAX_FILES="$(USAGE_MAX_FILES)" \
	LOCALTRACE_USAGE_MAX_SECONDS="$(USAGE_MAX_SECONDS)" \
	LOCALTRACE_CAPACITY_ALERT_PERCENT="$(CAPACITY_ALERT_PERCENT)" \
	LOCALTRACE_CAPACITY_REARM_PERCENT="$(CAPACITY_REARM_PERCENT)" \
	$(VENV_PYTHON) -m uvicorn \
		localtrace_backend.app:app --host 127.0.0.1 --port 8000 --reload

run-frontend:
	$(NPM) --prefix frontend run dev -- --host 127.0.0.1 --port 5173

test: test-tooling test-backend test-frontend

test-tooling:
	$(PYTHON) -m unittest discover -s tooling_tests -v

test-backend:
	$(VENV_PYTHON) -m pytest backend/tests

# The first frontend slice has no unit-test suite yet; a production build is
# still a useful deterministic check for TypeScript and bundling failures.
test-frontend:
	$(NPM) --prefix frontend run build

demo:
	$(PYTHON) scripts/demo_workload.py \
		--target "$(DEMO_TARGET)" \
		--size-mb "$(DEMO_SIZE_MB)" \
		--chunk-mb "$(DEMO_CHUNK_MB)" \
		--delay-ms "$(DEMO_DELAY_MS)" \
		--fsync-every-chunks "$(DEMO_FSYNC_EVERY_CHUNKS)"

demo-clean:
	$(PYTHON) scripts/demo_workload.py --cleanup --target "$(DEMO_TARGET)"

alert-log:
	@LOG="$(ALERT_LOG_PATH)"; \
	if [ -z "$$LOG" ]; then LOG="$$HOME/Library/Logs/LocalTrace/alerts.jsonl"; fi; \
	echo "Following $$LOG (Ctrl-C to stop)"; \
	touch "$$LOG" 2>/dev/null || mkdir -p "$$(dirname "$$LOG")"; \
	tail -n 20 -f "$$LOG"
