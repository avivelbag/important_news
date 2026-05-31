import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DEPLOYMENT = REPO_ROOT / "DEPLOYMENT.md"
SETTINGS = REPO_ROOT / ".github" / "settings.yml"
GITIGNORE = REPO_ROOT / ".gitignore"


def _readme():
    return README.read_text(encoding="utf-8")


def test_required_doc_files_exist():
    assert README.is_file()
    assert DEPLOYMENT.is_file()
    assert SETTINGS.is_file()


def test_readme_documents_enabling_github_pages():
    text = _readme().lower()
    assert "github pages" in text
    assert "settings" in text and "pages" in text
    assert "/docs" in _readme()


def test_readme_documents_folder_structure():
    text = _readme()
    assert "Folder structure" in text
    assert "docs/" in text
    assert re.search(r"docs/.*serv", text, re.IGNORECASE | re.DOTALL)


def test_readme_documents_local_refresh():
    text = _readme()
    assert "python scripts/refresh.py" in text
    assert "make refresh" in text


def test_readme_documents_automated_refresh():
    text = _readme()
    assert ".github/workflows/refresh-feed.yml" in text
    assert "GitHub Actions" in text
    assert "0 */6 * * *" in text


def test_readme_lists_data_sources():
    text = _readme()
    assert "Hacker News" in text
    assert "NASA" in text
    assert "MIT Technology Review" in text


def test_docs_dir_not_gitignored():
    # GitHub Pages serves committed docs/, so it must not be ignored.
    patterns = [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for pat in patterns:
        normalized = pat.rstrip("/")
        assert normalized != "docs"
        assert normalized != "/docs"


def test_settings_yaml_configures_pages_from_docs():
    config = yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))
    assert config["repository"]["has_pages"] is True
    assert config["pages"]["source"]["path"] == "/docs"


def test_deployment_guide_explains_adding_sources():
    text = DEPLOYMENT.read_text(encoding="utf-8")
    assert "DEFAULT_SOURCES" in text
    assert "src/scraper.py" in text
    assert "SourceSpec" in text


def test_readme_links_deployment_guide():
    assert "DEPLOYMENT.md" in _readme()


def test_readme_data_sources_match_scraper():
    # The documented sources must match the names actually scraped so the
    # README does not drift from DEFAULT_SOURCES.
    import src.scraper as scraper

    readme = _readme()
    for spec in scraper.DEFAULT_SOURCES:
        first_word = spec.name.split()[0]
        assert first_word in readme
