"""
macOS Notification Center watcher — macOS 15+ compatible.

Strategy (confirmed working on macOS 15.7):
  1. Stream the unified log for Slack notification delivery events.
  2. On each event, read the Accessibility tree of NotificationCenter.app (pid dynamic).
  3. Parse AXGroup description "Slack, {workspace}, {channel}, {body}" directly
     from the description string — more reliable than counting AXStaticText children.
  4. Also poll NC on a periodic timer to catch any notifications missed during startup.

Requires:
  - Accessibility permission: System Settings → Privacy & Security → Accessibility → Terminal ✓
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import psutil
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementPerformAction,
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXPressAction,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
    kAXWindowsAttribute,
)

logger = logging.getLogger(__name__)

_SLACK_BUNDLE_IDS = {
    "com.tinyspeck.slackmacgap",
    "com.slack.slackmacgap",
}

_AX_GROUP       = "AXGroup"
_AX_STATIC_TEXT = "AXStaticText"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SlackNotification:
    notification_id: str   # stable content hash (idempotent)
    sender: str            # person who sent the message
    channel: str           # #channel-name  (no workspace prefix)
    workspace: str         # Slack workspace name
    body: str              # message text (sender stripped)
    timestamp: str         # ISO-8601
    nc_group_desc: str = ""  # full AXGroup description — used to AXPress the exact message


# ---------------------------------------------------------------------------
# Accessibility helpers
# ---------------------------------------------------------------------------

def _ax(el, attr):
    err, val = AXUIElementCopyAttributeValue(el, attr, None)
    return val if err == 0 else None


def _find_nc_pid() -> Optional[int]:
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] == "NotificationCenter":
            return proc.info["pid"]
    return None


# ---------------------------------------------------------------------------
# NC AX tree parsing
# ---------------------------------------------------------------------------

def _parse_nc_group_desc(desc: str) -> Optional[dict]:
    """
    Parse "Slack, {workspace}, {channel}, {body}" from the AXGroup description.

    Returns dict with keys: workspace, channel, body — or None if not a Slack group.
    """
    if not desc.lower().startswith("slack,"):
        return None
    # Split on ", " with a max of 3 splits: ["Slack", workspace, channel, body]
    parts = desc.split(", ", 3)
    if len(parts) < 3:
        return None
    workspace = parts[1].strip()
    channel   = parts[2].strip()
    body      = parts[3].strip() if len(parts) > 3 else ""
    return {"workspace": workspace, "channel": channel, "body": body}


def _walk_nc_window(el, results: list, depth: int = 0, collect_elements: bool = False):
    """Recursively find Slack AXGroup elements in a NotificationCenter window."""
    if depth > 12:
        return

    role = _ax(el, kAXRoleAttribute) or ""
    if role == _AX_GROUP:
        desc = _ax(el, kAXDescriptionAttribute) or ""
        parsed = _parse_nc_group_desc(desc)
        if parsed is not None:
            entry = {"group_desc": desc, **parsed}
            if collect_elements:
                entry["_element"] = el
            results.append(entry)
            return  # don't recurse inside — content is already captured

    for child in (_ax(el, kAXChildrenAttribute) or []):
        _walk_nc_window(child, results, depth + 1, collect_elements)


def _read_nc_slack_notifications(nc_pid: int) -> list[dict]:
    """Walk the NotificationCenter AX tree and return Slack notification dicts."""
    ax      = AXUIElementCreateApplication(nc_pid)
    windows = _ax(ax, kAXWindowsAttribute) or []
    results = []
    for window in windows:
        title = _ax(window, kAXTitleAttribute) or ""
        if title and "Notification Center" not in title:
            continue
        _walk_nc_window(window, results)
    return results


def click_nc_notification(nc_group_desc: str) -> bool:
    """
    Find the NC AXGroup matching nc_group_desc and AXPress it.
    This is identical to the user clicking the notification — Slack opens the exact message.
    Returns True if clicked, False if the notification is no longer in NC.
    """
    nc_pid = _find_nc_pid()
    if nc_pid is None:
        return False

    ax      = AXUIElementCreateApplication(nc_pid)
    windows = _ax(ax, kAXWindowsAttribute) or []
    for window in windows:
        title = _ax(window, kAXTitleAttribute) or ""
        if title and "Notification Center" not in title:
            continue
        groups: list[dict] = []
        _walk_nc_window(window, groups, collect_elements=True)
        for g in groups:
            if g.get("group_desc", "") == nc_group_desc:
                elem = g.get("_element")
                if elem is not None:
                    err = AXUIElementPerformAction(elem, kAXPressAction)
                    if err == 0:
                        logger.info("Clicked NC notification: %s", nc_group_desc[:60])
                        return True
                    logger.warning("AXPress failed (err=%d): %s", err, nc_group_desc[:60])
    return False


def find_and_click_nc_for_channel(workspace: str, channel: str) -> bool:
    """
    Live-scan NC for any still-visible Slack notification matching the given
    workspace + channel.  Used as a second-tier fallback when the stored
    nc_group_desc is stale (notification was already dismissed / replaced).
    Returns True if a matching notification was found and clicked.
    """
    nc_pid = _find_nc_pid()
    if nc_pid is None:
        return False

    # Normalise for comparison: strip leading '#' and lowercase.
    bare_channel   = channel.lstrip("#").lower()
    bare_workspace = workspace.lower()

    ax      = AXUIElementCreateApplication(nc_pid)
    windows = _ax(ax, kAXWindowsAttribute) or []
    for window in windows:
        title = _ax(window, kAXTitleAttribute) or ""
        if title and "Notification Center" not in title:
            continue
        groups: list[dict] = []
        _walk_nc_window(window, groups, collect_elements=True)
        for g in groups:
            gws  = g.get("workspace", "").lower()
            gch  = g.get("channel",   "").lstrip("#").lower()
            if gch == bare_channel and (not bare_workspace or gws == bare_workspace):
                elem = g.get("_element")
                if elem is not None:
                    err = AXUIElementPerformAction(elem, kAXPressAction)
                    if err == 0:
                        logger.info("Clicked NC channel notification: %s / %s", workspace, channel)
                        return True
    return False


# ---------------------------------------------------------------------------
# Sender / body parsing
# ---------------------------------------------------------------------------

# Matches "Message: <username> rest…" — Slack bot-style notifications
_BRACKET_SENDER_RE = re.compile(r"^(?:Message:\s*)?<([^>]+)>\s*(.*)", re.DOTALL)

# Matches "Full Name: message body" — standard Slack message format
# Sender must be ≤ 50 chars, no leading @ or special chars, contains a letter
_COLON_SENDER_RE = re.compile(
    r"^([A-Za-z][^:]{0,48}?)\s*:\s+(.+)", re.DOTALL
)

# Names that look like content rather than senders (contain keywords)
_NOT_SENDER_PATTERNS = re.compile(
    r"https?://|www\.|@[A-Z]|\d{1,2}(am|pm)|today|yesterday|minute|second",
    re.IGNORECASE,
)


def _parse_sender(body: str) -> tuple[str, str]:
    """
    Extract (sender, clean_body) from the raw notification body text.

    Supports:
      "Message: <username> rest"       →  ("username", "rest")
      "Full Name: message text here"   →  ("Full Name", "message text here")
      Anything else                    →  ("", original_body)
    """
    body = body.strip()

    # Format 1: Message: <username> body
    m = _BRACKET_SENDER_RE.match(body)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Format 2: SenderName: message body
    m = _COLON_SENDER_RE.match(body)
    if m:
        candidate = m.group(1).strip()
        # Reject if it looks like content rather than a person/bot name
        if not _NOT_SENDER_PATTERNS.search(candidate):
            return candidate, m.group(2).strip()

    return "", body


def _make_notification_id(workspace: str, channel: str, body: str) -> str:
    raw = f"{workspace}|{channel}|{body}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_log_timestamp(line: str) -> Optional[str]:
    """
    Extract the ISO-8601 timestamp from a `log stream` output line.
    Format: "2026-03-26 17:07:23.456789+0530  0x... ..."
    Returns UTC ISO string, or None if unparseable.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)([+-]\d{4}|\+\d{2}:\d{2})?", line)
    if not m:
        return None
    dt_str  = m.group(1)
    tz_str  = (m.group(2) or "").strip()
    # Normalise tz: "+0530" → "+05:30"
    if tz_str and ":" not in tz_str:
        tz_str = tz_str[:3] + ":" + tz_str[3:]
    try:
        dt = datetime.fromisoformat(dt_str + (tz_str or "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _raw_to_slack_notification(raw: dict, hint_timestamp: Optional[str] = None) -> SlackNotification:
    workspace  = raw.get("workspace", "")
    channel    = raw.get("channel", "")
    body       = raw.get("body", "")
    group_desc = raw.get("group_desc", "")

    sender, clean_body = _parse_sender(body)

    return SlackNotification(
        notification_id=_make_notification_id(workspace, channel, body),
        sender=sender,
        channel=channel,
        workspace=workspace,
        body=clean_body or body,
        timestamp=hint_timestamp or datetime.now(timezone.utc).isoformat(),
        nc_group_desc=group_desc,
    )


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

class NotificationWatcher:
    """
    Watches macOS Notification Center for Slack notifications.
    Combines log stream (real-time trigger) + Accessibility API (content).
    """

    def __init__(self) -> None:
        self._nc_pid: Optional[int] = None
        self._seen_ids: set[str] = set()
        self._log_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[list[SlackNotification]], None]] = []

    def register_callback(self, cb: Callable[[list[SlackNotification]], None]) -> None:
        self._callbacks.append(cb)

    def poll(self, hint_timestamp: Optional[str] = None) -> list[SlackNotification]:
        """Read current NC AX tree, return new Slack notifications not yet seen."""
        nc_pid = self._get_nc_pid()
        if nc_pid is None:
            logger.warning(
                "NotificationCenter.app not found. "
                "Grant Accessibility access to Terminal in System Settings → Privacy & Security."
            )
            return []

        try:
            raw_list = _read_nc_slack_notifications(nc_pid)
        except Exception as exc:
            logger.error("Error reading NC AX tree: %s", exc)
            return []

        notifications = []
        for raw in raw_list:
            notif = _raw_to_slack_notification(raw, hint_timestamp=hint_timestamp)
            with self._lock:
                if notif.notification_id not in self._seen_ids:
                    self._seen_ids.add(notif.notification_id)
                    notifications.append(notif)
                    logger.info(
                        "New: [%s] %s: %s",
                        notif.channel, notif.sender or "?", notif.body[:60],
                    )

        return notifications

    def start_log_stream(self) -> None:
        """Start background thread that triggers poll() on Slack delivery log events."""
        t = threading.Thread(target=self._log_stream_loop, daemon=True, name="log-stream")
        t.start()

    def stop(self) -> None:
        if self._log_proc:
            self._log_proc.terminate()
            self._log_proc = None

    def _get_nc_pid(self) -> Optional[int]:
        if self._nc_pid is None:
            self._nc_pid = _find_nc_pid()
        else:
            try:
                p = psutil.Process(self._nc_pid)
                if p.name() != "NotificationCenter":
                    self._nc_pid = _find_nc_pid()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._nc_pid = _find_nc_pid()
        return self._nc_pid

    def _log_stream_loop(self) -> None:
        bundle_filter = " OR ".join(
            f'message CONTAINS "{bid}"' for bid in _SLACK_BUNDLE_IDS
        )
        predicate = (
            f'process == "usernoted" AND ({bundle_filter}) AND message CONTAINS "Delivering"'
        )
        logger.info("Starting log stream watcher")

        while True:
            try:
                self._log_proc = subprocess.Popen(
                    ["log", "stream", "--predicate", predicate],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                os.set_blocking(self._log_proc.stdout.fileno(), False)

                while True:
                    r, _, _ = select.select([self._log_proc.stdout], [], [], 1.0)
                    if r:
                        line = self._log_proc.stdout.readline()
                        if any(bid in line for bid in _SLACK_BUNDLE_IDS) and "Delivering" in line:
                            log_ts = _parse_log_timestamp(line)
                            time.sleep(0.15)
                            notifications = self.poll(hint_timestamp=log_ts)
                            if notifications:
                                for cb in self._callbacks:
                                    try:
                                        cb(notifications)
                                    except Exception:
                                        logger.exception("Callback error")
            except Exception as exc:
                logger.error("Log stream error: %s — restarting in 5s", exc)
                time.sleep(5)
