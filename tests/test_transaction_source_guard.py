"""Structural regression check: db.py is the sole owner of transaction
mechanics anywhere in the production source tree, with a narrow set of
exceptions.

scan_runner.py and scripts/grade_corpus_audit.py — the two remaining
pre-cohort exceptions — were migrated onto Db's
transaction()/begin_immediate()/read_transaction() contexts. Any future
transaction-control leakage anywhere in production code must fail this
test, with three exceptions: scripts/audit_feedback_policy.py,
scripts/grade_corpus_audit.py, and phase1_audit_data.py each open their
own dedicated sqlite3 connection via a `mode=ro` URI with `PRAGMA
query_only=ON` — mechanically incapable of writing regardless of
transaction state — and each needs its own explicit BEGIN/ROLLBACK read
transaction so every query in one report or audit run sees the same
snapshot. None can route through Db (db.py has no `mode=ro`/URI connection
mode), and none is a StateManager consumer, so each is exempted the same
way db.py is: it is the sole owner of transaction mechanics for its own
private, read-only connection.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {"tests", ".venv", "web", ".git"}

# db.py is the sole owner of transaction mechanics for the shared
# application connection. scripts/audit_feedback_policy.py,
# scripts/grade_corpus_audit.py, and paa/audit/data.py are exempted
# too, but only for their own separate, mechanically read-only (mode=ro +
# query_only=ON) connections — see the module docstring above. Non-Python
# artifacts are outside this Python-source guard by construction (only
# *.py is scanned below).
OWNER_MODULES = {
    "src/scout/storage/db.py",
    "scripts/audit_feedback_policy.py",
    "scripts/grade_corpus_audit.py",
    "src/scout/paa/audit/data.py",
}

# Case-insensitive: a literal opening quote (either style), optionally
# followed by leading whitespace/newline (e.g. execute("\nBEGIN") or a
# triple-quoted f"""  COMMIT"""), landing on one of these keywords, e.g.
# execute("BEGIN IMMEDIATE"), execute('begin'), f"SAVEPOINT {name}" —
# quote-style-agnostic and case-agnostic, unlike a fixed uppercase
# literal-string pattern list, which a lowercase or single-quoted call
# would slip past undetected. \b keeps "BEGIN" from matching inside an
# unrelated word like "beginning", and "COMMIT" from matching inside
# "committed".
_SQL_LITERAL_RE = re.compile(
    r"""['"]\s*(?:BEGIN\s+IMMEDIATE|BEGIN|SAVEPOINT|RELEASE\s+SAVEPOINT|RELEASE|"""
    r"""ROLLBACK\s+TO\s+SAVEPOINT|ROLLBACK|COMMIT)\b""",
    re.IGNORECASE,
)

# Bare commit()/rollback() calls on a `.conn` attribute — covers both
# `state.conn.commit()` (StateManager's compatibility passthrough) and any
# direct `db.conn.commit()` — the two ways application code could reach
# past Db's owned contexts straight to the underlying sqlite3.Connection.
_BARE_PATTERNS = (".conn.commit(", ".conn.rollback(")


def _is_production_file(path: pathlib.Path) -> bool:
    rel_parts = path.relative_to(REPO_ROOT).parts[:-1]
    return not any(part in EXCLUDED_DIRS for part in rel_parts)


def _has_transaction_control(text: str) -> bool:
    return bool(_SQL_LITERAL_RE.search(text)) or any(
        pattern in text for pattern in _BARE_PATTERNS
    )


def _production_files() -> list[pathlib.Path]:
    return sorted(p for p in REPO_ROOT.rglob("*.py") if _is_production_file(p))


@pytest.mark.parametrize("path", _production_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_only_db_py_issues_transaction_control(path: pathlib.Path) -> None:
    rel = str(path.relative_to(REPO_ROOT))
    has_control = _has_transaction_control(path.read_text())
    if rel in OWNER_MODULES:
        assert has_control, (
            f"{rel} is declared as a transaction-mechanics owner module but no "
            "longer issues any transaction-control SQL — remove it from "
            "OWNER_MODULES"
        )
    else:
        assert not has_control, (
            f"{rel} issues transaction-control SQL or a bare .conn.commit()/"
            ".conn.rollback() call outside db.py — route it through "
            "Db.transaction() / Db.begin_immediate() / Db.read_transaction() instead"
        )


@pytest.mark.parametrize(
    "text",
    [
        'conn.execute("BEGIN IMMEDIATE")',
        "conn.execute('begin')",
        'conn.execute("\nBEGIN")',
        'conn.execute("  COMMIT")',
        'conn.execute(f"""\n    ROLLBACK""")',
        "state.conn.commit()",
        "scout.db.conn.rollback()",
    ],
)
def test_has_transaction_control_detects_every_shape(text: str) -> None:
    assert _has_transaction_control(text)


@pytest.mark.parametrize(
    "text",
    [
        'x = "committed"',
        'logger.info("beginning scan")',
        "state.commit()",
        "state.rollback()",
    ],
)
def test_has_transaction_control_does_not_false_positive(text: str) -> None:
    assert not _has_transaction_control(text)


def test_production_files_were_found() -> None:
    assert _production_files(), "expected to find production .py files in the repo"


def test_owner_modules_are_a_subset_of_scanned_files() -> None:
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _production_files()}
    assert scanned >= OWNER_MODULES
