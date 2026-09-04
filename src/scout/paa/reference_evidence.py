"""Deterministic generator for evidence/paa/reference/.

Produces one publication-safe, byte-reproducible evidence pack: exact
copies of the checked-in PAA task declarations and the grading
schema, a redacted schema-2 evidence bundle (via
``evidence_bundle.create_reference_bundle``), a redacted correction-and-
prompt document, a redacted offline-replay experiment summary, and a manifest
recording every artifact's and source's path/hash plus the schema and
distribution versions that produced them.

Every input this module reads to render the tree is either a checked-in
repository file (the live declarations, the live grading schema) or an
explicit, pinned ``GenerationInputs`` value — nothing here reads the
wall clock or any other non-reproducible state, which is what makes
``render()`` byte-identical across two calls with the same inputs. The
one exception, deliberately: ``build_fixture_source_database`` seeds a
throwaway SQLite database from fixed constants (never ``datetime.now()``
or ``uuid.uuid4()`` for anything that reaches rendered output) so the
same fixture reproduces byte-for-byte on every call too.

``write_reference_tree`` is the only function that touches the checked-in
``evidence/paa/reference/`` — it renders into a sibling temporary
directory and atomically swaps it in. ``check_reference_tree`` never
writes anywhere: it re-renders from the checked-in manifest's own
recorded generation inputs, entirely under a temporary directory, and
diffs the result byte-for-byte against what is on disk.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest.mock
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paa_contracts
from jig import SQLiteFeedbackLoop, SQLiteTracer
from jsonschema import validators
from paa_runtime import service as paa_service
from paa_runtime.config import RuntimeConfig
from paa_runtime.events import CURRENT_EVENT_SCHEMA

from scout.config import GradeRecord, Message, RelevanceResult, SourceAuthor, SourceParent
from scout.grading.correction import NORMALIZED_EDIT_DISTANCE_GRADER_VERSION
from scout.paa.audit.runner import REPORT_SCHEMA_VERSION as AUDIT_REPORT_SCHEMA_VERSION
from scout.paa.audit.runner import canonical_json, parse_utc
from scout.paa.declarations import PRODUCER_REGISTRY, get_paa_declaration, load_paa_declarations
from scout.paa.event_store import ScoutEventStore
from scout.paa.evidence.bundle import (
    REFERENCE_BUNDLE_SCHEMA_VERSION,
    REFERENCE_REDACTION_SCHEMA,
    create_reference_bundle,
    find_redaction_markers,
    redact_value,
    verify_bundle,
)
from scout.replay.pricing import PRICING_CATALOG_VERSION
from scout.replay.reporting import BOOTSTRAP_VERSION as EXPERIMENT_STATISTICS_BOOTSTRAP_VERSION
from scout.replay.reporting import REPORT_SCHEMA_VERSION as EXPERIMENT_SUMMARY_SCHEMA_VERSION
from scout.storage.state import StateManager
from scout.verifier import DRAFT_TEXT_ASSEMBLER_VERSION, GateViolation

REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCE_DIR = REPO_ROOT / "evidence" / "paa" / "reference"
DECLARATIONS_SOURCE_DIR = REPO_ROOT / "contracts" / "paa"
GRADING_SCHEMA_SOURCE = REPO_ROOT / "web" / "grading_schema.json"
FIXTURE_SOURCE_DIR = REPO_ROOT / "tests" / "fixtures" / "paa_reference" / "source"
FIXTURE_AUDIT_REPORT_PATH = FIXTURE_SOURCE_DIR / "audit_report.json"
FIXTURE_BEFORE_PATH = FIXTURE_SOURCE_DIR / "before.bin"

DECLARATION_FILENAMES: tuple[str, ...] = (
    "reply_draft.v1.yaml",
    "canonical_promotion.v1.yaml",
    "inbound_reply_surfacing.v1.yaml",
)
DECLARATION_TASKS: tuple[str, ...] = (
    "reply_draft",
    "canonical_promotion",
    "inbound_reply_surfacing",
)

GENERATOR_SCHEMA_VERSION = 1

# No file tracked anywhere in this repo may be named manifest.json or
# manifest.sha256 (tests/test_dossier_no_legacy_mirror.py) — rename the
# bundle's own canonical artifact names only for the copy that lands in
# the checked-in tree.
_BUNDLE_ARTIFACT_RENAME: dict[str, str] = {
    "manifest.json": "bundle-manifest.json",
    "manifest.sha256": "bundle-manifest.sha256",
}
REFERENCE_MANIFEST_NAME = "reference-manifest.json"

# Fixed, non-wall-clock values every seeded fixture row uses — determinism
# requires that nothing in the fixture database depends on when it happens
# to be built.
_FIXTURE_NOW = "2026-01-01T00:00:00+00:00"
_FIXTURE_GRADED_AT_DT = datetime(2026, 1, 1, tzinfo=UTC)
_FIXTURE_DOSSIER_REVISION = "b" * 40

# Every sentinel is unique and namespaced so a test can assert none of
# them occur anywhere in rendered output bytes.
SENTINELS: dict[str, str] = {
    "source_text": "REFERENCE-FIXTURE-SOURCE-TEXT-3f8a1c2d",
    "author_id": "REFERENCE-FIXTURE-AUTHOR-ID-9b7e4f01",
    "author_name": "REFERENCE-FIXTURE-AUTHOR-NAME-1d6c8a22",
    "platform_msg_id": "REFERENCE-FIXTURE-PLATFORM-MSG-ID-77af03bc",
    "url": "https://example.test/REFERENCE-FIXTURE-URL-e21b90f4",
    "parent_id": "REFERENCE-FIXTURE-PARENT-ID-5a3d19e7",
    "parent_author_id": "REFERENCE-FIXTURE-PARENT-AUTHOR-ID-c40f8b12",
    "parent_author_name": "REFERENCE-FIXTURE-PARENT-AUTHOR-NAME-08e2d6f9",
    "parent_text": "REFERENCE-FIXTURE-PARENT-TEXT-4b91c7ae",
    "parent_url": "https://example.test/REFERENCE-FIXTURE-PARENT-URL-fa028d3c",
    "offending_text": "REFERENCE-FIXTURE-OFFENDING-TEXT-b6d40e91",
    "evaluation_reason": "REFERENCE-FIXTURE-EVALUATION-REASON-91ac0d3e",
    "failure_note": "REFERENCE-FIXTURE-FAILURE-NOTE-a08d3f61",
    "factual_offending_claim": "REFERENCE-FIXTURE-FACTUAL-OFFENDING-CLAIM-71bd4e0a",
    "factual_contradicting_evidence": "REFERENCE-FIXTURE-FACTUAL-CONTRADICTING-EVIDENCE-de590c3b",
    "context_missing_input": "REFERENCE-FIXTURE-CONTEXT-MISSING-INPUT-3af607d2",
    "implication_implied_claim": "REFERENCE-FIXTURE-IMPLICATION-IMPLIED-CLAIM-6b03fa8e",
    "implication_missing_support": "REFERENCE-FIXTURE-IMPLICATION-MISSING-SUPPORT-e47a1d92",
    "prompt_text": "REFERENCE-FIXTURE-PROMPT-TEXT-9f1e6a4d",
    "correction_text": "REFERENCE-FIXTURE-CORRECTION-TEXT-c72db801",
}

FIXTURE_GATE_BLOCK_ID = 1
FIXTURE_EVALUATION_ID = 1
# The second evaluation build_fixture_source_database seeds — surfaced,
# carrying the literal prompt override and reply_draft_revisions
# correction (a gate-blocked evaluation can never carry either).
FIXTURE_CORRECTION_EVALUATION_ID = 2


class ReferenceGenerationError(RuntimeError):
    """Raised when reference evidence cannot be generated or fails --check."""


@dataclass(frozen=True, slots=True)
class GenerationInputs:
    """Every pinned, operator-supplied input a reproducible generation run
    needs. Recorded verbatim in the manifest so --check can reconstruct
    the exact same render without any of these being re-typed."""

    gate_block_id: int
    code_revision: str
    model_id: str
    prompt_revision: str
    generated_at: datetime
    git_commit: str

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "gate_block_id": self.gate_block_id,
            "code_revision": self.code_revision,
            "model_id": self.model_id,
            "prompt_revision": self.prompt_revision,
            "generated_at": self.generated_at.isoformat(),
            "git_commit": self.git_commit,
        }

    @classmethod
    def from_manifest_dict(cls, data: Mapping[str, Any]) -> GenerationInputs:
        return cls(
            gate_block_id=int(data["gate_block_id"]),
            code_revision=str(data["code_revision"]),
            model_id=str(data["model_id"]),
            prompt_revision=str(data["prompt_revision"]),
            generated_at=parse_utc(str(data["generated_at"])),
            git_commit=str(data["git_commit"]),
        )


def default_generation_inputs() -> GenerationInputs:
    """The pinned inputs the reference-evidence generator uses when the operator
    supplies none — reproducible against the checked-in fixture source.

    git_commit is a deterministic placeholder here on purpose: this
    function must stay a pure, non-git-dependent default so tests (and
    --check, which re-renders from a manifest's own already-pinned
    inputs) reproduce identically regardless of the ambient checkout.
    A real `--write` run must resolve and pin the actual current commit
    instead of calling this function's placeholder verbatim — see
    resolve_git_commit and scripts/generate_paa/reference_evidence.py.
    """
    return GenerationInputs(
        gate_block_id=FIXTURE_GATE_BLOCK_ID,
        code_revision="0" * 40,
        model_id="reference-generator/fixture",
        prompt_revision="reference-generator/fixture",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        git_commit="0" * 40,
    )


def resolve_git_commit(repo_root: Path = REPO_ROOT) -> str:
    """The real, current `git rev-parse HEAD` for *repo_root* — required
    for every `--write` run so the checked-in manifest's git_revision
    names the actual commit the reference evidence was generated at,
    never a placeholder. Raises ReferenceGenerationError if HEAD cannot
    be resolved (not a git repository, detached with no commits, etc.)."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ReferenceGenerationError(
            f"failed to resolve the real git commit at {repo_root}: {result.stderr.strip()}"
        )
    commit = result.stdout.strip()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ReferenceGenerationError(f"unexpected `git rev-parse HEAD` output: {commit!r}")
    return commit


