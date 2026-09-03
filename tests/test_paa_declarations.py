"""Scout's PAA task declarations, and the binding that loads them.

Two concerns, kept separate:

- Schema-stage conformance (``TestSchemaConformance``): the two
  checked-in YAML declarations validate against the normative
  ``paa-task`` JSON Schema, loaded from the packaged ``paa_contracts``
  artifacts. This used to require a local paadotdev checkout and skipped
  silently without one — which is what it was doing: validating nothing.
  There is no checkout, no env var, and no skip path now, so the stage
  either runs or the suite fails to import.
- The Scout binding (everything else): the declarations say what Scout
  intends, and every evaluator they name resolves against Scout's own
  producer registry.

The loader itself is no longer tested here. ``paa_runtime.declarations``
owns it and covers its own vocabulary, cross-field semantics, and error
codes upstream; re-asserting that against a second copy is what let the
two implementations drift in the first place. What remains below is what
is genuinely Scout's: which declarations exist, what they say, and that
they resolve against Scout's registry.
"""

from __future__ import annotations

from pathlib import Path

import paa_contracts
import pytest
import yaml
from jsonschema import Draft202012Validator, validators

import scout.paa.declarations as paa_declarations
from scout.paa.declarations import (
    PRODUCER_REGISTRY,
    PaaDeclarationError,
    get_paa_declaration,
    load_paa_declarations,
)

_REPO_ROOT = Path(__file__).parent.parent
_DECLARATIONS_DIR = _REPO_ROOT / "contracts" / "paa"

_EXPECTED_DECLARATIONS: dict[str, dict[str, object]] = {
    "inbound_reply_surfacing": {"version": 1, "deployment": "shadow", "initial_position": "hitl"},
    "canonical_promotion": {"version": 1, "deployment": "disabled", "initial_position": "hitl"},
}


# ---------------------------------------------------------------------------
# Schema-stage conformance
# ---------------------------------------------------------------------------


class TestSchemaConformance:
    """Validated against the published artifact, unconditionally.

    The schema now arrives as a packaged dependency pinned to the same
    revision as paa_runtime, so the declarations and the loader that reads
    them cannot be checked against different versions of the contract.
    """

    def test_schema_is_draft_2020_12_compatible(self) -> None:
        Draft202012Validator.check_schema(paa_contracts.load_schema("paa-task"))

    def test_schema_version_is_recorded(self) -> None:
        """A visible identity for what the declarations were checked
        against, so a contract bump is legible in the diff."""
        assert paa_contracts.schema_version("paa-task") == "paa-task/0.2.1-draft"

    @pytest.mark.parametrize(
        "declaration_path",
        sorted(_DECLARATIONS_DIR.glob("*.yaml")),
        ids=lambda p: p.name,
    )
    def test_declaration_conforms_to_published_schema(self, declaration_path: Path) -> None:
        schema = paa_contracts.load_schema("paa-task")
        validator_cls = validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(schema)

        document = yaml.safe_load(declaration_path.read_text())
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        assert errors == [], (
            f"{declaration_path}: schema violations: {[e.message for e in errors]}"
        )


# ---------------------------------------------------------------------------
# Declaration identity
# ---------------------------------------------------------------------------


