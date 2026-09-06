"""Pure grouping/selection/report transforms and the local sklearn boundary."""

from __future__ import annotations

import math
import random
import unicodedata
import warnings
from collections import Counter
from collections.abc import Sequence

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

from scout.grading.artifacts import ArtifactDigest, ArtifactError, digest_artifact
from scout.grading.assistance_types import (
    AssistanceConfig,
    AssistanceOutputs,
    AssistanceReport,
    CandidateExclusion,
    CandidateExclusionReason,
    ConfusionCounts,
    FittedTfidf,
    FrozenPartition,
    GroupId,
    HeldoutComparison,
    PartitionMember,
    QueueReviewReport,
    QueueSource,
    RandomRate,
    RandomSelector,
    RejectedInput,
    RejectedPopulation,
    ReviewOutcome,
    ReviewQueue,
    ReviewQueueItem,
    ReviewYield,
    ScoredCandidate,
    SelectorResult,
    TermContribution,
    TfidfSelector,
    TrainingExample,
)
from scout.grading.snapshots import RecordedPost
from scout.result import Err, Ok, Result


def duplicate_key(post: RecordedPost) -> ArtifactDigest:
    """V1 duplicate policy: NFKC, casefold, collapse whitespace; no stemming."""
    normalized = " ".join(unicodedata.normalize("NFKC", post.content or "").casefold().split())
    return digest_artifact(normalized.encode())


def group_posts(posts: Sequence[RecordedPost]) -> dict[int, GroupId]:
    """Connected components of recorded parent edges, post IDs and exact duplicates.

    Platform namespaces mirror posts.UNIQUE(platform, platform_msg_id).
    A missing ancestor still joins siblings. Only recorded edges are claimed.
    """
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def message_key(post: RecordedPost, message_id: str) -> str:
        # Length-prefix fields avoid delimiter ambiguity in external identifiers.
        fields = (post.platform, message_id)
        return "message:" + "".join(f"{len(field)}:{field}" for field in fields)

    for post in posts:
        keys = [f"post:{post.id}", message_key(post, post.platform_msg_id)]
        if post.content and post.content.strip():
            keys.append(f"text:{duplicate_key(post)}")
        if post.parent_id:
            keys.append(message_key(post, post.parent_id))
        roots = [root(key) for key in keys]
        leader = min(roots)
        for value in roots:
            parents[value] = leader
    return {post.id: GroupId(digest_artifact(root(f"post:{post.id}").encode())) for post in posts}


def freeze_partition(
    examples: tuple[TrainingExample, ...],
    snapshot: ArtifactDigest,
    config: AssistanceConfig,
    *,
    related_posts: tuple[RecordedPost, ...] = (),
) -> Result[FrozenPartition, ArtifactError]:
    if len({item.evaluation_id for item in examples}) != len(examples):
        return Err(ArtifactError("partition", snapshot, "Duplicate corpus evaluation identity"))
    groups = group_posts((*[item.post for item in examples], *related_posts))
    grouped: dict[GroupId, list[TrainingExample]] = {}
    for item in examples:
        grouped.setdefault(groups[item.post.id], []).append(item)
    heldout: set[GroupId] = set()
    if config.ranked is not None:
        if {item.is_relevant for item in examples} != {False, True}:
            return Err(ArtifactError("partition", snapshot, "Project needs two usable classes"))
        eligible = [key for key, items in grouped.items() if not any(i.exposed for i in items)]
        order = sorted(
            eligible,
            key=lambda key: (
                not any(p.method == "random" for item in grouped[key] for p in item.provenance),
                digest_artifact(f"{config.seed}:{key}".encode()),
            ),
        )

        def can_hold(key: GroupId) -> bool:
            return {
                item.is_relevant
                for group, items in grouped.items()
                if group not in heldout and group != key
                for item in items
            } == {False, True}

        # Stratify at group granularity without splitting threads/duplicates.
        for target in (False, True):
            if any(item.is_relevant == target for key in heldout for item in grouped[key]):
                continue
            choice = next(
                (
                    key
                    for key in order
                    if key not in heldout
                    and any(item.is_relevant == target for item in grouped[key])
                    and can_hold(key)
                ),
                None,
            )
            if choice is None:
                return Err(
                    ArtifactError(
                        "partition",
                        snapshot,
                        "Cannot form two-class train/held-out groups "
                        "without known exposure leakage",
                    )
                )
            heldout.add(choice)
        target_count = max(2, math.ceil(len(grouped) * config.heldout_fraction))
        for key in order:
            if len(heldout) >= target_count:
                break
            if key not in heldout and can_hold(key):
                heldout.add(key)
        if len(heldout) < target_count:
            return Err(
                ArtifactError("partition", snapshot, "Selected held-out split is infeasible")
            )
    return Ok(
        FrozenPartition(
            snapshot_digest=snapshot,
            members=tuple(
                PartitionMember(
                    evaluation_id=item.evaluation_id,
                    input_digest=item.input_digest,
                    group_id=groups[item.post.id],
                    partition="heldout" if groups[item.post.id] in heldout else "train",
                    exposed=item.exposed,
                    provenance=item.provenance,
                )
                for item in sorted(examples, key=lambda item: item.evaluation_id)
            ),
        )
    )


