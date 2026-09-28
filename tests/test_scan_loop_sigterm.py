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

_CHILD = textwrap.dedent(
    """
    import asyncio

    import scout.cli.main as scout_cli


    async def fake_main_loop(args):
        try:
            print("ready", flush=True)
            await asyncio.sleep(3600)
        finally:
            print("lease released", flush=True)


    scout_cli.main_loop = fake_main_loop
    scout_cli.run_scan_loop(None)
    print("exited cleanly", flush=True)
    """
)


def test_sigterm_runs_scan_loop_cleanup_and_exits_zero() -> None:
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
        stdout, stderr = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    expected = (0, ["lease", "released", "exited", "cleanly"])
    assert (child.returncode, stdout.split()) == expected, stderr
