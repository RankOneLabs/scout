"""Catalogue-local YAML loading with YAML 1.2-style boolean handling."""

from __future__ import annotations

from typing import Any

import yaml


class CatalogueSafeLoader(yaml.SafeLoader):
    """A SafeLoader whose ``true`` and ``false`` scalars remain strings."""


CatalogueSafeLoader.yaml_implicit_resolvers = {
    key: [item for item in values if item[0] != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(source: bytes) -> Any:
    return yaml.load(source.decode("utf-8"), Loader=CatalogueSafeLoader)


__all__ = ["CatalogueSafeLoader", "load_yaml"]
