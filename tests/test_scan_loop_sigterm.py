"""SIGTERM stops the scan loop through its cleanup, not around it.

`docker stop` sends SIGTERM. Without a handler the worker either dies at once
or, as a container's PID 1, ignores the signal until Docker SIGKILLs it; both
skip main_loop's `finally`, so the environment lease stays held for its TTL
and the next worker crash-loops until it expires.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

# The fake loop's cleanup awaits before releasing, as main_loop's does (it
# closes clients and stops the heartbeat before the release).
_CHILD = textwrap.dedent(
    """
    import asyncio

    import scout.cli.main as scout_cli


    async def fake_main_loop(args):
        try:
            print("ready", flush=True)
            await asyncio.sleep(3600)
        finally:
            print("cleaning up", flush=True)
            await asyncio.sleep(0.5)
            print("lease released", flush=True)


    scout_cli.main_loop = fake_main_loop
    scout_cli.run_scan_loop(None)
    print("exited cleanly", flush=True)
    """
)

_CLEAN_EXIT = (0, ["cleaning", "up", "lease", "released", "exited", "cleanly"])


def _terminate(signal_count: int) -> tuple[int, list[str], str]:
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal.SIGTERM)
        output = []
        if signal_count > 1:
            output.append(child.stdout.readline())
            for _ in range(signal_count - 1):
                child.send_signal(signal.SIGTERM)
                time.sleep(0.05)
        stdout, stderr = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    return child.returncode, "".join([*output, stdout]).split(), stderr


def test_sigterm_runs_scan_loop_cleanup_and_exits_zero() -> None:
    returncode, words, stderr = _terminate(signal_count=1)

    assert (returncode, words) == _CLEAN_EXIT, stderr


def test_repeated_sigterm_does_not_interrupt_cleanup() -> None:
    returncode, words, stderr = _terminate(signal_count=3)

    assert (returncode, words) == _CLEAN_EXIT, stderr
