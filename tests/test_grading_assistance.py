from __future__ import annotations

import argparse
import math
import platform
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scout.cli.analysis import add_analysis_parser, run_analysis, verify_analysis_bundle
from scout.config import GradeRecord
from scout.dossiers.resolver import DossierResolution, DossierSummary, ResolutionMetadata
from scout.grading.artifacts import (
    ArtifactBundle,
    ProducerEnvironment,
    RetainedArtifact,
    digest_artifact,
    encode_lineage,
)
from scout.grading.assistance import (
    eligible_candidates,
    execute_assistance,
    fit_tfidf,
    freeze_partition,
    group_posts,
    report_queue_reviews,
    score_texts,
    select_random,
)
from scout.grading.assistance_store import (
    AssistanceRequest,
    build_assistance_bundle,
    capture_runtime,
    load_training_examples,
    outputs_match,
    read_rejected_population,
    resolve_queue_outcomes,
    verify_assistance_replay,
)
from scout.grading.assistance_types import (
    AssistanceConfig,
    RandomSelector,
    ReviewOutcome,
    SelectionReference,
    TfidfSelector,
)
from scout.grading.assistance_wire import encode_outputs, encode_queue
from scout.grading.snapshots import CorpusSelection, build_snapshot_bundle, read_grade_population
from scout.result import Err, Ok
from scout.storage.state import StateManager


