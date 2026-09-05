"""Synthetic contract tests; these do not establish producer reproducibility."""

from __future__ import annotations

import pytest

from scout.grading.artifacts import (
    ArtifactDigest,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    TransformKind,
    decode_lineage,
    digest_artifact,
    encode_lineage,
)
from scout.result import Err, Ok


@pytest.fixture
def lineage() -> ArtifactLineage:
    return ArtifactLineage(
        kind=TransformKind("scout.corpus.snapshot"),
        inputs=(digest_artifact(b"synthetic retained population"),),
        process=ArtifactProcess(
            id=ProcessId("scout.corpus.select"),
            version="1",
            config_digest=digest_artifact(b'{"project_key":"synthetic"}'),
            environment=EnvironmentIdentity(digest_artifact(b"synthetic-environment-pin")),
        ),
        outputs=(digest_artifact(b"synthetic snapshot"),),
    )


def test_digest_addresses_exact_bytes() -> None:
    assert digest_artifact(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_digest_does_not_normalize_input_text() -> None:
    assert digest_artifact(b"post\n") != digest_artifact(b"post")


def test_lineage_round_trip_preserves_all_four_parts(lineage: ArtifactLineage) -> None:
    assert decode_lineage(encode_lineage(lineage)) == Ok(lineage)


def test_equivalent_lineage_has_stable_encoded_digest(lineage: ArtifactLineage) -> None:
    decoded = decode_lineage(encode_lineage(lineage))
    assert isinstance(decoded, Ok)
    assert digest_artifact(encode_lineage(decoded.value)) == digest_artifact(
        encode_lineage(lineage)
    )


def test_v1_encoding_pins_legacy_bytes_and_preserves_array_order() -> None:
    # Literal legacy JSON: never derive this expected value with the encoder
    # under test or a dependency's current JSON renderer.
    legacy = (
        '{"kind":"test.é/\\"\\\\\\n\\u0000","inputs":["'
        + "b" * 64
        + '","'
        + "a" * 64
        + '"],"process":{"id":"test.producer","version":"1","config_digest":"'
        + "c" * 64
        + '","environment":"'
        + "d" * 64
        + '"},"outputs":["'
        + "f" * 64
        + '","'
        + "e" * 64
        + '"]}'
    ).encode("utf-8")
    lineage = ArtifactLineage(
        kind=TransformKind('test.é/"\\\n\x00'),
        inputs=(ArtifactDigest("b" * 64), ArtifactDigest("a" * 64)),
        process=ArtifactProcess(
            id=ProcessId("test.producer"),
            version="1",
            config_digest=ArtifactDigest("c" * 64),
            environment=EnvironmentIdentity("d" * 64),
        ),
        outputs=(ArtifactDigest("f" * 64), ArtifactDigest("e" * 64)),
    )
    assert encode_lineage(lineage) == legacy
    assert decode_lineage(legacy) == Ok(lineage)


def test_encoding_does_not_depend_on_model_json_rendering(
    lineage: ArtifactLineage, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = encode_lineage(lineage)
    monkeypatch.setattr(ArtifactLineage, "model_dump_json", lambda self: "changed rendering")
    assert encode_lineage(lineage) == expected


@pytest.mark.parametrize("part", ["kind", "inputs", "process", "outputs"])
def test_missing_lineage_part_is_rejected(lineage: ArtifactLineage, part: str) -> None:
    document = lineage.model_dump_json(exclude={part}).encode()
    assert isinstance(decode_lineage(document), Err)


@pytest.mark.parametrize("part", ["id", "version", "config_digest", "environment"])
def test_missing_process_part_is_rejected(lineage: ArtifactLineage, part: str) -> None:
    document = lineage.model_dump_json(exclude={"process": {part}}).encode()
    assert isinstance(decode_lineage(document), Err)


def test_new_opaque_kind_does_not_require_a_catalog_entry(lineage: ArtifactLineage) -> None:
    document = encode_lineage(lineage).replace(b"scout.corpus.snapshot", b"future.custom.transform")
    decoded = decode_lineage(document)
    assert isinstance(decoded, Ok)
    assert decoded.value.kind == "future.custom.transform"


@pytest.mark.parametrize("value", [b"", b"   "])
def test_blank_kind_is_rejected(lineage: ArtifactLineage, value: bytes) -> None:
    document = encode_lineage(lineage).replace(b"scout.corpus.snapshot", value)
    assert isinstance(decode_lineage(document), Err)


def test_malformed_digest_is_rejected(lineage: ArtifactLineage) -> None:
    document = encode_lineage(lineage).replace(lineage.inputs[0].encode(), b"not-a-digest")
    assert isinstance(decode_lineage(document), Err)


def test_empty_inputs_are_not_a_substitute_for_missing_provenance(lineage: ArtifactLineage) -> None:
    document = encode_lineage(lineage).replace(b'"' + lineage.inputs[0].encode() + b'"', b"")
    assert isinstance(decode_lineage(document), Err)


def test_invalid_document_is_an_error_value_without_input_content() -> None:
    result = decode_lineage(b"private invalid input")
    assert isinstance(result, Err)
    assert "private invalid input" not in repr(result.error)