def candidate_exclusion(item: RejectedInput, project: str) -> CandidateExclusionReason | None:
    evaluation = item.evaluation
    if not evaluation.project_key or not evaluation.project_key.strip():
        return "missing_project_context"
    if evaluation.project_key != project:
        return "outside_project"
    if evaluation.relevant not in (0, 1):
        return "invalid_original_decision"
    if evaluation.relevant != 0 or evaluation.surface_status != "not_relevant":
        return "not_rejected"
    if item.has_grade:
        return "already_graded"
    if item.post is None:
        return "missing_post"
    if not item.post.content or not item.post.content.strip():
        return "missing_post_text"
    if item.context is None:
        return "unavailable_pinned_context"
    return None


def eligible_candidates(
    population: RejectedPopulation, examples: tuple[TrainingExample, ...]
) -> tuple[tuple[RejectedInput, ...], tuple[CandidateExclusion, ...]]:
    # Protect future held-out cases from near-term model-guided review exposure.
    posts = (*[i.post for i in examples], *[i.post for i in population.items if i.post is not None])
    groups = group_posts(posts)
    graded_groups = {groups[item.post.id] for item in examples}
    candidates: list[RejectedInput] = []
    exclusions: list[CandidateExclusion] = []
    for item in sorted(population.items, key=lambda item: item.evaluation.id):
        reason = candidate_exclusion(item, population.project_key)
        if reason is None and item.post is not None and groups[item.post.id] in graded_groups:
            reason = "graded_group"
        if reason is None:
            candidates.append(item)
        else:
            exclusions.append(CandidateExclusion(evaluation_id=item.evaluation.id, reason=reason))
    return tuple(candidates), tuple(exclusions)


def _vectorizer(config: TfidfSelector, model: FittedTfidf | None = None) -> TfidfVectorizer:
    vectorizer = TfidfVectorizer(
        lowercase=True,
        strip_accents=None,
        analyzer="word",
        ngram_range=(1, 2),
        token_pattern=r"(?u)\b\w\w+\b",
        stop_words=None,
        min_df=1,
        max_df=1.0,
        max_features=config.max_features,
        norm="l2",
        use_idf=True,
        smooth_idf=True,
        sublinear_tf=False,
        dtype=np.float64,
        vocabulary=None
        if model is None
        else {term: index for index, term in enumerate(model.vocabulary)},
    )
    if model is not None:
        vectorizer.idf_ = np.asarray(model.idf, dtype=np.float64)
    return vectorizer


def fit_tfidf(
    train: tuple[TrainingExample, ...], config: TfidfSelector, seed: int
) -> Result[FittedTfidf, ArtifactError]:
    """The only fitting boundary. Callers pass train members, never all documents."""
    if {item.is_relevant for item in train} != {False, True}:
        return Err(ArtifactError("fit_tfidf", None, "Training partition needs two classes"))
    try:
        with threadpool_limits(limits=1), warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            vectorizer = _vectorizer(config)
            matrix = vectorizer.fit_transform([item.post.content or "" for item in train])
            classifier = LogisticRegression(
                l1_ratio=0.0,
                C=config.regularization_c,
                solver="liblinear",
                class_weight=config.class_weight,
                max_iter=config.max_iterations,
                tol=1e-8,
                random_state=seed,
                fit_intercept=True,
                intercept_scaling=1.0,
            )
            classifier.fit(matrix, [int(item.is_relevant) for item in train])
            return Ok(
                FittedTfidf(
                    train_evaluation_ids=tuple(item.evaluation_id for item in train),
                    vocabulary=tuple(str(term) for term in vectorizer.get_feature_names_out()),
                    idf=tuple(float(value) for value in vectorizer.idf_),
                    coefficients=tuple(float(value) for value in classifier.coef_[0]),
                    intercept=float(classifier.intercept_[0]),
                    iterations=int(classifier.n_iter_[0]),
                )
            )
    except (ValueError, ConvergenceWarning, FloatingPointError):
        return Err(
            ArtifactError(
                "fit_tfidf",
                None,
                "Cannot fit training data: empty vocabulary or nonconvergent model",
            )
        )


