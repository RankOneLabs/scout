"""CLI handlers for `scout feedback`: offline single-phase replay, batch/
sweep replay, retry, and reporting.

Preview (the default, no --execute-paid-replay) makes zero database writes
and zero model calls. --execute-paid-replay authorizes exactly one paid
candidate replay against the trusted baseline resolved from
--phase-run-id (single) or a --authorize-plan-sha256-gated batch/sweep
population.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import scout.replay.experiments as ee
import scout.replay.reporting as rr
from scout.config import DB_PATH
from scout.paa.replay_records import build_replay_paa_export, render_replay_paa_export
from scout.replay.experiments import (
    ExperimentOutcome,
    ReplayError,
    ReplayPreview,
    execute_replay,
    preview_replay,
)
from scout.replay.pricing import PricingCatalog, PricingCatalogError, load_pricing_catalog
from scout.replay.runtime import replay_runtime
from scout.result import Err


def positive_int(value: str) -> int:
    """argparse type for --phase-run-id: a positive integer only."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be a positive integer")
    return parsed


def _read_prompt_file(path: str) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        print(f"error: could not read --prompt-file {path!r}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"error: --prompt-file {path!r} is not valid UTF-8: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _print_preview(preview: ReplayPreview) -> None:
    print(f"phase: {preview.phase}")
    print(f"baseline model: {preview.baseline_model}")
    print(f"candidate model: {preview.candidate_model}")
    print(f"baseline system_prompt sha256: {preview.baseline_prompt_sha256}")
    print(f"candidate system_prompt sha256: {preview.candidate_prompt_sha256}")
    print(f"baseline prompt reused: {preview.baseline_prompt_reused}")
    print(f"recorded input sha256: {preview.recorded_input_sha256}")
    print(f"recorded input reused: {preview.recorded_input_reused}")
    print(
        f"feedback snapshot identity: snapshot_phase_id={preview.snapshot_phase_id} "
        f"snapshot_id={preview.snapshot_id} policy_version={preview.feedback_policy_version}"
    )
    print(f"trusted max_llm_calls: {preview.max_llm_calls}")
    print(f"no-op: {preview.is_no_op}")
    print(
        "warning: executing with --execute-paid-replay creates exactly one "
        f"candidate AGENT_RUN trace and may issue up to {preview.max_llm_calls} "
        "paid provider attempts."
    )


def _print_outcome(outcome: ExperimentOutcome) -> None:
    print(f"experiment #{outcome.experiment_id} complete")
    print(f"candidate trace: {outcome.candidate_trace_id}")
    print(f"candidate llm_call_count: {outcome.candidate_llm_call_count}")
    cost = "unavailable" if outcome.candidate_cost is None else f"{outcome.candidate_cost:.6f}"
    print(f"candidate cost: {cost}")


def replay_feedback(args: argparse.Namespace) -> None:
    """Handle `scout feedback replay`: preview (default) or execute
    (--execute-paid-replay) a single-phase offline candidate replay."""
    name = args.name
    if not name or not name.strip():
        print("error: --name must not be blank", file=sys.stderr)
        raise SystemExit(2)

    system_prompt_override = _read_prompt_file(args.prompt_file) if args.prompt_file else None

    async def _run() -> None:
        async with replay_runtime(db_path=DB_PATH) as rt:
            if not args.execute_paid_replay:
                preview = await preview_replay(
                    state=rt.state,
                    tracer=rt.tracer,
                    phase_run_id=args.phase_run_id,
                    model_override=args.model,
                    system_prompt_override=system_prompt_override,
                )
                _print_preview(preview)
                return
            outcome = await execute_replay(
                state=rt.state,
                tracer=rt.tracer,
                feedback=rt.feedback,
                phase_run_id=args.phase_run_id,
                name=name,
                model_override=args.model,
                system_prompt_override=system_prompt_override,
            )
            _print_outcome(outcome)

    try:
        asyncio.run(_run())
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _print_batch_preview(preview: ee.BatchPreview) -> None:
    plan = preview.plan
    catalog = plan.pricing_catalog
    print(f"population: {len(plan.phase_run_ids)} case(s)")
    if plan.dropped_duplicate_phase_run_ids:
        print(f"dropped duplicate baselines: {list(plan.dropped_duplicate_phase_run_ids)}")
    print(f"variants: {[variant.name for variant in plan.variants]}")
    print(f"scored: {preview.scored_count}")
    print(f"unscored: {preview.unscored_count}")
    print(f"no-op: {preview.no_op_count}")
    print(f"unpriceable: {preview.unpriceable_count}")
    print(f"selected: {preview.selected_count}")
    print(f"skipped: {preview.skipped_count}")
    print(
        f"price catalog: version={catalog.version} as_of={catalog.as_of} "
        f"hash={catalog.catalog_hash} source={catalog.source_url}"
    )
    for model, total in sorted(preview.total_estimated_usd_by_model.items()):
        print(f"estimated USD ({model}): {total:.6f}")
    print(f"estimated USD (total): {preview.total_estimated_usd:.6f}")
    print(f"max LLM calls per case: {preview.max_llm_calls_per_case}")
    print(f"aggregate max LLM calls: {preview.aggregate_max_llm_calls}")
    print(f"canonical plan sha256: {plan.plan_sha256}")
    for pair in plan.pairs:
        if pair.classification != "scored":
            print(
                f"  excluded: phase_run_id={pair.phase_run_id} variant={pair.variant_name!r} "
                f"classification={pair.classification} reason={pair.reason}"
            )


def _print_batch_outcome(outcome: ee.BatchExecutionOutcome) -> None:
    for name, run_id in sorted(outcome.experiment_run_ids.items()):
        print(f"experiment_run[{name}] = {run_id}")
    complete = sum(1 for attempt in outcome.attempts if attempt.status == "complete")
    failed = sum(1 for attempt in outcome.attempts if attempt.status == "failed")
    print(f"attempts complete: {complete}")
    print(f"attempts failed: {failed}")
    for attempt in outcome.attempts:
        detail = "" if attempt.error_detail is None else f" ({attempt.error_detail})"
        print(
            f"  phase_run_id={attempt.phase_run_id} variant={attempt.variant_name!r} "
            f"status={attempt.status}{detail}"
        )


def _resolve_batch_selector(args: argparse.Namespace) -> ee.BatchSelector:
    provided = []
    if args.phase_run_id:
        provided.append("phase_run_id")
    if args.scan_id is not None:
        provided.append("scan_id")
    if args.from_utc is not None or args.to_utc is not None:
        provided.append("window")
    if args.graded_with_corrections:
        provided.append("graded_with_corrections")
    if len(provided) != 1:
        print(
            "error: exactly one of --phase-run-id, --scan-id, --from/--to, or "
            "--graded-with-corrections is required",
            file=sys.stderr,
        )
        raise SystemExit(2)
    kind = provided[0]
    if kind == "phase_run_id":
        return ee.BatchSelector.by_phase_run_ids(args.phase_run_id)
    if kind == "scan_id":
        return ee.BatchSelector.by_scan_id(args.scan_id)
    if kind == "window":
        if args.from_utc is None or args.to_utc is None:
            print("error: --from and --to must be given together", file=sys.stderr)
            raise SystemExit(2)
        return ee.BatchSelector.by_window(args.from_utc, args.to_utc)
    return ee.BatchSelector.graded_with_corrections()


def _resolve_batch_variants(
    args: argparse.Namespace,
) -> tuple[tuple[ee.BatchVariant, ...], ee.SweepDefinition | None]:
    if args.sweep_file:
        if args.model or args.prompt_file:
            print(
                "error: --sweep-file cannot be combined with --model/--prompt-file",
                file=sys.stderr,
            )
            raise SystemExit(2)
        try:
            sweep = ee.load_and_validate_sweep(args.sweep_file)
        except ee.SweepValidationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        variants = ee.batch_variants_for_sweep(sweep, base_dir=Path(args.sweep_file).parent)
        return variants, sweep
    system_prompt_override = _read_prompt_file(args.prompt_file) if args.prompt_file else None
    return (
        (ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, args.model, system_prompt_override),),
        None,
    )


