.PHONY: help test test-live doctor sniff lint fmt install

help:
	@echo "test       unit tests (offline)"
	@echo "test-live  unit tests + live network checks"
	@echo "doctor     health-check all upstream data sources"
	@echo "sniff      live network read-out with FIR coverage"
	@echo "lint       ruff"
	@echo "install    editable install with dev extras"

test:
	python -m pytest -q

test-live:
	python -m pytest -q -m network

doctor:
	PYTHONPATH=src python -m skysherpa doctor

sniff:
	PYTHONPATH=src python -m skysherpa sniff --coverage

lint:
	python -m ruff check src tests

fmt:
	python -m ruff format src tests

install:
	pip install -e ".[dev,server,science]"
