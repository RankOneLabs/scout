import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from scout.typesafe.catalogue import CatalogueError, load_catalogue

FIXTURE = Path("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
BUNDLED_FIXTURE = Path("src/scout/typesafe/catalogues/agent-ops-relevance.v0-fixture.yaml")


def test_v1_format_preserves_bundled_catalogue_digest() -> None:
    catalogue = load_catalogue(BUNDLED_FIXTURE)
    assert catalogue.version == "e28bef8ad6f0e0868b31453f2f5f9b6161919e139be13ce28a569a4a3183a707"


def test_declared_format_changes_canonical_version(tmp_path: Path) -> None:
    raw = yaml.safe_load(FIXTURE.read_text())
    v1_path = tmp_path / "v1.yaml"
    v2_path = tmp_path / "v2.yaml"
    raw["format"] = "scout.typesafe-catalogue/v1"
    v1_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    raw["format"] = "scout.typesafe-catalogue/v2"
    raw["state"] = {
        "post": ["platform", "channel", "url", "text"],
        "parent_context_only": ["author_name", "text"],
        "author": ["name", "handle"],
        "project": ["key", "name", "description"],
    }
    v2_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    assert load_catalogue(v1_path).version != load_catalogue(v2_path).version


@pytest.mark.parametrize(
    ("format_name", "parent_context_only", "match"),
    [
        ("scout.typesafe-catalogue/v1", ["author_name", "text"], "legacy state"),
        ("scout.typesafe-catalogue/v2", True, "declared state"),
    ],
)
def test_format_selects_state_projection_shape(
    tmp_path: Path, format_name: str, parent_context_only: object, match: str
) -> None:
    raw = yaml.safe_load(FIXTURE.read_text())
    raw["format"] = format_name
    raw["state"]["post"] = []
    raw["state"]["author"] = []
    raw["state"]["project"] = []
    raw["state"]["parent_context_only"] = parent_context_only
    path = tmp_path / "mismatched-format.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))

    with pytest.raises(CatalogueError, match=match):
        load_catalogue(path)


def test_v1_version_still_selects_stored_shadow_run() -> None:
    catalogue = load_catalogue(BUNDLED_FIXTURE)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE shadow_relevance_runs (catalogue_version TEXT NOT NULL)")
    conn.execute("INSERT INTO shadow_relevance_runs VALUES (?)", (str(catalogue.version),))

    matched = conn.execute(
        "SELECT COUNT(*) FROM shadow_relevance_runs WHERE catalogue_version = ?",
        ("e28bef8ad6f0e0868b31453f2f5f9b6161919e139be13ce28a569a4a3183a707",),
    ).fetchone()

    assert matched == (1,)


def test_catalogue_hash_is_canonical(tmp_path: Path) -> None:
    first = load_catalogue(FIXTURE)
    raw = yaml.safe_load(FIXTURE.read_text())
    reformatted = tmp_path / "catalogue.yaml"
    reformatted.write_text(yaml.safe_dump(raw, sort_keys=True))
    assert load_catalogue(FIXTURE).version == first.version
    assert load_catalogue(reformatted).version == first.version
    raw["questions"][0]["prompt"] += " changed"
    reformatted.write_text(yaml.safe_dump(raw))
    assert load_catalogue(reformatted).version != first.version


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [("decide", "not_registered", "unknown decide"), ("author", ["bio"], "unavailable")],
)
def test_catalogue_rejects_unsupported_values(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    raw = yaml.safe_load(FIXTURE.read_text())
    if field == "decide":
        raw[field] = value
    else:
        raw["state"][field] = value
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(CatalogueError, match=match):
        load_catalogue(path)


def test_loaded_catalogue_is_deeply_immutable() -> None:
    catalogue = load_catalogue(FIXTURE)
    question = catalogue.document.questions[1]

    with pytest.raises(ValidationError, match="frozen"):
        catalogue.document.description = "changed"
    with pytest.raises(TypeError):
        catalogue.document.state.post[0] = "changed"
    with pytest.raises(TypeError):
        question.choices[0] = "changed"
    with pytest.raises(TypeError, match="immutable"):
        question.__pydantic_extra__["prompt"] = "changed"  # type: ignore[index]

    assert load_catalogue(FIXTURE).version == catalogue.version


def test_catalogue_wraps_source_decoding_errors(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"\xff")

    with pytest.raises(CatalogueError):
        load_catalogue(path)


def test_catalogue_wraps_canonicalization_errors(tmp_path: Path) -> None:
    raw = yaml.safe_load(FIXTURE.read_text())
    raw["questions"][0]["metadata"] = b"\xff"
    path = tmp_path / "invalid-binary.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(CatalogueError):
        load_catalogue(path)
