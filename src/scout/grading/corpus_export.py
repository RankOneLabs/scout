"""Preserve the grading corpus across a database reset.

Scout's PAA modules are being replaced by ``paa_runtime``, and the plan
is to start from a fresh database on the new schema rather than migrate
the old one. That decision is cheap for everything Scout can regenerate
— scans, candidates, drafts, publications all come back by running the
pipeline again — and unrecoverable for the one thing it cannot: human
grading judgments, which exist only because a person sat down and made
them.

This module carries exactly those forward.

Six tables, not three
---------------------
The grading verdicts live in ``grades``, ``grade_revisions``, and
``grade_usage_overrides``. Exporting only those three produces a file
that looks complete and is useless: ``grades.post_id`` is NOT NULL, and
the query the feedback loop actually runs joins it *inner*::

    FROM grades g
    JOIN posts p ON p.id = g.post_id
    LEFT JOIN evaluations e ON e.id = g.evaluation_id
    LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id

Drop ``posts`` and that returns zero rows — every grade still present,
none of them visible, and nothing raising. ``evaluations`` is the model
verdict a grade is a judgment *of*, and ``draft_comments`` is what the
``comment_quality`` and ``comment_issue`` columns score. A grade without
them is a number with no subject.

The last chance to notice existing damage
-----------------------------------------
Verification here is not ceremony. Once the source database is gone,
any referential damage it already carried becomes permanent and
undiagnosable — a grade pointing at a post that was deleted years ago
would export cleanly and simply never appear again. So the export
checks every reference it carries and refuses to finish if one dangles.
Better to fail here, while the source still exists to be inspected.

One reference deliberately leaves the set: ``evaluations.keyword_route_id``
points at ``project_keywords``, which is not exported. The column and its
values are preserved verbatim; only the referent is gone. Foreign keys are
therefore disabled on the destination — explicitly, in ``_open_destination``,
rather than by resting on SQLite's default — and the checks below name what
they verify rather than delegating to ``PRAGMA foreign_key_check``, which
would report that one dangling constraint as a failure of an otherwise
sound export.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from scout.result import Err
from scout.storage.artifacts import read_artifact_bundle
from scout.storage.db import read_only_snapshot

#: In dependency order — parents before children. Foreign keys are not
#: enforced in the destination, so this ordering buys nothing mechanically;
#: it is here because reading the list in dependency order is how you see
#: at a glance that the set is closed.
EXPORTED_TABLES: tuple[str, ...] = (
    "posts",
    "evaluations",
    "draft_comments",
    "grades",
    "grade_revisions",
    "grade_usage_overrides",
)

# Additive preservation extension. Older six-table exports remain readable;
# a half-present analysis schema is damage, not an older supported version.
ANALYSIS_TABLES = ("analysis_artifacts", "analysis_lineage")


def _analysis_tables(source: sqlite3.Connection) -> tuple[str, ...]:
    present = tuple(
        table
        for table in ANALYSIS_TABLES
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )
    if present and present != ANALYSIS_TABLES:
        raise GradingExportError("incomplete analysis artifact schema")
    return present


#: Every reference the exported set carries within itself, as
#: ``(table, column, parent_table)``. Checked after the copy against the
#: destination, so what is verified is the file that will outlive the
#: source — not the source it was read from.
_INTERNAL_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("evaluations", "post_id", "posts"),
    ("draft_comments", "post_id", "posts"),
    ("draft_comments", "evaluation_id", "evaluations"),
    ("grades", "post_id", "posts"),
    ("grades", "evaluation_id", "evaluations"),
    ("grade_revisions", "grade_id", "grades"),
    ("grade_revisions", "evaluation_id", "evaluations"),
    ("grade_usage_overrides", "grade_id", "grades"),
)


class GradingExportError(RuntimeError):
    """The export could not be completed, or could not be trusted.

    Raised rather than returned because every caller is a script whose
    only response is to report and exit nonzero — and because a
    half-verified grading export must never be mistaken for a finished
    one by a caller that forgot to check.
    """


@dataclass(frozen=True, slots=True)
class TableExport:
    name: str
    row_count: int


@dataclass(frozen=True, slots=True)
class GradingExportResult:
    destination: str
    tables: tuple[TableExport, ...]

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)


def _open_source(path: str) -> sqlite3.Connection:
    """The source database, opened so it cannot be written to.

    Two independent guards: the URI's ``mode=ro``, and ``query_only``.
    The first stops this process from opening a writable handle, the
    second stops any statement in it from writing. An export that
    modified the thing it was preserving is the one bug here genuinely
    worth fearing.

    The path is percent-encoded before it goes into the URI, and that is
    load-bearing rather than tidiness. Interpolated raw, a path holding
    ``?`` or ``#`` is split by SQLite at that character: everything after
    it becomes query parameters, ``mode=ro`` is never seen, and the
    connection silently falls back to read-write-*create* against a
    truncated path. Measured, for a real file named ``scout?v2.db``:

        raw    -> opened an empty database and created a stray "scout"
        quoted -> opened scout?v2.db

    So the failure is not a refusal to open, which would be harmless. It
    is opening the wrong database, writing a file, and voiding the first
    of the two guards above — and it happens at connect time, before
    ``query_only`` exists to stop it. ``is_file`` does not catch it
    either: a real file named ``scout?v2.db`` passes that check and then
    misparses.

    Note this is a correctness bug and not an injection one. A second
    ``mode=`` does not override the first; SQLite rejects the whole URI
    with "no such access mode".
    """
    if not Path(path).is_file():
        raise GradingExportError(f"source database not found: {path}")
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(Path(path)))}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise GradingExportError(f"cannot open {path} read-only: {exc}") from exc
    return connection


def _open_destination(path: Path) -> sqlite3.Connection:
    """The temporary destination, with foreign keys explicitly disabled.

    Off is already SQLite's default, so this changes nothing today —
    which is the point. The module documents that the destination does
    not enforce foreign keys, and one reference genuinely dangles by
    design (``evaluations.keyword_route_id`` into ``project_keywords``,
    which is not exported). Resting that on an inherited default means a
    build with the compile-time default flipped, or a future driver that
    enables it, turns a documented property into a total export failure:
    with the parent table absent, every ``evaluations`` insert would be
    rejected. Stating it makes the guarantee the module's own.
    """
    try:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
    except sqlite3.Error as exc:
        raise GradingExportError(f"cannot create export at {path}: {exc}") from exc
    return connection


def _object_ddl(source: sqlite3.Connection, table: str) -> tuple[str, tuple[str, ...]]:
    """The table's own ``CREATE`` statement, and its indexes'.

    Taken verbatim from ``sqlite_master`` rather than rebuilt. The
    export is meant to be a faithful record, and the CHECK constraints
    on these tables carry real meaning — ``grades.graded_at`` pins an
    exact 24-character UTC format, ``grade_usage_overrides`` requires a
    reason exactly when the mode is ``exclude``. Reconstructing that by
    hand would eventually lose one.
    """
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise GradingExportError(f"source database has no table {table!r}")

    indexes = source.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    return row[0], tuple(index[0] for index in indexes)


def _copy_table(source: sqlite3.Connection, destination: sqlite3.Connection, table: str) -> int:
    create_sql, index_sql = _object_ddl(source, table)
    destination.execute(create_sql)

    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    if not columns:
        raise GradingExportError(f"table {table!r} reports no columns")
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)

    rows = source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()  # noqa: S608
    destination.executemany(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        rows,  # noqa: S608
    )
    for statement in index_sql:
        destination.execute(statement)
    return len(rows)


def _verify_row_counts(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    tables: Sequence[str],
) -> None:
    for table in tables:
        expected = source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608
        actual = destination.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608
        if expected != actual:
            raise GradingExportError(f"{table}: copied {actual} rows but source holds {expected}")


def _verify_references(destination: sqlite3.Connection) -> None:
    for table, column, parent in _INTERNAL_REFERENCES:
        dangling = destination.execute(
            f'SELECT count(*) FROM "{table}" c '  # noqa: S608
            f'LEFT JOIN "{parent}" p ON p.id = c."{column}" '
            f'WHERE c."{column}" IS NOT NULL AND p.id IS NULL'
        ).fetchone()[0]
        if dangling:
            raise GradingExportError(
                f"{table}.{column} has {dangling} row(s) referencing a "
                f"{parent} that does not exist. This is pre-existing damage in "
                f"the source, not an export fault — inspect it before the source "
                f"database is discarded, because afterwards it cannot be diagnosed."
            )


def _verify_grades_are_present(destination: sqlite3.Connection) -> None:
    """An export of zero grades is almost certainly the wrong database.

    The whole operation exists to preserve human judgments. Producing an
    empty file and reporting success is the one outcome that would pass
    every other check here and still lose everything.
    """
    grades = destination.execute("SELECT count(*) FROM grades").fetchone()[0]
    if grades == 0:
        raise GradingExportError(
            "source database contains no grades — refusing to write an empty "
            "grading export. Check that the path points at the live database."
        )


def export_grading_corpus(source_db_path: str, destination_path: str) -> GradingExportResult:
    """Copy the grading corpus out of *source_db_path*, verified.

    Writes to a temporary sibling and moves it into place only once
    every check has passed, so a failure part-way through leaves no file
    that could later be mistaken for a complete export.
    """
    destination = Path(destination_path)
    tmp_path = destination.with_name(f"{destination.name}.partial")

    source = _open_source(source_db_path)
    try:
        with read_only_snapshot(source):
            analysis_tables = _analysis_tables(source)
            exported_tables = (*EXPORTED_TABLES, *analysis_tables)
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            written = _open_destination(tmp_path)
            try:
                tables = tuple(
                    TableExport(name=table, row_count=_copy_table(source, written, table))
                    for table in exported_tables
                )
                _verify_row_counts(source, written, exported_tables)
                _verify_references(written)
                _verify_grades_are_present(written)
                if analysis_tables:
                    if isinstance(read_artifact_bundle(written), Err):
                        raise GradingExportError(
                            "analysis artifacts failed preservation integrity checks"
                        )
                    for table in analysis_tables:
                        for row in source.execute(
                            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                            (table,),
                        ):
                            written.execute(row[0])
                written.commit()
            except BaseException:
                written.close()
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                raise
            written.close()
    finally:
        source.close()

    # The last step that can fail, and the one most easily left unguarded:
    # a missing parent directory, a read-only mount, or a destination on a
    # different filesystem all raise here, after every check has already
    # passed. Unwrapped it would surface as a raw OSError traceback and
    # leave .partial sitting next to the intended path — a complete,
    # verified export under a name that gives no hint it is one, which is
    # exactly the "half an export is never mistakable for a whole one"
    # property the temporary file exists to provide.
    try:
        os.replace(tmp_path, destination)
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise GradingExportError(
            f"export verified but could not be moved into place at {destination}: {exc}"
        ) from exc

    return GradingExportResult(destination=str(destination), tables=tables)


__all__ = [
    "EXPORTED_TABLES",
    "GradingExportError",
    "GradingExportResult",
    "TableExport",
    "export_grading_corpus",
]