class TestDeclarationIdentity:
    def test_exactly_two_declarations_are_checked_in(self) -> None:
        paths = sorted(_DECLARATIONS_DIR.glob("*.yaml"))
        assert [p.name for p in paths] == [
            "canonical_promotion.v1.yaml",
            "inbound_reply_surfacing.v1.yaml",
        ]

    def test_each_declaration_filename_matches_its_task_and_version(self) -> None:
        for path in _DECLARATIONS_DIR.glob("*.yaml"):
            document = yaml.safe_load(path.read_text())
            assert path.name == f"{document['task']}.v{document['version']}.yaml"

    def test_task_and_version_are_unique_across_declarations(self) -> None:
        seen: set[tuple[str, int]] = set()
        for path in _DECLARATIONS_DIR.glob("*.yaml"):
            document = yaml.safe_load(path.read_text())
            key = (document["task"], document["version"])
            assert key not in seen, f"duplicate (task, version) {key} at {path}"
            seen.add(key)

    def test_boundary_names_stay_in_scouts_namespace(self) -> None:
        """The published example declarations are de-branded; Scout's name
        its own entities. Adopting the contract's evaluator shape must not
        quietly adopt its boundary names too."""
        for path in _DECLARATIONS_DIR.glob("*.yaml"):
            boundary = yaml.safe_load(path.read_text())["boundary"]
            assert boundary["input"].startswith("scout.")
            assert boundary["output"].startswith("scout.")


class TestLoaderOutput:
    def test_loads_exactly_the_expected_tasks(self) -> None:
        assert set(load_paa_declarations(_DECLARATIONS_DIR)) == set(_EXPECTED_DECLARATIONS)

    @pytest.mark.parametrize("task", sorted(_EXPECTED_DECLARATIONS))
    def test_declaration_access_fields_match_the_final_spec(self, task: str) -> None:
        declaration = get_paa_declaration(task, directory=_DECLARATIONS_DIR)
        expected = _EXPECTED_DECLARATIONS[task]
        assert declaration.task == task
        assert declaration.version == expected["version"]
        assert declaration.deployment == expected["deployment"]
        assert declaration.initial_position == expected["initial_position"]

    def test_unknown_task_raises(self) -> None:
        with pytest.raises(PaaDeclarationError, match="no PAA declaration"):
            get_paa_declaration("does_not_exist", directory=_DECLARATIONS_DIR)


class TestScopes:
    """Scope is declaration-controlled now, not hardcoded in the service.

    Neither remaining task names scopes, so both resolve at scope None,
    which is what migration 33 relaxed the autonomy_events NOT NULL for.
    (The one scoped task, outbound_content_publish, left with the content
    engine.)
    """

    @pytest.mark.parametrize("task", ["inbound_reply_surfacing", "canonical_promotion"])
    def test_unscoped_tasks_declare_no_scopes(self, task: str) -> None:
        assert get_paa_declaration(task, directory=_DECLARATIONS_DIR).scopes is None


class TestPositionPolicy:
    """Scout still declares the flat placement form for every position.

    The contract now admits a per-evaluator refinement at a position (the
    published outbound example holds its deterministic invariants blocking
    at hotl while its human gates run async). Scout has not adopted that:
    nothing here reads position_policy to branch, so taking it would
    change the declarations without changing any behaviour. This test
    pins the current answer so adopting it later is a deliberate edit.
    """

    @pytest.mark.parametrize("task", sorted(_EXPECTED_DECLARATIONS))
    def test_every_position_is_declared_with_no_overrides(self, task: str) -> None:
        policy = get_paa_declaration(task, directory=_DECLARATIONS_DIR).position_policy
        assert policy.declared_positions == ("autonomous", "hitl", "hotl", "manual")
        assert all(policy[position].overrides == () for position in policy.declared_positions)

    @pytest.mark.parametrize("task", sorted(_EXPECTED_DECLARATIONS))
    def test_placement_defaults_match_scouts_runtime_semantics(self, task: str) -> None:
        policy = get_paa_declaration(task, directory=_DECLARATIONS_DIR).position_policy
        assert policy["manual"].default == "offline"
        assert policy["hitl"].default == "blocking"
        assert policy["hotl"].default == "async"
        assert policy["autonomous"].default == "offline"


