from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from scout.typesafe.catalogue import CatalogueError, load_catalogue

FIXTURE = Path("tests/fixtures/typesafe/fixture-two-question.v1.yaml")


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