def arguments(*values: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_analysis_parser(parser.add_subparsers(), "unused.db")
    return parser.parse_args(["analysis", *values])


@pytest.fixture
def state(tmp_path, monkeypatch):
    def resolve(repository, revision, project_key, summary_id):
        return DossierResolution(
            summary=DossierSummary(
                project_key=project_key, last_reviewed=date(2026, 1, 1), reviewer="synthetic"
            ),
            metadata=ResolutionMetadata(
                project_key=project_key,
                summary_id=summary_id,
                revision=revision,
                path="synthetic.yaml",
            ),
            known_gaps=(),
        )

    monkeypatch.setattr("scout.grading.snapshots.resolve_dossier", resolve)
    monkeypatch.setattr("scout.grading.assistance_store.resolve_dossier", resolve)
    with StateManager(str(tmp_path / "scout.db")) as state:
        with state.db.transaction():
            for index in (*range(1, 13), *range(100, 116)):
                positive = index % 2 == 0
                text = (
                    f"{'robot agent workflow' if positive else 'soup recipe kitchen'} unique{index}"
                )
                if index in (100, 101):
                    text = "robot assistant automation workflow"
                state.conn.execute(
                    "INSERT INTO posts(id, platform, platform_msg_id, content) "
                    "VALUES (?, 'bluesky', ?, ?)",
                    (index, f"synthetic-{index}", text),
                )
                state.conn.execute(
                    "INSERT INTO evaluations(id, post_id, relevant, score, surface_status, "
                    "project_key, posture, dossier_revision, dossier_summary_id) "
                    "VALUES (?, ?, ?, 0.2, ?, 'synthetic', 'answer', ?, 'summary')",
                    (
                        index,
                        index,
                        int(positive and index < 100),
                        "surfaced" if positive and index < 100 else "not_relevant",
                        "b" * 40,
                    ),
                )
        for index in range(1, 13):
            state.save_grade(
                GradeRecord(
                    post_id=index,
                    evaluation_id=index,
                    source="web",
                    graded_at=datetime(2026, 1, 1, tzinfo=UTC),
                    relevance_judgment="correct",
                    action_judgment="accept",
                    schema_version=3,
                )
            )
        yield state


@pytest.fixture
def request_data(state, tmp_path):
    with state.db.read_transaction():
        population = read_grade_population(state.conn, tmp_path)
        candidates = read_rejected_population(state.conn, "synthetic", tmp_path)
    assert isinstance(population, Ok)
    assert isinstance(candidates, Ok)
    snapshot = build_snapshot_bundle(
        population.value, CorpusSelection(project_key="synthetic"), b"synthetic"
    )
    assert isinstance(snapshot, Ok)
    assert state.artifacts.import_bundle(snapshot.value) == Ok(None)
    return AssistanceRequest(
        bundle=snapshot.value,
        snapshot_digest=snapshot.value.lineages[0].outputs[0],
        population=candidates.value,
        config=AssistanceConfig(random=RandomSelector(count=5, seed=17)),
    )


@pytest.fixture
def examples(request_data):
    result = load_training_examples(request_data.bundle, request_data.snapshot_digest)
    assert isinstance(result, Ok)
    assert len(result.value) == 12
    return result.value


@pytest.fixture
def runtime():
    lock = (Path(__file__).parents[1] / "uv.lock").read_bytes()
    declared = (
        ProducerEnvironment(
            code_revision="a" * 40,
            dependency_lock_digest=digest_artifact(lock),
            python_version=platform.python_version(),
        )
        .model_dump_json()
        .encode()
    )
    value = capture_runtime(declared, lock)
    assert isinstance(value, Ok)
    return value.value


def partition_for(request_data, examples):
    result = freeze_partition(examples, request_data.snapshot_digest, request_data.config)
    assert isinstance(result, Ok), result
    return result.value


def test_grouping_connects_ancestors_siblings_and_transitive_duplicates(examples):
    base = examples[0].post
    posts = (
        base.model_copy(update={"id": 1, "platform_msg_id": "root", "content": "one"}),
        base.model_copy(
            update={"id": 2, "platform_msg_id": "child", "parent_id": "root", "content": "two"}
        ),
        base.model_copy(
            update={"id": 3, "platform_msg_id": "sibling", "parent_id": "root", "content": "three"}
        ),
        base.model_copy(update={"id": 4, "platform_msg_id": "duplicate", "content": "  THREE  "}),
        base.model_copy(
            update={"id": 5, "platform_msg_id": "root", "platform": "discord", "content": "other"}
        ),
    )
    groups = group_posts(posts)
    assert len({groups[index] for index in range(1, 5)}) == 1
    assert groups[5] != groups[1]
    assert group_posts(tuple(reversed(posts))) == groups


def test_partition_groups_never_cross_train_and_heldout(request_data, examples):
    linked = (
        *examples[:-1],
        replace(
            examples[-1],
            post=examples[-1].post.model_copy(
                update={"parent_id": examples[0].post.platform_msg_id}
            ),
        ),
    )
    partition = partition_for(request_data, linked)
    assignments = {item.evaluation_id: item.partition for item in partition.members}
    assert assignments[linked[0].evaluation_id] == assignments[linked[-1].evaluation_id]
    assert {item.partition for item in partition.members} == {"train", "heldout"}


def test_partition_prefers_random_provenance_and_keeps_exposed_groups_in_train(
    request_data, examples
):
    random_ref = SelectionReference(queue_digest=digest_artifact(b"queue"), method="random")
    items = tuple(
        replace(item, provenance=(random_ref,)) if index < 2 else item
        for index, item in enumerate(examples)
    )
    items = (*items[:-1], replace(items[-1], exposed=True))
    partition = partition_for(request_data, items)
    by_id = {item.evaluation_id: item.partition for item in partition.members}
    assert by_id[items[0].evaluation_id] == by_id[items[1].evaluation_id] == "heldout"
    assert by_id[items[-1].evaluation_id] == "train"


def test_vocabulary_idf_and_fit_use_train_only(request_data, examples):
    partition = partition_for(request_data, examples)
    train_ids = {item.evaluation_id for item in partition.members if item.partition == "train"}
    train = tuple(item for item in examples if item.evaluation_id in train_ids)
    heldout = tuple(item for item in examples if item.evaluation_id not in train_ids)
    fitted = fit_tfidf(train, request_data.config.ranked, request_data.config.seed)
    assert isinstance(fitted, Ok), fitted
    model = fitted.value
    assert set(model.train_evaluation_ids) == train_ids
    assert all(f"unique{item.evaluation_id}" not in model.vocabulary for item in heldout)
    word = f"unique{train[0].evaluation_id}"
    assert model.idf[model.vocabulary.index(word)] == pytest.approx(
        math.log((1 + len(train)) / 2) + 1
    )
    baseline = execute_assistance(examples, request_data.population, partition, request_data.config)
    mutated = tuple(
        item
        if item.evaluation_id in train_ids
        else replace(
            item,
            post=item.post.model_copy(
                update={"content": "HELDOUT_SENTINEL exclusivelynew heldoutterm"}
            ),
            is_relevant=not item.is_relevant,
        )
        for item in examples
    )
    changed = execute_assistance(mutated, request_data.population, partition, request_data.config)
    assert isinstance(baseline, Ok) and isinstance(changed, Ok)
    assert baseline.value.model == changed.value.model
    assert baseline.value.queue.ranked == changed.value.queue.ranked


@pytest.mark.parametrize("text", ["", "! ?", "a i"])
def test_empty_vocabulary_returns_error(examples, text):
    train = tuple(
        replace(item, post=item.post.model_copy(update={"content": text})) for item in examples
    )
    assert isinstance(fit_tfidf(train, TfidfSelector(), 0), Err)


def test_one_class_or_infeasible_split_is_explicit(request_data, examples):
    assert isinstance(
        freeze_partition(
            tuple(item for item in examples if item.is_relevant),
            request_data.snapshot_digest,
            request_data.config,
        ),
        Err,
    )
    assert isinstance(
        freeze_partition(
            tuple(replace(item, exposed=True) for item in examples),
            request_data.snapshot_digest,
            request_data.config,
        ),
        Err,
    )


def test_random_selection_does_not_depend_on_ranked_count(request_data, examples):
    partition = partition_for(request_data, examples)
    one = execute_assistance(
        examples,
        request_data.population,
        partition,
        request_data.config.model_copy(update={"ranked": TfidfSelector(count=1)}),
    )
    all_items = execute_assistance(
        examples, request_data.population, partition, request_data.config
    )
    assert isinstance(one, Ok) and isinstance(all_items, Ok)
    assert one.value.queue.random == all_items.value.queue.random


def test_overlap_and_duplicates_keep_every_random_sample_member(request_data, examples):
    config = request_data.config.model_copy(update={"random": RandomSelector(count=16, seed=3)})
    result = execute_assistance(
        examples, request_data.population, partition_for(request_data, examples), config
    )
    assert isinstance(result, Ok), result
    queue = result.value.queue
    assert len(queue.random.selected_evaluation_ids) == 16
    assert result.value.report.overlap_count > 0
    sources = [source for item in queue.items for source in item.sources]
    assert {source.evaluation_id for source in sources} == set(queue.random.selected_evaluation_ids)
    assert len(queue.items) == 15
    duplicate = next(item for item in queue.items if len(item.sources) > 1)
    assert {source.evaluation_id for source in duplicate.sources} == {100, 101}
    assert all(source.random_position is not None for source in duplicate.sources)


def test_incomplete_random_reviews_never_estimate_a_rate(request_data, examples):
    result = execute_assistance(
        examples,
        request_data.population,
        partition_for(request_data, examples),
        request_data.config,
    )
    assert isinstance(result, Ok)
    queue = result.value.queue
    sample = queue.random.selected_evaluation_ids
    outcomes = tuple(
        ReviewOutcome(evaluation_id=value, is_relevant=True, grade_revision_id=index)
        for index, value in enumerate(sample[:-1], 1)
    )
    report = report_queue_reviews(queue, outcomes)
    assert isinstance(report, Ok)
    assert report.value.random.sample_count == 5
    assert report.value.random.unreviewed_evaluation_ids == (sample[-1],)
    assert report.value.random.estimated_relevant_fraction is None
    assert report.value.random.wilson_95_low is None
    complete = report_queue_reviews(
        queue,
        (
            *outcomes,
            ReviewOutcome(evaluation_id=sample[-1], is_relevant=False, grade_revision_id=20),
        ),
    )
    assert isinstance(complete, Ok)
    assert complete.value.random.estimated_relevant_fraction == 0.8
    assert complete.value.random.wilson_95_low < 0.8 < complete.value.random.wilson_95_high


def test_empty_pool_or_oversized_random_sample_is_an_error():
    assert isinstance(select_random((), RandomSelector(count=1, seed=1)), Err)
    assert isinstance(select_random((1,), RandomSelector(count=2, seed=1)), Err)


def test_missing_context_and_graded_related_candidates_are_excluded(request_data, examples):
    candidate = next(item for item in request_data.population.items if item.evaluation.id == 100)
    missing = candidate.model_copy(update={"context": None})
    related = candidate.model_copy(
        update={
            "post": candidate.post.model_copy(
                update={"parent_id": examples[0].post.platform_msg_id}
            )
        }
    )
    for item, expected in ((missing, "unavailable_pinned_context"), (related, "graded_group")):
        pool = request_data.population.model_copy(update={"items": (item,)})
        candidates, exclusions = eligible_candidates(pool, examples)
        assert candidates == ()
        assert exclusions[0].reason == expected


def test_pinned_execution_roundtrips_and_replays_after_live_changes(state, request_data, runtime):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok), built
    repeated = build_assistance_bundle(request_data, runtime)
    assert isinstance(repeated, Ok)
    assert encode_outputs(built.value.outputs) == encode_outputs(repeated.value.outputs)
    assert built.value.bundle == repeated.value.bundle
    restored = ArtifactBundle.model_validate_json(built.value.bundle.model_dump_json())
    with state.db.transaction():
        state.conn.execute("UPDATE posts SET content = 'changed live text'")
    assert verify_assistance_replay(restored) == Ok(1)
    assert verify_analysis_bundle(restored) == Ok(2)
    with StateManager(":memory:") as destination:
        assert destination.artifacts.import_bundle(restored) == Ok(None)
        exported = destination.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        assert verify_analysis_bundle(exported.value) == Ok(2)