class TestTransitionExtraction:
    def test_inbound_promotion_uses_a_duration_window(self) -> None:
        d = get_paa_declaration("inbound_reply_surfacing", directory=_DECLARATIONS_DIR)
        assert d.promotion.report == "phase1_audit"
        assert d.promotion.window.kind == "duration"
        assert d.promotion.window.size == "P14D"

    def test_canonical_promotion_uses_a_fifty_case_window(self) -> None:
        d = get_paa_declaration("canonical_promotion", directory=_DECLARATIONS_DIR)
        assert d.promotion.window.kind == "cases"
        assert d.promotion.window.size == 50


class TestVocabulary:
    def test_deployment_values_match_the_final_spec(self) -> None:
        assert get_paa_declaration(
            "inbound_reply_surfacing", directory=_DECLARATIONS_DIR
        ).deployment == "shadow"
        assert get_paa_declaration(
            "canonical_promotion", directory=_DECLARATIONS_DIR
        ).deployment == "disabled"


# ---------------------------------------------------------------------------
# Evaluator identity and producer resolution
# ---------------------------------------------------------------------------


class TestEvaluatorVersionResolution:
    """Identity is seven fields now, not six.

    evaluation_basis and epistemic_status replace the single `oracle`
    field, which conflated how a verdict is grounded with whether
    governance treats it as authoritative — a rubric-graded proxy and a
    rubric-graded ground truth were indistinguishable under it.
    """

    def _evaluator_tuples(self, task: str) -> set[tuple[str, str, str, str, str, str, str]]:
        d = get_paa_declaration(task, directory=_DECLARATIONS_DIR)
        return {
            (
                e.property, e.target, e.technique, e.evaluation_basis.kind,
                e.evaluation_basis.ref, e.epistemic_status, e.version,
            )
            for e in d.evaluators
        }

    def test_every_declared_evaluator_resolves_against_the_registry(self) -> None:
        registry_tuples = {
            (
                r.property, r.target, r.technique, r.evaluation_basis.kind,
                r.evaluation_basis.ref, r.epistemic_status, r.version,
            )
            for r in PRODUCER_REGISTRY
        }
        for task in _EXPECTED_DECLARATIONS:
            for evaluator_tuple in self._evaluator_tuples(task):
                assert evaluator_tuple in registry_tuples, (
                    f"{task}: evaluator {evaluator_tuple} not in PRODUCER_REGISTRY"
                )

    def test_inbound_reply_surfacing_evaluators(self) -> None:
        assert self._evaluator_tuples("inbound_reply_surfacing") == {
            (
                "content_invariants", "output", "deterministic",
                "invariant", "content_invariants", "ground_truth", "1",
            ),
            (
                "author_rate", "process", "deterministic",
                "invariant", "author_rate", "ground_truth", "1",
            ),
            (
                "response_quality", "output", "llm_judge",
                "rubric", "response_quality_rubric", "proxy", "1",
            ),
            (
                "response_quality", "output", "human",
                "human_gold", "response_quality_human_gold", "ground_truth", "3",
            ),
        }

    def test_canonical_promotion_evaluators(self) -> None:
        assert self._evaluator_tuples("canonical_promotion") == {
            (
                "claim_admissibility", "input", "deterministic",
                "invariant", "claim_admissibility", "ground_truth", "1",
            ),
            (
                "canonical_truth", "output", "human",
                "human_gold", "canonical_truth_human_gold", "ground_truth", "1",
            ),
        }

    def test_the_two_response_quality_producers_stay_distinguishable(self) -> None:
        """The case the single `oracle` field could not express: one
        property, one target, two genuinely different producers."""
        quality = [r for r in PRODUCER_REGISTRY if r.property == "response_quality"]
        assert {(r.evaluation_basis.kind, r.epistemic_status) for r in quality} == {
            ("rubric", "proxy"),
            ("human_gold", "ground_truth"),
        }

    def test_reserved_future_producers_are_marked_future(self) -> None:
        future_properties = {r.property for r in PRODUCER_REGISTRY if r.status == "future"}
        assert future_properties == {"claim_admissibility", "canonical_truth"}

    def test_no_two_registrations_share_a_full_identity(self) -> None:
        """Resolution returns the first match, so a duplicated seven-field
        identity would silently shadow one of the two registrations."""
        identities = [
            (
                r.property, r.target, r.technique, r.evaluation_basis,
                r.epistemic_status, r.version, r.authority,
            )
            for r in PRODUCER_REGISTRY
        ]
        assert len(identities) == len(set(identities))

    def test_at_most_one_implemented_producer_per_evaluator_kind(self) -> None:
        """Deliberately coarser than full identity, and not a weaker form
        of the test above.

        Two *implemented* registrations differing only by version would
        mean two live code paths claiming to produce the same verdict,
        while a declaration names exactly one version — so one of them is
        unreachable. Keying on version here would admit precisely the case
        this exists to catch. Future registrations are exempt: a reserved
        identity has no producer to conflict with.
        """
        kinds = [
            (r.property, r.target, r.technique, r.evaluation_basis, r.epistemic_status)
            for r in PRODUCER_REGISTRY
            if r.status == "implemented"
        ]
        assert len(kinds) == len(set(kinds))


