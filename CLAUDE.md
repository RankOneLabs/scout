# CLAUDE.md

## Tooling — uv

This project uses [uv](https://docs.astral.sh/uv/) for package management, virtual environments, and running scripts. Do not use `pip`, `pip-tools`, `poetry`, or `conda`.

### Project Setup

```bash
uv init                            # New project
uv sync                            # Install from lockfile (uv.lock)
```

### Dependencies

```bash
uv add <package>                   # Add dependency
uv add --dev <package>             # Add dev dependency
uv remove <package>                # Remove dependency
uv lock                            # Regenerate lockfile without installing
```

- All dependencies live in `pyproject.toml` under `[project.dependencies]` and `[project.optional-dependencies]` or `[dependency-groups]`
- Always commit `uv.lock` — deterministic installs across machines
- Never pin versions manually in `pyproject.toml` unless you have a specific reason — let the lockfile handle resolution

### Running Things

Always use `uv run` — it ensures the correct virtualenv and dependencies:

```bash
uv run python -m <module>          # Run a module
uv run pytest                      # Run tests
uv run ruff check .                # Lint
uv run ruff format .               # Format
uv run mypy .                      # Type check
```

Do **not** activate the venv manually or use bare `python`/`pytest` commands. `uv run` is the entry point for everything.

### Scripts & Tools

```bash
uv tool install ruff               # Install CLI tools globally via uv
uv run --with httpx python script.py  # Run with ad-hoc dependency
```

### Python Version

```bash
uv python install 3.12             # Install a Python version
uv python pin 3.12                 # Pin for this project (.python-version)
```

Pin the Python version in `.python-version` and commit it. uv respects this file automatically.

---

## Architecture — Pipelines as the Default

All logic should be expressible as **composable pipelines** whose execution can be **traced** and **visually represented as DAGs**. This is not a style preference — it's the core architectural constraint. The majority of development time is spent in visual representations of flows; the code must map cleanly to those visuals.

### Design rules

- **Default to pipelines.** Break logic into small named transforms that compose. Each transform takes typed input and returns typed output (including `Result` for fallible operations). If you can't draw the data flow as a DAG where each node is a transform and each edge is typed data, rethink the design.
- **Pipelines are fractal.** A node in a pipeline can itself be a pipeline. A cycle (like critique→revise) is a subgraph that nests inside the parent pipeline as a single node. Zoom out: `draft in → approved draft out`. Zoom in: its own DAG with a conditional back-edge. This nests indefinitely — an agent calling another agent is a node whose implementation is another graph. The type signature at the boundary (`Result[T, E]`) is the same at every level.
- **Each transform is independently testable.** A transform should be a pure (or pure-ish) function that can be unit-tested in isolation and reused across flows.
- **`Result[T, E]` is pipeline plumbing.** `Ok`/`Err` is the universal connector between stages — a standard interface contract for composability regardless of position in the stack. Every fallible transform returns `Result`, and the next stage pattern-matches on it. The same contract holds whether the node is a pure function or a 50-step sub-agent with retry loops.
- **Errors carry trace context.** In agent loops with multiple iterations, an error alone doesn't tell you how it got there. Domain error types carry operation name, entity ID, and detail so traces can reconstruct the path through the pipeline at whatever granularity you choose. Traces compose with nesting — the outer trace shows which node failed, expanding that node shows the inner trace.
- **Reserve `try`/`except` for I/O boundaries.** Catch at the boundary, convert to `Result` immediately, and let the pipeline handle it from there.
- **Use loops when they're genuinely clearer** — early breaks, side effects, complex accumulation. Don't force a pipeline when a loop reads better.
- **Roll lightweight project-local implementations** rather than importing a library (e.g., `result.py`, `errors.py`).
- Use `itertools` and `functools` when they genuinely help — don't import them to look clever.

### Preferred: Functional pipeline

Small named functions that compose and can be reused across flows:

```python
from __future__ import annotations
from itertools import groupby
from operator import attrgetter

def is_eligible(user: User) -> bool:
    return user.active and user.age >= 18

def to_summary(user: User) -> UserSummary:
    return UserSummary(name=user.name, region=user.region)

def group_by_region(summaries: Iterable[UserSummary]) -> dict[str, list[UserSummary]]:
    sorted_items = sorted(summaries, key=attrgetter("region"))
    return {
        region: list(items)
        for region, items in groupby(sorted_items, key=attrgetter("region"))
    }

result = group_by_region(
    to_summary(u) for u in users if is_eligible(u)
)
```

### Avoid: Imperative accumulation

Mutation and conditionals tangled together in a single block:

```python
result: dict[str, list] = {}
for user in users:
    if user.active and user.age >= 18:
        summary = {"name": user.name, "region": user.region}
        if user.region not in result:
            result[user.region] = []
        result[user.region].append(summary)
```

### Acceptable: Loop with side effects

When each iteration performs side effects with early exit on failure, a loop is the right tool:

```python
async def publish_all(events: Sequence[Event]) -> None:
    for event in events:
        ok = await broker.publish(event)
        if not ok:
            raise PublishError(event.id)
```

### Preferred: Async pipelines

Compose async stages rather than nesting try/except blocks:

```python
async def fetch_user(user_id: str) -> User:
    return await api.get(f"/users/{user_id}")

async def enrich_with_posts(user: User) -> EnrichedUser:
    posts = await api.get(f"/users/{user.id}/posts")
    return EnrichedUser(**user.dict(), posts=posts)

def to_view_model(user: EnrichedUser) -> ProfileView:
    return ProfileView(display=user.name, post_count=len(user.posts))

async def load_profile(user_id: str) -> ProfileView:
    user = await fetch_user(user_id)
    enriched = await enrich_with_posts(user)
    return to_view_model(enriched)
```

---

## Error Handling — Errors as Values

`Result[T, E]` is the standard interface for fallible operations. This isn't about taste — it's what makes pipelines composable and traceable.

### Result Type

Project-local implementation in `result.py`:

```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T

@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E

type Result[T, E] = Ok[T] | Err[E]
```

### Domain Error Types

Each error type carries enough context to reconstruct the trace — operation name, entity ID, and detail:

```python
@dataclass(frozen=True, slots=True)
class LLMError:
    operation: str          # which transform failed
    message_id: str         # which input it failed on
    detail: str             # what went wrong

@dataclass(frozen=True, slots=True)
class NetworkError:
    url: str
    status: int | None = None
    detail: str = ""
```

### Pipeline Usage

Each stage returns `Result`. The caller pattern-matches to decide the next step:

```python
match await evaluate(message):
    case Ok(result):
        draft = await generate(result)
    case Err(error):
        log_trace(error)    # operation, message_id, detail all available
```

### I/O Boundaries

`try`/`except` lives only at I/O boundaries. Catch, convert to `Result`, and let the pipeline handle it:

```python
async def fetch_document(url: str) -> Result[Document, NetworkError]:
    try:
        response = await client.get(url)
        response.raise_for_status()
        return Ok(Document.model_validate_json(response.content))
    except httpx.HTTPStatusError as exc:
        return Err(NetworkError(url=url, status=exc.response.status_code))
    except httpx.RequestError:
        return Err(NetworkError(url=url, detail="connection failed"))
```

---

## Option Handling

Represent absence explicitly. Prefer `X | None` with early returns over nested `if x is not None` chains.

### Preferred: Early return on None

```python
def get_display_name(user_id: str) -> str | None:
    user = repo.find(user_id)
    if user is None:
        return None
    profile = user.profile
    if profile is None:
        return None
    return profile.display_name
```

### Avoid: Deeply nested None checks

```python
def get_display_name(user_id: str) -> str | None:
    user = repo.find(user_id)
    if user is not None:
        if user.profile is not None:
            return user.profile.display_name
    return None
```

---

## Typing

- All functions must have type annotations — parameters and return types
- Use `from __future__ import annotations` at the top of every module
- Prefer concrete types over `Any`. If you reach for `Any`, reconsider the design.
- Use `X | None` not `Optional[X]`, use `X | Y` not `Union[X, Y]`
- Use `collections.abc` for function signatures: `Sequence`, `Mapping`, `Iterable` over `list`, `dict`
- Use `pydantic.BaseModel` for data crossing boundaries (API, config, serialization)
- Use `dataclasses.dataclass` for internal domain types — prefer `frozen=True, slots=True`
- Use `TypedDict` for typed dict shapes (JSON payloads, configs)
- Never use raw dicts for structured data — define a type

---

## Naming

- `snake_case` for functions, methods, variables, modules
- `PascalCase` for classes
- `UPPER_SNAKE_CASE` for module-level constants
- Private: single `_` prefix. Avoid `__` dunder mangling.
- Be descriptive: `user_count` not `n`, `is_valid` not `flag`
- Booleans read as questions: `is_active`, `has_permission`, `should_retry`

---

## Imports

Group in order, separated by blank lines:

1. Standard library
2. Third-party packages
3. Local/project imports

```python
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.services.auth import verify_token
```

- Use absolute imports from the project root
- Never use wildcard imports (`from x import *`)
- Prefer explicit imports over importing the whole module

---

## Testing

- Test behavior, not implementation. Tests should survive refactors.
- One assertion per test when practical. Name tests descriptively.
- Use `pytest` conventions: functions over classes, fixtures over setup/teardown.
- Use factory fixtures for objects that need variation.

```python
def test_parse_token_with_valid_bearer_returns_claims():
    result = parse_token("Bearer eyJhbGci...")
    assert isinstance(result, Ok)
    assert result.value.sub == "user-123"

def test_parse_token_without_bearer_prefix_returns_auth_error():
    result = parse_token("bad-token")
    assert isinstance(result, Err)
    assert result.error.reason == "missing bearer prefix"
```
