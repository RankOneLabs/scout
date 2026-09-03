"""Scout's RuntimeConfig: the whole construction surface for paa_runtime.

``paa_runtime`` holds no module-global state and resolves no paths at
import time — nothing defaults into a consumer's repository or a vendor's
naming convention. A consumer builds one ``RuntimeConfig`` and passes it
to the lifecycle API. This module is Scout's.

Scout used to carry those values as module constants spread across
paa_declarations (``DEFAULT_DECLARATIONS_DIR``), paa_evidence
(``DEFAULT_EVIDENCE_ROOT``), and paa_service (a hardcoded
``SCOUT_PAA_ACTOR`` read inside ``resolve_actor``), each defaulted into
its own function signature. Collecting them here is what makes the
per-call ``declarations_dir=`` / ``evidence_root=`` test-injection
parameters unnecessary: a test builds a config instead of overriding
three defaults independently.
"""

from __future__ import annotations

from pathlib import Path

from paa_runtime.config import RuntimeConfig

from scout.config import DB_PATH
from scout.paa.declarations import DEFAULT_DECLARATIONS_DIR
from scout.paa.registry import PRODUCER_REGISTRY

# Content-addressed evidence lives at evidence/paa/<sha256>/evidence.json
# beneath the package root.
DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parents[1]

# The environment variable `scout paa` reads to identify the acting
# operator when --actor is omitted. The runtime takes this as config
# rather than hardcoding a name, so it stays Scout's to choose.
SCOUT_PAA_ACTOR_ENV = "SCOUT_PAA_ACTOR"


def build_paa_config(
    *,
    declarations_dir: Path | str | None = None,
    evidence_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> RuntimeConfig:
    """Scout's runtime configuration, with per-field overrides for tests.

    ``db_path`` is carried because ``RuntimeConfig`` declares it, but
    nothing reads it on Scout's path: it backs the runtime's default
    ``SqliteEventStore``, and Scout supplies ``ScoutEventStore`` instead
    so the position read authorizing a publish shares a lock domain with
    the publish itself (see paa_event_store). It is set to Scout's real
    database anyway — a field describing where events live should not
    claim somewhere they don't.
    """
    return RuntimeConfig(
        declarations_dir=Path(declarations_dir or DEFAULT_DECLARATIONS_DIR),
        evidence_root=Path(evidence_root or DEFAULT_EVIDENCE_ROOT),
        registry=PRODUCER_REGISTRY,
        db_path=Path(db_path or DB_PATH),
        actor_env_var=SCOUT_PAA_ACTOR_ENV,
    )


__all__ = ["DEFAULT_EVIDENCE_ROOT", "SCOUT_PAA_ACTOR_ENV", "build_paa_config"]