# ---------------------------------------------------------------------------
# Fail-closed behaviour of Scout's binding
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Only the failures Scout's binding is responsible for.

    Per-field vocabulary and cross-field semantics are the runtime
    loader's, tested upstream. These are the ones that depend on Scout's
    directory and Scout's registry.
    """

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PaaDeclarationError, match="not found"):
            load_paa_declarations(tmp_path / "does_not_exist")

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PaaDeclarationError, match="no PAA task declarations"):
            load_paa_declarations(tmp_path)

    def test_duplicate_task_across_files_raises(self, tmp_path: Path) -> None:
        base = yaml.safe_load((_DECLARATIONS_DIR / "canonical_promotion.v1.yaml").read_text())
        (tmp_path / "canonical_promotion.v1.yaml").write_text(yaml.dump(base))
        (tmp_path / "canonical_promotion_dupe.yaml").write_text(yaml.dump(base))
        with pytest.raises(PaaDeclarationError, match="duplicate PAA declaration"):
            load_paa_declarations(tmp_path)

    def test_evaluator_version_absent_from_the_registry_raises(self, tmp_path: Path) -> None:
        base = yaml.safe_load((_DECLARATIONS_DIR / "canonical_promotion.v1.yaml").read_text())
        base["evaluators"][0]["version"] = "999"
        (tmp_path / "canonical_promotion.v1.yaml").write_text(yaml.dump(base))
        with pytest.raises(PaaDeclarationError, match="does not resolve"):
            load_paa_declarations(tmp_path)

    def test_evaluation_basis_absent_from_the_registry_raises(self, tmp_path: Path) -> None:
        """The new axis has to participate in resolution, or splitting
        `oracle` into two fields bought nothing."""
        base = yaml.safe_load((_DECLARATIONS_DIR / "canonical_promotion.v1.yaml").read_text())
        base["evaluators"][0]["evaluation_basis"]["ref"] = "some_other_invariant_set"
        (tmp_path / "canonical_promotion.v1.yaml").write_text(yaml.dump(base))
        with pytest.raises(PaaDeclarationError, match="does not resolve"):
            load_paa_declarations(tmp_path)

    def test_epistemic_status_absent_from_the_registry_raises(self, tmp_path: Path) -> None:
        base = yaml.safe_load((_DECLARATIONS_DIR / "canonical_promotion.v1.yaml").read_text())
        base["evaluators"][0]["epistemic_status"] = "proxy"
        (tmp_path / "canonical_promotion.v1.yaml").write_text(yaml.dump(base))
        with pytest.raises(PaaDeclarationError, match="does not resolve"):
            load_paa_declarations(tmp_path)


def test_default_declarations_dir_points_at_contracts_paa() -> None:
    assert paa_declarations.DEFAULT_DECLARATIONS_DIR == _DECLARATIONS_DIR
