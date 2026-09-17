from scout.result import Ok
from scout.typesafe.catalogue import load_catalogue
from scout.typesafe.placeholder import PlaceholderBackend


async def test_placeholder_selects_post_or_default() -> None:
    backend = PlaceholderBackend("tests/fixtures/typesafe/placeholder-answers.yaml")
    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    known = await backend({"post": {"id": "known-post"}}, catalogue)
    unknown = await backend({"post": {"id": "other"}}, catalogue)
    assert isinstance(known, Ok) and known.value.request_id == "placeholder-known"
    assert isinstance(unknown, Ok) and unknown.value.request_id == "placeholder-default"
