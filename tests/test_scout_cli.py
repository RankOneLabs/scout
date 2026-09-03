"""Regression tests for async resource cleanup in scout_cli command handlers.

Each CLI handler that owns feedback/tracer resources must await their close
methods. These tests verify that no RuntimeWarning: coroutine was never awaited
is emitted and that close is actually awaited (not just called).
"""

from __future__ import annotations

import pytest


class TestFeedbackBatchArgParsing:
    """`scout feedback batch-replay`/`batch-retry`/`report` argparse wiring
    — mirrors tests/test_scout_cli_paa.py's parse_args()/sys.argv pattern.
    Selector mutual-exclusivity itself is enforced by replay_cli's own
    validation (tests/test_replay_cli.py), not by argparse."""

    def test_batch_replay_phase_run_id_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            [
                "scout", "feedback", "batch-replay", "--phase-run-id", "1", "2", "3",
                "--name", "my-batch", "--model", "claude-sonnet-4-20250514",
                "--skip-unscored",
            ],
        )
        args = scout_cli.parse_args()
        assert args.subcommand == "feedback"
        assert args.feedback_command == "batch-replay"
        assert args.phase_run_id == [1, 2, 3]
        assert args.scan_id is None
        assert args.from_utc is None
        assert args.to_utc is None
        assert args.graded_with_corrections is False
        assert args.name == "my-batch"
        assert args.model == "claude-sonnet-4-20250514"
        assert args.skip_unscored is True
        assert args.skip_no_op is False
        assert args.execute_paid_replay is False

    def test_batch_replay_window_selector_and_execute_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            [
                "scout", "feedback", "batch-replay",
                "--from", "2026-01-01T00:00:00+00:00", "--to", "2026-02-01T00:00:00+00:00",
                "--name", "windowed", "--sweep-file", "sweep.yaml",
                "--execute-paid-replay", "--authorize-plan-sha256", "a" * 64,
            ],
        )
        args = scout_cli.parse_args()
        assert args.from_utc == "2026-01-01T00:00:00+00:00"
        assert args.to_utc == "2026-02-01T00:00:00+00:00"
        assert args.sweep_file == "sweep.yaml"
        assert args.execute_paid_replay is True
        assert args.authorize_plan_sha256 == "a" * 64

    def test_batch_retry_minimal_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            ["scout", "feedback", "batch-retry", "--experiment-run-id", "7"],
        )
        args = scout_cli.parse_args()
        assert args.subcommand == "feedback"
        assert args.feedback_command == "batch-retry"
        assert args.experiment_run_id == 7
        assert args.phase_run_id is None

    def test_batch_retry_restricted_to_phase_run_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            [
                "scout", "feedback", "batch-retry", "--experiment-run-id", "7",
                "--phase-run-id", "10", "11",
            ],
        )
        args = scout_cli.parse_args()
        assert args.phase_run_id == [10, 11]

    def test_report_minimal_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            ["scout", "feedback", "report", "--experiment-run-id", "3", "4"],
        )
        args = scout_cli.parse_args()
        assert args.subcommand == "feedback"
        assert args.feedback_command == "report"
        assert args.experiment_run_id == [3, 4]
        assert args.format == "markdown"
        assert args.out is None

    def test_report_json_format_and_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scout.cli.main as scout_cli

        monkeypatch.setattr(
            "sys.argv",
            [
                "scout", "feedback", "report", "--experiment-run-id", "3",
                "--format", "json", "--out", "report.json",
            ],
        )
        args = scout_cli.parse_args()
        assert args.format == "json"
        assert args.out == "report.json"