def test_persisted_model_scores_without_fitting_again(request_data, examples, monkeypatch):
    fitted = fit_tfidf(examples, request_data.config.ranked, 0)
    assert isinstance(fitted, Ok)

    def forbidden(*args, **kwargs):
        raise AssertionError("fit must not run during scoring")

    monkeypatch.setattr("scout.grading.assistance.TfidfVectorizer.fit", forbidden)
    monkeypatch.setattr("scout.grading.assistance.TfidfVectorizer.fit_transform", forbidden)
    scores = score_texts(((1, "robot workflow"),), fitted.value, request_data.config.ranked)
    assert isinstance(scores, Ok), scores
    assert scores.value[0].explanation


def test_preview_and_execute_commands_preserve_grades(state, request_data, runtime, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(request_data.config.model_dump_json())
    environment = tmp_path / "environment.json"
    contents = {item.digest: item.content for item in runtime.artifacts}
    environment.write_bytes(contents[runtime.identity.declared_environment_digest])
    lock_path = tmp_path / "uv.lock"
    lock_path.write_bytes(contents[runtime.identity.lock_digest])
    before = tuple(tuple(row) for row in state.conn.execute("SELECT * FROM grades ORDER BY id"))
    shared = (
        "--db-path",
        state.db.db_path,
        "--snapshot",
        request_data.snapshot_digest,
        "--config",
        str(config_path),
        "--dossier-root",
        str(tmp_path),
    )
    artifact_count = state.conn.execute("SELECT COUNT(*) FROM analysis_artifacts").fetchone()[0]
    preview = run_analysis(arguments("assistance-preview", *shared))
    assert isinstance(preview, Ok), preview
    assert preview.value.candidate_count == 16
    assert (
        state.conn.execute("SELECT COUNT(*) FROM analysis_artifacts").fetchone()[0]
        == artifact_count
    )
    executed = run_analysis(
        arguments(
            "assistance-run", *shared, "--environment", str(environment), "--lock", str(lock_path)
        )
    )
    assert isinstance(executed, Ok), executed
    assert (
        tuple(tuple(row) for row in state.conn.execute("SELECT * FROM grades ORDER BY id"))
        == before
    )
    replayed = run_analysis(
        arguments(
            "assistance-replay",
            "--db-path",
            state.db.db_path,
            "--queue",
            executed.value.queue_digest,
        )
    )
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.candidate_count == 16


def test_runtime_rejects_a_different_lock(runtime):
    contents = {item.digest: item.content for item in runtime.artifacts}
    assert isinstance(
        capture_runtime(contents[runtime.identity.declared_environment_digest], b"different"), Err
    )


def test_ungraded_bridge_cannot_join_train_and_heldout(request_data, examples):
    root, leaf = examples[:2]
    leaf = replace(leaf, post=leaf.post.model_copy(update={"parent_id": "bridge"}))
    bridge = root.post.model_copy(
        update={
            "id": 999,
            "platform_msg_id": "bridge",
            "parent_id": root.post.platform_msg_id,
            "content": "synthetic ungraded bridge",
        }
    )
    changed = (root, leaf, *examples[2:])
    result = freeze_partition(
        changed, request_data.snapshot_digest, request_data.config, related_posts=(bridge,)
    )
    assert isinstance(result, Ok)
    members = {item.evaluation_id: item for item in result.value.members}
    assert members[root.evaluation_id].group_id == members[leaf.evaluation_id].group_id
    assert members[root.evaluation_id].partition == members[leaf.evaluation_id].partition


def test_fitting_rejects_tampered_partition_membership(request_data, examples):
    partition = partition_for(request_data, examples)
    tampered = partition.model_copy(update={"members": partition.members[:-1]})
    result = execute_assistance(examples, request_data.population, tampered, request_data.config)
    assert isinstance(result, Err)
    assert result.error.operation == "validate_partition"


def test_random_only_runs_without_a_two_class_training_requirement(request_data, runtime):
    request = replace(request_data, config=request_data.config.model_copy(update={"ranked": None}))
    result = build_assistance_bundle(request, runtime)
    assert isinstance(result, Ok)
    assert result.value.outputs.model is None
    assert result.value.outputs.report.heldout is None
    assert verify_assistance_replay(result.value.bundle) == Ok(1)


def test_selected_duplicate_keeps_nonselected_source_link(request_data):
    from scout.grading.assistance import assemble_queue
    from scout.grading.assistance_types import SelectorResult

    sample = SelectorResult(
        kind="seeded_random", population_evaluation_ids=(100, 101), selected_evaluation_ids=(100,)
    )
    queue = assemble_queue(request_data.population, None, sample)
    assert len(queue.items) == 1
    assert [source.evaluation_id for source in queue.items[0].sources] == [100, 101]
    assert queue.items[0].sources[1].random_position is None


@pytest.mark.parametrize("output_index", [0, 1, 2, 3])
def test_replay_rejects_tampered_supported_outputs(request_data, runtime, output_index):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    bundle = built.value.bundle
    lineage = bundle.lineages[-1]
    artifact = RetainedArtifact(digest=digest_artifact(b"{}"), content=b"{}")
    output_ids = list(lineage.outputs)
    output_ids[output_index] = artifact.digest
    corrupt = bundle.model_copy(
        update={
            "artifacts": (*bundle.artifacts, artifact),
            "lineages": (
                *bundle.lineages[:-1],
                lineage.model_copy(update={"outputs": tuple(output_ids)}),
            ),
        }
    )
    assert isinstance(verify_assistance_replay(corrupt), Err)


def test_numeric_tolerance_never_relaxes_queue_membership(request_data, runtime):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    outputs = built.value.outputs
    model = outputs.model
    assert model is not None
    tiny = replace(outputs, model=model.model_copy(update={"intercept": model.intercept + 1e-12}))
    large = replace(outputs, model=model.model_copy(update={"intercept": model.intercept + 1e-5}))
    assert outputs_match(outputs, tiny)
    assert not outputs_match(outputs, large)
    reordered = replace(
        outputs,
        queue=outputs.queue.model_copy(update={"items": tuple(reversed(outputs.queue.items))}),
    )
    assert not outputs_match(outputs, reordered)


def test_source_archive_can_differ_when_current_v1_adapter_reproduces_outputs(
    request_data, runtime, monkeypatch
):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    monkeypatch.setattr(
        "scout.grading.assistance_store._installed_source", lambda: b"updated unrelated module"
    )
    assert verify_assistance_replay(built.value.bundle) == Ok(1)


def test_new_snapshot_preserves_selection_provenance_but_not_edited_input_labels(
    state,
    request_data,
    runtime,
    tmp_path,
):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    queue = built.value.outputs.queue
    queue_digest = digest_artifact(encode_queue(queue))
    assert state.artifacts.import_bundle(built.value.bundle) == Ok(None)
    sampled_id = queue.random.selected_evaluation_ids[0]
    state.save_grade(
        GradeRecord(
            post_id=sampled_id,
            evaluation_id=sampled_id,
            source="web",
            graded_at=datetime(2026, 9, 5, tzinfo=UTC),
            relevance_judgment="false_negative",
            action_judgment="fail",
            dimensions=["usefulness"],
            failure_note="Synthetic relevant post was missed",
            schema_version=3,
        )
    )

    def new_snapshot():
        with state.db.read_transaction():
            captured = read_grade_population(state.conn, tmp_path)
        assert isinstance(captured, Ok)
        snapshot = build_snapshot_bundle(
            captured.value, CorpusSelection(project_key="synthetic"), b"synthetic"
        )
        assert isinstance(snapshot, Ok)
        assert state.artifacts.import_bundle(snapshot.value) == Ok(None)
        exported = state.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        return exported.value, snapshot.value.lineages[0].outputs[0]

    bundle, snapshot_digest = new_snapshot()
    loaded = load_training_examples(bundle, snapshot_digest, (queue_digest,))
    assert isinstance(loaded, Ok)
    sampled = next(item for item in loaded.value if item.evaluation_id == sampled_id)
    assert SelectionReference(queue_digest=queue_digest, method="random") in sampled.provenance
    outcomes = resolve_queue_outcomes(bundle, snapshot_digest, queue)
    assert isinstance(outcomes, Ok)
    assert sampled_id in {item.evaluation_id for item in outcomes.value}
    with state.db.transaction():
        state.conn.execute(
            "UPDATE posts SET content = 'different source text' WHERE id = ?", (sampled_id,)
        )
    changed, changed_digest = new_snapshot()
    outcomes = resolve_queue_outcomes(changed, changed_digest, queue)
    assert isinstance(outcomes, Ok)
    assert sampled_id not in {item.evaluation_id for item in outcomes.value}


def test_capture_invalid_decision_and_missing_context_are_exclusions(state, request_data, tmp_path):
    with state.db.transaction():
        state.conn.execute("UPDATE evaluations SET relevant = 2 WHERE id = 100")
        state.conn.execute("UPDATE evaluations SET dossier_revision = NULL WHERE id = 102")
        state.conn.execute("UPDATE evaluations SET project_key = NULL WHERE id = 103")
    with state.db.read_transaction():
        captured = read_rejected_population(state.conn, "synthetic", tmp_path)
    assert isinstance(captured, Ok)
    _, exclusions = eligible_candidates(captured.value, ())
    by_id = {item.evaluation_id: item.reason for item in exclusions}
    assert by_id[100] == "invalid_original_decision"
    assert by_id[102] == "unavailable_pinned_context"
    assert by_id[103] == "missing_project_context"


def test_unknown_assistance_producer_is_reported_not_replayed(request_data, runtime):
    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    bundle = built.value.bundle
    lineage = bundle.lineages[-1]
    future = lineage.model_copy(
        update={"process": lineage.process.model_copy(update={"version": "future"})}
    )
    mixed = bundle.model_copy(update={"lineages": (*bundle.lineages, future)})
    assert verify_assistance_replay(mixed) == Ok(1)


@pytest.mark.parametrize(
    "command", ["assistance-preview", "assistance-run", "assistance-replay", "assistance-report"]
)
def test_assistance_commands_do_not_create_mistyped_database(tmp_path, command):
    path = tmp_path / "absent.db"
    values = [command, "--db-path", str(path)]
    if command in ("assistance-preview", "assistance-run"):
        values.extend(
            [
                "--snapshot",
                "a" * 64,
                "--config",
                str(tmp_path / "config.json"),
                "--dossier-root",
                str(tmp_path),
            ]
        )
    else:
        values.extend(["--queue", "a" * 64])
    if command == "assistance-run":
        values.extend(
            ["--environment", str(tmp_path / "env.json"), "--lock", str(tmp_path / "uv.lock")]
        )
    if command == "assistance-report":
        values.extend(["--snapshot", "a" * 64])
    assert isinstance(run_analysis(arguments(*values)), Err)
    assert not path.exists()


def test_assistance_config_has_pinned_v1_wire_bytes():
    from scout.grading.assistance_wire import encode_config

    config = AssistanceConfig(random=RandomSelector(count=5, seed=29))
    assert encode_config(config) == (
        b'{"format":"scout.assistance-config/v1","seed":0,"heldout_fraction":0.2,'
        b'"ranked":{"kind":"tfidf_logistic","count":20,"regularization_c":1.0,'
        b'"class_weight":"balanced","max_features":20000,"max_iterations":1000},'
        b'"random":{"kind":"seeded_random","count":5,"seed":29,'
        b'"design":"srs_without_replacement_evaluations/v1"}}'
    )


def test_new_wire_layout_ignores_operational_field_order(request_data, runtime):
    from dataclasses import make_dataclass

    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)
    outputs = built.value.outputs
    fields = tuple(reversed(tuple(type(outputs.model).model_fields)))
    reordered_type = make_dataclass("ReorderedModel", [(name, object) for name in fields])
    reordered = reordered_type(**{name: getattr(outputs.model, name) for name in fields})
    assert encode_outputs(replace(outputs, model=reordered)) == encode_outputs(outputs)