# --- Fixture source construction ---------------------------------------


def build_fixture_source_database(db_path: Path) -> None:
    """Build the reference generator's seeded source database at *db_path*.

    One production/live gate-blocked evaluation carrying a unique
    sentinel for every prohibited data class the redaction pipeline must
    strip (source/parent text, author identity, platform identity, URLs,
    prompt-derived reasoning, and free-form grade detail). Every timestamp is a
    fixed constant, never the wall clock, so two builds are byte-for-byte
    identical database contents (values, not file bytes — SQLite's own
    on-disk layout is not guaranteed byte-stable, which is why callers
    diff *rendered evidence*, never the database file itself).
    """
    if db_path.exists():
        db_path.unlink()
    with StateManager(str(db_path)) as state:
        state.conn.execute(
            "INSERT INTO scans(started_at, environment, run_kind) VALUES (?, 'production', 'live')",
            (_FIXTURE_NOW,),
        )
        message = Message(
            platform="bluesky",
            platform_id=SENTINELS["platform_msg_id"],
            channel_name="general",
            channel_id="ch-1",
            author_name=SENTINELS["author_name"],
            author_id=SENTINELS["author_id"],
            content=SENTINELS["source_text"],
            created_at=datetime.fromisoformat(_FIXTURE_NOW),
            url=SENTINELS["url"],
            parent=SourceParent(
                id=SENTINELS["parent_id"],
                author=SourceAuthor(
                    id=SENTINELS["parent_author_id"], name=SENTINELS["parent_author_name"]
                ),
                text=SENTINELS["parent_text"],
                url=SENTINELS["parent_url"],
            ),
            parent_lookup_status="resolved",
        )
        post_id = state.save_post(message, scan_id=1)

        # A gate-blocked evaluation needs at least one complete, unlinked
        # phase run to attach — the ordinary relevance-only sequence.
        snapshot = state.record_feedback_snapshot(1, mode="shadow")
        relevance_snapshot_phase_id = next(
            p.snapshot_phase_id for p in snapshot.phases if p.phase == "relevance"
        )
        phase_run_id = state.insert_phase_run(
            scan_id=1, post_id=post_id, snapshot_phase_id=relevance_snapshot_phase_id,
            phase="relevance", trace_id="reference-fixture-trace", model="reference-fixture-model",
            status="complete",
        )

        # Routed through the sole authoritative evaluations/gate_blocks
        # writer (StateManager.persist_terminal_outcome) rather than raw
        # INSERTs — see TestGateBlocksWriter in tests/test_state_manager.py.
        # persist_terminal_outcome derives the gate block's `context` as
        # f"{platform}:{platform_id}", so it carries the platform_msg_id
        # sentinel too — exercising that redaction path without a
        # dedicated sentinel of its own.
        result = RelevanceResult(
            message=message, relevant=True, score=1.0,
            reason=SENTINELS["evaluation_reason"], relevant_to=("gateway",),
        )
        state.persist_terminal_outcome(
            result, post_id, 1,
            surface_status="gate_blocked",
            contributor_phase_run_ids=(phase_run_id,),
            project_key="gateway",
            dossier_revision=_FIXTURE_DOSSIER_REVISION,
            dossier_summary_id="gateway-dossier",
            gate_violations=[
                GateViolation(
                    reason_code="safe_phrasing",
                    offending_text=SENTINELS["offending_text"],
                    segment_index=0,
                )
            ],
        )
        # save_evaluation/_save_gate_violations always stamp the wall
        # clock and take no injectable `now` — overwrite both to the
        # fixed fixture timestamp afterward so two builds stay
        # byte-identical.
        state.conn.execute(
            "UPDATE evaluations SET created_at = ? WHERE id = ?",
            (_FIXTURE_NOW, FIXTURE_EVALUATION_ID),
        )
        state.conn.execute(
            "UPDATE gate_blocks SET created_at = ? WHERE id = ?",
            (_FIXTURE_NOW, FIXTURE_GATE_BLOCK_ID),
        )
        # Routed through the sole authoritative grades writer (StateManager
        # .save_grade) rather than a raw INSERT — see
        # tests/test_grade_write_source_guard.py.
        state.save_grade(
            GradeRecord(
                post_id=1,
                evaluation_id=FIXTURE_EVALUATION_ID,
                scan_id=1,
                source="web",
                graded_at=_FIXTURE_GRADED_AT_DT,
                relevance_judgment="correct",
                action_judgment="fail",
                dimensions=[
                    "factual_support", "contextual_understanding", "unsupported_implication",
                ],
                failure_note=SENTINELS["failure_note"],
                factual_offending_claim=SENTINELS["factual_offending_claim"],
                factual_disposition="contradicted",
                factual_contradicting_evidence=SENTINELS["factual_contradicting_evidence"],
                context_missing_input=SENTINELS["context_missing_input"],
                implication_implied_claim=SENTINELS["implication_implied_claim"],
                implication_missing_support=SENTINELS["implication_missing_support"],
            )
        )

        # A second, surfaced evaluation carrying a real per-keyword literal
        # prompt override (project_keywords.evaluate_prompt) and a real
        # reply_draft_revisions correction. A gate-blocked evaluation (like
        # the one above) can never carry either — persist_terminal_outcome
        # deliberately never creates a draft, and save_grade's edited_text
        # path requires an existing draft_comments row for the same
        # evaluation — so this is a separate, surfaced case.
        state.conn.execute(
            "INSERT INTO projects(key, name, description, link, active, created_at, updated_at) "
            "VALUES ('gateway', 'Gateway', 'Reference fixture project', "
            "'https://example.test/gateway', 1, ?, ?)",
            (_FIXTURE_NOW, _FIXTURE_NOW),
        )
        keyword_cursor = state.conn.execute(
            "INSERT INTO project_keywords(project_key, keyword, evaluate_prompt, match_type, "
            "active, priority, created_at, updated_at) "
            "VALUES ('gateway', 'gateway', ?, 'substring', 1, 100, ?, ?)",
            (SENTINELS["prompt_text"], _FIXTURE_NOW, _FIXTURE_NOW),
        )
        keyword_route_id = keyword_cursor.lastrowid

        correction_message = Message(
            platform="bluesky",
            platform_id="reference-fixture-correction-post",
            channel_name="general",
            channel_id="ch-1",
            author_name="reference-fixture-correction-author",
            author_id=SENTINELS["author_id"],
            content="reference fixture surfaced post content",
            created_at=datetime.fromisoformat(_FIXTURE_NOW),
            url="https://example.test/reference-fixture-correction-post",
        )
        correction_post_id = state.save_post(correction_message, scan_id=1)
        correction_phase_run_id = state.insert_phase_run(
            scan_id=1, post_id=correction_post_id,
            snapshot_phase_id=relevance_snapshot_phase_id, phase="relevance",
            trace_id="reference-fixture-trace-correction", model="reference-fixture-model",
            status="complete",
        )
        correction_evaluation_id, _draft_id, _event_id = state.persist_surfaced_outcome(
            RelevanceResult(
                message=correction_message, relevant=True, score=1.0,
                reason="reference fixture surfaced reason", relevant_to=("gateway",),
            ),
            correction_post_id, 1,
            project_key="gateway", author_id=SENTINELS["author_id"], platform="bluesky",
            comment_text="reference fixture drafted reply text", structured_output=None,
            contributor_phase_run_ids=(correction_phase_run_id,),
            keyword_route_id=keyword_route_id,
            dossier_revision=_FIXTURE_DOSSIER_REVISION, dossier_summary_id="gateway-dossier",
            surfaced_at=_FIXTURE_NOW,
        )
        state.save_grade_for_migration(
            GradeRecord(
                post_id=correction_post_id, source="migration",
                graded_at=_FIXTURE_GRADED_AT_DT, relevance_judgment="correct",
                evaluation_id=correction_evaluation_id, edited_text=SENTINELS["correction_text"],
            ),
            migration_reason="reference fixture correction",
        )
        state.commit()

        # One hermetic offline-replay batch (the evaluation_experiments /
        # replay_reporting domain) — jig needs its own separate trace and
        # feedback stores, thrown away with this temporary directory since
        # nothing downstream reads them directly (build_batch_report reads
        # only the StateManager rows execute_batch_replay wrote — see its
        # module docstring).
        with tempfile.TemporaryDirectory() as jig_tmp:
            tracer = SQLiteTracer(db_path=str(Path(jig_tmp) / "tracer.db"))
            feedback = SQLiteFeedbackLoop(db_path=str(Path(jig_tmp) / "feedback.db"))
            asyncio.run(_seed_and_run_experiment_batch(state, tracer, feedback))


