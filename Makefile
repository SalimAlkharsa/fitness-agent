.PHONY: dev dev-dry test lint sandbox eval bootstrap migrate stage deploy url fn-logs

# uv installs to ~/.local/bin; make sure it's reachable in non-login shells.
export PATH := $(HOME)/.local/bin:$(PATH)

dev:
	uv run python -m app.devserver

# Local UI/flow testing without spending the OpenRouter free-tier quota: the
# agent serves canned responses (see app/llm.py DryRunClient).
dev-dry:
	DRY_RUN=1 uv run python -m app.devserver

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

# Offline skill tester: the real agent against a mock model + mock sheet, no keys.
# Slow (real model calls per step), so it's deliberately not part of `make test`.
sandbox:
	uv run python -m sandbox

eval:
	uv run python -m app.evals MODEL=$(MODEL)

bootstrap:
	uv run python -m app.bootstrap

migrate:
	uv run python -m app.migrate

# Stage the function bundle: DO Functions deploys packages/<pkg>/<fn>/, so copy
# the app package + entry point + requirements in (packages/ is gitignored).
stage:
	rm -rf packages
	mkdir -p packages/fitness/app
	cp deploy/fn_main.py packages/fitness/app/__main__.py
	cp deploy/fn_requirements.txt packages/fitness/app/requirements.txt
	cp deploy/fn_build.sh packages/fitness/app/build.sh
	chmod +x packages/fitness/app/build.sh
	cp -r src/app packages/fitness/app/app
	cp -r skills packages/fitness/app/skills

# Deploy to DigitalOcean Functions (needs doctl auth'd + `doctl serverless connect`;
# see deploy/RUNBOOK.md). Env vars for project.yml come from .env; the optional
# ones get defaults here because the substituter rejects empty/unset variables
# (BASE_PATH "/" is equivalent to "" — config strips trailing slashes).
deploy: stage
	set -a && . ./.env && set +a && \
	export BASE_PATH="$${BASE_PATH:-/}" MODEL="$${MODEL:-nvidia/nemotron-3-ultra-550b-a55b:free}" TOOL_MODE="$${TOOL_MODE:-native}" && \
	doctl serverless deploy . --remote-build

url:
	doctl serverless functions get fitness/app --url

fn-logs:
	doctl serverless activations logs --follow
