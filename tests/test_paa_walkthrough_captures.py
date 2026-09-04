"""The reviewer-walkthrough captures are real, reproducible, and publication-safe."""

from __future__ import annotations

import getpass
import tempfile
from pathlib import Path

import pytest

from scout.paa.walkthrough_captures import (
    CAPTURES_DIR,
    LABEL,
    check_captures,
    render_captures,
)


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return render_captures()


def test_render_is_byte_reproducible(rendered: dict[str, str]) -> None:
    assert render_captures() == rendered


def test_every_capture_opens_with_the_reference_execution_label(
    rendered: dict[str, str],
) -> None:
    assert all(text.startswith(f"Label: {LABEL}\n") for text in rendered.values())


def test_no_capture_uses_the_forbidden_production_label(rendered: dict[str, str]) -> None:
    assert not any("Production transition" in text for text in rendered.values())


def test_no_temporary_path_leaks_into_a_capture(rendered: dict[str, str]) -> None:
    tmp_root = tempfile.gettempdir()
    assert not any(tmp_root in text or "paa-walkthrough-" in text for text in rendered.values())


def test_no_login_name_fallback_reaches_a_capture(rendered: dict[str, str]) -> None:
    login = getpass.getuser()
    assert not any(f'"{login}"' in text for text in rendered.values())


def test_scenario_visits_every_position_and_outcome(rendered: dict[str, str]) -> None:
    joined = "\n".join(rendered.values())
    assert '"status": "executed"' in joined
    assert '"status": "rejected"' in joined
    assert "has been tampered with" in joined
    assert '"current_position": "hotl"' in joined
    assert '"current_position": "hitl"' in joined


def test_checked_in_captures_are_current() -> None:
    assert check_captures(CAPTURES_DIR) == []


def test_check_detects_drift(tmp_path: Path, rendered: dict[str, str]) -> None:
    shadow = tmp_path / "captures"
    shadow.mkdir()
    for name, text in rendered.items():
        (shadow / name).write_text(text)
    first = sorted(rendered)[0]
    (shadow / first).write_text("Label: Reference execution\nedited\n")
    assert check_captures(shadow) == [f"stale capture: {first}"]
