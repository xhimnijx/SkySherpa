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

publish:
	PYTHONPATH=src python -m skysherpa publish --out dist

publish-watch:
	PYTHONPATH=src python -m skysherpa publish --out dist --watch

site:
	python -m http.server 8099 --directory dist

demo-site:
	PYTHONPATH=src python -m skysherpa simulate --data-dir data/sim --days 56 --force
	PYTHONPATH=src python -m skysherpa publish --data-dir data/sim --out dist
	python -m http.server 8099 --directory dist

lint:
	python -m ruff check src tests

fmt:
	python -m ruff format src tests

install:
	pip install -e ".[dev,server,science]"
