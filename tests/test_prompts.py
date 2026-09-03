"""Tests for the prompt loader with mtime-keyed cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scout.prompts as prompts


@pytest.fixture
def tmp_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the prompt loader at a temp directory and reset its cache."""
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(prompts, "_cache", {})
    return tmp_path


def test_get_prompt_missing_file_raises_file_not_found(tmp_prompts_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        prompts.get_prompt("does_not_exist")


def test_get_prompt_returns_file_content(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "greeting.md").write_text("hello world\n")
    assert prompts.get_prompt("greeting") == "hello world"


def test_get_prompt_reloads_when_file_mtime_changes(tmp_prompts_dir: Path) -> None:
    path = tmp_prompts_dir / "greeting.md"
    path.write_text("first content\n")
    assert prompts.get_prompt("greeting") == "first content"

    # Rewrite with a bumped mtime to ensure cache invalidation.
    path.write_text("second content\n")
    original_mtime_ns = path.stat().st_mtime_ns
    new_mtime_ns = original_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_mtime_ns, new_mtime_ns))

    assert prompts.get_prompt("greeting") == "second content"


def test_get_prompt_serves_from_cache_when_file_unchanged(
    tmp_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_prompts_dir / "greeting.md"
    path.write_text("cached content\n")

    # Prime the cache.
    assert prompts.get_prompt("greeting") == "cached content"

    # Count read_text calls on Path; a cached read should not call read_text.
    call_count = {"n": 0}
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        call_count["n"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert prompts.get_prompt("greeting") == "cached content"
    assert call_count["n"] == 0


def test_get_prompt_resolves_include_marker(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "shared.md").write_text("shared rules")
    (tmp_prompts_dir / "main.md").write_text("intro\n\n<!-- include: shared -->\n\noutro")
    out = prompts.get_prompt("main")
    assert "intro" in out
    assert "shared rules" in out
    assert "outro" in out
    assert "<!-- include:" not in out


def test_get_prompt_picks_up_changes_in_included_file(tmp_prompts_dir: Path) -> None:
    shared = tmp_prompts_dir / "shared.md"
    shared.write_text("v1 rules")
    (tmp_prompts_dir / "main.md").write_text("<!-- include: shared -->")
    assert "v1 rules" in prompts.get_prompt("main")

    shared.write_text("v2 rules")
    new_mtime_ns = shared.stat().st_mtime_ns + 1_000_000_000
    os.utime(shared, ns=(new_mtime_ns, new_mtime_ns))
    assert "v2 rules" in prompts.get_prompt("main")


def test_get_prompt_missing_include_raises(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "main.md").write_text("<!-- include: nonexistent -->")
    with pytest.raises(FileNotFoundError):
        prompts.get_prompt("main")


def test_get_prompt_self_include_raises_cycle_error(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "loop.md").write_text("<!-- include: loop -->")
    with pytest.raises(ValueError, match="cycle detected.*loop"):
        prompts.get_prompt("loop")


def test_get_prompt_indirect_cycle_raises(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "a.md").write_text("<!-- include: b -->")
    (tmp_prompts_dir / "b.md").write_text("<!-- include: c -->")
    (tmp_prompts_dir / "c.md").write_text("<!-- include: a -->")
    with pytest.raises(ValueError, match="cycle detected.*a -> b -> c -> a"):
        prompts.get_prompt("a")


def test_list_prompts_returns_all_md_stems(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "alpha.md").write_text("a")
    (tmp_prompts_dir / "bravo.md").write_text("b")
    (tmp_prompts_dir / "charlie.md").write_text("c")
    (tmp_prompts_dir / "notes.txt").write_text("ignored")

    assert prompts.list_prompts() == ["alpha", "bravo", "charlie"]


# --- get_prompt_db tests ---


def test_get_prompt_db_prefers_template_over_file(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "eval.md").write_text("file version")
    result = prompts.get_prompt_db("eval", {"eval": "db version"})
    assert result == "db version"


def test_get_prompt_db_falls_back_to_file_when_missing_from_templates(
    tmp_prompts_dir: Path,
) -> None:
    (tmp_prompts_dir / "eval.md").write_text("file fallback")
    result = prompts.get_prompt_db("eval", {})
    assert result == "file fallback"


def test_get_prompt_db_raises_file_not_found_when_both_missing(
    tmp_prompts_dir: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        prompts.get_prompt_db("missing", {})


def test_get_prompt_db_resolves_include_via_db_first(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "shared.md").write_text("file shared")
    (tmp_prompts_dir / "main.md").write_text("<!-- include: shared -->")
    templates = {"shared": "db shared"}
    result = prompts.get_prompt_db("main", templates)
    assert result == "db shared"


def test_get_prompt_db_include_falls_back_to_file_when_not_in_templates(
    tmp_prompts_dir: Path,
) -> None:
    (tmp_prompts_dir / "shared.md").write_text("file shared")
    (tmp_prompts_dir / "main.md").write_text("<!-- include: shared -->")
    result = prompts.get_prompt_db("main", {})
    assert result == "file shared"


def test_get_prompt_db_detects_cycle(tmp_prompts_dir: Path) -> None:
    templates = {"a": "<!-- include: b -->", "b": "<!-- include: a -->"}
    with pytest.raises(ValueError, match="cycle detected"):
        prompts.get_prompt_db("a", templates)


def test_get_prompt_resolves_hyphenated_include_name(tmp_prompts_dir: Path) -> None:
    # Prompt names may contain hyphens (matching the web API's name pattern),
    # so include markers must resolve those too.
    (tmp_prompts_dir / "style-constraints.md").write_text("be concise")
    (tmp_prompts_dir / "main.md").write_text("<!-- include: style-constraints -->")
    assert prompts.get_prompt("main") == "be concise"


def test_get_prompt_db_resolves_hyphenated_include_name(tmp_prompts_dir: Path) -> None:
    templates = {
        "main": "<!-- include: style-constraints -->",
        "style-constraints": "db shared body",
    }
    assert prompts.get_prompt_db("main", templates) == "db shared body"


# --- prompt_source_report (diagnostics) tests ---


def test_prompt_source_report_classifies_db_file_and_missing(
    tmp_prompts_dir: Path,
) -> None:
    (tmp_prompts_dir / "on_disk.md").write_text("file body")
    report = prompts.prompt_source_report(
        ["in_db", "on_disk", "nowhere"], {"in_db": "db body"}
    )
    assert report == {"in_db": "db", "on_disk": "file", "nowhere": "missing"}


def test_prompt_source_report_follows_includes(tmp_prompts_dir: Path) -> None:
    (tmp_prompts_dir / "shared_file.md").write_text("on disk")
    templates = {
        "main": "<!-- include: shared_db -->\n<!-- include: shared_file -->",
        "shared_db": "in db",
    }
    report = prompts.prompt_source_report(["main"], templates)
    assert report == {"main": "db", "shared_db": "db", "shared_file": "file"}


def test_prompt_source_report_terminates_on_include_cycle(
    tmp_prompts_dir: Path,
) -> None:
    templates = {"a": "<!-- include: b -->", "b": "<!-- include: a -->"}
    report = prompts.prompt_source_report(["a"], templates)
    assert report == {"a": "db", "b": "db"}


def test_critique_prompt_loads_from_the_real_prompts_directory() -> None:
    """prompts/critique.md — the file scout_agent.LLM_CRITIC_PROMPT_VERSION
    versions — is present and loadable outside the tmp_prompts_dir fixture."""
    import scout.prompts as real_prompts

    assert "verdict" in real_prompts.get_prompt("critique")