def test_rejection_explanations_are_never_features(request_data, examples):
    partition = partition_for(request_data, examples)
    before = execute_assistance(examples, request_data.population, partition, request_data.config)
    changed = request_data.population.model_copy(
        update={
            "items": tuple(
                item.model_copy(
                    update={
                        "evaluation": item.evaluation.model_copy(
                            update={
                                "reason": "HELDOUT REJECTION TEXT robot robot soup soup",
                            }
                        )
                    }
                )
                for item in request_data.population.items
            )
        }
    )
    after = execute_assistance(examples, changed, partition, request_data.config)
    assert isinstance(before, Ok) and isinstance(after, Ok)
    assert before.value.model == after.value.model
    assert before.value.queue.ranked == after.value.queue.ranked
    assert before.value.queue.random == after.value.queue.random


def test_study_index_projects_assistance_without_fitting(request_data, runtime, monkeypatch):
    from scout.cli.analysis import project_study_index

    built = build_assistance_bundle(request_data, runtime)
    assert isinstance(built, Ok)

    def forbidden(*args, **kwargs):
        raise AssertionError("Index must not fit a model")

    monkeypatch.setattr("scout.grading.assistance.fit_tfidf", forbidden)
    index = project_study_index(built.value.bundle)
    assert isinstance(index, Ok)
    assert all(item.issue is None for item in index.value.entries)


def test_multiple_producers_of_same_queue_require_explicit_lineage(state, request_data, runtime):
    config = request_data.config.model_copy(update={"ranked": None})
    first = build_assistance_bundle(replace(request_data, config=config), runtime)
    assert isinstance(first, Ok)
    second = build_assistance_bundle(
        replace(
            request_data,
            bundle=first.value.bundle,
            config=config.model_copy(update={"seed": config.seed + 1}),
        ),
        runtime,
    )
    assert isinstance(second, Ok)
    queue_digest = digest_artifact(encode_queue(second.value.outputs.queue))
    assert queue_digest == digest_artifact(encode_queue(first.value.outputs.queue))
    assert first.value.lineage != second.value.lineage
    assert state.artifacts.import_bundle(second.value.bundle) == Ok(None)
    command = ("assistance-replay", "--db-path", state.db.db_path, "--queue", queue_digest)
    ambiguous = run_analysis(arguments(*command))
    assert isinstance(ambiguous, Err)
    assert "--lineage" in ambiguous.error.detail
    selected = run_analysis(
        arguments(*command, "--lineage", digest_artifact(encode_lineage(second.value.lineage)))
    )
    assert isinstance(selected, Ok), selected
