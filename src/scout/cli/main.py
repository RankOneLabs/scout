"""CLI entry point — argparse, logging setup, and dispatch to subcommands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime

from paa_runtime import service as paa_service
from paa_runtime.config import RuntimeConfig
from paa_runtime.events import AUTONOMY_POSITIONS, MOTION_STATUSES
from paa_runtime.evidence import EvidenceError

from scout.config import DB_PATH, MODES, SCAN_INTERVAL_HOURS
from scout.paa.config import DEFAULT_EVIDENCE_ROOT, build_paa_config
from scout.paa.declarations import PaaDeclarationError
from scout.paa.event_store import ScoutEventStore
from scout.scanning.runner import main_loop, run_preflight
from scout.storage.state import StateManager

logger = logging.getLogger("scout.cli")

# The content-addressed PAA evidence store root — mirrors DB_PATH's
# testability pattern so tests can monkeypatch this module attribute
# instead of writing into the real repo's evidence/paa directory.
PAA_EVIDENCE_ROOT = DEFAULT_EVIDENCE_ROOT

def setup_logging(debug: bool = False) -> None:
    """Configure logging with appropriate level and format."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not debug:
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scout — find relevant Discord posts and draft engagement comments",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=f"Run continuously, scanning every {SCAN_INTERVAL_HOURS} hours",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show scan statistics and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--rescore",
        nargs="?",
        const="all",
        default=None,
        metavar="SCAN_ID",
        help=(
            "Re-evaluate posts from DB instead of fetching."
            " Use --rescore for all posts, or --rescore 3 for a specific scan"
        ),
    )
    parser.add_argument(
        "--rescore-failed",
        nargs="?",
        const="all",
        default=None,
        metavar="SCAN_ID",
        help=(
            "Re-evaluate only posts that have no evaluation (e.g. after API failure)."
            " Use --rescore-failed for all, or --rescore-failed 3 for a specific scan"
        ),
    )
    parser.add_argument(
        "--review",
        nargs="?",
        const="latest",
        default=None,
        metavar="SCAN_ID",
        help=(
            "Review and grade a scan interactively."
            " Use --review for latest, or --review 5 for a specific scan"
        ),
    )
    parser.add_argument(
        "--export-eval",
        nargs="?",
        const="eval_cases.json",
        default=None,
        metavar="OUTPUT_PATH",
        help="Export graded data as eval test cases (JSON)",
    )
    mode_choices = list(MODES.keys()) + ["both"]
    parser.add_argument(
        "--mode",
        choices=mode_choices,
        default=mode_choices[0],
        help=f"Scoring mode: {', '.join(MODES.keys())}, or both",
    )

    subparsers = parser.add_subparsers(dest="subcommand")
    from scout.cli.analysis import add_analysis_parser

    add_analysis_parser(subparsers, DB_PATH)
    preflight_p = subparsers.add_parser("preflight", help="Read-only Phase 1 deployment gate")
    preflight_p.add_argument("--dossier-root", required=True)
    preflight_p.add_argument("--db-path", default=DB_PATH)
    paa_parser = subparsers.add_parser("paa", help="PAA autonomy control-plane commands")
    paa_sub = paa_parser.add_subparsers(dest="paa_command", required=True)

    paa_propose_p = paa_sub.add_parser("propose", help="Propose an autonomy transition")
    paa_propose_p.add_argument("task")
    paa_propose_p.add_argument("--scope", default=None)
    paa_propose_p.add_argument("--to", required=True, choices=sorted(AUTONOMY_POSITIONS))
    paa_propose_p.add_argument("--evidence", required=True, help="Path to the evidence file")
    paa_propose_p.add_argument("--actor")
    paa_propose_p.add_argument("--reason")

    paa_approve_p = paa_sub.add_parser("approve", help="Approve a proposed motion")
    paa_approve_p.add_argument("motion_id")
    paa_approve_p.add_argument("--reason", required=True)
    paa_approve_p.add_argument("--actor")

    paa_reject_p = paa_sub.add_parser("reject", help="Reject a proposed motion")
    paa_reject_p.add_argument("motion_id")
    paa_reject_p.add_argument("--reason", required=True)
    paa_reject_p.add_argument("--actor")

    paa_demote_p = paa_sub.add_parser("demote", help="One-step emergency demotion")
    paa_demote_p.add_argument("task")
    paa_demote_p.add_argument("--scope", default=None)
    paa_demote_p.add_argument("--reason", required=True)
    paa_demote_p.add_argument("--actor")
    paa_demote_p.add_argument(
        "--source-row",
        dest="source_rows",
        action="append",
        default=[],
        metavar="table:id",
        help="Repeatable opaque source-row reference (e.g. --source-row posts:42)",
    )

    paa_show_p = paa_sub.add_parser("show", help="Show the resolved current position")
    paa_show_p.add_argument("task")
    paa_show_p.add_argument("--scope", default=None)

    paa_list_p = paa_sub.add_parser("list", help="List motions derived from event history")
    paa_list_p.add_argument("--status", choices=sorted(MOTION_STATUSES), default=None)
    paa_list_p.add_argument("--task", default=None)

    phase1_parser = subparsers.add_parser("phase1", help="Phase 1 rollout evidence")
    phase1_sub = phase1_parser.add_subparsers(dest="phase1_command", required=True)
    audit_p = phase1_sub.add_parser("audit", help="Run the read-only exit audit")
    audit_p.add_argument("--from", dest="window_from", required=True)
    audit_p.add_argument("--to", dest="window_to", required=True)
    audit_p.add_argument("--dossier-root", required=True)
    audit_p.add_argument("--db-path", default=DB_PATH)
    audit_p.add_argument(
        "--approval-references", help="JSON mapping of project to exact revision approval"
    )
    audit_p.add_argument("--corpus-dir")
    audit_p.add_argument("--strict", action="store_true")
    audit_p.add_argument("--format", choices=["json", "markdown"], default="markdown")
    audit_p.add_argument("--output", default="-")
    audit_p.add_argument(
        "--replace", action="store_true", help="Replace an existing evidence artifact"
    )

    bundle_p = phase1_sub.add_parser("bundle", help="Create or verify tamper-evident evidence")
    bundle_sub = bundle_p.add_subparsers(dest="bundle_command", required=True)
    bundle_create = bundle_sub.add_parser("create", help="Create a new same-source evidence bundle")
    bundle_create.add_argument("--report", required=True)
    bundle_create.add_argument("--db-path", default=DB_PATH)
    bundle_create.add_argument("--gate-block-id", required=True, type=int)
    bundle_create.add_argument("--before", required=True)
    bundle_create.add_argument("--output", required=True)
    bundle_create.add_argument("--code-revision", required=True)
    bundle_create.add_argument("--model-id", required=True)
    bundle_create.add_argument("--prompt-revision", required=True)
    bundle_create.add_argument("--force", action="store_true")
    bundle_verify = bundle_sub.add_parser("verify", help="Verify a bundle read-only")
    bundle_verify.add_argument("--bundle", required=True)
    bundle_verify.add_argument("--db-path")

    parent_p = phase1_sub.add_parser(
        "parent-change", help="Annotate how removing a resolved parent changes action"
    )
    parent_p.add_argument("evaluation_id", type=int)
    parent_p.add_argument(
        "--without-parent-relevance", choices=["relevant", "not_relevant"], required=True
    )
    parent_p.add_argument(
        "--without-parent-posture", choices=["answer", "engage", "ask", "abstain"], required=True
    )
    parent_p.add_argument("--assessor", required=True)
    parent_p.add_argument("--explanation", required=True)

    eval_parser = subparsers.add_parser("eval", help="Run Jig evaluation sweeps")
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_phase1_p = eval_sub.add_parser(
        "phase1", help="Live paid sweep of the Phase 1 corpus (operator-only)"
    )
    eval_phase1_p.add_argument(
        "--configs", required=True, help="Sweep configs YAML: one baseline + candidate variants"
    )
    eval_phase1_p.add_argument(
        "--dossier-root",
        required=True,
        help="dossier-source checkout to resolve pinned dossiers from",
    )
    eval_phase1_p.add_argument(
        "--corpus-dir", default=None, help="Override the default evals/phase1 corpus directory"
    )
    eval_phase1_p.add_argument(
        "--output", default=None, help="Write the SweepResult summary as JSON to this path"
    )

    grade_corpus_parser = subparsers.add_parser(
        "grade-corpus", help="Audit and remediate the grade corpus"
    )
    grade_corpus_sub = grade_corpus_parser.add_subparsers(
        dest="grade_corpus_command", required=True
    )

    gc_audit_p = grade_corpus_sub.add_parser("audit", help="Run the read-only grade corpus audit")
    gc_audit_p.add_argument(
        "--db-uri",
        required=True,
        help="Read-only SQLite URI; must include mode=ro",
    )
    gc_audit_p.add_argument("--manifest-output", default="grade_corpus_audit_manifest.json")
    gc_audit_p.add_argument("--report-output", default="grade_corpus_audit.md")
    gc_audit_p.add_argument(
        "--replace", action="store_true", help="Replace an existing manifest/report"
    )

    gc_remediate_p = grade_corpus_sub.add_parser(
        "remediate", help="Flag and regrade known-bad rows (gated behind --apply)"
    )
    gc_remediate_p.add_argument("--db-path", required=True, help="Writable database path")
    gc_remediate_p.add_argument(
        "--manifest", required=True, help="Audit manifest JSON produced by `grade-corpus audit`"
    )
    gc_remediate_p.add_argument("--replacement-manifest", help="Reviewed replacement manifest JSON")
    gc_remediate_p.add_argument("--apply", action="store_true")

    gc_convergence_audit_p = grade_corpus_sub.add_parser(
        "convergence-audit", help="Run the read-only grade_revisions convergence audit"
    )
    gc_convergence_audit_p.add_argument(
        "--db-uri",
        required=True,
        help="Read-only SQLite URI; must include mode=ro",
    )
    gc_convergence_audit_p.add_argument(
        "--manifest-output", default="grade_revision_convergence_manifest.json"
    )
    gc_convergence_audit_p.add_argument(
        "--report-output", default="comms/grade-revision-convergence-audit.md"
    )
    gc_convergence_audit_p.add_argument(
        "--replace", action="store_true", help="Replace an existing manifest/report"
    )

    gc_convergence_repair_p = grade_corpus_sub.add_parser(
        "convergence-repair",
        help="Append missing/divergent grade_revisions entries (gated behind --apply)",
    )
    gc_convergence_repair_p.add_argument("--db-path", required=True, help="Writable database path")
    gc_convergence_repair_p.add_argument(
        "--manifest",
        required=True,
        help="Convergence audit manifest JSON produced by `grade-corpus convergence-audit`",
    )
    gc_convergence_repair_p.add_argument("--apply", action="store_true")

    from scout.cli.replay import positive_int

    feedback_parser = subparsers.add_parser(
        "feedback", help="Offline replay and comparison commands"
    )
    feedback_sub = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    replay_p = feedback_sub.add_parser(
        "replay",
        help=(
            "Preview (default) or execute a single explicitly authorized "
            "single-phase offline candidate replay against a trusted baseline"
        ),
    )
    replay_p.add_argument(
        "--phase-run-id", required=True, type=positive_int,
        help="evaluation_phase_runs id to resolve the trusted baseline from",
    )
    replay_p.add_argument("--name", required=True, help="Name for the new experiment")
    replay_p.add_argument(
        "--model", default=None,
        help="Candidate model override (defaults to the baseline's resolved model)",
    )
    replay_p.add_argument(
        "--prompt-file", default=None,
        help="UTF-8 text file replacing only the candidate's AgentConfig.system_prompt",
    )
    replay_p.add_argument(
        "--execute-paid-replay", action="store_true",
        help="Authorize execution: without this flag, replay only previews read-only",
    )

    batch_replay_p = feedback_sub.add_parser(
        "batch-replay",
        help=(
            "Preview (default) or execute a plan-hash-authorized batch or sweep offline "
            "candidate replay against a selector-resolved reply_draft baseline population"
        ),
    )
    batch_selector_group = batch_replay_p.add_argument_group("selector (exactly one required)")
    batch_selector_group.add_argument(
        "--phase-run-id", type=positive_int, nargs="+", default=None,
        help="One or more explicit evaluation_phase_runs ids",
    )
    batch_selector_group.add_argument(
        "--scan-id", type=positive_int, default=None,
        help="Every complete reply_draft phase run in this scan",
    )
    batch_selector_group.add_argument(
        "--from", dest="from_utc", default=None,
        help="Start of the UTC [from, to) window (requires --to)",
    )
    batch_selector_group.add_argument(
        "--to", dest="to_utc", default=None,
        help="End of the UTC [from, to) window (requires --from)",
    )
    batch_selector_group.add_argument(
        "--graded-with-corrections", action="store_true",
        help="Every complete reply_draft phase run with a recorded human correction",
    )
    batch_replay_p.add_argument("--name", required=True, help="Name for the new experiment run(s)")
    batch_replay_p.add_argument(
        "--model", default=None,
        help="Candidate model override applied to every case (plain batch only, not --sweep-file)",
    )
    batch_replay_p.add_argument(
        "--prompt-file", default=None,
        help=(
            "UTF-8 text file replacing every case's candidate system_prompt "
            "(plain batch only, not --sweep-file)"
        ),
    )
    batch_replay_p.add_argument(
        "--sweep-file", default=None,
        help="Path to a canonical YAML or JSON replay-sweep v1 document",
    )
    batch_replay_p.add_argument(
        "--skip-unscored", action="store_true",
        help="Exclude unscored cases (no resolvable correction oracle) rather than refusing",
    )
    batch_replay_p.add_argument(
        "--skip-no-op", action="store_true",
        help="Exclude no-op pairs (candidate identical to baseline) rather than refusing",
    )
    batch_replay_p.add_argument(
        "--skip-unpriceable", action="store_true",
        help="Exclude unpriceable pairs (missing usage or pricing) rather than refusing",
    )
    batch_replay_p.add_argument(
        "--pricing-catalog", default=None,
        help="Path to a replay-pricing v1 catalog (defaults to contracts/replay-pricing.v1.json)",
    )
    batch_replay_p.add_argument(
        "--dossier-root", default=None, help="Override the dossier git checkout root",
    )
    batch_replay_p.add_argument(
        "--authorize-plan-sha256", default=None,
        help="The canonical plan SHA-256 printed by preview -- required for --execute-paid-replay",
    )
    batch_replay_p.add_argument(
        "--execute-paid-replay", action="store_true",
        help="Authorize execution: without this flag, batch-replay only previews read-only",
    )

    batch_retry_p = feedback_sub.add_parser(
        "batch-retry",
        help="Retry every (or an explicitly selected subset of) failed latest-attempt cases "
        "under one existing batch/sweep experiment_runs parent",
    )
    batch_retry_p.add_argument(
        "--experiment-run-id", type=positive_int, required=True,
        help="The batch/sweep experiment_runs parent id to retry failed cases under",
    )
    batch_retry_p.add_argument(
        "--phase-run-id", type=positive_int, nargs="*", default=None,
        help="Restrict the retry to these phase_run_ids (default: every failed latest attempt)",
    )
    batch_retry_p.add_argument(
        "--pricing-catalog", default=None,
        help="Path to a replay-pricing v1 catalog (defaults to contracts/replay-pricing.v1.json)",
    )
    batch_retry_p.add_argument(
        "--dossier-root", default=None, help="Override the dossier git checkout root",
    )

    report_p = feedback_sub.add_parser(
        "report",
        help="Build the canonical segmented JSON/Markdown report for one or more batch/sweep "
        "experiment_runs parent ids",
    )
    report_p.add_argument(
        "--experiment-run-id", type=positive_int, nargs="+", required=True,
        help="One or more experiment_runs parent ids (a sweep's variants share one report)",
    )
    report_p.add_argument(
        "--format", choices=["markdown", "json", "paa-json"], default="markdown",
        help="Output format (default: markdown)",
    )
    report_p.add_argument(
        "--out", default=None, help="Write the report to this file instead of stdout",
    )
    report_p.add_argument(
        "--pricing-catalog", default=None,
        help="Catalog used to price candidate usage in --format paa-json; estimates, not invoices",
    )

    return parser.parse_args()


