"""Shared fixtures for scout tests.

Also hosts the dossier-source contract checkout discovery/verification
helpers (DOSSIER_SOURCE_CONFORMANCE_REQUIRED), shared by every dossier-source
contract test module (test_dossier_conformance.py,
test_eval_corpus.py::TestLinterScript,
test_phase1_eval_runner.py::TestHermeticPhase1Sweep). See
docs/dossier-contract.md's "Running the shared conformance corpus"
section for the full pinned/candidate/required-mode contract.

The PAA task-declaration schema used to need an equivalent set here. It
does not any more: the schema arrives as a packaged dependency
(paa_contracts), so there is no checkout to locate, no revision to
verify, and no skip path to guard.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pluggy import Result

from scout.config import Message, RelevanceResult
from scout.dossiers.resolver import (
    DossierResolution,
    DossierResolutionError,
    _build_resolution,
)
from scout.storage.state import PHASE_RUN_PHASE_ORDER, StateManager

_REPO_ROOT = Path(__file__).parent.parent
_PIN_PATH = _REPO_ROOT / "contracts" / "dossier-source-v1.json"

# DOSSIER_SOURCE_MODE selects how a dossier-source checkout is located:
#   "pinned"    (default) — DOSSIER_SOURCE_PINNED_CHECKOUT env var, else a
#                sibling checkout at ../dossier-source (matching the
#                convention evals/phase1/loader.py already uses for
#                DOSSIER_SOURCE_REPO). Its HEAD must equal contracts/dossier-source-v1.json's
#                pinned revision.
#   "candidate" — an explicit opt-in for testing dossier-source's own working
#                tree: requires both DOSSIER_SOURCE_CANDIDATE_CHECKOUT and
#                DOSSIER_SOURCE_CANDIDATE_SHA, and the checkout's actual HEAD
#                must equal the supplied SHA. Never a silent fallback when
#                the pin is missing — candidate mode must be selected
#                explicitly.
# DOSSIER_SOURCE_CONFORMANCE_REQUIRED, when set, turns "no checkout"/"wrong
# HEAD"/"missing manifest or schemas" from a per-test skip into a
# session-level failure raised before any test runs (pytest_sessionstart),
# and turns any later skip of a test marked "dossier_source_contract" into a
# failure too (pytest_runtest_makereport) — required automation must fail
# closed. Unset, these tests skip with a clear message: convenient for
# developers without a local checkout.
_SOURCE_MODE_ENV = "DOSSIER_SOURCE_MODE"
_PINNED_CHECKOUT_ENV = "DOSSIER_SOURCE_PINNED_CHECKOUT"
_CANDIDATE_CHECKOUT_ENV = "DOSSIER_SOURCE_CANDIDATE_CHECKOUT"
_CANDIDATE_SHA_ENV = "DOSSIER_SOURCE_CANDIDATE_SHA"
_CONFORMANCE_REQUIRED_ENV = "DOSSIER_SOURCE_CONFORMANCE_REQUIRED"
DOSSIER_SOURCE_CONTRACT_MARKER = "dossier_source_contract"


def load_dossier_source_pin() -> dict[str, str]:
    """Load Scout's sole dossier-source contract pin."""
    pin: dict[str, str] = json.loads(_PIN_PATH.read_text())
    return pin


def dossier_source_mode() -> str:
    return os.getenv(_SOURCE_MODE_ENV, "pinned")


def _conformance_required() -> bool:
    return bool(os.getenv(_CONFORMANCE_REQUIRED_ENV))


