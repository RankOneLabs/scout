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

Why the holdout cohort touched this guard, and how far: `scout holdout release`
put the English word "Release" at the start of a module docstring, a CLI
subcommand name and a help string, and the pattern matched any literal opening
with a transaction-control keyword — so correct code failed the guard. The
change is confined to what the guard recognizes as a statement, and it
recognizes strictly more than before rather than less: every real call in db.py
still matches, verified case by case, and a transaction-control statement inside
a multi-statement `executescript()` literal now matches too, which the earlier
whole-literal anchoring missed. No module was added to OWNER_MODULES and no file
was exempted.
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

# A savepoint name as it appears in a literal: a plain identifier, or an
# f-string placeholder standing in for one.
_SAVEPOINT_NAME = r"(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)"

# Case-insensitive and quote-style-agnostic, unlike a fixed uppercase
# literal-string pattern list, which a lowercase or single-quoted call would
# slip past undetected. SAVEPOINT, RELEASE and ROLLBACK TO each require a
# savepoint name, spelled as a plain identifier or an f-string placeholder.
_TRANSACTION_STATEMENTS = (
    r"BEGIN\s+IMMEDIATE",
    r"BEGIN",
    r"COMMIT",
    rf"SAVEPOINT\s+{_SAVEPOINT_NAME}",
    rf"RELEASE\s+(?:SAVEPOINT\s+)?{_SAVEPOINT_NAME}",
    rf"ROLLBACK\s+TO\s+(?:SAVEPOINT\s+)?{_SAVEPOINT_NAME}",
    r"ROLLBACK",
)
_STATEMENT = "|".join(_TRANSACTION_STATEMENTS)
_LITERAL_OPEN = r"""['"]{1,3}"""

# Two shapes reach the driver, and both count.
#
# `execute()` rejects multiple statements, so what it is given is a literal
# that is one transaction-control statement and nothing else — matched whole,
# allowing leading whitespace or a newline (`execute("\nBEGIN")`, a
# triple-quoted f"""  COMMIT""") and an optional trailing semicolon.
#
# `executescript()` accepts a whole script, so a transaction-control statement
# can sit anywhere inside a longer literal. That shape is matched at a
# statement position — right after the opening quote or after a preceding
# statement's semicolon — and only when terminated by its own semicolon, which
# is what keeps `executescript("BEGIN; ...; COMMIT")` visible to this guard
# rather than passing as "not the whole literal".
#
# Neither shape matches English prose. A docstring or a CLI help string that
# opens with "Release ..." has no savepoint name and closing quote after it and
# no terminating semicolon; a sentence that happens to end "...so those rows
# commit;" is not at a statement position; and a trigger body's `BEGIN` is
# followed by the statement it guards rather than by a semicolon. \b keeps
# "BEGIN" out of "beginning" and "COMMIT" out of "committed".
_SQL_LITERAL_RE = re.compile(
    "|".join(
        (
            rf"""{_LITERAL_OPEN}\s*(?:{_STATEMENT})\s*;?\s*['"]""",
            rf"""(?:{_LITERAL_OPEN}|;)\s*\b(?:{_STATEMENT})\s*;""",
        )
    ),
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
        'conn.execute("COMMIT;")',
        'conn.execute(f"""\n    ROLLBACK""")',
        'conn.execute(f"SAVEPOINT {name}")',
        'conn.execute(f"RELEASE SAVEPOINT {name}")',
        "state.conn.commit()",
        "scout.db.conn.rollback()",
        # A script is the other thing that reaches the driver, and the
        # statement inside it is no less real for having company.
        'conn.executescript("BEGIN; UPDATE posts SET id = 1; COMMIT")',
        'conn.executescript("""\n    BEGIN IMMEDIATE;\n    DELETE FROM posts;\n    """)',
        "conn.executescript('SAVEPOINT s; DELETE FROM posts; RELEASE s;')",
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
        # Prose that opens on one of the keywords. The release subcommand's own
        # help string is this shape, and is what the earlier whole-literal
        # anchoring was added for.
        '"""Release held posts against stored labels."""',
        'parser.add_parser("release", help="Release one holdout")',
        '"Releases the claim lease; the fence advances"',
        # Prose that closes on one, semicolon included — not at a statement
        # position, so not a script.
        '"persist_surfaced_outcome raises only so those rows commit; this mirrors it"',
        # A trigger body, which owns a BEGIN that is not transaction control.
        '"""CREATE TRIGGER t BEFORE UPDATE ON posts\n'
        "BEGIN\n    SELECT RAISE(ABORT, 'posts is immutable');\nEND\"\"\"",
    ],
)
def test_has_transaction_control_does_not_false_positive(text: str) -> None:
    assert not _has_transaction_control(text)


def test_production_files_were_found() -> None:
    assert _production_files(), "expected to find production .py files in the repo"


def test_owner_modules_are_a_subset_of_scanned_files() -> None:
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _production_files()}
    assert scanned >= OWNER_MODULES