def show_stats() -> None:
    """Display scan statistics from the database."""
    with StateManager(db_path=DB_PATH) as state:
        stats = state.get_scan_stats()

    print("Scout Statistics")
    print("=" * 40)
    print(f"Total scans:          {stats.total_scans}")
    print(f"Total posts seen:     {stats.total_posts}")
    print(f"Relevant posts found: {stats.total_relevant}")
    print(f"Draft comments:       {stats.total_drafts}")


def export_eval(args: argparse.Namespace) -> None:
    """Export graded data as eval test cases (JSON)."""
    with StateManager(db_path=DB_PATH) as state:
        cases = state.export_eval_cases()
        with open(args.export_eval, "w") as f:
            json.dump(cases, f, indent=2)
        print(f"Exported {len(cases)} eval cases to {args.export_eval}")


def review(args: argparse.Namespace) -> None:
    """Review and grade a scan interactively."""
    from scout.grading.service import review_scan

    with StateManager(db_path=DB_PATH) as state:
        if args.review == "latest":
            latest_scan_id = state.get_latest_completed_scan_id()
            if latest_scan_id is None:
                print("No completed scans found.")
                raise SystemExit(1)
            scan_id = latest_scan_id
        else:
            scan_id = int(args.review)
        review_scan(state, scan_id)
        state.commit()


