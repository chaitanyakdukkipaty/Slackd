"""
macOS LaunchAgent management — "Launch at Login" support.

Installs / removes ~/Library/LaunchAgents/com.slackorganizer.plist
and loads/unloads it with launchctl so the service starts on login.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL      = "com.slackorganizer"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"

# Resolve paths relative to this file so they survive moves.
_PROJECT_DIR = Path(__file__).resolve().parent.parent
_PYTHON      = _PROJECT_DIR / ".venv" / "bin" / "python3"
_MAIN        = _PROJECT_DIR / "main.py"
_LOG_OUT     = _PROJECT_DIR / "data" / "launchagent.stdout.log"
_LOG_ERR     = _PROJECT_DIR / "data" / "launchagent.stderr.log"


def _plist_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{_PYTHON}</string>
        <string>{_MAIN}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>{_LOG_OUT}</string>

    <key>StandardErrorPath</key>
    <string>{_LOG_ERR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""


def is_enabled() -> bool:
    """Return True if the LaunchAgent plist exists."""
    return _PLIST_PATH.exists()


def enable() -> bool:
    """Install the plist for next login. Returns True on success."""
    try:
        _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOG_OUT.parent.mkdir(parents=True, exist_ok=True)
        _PLIST_PATH.write_text(_plist_xml(), encoding="utf-8")

        # Unload any previously loaded version, then load the new plist.
        # We pass -w so launchd registers it but does NOT immediately spawn
        # a second instance (the app is already running).
        subprocess.run(
            ["launchctl", "unload", "-w", str(_PLIST_PATH)],
            capture_output=True,
        )
        result = subprocess.run(
            ["launchctl", "load", "-w", str(_PLIST_PATH)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("launchctl load failed: %s", result.stderr.strip())
            # Plist is still written — it will activate on next login.

        logger.info("LaunchAgent installed (active from next login): %s", _PLIST_PATH)
        return True
    except Exception as exc:
        logger.error("Failed to install LaunchAgent: %s", exc)
        return False


def disable() -> bool:
    """Unload and remove the plist. Returns True on success."""
    try:
        if _PLIST_PATH.exists():
            subprocess.run(
                ["launchctl", "unload", str(_PLIST_PATH)],
                capture_output=True,
            )
            _PLIST_PATH.unlink()
        logger.info("LaunchAgent removed")
        return True
    except Exception as exc:
        logger.error("Failed to remove LaunchAgent: %s", exc)
        return False
