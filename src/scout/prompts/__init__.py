"""Prompt library — load prompts by name from .md files.

Prompts may include other prompts via `<!-- include: name -->` markers
on their own; the marker is replaced with the included prompt's
content at load time. Cycles raise ValueError with the include chain.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

logger = logging.getLogger("scout.prompts")

_PROMPTS_DIR = Path(__file__).parent
_cache: dict[str, tuple[tuple[int, int], str]] = {}  # name -> ((mtime_ns, size), raw content)
# Include names follow the prompt-name pattern (letters/digits/underscore to
# start, then also hyphens), matching the validation used by the web API so
# markers like `<!-- include: style-constraints -->` resolve correctly.
_INCLUDE_RE = re.compile(r"<!--\s*include:\s*([a-zA-Z0-9_][a-zA-Z0-9_-]*)\s*-->")


def _load_raw(name: str) -> str:
    """Read a prompt file and cache by (mtime_ns, size)."""
    path = _PROMPTS_DIR / f"{name}.md"
    stat = path.stat()  # raises FileNotFoundError
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(name)
    if cached is not None and cached[0] == key:
        return cached[1]
    raw = path.read_text().strip()
    _cache[name] = (key, raw)
    return raw


def get_prompt(name: str) -> str:
    """Load a prompt by name, resolving `<!-- include: ... -->` markers.

    The cache stores raw file content keyed by (mtime, size); include
    expansion runs every call so that touching an included file
    propagates to importers without manual cache invalidation.

    Raises FileNotFoundError if the prompt or any included prompt is missing.
    Raises ValueError if includes form a cycle.
    """
    return _resolve(name, (), _load_raw)


def get_prompt_db(name: str, templates: Mapping[str, str]) -> str:
    """Load a prompt by name, preferring DB templates over files.

    Resolution order: (1) templates[name], (2) _load_raw(name), (3) raise.
    Include markers resolve through the same DB-first loader, so DB-backed
    shared prompts expand correctly.

    Raises FileNotFoundError if neither source yields the prompt.
    Raises ValueError if includes form a cycle.
    """
    def loader(n: str) -> str:
        if n in templates:
            return templates[n]
        # Not in the DB — fall back to the file. Logged at debug so a single
        # scan's repeated lookups stay quiet; prompt_source_report() is the
        # operator-facing surface that reports file fallbacks once per scan.
        logger.debug("prompt %r not in DB templates; falling back to file", n)
        return _load_raw(n)

    return _resolve(name, (), loader)


def prompt_source_report(
    names: Iterable[str], templates: Mapping[str, str]
) -> dict[str, str]:
    """Classify each required prompt (and its includes) by resolution source.

    Walks every name in ``names`` and every name reachable through
    ``<!-- include: ... -->`` markers, returning a mapping of
    ``name -> "db" | "file" | "missing"``:

    - ``"db"``     — served from an active DB template.
    - ``"file"``   — absent from the DB, resolved via file fallback.
    - ``"missing"``— absent from both; ``get_prompt_db`` would raise.

    This is the diagnostics surface the scan path uses to make file fallbacks
    and missing prompts visible, so the system never silently depends on files.
    """
    report: dict[str, str] = {}

    def visit(name: str) -> None:
        if name in report:
            return
        if name in templates:
            report[name] = "db"
            raw = templates[name]
        else:
            try:
                raw = _load_raw(name)
            except FileNotFoundError:
                report[name] = "missing"
                return
            report[name] = "file"
        for match in _INCLUDE_RE.finditer(raw):
            visit(match.group(1))

    for name in names:
        visit(name)
    return report


def _resolve(name: str, seen: tuple[str, ...], loader: Callable[[str], str]) -> str:
    if name in seen:
        chain = " -> ".join([*seen, name])
        raise ValueError(f"prompt include cycle detected: {chain}")
    raw = loader(name)
    if not _INCLUDE_RE.search(raw):
        return raw
    next_seen = (*seen, name)
    return _INCLUDE_RE.sub(lambda m: _resolve(m.group(1), next_seen, loader), raw)


def list_prompts() -> list[str]:
    """Return names of all available prompts."""
    return sorted(p.stem for p in _PROMPTS_DIR.glob("*.md"))
