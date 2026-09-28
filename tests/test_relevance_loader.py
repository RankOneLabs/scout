from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scout.relevance.loader import (
    MalformedCatalogueError,
    load_catalogue,
    load_catalogue_bytes,
)
from scout.typesafe.catalogue import CatalogueError


def _document() -> dict[str, Any]:
    question = {
        "type": "noul",
        "instructions": "Synthetic question",
        "criteria": {
            "true": {"what": "It is true."},
            "false": {"what": "It is false."},
        },
    }
    return {
        "id": "synthetic-relevance",
        "decide": "agent_ops_route/v1",
        "description": "Made-up relevance catalogue.",
        "state": {
            "post": ["platform", "channel", "url", "text"],
            "parent_context_only": ["author_name", "text"],
            "author": ["name", "handle"],
            "project": ["key", "name", "description"],
        },
        "questions": {
            "excl_synthetic": question,
            "needs_thread": question,
            "answerable_from_post": question,
            "about_agent_work": question,
            "points_somewhere": question,
        },
    }


def _source(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode()


def test_loads_noul_questions_in_stable_declaration_order() -> None:
    source = _source(_document())
    first = load_catalogue_bytes(source)
    second = load_catalogue_bytes(source)

    assert tuple(question.id for question in first.questions) == (
        "excl_synthetic",
        "needs_thread",
        "answerable_from_post",
        "about_agent_work",
        "points_somewhere",
    )
    assert tuple(question.id for question in second.questions) == tuple(
        question.id for question in first.questions
    )


def test_question_mapping_key_overrides_nested_id() -> None:
    raw = _document()
    raw["questions"]["needs_thread"]["id"] = "spoofed"

    catalogue = load_catalogue_bytes(_source(raw))

    assert catalogue.questions[1].id == "needs_thread"
    assert catalogue.document.questions[1].id == "needs_thread"


def test_records_id_and_file_bytes_sha256(tmp_path: Path) -> None:
    source = _source(_document())
    path = tmp_path / "catalogue.yaml"
    path.write_bytes(source)

    catalogue = load_catalogue(path)

    assert (catalogue.id, catalogue.file_sha256) == (
        "synthetic-relevance",
        hashlib.sha256(source).hexdigest(),
    )


def test_yaml_boolean_criteria_keys_remain_strings() -> None:
    catalogue = load_catalogue_bytes(
        b"""\
id: unquoted-boolean-criteria
decide: agent_ops_route/v1
description: Direct YAML boolean resolver regression fixture.
state:
  post: [platform, channel, url, text]
  parent_context_only: [author_name, text]
  author: [name, handle]
  project: [key, name, description]
questions:
  excl_synthetic: &question
    type: noul
    instructions: Synthetic question
    criteria:
      true: {what: It is true.}
      false: {what: It is false.}
  needs_thread: *question
  answerable_from_post: *question
  about_agent_work: *question
  points_somewhere: *question
"""
    )
    assert tuple(catalogue.questions[0].criteria or {}) == ("true", "false")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda raw: raw.update(decide="other"), "decide"),
        (lambda raw: raw["state"]["post"].append("unbound"), "bind to no available"),
        (
            lambda raw: raw["questions"]["needs_thread"].update(type="choice"),
            "must have type 'noul'",
        ),
        (lambda raw: raw["questions"].pop("points_somewhere"), "missing routed feature"),
        (
            lambda raw: raw.update(
                questions={
                    key: value
                    for key, value in raw["questions"].items()
                    if not key.startswith("excl_")
                }
            ),
            "at least one excl_",
        ),
    ],
)
def test_refuses_each_non_dispatchable_condition(
    mutate: Any, match: str
) -> None:
    raw = _document()
    mutate(raw)

    with pytest.raises(CatalogueError, match=match):
        load_catalogue_bytes(_source(raw))


def test_allows_null_optional_criterion_descriptions() -> None:
    raw = _document()
    criterion = raw["questions"]["excl_synthetic"]["criteria"]["true"]
    criterion.update(not_for=None, examples=None)

    load_catalogue_bytes(_source(raw))


def test_refuses_null_required_criterion_value() -> None:
    raw = _document()
    raw["questions"]["excl_synthetic"]["criteria"]["true"]["what"] = None

    with pytest.raises(MalformedCatalogueError, match="excl_synthetic.criteria.true.what"):
        load_catalogue_bytes(_source(raw))


@pytest.mark.parametrize("missing", ["id", "decide", "description", "state", "questions"])
def test_refuses_missing_required_key(missing: str) -> None:
    raw = _document()
    del raw[missing]

    with pytest.raises(CatalogueError, match=f"missing {missing}"):
        load_catalogue_bytes(_source(raw))