_LIST_COLUMNS = ("id", "source", "status", "channels", "days", "ver", "title")


def _paa_config() -> RuntimeConfig:
    """Scout's runtime config for one CLI invocation.

    Reads PAA_EVIDENCE_ROOT at call time rather than import time so the
    module-level seam tests monkeypatch keeps working now that evidence
    root is config rather than a per-call keyword argument.
    """
    return build_paa_config(evidence_root=PAA_EVIDENCE_ROOT)


def _print_paa_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def paa_propose(args: argparse.Namespace) -> None:
    """Handle `scout paa propose`: insert one motion_proposed event."""
    from pathlib import Path

    with StateManager(db_path=DB_PATH) as state:
        motion = paa_service.propose(
            ScoutEventStore(state),
            _paa_config(),
            task=args.task,
            scope=args.scope,
            to_position=args.to,
            evidence_path=Path(args.evidence),
            actor=args.actor,
            reason=args.reason,
        )
    _print_paa_json(motion.to_json_dict())


def paa_approve(args: argparse.Namespace) -> None:
    """Handle `scout paa approve`: atomically authorize and execute a proposal."""
    with StateManager(db_path=DB_PATH) as state:
        motion = paa_service.approve(
            ScoutEventStore(state),
            _paa_config(),
            motion_id=args.motion_id,
            reason=args.reason,
            actor=args.actor,
        )
    _print_paa_json(motion.to_json_dict())


