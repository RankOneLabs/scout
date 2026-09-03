"""Tests for paa_reference_evidence.py.

Exercises the generator against the checked-in fixture source
(tests/fixtures/paa_reference/source/), proving: determinism across two
runs, byte-for-byte equality of copied declarations/grading schema
against their live sources, that no seeded sentinel for any prohibited
data class survives into rendered output, that the redacted bundle
verifies (and rejects tampering), and that --check accepts the checked-in
evidence/paa/reference/ tree without ever writing to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scout.paa.evidence.bundle import BundleError, verify_bundle
from scout.paa.reference_evidence import (
    DECLARATIONS_SOURCE_DIR,
    FIXTURE_AUDIT_REPORT_PATH,
    FIXTURE_BEFORE_PATH,
    FIXTURE_CORRECTION_EVALUATION_ID,
    FIXTURE_SOURCE_DB_LOGICAL_PATH,
    GRADING_SCHEMA_SOURCE,
    REFERENCE_DIR,
    REFERENCE_MANIFEST_NAME,
    REPO_ROOT,
    SENTINELS,
    GenerationInputs,
    build_fixture_source_database,
    check_reference_tree,
    default_generation_inputs,
    exercise_paa_lifecycle,
    render,
    resolve_git_commit,
    validate_declarations_conform,
    write_reference_tree,
)

EXPECTED_DIR = Path(__file__).parent / "fixtures" / "paa_reference" / "expected"


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "source.db"
    build_fixture_source_database(db_path)
    return db_path


@pytest.fixture
def rendered(source_db: Path) -> dict[str, bytes]:
    return render(default_generation_inputs(), source_db_path=source_db)


class TestDeterminism:
    def test_two_renders_of_the_same_inputs_are_byte_identical(
        self, tmp_path: Path, rendered: dict[str, bytes]
    ) -> None:
        other_db = tmp_path / "source2.db"
        build_fixture_source_database(other_db)
        again = render(default_generation_inputs(), source_db_path=other_db)
        assert again == rendered


class TestCopiedSources:
    def test_declarations_are_byte_for_byte_equal_to_contracts_paa(
        self, rendered: dict[str, bytes]
    ) -> None:
        for name in (
            "canonical_promotion.v1.yaml",
            "inbound_reply_surfacing.v1.yaml",
        ):
            expected = (DECLARATIONS_SOURCE_DIR / name).read_bytes()
            assert rendered[f"contracts/paa/{name}"] == expected

    def test_grading_schema_is_byte_for_byte_equal_to_web_grading_schema(
        self, rendered: dict[str, bytes]
    ) -> None:
        assert rendered["grading_schema.json"] == GRADING_SCHEMA_SOURCE.read_bytes()


class TestSentinelAbsence:
    def test_no_seeded_sentinel_survives_into_any_rendered_file(
        self, rendered: dict[str, bytes]
    ) -> None:
        haystack = b"\n".join(rendered.values())
        leaked = [name for name, value in SENTINELS.items() if value.encode() in haystack]
        assert leaked == []

    def test_sentinel_set_is_non_trivial(self) -> None:
        # Guards the test above against a future refactor accidentally
        # emptying SENTINELS, which would make "no sentinel leaked" vacuous.
        assert len(SENTINELS) >= 10


class TestManifest:
    def test_manifest_declares_every_other_file_exactly_once(
        self, rendered: dict[str, bytes]
    ) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        declared = manifest["artifacts"]
        expected = set(rendered) - {REFERENCE_MANIFEST_NAME}
        assert set(declared) == expected
        assert len(declared) == len(expected)

    def test_every_declared_artifact_hash_verifies(self, rendered: dict[str, bytes]) -> None:
        import hashlib

        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        for name, expected_hash in manifest["artifacts"].items():
            assert hashlib.sha256(rendered[name]).hexdigest() == expected_hash

    def test_recorded_versions_match_installed_declarations(
        self, rendered: dict[str, bytes]
    ) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        declaration_versions = validate_declarations_conform(DECLARATIONS_SOURCE_DIR)
        assert manifest["versions"]["declarations"] == declaration_versions


def _independent_sha256_hex(data: bytes) -> str:
    """A from-scratch sha256 computation — deliberately not routed through
    any paa_reference_evidence helper — so a test using this cannot be
    fooled by a bug shared between the generator and its own check."""
    import hashlib as _hashlib

    return _hashlib.sha256(data).hexdigest()


def _independent_canonical_json(value: object) -> str:
    """Reimplements phase1_audit.canonical_json's exact convention (sorted
    keys, compact separators, one trailing newline) from scratch, rather
    than importing it, so tests using this stay independent of the
    module under test."""
    import json as _json

    return _json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def _independent_fixture_dump_hash(db_path: Path) -> str:
    """Re-derive the fixture database's logical-dump hash via a
    hand-written query, deliberately not calling
    dump_fixture_database_logical — a genuinely separate implementation
    of "every table, every row, minus wall-clock/random-id columns", so
    this test cannot pass merely because both sides share a bug."""
    import hashlib as _hashlib
    import json as _json
    import sqlite3 as _sqlite3

    excluded_suffix = "_at"
    excluded_names = {
        "trace_id", "root_trace_id", "candidate_trace_id", "trace_a_id", "trace_b_id",
        "trace_diff", "idempotency_key", "as_of",
    }
    conn = _sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        dump = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
            dump[table] = [
                {
                    k: v
                    for k, v in dict(row).items()
                    if not k.endswith(excluded_suffix) and k not in excluded_names
                }
                for row in rows
            ]
    finally:
        conn.close()
    # Matches phase1_audit.canonical_json's exact convention (sorted keys,
    # compact separators, one trailing newline) — reimplemented here
    # rather than imported, so this stays an independent computation.
    canonical = _json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return _hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestSourceHashes:
    """Every manifest["sources"] entry names an explicit path and a hash —
    verified here by an independent computation, not by re-calling the
    generator's own hashing code."""

    def test_sources_declare_explicit_paths(self, rendered: dict[str, bytes]) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        sources = manifest["sources"]
        assert sources["source_db"]["path"] == FIXTURE_SOURCE_DB_LOGICAL_PATH
        assert sources["before"]["path"] == "tests/fixtures/paa_reference/source/before.bin"
        assert (
            sources["audit_report"]["path"]
            == "tests/fixtures/paa_reference/source/audit_report.json"
        )

    def test_before_hash_matches_an_independent_read(self, rendered: dict[str, bytes]) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        expected = _independent_sha256_hex(FIXTURE_BEFORE_PATH.read_bytes())
        assert manifest["sources"]["before"]["sha256"] == expected

    def test_audit_report_hash_matches_an_independent_read(
        self, rendered: dict[str, bytes]
    ) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        expected = _independent_sha256_hex(FIXTURE_AUDIT_REPORT_PATH.read_bytes())
        assert manifest["sources"]["audit_report"]["sha256"] == expected

    def test_source_db_hash_matches_an_independent_logical_dump(
        self, rendered: dict[str, bytes], source_db: Path
    ) -> None:
        manifest = json.loads(rendered[REFERENCE_MANIFEST_NAME])
        expected = _independent_fixture_dump_hash(source_db)
        assert manifest["sources"]["source_db"]["sha256"] == expected

    def test_source_db_hash_changes_when_a_seeded_value_changes(self, tmp_path: Path) -> None:
        """The hash must be bound to real content, not a static label —
        prove it actually reacts to the database differing."""
        db_a = tmp_path / "a.db"
        build_fixture_source_database(db_a)
        hash_a = _independent_fixture_dump_hash(db_a)

        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(db_a)
        conn.execute(
            "UPDATE posts SET content = 'a different post' WHERE id = 1",
        )
        conn.commit()
        conn.close()
        hash_b = _independent_fixture_dump_hash(db_a)
        assert hash_a != hash_b

    def test_independent_read_confirms_the_seeded_correction_and_prompt(
        self, source_db: Path
    ) -> None:
        """A content-level independent check (not just a hash comparison):
        directly query the fixture database for the exact sentinel values
        read_correction_and_prompt/redact_correction_and_prompt are
        supposed to redact."""
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(source_db)
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            """SELECT pk.evaluate_prompt, rdr.reply_text
                 FROM evaluations e
                 JOIN project_keywords pk ON pk.id = e.keyword_route_id
                 JOIN grades g ON g.evaluation_id = e.id
                 JOIN reply_draft_revisions rdr ON rdr.id = g.reply_revision_id
                WHERE e.id = ?""",
            (FIXTURE_CORRECTION_EVALUATION_ID,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["evaluate_prompt"] == SENTINELS["prompt_text"]
        assert row["reply_text"] == SENTINELS["correction_text"]


def _write_bundle_dir(rendered: dict[str, bytes], bundle_dir: Path) -> None:
    """Write the rendered bundle/* entries into *bundle_dir*, restoring
    the bundle's real manifest.json/manifest.sha256 names (renamed to
    bundle-manifest.* only for git tracking — see
    paa_reference_evidence._BUNDLE_ARTIFACT_RENAME) so evidence_bundle
    .verify_bundle recognizes it."""
    restore = {"bundle-manifest.json": "manifest.json", "bundle-manifest.sha256": "manifest.sha256"}
    for name, content in rendered.items():
        if not name.startswith("bundle/"):
            continue
        rel = name[len("bundle/") :]
        dest = bundle_dir / restore.get(rel, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


class TestRedactedBundle:
    def test_generated_bundle_verifies(self, rendered: dict[str, bytes]) -> None:
        bundle_files = {
            name[len("bundle/") :]: content
            for name, content in rendered.items()
            if name.startswith("bundle/")
        }
        assert bundle_files
        assert json.loads(bundle_files["bundle-manifest.json"])["schema_version"] == 2

    def test_write_then_verify_bundle_from_disk(
        self, tmp_path: Path, rendered: dict[str, bytes]
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        _write_bundle_dir(rendered, bundle_dir)
        result = verify_bundle(bundle_dir)
        assert result["ok"]

    def test_tampering_with_a_redacted_artifact_is_rejected(
        self, tmp_path: Path, rendered: dict[str, bytes]
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        _write_bundle_dir(rendered, bundle_dir)
        (bundle_dir / "source.json").write_bytes(b"{}")
        with pytest.raises(BundleError, match="artifact hash mismatch"):
            verify_bundle(bundle_dir)

    def test_dropping_a_redaction_entry_is_rejected(
        self, tmp_path: Path, rendered: dict[str, bytes]
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        _write_bundle_dir(rendered, bundle_dir)
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        redactions = json.loads((bundle_dir / "redactions.json").read_text())
        (bundle_dir / "redactions.json").write_text(_independent_canonical_json(redactions[1:]))
        manifest["artifacts"]["redactions.json"] = _independent_sha256_hex(
            (bundle_dir / "redactions.json").read_bytes()
        )
        (bundle_dir / "manifest.json").write_text(_independent_canonical_json(manifest))
        (bundle_dir / "manifest.sha256").write_text(
            _independent_sha256_hex((bundle_dir / "manifest.json").read_bytes())
            + "  manifest.json\n"
        )
        with pytest.raises(BundleError, match="redaction metadata mismatch"):
            verify_bundle(bundle_dir)


class TestGitCommitPinning:
    """`--write` must pin the real commit, never a placeholder — see
    scripts/generate_paa_reference_evidence.py and
    docs/runbooks/paa-operations.md."""

    def test_default_inputs_use_a_placeholder_not_a_real_commit(self) -> None:
        # default_generation_inputs stays a pure, git-independent function
        # (so tests/--check reproduce regardless of the ambient checkout);
        # its placeholder must never be mistaken for a real pin.
        assert default_generation_inputs().git_commit == "0" * 40

    def test_resolve_git_commit_matches_an_independent_git_rev_parse(self) -> None:
        import subprocess

        expected = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert resolve_git_commit(REPO_ROOT) == expected

    def test_resolve_git_commit_is_forty_lowercase_hex_chars(self) -> None:
        commit = resolve_git_commit(REPO_ROOT)
        assert len(commit) == 40
        assert all(c in "0123456789abcdef" for c in commit)


class TestExpectedGoldenFixture:
    def test_render_matches_the_checked_in_expected_tree(self, rendered: dict[str, bytes]) -> None:
        golden = {
            str(p.relative_to(EXPECTED_DIR)): p.read_bytes()
            for p in EXPECTED_DIR.rglob("*")
            if p.is_file()
        }
        assert set(rendered) == set(golden)
        for name in rendered:
            assert rendered[name] == golden[name], f"{name} differs from the checked-in golden copy"


class TestGenerationInputsRoundTrip:
    def test_manifest_dict_round_trips(self) -> None:
        inputs = default_generation_inputs()
        assert GenerationInputs.from_manifest_dict(inputs.to_manifest_dict()) == inputs


class TestPaaLifecycleAndConformance:
    def test_declarations_conform_and_resolve(self) -> None:
        versions = validate_declarations_conform(DECLARATIONS_SOURCE_DIR)
        assert versions == {
            "canonical_promotion": 1,
            "inbound_reply_surfacing": 1,
        }

    def test_lifecycle_smoke_test_completes(self) -> None:
        exercise_paa_lifecycle(DECLARATIONS_SOURCE_DIR)


class TestCheckedInReferenceTree:
    """These exercise the real checked-in evidence/paa/reference/, so they
    double as the "generation is reproducible from what's on disk"
    acceptance check for generated PAA reference evidence runs in CI."""

    def test_checked_in_tree_exists(self) -> None:
        assert (REFERENCE_DIR / REFERENCE_MANIFEST_NAME).is_file()

    def test_check_accepts_the_checked_in_tree(self) -> None:
        assert check_reference_tree(target_dir=REFERENCE_DIR) == []

    def test_check_never_writes_to_the_checked_in_tree(self) -> None:
        before = {
            p: (p.stat().st_mtime_ns, p.read_bytes())
            for p in REFERENCE_DIR.rglob("*")
            if p.is_file()
        }
        check_reference_tree(target_dir=REFERENCE_DIR)
        after = {
            p: (p.stat().st_mtime_ns, p.read_bytes())
            for p in REFERENCE_DIR.rglob("*")
            if p.is_file()
        }
        assert before == after

    def test_check_never_writes_to_the_fixture_source_database(self) -> None:
        # build_fixture_source_database always targets a fresh temporary
        # path internally; the checked-in static fixtures must also be
        # untouched by a --check run.
        before = FIXTURE_BEFORE_PATH.read_bytes()
        check_reference_tree(target_dir=REFERENCE_DIR)
        assert FIXTURE_BEFORE_PATH.read_bytes() == before

    def test_check_detects_byte_drift(self, tmp_path: Path) -> None:
        import shutil

        shadow = tmp_path / "reference"
        shutil.copytree(REFERENCE_DIR, shadow)
        (shadow / "grading_schema.json").write_bytes(b"{}")
        problems = check_reference_tree(target_dir=shadow)
        assert any("grading_schema.json" in p for p in problems)

    def test_check_reports_a_missing_manifest_without_raising(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        problems = check_reference_tree(target_dir=empty)
        assert problems and REFERENCE_MANIFEST_NAME in problems[0]


class TestWriteReferenceTreeAtomicity:
    def test_write_then_rewrite_leaves_only_the_new_tree(
        self, tmp_path: Path, rendered: dict[str, bytes]
    ) -> None:
        target = tmp_path / "reference"
        write_reference_tree(rendered, target_dir=target)
        first = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        assert first == set(rendered)

        write_reference_tree(rendered, target_dir=target)
        second = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        assert second == set(rendered)

    def test_write_failure_leaves_target_untouched(
        self, tmp_path: Path, rendered: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "reference"
        write_reference_tree(rendered, target_dir=target)
        before = (target / REFERENCE_MANIFEST_NAME).read_bytes()

        import os

        real_replace = os.replace
        # Fail only the *first* replace targeting `target` — the swap-in
        # of the freshly rendered tree — not the recovery replace that
        # write_reference_tree issues right after to restore the old one.
        failed = False

        def failing_replace(src: object, dst: object) -> None:
            nonlocal failed
            if str(dst) == str(target) and not failed:
                failed = True
                raise OSError("simulated crash mid-swap")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            write_reference_tree(rendered, target_dir=target)
        assert (target / REFERENCE_MANIFEST_NAME).read_bytes() == before


class TestReferenceGenerationErrorHandling:
    def test_bad_gate_block_id_fails_closed(self, source_db: Path) -> None:
        bad_inputs = GenerationInputs(
            gate_block_id=999,
            code_revision="0" * 40,
            model_id="m",
            prompt_revision="p",
            generated_at=default_generation_inputs().generated_at,
            git_commit="0" * 40,
        )
        with pytest.raises(BundleError, match="qualifying"):
            render(bad_inputs, source_db_path=source_db)
