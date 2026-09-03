from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _patterns(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_root_dockerignore_excludes_sensitive_context_files() -> None:
    patterns = _patterns(ROOT / ".dockerignore")

    required = {
        ".env",
        ".env.*",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        ".git",
        ".gitignore",
        "node_modules/",
        "web/node_modules/",
        "__pycache__/",
        "*.pyc",
        ".venv/",
        "venv/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".next/",
        "web/.next/",
        "digests/",
        "scout.db",
        "scout_feedback.db",
        "scout_traces.db",
    }

    assert required <= patterns


def test_web_dockerignore_excludes_web_context_secrets_and_state() -> None:
    patterns = _patterns(ROOT / "web" / ".dockerignore")

    required = {
        ".env",
        ".env.*",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "node_modules/",
        ".next/",
        ".git",
        ".gitignore",
        "coverage/",
    }

    assert required <= patterns