def paa_reject(args: argparse.Namespace) -> None:
    """Handle `scout paa reject`: terminally reject an unexecuted proposal."""
    with StateManager(db_path=DB_PATH) as state:
        motion = paa_service.reject(
            ScoutEventStore(state),
            _paa_config(),
            motion_id=args.motion_id,
            reason=args.reason,
            actor=args.actor,
        )
    _print_paa_json(motion.to_json_dict())


def paa_demote(args: argparse.Namespace) -> None:
    """Handle `scout paa demote`: one-command emergency demotion."""
    with StateManager(db_path=DB_PATH) as state:
        motion = paa_service.demote(
            ScoutEventStore(state),
            _paa_config(),
            task=args.task,
            scope=args.scope,
            reason=args.reason,
            actor=args.actor,
            source_rows=args.source_rows,
        )
    _print_paa_json(motion.to_json_dict())


def paa_show(args: argparse.Namespace) -> None:
    """Handle `scout paa show`: the declaration and resolved current position."""
    with StateManager(db_path=DB_PATH) as state:
        result = paa_service.show(
            ScoutEventStore(state), _paa_config(), task=args.task, scope=args.scope,
        )
    _print_paa_json(result)


def paa_list(args: argparse.Namespace) -> None:
    """Handle `scout paa list`: every motion derived from event history."""
    with StateManager(db_path=DB_PATH) as state:
        motions = paa_service.list_motions(
            ScoutEventStore(state), status=args.status, task=args.task,
        )
    _print_paa_json({"motions": [m.to_json_dict() for m in motions]})


