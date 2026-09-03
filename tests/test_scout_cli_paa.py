"""Tests for `scout paa` CLI parsing, output, and error handling."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import scout.cli.main as scout_cli
from scout.cli.main import (
    paa_approve,
    paa_demote,
    paa_list,
    paa_propose,
    paa_reject,
    paa_show,
    parse_args,
)

INBOUND = "inbound_reply_surfacing"
CANONICAL = "canonical_promotion"


@pytest.fixture(autouse=True)
def _isolate_evidence_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the PAA evidence store away from the real repo checkout —
    mirrors conftest's _isolate_db_paths for DB_PATH."""
    monkeypatch.setattr(scout_cli, "PAA_EVIDENCE_ROOT", tmp_path / "evroot")


@pytest.fixture
def evidence_file(tmp_path: Path) -> Path:
    path = tmp_path / "evidence.json"
    path.write_text('{"report": "ok"}')
    return path


class TestArgParsing:
    def test_propose_minimal_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "propose", INBOUND, "--to", "hotl",
             "--evidence", "ev.json"],
        )
        args = parse_args()
        assert args.subcommand == "paa"
        assert args.paa_command == "propose"
        assert args.task == INBOUND
        assert args.scope is None
        assert args.to == "hotl"
        assert args.evidence == "ev.json"
        assert args.actor is None
        assert args.reason is None

    def test_propose_with_actor_and_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "propose", INBOUND, "--to", "hotl",
             "--evidence", "ev.json", "--actor", "operator", "--reason", "custom reason"],
        )
        args = parse_args()
        assert args.actor == "operator"
        assert args.reason == "custom reason"

    def test_propose_rejects_invalid_position_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "propose", INBOUND, "--to", "nonsense",
             "--evidence", "ev.json"],
        )
        with pytest.raises(SystemExit):
            parse_args()

    def test_approve_requires_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["scout", "paa", "approve", "motion-1"])
        with pytest.raises(SystemExit):
            parse_args()

    def test_approve_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "approve", "motion-1", "--reason", "go", "--actor", "ops"],
        )
        args = parse_args()
        assert args.paa_command == "approve"
        assert args.motion_id == "motion-1"
        assert args.reason == "go"
        assert args.actor == "ops"

    def test_reject_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv", ["scout", "paa", "reject", "motion-1", "--reason", "no"],
        )
        args = parse_args()
        assert args.paa_command == "reject"
        assert args.motion_id == "motion-1"
        assert args.reason == "no"
        assert args.actor is None

    def test_demote_parses_repeatable_source_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "demote", INBOUND, "--reason", "incident",
             "--source-row", "posts:5", "--source-row", "posts:2"],
        )
        args = parse_args()
        assert args.paa_command == "demote"
        assert args.source_rows == ["posts:5", "posts:2"]

    def test_demote_source_row_defaults_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "demote", INBOUND, "--reason", "incident"],
        )
        args = parse_args()
        assert args.source_rows == []

    def test_show_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv", ["scout", "paa", "show", INBOUND],
        )
        args = parse_args()
        assert args.paa_command == "show"
        assert args.task == INBOUND
        assert args.scope is None

    def test_list_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["scout", "paa", "list"])
        args = parse_args()
        assert args.paa_command == "list"
        assert args.status is None
        assert args.task is None

    def test_list_with_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "list", "--status", "executed", "--task", INBOUND],
        )
        args = parse_args()
        assert args.status == "executed"
        assert args.task == INBOUND

    def test_list_rejects_invalid_status_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["scout", "paa", "list", "--status", "bogus"])
        with pytest.raises(SystemExit):
            parse_args()


class TestHandlersOutputStableJson:
    def test_propose_prints_motion_json(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        )
        paa_propose(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "proposed"
        assert payload["task"] == INBOUND
        assert payload["scope"] is None
        assert payload["to_position"] == "hotl"
        assert payload["proposed_by"] == "operator"

    def test_approve_prints_executed_motion(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        paa_propose(Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        ))
        motion_id = json.loads(capsys.readouterr().out)["motion_id"]

        paa_approve(Namespace(motion_id=motion_id, reason="go", actor="ops"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "executed"
        assert payload["approved_by"] == "ops"

    def test_reject_prints_rejected_motion(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        paa_propose(Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        ))
        motion_id = json.loads(capsys.readouterr().out)["motion_id"]

        paa_reject(Namespace(motion_id=motion_id, reason="not ready", actor=None))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "rejected"
        assert payload["rejected_reason"] == "not ready"

    def test_demote_prints_executed_motion(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        paa_propose(Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        ))
        motion_id = json.loads(capsys.readouterr().out)["motion_id"]
        paa_approve(Namespace(motion_id=motion_id, reason="go", actor="ops"))
        capsys.readouterr()

        paa_demote(Namespace(
            task=INBOUND, scope=None, reason="incident", actor="oncall",
            source_rows=["posts:5", "posts:2"],
        ))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "executed"
        assert payload["from_position"] == "hotl"
        assert payload["to_position"] == "hitl"

    def test_show_prints_resolved_position(self, capsys: pytest.CaptureFixture[str]) -> None:
        paa_show(Namespace(task=INBOUND, scope=None))
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "task": INBOUND,
            "declaration_version": 1,
            "deployment": "shadow",
            "initial_position": "hitl",
            "scope": None,
            "current_position": "hitl",
            "latest_position_event": None,
        }

    def test_list_prints_motions_wrapper(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        paa_propose(Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        ))
        capsys.readouterr()

        paa_list(Namespace(status=None, task=None))
        payload = json.loads(capsys.readouterr().out)
        assert "motions" in payload
        assert len(payload["motions"]) == 1
        assert payload["motions"][0]["task"] == INBOUND

    def test_list_filters_by_task(
        self, evidence_file: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # inbound_reply_surfacing declares no `scopes:`, so it resolves at
        # scope None — the loader rejects any invented scope string.
        paa_propose(Namespace(
            task=INBOUND, scope=None, to="hotl", evidence=str(evidence_file),
            actor="operator", reason=None,
        ))
        capsys.readouterr()

        paa_list(Namespace(status=None, task=CANONICAL))
        assert json.loads(capsys.readouterr().out)["motions"] == []

        paa_list(Namespace(status=None, task=INBOUND))
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["motions"]) == 1
        assert payload["motions"][0]["task"] == INBOUND


class TestMainDispatchErrorHandling:
    def test_invalid_task_exits_nonzero_with_stderr_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["scout", "paa", "show", "not_a_real_task"],
        )
        with pytest.raises(SystemExit) as exc_info:
            scout_cli.main()
        assert exc_info.value.code == 1
        assert "paa show error" in capsys.readouterr().err

    def test_missing_evidence_file_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "propose", INBOUND, "--to", "hotl",
             "--evidence", str(tmp_path / "missing.json")],
        )
        with pytest.raises(SystemExit) as exc_info:
            scout_cli.main()
        assert exc_info.value.code == 1
        assert "paa propose error" in capsys.readouterr().err

    def test_undeclared_transition_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        evidence_file: Path,
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["scout", "paa", "propose", INBOUND, "--to", "autonomous",
             "--evidence", str(evidence_file)],
        )
        with pytest.raises(SystemExit) as exc_info:
            scout_cli.main()
        assert exc_info.value.code == 1
        assert "paa propose error" in capsys.readouterr().err


