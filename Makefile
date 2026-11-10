# Ubuntu ships python3 and not python; a venv has both. Prefer the project venv, then
# python3, and fall back to python only so this still works where that is the name.
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	elif command -v python3 >/dev/null 2>&1; then echo python3; else echo python; fi)
SKY = PYTHONPATH=src $(PYTHON) -m skysherpa

.PHONY: help test test-live check doctor sniff publish publish-watch site demo-site lint fmt install

help:
	@echo "test       unit tests (offline)"
	@echo "check      tests plus bundle verification -- what CI should run"
	@echo "test-live  unit tests + live network checks"
	@echo "doctor     health-check all upstream data sources"
	@echo "sniff      live network read-out with FIR coverage"
	@echo "publish    build the bundle once"
	@echo "lint       ruff"
	@echo "install    editable install with dev extras"

test:
	$(PYTHON) -m pytest -q

check: test
	$(SKY) verify

test-live:
	$(PYTHON) -m pytest -q -m network

doctor:
	$(SKY) doctor

sniff:
	$(SKY) sniff --coverage

publish:
	$(SKY) publish --out dist

publish-watch:
	$(SKY) publish --out dist --watch

site:
	$(PYTHON) -m http.server 8099 --directory dist

demo-site:
	$(SKY) simulate --data-dir data/sim --days 56 --force
	$(SKY) publish --data-dir data/sim --out dist
	$(PYTHON) -m http.server 8099 --directory dist

lint:
	$(PYTHON) -m ruff check src tests

fmt:
	$(PYTHON) -m ruff format src tests

install:
	$(PYTHON) -m pip install -e ".[dev]"