def _load_catalog_or_exit(pricing_catalog_path: str | None) -> PricingCatalog:
    try:
        return (
            load_pricing_catalog(pricing_catalog_path)
            if pricing_catalog_path
            else load_pricing_catalog()
        )
    except PricingCatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def batch_replay_feedback(args: argparse.Namespace) -> None:
    """Handle `scout feedback batch-replay`: preview (default) or execute
    (--execute-paid-replay, with --authorize-plan-sha256) a batch or sweep
    offline candidate replay against a selector-resolved population of
    trusted reply_draft baselines."""
    name = args.name
    if not name or not name.strip():
        print("error: --name must not be blank", file=sys.stderr)
        raise SystemExit(2)
    if args.execute_paid_replay and not args.authorize_plan_sha256:
        print(
            "error: --execute-paid-replay requires --authorize-plan-sha256", file=sys.stderr,
        )
        raise SystemExit(2)

    selector = _resolve_batch_selector(args)
    variants, sweep = _resolve_batch_variants(args)
    skip_policy = ee.SkipPolicy(
        skip_unscored=args.skip_unscored,
        skip_no_op=args.skip_no_op,
        skip_unpriceable=args.skip_unpriceable,
    )
    catalog = _load_catalog_or_exit(args.pricing_catalog)
    dossier_root = Path(args.dossier_root) if args.dossier_root else None

    async def _run() -> None:
        async with replay_runtime(db_path=DB_PATH) as rt:
            if not args.execute_paid_replay:
                preview = await ee.preview_batch_replay(
                    state=rt.state, tracer=rt.tracer, selector=selector, variants=variants,
                    skip_policy=skip_policy, pricing_catalog=catalog, dossier_root=dossier_root,
                    sweep=sweep,
                )
                _print_batch_preview(preview)
                return
            outcome = await ee.execute_batch_replay(
                state=rt.state, tracer=rt.tracer, feedback=rt.feedback, name=name,
                selector=selector, variants=variants, skip_policy=skip_policy,
                authorize_plan_sha256=args.authorize_plan_sha256, pricing_catalog=catalog,
                dossier_root=dossier_root, sweep=sweep,
            )
            _print_batch_outcome(outcome)

    try:
        asyncio.run(_run())
    except ee.ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def batch_retry_feedback(args: argparse.Namespace) -> None:
    """Handle `scout feedback batch-retry`: retry every (or an explicitly
    selected subset of) failed latest-attempt cases under one existing
    batch/sweep experiment_runs parent."""
    catalog = _load_catalog_or_exit(args.pricing_catalog)
    dossier_root = Path(args.dossier_root) if args.dossier_root else None
    phase_run_ids = tuple(args.phase_run_id) if args.phase_run_id else None

    async def _run() -> None:
        async with replay_runtime(db_path=DB_PATH) as rt:
            outcome = await ee.retry_batch_replay(
                state=rt.state, tracer=rt.tracer, feedback=rt.feedback,
                experiment_run_id=args.experiment_run_id, phase_run_ids=phase_run_ids,
                pricing_catalog=catalog, dossier_root=dossier_root,
            )
            _print_batch_outcome(outcome)

    try:
        asyncio.run(_run())
    except ee.ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def report_feedback(args: argparse.Namespace) -> None:
    """Handle `scout feedback report`: build the canonical batch/sweep
    report for one or more experiment_runs parent ids (a plain batch's one
    parent, or every variant parent sharing one sweep) and print it (or
    write it to --out) in the requested --format."""

    async def _run() -> None:
        async with replay_runtime(db_path=DB_PATH) as rt:
            if args.format == "paa-json":
                catalog_path = getattr(args, "pricing_catalog", None)
                catalog = (
                    load_pricing_catalog(catalog_path) if catalog_path else load_pricing_catalog()
                )
                result = await build_replay_paa_export(
                    rt.state, rt.tracer, experiment_run_ids=args.experiment_run_id, catalog=catalog,
                )
                if isinstance(result, Err):
                    raise rr.ReportError(str(result.error))
                rendered = render_replay_paa_export(result.value)
            else:
                report = rr.build_batch_report(rt.state, experiment_run_ids=args.experiment_run_id)
                rendered = (
                    rr.render_json(report) if args.format == "json" else rr.render_markdown(report)
                )
            if args.out:
                Path(args.out).write_text(rendered, encoding="utf-8")
                print(f"wrote {args.format} report to {args.out}")
            else:
                print(rendered)

    try:
        asyncio.run(_run())
    except (rr.ReportError, PricingCatalogError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = [
    "batch_replay_feedback",
    "batch_retry_feedback",
    "positive_int",
    "replay_feedback",
    "report_feedback",
]
