"""Structural regression: the schema mirror and legacy loader this contract
change eliminated must stay absent, and no tracked file ever mirrors the
shared conformance corpus dossier-source publishes either.

Before this epic, Scout carried its own copy of the upstream summary
schema plus a parallel load_dossier/DossierResult/DossierIndex/
DossierIndexEntry loading path — a second, hand-maintained contract that
could (and did) silently drift from the real producer schema. resolve_dossier
now retrieves the pinned schemas/index.v1.schema.json and
schemas/summary.v1.schema.json directly from the requested dossier-source
revision via git show (see dossier.py), so there is exactly one schema
distribution mechanism. Likewise, tests/test_dossier_conformance.py reads
dossier-source's conformance/v1/ corpus (manifest, fixtures, normalization/
prohibition vectors) directly from a dossier-source checkout — never a copy
committed into Scout — per contracts/dossier-source-v1.json's corpus_path.
These tests fail closed if the schema mirror, the corpus mirror, or the
legacy symbols are ever reintroduced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import load_dossier_source_pin

_REPO_ROOT = Path(__file__).parent.parent
_PIN = load_dossier_source_pin()

# Basenames of the schemas dossier-source publishes at its pinned revision
# (schemas/index.v1.schema.json, schemas/summary.v1.schema.json). A tracked
# Scout file with any of these basenames would be a second, competing schema
# copy.
_FORBIDDEN_BASENAMES = frozenset({
    "index.v1.schema.json",
    "summary.v1.schema.json",
    "manifest.json",
    "normalization-vectors.json",
    "prohibition-vectors.json",
})

# The corpus directory dossier-source publishes (contracts/dossier-source-v1.json's
# corpus_path, e.g. "conformance/v1") — no tracked Scout path may live under
# a top-level directory of this name, which would be a second, competing
# copy of the shared conformance corpus.
_FORBIDDEN_TOP_LEVEL_DIRS = frozenset({Path(_PIN["corpus_path"]).parts[0]})

_FORBIDDEN_SYMBOLS = (
    "_SUMMARY_SCHEMA",
    "load_dossier",
    "DossierResult",
    "DossierIndex",
    "DossierIndexEntry",
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _git_grep_word(symbol: str, exclude_path: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "grep", "-n", "-w", symbol, "--",
         "*.py", f":!{exclude_path}"],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {result.stderr}")
    return result.stdout.splitlines() if result.returncode == 0 else []


def test_no_tracked_schema_mirror_file() -> None:
    """No tracked Scout file may share a basename with a published dossier-source
    schema or corpus artifact — that would be a second, hand-maintained copy
    that can drift."""
    offenders = [f for f in _tracked_files() if Path(f).name in _FORBIDDEN_BASENAMES]
    assert not offenders, f"tracked files mirror the published dossier schema: {offenders}"


def test_no_tracked_conformance_corpus_directory() -> None:
    """No tracked Scout path may live under dossier-source's published corpus
    directory name (e.g. ``conformance/``) — the corpus is read live from a
    checkout (tests/conftest.py), never vendored."""
    offenders = [
        f for f in _tracked_files()
        if Path(f).parts and Path(f).parts[0] in _FORBIDDEN_TOP_LEVEL_DIRS
    ]
    assert not offenders, f"tracked files mirror the published conformance corpus: {offenders}"


def test_no_legacy_dossier_symbols() -> None:
    """The pre-contract loader and its schema constant must stay deleted,
    not merely unused."""
    this_file = Path(__file__).relative_to(_REPO_ROOT).as_posix()
    offenders: list[str] = []
    for symbol in _FORBIDDEN_SYMBOLS:
        offenders.extend(f"{symbol}: {line}" for line in _git_grep_word(symbol, this_file))
    assert not offenders, "legacy dossier symbols reintroduced:\n" + "\n".join(offenders)