def _git_head(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def find_dossier_source_checkout() -> Path | None:
    """Locate a local dossier-source checkout for the active source mode.

    Only checks that the checkout *looks* like dossier-source (the pinned
    summary schema file is present, or — in candidate mode — HEAD matches
    the supplied SHA). Returns None (never raises) if none is found; callers
    skip in that case rather than vendoring a copy of the schema into
    Scout's tree. Use ``verify_dossier_source_checkout`` for the full
    HEAD/manifest/schema preflight.
    """
    pin = load_dossier_source_pin()
    mode = dossier_source_mode()
    if mode == "candidate":
        candidate_checkout = os.getenv(_CANDIDATE_CHECKOUT_ENV)
        candidate_sha = os.getenv(_CANDIDATE_SHA_ENV)
        if not candidate_checkout or not candidate_sha:
            return None
        path = Path(candidate_checkout)
        if _git_head(path) != candidate_sha:
            return None
        return path
    if mode != "pinned":
        return None
    candidates = [
        os.getenv(_PINNED_CHECKOUT_ENV),
        str(_REPO_ROOT.parent / "dossier-source"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if (path / pin["summary_schema_path"]).is_file():
            return path
    return None


def verify_dossier_source_checkout(checkout: Path) -> list[str]:
    """Return preflight problems with *checkout* (empty list if none).

    Checks, in order: is a git repository; HEAD matches the revision the
    active source mode requires (the pin's revision in "pinned" mode,
    DOSSIER_SOURCE_CANDIDATE_SHA in "candidate" mode — both modes read schema
    and corpus paths from the same pin); the corpus manifest exists, parses,
    and declares at least one fixture; both pinned schema files exist. Used
    both as a normal, informative pytest assertion
    (test_checkout_head_matches_pinned_revision) and by this module's
    required-mode session preflight, which fails before any test executes.
    """
    pin = load_dossier_source_pin()
    mode = dossier_source_mode()
    head = _git_head(checkout)
    if head is None:
        return [f"{checkout} is not a git repository (or git is unavailable)"]

    problems: list[str] = []
    expected_head = os.getenv(_CANDIDATE_SHA_ENV) if mode == "candidate" else pin["revision"]
    if head != expected_head:
        problems.append(
            f"checkout HEAD {head!r} does not match expected revision {expected_head!r}"
        )

    manifest_path = checkout / pin["corpus_path"] / "manifest.json"
    if not manifest_path.is_file():
        problems.append(f"missing corpus manifest: {manifest_path}")
    else:
        try:
            manifest: Any = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"corpus manifest is unreadable or invalid JSON: {exc}")
        else:
            if not manifest.get("fixtures"):
                problems.append(f"corpus manifest declares zero fixtures: {manifest_path}")

    for schema_key in ("index_schema_path", "summary_schema_path"):
        schema_path = checkout / pin[schema_key]
        if not schema_path.is_file():
            problems.append(f"missing pinned schema: {schema_path}")

    return problems


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{DOSSIER_SOURCE_CONTRACT_MARKER}: exercises the pinned dossier-source contract "
        f"(schema/corpus checkout) — required (never skipped) when "
        f"{_CONFORMANCE_REQUIRED_ENV} is set.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """In required mode, fail the whole session before any test runs if the
    dossier-source checkout can't be found or fails preflight — fail closed
    rather than let individual tests skip past a missing or wrong
    checkout."""
    if _conformance_required():
        checkout = find_dossier_source_checkout()
        if checkout is None:
            raise pytest.UsageError(
                f"{_CONFORMANCE_REQUIRED_ENV} is set but no dossier-source checkout was found for "
                f"source mode {dossier_source_mode()!r} — set {_PINNED_CHECKOUT_ENV}, or "
                f"both {_CANDIDATE_CHECKOUT_ENV} and {_CANDIDATE_SHA_ENV} in candidate mode."
            )
        problems = verify_dossier_source_checkout(checkout)
        if problems:
            joined = "\n".join(f"  - {p}" for p in problems)
            raise pytest.UsageError(
                f"{_CONFORMANCE_REQUIRED_ENV} is set but the dossier-source checkout at {checkout} "
                f"failed preflight:\n{joined}"
            )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _conformance_required():
        marked = [item for item in items if item.get_closest_marker(DOSSIER_SOURCE_CONTRACT_MARKER)]
        if not marked:
            raise pytest.UsageError(
                f"{_CONFORMANCE_REQUIRED_ENV} is set but no test marked "
                f"{DOSSIER_SOURCE_CONTRACT_MARKER!r} was collected — the test selection likely "
                "excluded the required dossier-source contract tests."
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Result[pytest.TestReport], None]:
    """Turn a skip of a dossier-source contract test into a failure when its
    REQUIRED env var is set — a safety net alongside the session preflight
    above for any skip a future change might reintroduce."""
    outcome = yield
    report = outcome.get_result()
    if report.when not in ("setup", "call") or not report.skipped:
        return

    if _conformance_required() and item.get_closest_marker(DOSSIER_SOURCE_CONTRACT_MARKER):
        report.outcome = "failed"
        report.longrepr = (
            f"{_CONFORMANCE_REQUIRED_ENV} is set: a dossier-source contract test may not "
            f"skip.\n{report.longrepr}"
        )


def resolve_dossier_from_disk(
    repository: Path | str,
    revision: str,
    project_key: str,
    dossier_summary_id: str,
    *,
    max_age_days: int | None = None,
    min_entries: int = 0,
) -> DossierResolution:
    """Test-only resolve_dossier substitute for tests that exercise a
    caller's own aggregation, readiness, or replay logic — not dossier
    schema conformance (see tests/test_dossier_conformance.py for that
    pinned, CI-gated coverage). resolve_dossier now retrieves the pinned
    dossier-source schema via ``git show``; synthetic test repos deliberately
    carry no schema copy (vendoring one here would recreate exactly the
    mirror-drift problem this contract change eliminates), so this reads
    the working tree directly and applies only Scout's own identity/
    readiness/cross-reference layer via ``dossier._build_resolution``.
    """
    root = Path(repository)
    index = yaml.safe_load((root / "index.yaml").read_text())
    entry = index.get("entries", {}).get(dossier_summary_id)
    if entry is None:
        raise DossierResolutionError(
            "summary id is absent from index", project_key=project_key,
            summary_id=dossier_summary_id, revision=revision,
        )
    path = entry["path"]
    document = yaml.safe_load((root / path).read_text())
    return _build_resolution(
        document, project_key, dossier_summary_id, revision, path,
        max_age_days=max_age_days, min_entries=min_entries,
    )


@pytest.fixture(autouse=True)
def _isolate_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect DB_PATH, TRACE_DB_PATH, and FEEDBACK_DB_PATH to tmp_path.

    Prevents any test from accidentally writing scout.db / scout_traces.db /
    scout_feedback.db at the repo root. Module-level imports in scout_cli and
    grading_api_sidecar are patched alongside the config module so all call
    sites pick up the redirected paths.
    """
    import scout.cli.grading_api as grading_api_sidecar
    import scout.cli.main as scout_cli
    import scout.config as config
    import scout.scanning.runner as scan_runner

    db = str(tmp_path / "scout.db")
    trace_db = str(tmp_path / "scout_traces.db")
    feedback_db = str(tmp_path / "scout_feedback.db")

    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "TRACE_DB_PATH", trace_db)
    monkeypatch.setattr(config, "FEEDBACK_DB_PATH", feedback_db)
    monkeypatch.setattr(scout_cli, "DB_PATH", db)
    monkeypatch.setattr(grading_api_sidecar, "DB_PATH", db)
    monkeypatch.setattr(grading_api_sidecar, "TRACE_DB_PATH", trace_db)
    monkeypatch.setattr(grading_api_sidecar, "FEEDBACK_DB_PATH", feedback_db)
    monkeypatch.setattr(scan_runner, "DB_PATH", db)
    monkeypatch.setattr(scan_runner, "TRACE_DB_PATH", trace_db)
    monkeypatch.setattr(scan_runner, "FEEDBACK_DB_PATH", feedback_db)


# --- Factory fixtures ---


@pytest.fixture
def make_message() -> type[_MessageFactory]:
    """Factory for creating Message instances with sensible defaults."""
    return _MessageFactory


class _MessageFactory:
    _counter = 0

    @classmethod
    def create(
        cls,
        content: str = "Hello world",
        platform: str = "discord",
        channel_name: str = "general",
        author_name: str = "alice",
        **overrides: object,
    ) -> Message:
        cls._counter += 1
        defaults = {
            "platform": platform,
            "platform_id": f"msg-{cls._counter}",
            "channel_name": channel_name,
            "channel_id": "ch-1",
            "author_name": author_name,
            "author_id": "user-1",
            "content": content,
            "created_at": datetime.now(UTC),
            "url": "",
        }
        defaults.update(overrides)
        return Message(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def msg_factory() -> _MessageFactory:
    """Return a message factory."""
    return _MessageFactory()


@pytest.fixture
def sample_message() -> Message:
    """A single sample message for tests that need just one."""
    return Message(
        platform="discord",
        platform_id="test-1",
        channel_name="general",
        channel_id="ch-1",
        author_name="alice",
        author_id="user-1",
        content="Looking for bot detection solutions for our agent signup flow",
        created_at=datetime.now(UTC),
        url="https://discord.com/test",
    )


@pytest.fixture
def sample_relevance_result(sample_message: Message) -> RelevanceResult:
    """A sample relevant evaluation result."""
    return RelevanceResult(
        message=sample_message,
        relevant=True,
        score=0.85,
        reason="Discusses bot detection for agent signups",
        relevant_to=("gateway",),
    )


@pytest.fixture
def in_memory_state() -> Generator[StateManager, None, None]:
    """StateManager backed by an in-memory SQLite database."""
    state = StateManager(db_path=":memory:")
    yield state
    # Mirror how production callers use StateManager: issue writes, then
    # commit once. Db.close() refuses to close over an unmanaged pending
    # transaction, so a test that wrote without an explicit state.commit()
    # (relying on the old lenient close()) needs this same flush.
    state.commit()
    state.close()


def seed_phase_run_contributors(
    state: StateManager, scan_id: int, post_id: int, count: int = 3,
) -> tuple[int, ...]:
    """Insert `count` durable, unlinked, complete evaluation_phase_runs rows
    (relevance, reply_draft, critic in order) for scan_id/post_id and return
    their ids.

    A test seam for exercising StateManager's contributor-linkage paths
    (persist_terminal_outcome / persist_surfaced_outcome) without running
    the real pipeline. Reuses `scan_id`'s feedback snapshot if one was
    already recorded (mirrors production: one snapshot per scan, many
    phase runs across many posts referencing its three phase rows);
    otherwise records one (shadow mode) to obtain real snapshot_phase_id FKs.
    """
    existing = state.conn.execute(
        "SELECT id FROM feedback_snapshots WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    if existing is not None:
        rows = state.conn.execute(
            "SELECT phase, id FROM feedback_snapshot_phases WHERE snapshot_id = ?",
            (existing["id"],),
        ).fetchall()
        phase_by_name = {row["phase"]: row["id"] for row in rows}
    else:
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
    ids = []
    for phase in PHASE_RUN_PHASE_ORDER[:count]:
        phase_run_id = state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name[phase],
            phase=phase,
            trace_id=f"test-trace-{uuid.uuid4()}",
            model="test-model",
            status="complete",
        )
        ids.append(phase_run_id)
    return tuple(ids)
