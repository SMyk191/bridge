# Bridge — top-level developer entry points. Run from the repo root.
#
# Every target invokes backend/.venv's own interpreter directly (no `cd` and no
# activation) — the editable install resolves `bridge.*` regardless of CWD, and
# config.py locates data/index, data/repos, etc. from its own file location, not
# from CWD either. Verified: `backend/.venv/bin/python -m bridge.index.build ...`
# and `-c "from bridge.api.main import app"` both work unchanged from repo root.
#
# One-time setup:
#   cd backend && uv venv .venv --python 3.12 && uv pip install -e ".[dev]"

REPO := data/repos/spring-petclinic
PY := backend/.venv/bin/python
UVICORN := backend/.venv/bin/uvicorn
API_HOST ?= 127.0.0.1
API_PORT ?= 8000

.PHONY: help index api ui smoke seed determinism full gate gate-baseline quality

help:
	@echo "make index        - build the structural + semantic index for $(REPO)"
	@echo "make api          - run the FastAPI app on http://$(API_HOST):$(API_PORT)"
	@echo "make ui           - run the Vite dev server on http://localhost:5173"
	@echo "make smoke        - run eval/run_smoke.py against the current index"
	@echo "make seed         - seed 3 demo threads so the dashboard isn't empty"
	@echo "make determinism  - 3 cold runs/question, assert identical citation sets (CI hook)"
	@echo "make full         - eval/run_full.py: recall, rubric, confidence, cost, determinism"
	@echo "make gate         - eval/gate.py: run the suite, fail on regression vs the stored baseline"
	@echo "make gate-baseline - run the suite and STORE it as the new baseline"
	@echo "make quality      - eval/rolling_quality.py: sample+score real traffic, print the trend"

## Build data/index/<sha>/ — graph, descriptions, vectors, history, suggestions.
index:
	$(PY) -m bridge.index.build $(REPO)

## Serve the orchestrator over HTTP. Requires `make index` to have run at least
## once — the app resolves the most recent index at startup (lifespan) and fails
## loudly if none exists, rather than starting in a silently-broken state.
## --reload-dir is explicit (not left to uvicorn's CWD-based default) to keep the
## watcher scoped to source. `make api` runs from the repo root, so the default
## (Path.cwd()) would already walk into backend/bridge/ just fine — but it would
## also walk data/repos/<cloned-repo> and data/index/, which `make index`
## rewrites and which can hold thousands of files, triggering needless reloads.
api:
	$(UVICORN) bridge.api.main:app --reload --reload-dir backend/bridge \
		--host $(API_HOST) --port $(API_PORT)

## Vite + React + TypeScript + Tailwind, in frontend/. Its dev server binds to
## localhost:5173 (frontend/vite.config.ts pins host+port) — that exact origin
## is what backend/bridge/config.py::API_CORS_ORIGINS allows, so `make api`
## must already be running for the frontend to reach it.
ui:
	cd frontend && npm run dev

## Run the smoke suite against the current index and print recall, confidence,
## and cost per question.
smoke:
	$(PY) eval/run_smoke.py

## Run 3 real questions through the orchestrator so `make ui` has something to
## show — a question, a change_request, and a bug, each its own thread.
seed:
	$(PY) eval/seed_threads.py

## Ask the same question 3x cold (fresh=True, never the answer cache) and
## assert the citation SET is identical every time. This is the artifact meant
## to run in CI; there's no workflow file to wire it into yet (Bridge has no CI
## pipeline configured — see PROGRESS.md, phase D), so this target IS the hook.
determinism:
	$(PY) eval/check_determinism.py

## The full report: file precision/recall, core-file recall, rubric score,
## confidence, lane, cost, latency, and determinism, for eval/sets/*.yaml.
full:
	$(PY) eval/run_full.py

## Gate CI on eval/gate.py's stored baseline (eval/eval_baseline.json). Fails
## (exit 1) on a core-recall drop past config.GATE_CORE_RECALL_DROP_PTS, a
## rubric drop past config.GATE_RUBRIC_DROP, or any question that was
## determinism-stable in the baseline and is not stable now.
gate:
	$(PY) eval/gate.py

## Run the full suite and store its report as the new baseline gate.py compares
## against. Run this deliberately, after a human has looked at the numbers —
## never automatically on a passing CI run, or a slow regression could ratchet
## itself into the new normal one baseline at a time.
gate-baseline:
	$(PY) eval/gate.py --save-baseline

## Rubric-score a stratified sample of real traffic and print the trend table
## (eval/rolling_quality.py). Meant to run weekly; also mirrors every
## thumbs-down piece of feedback into eval_candidates for a human to review.
quality:
	$(PY) eval/rolling_quality.py
