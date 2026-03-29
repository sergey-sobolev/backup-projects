# Подготовка системы и окружения для backup-projects.
# system-deps обычно: sudo make system-deps

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: venv deps install system-deps test clean

venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip

deps: venv
	$(PIP) install -e ".[dev]"

install: deps

# Debian/Ubuntu: rsync, PyYAML для системного python, venv и pip
system-deps:
	apt-get update
	apt-get install -y rsync python3-yaml python3-venv python3-pip

test: deps
	$(PY) -m pytest tests/ -v --tb=short

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
