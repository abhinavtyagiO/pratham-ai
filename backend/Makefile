PYTHON ?= ../.venv/bin/python
ALEMBIC = ../.venv/bin/alembic

.PHONY: migrate seed test eval simulate serve lint
migrate:
	$(ALEMBIC) upgrade head
seed:
	$(PYTHON) -m app.seed
test:
	$(PYTHON) -m pytest
eval:
	$(PYTHON) -m app.evals --report reports/eval.json
simulate:
	$(PYTHON) -m app.simulate_call "MRF 195/65 R15 ka rate kya hai?"
serve:
	$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
lint:
	../.venv/bin/ruff check .