def main() -> None:
    args = parse_args()
    setup_logging(debug=args.debug)
    if args.subcommand == "analysis":
        from scout.cli.analysis import run_analysis
        from scout.result import Err, Ok

        match run_analysis(args):
            case Ok(result):
                print(result.model_dump_json(indent=2))
            case Err(error):
                print(f"{error.operation}: {error.detail}", file=sys.stderr)
                raise SystemExit(1)
        return

    if args.subcommand == "preflight":
        report = run_preflight(args.db_path, args.dossier_root)
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1 if report["errors"] else 0)
    if args.subcommand == "phase1":
        from pathlib import Path

        from scout.paa.audit.runner import (
            canonical_json,
            parse_utc,
            record_parent_change,
            render_markdown,
            run_audit,
        )

        if args.phase1_command == "parent-change":
            assessment_id = record_parent_change(
                Path(DB_PATH),
                evaluation_id=args.evaluation_id,
                without_parent_relevance=args.without_parent_relevance,
                without_parent_posture=args.without_parent_posture,
                assessor=args.assessor,
                explanation=args.explanation,
            )
            print(f"Recorded parent-context assessment #{assessment_id}")
            return
        if args.phase1_command == "bundle":
            from scout.paa.evidence.bundle import BundleError, create_bundle, verify_bundle

            try:
                if args.bundle_command == "create":
                    bundle_result = create_bundle(
                        report_path=Path(args.report),
                        db_path=Path(args.db_path),
                        gate_block_id=args.gate_block_id,
                        before_path=Path(args.before),
                        destination=Path(args.output),
                        code_revision=args.code_revision,
                        model_id=args.model_id,
                        prompt_revision=args.prompt_revision,
                        force=args.force,
                    )
                    print(
                        canonical_json({"bundle": str(bundle_result.path), "force": args.force}),
                        end="",
                    )
                else:
                    print(
                        canonical_json(
                            verify_bundle(
                                Path(args.bundle), Path(args.db_path) if args.db_path else None
                            )
                        ),
                        end="",
                    )
            except (BundleError, OSError, sqlite3.Error) as exc:
                print(f"Phase 1 bundle error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
            return
        approvals: dict[str, dict[str, str]] | None = None
        if args.approval_references:
            try:
                loaded = json.loads(Path(args.approval_references).read_text())
                if not isinstance(loaded, dict):
                    raise ValueError("approval references must be a JSON object")
                approvals = loaded
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Phase 1 audit error: invalid approval references: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
        try:
            audit_result = run_audit(
                Path(args.db_path),
                Path(args.dossier_root),
                window_from=parse_utc(args.window_from),
                window_to=parse_utc(args.window_to),
                strict=args.strict,
                approval_references=approvals,
                corpus_dir=Path(args.corpus_dir) if args.corpus_dir else None,
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f"Phase 1 audit error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        rendered = (
            canonical_json(audit_result.report)
            if args.format == "json"
            else render_markdown(audit_result.report)
        )
        if args.output == "-":
            print(rendered, end="")
        else:
            output = Path(args.output)
            if output.exists() and not args.replace:
                print(
                    f"Refusing to replace existing evidence artifact: {output}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            markdown_output = output.with_suffix(".md") if args.format == "json" else None
            if markdown_output is not None and markdown_output.exists() and not args.replace:
                print(
                    f"Refusing to replace existing evidence artifact: {markdown_output}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered)
            if markdown_output is not None:
                markdown_output.write_text(render_markdown(audit_result.report))
        raise SystemExit(0 if audit_result.passed else 1)
    if args.subcommand == "eval":
        from pathlib import Path

        if args.eval_command == "phase1":
            from scout.evals.phase1.loader import DEFAULT_CORPUS_DIR
            from scout.evals.phase1.runner import run_live_operator_gate

            exit_code = run_live_operator_gate(
                corpus_dir=Path(args.corpus_dir) if args.corpus_dir else DEFAULT_CORPUS_DIR,
                dossier_root=Path(args.dossier_root),
                configs_path=Path(args.configs),
                output_path=Path(args.output) if args.output else None,
            )
            raise SystemExit(exit_code)
        return
    if args.subcommand == "grade-corpus":
        from pathlib import Path

        from scripts import grade_corpus_audit as gca

        if args.grade_corpus_command == "audit":
            try:
                gc_manifest = gca.run_audit(args.db_uri, now=datetime.now(UTC))
            except gca.AuditError as exc:
                print(f"grade-corpus audit error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
            manifest_path = Path(args.manifest_output)
            if manifest_path.exists() and not args.replace:
                print(f"Refusing to replace existing manifest: {manifest_path}", file=sys.stderr)
                raise SystemExit(2)
            manifest_path.write_text(gca.canonical_json(gc_manifest))
            report_path = Path(args.report_output)
            if report_path.exists() and not args.replace:
                print(f"Refusing to replace existing report: {report_path}", file=sys.stderr)
                raise SystemExit(2)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(gca.render_markdown(gc_manifest))
            print(f"Wrote manifest to {manifest_path} and report to {report_path}")
            print(
                f"Affected total: {gc_manifest['affected_total']} / "
                f"{gc_manifest['total_eligible_grades']}"
            )
            return
        if args.grade_corpus_command == "convergence-audit":
            try:
                gc_convergence_manifest = gca.run_convergence_audit(
                    args.db_uri, now=datetime.now(UTC)
                )
            except gca.AuditError as exc:
                print(f"grade-corpus convergence-audit error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
            manifest_path = Path(args.manifest_output)
            if manifest_path.exists() and not args.replace:
                print(f"Refusing to replace existing manifest: {manifest_path}", file=sys.stderr)
                raise SystemExit(2)
            manifest_path.write_text(gca.canonical_json(gc_convergence_manifest))
            report_path = Path(args.report_output)
            if report_path.exists() and not args.replace:
                print(f"Refusing to replace existing report: {report_path}", file=sys.stderr)
                raise SystemExit(2)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(gca.render_convergence_markdown(gc_convergence_manifest))
            print(f"Wrote manifest to {manifest_path} and report to {report_path}")
            print(
                f"Converged: {gc_convergence_manifest['converged_count']} / "
                f"{gc_convergence_manifest['total_grades']} — "
                f"missing_revision: {gc_convergence_manifest['missing_revision_count']}, "
                f"divergent_revision: {gc_convergence_manifest['divergent_revision_count']}"
            )
            return
        if args.grade_corpus_command == "convergence-repair":
            try:
                gc_convergence_manifest = json.loads(Path(args.manifest).read_text())
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(
                    f"grade-corpus convergence-repair error: invalid manifest: {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(2) from exc
            try:
                gc_convergence_result = gca.convergence_repair(
                    args.db_path,
                    gc_convergence_manifest,
                    apply=args.apply,
                )
            except gca.AuditError as exc:
                print(f"grade-corpus convergence-repair error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
            print(gca.canonical_json(gc_convergence_result), end="")
            return
        # remediate
        try:
            gc_manifest = json.loads(Path(args.manifest).read_text())
            replacement_manifest = (
                json.loads(Path(args.replacement_manifest).read_text())
                if args.replacement_manifest
                else None
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"grade-corpus remediate error: invalid manifest: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        try:
            gc_result = gca.remediate(
                args.db_path,
                gc_manifest,
                apply=args.apply,
                replacement_manifest=replacement_manifest,
                now=datetime.now(UTC),
            )
        except gca.AuditError as exc:
            print(f"grade-corpus remediate error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(gca.canonical_json(gc_result), end="")
        return
    if args.subcommand == "paa":
        try:
            if args.paa_command == "propose":
                paa_propose(args)
            elif args.paa_command == "approve":
                paa_approve(args)
            elif args.paa_command == "reject":
                paa_reject(args)
            elif args.paa_command == "demote":
                paa_demote(args)
            elif args.paa_command == "show":
                paa_show(args)
            elif args.paa_command == "list":
                paa_list(args)
        except (
            paa_service.PaaServiceError, PaaDeclarationError, EvidenceError, OSError,
        ) as exc:
            print(f"paa {args.paa_command} error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return
    if args.subcommand == "feedback":
        from scout.cli.replay import (
            batch_replay_feedback,
            batch_retry_feedback,
            replay_feedback,
            report_feedback,
        )

        if args.feedback_command == "replay":
            replay_feedback(args)
        elif args.feedback_command == "batch-replay":
            batch_replay_feedback(args)
        elif args.feedback_command == "batch-retry":
            batch_retry_feedback(args)
        elif args.feedback_command == "report":
            report_feedback(args)
        return
    if args.stats:
        show_stats()
    elif args.export_eval is not None:
        export_eval(args)
    elif args.review is not None:
        review(args)
    else:
        asyncio.run(main_loop(args))


if __name__ == "__main__":
    main()