def score_texts(
    documents: tuple[tuple[int, str], ...], model: FittedTfidf, config: TfidfSelector
) -> Result[tuple[ScoredCandidate, ...], ArtifactError]:
    if not documents:
        return Ok(())
    try:
        with threadpool_limits(limits=1):
            matrix = _vectorizer(config, model).transform([text for _, text in documents])
            scores: list[ScoredCandidate] = []
            for index, (evaluation_id, _) in enumerate(documents):
                row = matrix.getrow(index)
                contributions = tuple(
                    TermContribution(
                        term=model.vocabulary[int(column)],
                        contribution=float(value) * model.coefficients[int(column)],
                    )
                    for column, value in zip(row.indices, row.data, strict=True)
                )
                logit = model.intercept + math.fsum(item.contribution for item in contributions)
                probability = (
                    1 / (1 + math.exp(-logit))
                    if logit >= 0
                    else math.exp(logit) / (1 + math.exp(logit))
                )
                scores.append(
                    ScoredCandidate(
                        evaluation_id=evaluation_id,
                        probability=probability,
                        explanation=tuple(
                            sorted(
                                contributions,
                                key=lambda item: (-abs(item.contribution), item.term),
                            )[:8]
                        ),
                    )
                )
            return Ok(tuple(scores))
    except (ValueError, IndexError, FloatingPointError):
        return Err(ArtifactError("score_tfidf", None, "Invalid retained model or scoring inputs"))


def select_random(
    ids: tuple[int, ...], config: RandomSelector
) -> Result[SelectorResult, ArtifactError]:
    if len(set(ids)) != len(ids):
        return Err(ArtifactError("select_random", None, "Duplicate population evaluation identity"))
    if config.count > len(ids):
        return Err(
            ArtifactError("select_random", None, "Requested random count exceeds eligible pool")
        )
    return Ok(
        SelectorResult(
            kind=config.kind,
            population_evaluation_ids=tuple(sorted(ids)),
            selected_evaluation_ids=tuple(
                random.Random(config.seed).sample(sorted(ids), config.count)
            ),
        )
    )


def select_ranked(
    scores: tuple[ScoredCandidate, ...],
    candidates: tuple[RejectedInput, ...],
    config: TfidfSelector,
) -> SelectorResult:
    by_id = {item.evaluation.id: item for item in candidates}
    selected: list[int] = []
    seen: set[ArtifactDigest] = set()
    ordered = sorted(scores, key=lambda item: (-round(item.probability, 12), item.evaluation_id))
    for score in ordered:
        post = by_id[score.evaluation_id].post
        if post is None:
            continue  # Eligibility requires post; no substitute identity.
        key = duplicate_key(post)
        if key not in seen and len(selected) < config.count:
            selected.append(score.evaluation_id)
            seen.add(key)
    return SelectorResult(
        kind=config.kind,
        population_evaluation_ids=tuple(sorted(by_id)),
        selected_evaluation_ids=tuple(selected),
        scores=tuple(ordered),
        explanation_method="tfidf-times-coefficient/v1",
    )


