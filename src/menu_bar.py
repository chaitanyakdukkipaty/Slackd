"""
macOS menu bar app — built with rumps.

Thread list with accordion-style submenus:

  💬 3
  ─────────────────────────────────────
  ● 🔴  [#oncall]  Alice: server down   ▶
       ├─ 🔗 Open in Slack (exact message)
       ├─ ─────────────────────────────
       ├─ 10:23  Alice: server is down ASAP
       └─ 10:25  Bob: looking into it
  ● 🟠  [#general]  Carol: lunch today  ▶
       └─ 🔗 Open in Slack
  ─────────────────────────────────────
  ✅  Mark all read
  ⚙  Settings ▶
  ✗  Quit
"""
import logging
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import rumps

from src.config import cfg, save_config
from src import storage, launch_agent
import src.caffeinate as _caffeinate
from src.notification_watcher import click_nc_notification, find_and_click_nc_for_channel

if TYPE_CHECKING:
    from src.thread_organizer import ThreadOrganizer

logger = logging.getLogger(__name__)

_THRESHOLDS    = cfg.get("priority_thresholds", {})
_T_RED         = _THRESHOLDS.get("red", 8)
_T_ORANGE      = _THRESHOLDS.get("orange", 5)
_T_YELLOW      = _THRESHOLDS.get("yellow", 2)
_MAX_THREADS   = cfg.get("max_threads_displayed", 20)
_BADGE_THRESHOLD = cfg.get("badge_threshold", 1)

# Menu bar icon — Slack-themed hashtag PNG (22×22 pt, @2x for retina).
_ICON_PATH = str(Path(__file__).resolve().parent.parent / "assets" / "slackd_icon.png")

# Interval options shown in the Settings submenus.
_INTERVAL_OPTIONS = [
    ("Off",                  0),
    ("On notification",     -1),
    ("Every 15 min",        15),
    ("Every 30 min",        30),
    ("Every 1 hr",          60),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _priority_icon(priority: float) -> str:
    if priority >= _T_RED:    return "🔴"
    if priority >= _T_ORANGE: return "🟠"
    if priority >= _T_YELLOW: return "🟡"
    return "⚪"


def _fmt_time(iso: str) -> str:
    """Convert ISO-8601 to HH:MM local time."""
    try:
        # Python 3.9 doesn't parse the 'Z' suffix — normalise it first.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _bare_channel_name(channel: str) -> str:
    """Extract just the channel name from strings like 'Workspace##channel' or '#channel'."""
    # Split on ## first (Slack workspace separator), then on # to strip leading hash
    name = channel.split("##")[-1].split("#")[-1].strip()
    return name


def _slack_date_range(timestamp: str):
    """
    Parse an ISO-8601 timestamp and return (after_date, before_date) strings
    for a ±10-minute Slack search window (YYYY-MM-DD format).
    Returns (None, None) if timestamp is unparseable.
    """
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except Exception:
        return None, None
    start = ts - timedelta(minutes=10)
    end   = ts + timedelta(minutes=10)
    # Return date strings; if the window crosses midnight include both dates
    after  = start.strftime("%Y-%m-%d")
    before = end.strftime("%Y-%m-%d")
    return after, before


def _open_in_slack_fallback(channel: str, workspace: str, body: str = "",
                             timestamp: str = "") -> None:
    """
    Fallback: open Slack, jump to channel via Quick Switcher (⌘K),
    then search with ⌘F. When a timestamp is provided, a ±10-minute date
    range (after:/before:) is prepended to the query for tighter filtering.
    """
    bare = _bare_channel_name(channel).replace("'", "\\'")
    if not bare:
        subprocess.run(["open", "-a", "Slack"], check=False)
        return

    # Trim body for search — first ~50 chars, no quotes/backslashes
    search_text = ""
    if body:
        search_text = body[:50].replace("'", "").replace('"', "").replace("\\", "").strip()

    # Build the search query, optionally with a ±10-min date range.
    if timestamp:
        after, before = _slack_date_range(timestamp)
        if after and before and after != before:
            date_filter = f"after:{after} before:{before} "
        elif after:
            date_filter = f"after:{after} "
        else:
            date_filter = ""
    else:
        date_filter = ""

    full_query = f"{date_filter}{search_text}".strip()

    if full_query:
        script = f"""
            tell application "Slack" to activate
            delay 0.6
            tell application "System Events"
                tell process "Slack"
                    keystroke "k" using command down
                    delay 0.5
                    keystroke "{bare}"
                    delay 0.5
                    key code 36
                    delay 1.0
                    key code 53
                    delay 0.3
                    keystroke "f" using command down
                    delay 0.5
                    keystroke "{full_query}"
                    delay 0.5
                    key code 36
                end tell
            end tell
        """
    else:
        script = f"""
            tell application "Slack" to activate
            delay 0.6
            tell application "System Events"
                tell process "Slack"
                    keystroke "k" using command down
                    delay 0.5
                    keystroke "{bare}"
                    delay 0.5
                    key code 36
                end tell
            end tell
        """
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=12)
    except Exception as exc:
        logger.warning("AppleScript fallback failed: %s", exc)
        subprocess.run(["open", "-a", "Slack"], check=False)


