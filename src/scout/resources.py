"""Resolve runtime data files in source checkouts and installed wheels."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def runtime_resource(*parts: str) -> Path:
    """Return a packaged resource, falling back to its source-tree path."""
    packaged = PACKAGE_ROOT.joinpath(*parts)
    if packaged.exists():
        return packaged
    return PROJECT_ROOT.joinpath(*parts)