def assemble_queue(
    population: RejectedPopulation, ranked: SelectorResult | None, random_result: SelectorResult
) -> ReviewQueue:
    from scout.grading.assistance_wire import encode_population

    ranked_ids = () if ranked is None else ranked.selected_evaluation_ids
    random_ids = random_result.selected_evaluation_ids
    ranked_positions = {
        evaluation_id: position for position, evaluation_id in enumerate(ranked_ids, 1)
    }
    random_positions = {
        evaluation_id: position for position, evaluation_id in enumerate(random_ids, 1)
    }
    by_id = {item.evaluation.id: item for item in population.items}
    grouped: dict[ArtifactDigest, list[QueueSource]] = {}
    for evaluation_id in dict.fromkeys((*ranked_ids, *random_ids)):
        post = by_id[evaluation_id].post
        if post is None:
            continue
        grouped.setdefault(duplicate_key(post), []).append(
            QueueSource(
                evaluation_id=evaluation_id,
                ranked_position=ranked_positions.get(evaluation_id),
                random_position=random_positions.get(evaluation_id),
            )
        )
    # Preserve nonselected duplicates as source links, without pretending they
    # were independently selected or propagating a grade to their evaluations.
    selected_ids = set(ranked_ids) | set(random_ids)
    for evaluation_id in random_result.population_evaluation_ids:
        post = by_id[evaluation_id].post
        if evaluation_id not in selected_ids and post is not None:
            key = duplicate_key(post)
            if key in grouped:
                grouped[key].append(
                    QueueSource(
                        evaluation_id=evaluation_id,
                        ranked_position=None,
                        random_position=None,
                    )
                )
    return ReviewQueue(
        project_key=population.project_key,
        population_digest=digest_artifact(encode_population(population)),
        items=tuple(
            ReviewQueueItem(duplicate_key=key, sources=tuple(items))
            for key, items in grouped.items()
        ),
        ranked=ranked,
        random=random_result,
    )


def confusion(truth: tuple[bool, ...], predictions: tuple[bool, ...]) -> ConfusionCounts:
    counts = Counter(zip(truth, predictions, strict=True))
    return ConfusionCounts(
        true_positive=counts[True, True],
        true_negative=counts[False, False],
        false_positive=counts[False, True],
        false_negative=counts[True, False],
    )


def execute_assistance(
    examples: tuple[TrainingExample, ...],
    population: RejectedPopulation,
    partition: FrozenPartition,
    config: AssistanceConfig,
) -> Result[AssistanceOutputs, ArtifactError]:
    checked = validate_partition(examples, population, partition)
    if isinstance(checked, Err):
        return checked
    candidates, exclusions = eligible_candidates(population, examples)
    random_result = select_random(tuple(item.evaluation.id for item in candidates), config.random)
    if isinstance(random_result, Err):
        return random_result
    model = None
    ranked = None
    heldout = None
    if config.ranked is not None:
        train_ids = {item.evaluation_id for item in partition.members if item.partition == "train"}
        train = tuple(item for item in examples if item.evaluation_id in train_ids)
        test = tuple(item for item in examples if item.evaluation_id not in train_ids)
        fitted = fit_tfidf(train, config.ranked, config.seed)
        if isinstance(fitted, Err):
            return fitted
        model = fitted.value
        scored = score_texts(
            tuple(
                (item.evaluation.id, item.post.content or "")
                for item in candidates
                if item.post is not None
            ),
            model,
            config.ranked,
        )
        if isinstance(scored, Err):
            return scored
        ranked = select_ranked(scored.value, candidates, config.ranked)
        test_scores = score_texts(
            tuple((item.evaluation_id, item.post.content or "") for item in test),
            model,
            config.ranked,
        )
        if isinstance(test_scores, Err):
            return test_scores
        truth = tuple(item.is_relevant for item in test)
        majority = sum(item.is_relevant for item in train) > len(train) / 2
        heldout = HeldoutComparison(
            sample_count=len(test),
            random_provenance_count=sum(
                any(p.method == "random" for p in i.provenance) for i in test
            ),
            classifier=confusion(
                truth, tuple(score.probability >= 0.5 for score in test_scores.value)
            ),
            train_majority_baseline=confusion(truth, (majority,) * len(test)),
        )
    queue = assemble_queue(population, ranked, random_result.value)
    ranked_ids = set(() if ranked is None else ranked.selected_evaluation_ids)
    random_ids = set(random_result.value.selected_evaluation_ids)
    selected_count = len(ranked_ids | random_ids)
    return Ok(
        AssistanceOutputs(
            partition=partition,
            model=model,
            queue=queue,
            report=AssistanceReport(
                project_key=population.project_key,
                source_count=len(population.items),
                candidate_count=len(candidates),
                candidate_duplicate_count=len(candidates)
                - len({duplicate_key(item.post) for item in candidates if item.post is not None}),
                selected_evaluation_count=selected_count,
                queue_item_count=len(queue.items),
                duplicates_removed=selected_count - len(queue.items),
                overlap_count=len(ranked_ids & random_ids),
                exclusions=exclusions,
                heldout=heldout,
            ),
        )
    )


