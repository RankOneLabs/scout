"""Environment lease acquisition and heartbeat for the owned scan lifecycle.

Wraps `StateManager`'s acquire/renew/release_environment_lease
compare-and-set primitives (scout.storage.scans) with a process identity
and a background heartbeat task. The runner acquires one lease per process
invocation, reconciles abandoned canonical owners under it, then holds it
for the duration of the scan loop — see coverage.py for how a canonical
owner scan is committed and finalized under a held lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from scout.result import Err, Ok, Result
from scout.storage.scans import EnvironmentLease, LeaseError
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.lease")


def generate_owner_id() -> str:
    """A process-unique lease owner identity. A random uuid4 is sufficient
    to distinguish concurrent workers without leaking host/pid details
    into durable storage."""
    return str(uuid.uuid4())


class EnvironmentLeaseHandle:
    """One process's held environment lease, plus its background heartbeat.

    Heartbeat renewals go through a dedicated `StateManager`/`Db`
    connection (`_heartbeat_state`), never the caller's primary
    connection: decision requires heartbeat writes on their own
    connection so a renewal can never contend with or be blocked by a
    long-running scan transaction on the primary connection, and every
    heartbeat transaction stays short regardless of what the primary
    connection is doing at that moment.
    """

    def __init__(
        self,
        *,
        environment: str,
        owner_id: str,
        lease: EnvironmentLease,
        db_path: str,
        ttl_seconds: float,
        heartbeat_interval_seconds: float,
    ) -> None:
        self.environment = environment
        self.owner_id = owner_id
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._lease = lease
        self._heartbeat_state = StateManager(db_path=db_path, init_schema=False)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()

    @property
    def fence(self) -> int:
        return self._lease.fence

    @property
    def lost(self) -> bool:
        """True once a heartbeat renewal has been refused — the lease is
        no longer safely held. A caller must treat any in-flight canonical
        owner scan as no longer safe to finalize with advance_watermark
        once this is set; reconciliation on the next acquire will clean it
        up instead."""
        return self._lost.is_set()

    def start_heartbeat(self) -> None:
        """Begin periodic renewal. Idempotent: a second call is a no-op."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                if self._lost.is_set():
                    return
                result = self._heartbeat_state.renew_environment_lease(
                    self.environment,
                    self.owner_id,
                    self._lease.fence,
                    ttl_seconds=self._ttl_seconds,
                )
                match result:
                    case Ok(lease):
                        self._lease = lease
                        logger.debug(
                            "Lease heartbeat ok for environment=%s owner=%s fence=%d",
                            self.environment, self.owner_id, self._lease.fence,
                        )
                    case Err(error):
                        logger.error(
                            "Lease heartbeat lost for environment=%s owner=%s: %s",
                            self.environment, self.owner_id, error.detail,
                        )
                        self._lost.set()
                        return
        except asyncio.CancelledError:
            raise

    async def stop(self, *, release: bool) -> None:
        """Stop the heartbeat task and, if requested and the lease was
        never lost, release it cleanly. Always closes the dedicated
        heartbeat connection."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if release and not self._lost.is_set():
            try:
                self._heartbeat_state.release_environment_lease(
                    self.environment, self.owner_id, self._lease.fence
                )
            except Exception:
                logger.warning(
                    "Failed to release lease for environment=%s owner=%s",
                    self.environment, self.owner_id, exc_info=True,
                )
        self._heartbeat_state.close()


def acquire_lease(
    state: StateManager,
    *,
    environment: str,
    owner_id: str,
    db_path: str,
    ttl_seconds: float,
    heartbeat_interval_seconds: float,
) -> Result[EnvironmentLeaseHandle, LeaseError]:
    """Acquire environment's lease on the primary connection, then wrap it
    in a handle with its own dedicated heartbeat connection."""
    result = state.acquire_environment_lease(environment, owner_id, ttl_seconds=ttl_seconds)
    match result:
        case Ok(lease):
            return Ok(EnvironmentLeaseHandle(
                environment=environment,
                owner_id=owner_id,
                lease=lease,
                db_path=db_path,
                ttl_seconds=ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            ))
        case Err(error):
            return Err(error)


def reconcile_abandoned_owners(state: StateManager, handle: EnvironmentLeaseHandle) -> list[int]:
    """Interrupt every canonical-live scan abandoned under a fence older
    than this handle's — call once, immediately after acquiring the
    lease and before any platform I/O for this process invocation."""
    return state.reconcile_abandoned_canonical_owners(handle.environment, handle.fence)