# build_fixture_source_database's own qualified name — recorded as the
# fixture database's manifest "source path" in place of a real filesystem
# path, since the database itself is an ephemeral temporary file rebuilt
# fresh on every render() call, never a checked-in artifact.
FIXTURE_SOURCE_DB_LOGICAL_PATH = f"{__name__}.build_fixture_source_database()"


# Columns StateManager or jig writers stamp from the wall clock (or a
# random trace/idempotency id) with no way for a caller to pin them, on
# tables this fixture writes through only via the sanctioned writer
# methods (StateManager writers, or jig's run_agent/execute_batch_replay
# for the offline-replay batch) — excluded from the logical dump so it
# reflects the fixture's actual authored content, not incidental
# non-reproducible bookkeeping. Every column this generator *does* pin
# (e.g. evaluations.created_at, grades.graded_at) is excluded too,
# harmlessly — it is identical across builds either way, so dropping it
# from the dump loses no coverage. trace_diff is dropped alongside its
# own trace_a_id/trace_b_id since it embeds them inline as JSON — nothing
# downstream reads it, unlike the distances/deltas replay_reporting
# derives from it, which do get pinned deterministically.
_LOGICAL_DUMP_EXCLUDED_COLUMNS = frozenset({
    "trace_id", "root_trace_id", "candidate_trace_id", "trace_a_id", "trace_b_id",
    "trace_diff", "idempotency_key", "as_of",
})


