.PHONY: refresh scrape site test

refresh:
	python scripts/refresh.py

scrape:
	python -m src.scraper

site:
	python -m src.generate_site

test:
	python3 -m pytest tests/ -x --tb=short -q
