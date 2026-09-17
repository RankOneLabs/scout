from pathlib import Path

import pytest
import yaml

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
