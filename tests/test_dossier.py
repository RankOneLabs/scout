"""Tests for the dossier loading, validation, and revision helpers.

``resolve_dossier`` now retrieves and validates the pinned dossier-source
schemas via ``git show`` before any semantic check runs, so a synthetic
temp-repo fixture without a real schema copy cannot exercise its success
path — that would recreate exactly the schema-mirror drift this contract
change eliminates (see tests/test_dossier_conformance.py for the pinned,
CI-gated integration coverage of the full schema+semantic pipeline).

This file instead covers:
  - fail-closed behavior that does not require real schemas (revision
    format, dirty checkout, unresolvable revision, missing schema file);
  - Scout's own identity/readiness/cross-reference logic via
    ``dossier._build_resolution``, a pure function operating on an
    already-parsed, already-schema-validated document — exercised directly
    against dicts and against tests/fixtures/dossier_source's real dossier
    shapes without needing git or a schema copy at all;
  - pydantic model construction.
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from scout.dossiers.resolver import (
    DOSSIER_SOURCE_PIN_PATH,
    DossierFact,
    DossierProhibition,
    DossierResolutionError,
    DossierResource,
    DossierSummary,
    _build_resolution,
    _is_v1_index_version,
    get_dossier_revision,
    get_pinned_dossier_revision,
    resolve_dossier,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "dossier_source"


@pytest.mark.parametrize("index", ["version: 1.0.0\n", "version: 1.0.0\nentries: []\n"])
def test_missing_or_invalid_index_entries_raise_boundary_error(
    monkeypatch: pytest.MonkeyPatch, index: str
) -> None:
    from unittest.mock import Mock

    revision = "b" * 40
    monkeypatch.setattr("scout.dossiers.resolver.subprocess.run", Mock(side_effect=[
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, revision + "\n", ""),
    ]))
    # A permissive but well-formed pinned schema must not leak Python exceptions.
    monkeypatch.setattr("scout.dossiers.resolver._git_show", Mock(side_effect=[
        '{"$id":"https://synthetic.test/schemas/index.v1.schema.json","type":"object"}',
        '{"$id":"https://synthetic.test/schemas/summary.v1.schema.json","type":"object"}',
        index,
    ]))
    with pytest.raises(DossierResolutionError, match="entries must be a mapping"):
        resolve_dossier("/synthetic", revision, "synthetic", "summary")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.0.0", True),
        (1, False),
        (True, False),
        ("1", False),
        ("1.0.1", False),
        (2, False),
    ],
)
def test_v1_index_version_spellings(value: object, expected: bool) -> None:
    assert _is_v1_index_version(value) is expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo and commit all current files."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    for key, value in (
        ("user.email", "test@test.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "config", key, value],
            cwd=path,
            check=True,
            capture_output=True,
        )
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _minimal_document(**overrides: Any) -> dict[str, Any]:
    """A minimal, schema-shaped summary document for _build_resolution tests."""
    doc: dict[str, Any] = {
        "id": "gateway-dossier",
        "type": "summary",
        "dossier": {
            "project_key": "gateway",
            "last_reviewed": date.today().isoformat(),
            "reviewer": {"id": "reviewer", "display_name": "Reviewer"},
            "evidence": [
                {
                    "id": "evidence-source", "kind": "git-blob",
                    "locator": "repository:summary.yaml", "immutable_ref": "a" * 40,
                },
            ],
            "facts": [{
                "id": "fact-gateway", "claim": "Gateway is planned.", "status": "planned",
                "safe_phrasings": ["Gateway is planned."], "evidence_ids": ["evidence-source"],
            }],
            "resources": [{
                "id": "res-gateway", "label": "Gateway", "purpose": "Primary reference",
                "canonical_url": "https://example.com/gateway",
                "evidence_ids": ["evidence-source"],
            }],
            "prohibitions": [{
                "id": "proh-live", "normalized_phrase": "already live",
                "forbidden_phrasings": ["Gateway is already live."],
                "evidence_ids": ["evidence-source"], "resource_ids": ["res-gateway"],
            }],
            "known_gaps": [{
                "id": "gap-timeline", "question": "What is the timeline?",
                "related_fact_ids": ["fact-gateway"], "related_resource_ids": ["res-gateway"],
            }],
        },
    }
    doc.update(overrides)
    return doc


def _resolve_minimal(document: dict[str, Any], **kwargs: Any) -> Any:
    return _build_resolution(
        document, "gateway", "gateway-dossier", "a" * 40, "summaries/gateway.yaml", **kwargs
    )


# ---------------------------------------------------------------------------
# resolve_dossier: fail-closed behavior that does not require real schemas
# ---------------------------------------------------------------------------


def test_resolve_dossier_rejects_short_revision(tmp_path: Path) -> None:
    # Revision-format is checked before any git subprocess call, so the
    # directory need not even be a git repo.
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    with pytest.raises(DossierResolutionError, match="full 40-character lowercase SHA"):
        resolve_dossier(repo, "abc123", "gateway", "gateway-dossier")


def test_resolve_dossier_rejects_unresolvable_revision(tmp_path: Path) -> None:
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1.0.0\nentries: {}\n")
    _init_git_repo(repo)
    with pytest.raises(DossierResolutionError, match="not a resolvable full commit"):
        resolve_dossier(repo, "f" * 40, "gateway", "gateway-dossier")


def test_resolve_dossier_rejects_dirty_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1.0.0\nentries: {}\n")
    _init_git_repo(repo)
    revision = get_dossier_revision(repo)
    (repo / "scratch.txt").write_text("uncommitted")
    with pytest.raises(DossierResolutionError, match="uncommitted changes"):
        resolve_dossier(repo, revision, "gateway", "gateway-dossier")


def test_resolve_dossier_fails_closed_when_schemas_absent(tmp_path: Path) -> None:
    """No schemas/*.v1.schema.json in the repo at all — must fail closed
    rather than falling back to any local or bundled schema."""
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text(yaml.safe_dump({"version": "1.0.0", "entries": {}}))
    _init_git_repo(repo)
    revision = get_dossier_revision(repo)
    with pytest.raises(DossierResolutionError, match="schemas/index.v1.schema.json"):
        resolve_dossier(repo, revision, "gateway", "gateway-dossier")


def test_resolve_dossier_fails_closed_on_wrong_schema_id(tmp_path: Path) -> None:
    """A schema file present but declaring the wrong (or no) $id must be
    rejected — the pinned contract is identified by its versioned $id path,
    not by the file merely existing."""
    repo = tmp_path / "dossier-source"
    (repo / "schemas").mkdir(parents=True)
    (repo / "schemas" / "index.v1.schema.json").write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
        '"$id": "https://example.com/not-the-real-id", "type": "object"}'
    )
    (repo / "index.yaml").write_text(yaml.safe_dump({"version": "1.0.0", "entries": {}}))
    _init_git_repo(repo)
    revision = get_dossier_revision(repo)
    with pytest.raises(DossierResolutionError, match=r"must declare \$id"):
        resolve_dossier(repo, revision, "gateway", "gateway-dossier")


# ---------------------------------------------------------------------------
# get_dossier_revision
# ---------------------------------------------------------------------------


def test_get_dossier_revision_valid_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "readme.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-c", "user.email=t@t.com", "-c", "user.name=T",
            "-c", "commit.gpgsign=false", "commit", "-m", "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    revision = get_dossier_revision(repo)
    assert len(revision) == 40
    assert all(c in "0123456789abcdef" for c in revision)


def test_get_dossier_revision_non_git_dir(tmp_path: Path) -> None:
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    with pytest.raises(RuntimeError, match="git rev-parse HEAD failed"):
        get_dossier_revision(non_git)


def test_get_dossier_revision_rejects_dirty_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)

    (repo / "index.yaml").write_text("version: 1\n# uncommitted\n")

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        get_dossier_revision(repo)


def test_get_dossier_revision_rejects_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "untracked-repo"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)

    (repo / "scratch.txt").write_text("not committed")

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        get_dossier_revision(repo)


# ---------------------------------------------------------------------------
# _build_resolution — Scout's identity/readiness/cross-reference layer,
# exercised directly on already-schema-validated documents.
# ---------------------------------------------------------------------------


def test_build_resolution_happy_path() -> None:
    result = _resolve_minimal(_minimal_document(), max_age_days=90, min_entries=2)
    assert result.metadata.summary_id == "gateway-dossier"
    assert result.summary.facts[0].text == "Gateway is planned."
    assert result.summary.resources[0].label == "Gateway"
    assert result.summary.prohibitions[0].id == "proh-live"
    assert result.summary.prohibitions[0].mode == "normalized_phrase"
    assert result.known_gaps == ("What is the timeline?",)


def test_build_resolution_rejects_summary_id_mismatch() -> None:
    doc = _minimal_document(id="some-other-id")
    with pytest.raises(DossierResolutionError, match="document id does not match"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_project_key_mismatch() -> None:
    doc = _minimal_document()
    doc["dossier"]["project_key"] = "not-gateway"
    with pytest.raises(DossierResolutionError, match="project_key does not match"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_stale_review(tmp_path: Path) -> None:
    doc = _minimal_document()
    doc["dossier"]["last_reviewed"] = (date.today() - timedelta(days=200)).isoformat()
    with pytest.raises(DossierResolutionError, match="review age"):
        _resolve_minimal(doc, max_age_days=90, min_entries=0)


def test_build_resolution_rejects_future_review_date() -> None:
    doc = _minimal_document()
    doc["dossier"]["last_reviewed"] = (date.today() + timedelta(days=5)).isoformat()
    with pytest.raises(DossierResolutionError, match="review age"):
        _resolve_minimal(doc, max_age_days=90, min_entries=0)


def test_build_resolution_enforces_min_entries() -> None:
    doc = _minimal_document()
    with pytest.raises(DossierResolutionError, match="minimum fact/resource entries"):
        _resolve_minimal(doc, max_age_days=None, min_entries=5)


def test_build_resolution_rejects_broken_evidence_reference() -> None:
    doc = _minimal_document()
    doc["dossier"]["facts"][0]["evidence_ids"] = ["evidence-does-not-exist"]
    with pytest.raises(DossierResolutionError, match="scout.dossier.broken-evidence-reference"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_broken_resource_reference_in_prohibition() -> None:
    doc = _minimal_document()
    doc["dossier"]["prohibitions"][0]["resource_ids"] = ["res-does-not-exist"]
    with pytest.raises(DossierResolutionError, match="scout.dossier.broken-resource-reference"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_broken_gap_fact_reference() -> None:
    doc = _minimal_document()
    doc["dossier"]["known_gaps"][0]["related_fact_ids"] = ["fact-does-not-exist"]
    with pytest.raises(DossierResolutionError, match="scout.dossier.broken-gap-fact-reference"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_broken_gap_resource_reference() -> None:
    doc = _minimal_document()
    doc["dossier"]["known_gaps"][0]["related_resource_ids"] = ["res-does-not-exist"]
    with pytest.raises(DossierResolutionError, match="scout.dossier.broken-gap-resource-reference"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_duplicate_id_across_record_types() -> None:
    """Global id uniqueness, matching the producer's own semantic pass: a
    resource id colliding with a fact id is still a duplicate."""
    doc = _minimal_document()
    doc["dossier"]["resources"][0]["id"] = "fact-gateway"
    with pytest.raises(DossierResolutionError, match="duplicate dossier id"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_nonportable_regex() -> None:
    doc = _minimal_document()
    doc["dossier"]["prohibitions"][0] = {
        "id": "proh-bad-regex", "regex": "\\d+ uptime", "flags": "i",
        "forbidden_phrasings": ["100% uptime"], "evidence_ids": ["evidence-source"],
    }
    with pytest.raises(DossierResolutionError, match="prohibition regex rejected"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_duplicate_canonical_url() -> None:
    """scout.dossiers.contract.validate_dossier_semantics's duplicate-canonical-url
    check, previously enforced only in the conformance corpus runner, now
    fails closed in live resolution too."""
    doc = _minimal_document()
    doc["dossier"]["resources"].append({
        "id": "res-gateway-2", "label": "Gateway Mirror", "purpose": "Mirror",
        "canonical_url": "https://example.com/gateway",
        "evidence_ids": ["evidence-source"],
    })
    with pytest.raises(DossierResolutionError, match="scout.dossier.duplicate-canonical-url"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_rejects_future_last_reviewed_without_max_age_days() -> None:
    """Unlike the age-window check (which only fires when max_age_days is
    set), dossier-source's future-last-reviewed rule always applies."""
    doc = _minimal_document()
    doc["dossier"]["last_reviewed"] = (date.today() + timedelta(days=5)).isoformat()
    with pytest.raises(DossierResolutionError, match="scout.dossier.future-last-reviewed"):
        _resolve_minimal(doc, max_age_days=None, min_entries=0)


def test_build_resolution_accepts_portable_regex_with_flags() -> None:
    doc = _minimal_document()
    doc["dossier"]["prohibitions"][0] = {
        "id": "proh-regex", "regex": "guarantee[sd]? uptime", "flags": "im",
        "forbidden_phrasings": ["We guarantee uptime"], "evidence_ids": ["evidence-source"],
    }
    result = _resolve_minimal(doc, max_age_days=None, min_entries=0)
    prohibition = result.summary.prohibitions[0]
    assert prohibition.mode == "regex"
    assert prohibition.pattern == "guarantee[sd]? uptime"
    assert prohibition.flags == "im"


# ---------------------------------------------------------------------------
# tests/fixtures/dossier_source — real dossier shapes exercised through
# _build_resolution directly (no schema copy, so no conformance claim; see
# tests/test_dossier_conformance.py for the pinned, schema-validating path).
# ---------------------------------------------------------------------------


def test_dossier_source_fixture_resolves_all_indexed_summaries() -> None:
    index = yaml.safe_load((_FIXTURE_ROOT / "index.yaml").read_text())
    assert index["version"] == "1.0.0"
    for entry in index["entries"].values():
        assert set(entry) == {"path", "type"}
    for summary_id, entry in index["entries"].items():
        document = yaml.safe_load((_FIXTURE_ROOT / entry["path"]).read_text())
        project_key = document["dossier"]["project_key"]
        result = _build_resolution(
            document, project_key, summary_id, "a" * 40, entry["path"],
            max_age_days=None, min_entries=0,
        )
        assert result.metadata.summary_id == summary_id


def test_agent_evals_and_agent_ops_fixtures_clear_min_entries_with_headroom() -> None:
    """The two real dossiers introduced alongside schema v18 must each clear
    SCOUT_DOSSIER_MIN_ENTRIES (20) with headroom, using their real
    fact+resource totals rather than a fabricated round number."""
    index = yaml.safe_load((_FIXTURE_ROOT / "index.yaml").read_text())
    for summary_id in ("agent-evals-dossier", "agent-ops-dossier"):
        entry = index["entries"][summary_id]
        document = yaml.safe_load((_FIXTURE_ROOT / entry["path"]).read_text())
        project_key = document["dossier"]["project_key"]
        result = _build_resolution(
            document, project_key, summary_id, "a" * 40, entry["path"],
            max_age_days=None, min_entries=0,
        )
        total_entries = len(result.summary.facts) + len(result.summary.resources)
        assert total_entries > 20


def test_dossier_source_fixture_rejects_mutated_invalid_evidence() -> None:
    """A minimal regression that resolve_dossier's semantic layer still
    fails closed on structurally-broken evidence, using a real fixture
    document as the base (mirrors the old inline-mutation test)."""
    index = yaml.safe_load((_FIXTURE_ROOT / "index.yaml").read_text())
    entry = index["entries"]["gateway-dossier"]
    document = copy.deepcopy(yaml.safe_load((_FIXTURE_ROOT / entry["path"]).read_text()))
    document["dossier"]["facts"][0]["evidence_ids"] = ["evidence-does-not-exist"]
    with pytest.raises(DossierResolutionError, match="scout.dossier.broken-evidence-reference"):
        _build_resolution(
            document, "gateway", "gateway-dossier", "a" * 40, entry["path"],
            max_age_days=None, min_entries=0,
        )


# ---------------------------------------------------------------------------
# Model construction smoke tests
# ---------------------------------------------------------------------------


class TestDossierModels:
    def test_dossier_fact(self) -> None:
        fact = DossierFact(
            id="f1",
            text="We detect bots using ML.",
            safe_phrasings=["We detect bots using ML."],
            immutable_evidence=["doi:10.1234/abc"],
        )
        assert fact.id == "f1"
        assert fact.safe_phrasings == ["We detect bots using ML."]

    def test_dossier_resource(self) -> None:
        resource = DossierResource(
            id="r1",
            label="Gateway Docs",
            canonical_url="https://gateway.example.com/docs",
            immutable_evidence=["commit:abc123"],
        )
        assert resource.id == "r1"
        assert resource.canonical_url == "https://gateway.example.com/docs"

    def test_dossier_prohibition(self) -> None:
        prohibition = DossierProhibition(
            id="proh-1",
            mode="exact_phrase",
            pattern="100% accurate",
            immutable_evidence=["internal:policy-2024"],
        )
        assert prohibition.id == "proh-1"
        assert prohibition.mode == "exact_phrase"
        assert prohibition.pattern == "100% accurate"
        assert prohibition.flags == ""

    def test_dossier_prohibition_carries_authored_flags(self) -> None:
        prohibition = DossierProhibition(
            id="proh-2",
            mode="regex",
            pattern="guarantee[sd]? uptime",
            flags="im",
            immutable_evidence=["internal:policy-2024"],
        )
        assert prohibition.flags == "im"

    def test_dossier_summary(self) -> None:
        summary = DossierSummary(
            project_key="gateway",
            last_reviewed=date.fromisoformat("2026-01-15"),
            reviewer="alice",
            facts=[
                DossierFact(
                    id="f1",
                    text="We detect bots using ML.",
                    safe_phrasings=["We detect bots using ML."],
                    immutable_evidence=["doi:10.1234/abc"],
                )
            ],
            resources=[
                DossierResource(
                    id="r1",
                    label="Gateway Docs",
                    canonical_url="https://gateway.example.com/docs",
                    immutable_evidence=["commit:abc123"],
                )
            ],
            prohibitions=[
                DossierProhibition(
                    id="proh-1",
                    mode="exact_phrase",
                    pattern="100% accurate",
                    immutable_evidence=["internal:policy-2024"],
                )
            ],
            references=[],
        )
        assert summary.project_key == "gateway"
        assert summary.reviewer == "alice"
        assert len(summary.facts) == 1
        assert len(summary.resources) == 1
        assert len(summary.prohibitions) == 1


# ---------------------------------------------------------------------------
# get_pinned_dossier_revision — the revision runtime resolution actually uses
# ---------------------------------------------------------------------------


def _write_pin(tmp_path: Path, revision: str) -> Path:
    pin = tmp_path / "dossier-source-v1.json"
    pin.write_text(json.dumps({
        "repository": "external/dossier-source",
        "revision": revision,
        "corpus_path": "conformance/v1",
        "summary_schema_path": "schemas/summary.v1.schema.json",
        "index_schema_path": "schemas/index.v1.schema.json",
    }))
    return pin


def test_pinned_revision_is_the_pin_not_the_checkout_head(tmp_path: Path) -> None:
    """The whole point: HEAD moves, what Scout reads does not.

    A second commit stands in for the producer advancing its own checkout —
    which it owns and does for its own reasons. Scout must keep resolving
    at the revision it pinned and tested against.
    """
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)
    pinned = get_dossier_revision(repo)

    (repo / "index.yaml").write_text("version: 1\n# producer moved on\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", "commit", "-m", "second"],
        cwd=repo, check=True, capture_output=True,
    )
    head = get_dossier_revision(repo)
    assert head != pinned

    resolved = get_pinned_dossier_revision(repo, pin_path=_write_pin(tmp_path, pinned))
    assert resolved == pinned


def test_a_dirty_checkout_is_accepted(tmp_path: Path) -> None:
    # Deliberate divergence from get_dossier_revision. Under a pin the
    # working tree is expected to differ from what Scout reads, and `git
    # show` reads objects rather than the tree — so refusing a dirty
    # checkout would reject a state that is now entirely normal.
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)
    pinned = get_dossier_revision(repo)

    (repo / "index.yaml").write_text("version: 1\n# uncommitted edit\n")
    (repo / "untracked.txt").write_text("scratch")

    assert get_pinned_dossier_revision(
        repo, pin_path=_write_pin(tmp_path, pinned)
    ) == pinned


def test_a_pinned_commit_absent_from_the_checkout_is_named(tmp_path: Path) -> None:
    # What a shallow or stale clone produces. Worth its own message: the
    # symptom is otherwise a confusing "path does not exist at revision"
    # from deep inside resolution.
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)

    absent = "0" * 40
    with pytest.raises(RuntimeError, match="is not present in"):
        get_pinned_dossier_revision(repo, pin_path=_write_pin(tmp_path, absent))


def test_a_malformed_pin_revision_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)

    with pytest.raises(RuntimeError, match="no valid 40-character revision"):
        get_pinned_dossier_revision(repo, pin_path=_write_pin(tmp_path, "main"))


def test_a_missing_pin_file_is_named(tmp_path: Path) -> None:
    repo = tmp_path / "dossier-source"
    repo.mkdir()
    (repo / "index.yaml").write_text("version: 1\n")
    _init_git_repo(repo)

    with pytest.raises(RuntimeError, match="pin unreadable"):
        get_pinned_dossier_revision(repo, pin_path=tmp_path / "nope.json")


def test_scouts_real_pin_is_well_formed() -> None:
    # The shipped file, not a fixture. A pin that cannot be parsed would
    # take every scan down, and this is the cheapest place to notice.
    pin = json.loads(DOSSIER_SOURCE_PIN_PATH.read_text(encoding="utf-8"))
    assert len(pin["revision"]) == 40
    assert all(c in "0123456789abcdef" for c in pin["revision"])