def _navigate_to_message(nc_group_desc: str, channel: str, workspace: str,
                         body: str = "", timestamp: str = "") -> None:
    """
    3-tier fallback to open the right message in Slack:
      1. Exact NC click using stored nc_group_desc (opens the exact thread).
      2. Live channel scan — if the exact desc is stale, find any still-visible
         notification for this workspace+channel and click it.
      3. AppleScript ⌘K → channel → ⌘F search with optional ±10-min date filter.
    """
    if nc_group_desc and click_nc_notification(nc_group_desc):
        return
    if channel and find_and_click_nc_for_channel(workspace, channel):
        return
    _open_in_slack_fallback(channel, workspace, body, timestamp)


# ---------------------------------------------------------------------------
# Menu bar app
# ---------------------------------------------------------------------------

class SlackOrganizerApp(rumps.App):
    def __init__(self, organizer=None, scheduler=None) -> None:
        super().__init__(name="SlackOrganizer", title=None, quit_button=None)
        self.icon = _ICON_PATH
        self.template = False  # keep original colours (not a monochrome template)
        self._organizer = organizer
        self._scheduler = scheduler
        self._llm_running = False  # guards concurrent LLM button presses
        self._build_menu()

    # ------------------------------------------------------------------ #
    #  Menu construction                                                   #
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        self.menu.clear()

        threads = storage.get_threads_by_priority(limit=_MAX_THREADS)
        unread  = storage.get_unread_count()

        self.title = f" {unread}" if unread >= _BADGE_THRESHOLD else None

        if not threads:
            self.menu.add(rumps.MenuItem("No Slack notifications yet", callback=None))
        else:
            for thread in threads:
                self.menu.add(self._build_thread_item(thread))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Cluster Threads", callback=self._run_cluster))
        self.menu.add(rumps.MenuItem("Score Priority",  callback=self._run_score))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Mark all read", callback=self._mark_all_read))
        self.menu.add(rumps.MenuItem("Delete all threads", callback=self._delete_all_threads))

        # Settings sub-menu
        settings = rumps.MenuItem("⚙  Settings")
        current  = cfg.get("llm", {}).get("backend", "copilot")
        for name in ["copilot", "claude", "openai"]:
            check = "✓ " if name == current else "   "
            settings.add(rumps.MenuItem(
                f"{check}{name}",
                callback=self._make_backend_callback(name),
            ))
        settings.add(rumps.separator)
        login_check = "✓ " if launch_agent.is_enabled() else "   "
        settings.add(rumps.MenuItem(
            f"{login_check}Launch at Login",
            callback=self._toggle_launch_at_login,
        ))
        sleep_check = "✓ " if cfg.get("prevent_sleep", True) else "   "
        settings.add(rumps.MenuItem(
            f"{sleep_check}Prevent Sleep",
            callback=self._toggle_prevent_sleep,
        ))
        settings.add(rumps.separator)
        settings.add(self._build_interval_submenu("Auto-cluster", "cluster_interval"))
        settings.add(self._build_interval_submenu("Auto-score",   "score_interval"))
        self.menu.add(settings)

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("✗  Quit", callback=rumps.quit_application))

    def _build_interval_submenu(self, label: str, config_key: str) -> rumps.MenuItem:
        """Build a submenu for selecting an auto-run interval."""
        current_val = cfg.get(config_key, 0)
        current_label = next(
            (lbl for lbl, val in _INTERVAL_OPTIONS if val == current_val),
            str(current_val),
        )
        parent = rumps.MenuItem(f"{label}: {current_label}")
        for opt_label, opt_val in _INTERVAL_OPTIONS:
            check = "✓ " if opt_val == current_val else "   "
            parent.add(rumps.MenuItem(
                f"{check}{opt_label}",
                callback=self._make_interval_callback(config_key, opt_val),
            ))
        return parent

    def _build_thread_item(self, thread) -> rumps.MenuItem:
        """
        Build a thread menu item with an accordion submenu.

        Thread header: ● 🔴  [#channel]  sender: preview...
        Submenu:
            🔗 Open in Slack (exact message)
            ─────────────────────────────
            HH:MM  sender: message body
            HH:MM  sender: message body
            ...
        """
        icon      = _priority_icon(thread["priority"])
        unread    = "●" if thread["unread"] else "○"
        sender    = (thread["sender"]    or "")[:22]
        channel   = (thread["channel"]   or "")
        workspace = (thread["workspace"] or "")
        preview   = (thread["last_body"] or "")[:50]
        nc_desc   = thread["nc_group_desc"] or ""
        tid       = thread["id"]

        sender_part = f"{sender}: " if sender else ""
        label = f"{unread} {icon}  [{channel}]  {sender_part}{preview}"

        # Thread header — has a submenu so clicking opens accordion, not Slack directly.
        item = rumps.MenuItem(label)

        # ── 🔗 Open in Slack (exact message / thread) ──
        # Thread-level open: the label hints whether exact NC click is likely.
        # nc_desc here is the thread's stored desc (latest message's desc).
        open_label = "🔗  Open in Slack — exact thread" if nc_desc else "🔗  Open in Slack — search in channel"
        item.add(rumps.MenuItem(
            open_label,
            callback=self._make_open_callback(
                tid, nc_desc, channel, workspace, preview,
                timestamp=thread["updated_at"] or "",
            ),
        ))

        # ── Message history (accordion) — newest first ──
        messages = storage.get_messages_for_thread(tid)
        if messages:
            item.add(rumps.separator)
            for msg in messages:
                time_str = _fmt_time(msg["timestamp"])
                msender  = (msg["sender"] or "")[:18]
                mbody    = (msg["body"]   or "")[:60]
                sender_label = f"{msender}: " if msender else ""
                msg_label = f"  {time_str}  {sender_label}{mbody}"
                # Each message stores its own nc_group_desc — use it for exact NC click.
                msg_nc_desc = (msg["nc_group_desc"] or "") if "nc_group_desc" in msg.keys() else ""
                item.add(rumps.MenuItem(
                    msg_label,
                    callback=self._make_open_callback(
                        tid, msg_nc_desc, channel, workspace, msg["body"] or "",
                        timestamp=msg["timestamp"] or "",
                        msg_id=msg["id"],
                    ),
                ))

        # ── Delete thread ──
        item.add(rumps.separator)
        item.add(rumps.MenuItem(
            "🗑  Delete thread",
            callback=self._make_delete_callback(tid),
        ))

        return item

    # ------------------------------------------------------------------ #
    #  Callbacks                                                           #
    # ------------------------------------------------------------------ #

    def _make_open_callback(self, thread_id: str, nc_group_desc: str,
                             channel: str, workspace: str, body: str = "",
                             timestamp: str = "", msg_id: Optional[str] = None):
        def callback(_):
            if msg_id:
                storage.mark_message_read(msg_id)
            else:
                storage.mark_thread_read(thread_id)
            _navigate_to_message(nc_group_desc, channel, workspace, body, timestamp)
            self._build_menu()
        return callback

    def _make_backend_callback(self, name: str):
        def callback(_):
            cfg["llm"]["backend"] = name
            save_config(cfg)
            logger.info("LLM backend switched to: %s", name)
            self._build_menu()
        return callback

    def _make_delete_callback(self, thread_id: str):
        def callback(_):
            storage.delete_thread(thread_id)
            self._build_menu()
        return callback

    def _make_interval_callback(self, config_key: str, value: int):
        def callback(_):
            cfg[config_key] = value
            save_config(cfg)
            self._rewire_scheduler(config_key, value)
            self._build_menu()
        return callback

    def _rewire_scheduler(self, config_key: str, value: int) -> None:
        """
        Update APScheduler job for cluster or score after interval change.
        Also re-registers post-process hooks for on-notification mode.
        """
        if self._scheduler is None or self._organizer is None:
            return

        if config_key == "cluster_interval":
            job_id = "cluster_job"
            fn = self._organizer.cluster_all
            hook_name = "cluster-on-notif"
        else:
            job_id = "score_job"
            fn = self._organizer.score_all
            hook_name = "score-on-notif"

        # Remove existing scheduled job if any.
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

        # Remove on-notification hooks for this operation and re-add if needed.
        self._organizer._post_process_hooks = [
            h for h in self._organizer._post_process_hooks
            if getattr(h, "_hook_name", None) != hook_name
        ]

        if value == -1:
            def _hook(fn=fn, hook_name=hook_name):
                t = threading.Thread(target=fn, name=hook_name, daemon=True)
                t.start()
            _hook._hook_name = hook_name
            self._organizer.add_post_process_hook(_hook)
            logger.info("%s set to: on notification", config_key)
        elif value > 0:
            def _job(fn=fn):
                t = threading.Thread(target=fn, name=job_id, daemon=True)
                t.start()
            self._scheduler.add_job(
                _job,
                trigger="interval",
                minutes=value,
                id=job_id,
                max_instances=1,
            )
            logger.info("%s set to: every %d min", config_key, value)
        else:
            logger.info("%s set to: manual only", config_key)

    def _toggle_launch_at_login(self, _) -> None:
        if launch_agent.is_enabled():
            launch_agent.disable()
        else:
            launch_agent.enable()
        self._build_menu()

    def _toggle_prevent_sleep(self, _) -> None:
        current = cfg.get("prevent_sleep", True)
        cfg["prevent_sleep"] = not current
        save_config(cfg)
        if cfg["prevent_sleep"]:
            _caffeinate.start()
        else:
            _caffeinate.stop()
        self._build_menu()

    def _run_cluster(self, _) -> None:
        if self._organizer is None or self._llm_running:
            return
        self._llm_running = True
        self.title = " …"

        def _task():
            try:
                self._organizer.cluster_all()
            finally:
                self._llm_running = False
                self._build_menu()

        threading.Thread(target=_task, name="cluster-manual", daemon=True).start()

    def _run_score(self, _) -> None:
        if self._organizer is None or self._llm_running:
            return
        self._llm_running = True
        self.title = " …"

        def _task():
            try:
                self._organizer.score_all()
            finally:
                self._llm_running = False
                self._build_menu()

        threading.Thread(target=_task, name="score-manual", daemon=True).start()

    def _mark_all_read(self, _) -> None:
        storage.mark_all_read()
        self._build_menu()

    def _delete_all_threads(self, _) -> None:
        with storage.db() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM threads")
        self._build_menu()

    # ------------------------------------------------------------------ #
    #  Timer refresh                                                       #
    # ------------------------------------------------------------------ #

    @rumps.timer(5)
    def refresh(self, _) -> None:
        self._build_menu()