def validate_partition(
    examples: tuple[TrainingExample, ...],
    population: RejectedPopulation,
    partition: FrozenPartition,
) -> Result[None, ArtifactError]:
    """Enforce the leakage invariant at the fitting boundary, not just in docs."""
    members = {item.evaluation_id: item for item in partition.members}
    if len(members) != len(partition.members) or set(members) != {
        i.evaluation_id for i in examples
    }:
        return Err(
            ArtifactError("validate_partition", None, "Partition must cover each example once")
        )
    groups = group_posts(
        (*[i.post for i in examples], *[i.post for i in population.items if i.post is not None])
    )
    assignments: dict[GroupId, str] = {}
    for example in examples:
        member = members[example.evaluation_id]
        if member.input_digest != example.input_digest or (
            example.exposed and member.partition == "heldout"
        ):
            return Err(ArtifactError("validate_partition", None, "Input or exposure mismatch"))
        key = groups[example.post.id]
        if key in assignments and assignments[key] != member.partition:
            return Err(
                ArtifactError("validate_partition", None, "Related group crosses partitions")
            )
        assignments[key] = member.partition
    return Ok(None)


def report_queue_reviews(
    queue: ReviewQueue, outcomes: tuple[ReviewOutcome, ...]
) -> Result[QueueReviewReport, ArtifactError]:
    """Keep random denominator intact. No completed-subset rate or overall recall."""
    from scout.grading.assistance_wire import encode_queue

    if len({item.evaluation_id for item in outcomes}) != len(outcomes):
        return Err(ArtifactError("review_report", None, "Conflicting review outcomes"))
    labels = {item.evaluation_id: item.is_relevant for item in outcomes}
    sample = queue.random.selected_evaluation_ids
    population_ids = queue.random.population_evaluation_ids
    if (
        len(set(sample)) != len(sample)
        or len(set(population_ids)) != len(population_ids)
        or not set(sample) <= set(population_ids)
    ):
        return Err(ArtifactError("review_report", None, "Invalid random sample membership"))
    missing = tuple(value for value in sample if value not in labels)
    positive = sum(labels.get(value, False) for value in sample)
    rate = None
    low = None
    high = None
    if sample and not missing:
        rate = positive / len(sample)
        if len(sample) == len(queue.random.population_evaluation_ids):
            low = high = rate  # Complete eligible-population census.
        else:
            z = 1.959963984540054
            denominator = 1 + z * z / len(sample)
            center = (rate + z * z / (2 * len(sample))) / denominator
            half = (
                z
                * math.sqrt(rate * (1 - rate) / len(sample) + z * z / (4 * len(sample) ** 2))
                / denominator
            )
            low, high = max(0.0, center - half), min(1.0, center + half)
    ranked = None
    if queue.ranked is not None:
        ids = queue.ranked.selected_evaluation_ids
        reviewed = sum(value in labels for value in ids)
        discovered = sum(labels.get(value, False) for value in ids)
        ranked = ReviewYield(
            selected_count=len(ids),
            reviewed_count=reviewed,
            relevant_count=discovered,
            discovery_yield=discovered / reviewed if reviewed else None,
        )
    return Ok(
        QueueReviewReport(
            queue_digest=digest_artifact(encode_queue(queue)),
            outcomes=tuple(
                sorted(
                    (
                        item
                        for item in outcomes
                        if item.evaluation_id
                        in (
                            set(sample)
                            | set(
                                () if queue.ranked is None else queue.ranked.selected_evaluation_ids
                            )
                        )
                    ),
                    key=lambda item: item.evaluation_id,
                )
            ),
            random=RandomRate(
                population_count=len(queue.random.population_evaluation_ids),
                sample_count=len(sample),
                reviewed_count=len(sample) - len(missing),
                relevant_count=positive,
                unreviewed_evaluation_ids=missing,
                estimated_relevant_fraction=rate,
                wilson_95_low=low,
                wilson_95_high=high,
            ),
            ranked=ranked,
        )
    )
