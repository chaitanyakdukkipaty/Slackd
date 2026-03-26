"""
macOS menu bar app — built with rumps.

Thread list with accordion-style submenus:

  🔔 3
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
from datetime import datetime, timezone

import rumps

from src.config import cfg, save_config
from src import storage, launch_agent
import src.caffeinate as _caffeinate
from src.notification_watcher import click_nc_notification

logger = logging.getLogger(__name__)

_THRESHOLDS    = cfg.get("priority_thresholds", {})
_T_RED         = _THRESHOLDS.get("red", 8)
_T_ORANGE      = _THRESHOLDS.get("orange", 5)
_T_YELLOW      = _THRESHOLDS.get("yellow", 2)
_MAX_THREADS   = cfg.get("max_threads_displayed", 20)
_BADGE_THRESHOLD = cfg.get("badge_threshold", 1)


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


def _open_in_slack_fallback(channel: str, workspace: str, body: str = "") -> None:
    """
    Fallback: open Slack, jump to channel via Quick Switcher (⌘K),
    then search for the message body with ⌘F so Slack highlights it.
    User can then click the message to open the thread panel.
    """
    bare = _bare_channel_name(channel).replace("'", "\\'")
    if not bare:
        subprocess.run(["open", "-a", "Slack"], check=False)
        return

    # Trim body for search — use first ~50 chars, no quotes/special chars
    search_text = ""
    if body:
        search_text = body[:50].replace("'", "").replace('"', "").replace("\\", "").strip()

    if search_text:
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
                    keystroke "f" using command down
                    delay 0.5
                    keystroke "{search_text}"
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
                         body: str = "") -> None:
    """
    Primary: click the NC notification (opens exact message/thread in Slack).
    Fallback: Quick Switcher to channel + ⌘F search for the message body.
    """
    if nc_group_desc and click_nc_notification(nc_group_desc):
        return  # exact message/thread opened via NC click ✓
    _open_in_slack_fallback(channel, workspace, body)


# ---------------------------------------------------------------------------
# Menu bar app
# ---------------------------------------------------------------------------

class SlackOrganizerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(name="SlackOrganizer", title="🔔", quit_button=None)
        self._build_menu()

    # ------------------------------------------------------------------ #
    #  Menu construction                                                   #
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        self.menu.clear()

        threads = storage.get_threads_by_priority(limit=_MAX_THREADS)
        unread  = storage.get_unread_count()

        self.title = f"🔔 {unread}" if unread >= _BADGE_THRESHOLD else "🔔"

        if not threads:
            self.menu.add(rumps.MenuItem("No Slack notifications yet", callback=None))
        else:
            for thread in threads:
                self.menu.add(self._build_thread_item(thread))

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
        noai_check = "✓ " if cfg.get("no_ai", False) else "   "
        settings.add(rumps.MenuItem(
            f"{noai_check}No AI Mode",
            callback=self._toggle_no_ai,
        ))
        self.menu.add(settings)

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("✗  Quit", callback=rumps.quit_application))

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
        open_label = "🔗  Open in Slack — exact thread" if nc_desc else "🔗  Open in Slack — search in channel"
        item.add(rumps.MenuItem(
            open_label,
            callback=self._make_open_callback(tid, nc_desc, channel, workspace, preview),
        ))

        # ── Message history (accordion) — newest first ──
        messages = storage.get_messages_for_thread(tid)
        if messages:
            item.add(rumps.separator)
            for i, msg in enumerate(messages):
                time_str = _fmt_time(msg["timestamp"])
                msender  = (msg["sender"] or "")[:18]
                mbody    = (msg["body"]   or "")[:60]
                sender_label = f"{msender}: " if msender else ""
                msg_label = f"  {time_str}  {sender_label}{mbody}"
                # The latest message (i==0, since DESC order) can use the stored nc_desc
                # to AXPress the exact notification. Older messages fall back to ⌘F search.
                msg_nc_desc = nc_desc if i == 0 else ""
                item.add(rumps.MenuItem(
                    msg_label,
                    callback=self._make_open_callback(
                        tid, msg_nc_desc, channel, workspace, msg["body"] or "",
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
                             channel: str, workspace: str, body: str = ""):
        def callback(_):
            storage.mark_thread_read(thread_id)
            _navigate_to_message(nc_group_desc, channel, workspace, body)
            self._build_menu()
        return callback

    def _make_backend_callback(self, name: str):
        def callback(_):
            cfg["llm"]["backend"] = name
            logger.info("LLM backend switched to: %s", name)
            self._build_menu()
        return callback

    def _make_delete_callback(self, thread_id: str):
        def callback(_):
            storage.delete_thread(thread_id)
            self._build_menu()
        return callback

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

    def _toggle_no_ai(self, _) -> None:
        cfg["no_ai"] = not cfg.get("no_ai", False)
        save_config(cfg)
        self._build_menu()

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
