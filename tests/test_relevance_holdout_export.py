"""Held-row batch export, replay, rollback, and state parity."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.relevance import holdout_export
from scout.relevance.binding import bind_state, population_export_state_source
from scout.relevance.holdout_export import export_holdouts
from scout.storage.state import StateManager


def test_argparse_wires_holdout_export(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.cli.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        ["scout", "replay", "holdout", "export", "--out", "packet", "--batch", "b-1"],
    )
    args = parse_args()
    assert (args.replay_command, args.holdout_command, args.out, args.batch) == (
        "holdout",
        "export",
        "packet",
        "b-1",
    )


def _seed_holdout(
    state: StateManager,
    *,
    evaluation_id: int,
    project_key: str,
    action: str,
) -> None:
    state.conn.execute(
        "INSERT INTO scans(id, started_at, environment, run_kind) "
        "VALUES (?, ?, 'test', 'live')",
        (evaluation_id, "2026-09-01T00:00:00+00:00"),
    )
    state.conn.execute(
        "INSERT INTO posts(id, platform, platform_msg_id, channel_name, author_name, "
        "author_id, content, url, scan_id) VALUES (?, 'bluesky', ?, 'research', "
        "'Ada', 'did:ada', ?, ?, ?)",
        (
            evaluation_id,
            f"post-{evaluation_id}",
            f"text {evaluation_id}",
            f"https://bsky.app/profile/ada.test/post/{evaluation_id}",
            evaluation_id,
        ),
    )
    state.conn.execute(
        "INSERT INTO evaluations(id, post_id, relevant, score, scan_id, project_key, "
        "surface_status, created_at) VALUES (?, ?, ?, 0.75, ?, ?, 'not_relevant', ?)",
        (
            evaluation_id,
            evaluation_id,
            int(action == "respond"),
            evaluation_id,
            project_key,
            "2026-09-01T00:00:00+00:00",
        ),
    )
    registry = {
        "projects": {
            project_key: {
                "key": project_key,
                "name": project_key.title(),
                "description": f"{project_key} description",
                "link": "https://example.test",
                "dossier_summary_id": None,
            }
        },
        "keywords": [],
        "prompt_templates": {},
    }
    state.conn.execute(
        "INSERT INTO relevance_holdouts(evaluation_id, production_action, held, "
        "registry_state, created_at) VALUES (?, ?, 1, ?, ?)",
        (
            evaluation_id,
            action,
            json.dumps(registry),
            "2026-09-01T00:00:00+00:00",
        ),
    )


def test_two_projects_write_two_population_files_plus_manifest(tmp_path: Path) -> None:
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")
        _seed_holdout(state, evaluation_id=22, project_key="agent-evals", action="drop")
        result = export_holdouts(
            state,
            tmp_path,
            now=datetime(2026, 9, 2, tzinfo=UTC),
        )

        assert {path.name for path in result.files} == {
            "agent-ops.jsonl",
            "agent-evals.jsonl",
            "batch.json",
        }
        assert json.loads((tmp_path / "agent-ops.jsonl").read_text())["evaluation_id"] == 11
        assert json.loads((tmp_path / "agent-evals.jsonl").read_text())["evaluation_id"] == 22
        assert state.conn.execute(
            "SELECT COUNT(*) FROM relevance_holdouts WHERE batch_id = ?",
            (result.batch_id,),
        ).fetchone()[0] == 2


def test_existing_batch_rewrites_byte_identical_files(tmp_path: Path) -> None:
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="review")
        first = export_holdouts(state, tmp_path)
        original = {path.name: path.read_bytes() for path in first.files}

        second = export_holdouts(state, tmp_path, batch_id=first.batch_id)

        assert first.exported_at == second.exported_at
        assert original == {path.name: path.read_bytes() for path in second.files}


def test_failed_file_write_does_not_mark_rows_exported(tmp_path: Path) -> None:
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")

        def crash(_directory: Path, _payloads: dict[str, bytes]) -> tuple[Path, ...]:
            raise OSError("simulated crash")

        with pytest.raises(OSError, match="simulated crash"):
            export_holdouts(state, tmp_path, writer=crash)

        row = state.conn.execute(
            "SELECT batch_id, exported_at FROM relevance_holdouts"
        ).fetchone()
        assert tuple(row) == (None, None)


def test_failed_publish_restores_the_previous_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")
        export_holdouts(state, tmp_path)
        previous = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        _seed_holdout(state, evaluation_id=12, project_key="agent-ops", action="drop")
        _seed_holdout(state, evaluation_id=22, project_key="zeta-ops", action="drop")

        real_replace = holdout_export.os.replace

        def fail_on_zeta(source: str | Path, destination: str | Path) -> None:
            if Path(destination).name == "zeta-ops.jsonl":
                raise OSError("simulated replace failure")
            real_replace(source, destination)

        monkeypatch.setattr(holdout_export.os, "replace", fail_on_zeta)
        with pytest.raises(OSError, match="simulated replace failure"):
            export_holdouts(state, tmp_path)

        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == previous


def test_export_record_rebuilds_the_live_declared_state(tmp_path: Path) -> None:
    declared = {
        "post": ("platform", "channel", "url", "text"),
        "parent_context_only": ("author_name", "text"),
        "author": ("name", "handle"),
        "project": ("key", "name", "description"),
    }
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")
        live = dict(
            state.conn.execute(
                "SELECT platform, channel_name, url, content, parent_author_name, "
                "parent_text, author_name FROM posts WHERE id = 11"
            ).fetchone()
        )
        live["author_handle"] = "ada.test"
        project = {
            "key": "agent-ops",
            "name": "Agent-Ops",
            "description": "agent-ops description",
        }
        expected = bind_state(declared, live, project)

        export_holdouts(state, tmp_path)
        exported = json.loads((tmp_path / "agent-ops.jsonl").read_text())

    assert bind_state(declared, population_export_state_source(exported), project) == expected


def test_bind_state_raises_when_a_bound_source_is_missing() -> None:
    declared = {"post": ("platform", "text")}

    with pytest.raises(KeyError, match="content"):
        bind_state(declared, {"platform": "bluesky"}, {})
