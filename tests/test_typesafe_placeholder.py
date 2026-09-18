from scout.result import Ok
from scout.typesafe.catalogue import load_catalogue
from scout.typesafe.placeholder import PlaceholderBackend


async def test_placeholder_selects_post_or_default() -> None:
    backend = PlaceholderBackend("tests/fixtures/typesafe/placeholder-answers.yaml")
    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    known = await backend({"post": {"id": "known-post"}}, catalogue)
    unknown = await backend({"post": {"id": "other"}}, catalogue)
    assert isinstance(known, Ok)
    assert known.value.request_id.startswith("placeholder-known:")
    assert isinstance(unknown, Ok)
    assert unknown.value.request_id.startswith("placeholder-default:other:")

    repeated = await backend({"post": {"id": "known-post"}}, catalogue)
    assert isinstance(repeated, Ok)
    assert repeated.value.request_id != known.value.request_id
