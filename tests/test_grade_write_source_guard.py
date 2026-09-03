"""Structural regression check: production SQL writes to the `grades`
table are confined to grade_store.py (the GradeStore aggregate, behind the
StateManager facade) and migrations.py — the authoritative write paths for
that table.

`grades` is the single-writer table this cohort protects: every other
grades-table write site in the repo is either a test fixture (excluded
below) or a violation this guard is meant to catch. A new write location
requires deliberately adding it to ALLOWED_FILES — the single-writer rule
stays enforceable in CI rather than depending on code review alone.

This intentionally does not police `grade_revisions` or
`grade_usage_overrides` writes — those are separate tables with their own
invariants (grade_revisions is append-only and immutable at the database
trigger level; see state_manager.py's `grade_revisions_no_update` /
`grade_revisions_no_delete` triggers) and are out of scope for this guard.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"

# Directories excluded from the Python scan: test fixtures, the venv, and
# the web/ subtree (scanned separately below with its own exclusions and
# file extensions).
PY_EXCLUDED_DIRS = {"tests", ".venv", "web", ".git"}

# Directories excluded from the TypeScript scan: tests, dependencies, and
# build/generated output — none of these are production source.
WEB_EXCLUDED_DIRS = {"__tests__", "node_modules", ".next", "generated"}

# The only production files permitted to write to `grades`: every
# migration that rebuilds or backfills the table
# (_migrate_to_11/15/19/20/21/35, in migrations.py) plus every live write
# path (_upsert_resolved_grade_in_transaction, _write_grade,
# mark_grade_needs_regrade_for_remediation, in storage/grades.py — the
# GradeStore aggregate StateManager delegates to). A future split into
# further migration/remediation files must add each one here explicitly.
ALLOWED_FILES = {"grades.py", "migrations.py"}

# Case-insensitive, whitespace-agnostic (including newlines, so a
# statement split across lines in a multi-line string or template literal
# still matches) match for a write statement targeting the `grades` table
# specifically. \b after "grades" excludes other tables that merely start
# with "grade" — grade_revisions, grade_usage_overrides — since the
# underscore that follows is itself a word character and blocks the
# boundary. INSERT's optional `OR <conflict-action>` clause (e.g. `INSERT
# OR REPLACE INTO`, `INSERT OR IGNORE INTO`) is matched too — SQLite
# accepts it between INSERT and INTO, and it is a real, otherwise
# undetected way to write to grades.
_GRADES_WRITE_RE = re.compile(
    r"""\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+grades\b""",
    re.IGNORECASE,
)


def _has_grades_write(text: str) -> bool:
    return bool(_GRADES_WRITE_RE.search(text))


def _is_excluded(path: pathlib.Path, root: pathlib.Path, excluded_dirs: set[str]) -> bool:
    rel_parts = path.relative_to(root).parts[:-1]
    return any(part in excluded_dirs for part in rel_parts)


def _python_production_files() -> list[pathlib.Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not _is_excluded(p, REPO_ROOT, PY_EXCLUDED_DIRS)
    )


def _web_production_files() -> list[pathlib.Path]:
    if not WEB_ROOT.is_dir():
        return []
    files: list[pathlib.Path] = []
    for pattern in ("*.ts", "*.tsx"):
        files.extend(
            p
            for p in WEB_ROOT.rglob(pattern)
            if not _is_excluded(p, WEB_ROOT, WEB_EXCLUDED_DIRS)
        )
    return sorted(files)


def _all_production_files() -> list[pathlib.Path]:
    return _python_production_files() + _web_production_files()


@pytest.mark.parametrize(
    "path", _all_production_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_only_allowed_files_write_to_grades(path: pathlib.Path) -> None:
    rel = str(path.relative_to(REPO_ROOT))
    has_write = _has_grades_write(path.read_text())
    if path.name in ALLOWED_FILES:
        assert has_write, (
            f"{rel} is declared in ALLOWED_FILES as a grades-table writer but no "
            "longer contains any INSERT/UPDATE/DELETE targeting grades — remove "
            "it from ALLOWED_FILES"
        )
    else:
        assert not has_write, (
            f"{rel} issues a SQL write against the grades table outside the "
            "single authoritative writer — route this through StateManager's "
            "validated grade-write methods instead, or add this file to "
            "ALLOWED_FILES in tests/test_grade_write_source_guard.py after "
            "deliberate review"
        )


@pytest.mark.parametrize(
    "text",
    [
        "conn.execute(\"INSERT INTO grades (post_id) VALUES (?)\", (1,))",
        "conn.execute('UPDATE grades SET needs_regrade = 1 WHERE id = ?', (1,))",
        'conn.execute("DELETE FROM grades WHERE id = ?", (1,))',
        "scout.db.prepare(`INSERT INTO grades\n  (id, post_id) VALUES (?, ?)`)",
        'conn.execute(\n    "UPDATE grades "\n    "SET needs_regrade = 1 WHERE id = ?",\n'
        "    (1,),\n)",
        "insert into grades (post_id) values (?)",
        "conn.execute(\"INSERT OR REPLACE INTO grades (post_id) VALUES (?)\", (1,))",
        "conn.execute(\"INSERT OR IGNORE INTO grades (post_id) VALUES (?)\", (1,))",
        "scout.db.prepare(`INSERT OR ABORT INTO grades (id, post_id) VALUES (?, ?)`)",
    ],
)
def test_has_grades_write_detects_every_shape(text: str) -> None:
    assert _has_grades_write(text)


@pytest.mark.parametrize(
    "text",
    [
        "conn.execute(\"INSERT INTO grade_revisions (grade_id) VALUES (?)\", (1,))",
        "conn.execute('UPDATE grade_usage_overrides SET mode = ? "
        "WHERE grade_id = ?', ('exclude', 1))",
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique ON grades(evaluation_id)",
        "SELECT * FROM grades WHERE id = ?",
        "x = 'grades are important'",
    ],
)
def test_has_grades_write_does_not_false_positive(text: str) -> None:
    assert not _has_grades_write(text)


def test_python_production_files_were_found() -> None:
    assert _python_production_files(), "expected to find production .py files in the repo"


def test_web_production_files_were_found() -> None:
    assert _web_production_files(), "expected to find production .ts/.tsx files under web/"


def test_allowed_files_are_a_subset_of_scanned_files() -> None:
    scanned = {p.name for p in _all_production_files()}
    assert scanned >= ALLOWED_FILES
