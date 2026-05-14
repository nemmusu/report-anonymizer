.PHONY: install dev-install test test-fast lint format check selftest gui clean

PY ?= python
VENV := .venv
ACT := . $(VENV)/bin/activate &&

$(VENV):
	$(PY) -m venv $(VENV)
	$(ACT) pip install -U pip wheel

install: $(VENV)
	$(ACT) pip install -r requirements.txt

dev-install: install
	$(ACT) pip install -r requirements-dev.txt

test:
	$(ACT) pytest -ra

test-fast:
	$(ACT) pytest -ra -x -q --ignore=tests/test_perf_smoke.py

lint:
	$(ACT) ruff check anonymize gui tests

format:
	$(ACT) ruff format anonymize gui tests

check: lint test

selftest:
	$(ACT) python bin/anonymize-dossier selftest

gui:
	$(ACT) python -m gui.main

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache htmlcov build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
