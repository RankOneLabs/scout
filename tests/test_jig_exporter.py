"""Tests for the effectful finalized-grade -> Jig database rebuild."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from jig import FeedbackQuery, Score, ScoreSource, SQLiteFeedbackLoop

import scout.evals.phase1.jig_exporter as jig_exporter
from scout.config import GradeRecord, Message, RelevanceResult
from scout.evals.phase1.export_adapter import CANONICAL_FAILURE_DIMENSIONS
from scout.evals.phase1.jig_exporter import (
    JigRebuildError,
    rebuild_finalized_grades_to_jig,
)
from scout.storage.state import StateManager


class _FakeEmbedFeedbackLoop(SQLiteFeedbackLoop):
    """Real SQLiteFeedbackLoop with the network-calling embedding step
    swapped for a deterministic in-memory vector, so tests exercise the
    genuine schema/query/export_eval_set behavior without a live Ollama."""

    async def _embed(self, text: str) -> np.ndarray:
        return np.ones(8, dtype=np.float32)


async def _fake_embedding_provider(text: str) -> np.ndarray:
    return np.ones(8, dtype=np.float32)


def _rebuild_kwargs() -> dict:
    return {
        "embedding_provider": _fake_embedding_provider,
        "feedback_loop_factory": lambda path: _FakeEmbedFeedbackLoop(db_path=path),
    }


def _seed_message(state: StateManager, platform_id: str) -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="ch-1",
        author_name="alice",
        author_id="u1",
        content="how do I configure the gateway?",
        created_at=datetime.now(UTC),
    )


def _seed_surfaced_correct_grade(state: StateManager, *, with_draft: bool = True) -> int:
    """A relevant, surfaced, correctly-judged grade — complete dossier
    identity, replay-ready."""
    scan_id = state.start_scan()
    msg = _seed_message(state, f"surfaced-{scan_id}")
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=True, score=0.9, reason="matches project", relevant_to=("gateway",)
    )
    evaluation_id = state.save_evaluation(
        result,
        post_id,
        scan_id,
        project_key="gateway",
        posture="answer",
        surface_status="surfaced",
        dossier_revision="a" * 40,
        dossier_summary_id="gateway-dossier",
    )
    if with_draft:
        state.save_draft(
            post_id,
            evaluation_id,
            "gateway",
            "Here's how to configure the gateway.",
            scan_id,
            posture="answer",
            dossier_revision="a" * 40,
            dossier_summary_id="gateway-dossier",
        )
    state.complete_scan(scan_id, 1, 1)
    state.save_grade(GradeRecord(
        post_id=post_id,
        evaluation_id=evaluation_id,
        scan_id=scan_id,
        source="cli",
        graded_at=datetime.now(UTC),
        relevance_judgment="correct",
        action_judgment="accept",
    ))
    state.commit()
    return evaluation_id


def _seed_false_positive_grade(state: StateManager) -> int:
    """A not-relevant correction — no dossier identity needed."""
    scan_id = state.start_scan()
    msg = _seed_message(state, f"fp-{scan_id}")
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=True, score=0.6, reason="looked relevant", relevant_to=("gateway",)
    )
    evaluation_id = state.save_evaluation(
        result, post_id, scan_id, surface_status="not_relevant",
    )
    state.complete_scan(scan_id, 1, 1)
    state.save_grade(GradeRecord(
        post_id=post_id,
        evaluation_id=evaluation_id,
        scan_id=scan_id,
        source="cli",
        graded_at=datetime.now(UTC),
        relevance_judgment="false_positive",
        action_judgment="fail",
        dimensions=["tone"],
        failure_note="This message was not relevant.",
    ))
    state.commit()
    return evaluation_id


def _seed_false_negative_missing_identity_grade(state: StateManager) -> int:
    """A false-negative correction with no counterfactual identity — the
    replay-unready branch."""
    scan_id = state.start_scan()
    msg = _seed_message(state, f"fn-{scan_id}")
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=False, score=0.2, reason="looked irrelevant",
    )
    evaluation_id = state.save_evaluation(
        result, post_id, scan_id, surface_status="not_relevant",
    )
    state.complete_scan(scan_id, 1, 1)
    state.save_grade(GradeRecord(
        post_id=post_id,
        evaluation_id=evaluation_id,
        scan_id=scan_id,
        source="cli",
        graded_at=datetime.now(UTC),
        relevance_judgment="false_negative",
        action_judgment="fail",
        dimensions=["contextual_understanding"],
        failure_note="should have been surfaced",
        context_missing_input="the original post text",
    ))
    state.commit()
    return evaluation_id


def _seed_malformed_grade(state: StateManager) -> int:
    """Directly writes a grade row that violates the shared grading
    contract (action_judgment=fail with no dimensions/failure_note) —
    the grades table itself has no CHECK constraint enforcing this, so a
    malformed historical row is reachable without bypassing state_manager.
    """
    scan_id = state.start_scan()
    msg = _seed_message(state, f"malformed-{scan_id}")
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(message=msg, relevant=True, score=0.5, reason="r")
    evaluation_id = state.save_evaluation(
        result, post_id, scan_id, surface_status="not_relevant",
    )
    state.complete_scan(scan_id, 1, 1)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(UTC).microsecond // 1000:03d}Z"
    )
    state.conn.execute(
        "INSERT INTO grades "
        "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
        "schema_version, needs_regrade, action_judgment, dimensions, failure_note) "
        "VALUES (?, ?, ?, 'cli', ?, 'false_positive', 3, 0, 'fail', NULL, NULL)",
        (evaluation_id, post_id, scan_id, now),
    )
    state.commit()
    return evaluation_id


class TestRebuildHappyPath:
    async def test_rebuild_produces_n_results_and_canonical_scores(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"

        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        _seed_false_positive_grade(state)
        state.close()

        result = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )

        assert result.result_count == 2
        assert result.score_count == 2 * len(CANONICAL_FAILURE_DIMENSIONS)
        assert jig_db.exists()

        feedback = SQLiteFeedbackLoop(db_path=str(jig_db))
        try:
            stored = await feedback.query(FeedbackQuery(limit=10))
            assert len(stored) == 2
            cases = await feedback.export_eval_set()
            assert len(cases) == 2
            for case in cases:
                dims = [s["dimension"] for s in case.metadata["scores"]]
                assert dims == list(CANONICAL_FAILURE_DIMENSIONS)
        finally:
            await feedback.close()

    async def test_no_leftover_temp_files_after_success(self, tmp_path: Path) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()

        await rebuild_finalized_grades_to_jig(str(scout_db), str(jig_db), **_rebuild_kwargs())

        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_repeated_rebuild_replaces_and_yields_fresh_counts(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()

        first = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        assert first.result_count == 1

        state = StateManager(str(scout_db))
        _seed_false_positive_grade(state)
        state.close()

        second = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        assert second.result_count == 2
        assert second.score_count == 2 * len(CANONICAL_FAILURE_DIMENSIONS)

    async def test_replay_unready_false_negative_included_and_replay_ready(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_false_negative_missing_identity_grade(state)
        state.close()

        result = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        assert result.result_count == 1

        feedback = SQLiteFeedbackLoop(db_path=str(jig_db))
        try:
            cases = await feedback.export_eval_set()
            assert len(cases) == 1
            score_meta = cases[0].metadata["scores"][0]["metadata"]
            assert score_meta["evaluation_id"] is not None
        finally:
            await feedback.close()


class TestVerificationScale:
    async def test_exact_export_verification_is_not_limited_by_query_search_window(
        self, tmp_path: Path
    ) -> None:
        """Jig query() intentionally considers at most 900 search candidates.

        Exact rebuild verification comes from export_eval_set(), so a valid
        database larger than that search window must still verify in full.
        """
        result_count = 901
        feedback = _FakeEmbedFeedbackLoop(db_path=str(tmp_path / "large-jig.db"))
        try:
            for evaluation_id in range(result_count):
                result_id = await feedback.store_result(
                    "actual output",
                    f"input {evaluation_id}",
                    {"evaluation_id": evaluation_id},
                )
                await feedback.score(
                    result_id,
                    [
                        Score(
                            dimension=dimension,
                            value=1.0,
                            source=ScoreSource.HUMAN,
                            metadata={"evaluation_id": evaluation_id},
                        )
                        for dimension in CANONICAL_FAILURE_DIMENSIONS
                    ],
                )

            score_count = await jig_exporter._verify_rebuilt_database(
                feedback, expected_result_count=result_count
            )
        finally:
            await feedback.close()

        assert score_count == len(CANONICAL_FAILURE_DIMENSIONS) * result_count


class TestRebuildFailuresPreserveDestination:
    async def test_malformed_grade_rejects_without_touching_prior_destination(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"

        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()
        good = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        assert good.result_count == 1
        prior_bytes = jig_db.read_bytes()

        state = StateManager(str(scout_db))
        _seed_malformed_grade(state)
        state.close()

        with pytest.raises(JigRebuildError, match="invalid grade"):
            await rebuild_finalized_grades_to_jig(str(scout_db), str(jig_db), **_rebuild_kwargs())

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_non_json_projection_rejects_before_touching_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()

        await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        prior_bytes = jig_db.read_bytes()
        original_project = jig_exporter.project_exported_grades

        def project_with_non_json_metadata(records):
            projections = original_project(records)
            metadata = dict(projections[0].result_metadata)
            metadata["not_json_serializable"] = object()
            return [replace(projections[0], result_metadata=metadata)]

        monkeypatch.setattr(
            jig_exporter, "project_exported_grades", project_with_non_json_metadata
        )

        with pytest.raises(JigRebuildError, match="result metadata cannot be JSON-serialized"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db), str(jig_db), **_rebuild_kwargs()
            )

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_embedding_provider_failure_leaves_destination_untouched(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()
        good = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        prior_bytes = jig_db.read_bytes()
        assert good.result_count == 1

        async def _broken_provider(text: str) -> np.ndarray:
            raise RuntimeError("ollama unreachable")

        with pytest.raises(JigRebuildError, match="preflight failed"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_broken_provider,
                feedback_loop_factory=lambda path: _FakeEmbedFeedbackLoop(db_path=path),
            )

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_non_finite_embedding_vector_rejected_at_preflight(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()

        async def _nan_provider(text: str) -> np.ndarray:
            return np.array([float("nan")], dtype=np.float32)

        with pytest.raises(JigRebuildError, match="non-finite"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_nan_provider,
                feedback_loop_factory=lambda path: _FakeEmbedFeedbackLoop(db_path=path),
            )
        assert not jig_db.exists()
        assert list(tmp_path.iterdir()) == [scout_db]

    async def test_non_numeric_embedding_vector_wrapped_as_jig_rebuild_error(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()

        async def _non_numeric_provider(text: str) -> list:
            return ["not", "numeric"]

        with pytest.raises(JigRebuildError, match="preflight failed"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_non_numeric_provider,
                feedback_loop_factory=lambda path: _FakeEmbedFeedbackLoop(db_path=path),
            )
        assert not jig_db.exists()
        assert list(tmp_path.iterdir()) == [scout_db]

    async def test_store_result_failure_cleans_up_temp_and_preserves_destination(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        _seed_false_positive_grade(state)
        state.close()
        good = await rebuild_finalized_grades_to_jig(
            str(scout_db), str(jig_db), **_rebuild_kwargs()
        )
        prior_bytes = jig_db.read_bytes()
        assert good.result_count == 2

        class _BrokenStoreFeedbackLoop(_FakeEmbedFeedbackLoop):
            _calls = 0

            async def store_result(self, content, input_text, metadata=None):  # type: ignore[override]
                type(self)._calls += 1
                if type(self)._calls == 2:
                    raise RuntimeError("simulated store failure")
                return await super().store_result(content, input_text, metadata)

        with pytest.raises(RuntimeError, match="simulated store failure"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_fake_embedding_provider,
                feedback_loop_factory=lambda path: _BrokenStoreFeedbackLoop(db_path=path),
            )

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_keyboard_interrupt_during_write_preserves_destination(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()
        await rebuild_finalized_grades_to_jig(str(scout_db), str(jig_db), **_rebuild_kwargs())
        prior_bytes = jig_db.read_bytes()

        state = StateManager(str(scout_db))
        _seed_false_positive_grade(state)
        state.close()

        class _InterruptedFeedbackLoop(_FakeEmbedFeedbackLoop):
            async def score(self, result_id, scores):  # type: ignore[override]
                raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_fake_embedding_provider,
                feedback_loop_factory=lambda path: _InterruptedFeedbackLoop(db_path=path),
            )

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]

    async def test_verification_failure_cleans_up_and_preserves_destination(
        self, tmp_path: Path
    ) -> None:
        scout_db = tmp_path / "scout.db"
        jig_db = tmp_path / "jig.db"
        state = StateManager(str(scout_db))
        _seed_surfaced_correct_grade(state)
        state.close()
        await rebuild_finalized_grades_to_jig(str(scout_db), str(jig_db), **_rebuild_kwargs())
        prior_bytes = jig_db.read_bytes()

        state = StateManager(str(scout_db))
        _seed_false_positive_grade(state)
        state.close()

        class _DroppingScoreFeedbackLoop(_FakeEmbedFeedbackLoop):
            async def score(self, result_id, scores):  # type: ignore[override]
                # Drop one score to desync the 6-per-result invariant.
                await super().score(result_id, scores[:-1])

        with pytest.raises(JigRebuildError, match="verification failed"):
            await rebuild_finalized_grades_to_jig(
                str(scout_db),
                str(jig_db),
                embedding_provider=_fake_embedding_provider,
                feedback_loop_factory=lambda path: _DroppingScoreFeedbackLoop(db_path=path),
            )

        assert jig_db.read_bytes() == prior_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["jig.db", "scout.db"]
