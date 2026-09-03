"""Tests for the FastAPI grading sidecar.

Uses FastAPI's TestClient over a tmp_path scout.db. Each test
monkeypatches `grading_api_sidecar.DB_PATH` to a per-test path, seeds
state via the existing Python entry points, and verifies that each
handler delegates correctly and surfaces validation failures as 400.

Schema DDL now runs once in the app's lifespan bootstrap rather than on
every request — Starlette's TestClient only fires ASGI lifespan events
when it's entered as a context manager, so every client in this file is
built through `make_client`, which does that entering (and matching
exit) for you.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import scout.cli.grading_api as grading_api_sidecar
from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.state import StateManager, format_graded_at, parse_graded_at

# A well-formed grade body for middleware probes. Evaluation 1 never exists
# in those tests, so a request that clears token/Host/Origin checks reaches
# the handler and 404s — distinguishable from the middleware's 401/403.
_PROBE_GRADE = {"relevance_judgment": "correct", "action_judgment": "accept"}


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    """Seed a fresh scout.db at tmp_path and point the sidecar at it."""
    path = str(tmp_path / "scout.db")
    monkeypatch.setattr(grading_api_sidecar, "DB_PATH", path)
    return path


@pytest.fixture
def make_client(request, db_path: str) -> Callable[..., TestClient]:
    """Build a TestClient with its ASGI lifespan actually run.

    Depending on `db_path` (rather than relying on each test's own
    parameter order) guarantees DB_PATH is monkeypatched before the
    lifespan bootstrap ever opens a connection. Every produced client is
    entered immediately so lifespan.startup runs now; its __exit__ (and
    lifespan.shutdown) is deferred to a finalizer so tests can construct
    several clients without nesting `with` blocks.
    """
    stack = contextlib.ExitStack()
    request.addfinalizer(stack.close)

    def _make(**kwargs) -> TestClient:
        return stack.enter_context(TestClient(grading_api_sidecar.app, **kwargs))

    return _make


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    # Default Host: localhost so requests pass Host validation for the
    # default loopback bind without per-test boilerplate.
    return make_client(headers={"Host": "localhost"})


class TestStartupConfig:
    def test_empty_token_fails_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "")
        with pytest.raises(SystemExit):
            grading_api_sidecar._validate_startup_config("127.0.0.1")

    def test_whitespace_token_fails_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "   ")
        with pytest.raises(SystemExit):
            grading_api_sidecar._validate_startup_config("127.0.0.1")

    def test_non_loopback_without_token_fails_startup(self, monkeypatch) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            grading_api_sidecar._validate_startup_config("0.0.0.0")

    def test_loopback_without_token_succeeds(self, monkeypatch) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        grading_api_sidecar._validate_startup_config("127.0.0.1")

    def test_loopback_localhost_without_token_succeeds(self, monkeypatch) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        grading_api_sidecar._validate_startup_config("localhost")

    def test_non_loopback_with_token_succeeds(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        grading_api_sidecar._validate_startup_config("0.0.0.0")

    def test_empty_trusted_host_fails_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TRUSTED_HOST", "")
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            grading_api_sidecar._validate_startup_config("127.0.0.1")


class TestAuthMiddleware:
    def test_missing_token_rejected_when_secret_configured(
        self, db_path: str, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        resp = client.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 401

    def test_correct_token_accepted(
        self, db_path: str, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        resp = client.post(
            "/grades/1",
            json=_PROBE_GRADE,
            headers={"X-Scout-Sidecar-Token": "s3cret"},
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_invalid_token_rejected(
        self, db_path: str, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        resp = client.post(
            "/grades/1",
            json=_PROBE_GRADE,
            headers={"X-Scout-Sidecar-Token": "wrong"},
        )
        assert resp.status_code == 401

    def test_healthz_open(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestHostValidation:
    def test_loopback_ip_host_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(headers={"Host": "127.0.0.1"})
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_localhost_host_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(headers={"Host": "localhost"})
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_configured_host_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "sidecar.internal")
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "tok")
        c = make_client(
            headers={"Host": "sidecar.internal", "X-Scout-Sidecar-Token": "tok"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_unexpected_host_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(headers={"Host": "evil.example.com"})
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403
        assert "Host" in resp.json()["detail"]

    def test_missing_host_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        # httpx always sends Host; override with empty string to simulate absence.
        c = make_client(headers={"Host": ""})
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403

    def test_wildcard_bind_trusts_loopback_only(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "0.0.0.0")
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "tok")
        monkeypatch.delenv("SCOUT_SIDECAR_TRUSTED_HOST", raising=False)
        c = make_client(
            headers={"Host": "localhost", "X-Scout-Sidecar-Token": "tok"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_wildcard_bind_non_loopback_host_rejected_without_trusted_host(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "0.0.0.0")
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "tok")
        monkeypatch.delenv("SCOUT_SIDECAR_TRUSTED_HOST", raising=False)
        c = make_client(
            headers={"Host": "sidecar.internal", "X-Scout-Sidecar-Token": "tok"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403

    def test_trusted_host_env_var_allows_named_host(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "0.0.0.0")
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "tok")
        monkeypatch.setenv("SCOUT_SIDECAR_TRUSTED_HOST", "sidecar.internal")
        c = make_client(
            headers={"Host": "sidecar.internal", "X-Scout-Sidecar-Token": "tok"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist


class TestOriginRefererValidation:
    def test_trusted_origin_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Origin": "http://localhost:3000"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_trusted_ipv6_origin_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Origin": "http://[::1]:3000"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_untrusted_origin_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Origin": "http://evil.example.com"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403
        assert "Origin" in resp.json()["detail"]

    def test_trusted_referer_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Referer": "http://localhost:3000/review"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_trusted_ipv6_referer_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Referer": "http://[::1]:3000/review"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_untrusted_referer_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={"Host": "localhost", "Referer": "http://evil.example.com/attack"},
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403
        assert "Origin" in resp.json()["detail"]

    def test_untrusted_referer_rejected_when_origin_is_trusted(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={
                "Host": "localhost",
                "Origin": "http://localhost:3000",
                "Referer": "http://evil.example.com/attack",
            },
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403
        assert "Origin" in resp.json()["detail"]

    def test_absent_origin_and_referer_allowed(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        # No Origin or Referer — simulates a non-browser client.
        c = make_client(headers={"Host": "localhost"})
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 404  # cleared the middleware; evaluation 1 does not exist

    def test_userinfo_attack_in_origin_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        # Regression: http://localhost:3000@evil.example.com crafts a URL where
        # naive string splitting on "://" then "/" extracts "localhost" as the
        # host. urlparse.hostname correctly returns "evil.example.com".
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={
                "Host": "localhost",
                "Origin": "http://localhost:3000@evil.example.com",
            },
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403

    def test_userinfo_attack_in_referer_rejected(
        self, db_path: str, make_client: Callable[..., TestClient], monkeypatch
    ) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        c = make_client(
            headers={
                "Host": "localhost",
                "Referer": "http://localhost@evil.example.com/steal",
            },
        )
        resp = c.post(
            "/grades/1",
            json=_PROBE_GRADE,
        )
        assert resp.status_code == 403


def _seed_evaluation(
    db_path: str,
    *,
    platform_id: str = "grade-1",
    posture: str | None = "ask",
    relevant: bool = True,
) -> tuple[int, int, int]:
    """Seed a scan/post/evaluation triple and return (scan_id, post_id, eval_id)."""
    with StateManager(db_path=db_path) as state:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="user-1",
            content="test post",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=relevant, score=0.9,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id, posture=posture)
        state.commit()
    return scan_id, post_id, eval_id


class TestGrade:
    def test_happy_path(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluation_id"] == eval_id
        assert body["source"] == "web"
        assert body["schema_version"] == 3
        assert body["needs_regrade"] == 0
        # The stored 0/1, not a JSON boolean: web/types/schema.ts declares
        # needs_regrade a number, and False == 0 would let a bool slip past
        # the assertion above.
        assert type(body["needs_regrade"]) is int
        assert body["relevance_judgment"] == "correct"
        assert body["action_judgment"] == "accept"
        assert isinstance(body["id"], int)
        # graded_at must be the canonical, server-derived instant.
        assert format_graded_at(parse_graded_at(body["graded_at"])) == body["graded_at"]

    def test_false_negative_promotion_returns_source_grade_and_target(
        self,
        db_path: str,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import scout.grading.promotion as negative_case_promotion
        from scout.grading.promotion import NegativeCasePromotionResult

        _scan_id, _post_id, eval_id = _seed_evaluation(
            db_path, platform_id="negative-grade", posture=None, relevant=False
        )

        async def _promote(**kwargs):  # type: ignore[no-untyped-def]
            grade_id = kwargs["state"].save_grade(kwargs["grade"])
            return NegativeCasePromotionResult(
                source_grade_id=grade_id,
                scan_id=22,
                target_evaluation_id=33,
                surface_status="surfaced",
            )

        monkeypatch.setattr(negative_case_promotion, "promote_negative_case", _promote)
        resp = client.post(
            f"/grades/{eval_id}/promote",
            json={
                "relevance_judgment": "false_negative",
                "action_judgment": "fail",
                "dimensions": ["usefulness"],
                "failure_note": "Scout should have surfaced this",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluation_id"] == eval_id
        assert body["relevance_judgment"] == "false_negative"
        assert body["promotion"] == {
            "scan_id": 22,
            "target_evaluation_id": 33,
            "surface_status": "surfaced",
            "already_completed": False,
        }

    def test_missing_evaluation_404(self, db_path: str, client: TestClient) -> None:
        StateManager(db_path=db_path).close()
        resp = client.post(
            "/grades/9999",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        assert resp.status_code == 404

    def test_validation_error_maps_to_400_with_errors_array_and_writes_nothing(
        self, db_path: str, client: TestClient
    ) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        # A fail judgment with no dimensions/failure_note fails causal validation.
        resp = client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "fail"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)
        assert len(body["errors"]) > 0

        with StateManager(db_path=db_path) as state:
            count = state.conn.execute(
                "SELECT COUNT(*) FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchone()[0]
        assert count == 0

    def test_client_cannot_override_server_controlled_fields(
        self, db_path: str, client: TestClient
    ) -> None:
        """Provenance/timing/version/identity fields were never part of the
        client-settable grade contract. The shared schema's
        additionalProperties: false on gradePayload now rejects a request
        that includes them outright (400) rather than silently discarding
        them and writing server-derived values — an explicit contract
        violation beats a quietly-ignored one."""
        _scan_id, post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={
                "relevance_judgment": "correct",
                "action_judgment": "accept",
                "source": "cli",
                "graded_at": "2020-01-01T00:00:00.000Z",
                "schema_version": 1,
                "needs_regrade": True,
                "post_id": post_id + 999,
                "scan_id": 999,
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)
        assert len(body["errors"]) > 0

        with StateManager(db_path=db_path) as state:
            count = state.conn.execute(
                "SELECT COUNT(*) FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchone()[0]
        assert count == 0

    def test_legacy_evaluation_id_null_adoption(
        self, db_path: str, client: TestClient
    ) -> None:
        """Moving web writes to the sidecar must not regress legacy adoption:
        a pre-migration grade row with evaluation_id NULL for the same
        (post_id, scan_id) gets adopted by id rather than duplicated."""
        scan_id, post_id, eval_id = _seed_evaluation(db_path)
        with StateManager(db_path=db_path) as state:
            legacy_id = state.save_grade_for_migration(
                GradeRecord(
                    post_id=post_id,
                    scan_id=scan_id,
                    source="migration",
                    graded_at=datetime(2025, 1, 1, tzinfo=UTC),
                    relevance_judgment="correct",
                    schema_version=1,
                ),
                migration_reason="v1 backfill",
            )
            state.commit()

        resp = client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == legacy_id
        assert body["evaluation_id"] == eval_id
        assert format_graded_at(parse_graded_at(body["graded_at"])) == body["graded_at"]

        with StateManager(db_path=db_path) as state:
            count = state.conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0]
        assert count == 1


class TestGradeAdapterEditedTextAndRevisionId:
    """Sidecar-adapter-specific coverage for the schema-v3 edit path: the
    unfiltered JSON body must carry edited_text losslessly into the
    persisted reply_draft_revisions row, the response must surface the
    server-assigned reply_revision_id (or null when there was no edit),
    unknown fields must be rejected rather than silently dropped, and the
    draftless-edit rejection at StateManager's persistence boundary must
    apply identically on the ordinary and promotion routes with zero
    partial writes."""

    def _seed_evaluation_with_draft(
        self, db_path: str, *, platform_id: str = "grade-edit-1"
    ) -> tuple[int, int, int]:
        scan_id, post_id, eval_id = _seed_evaluation(db_path, platform_id=platform_id)
        with StateManager(db_path=db_path) as state:
            state.save_draft(
                post_id=post_id,
                evaluation_id=eval_id,
                project_key="gateway",
                comment_text="original reply",
                scan_id=scan_id,
            )
            state.commit()
        return scan_id, post_id, eval_id

    def test_edited_text_persists_and_response_carries_reply_revision_id(
        self, db_path: str, client: TestClient
    ) -> None:
        _scan_id, _post_id, eval_id = self._seed_evaluation_with_draft(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={
                "relevance_judgment": "correct",
                "action_judgment": "fail",
                "dimensions": ["tone"],
                "failure_note": "too casual",
                "edited_text": "a corrected, more formal reply",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply_revision_id"] is not None
        assert body["edited_text"] == "a corrected, more formal reply"

        with StateManager(db_path=db_path) as state:
            rev = state.conn.execute(
                "SELECT reply_text FROM reply_draft_revisions WHERE id = ?",
                (body["reply_revision_id"],),
            ).fetchone()
        assert rev is not None
        assert rev["reply_text"] == "a corrected, more formal reply"

    def test_no_edit_grade_leaves_reply_revision_id_null_in_response(
        self, db_path: str, client: TestClient
    ) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        assert resp.status_code == 200
        assert resp.json()["reply_revision_id"] is None

    def test_edited_text_without_draft_rejected_with_no_partial_write(
        self, db_path: str, client: TestClient
    ) -> None:
        """edited_text requires a resolvable draft_comments row — the
        shared envelope schema alone cannot see draft existence, so this
        is StateManager's persistence-time check, not client-side
        validation. Must reject with zero rows written anywhere."""
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={
                "relevance_judgment": "correct",
                "action_judgment": "fail",
                "dimensions": ["tone"],
                "failure_note": "too casual",
                "edited_text": "nothing to attach this to",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)
        assert any("draft_comments" in e for e in body["errors"])

        with StateManager(db_path=db_path) as state:
            grade_count = state.conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0]
            rev_count = state.conn.execute(
                "SELECT COUNT(*) FROM reply_draft_revisions"
            ).fetchone()[0]
        assert grade_count == 0
        assert rev_count == 0

    def test_unknown_field_rejected_before_any_write(
        self, db_path: str, client: TestClient
    ) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}",
            json={
                "relevance_judgment": "correct",
                "action_judgment": "accept",
                "totally_made_up_field": "nonsense",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)
        assert len(body["errors"]) > 0

        with StateManager(db_path=db_path) as state:
            count = state.conn.execute(
                "SELECT COUNT(*) FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchone()[0]
        assert count == 0

    def test_promote_with_edited_text_and_no_draft_rejected_with_no_partial_write(
        self, db_path: str, client: TestClient
    ) -> None:
        """The promotion route (a false-negative case has no draft yet)
        must apply the same draftless-edit rejection as the ordinary
        route, through the same StateManager.save_grade boundary, leaving
        neither a grade nor a promotion claim behind."""
        _scan_id, _post_id, eval_id = _seed_evaluation(
            db_path, platform_id="negative-grade-edit", posture=None, relevant=False
        )
        resp = client.post(
            f"/grades/{eval_id}/promote",
            json={
                "relevance_judgment": "false_negative",
                "action_judgment": "fail",
                "dimensions": ["usefulness"],
                "failure_note": "Scout should have surfaced this",
                "edited_text": "a reply with nothing to attach to",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)
        assert len(body["errors"]) > 0

        with StateManager(db_path=db_path) as state:
            grade_count = state.conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0]
            promo_count = state.conn.execute(
                "SELECT COUNT(*) FROM human_positive_promotions"
            ).fetchone()[0]
        assert grade_count == 0
        assert promo_count == 0


class TestGradeUsageOverride:
    def test_auto_mode_happy_path(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        resp = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "auto"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "auto"
        assert body["reason"] is None

    def test_exclude_mode_requires_reason(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        resp = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "exclude"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert isinstance(body["errors"], list)

        resp_ok = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "exclude", "reason": "stale evidence"},
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json()["mode"] == "exclude"
        assert resp_ok.json()["reason"] == "stale evidence"

    def test_missing_grade_404(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        resp = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "auto"},
        )
        assert resp.status_code == 404

    def test_nonexistent_evaluation_404(self, db_path: str, client: TestClient) -> None:
        StateManager(db_path=db_path).close()
        resp = client.post(
            "/grades/9999/usage-override",
            json={"mode": "auto"},
        )
        assert resp.status_code == 404

    def test_invalid_mode_400(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        resp = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "force_include"},
        )
        assert resp.status_code == 400

    def test_no_force_include_value(self, db_path: str, client: TestClient) -> None:
        _scan_id, _post_id, eval_id = _seed_evaluation(db_path)
        client.post(
            f"/grades/{eval_id}",
            json={"relevance_judgment": "correct", "action_judgment": "accept"},
        )
        resp = client.post(
            f"/grades/{eval_id}/usage-override",
            json={"mode": "force_include", "reason": "trust me"},
        )
        assert resp.status_code == 400
        with StateManager(db_path=db_path) as state:
            count = state.conn.execute(
                "SELECT COUNT(*) FROM grade_usage_overrides"
            ).fetchone()[0]
        assert count == 0


class TestMain:
    def test_default_host_no_warning(self, monkeypatch, caplog) -> None:
        monkeypatch.delenv("SCOUT_SIDECAR_HOST", raising=False)
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        with (
            patch("uvicorn.run") as mock_run,
            caplog.at_level("WARNING", logger="grading_api_sidecar"),
        ):
            grading_api_sidecar.main()
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert "SCOUT_SIDECAR_HOST" not in caplog.text

    def test_non_loopback_host_warns(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "0.0.0.0")
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "s3cret")
        with (
            patch("uvicorn.run") as mock_run,
            caplog.at_level("WARNING", logger="grading_api_sidecar"),
        ):
            grading_api_sidecar.main()
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert "SCOUT_SIDECAR_HOST=0.0.0.0" in caplog.text

    def test_non_loopback_without_token_exits(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "0.0.0.0")
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        with patch("uvicorn.run"), pytest.raises(SystemExit):
            grading_api_sidecar.main()

    def test_empty_token_exits(self, monkeypatch) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_TOKEN", "")
        with patch("uvicorn.run"), pytest.raises(SystemExit):
            grading_api_sidecar.main()

    def test_empty_host_falls_back_to_loopback(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("SCOUT_SIDECAR_HOST", "  ")
        monkeypatch.delenv("SCOUT_SIDECAR_TOKEN", raising=False)
        with (
            patch("uvicorn.run") as mock_run,
            caplog.at_level("WARNING", logger="grading_api_sidecar"),
        ):
            grading_api_sidecar.main()
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert "SCOUT_SIDECAR_HOST" not in caplog.text


class TestHealthDuringBlockedWrite:
    """T-010: /healthz must stay responsive while a stateful write is in
    flight. grade_usage_override_endpoint is a plain `def` handler with no
    genuine async work, so FastAPI dispatches it to its threadpool — a
    deterministic threading.Event barrier (not a sleep) holds it there while
    this test confirms /healthz answers on the event loop without waiting
    behind it.
    """

    def test_healthz_responds_while_usage_override_is_blocked(
        self, db_path: str, client: TestClient, monkeypatch
    ) -> None:
        import threading
        import time

        started = threading.Event()
        release = threading.Event()
        real_lookup = StateManager.get_grade_id_for_evaluation

        def _blocking_lookup(self: StateManager, evaluation_id: int) -> int | None:
            started.set()
            assert release.wait(timeout=5), "test never released the blocked write"
            return real_lookup(self, evaluation_id)

        monkeypatch.setattr(StateManager, "get_grade_id_for_evaluation", _blocking_lookup)

        result: dict[str, object] = {}

        def _do_write() -> None:
            result["resp"] = client.post(
                "/grades/1/usage-override", json={"mode": "auto", "reason": None}
            )

        writer = threading.Thread(target=_do_write)
        writer.start()
        try:
            assert started.wait(timeout=5), "write handler never started"
            # Confirm the write is genuinely still in flight before timing
            # /healthz, so a race where the writer finished early can't
            # masquerade as a passing responsiveness check.
            assert writer.is_alive()

            start = time.monotonic()
            health_resp = client.get("/healthz")
            elapsed = time.monotonic() - start

            assert health_resp.status_code == 200
            assert elapsed < 1.0, (
                f"/healthz took {elapsed:.3f}s while a write was in flight"
            )
        finally:
            release.set()
            writer.join(timeout=5)

        assert not writer.is_alive()
        # No grade exists for evaluation 1, so the released write 404s.
        assert result["resp"].status_code == 404  # type: ignore[union-attr]
