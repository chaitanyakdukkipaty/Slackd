"""
Caffeinate manager — keeps macOS awake while Slackd is running.

Uses `caffeinate -i` which prevents idle sleep without keeping the
display on (battery-friendly). The subprocess is started/stopped
programmatically so it is always tied to the Slackd process lifetime.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_proc: Optional[subprocess.Popen] = None


def start() -> None:
    """Start caffeinate if it is not already running."""
    global _proc
    if is_running():
        return
    try:
        # -i  prevent idle sleep
        _proc = subprocess.Popen(
            ["caffeinate", "-i"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("caffeinate started (PID %d) — idle sleep prevented", _proc.pid)
    except Exception as exc:
        logger.error("Failed to start caffeinate: %s", exc)


def stop() -> None:
    """Stop caffeinate if it is running."""
    global _proc
    if _proc is None:
        return
    try:
        _proc.terminate()
        _proc.wait(timeout=3)
        logger.info("caffeinate stopped")
    except Exception as exc:
        logger.warning("Error stopping caffeinate: %s", exc)
    finally:
        _proc = None


def is_running() -> bool:
    """Return True if the caffeinate subprocess is alive."""
    return _proc is not None and _proc.poll() is None
