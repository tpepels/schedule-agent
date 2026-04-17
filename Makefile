PYTHON ?= python3

.PHONY: install-dev format lint test check

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

test:
	$(PYTHON) -m pytest -q

check: lint test