def _is_excluded_dump_column(name: str) -> bool:
    return name.endswith("_at") or name in _LOGICAL_DUMP_EXCLUDED_COLUMNS


def dump_fixture_database_logical(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """A canonical, complete dump of every table's rows in *db_path*, via a
    fresh mode=ro connection, keyed by table name and ordered by rowid,
    excluding non-reproducible bookkeeping columns (see
    _LOGICAL_DUMP_EXCLUDED_COLUMNS / _is_excluded_dump_column).

    The manifest's source_db hash binds to this dump, never to
    db_path.read_bytes() — SQLite's own on-disk page layout is not
    guaranteed byte-stable across rebuilds even when every inserted value
    is identical (allocation order, freelist state), so hashing the file
    directly would make two logically-identical fixture builds report
    different source hashes.
    """
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        table_names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        dump: dict[str, list[dict[str, Any]]] = {}
        for name in table_names:
            rows = conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
            dump[name] = [
                {k: v for k, v in dict(row).items() if not _is_excluded_dump_column(k)}
                for row in rows
            ]
        return dump
    finally:
        conn.close()


# --- Declaration conformance / lifecycle exercise -----------------------


def validate_declarations_conform(declarations_dir: Path) -> dict[str, int]:
    """Validate every declaration in *declarations_dir* against the
    installed paa-task JSON Schema and Scout's PRODUCER_REGISTRY, then
    resolve each by exact identity. Returns {task: declaration_version}.
    Raises ReferenceGenerationError on any conformance or resolution
    failure — this is the "installed PAA contracts" half of the check.
    """
    import yaml

    schema = paa_contracts.load_schema("paa-task")
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    for name in DECLARATION_FILENAMES:
        path = declarations_dir / name
        document = yaml.safe_load(path.read_text())
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        if errors:
            raise ReferenceGenerationError(
                f"{path} fails paa-task schema conformance: {[e.message for e in errors]}"
            )

    try:
        load_paa_declarations(declarations_dir)
    except Exception as exc:  # noqa: BLE001 - re-raised with generator context
        raise ReferenceGenerationError(f"declarations failed to resolve: {exc}") from exc

    versions: dict[str, int] = {}
    for task in DECLARATION_TASKS:
        declaration = get_paa_declaration(task, directory=declarations_dir)
        versions[task] = declaration.version
    return versions


def exercise_paa_lifecycle(declarations_dir: Path) -> None:
    """Drive propose -> approve -> resolve -> demote against temporary,
    throwaway storage using the real paa_runtime lifecycle and Scout's
    ScoutEventStore adapter. Never touches Scout's checked-in database or
    evidence/paa/ — a fresh in-memory StateManager and a temporary
    evidence root are used and discarded. Raises ReferenceGenerationError
    if any lifecycle step fails or ends in an unexpected position.
    """
    with StateManager(db_path=":memory:") as state:
        store = ScoutEventStore(state)
        with tempfile.TemporaryDirectory() as evidence_root:
            config = RuntimeConfig(
                declarations_dir=declarations_dir,
                evidence_root=Path(evidence_root),
                registry=PRODUCER_REGISTRY,
                db_path=Path(evidence_root) / "unused.db",
                actor_env_var="SCOUT_PAA_ACTOR",
            )
            evidence_path = Path(evidence_root) / "lifecycle-evidence.json"
            evidence_path.write_text(canonical_json({"purpose": "reference-generator-smoke-test"}))
            try:
                motion = paa_service.propose(
                    store, config,
                    task="inbound_reply_surfacing", scope=None,
                    to_position="hotl", evidence_path=evidence_path,
                    actor="reference-generator", reason="reference evidence lifecycle smoke test",
                )
                paa_service.approve(
                    store, config, motion_id=motion.motion_id,
                    reason="reference evidence lifecycle smoke test", actor="reference-generator",
                )
                after_approve = paa_service.resolve_current_position(
                    store, config, task="inbound_reply_surfacing", scope=None,
                )
                if after_approve.current_position != "hotl":
                    raise ReferenceGenerationError(
                        "lifecycle smoke test: expected hotl after approval, got "
                        f"{after_approve.current_position!r}"
                    )
                paa_service.demote(
                    store, config,
                    task="inbound_reply_surfacing", scope=None,
                    reason="reference evidence lifecycle smoke test demotion",
                    actor="reference-generator",
                    source_rows=["reference:lifecycle-smoke-test"],
                )
                after_demote = paa_service.resolve_current_position(
                    store, config, task="inbound_reply_surfacing", scope=None,
                )
                if after_demote.current_position != "hitl":
                    raise ReferenceGenerationError(
                        "lifecycle smoke test: expected hitl after demotion, got "
                        f"{after_demote.current_position!r}"
                    )
            except ReferenceGenerationError:
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised with generator context
                raise ReferenceGenerationError(f"PAA lifecycle smoke test failed: {exc}") from exc
        state.commit()


# --- Correction and prompt text -----------------------------------------


def read_correction_and_prompt(db_path: Path, *, evaluation_id: int) -> dict[str, Any]:
    """Read the one real reply_draft_revisions correction and the one real
    per-keyword literal prompt override attached to *evaluation_id*, via a
    fresh mode=ro connection. Raises ReferenceGenerationError if the
    evaluation doesn't exist."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        row = conn.execute(
            """SELECT e.id AS evaluation_id, pk.evaluate_prompt,
                      g.id AS grade_id, rdr.reply_text
                 FROM evaluations e
                 LEFT JOIN project_keywords pk ON pk.id = e.keyword_route_id
                 LEFT JOIN grades g ON g.evaluation_id = e.id
                 LEFT JOIN reply_draft_revisions rdr ON rdr.id = g.reply_revision_id
                WHERE e.id = ?""",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise ReferenceGenerationError(
                f"no evaluation {evaluation_id} to read correction/prompt evidence from"
            )
        return dict(row)
    finally:
        conn.close()


def redact_correction_and_prompt(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Redact the literal prompt override and the reply_draft_revisions
    correction text read by read_correction_and_prompt. Both are real
    free-text content (an operator-authored keyword prompt, and a grader's
    corrected reply) with nothing else to retain from this evaluation, so
    the whole document is just these two typed markers plus the
    structural ids that name what was redacted."""
    redactions: list[dict[str, Any]] = []

    def redact(value: object, *, path: str) -> dict[str, Any] | None:
        return redact_value(value, redactions, artifact="correction-and-prompt.json", path=path)

    redacted = {
        "evaluation_id": row["evaluation_id"],
        "grade_id": row["grade_id"],
        "prompt_text": redact(row["evaluate_prompt"], path="prompt_text"),
        "correction_text": redact(row["reply_text"], path="correction_text"),
    }
    return redacted, redactions


def verify_correction_and_prompt_redactions(
    document: Mapping[str, Any], redactions: list[dict[str, Any]]
) -> None:
    """Duplicate/missing/extra cross-check of declared redactions against
    the markers actually embedded in correction-and-prompt.json."""
    found = {
        (path, str(marker.get("kind")), marker.get("sha256"), marker.get("length"))
        for path, marker in find_redaction_markers(dict(document))
    }
    declared_keys = [
        (str(r.get("path")), str(r.get("kind")), r.get("sha256"), r.get("length"))
        for r in redactions
    ]
    declared = set(declared_keys)
    if len(declared_keys) != len(declared):
        duplicates = sorted({key for key in declared_keys if declared_keys.count(key) > 1})
        raise ReferenceGenerationError(
            f"correction-and-prompt redactions.json declares duplicate entries: {duplicates}"
        )
    if found != declared:
        raise ReferenceGenerationError(
            "correction-and-prompt redaction metadata mismatch: "
            f"missing={sorted(found - declared)} extra={sorted(declared - found)}"
        )


# --- Experiment summary (offline replay) --------------------------------

# The first (and only) experiment_runs parent build_fixture_source_database
# seeds — a fresh database's first INSERT into that table always gets id 1.
FIXTURE_EXPERIMENT_RUN_ID = 1

_EXPERIMENT_BASELINE_MODEL = "reference-fixture-baseline-model"
_EXPERIMENT_CANDIDATE_MODEL = "reference-fixture-candidate-model"
_EXPERIMENT_CASE_COUNT = 2


def _experiment_correction_payload(case_index: int) -> dict[str, Any]:
    text = f"Reference fixture correction text for case {case_index}."
    return {
        "posture": "answer",
        "segments": [{"type": "declarative", "fact_id": "fact-1", "text": text}],
        "claims": [text],
        "resources_used": [],
    }


async def _seed_and_run_experiment_batch(state: StateManager, tracer: Any, feedback: Any) -> int:
    """Seed _EXPERIMENT_CASE_COUNT hermetic reply_draft baseline cases and
    execute one batch replay against them with a single scripted candidate
    variant that matches its baseline exactly (distance 0.0 on every
    case). Entirely offline: dossier resolution and the candidate model
    backend are both faked (unittest.mock.patch.object, scoped tightly
    around the batch calls) so this never touches a live model, network,
    or dossier-source checkout — the same substitution
    tests/test_evaluation_experiments.py makes via monkeypatch, done here
    with the stdlib mechanism since this runs outside pytest. Returns the
    resulting experiment_runs parent id.
    """
    from jig import (
        AgentConfig,
        LLMClient,
        LLMResponse,
        ToolCall,
        ToolRegistry,
        Usage,
        run_agent,
    )

    import scout.replay.experiments as ee
    from scout.dossiers.resolver import (
        DossierFact,
        DossierResolution,
        DossierSummary,
        ResolutionMetadata,
    )
    from scout.replay.pricing import ModelRate, PricingCatalog
    from scout.scanning.schemas import StructuredDraftOutput

    class _FakeLLMClient(LLMClient):  # type: ignore[misc]
        def __init__(self, responses: list[Any], *, model: str) -> None:
            self._responses = list(responses)
            self._model = model

        async def complete(self, params: Any) -> Any:
            if not self._responses:
                raise RuntimeError("reference fixture LLM client exhausted")
            return self._responses.pop(0)

    def _submit_response(args: dict[str, Any]) -> Any:
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id="call-submit", name="submit_output", arguments=args)],
            usage=Usage(input_tokens=100, output_tokens=50, cost=0.001),
            latency_ms=10.0,
            model="scripted",
        )

    phase_run_ids: list[int] = []
    for case_index in range(_EXPERIMENT_CASE_COUNT):
        payload = _experiment_correction_payload(case_index)
        baseline_config = AgentConfig(
            name="scout_reply_draft",
            description="reference fixture baseline",
            system_prompt="You are Scout's reply drafter.",
            llm=_FakeLLMClient([_submit_response(payload)], model=_EXPERIMENT_BASELINE_MODEL),
            feedback=feedback,
            tracer=tracer,
            tools=ToolRegistry([]),
            output_schema=StructuredDraftOutput,
            max_tool_calls=1,
            max_llm_calls=4,
            max_parse_retries=2,
            include_memory_in_prompt=False,
            include_feedback_in_prompt=False,
        )
        baseline_result = await run_agent(baseline_config, "Draft a reply.")
        if baseline_result.parsed is None:
            raise ReferenceGenerationError(
                f"reference fixture baseline run failed to parse: {baseline_result.error}"
            )
        trace_id = baseline_result.trace_id

        scan_id = state.start_scan()
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        reply_draft_snapshot_phase_id = next(
            p.snapshot_phase_id for p in snapshot.phases if p.phase == "reply_draft"
        )
        message = Message(
            platform="discord",
            platform_id=f"reference-fixture-experiment-post-{case_index}",
            channel_name="general",
            channel_id="ch-1",
            author_name="reference-fixture-experiment-author",
            author_id=f"reference-fixture-experiment-author-{case_index}",
            content="reference fixture experiment post",
            created_at=datetime.fromisoformat(_FIXTURE_NOW),
        )
        post_id = state.save_post(message, scan_id)
        phase_run_id = state.insert_phase_run(
            scan_id=scan_id, post_id=post_id,
            snapshot_phase_id=reply_draft_snapshot_phase_id, phase="reply_draft",
            trace_id=trace_id, model=_EXPERIMENT_BASELINE_MODEL, status="complete",
        )
        evaluation_id, _draft_id, _event_id = state.persist_surfaced_outcome(
            RelevanceResult(
                message=message, relevant=True, score=1.0,
                reason="reference fixture experiment case", relevant_to=("gateway",),
            ),
            post_id, scan_id,
            project_key="gateway", author_id=message.author_id, platform="discord",
            comment_text="reference fixture drafted reply text",
            structured_output=canonical_json(payload),
            contributor_phase_run_ids=[phase_run_id],
            dossier_revision=_FIXTURE_DOSSIER_REVISION, dossier_summary_id="gateway-dossier",
            allow_response_only_phase_runs=True,
        )
        state.save_grade_for_migration(
            GradeRecord(
                post_id=post_id, source="migration", graded_at=_FIXTURE_GRADED_AT_DT,
                relevance_judgment="correct", evaluation_id=evaluation_id,
                edited_text=payload["claims"][0],
            ),
            migration_reason="reference fixture experiment summary",
        )
        state.commit()
        phase_run_ids.append(phase_run_id)

    dossier = DossierSummary(
        project_key="gateway",
        last_reviewed=datetime.fromisoformat(_FIXTURE_NOW).date(),
        reviewer="reference-fixture-reviewer",
        facts=[
            DossierFact(
                id="fact-1", text="Reference fixture correction text for case 0.",
                safe_phrasings=[
                    _experiment_correction_payload(i)["claims"][0]
                    for i in range(_EXPERIMENT_CASE_COUNT)
                ],
                immutable_evidence=["reference-fixture-evidence"],
            ),
        ],
        resources=[], prohibitions=[], references=[],
    )

    def _fake_resolve_dossier(
        repository: object, revision: str, project_key: str, dossier_summary_id: str,
        **_kwargs: object,
    ) -> DossierResolution:
        return DossierResolution(
            summary=dossier,
            metadata=ResolutionMetadata(
                project_key=project_key, summary_id=dossier_summary_id,
                revision=revision, path="summaries/gateway.yaml",
            ),
            known_gaps=(),
        )

    candidate_client = _FakeLLMClient(
        [
            _submit_response(_experiment_correction_payload(i))
            for i in range(_EXPERIMENT_CASE_COUNT)
        ],
        model=_EXPERIMENT_CANDIDATE_MODEL,
    )

    def _fake_from_model(model: str, **_kwargs: object) -> _FakeLLMClient:
        if model != _EXPERIMENT_CANDIDATE_MODEL:
            raise ValueError(f"reference fixture has no fake provider for model {model!r}")
        return candidate_client

    catalog = PricingCatalog(
        version=1, as_of="2026-01-01", source_url="https://example.test/reference-fixture-pricing",
        catalog_hash="reference-fixture-pricing-catalog",
        models={
            _EXPERIMENT_BASELINE_MODEL: ModelRate(
                input_usd_per_million=1.0, output_usd_per_million=5.0
            ),
            _EXPERIMENT_CANDIDATE_MODEL: ModelRate(
                input_usd_per_million=3.0, output_usd_per_million=15.0
            ),
        },
    )
    variants = (ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, _EXPERIMENT_CANDIDATE_MODEL, None),)
    selector = ee.BatchSelector.by_phase_run_ids(phase_run_ids)

    with (
        unittest.mock.patch.object(ee, "resolve_dossier", _fake_resolve_dossier),
        unittest.mock.patch.object(ee, "from_model", _fake_from_model),
    ):
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="reference-fixture-batch",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        )
    return outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]


