VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv install ci lint type test fmt init doctor clean

venv:
	python3 -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip -q
	$(PIP) install -e ".[dev]" -q

lint:
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/lint-imports

fmt:
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

type:
	$(VENV)/bin/mypy

test:
	$(VENV)/bin/pytest -m "not live" -q

cov:
	$(VENV)/bin/pytest -m "not live" \
	  --cov=sali.memory --cov=sali.graph --cov=sali.security --cov=sali.verify \
	  --cov=sali.runtime --cov=sali.context --cov=sali.retrieval \
	  --cov-report=term-missing --cov-fail-under=85 -q

# The gate: ruff + import-layers + types + tests-with-coverage.
# (DB tests auto-skip if Postgres is unreachable.)
ci: lint type cov

init:
	$(VENV)/bin/sali init

doctor:
	$(VENV)/bin/sali doctor

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache **/__pycache__
