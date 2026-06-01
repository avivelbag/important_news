.PHONY: refresh scrape site test

# Prefer the project virtualenv if present, else fall back to python3 (e.g. CI,
# where setup-python provides the interpreter and deps via requirements.txt).
PYTHON := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

refresh:
	$(PYTHON) scripts/refresh.py

scrape:
	$(PYTHON) -m src.scraper

site:
	$(PYTHON) -m src.generate_site

test:
	$(PYTHON) -m pytest tests/ -x --tb=short -q
