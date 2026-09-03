"""The grading export runs once, against a database about to be deleted.

Which makes its failure modes unusual: there is no second attempt, and
no way to notice afterwards that it took less than it should have. So
the tests below lean on the silent losses — an export that succeeds and
carries nothing useful — rather than on the loud ones.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scout.grading.corpus_export import (
    EXPORTED_TABLES,
    GradingExportError,
    export_grading_corpus,
)
from scout.storage.state import StateManager

_GRADED_AT = "2026-08-01T12:00:00.000Z"


def test_direct_script_invocation_loads_project_modules() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/export_grading_corpus.py", "--help"],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Export the grading corpus" in result.stdout


def _seed(db_path: Path) -> None:
    """A minimal but referentially complete grading corpus."""
    state = StateManager(db_path=str(db_path))
    db = state.db
    db.execute(
        "INSERT INTO posts (id, platform, platform_msg_id, content) "
        "VALUES (1, 'discord', 'msg-1', 'a post that was graded')"
    )
    db.execute(
        "INSERT INTO evaluations (id, post_id, relevant, score, surface_status) "
        "VALUES (10, 1, 1, 0.9, 'surfaced')"
    )
    db.execute(
        "INSERT INTO draft_comments (id, post_id, evaluation_id, comment_text) "
        "VALUES (100, 1, 10, 'the drafted reply')"
    )
    db.execute(
        "INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, "
        "relevance_judgment, comment_quality) "
        f"VALUES (1000, 10, 1, 'cli', '{_GRADED_AT}', 'correct', 4)"
    )
    db.execute(
        "INSERT INTO grade_revisions (id, grade_id, evaluation_id, revision, "
        "source, payload, recorded_at) "
        f"VALUES (1, 1000, 10, 1, 'cli', '{{}}', '{_GRADED_AT}')"
    )
    db.execute(
        "INSERT INTO grade_usage_overrides (id, grade_id, mode, reason, updated_at) "
        f"VALUES (1, 1000, 'exclude', 'superseded', '{_GRADED_AT}')"
    )
    state.commit()
    state.close()


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "scout.db"
    _seed(path)
    return path


class TestTheExportCarriesTheCorpus:
    def test_every_declared_table_is_present(self, source_db: Path, tmp_path: Path) -> None:
        destination = tmp_path / "scout.grading.db"
        export_grading_corpus(str(source_db), str(destination))

        with sqlite3.connect(destination) as out:
            names = {
                row[0]
                for row in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert set(EXPORTED_TABLES) <= names

    def test_the_feedback_loop_query_still_returns_the_grade(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # The claim the whole six-table set exists to make. This is the
        # join evaluation_feedback runs, and against a three-table export
        # it returns zero rows while every grade is still technically
        # present.
        destination = tmp_path / "scout.grading.db"
        export_grading_corpus(str(source_db), str(destination))

        with sqlite3.connect(destination) as out:
            rows = out.execute(
                "SELECT g.id, p.content, e.score, dc.comment_text, o.mode "
                "FROM grades g "
                "JOIN posts p ON p.id = g.post_id "
                "LEFT JOIN evaluations e ON e.id = g.evaluation_id "
                "LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id "
                "LEFT JOIN grade_usage_overrides o ON o.grade_id = g.id"
            ).fetchall()

        assert rows == [(1000, "a post that was graded", 0.9, "the drafted reply", "exclude")]

    def test_check_constraints_survive_the_copy(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # The schema is copied verbatim from sqlite_master rather than
        # rebuilt, so the constraints that encode real rules come with
        # it. grade_usage_overrides requires a reason exactly when the
        # mode is 'exclude'.
        destination = tmp_path / "scout.grading.db"
        export_grading_corpus(str(source_db), str(destination))

        with sqlite3.connect(destination) as out, pytest.raises(sqlite3.IntegrityError):
            out.execute(
                "INSERT INTO grade_usage_overrides (grade_id, mode, reason, updated_at) "
                f"VALUES (1000, 'exclude', NULL, '{_GRADED_AT}')"
            )

    def test_the_source_is_not_modified(self, source_db: Path, tmp_path: Path) -> None:
        before = source_db.read_bytes()
        export_grading_corpus(str(source_db), str(tmp_path / "scout.grading.db"))
        assert source_db.read_bytes() == before

    def test_v3_reply_revision_id_value_survives_generic_column_copy(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # grades.reply_revision_id is copied like any other grades column —
        # _copy_table selects every column generically, so the new nullable
        # v3 pointer needs no export-side handling to survive as a value.
        with sqlite3.connect(source_db) as db:
            db.execute(
                "INSERT INTO reply_draft_revisions "
                "(id, draft_comment_id, version, reply_text, source, created_at) "
                f"VALUES (1, 100, 1, 'corrected reply', 'cli', '{_GRADED_AT}')"
            )
            db.execute(
                "INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, "
                "relevance_judgment, action_judgment, schema_version, reply_revision_id) "
                f"VALUES (3000, NULL, 1, 'cli', '{_GRADED_AT}', 'false_positive', "
                "'fail', 3, 1)"
            )

        destination = tmp_path / "scout.grading.db"
        export_grading_corpus(str(source_db), str(destination))

        with sqlite3.connect(destination) as out:
            row = out.execute(
                "SELECT reply_revision_id FROM grades WHERE id = 3000"
            ).fetchone()
            assert row == (1,)

            # Known limitation, not asserted as a bug here: reply_draft_
            # revisions is not one of the six exported tables (see
            # EXPORTED_TABLES), so the corrected reply text this pointer
            # addresses does not travel with the export — only the
            # integer id does. See known_issues for this cohort.
            tables = {
                r[0]
                for r in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "reply_draft_revisions" not in tables

    def test_v1_v2_grades_without_reply_revision_id_still_export(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # Historical grades predate reply_revision_id entirely (schema_version
        # 1/2) and always carry it NULL — the export must not require or
        # special-case the column for them.
        with sqlite3.connect(source_db) as db:
            db.execute(
                "INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, "
                "relevance_judgment, schema_version) "
                f"VALUES (3001, NULL, 1, 'migration', '{_GRADED_AT}', 'correct', 1)"
            )

        destination = tmp_path / "scout.grading.db"
        export_grading_corpus(str(source_db), str(destination))

        with sqlite3.connect(destination) as out:
            row = out.execute(
                "SELECT reply_revision_id, schema_version FROM grades WHERE id = 3001"
            ).fetchone()
        assert row == (None, 1)


class TestItRefusesToSucceedQuietly:
    def test_an_empty_corpus_is_refused(self, tmp_path: Path) -> None:
        # Pointing at the wrong path is the likely mistake, and it
        # produces a perfectly valid export of nothing.
        empty = tmp_path / "empty.db"
        StateManager(db_path=str(empty)).close()

        with pytest.raises(GradingExportError, match="no grades"):
            export_grading_corpus(str(empty), str(tmp_path / "scout.grading.db"))

    def test_pre_existing_dangling_references_are_reported(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # A grade whose post was deleted would export cleanly and simply
        # never appear again. Once the source is gone that is
        # undiagnosable, so it has to fail here.
        with sqlite3.connect(source_db) as db:
            db.execute(
                "INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, "
                f"relevance_judgment) VALUES (2000, NULL, 4242, 'cli', '{_GRADED_AT}', "
                "'false_positive')"
            )

        with pytest.raises(GradingExportError, match=r"grades\.post_id.*posts"):
            export_grading_corpus(str(source_db), str(tmp_path / "scout.grading.db"))

    def test_a_failed_export_leaves_no_file_behind(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # Half an export must never be mistakable for a whole one.
        with sqlite3.connect(source_db) as db:
            db.execute(
                "INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, "
                f"relevance_judgment) VALUES (2000, NULL, 4242, 'cli', '{_GRADED_AT}', "
                "'false_positive')"
            )

        destination = tmp_path / "scout.grading.db"
        with pytest.raises(GradingExportError):
            export_grading_corpus(str(source_db), str(destination))

        assert not destination.exists()
        assert not destination.with_name(f"{destination.name}.partial").exists()

    def test_a_missing_source_is_named(self, tmp_path: Path) -> None:
        with pytest.raises(GradingExportError, match="not found"):
            export_grading_corpus(str(tmp_path / "nope.db"), str(tmp_path / "out.db"))

    def test_an_unwritable_destination_is_named_not_raised_raw(
        self, source_db: Path, tmp_path: Path,
    ) -> None:
        # Every caller is a script that catches GradingExportError and
        # nothing else, so a raw sqlite3.Error here reaches the operator
        # as a traceback instead of a sentence.
        with pytest.raises(GradingExportError, match="cannot create export"):
            export_grading_corpus(str(source_db), str(tmp_path / "no" / "such" / "dir.db"))

    def test_a_failed_move_into_place_leaves_nothing_behind(
        self, source_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The move is the last thing that can fail, and it fails after
        # every check has passed — so without this, a fully verified
        # export is left sitting under the .partial name, which is the
        # one state the temporary file exists to prevent.
        def _boom(src: object, dst: object) -> None:
            raise OSError("cross-device link")

        monkeypatch.setattr("scout.grading.corpus_export.os.replace", _boom)
        destination = tmp_path / "scout.grading.db"

        with pytest.raises(GradingExportError, match="could not be moved into place"):
            export_grading_corpus(str(source_db), str(destination))

        assert not destination.exists()
        assert not destination.with_name(f"{destination.name}.partial").exists()


class TestPathsThatBreakTheUri:
    """A source path holding a URI-reserved character.

    Interpolated raw, SQLite splits the URI at the ``?``, never sees
    ``mode=ro``, and opens read-write-create against the truncated path —
    so it silently reads an empty database and writes a stray file, in a
    tool whose whole contract is that it does not write.
    """

    def test_a_question_mark_in_the_path_still_reads_the_real_database(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "scout?v2.db"
        _seed(source)
        destination = tmp_path / "scout.grading.db"

        result = export_grading_corpus(str(source), str(destination))

        assert dict((t.name, t.row_count) for t in result.tables)["grades"] == 1

    def test_no_stray_file_is_created_beside_it(self, tmp_path: Path) -> None:
        # The truncation lands at "scout" — the empty database the raw
        # form created and then reported on.
        source = tmp_path / "scout?v2.db"
        _seed(source)
        export_grading_corpus(str(source), str(tmp_path / "scout.grading.db"))

        assert not (tmp_path / "scout").exists()