def build_experiment_summary(db_path: Path) -> dict[str, Any]:
    """Read the real batch replay report (replay_reporting.build_batch_report)
    for FIXTURE_EXPERIMENT_RUN_ID out of *db_path*. The batch itself is
    seeded and executed by build_fixture_source_database, not here — this
    only re-derives the report from what is already durably persisted,
    the same way an operator would run `scout eval report` against a real
    database."""
    import scout.replay.reporting as replay_reporting

    with StateManager(str(db_path)) as state:
        return replay_reporting.build_batch_report(
            state, experiment_run_ids=[FIXTURE_EXPERIMENT_RUN_ID]
        )


def redact_experiment_summary(
    report: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Sanitize a real replay_reporting batch report with
    reference-redaction/v1: replay_reporting.build_batch_report already
    never surfaces raw prompt/correction/structured-output text by
    construction (only versioned identity hashes, numeric distances/
    deltas, and cost — see its module docstring), so the only case
    identity left to pseudonymize is phase_run_id, which appears in three
    places. Everything else — segment/model/prompt hashes, scores,
    coverage, uncertainty, and costs — is retained verbatim.
    """
    redactions: list[dict[str, Any]] = []

    def redact(value: object, *, path: str) -> dict[str, Any] | None:
        return redact_value(value, redactions, artifact="experiment-summary.json", path=path)

    redacted = dict(report)

    coverage = report.get("correction_coverage")
    if isinstance(coverage, Mapping):
        redacted_coverage = dict(coverage)
        redacted_coverage["dropped_duplicate_phase_run_ids"] = [
            redact(value, path=f"correction_coverage.dropped_duplicate_phase_run_ids[{i}]")
            for i, value in enumerate(coverage.get("dropped_duplicate_phase_run_ids", []))
        ]
        redacted["correction_coverage"] = redacted_coverage

    redacted["exclusions"] = [
        {
            **item,
            "phase_run_id": redact(
                item.get("phase_run_id"), path=f"exclusions[{i}].phase_run_id"
            ),
        }
        for i, item in enumerate(report.get("exclusions", []))
    ]

    redacted_segments = []
    for segment_index, segment in enumerate(report.get("segments", [])):
        redacted_segment = dict(segment)
        redacted_segment["cases"] = [
            {
                **case,
                "phase_run_id": redact(
                    case.get("phase_run_id"),
                    path=f"segments[{segment_index}].cases[{case_index}].phase_run_id",
                ),
            }
            for case_index, case in enumerate(segment.get("cases", []))
        ]
        redacted_segments.append(redacted_segment)
    redacted["segments"] = redacted_segments

    return redacted, redactions


def verify_experiment_summary_redactions(
    report: Mapping[str, Any], redactions: list[dict[str, Any]]
) -> None:
    """Duplicate/missing/extra cross-check of declared redactions against
    the markers actually embedded in experiment-summary.json."""
    found = {
        (path, str(marker.get("kind")), marker.get("sha256"), marker.get("length"))
        for path, marker in find_redaction_markers(dict(report))
    }
    declared_keys = [
        (str(r.get("path")), str(r.get("kind")), r.get("sha256"), r.get("length"))
        for r in redactions
    ]
    declared = set(declared_keys)
    if len(declared_keys) != len(declared):
        duplicates = sorted({key for key in declared_keys if declared_keys.count(key) > 1})
        raise ReferenceGenerationError(
            f"experiment summary redactions.json declares duplicate entries: {duplicates}"
        )
    if found != declared:
        raise ReferenceGenerationError(
            "experiment summary redaction metadata mismatch: "
            f"missing={sorted(found - declared)} extra={sorted(declared - found)}"
        )


# --- Rendering -------------------------------------------------------------


def render(
    inputs: GenerationInputs,
    *,
    source_db_path: Path,
    before_path: Path = FIXTURE_BEFORE_PATH,
    audit_report_path: Path = FIXTURE_AUDIT_REPORT_PATH,
) -> dict[str, bytes]:
    """Render the complete evidence/paa/reference/ tree as {relative
    posix path: bytes}, entirely from *inputs* and the files at
    *source_db_path*/*before_path*/*audit_report_path* — no other
    non-deterministic input. Never writes to evidence/paa/reference/
    itself; callers decide whether and where to persist the result.
    """
    declaration_versions = validate_declarations_conform(DECLARATIONS_SOURCE_DIR)
    exercise_paa_lifecycle(DECLARATIONS_SOURCE_DIR)

    files: dict[str, bytes] = {}

    for name in DECLARATION_FILENAMES:
        files[f"contracts/paa/{name}"] = (DECLARATIONS_SOURCE_DIR / name).read_bytes()
    files["grading_schema.json"] = GRADING_SCHEMA_SOURCE.read_bytes()

    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp) / "bundle"
        create_reference_bundle(
            report_path=audit_report_path,
            db_path=source_db_path,
            gate_block_id=inputs.gate_block_id,
            before_path=before_path,
            destination=bundle_dir,
            code_revision=inputs.code_revision,
            model_id=inputs.model_id,
            prompt_revision=inputs.prompt_revision,
        )
        verify_bundle(bundle_dir)
        for item in sorted(bundle_dir.rglob("*")):
            if item.is_file():
                # No file tracked anywhere in the repo may be literally
                # named manifest.json/manifest.sha256 — a legacy guard
                # against a hand-maintained dossier-schema mirror (see
                # tests/test_dossier_no_legacy_mirror.py) that also
                # catches these otherwise-unrelated bundle artifact
                # names once they are checked in here. The bundle's own
                # on-disk contract (evidence_bundle.verify_bundle) is
                # untouched — this rename applies only to the copy that
                # lands in the checked-in tree.
                name = _BUNDLE_ARTIFACT_RENAME.get(
                    item.name, item.relative_to(bundle_dir).as_posix()
                )
                files[f"bundle/{name}"] = item.read_bytes()

    correction_and_prompt = read_correction_and_prompt(
        source_db_path, evaluation_id=FIXTURE_CORRECTION_EVALUATION_ID
    )
    redacted_correction_and_prompt, correction_redactions = redact_correction_and_prompt(
        correction_and_prompt
    )
    verify_correction_and_prompt_redactions(redacted_correction_and_prompt, correction_redactions)
    files["correction-and-prompt.json"] = canonical_json(redacted_correction_and_prompt).encode()
    files["correction-and-prompt.redactions.json"] = canonical_json(
        sorted(correction_redactions, key=lambda r: (r["path"],))
    ).encode()

    experiment_summary = build_experiment_summary(source_db_path)
    redacted_experiment_summary, experiment_redactions = redact_experiment_summary(
        experiment_summary
    )
    verify_experiment_summary_redactions(redacted_experiment_summary, experiment_redactions)
    files["experiment-summary.json"] = canonical_json(redacted_experiment_summary).encode()
    files["experiment-summary.redactions.json"] = canonical_json(
        sorted(experiment_redactions, key=lambda r: (r["path"],))
    ).encode()

    manifest = _build_manifest(
        inputs, files=files, declaration_versions=declaration_versions,
        source_db_path=source_db_path, before_path=before_path,
        audit_report_path=audit_report_path,
    )
    files[REFERENCE_MANIFEST_NAME] = canonical_json(manifest).encode()
    return files


def _hash_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _source_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _build_manifest(
    inputs: GenerationInputs,
    *,
    files: Mapping[str, bytes],
    declaration_versions: Mapping[str, int],
    source_db_path: Path,
    before_path: Path,
    audit_report_path: Path,
) -> dict[str, Any]:
    return {
        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
        "generation_inputs": inputs.to_manifest_dict(),
        "artifacts": {name: _hash_bytes(files[name]) for name in sorted(files)},
        "sources": {
            "source_db": {
                "path": FIXTURE_SOURCE_DB_LOGICAL_PATH,
                "sha256": _hash_bytes(
                    canonical_json(dump_fixture_database_logical(source_db_path)).encode()
                ),
            },
            "before": {
                "path": _source_path(before_path),
                "sha256": _hash_bytes(before_path.read_bytes()),
            },
            "audit_report": {
                "path": _source_path(audit_report_path),
                "sha256": _hash_bytes(audit_report_path.read_bytes()),
            },
        },
        "versions": {
            "generator_schema": GENERATOR_SCHEMA_VERSION,
            "git_revision": inputs.git_commit,
            "paa_task_schema": paa_contracts.schema_version("paa-task"),
            "paa_contracts_distribution": importlib.metadata.version("paa-contracts"),
            "paa_runtime_distribution": importlib.metadata.version("paa-runtime"),
            "paa_runtime_event_schema": CURRENT_EVENT_SCHEMA,
            "declarations": dict(sorted(declaration_versions.items())),
            "audit_report_schema": AUDIT_REPORT_SCHEMA_VERSION,
            "bundle_schema": REFERENCE_BUNDLE_SCHEMA_VERSION,
            "redaction_schema": REFERENCE_REDACTION_SCHEMA,
            "experiment_summary_schema": EXPERIMENT_SUMMARY_SCHEMA_VERSION,
            "experiment_statistics_bootstrap": EXPERIMENT_STATISTICS_BOOTSTRAP_VERSION,
            "experiment_assembler": DRAFT_TEXT_ASSEMBLER_VERSION,
            "experiment_grader": NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
            "experiment_pricing_catalog": PRICING_CATALOG_VERSION,
        },
    }


# --- Write / check -----------------------------------------------------


def write_reference_tree(files: Mapping[str, bytes], *, target_dir: Path = REFERENCE_DIR) -> None:
    """Atomically replace *target_dir* with the contents of *files*.

    Renders into a fresh sibling temporary directory first, then swaps it
    in with two directory renames (move the old tree aside, move the new
    tree into place, discard the old one) — a reader never observes a
    partially written tree, and a crash between the two renames leaves
    either the complete old tree or the complete new one, never a mix.
    """
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f".{target_dir.name}-tmp-"))
    try:
        for rel_path, content in files.items():
            dest = tmp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        if target_dir.exists():
            old_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f".{target_dir.name}-old-"))
            old_dir.rmdir()
            os.replace(target_dir, old_dir)
            try:
                os.replace(tmp_dir, target_dir)
            except BaseException:
                os.replace(old_dir, target_dir)
                raise
            shutil.rmtree(old_dir, ignore_errors=True)
        else:
            os.replace(tmp_dir, target_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def check_reference_tree(*, target_dir: Path = REFERENCE_DIR) -> list[str]:
    """Re-render from the checked-in manifest's own recorded generation
    inputs, entirely under a temporary directory, and diff byte-for-byte
    against *target_dir*. Returns a list of problems (empty means up to
    date). Never writes to *target_dir*, its parents, or any repository
    file — the fixture source database is rebuilt fresh in a temporary
    directory on every call.
    """
    manifest_path = target_dir / REFERENCE_MANIFEST_NAME
    if not manifest_path.is_file():
        return [
            f"{target_dir} does not exist or has no {REFERENCE_MANIFEST_NAME}; "
            "run `uv run python scripts/generate_paa/reference_evidence.py --write`"
        ]
    try:
        manifest = json.loads(manifest_path.read_text())
        inputs = GenerationInputs.from_manifest_dict(manifest["generation_inputs"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        return [f"checked-in {REFERENCE_MANIFEST_NAME} is unreadable: {exc}"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_db_path = tmp_path / "source.db"
        build_fixture_source_database(source_db_path)
        try:
            rendered = render(inputs, source_db_path=source_db_path)
        except ReferenceGenerationError as exc:
            return [str(exc)]

    checked_in: dict[str, bytes] = {}
    for item in sorted(target_dir.rglob("*")):
        if item.is_file():
            checked_in[item.relative_to(target_dir).as_posix()] = item.read_bytes()

    problems: list[str] = []
    missing = sorted(set(rendered) - set(checked_in))
    extra = sorted(set(checked_in) - set(rendered))
    if missing or extra:
        problems.append(f"file set drift: missing={missing} extra={extra}")
    for rel_path in sorted(set(rendered) & set(checked_in)):
        if rendered[rel_path] != checked_in[rel_path]:
            problems.append(f"byte drift: {rel_path}")
    return problems
