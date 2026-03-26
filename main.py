"""
Entry point — wires together the notification watcher, thread organizer,
and the macOS menu bar app.

The watcher uses two complementary mechanisms:
  1. `log stream` subprocess (real-time trigger via NotificationWatcher.start_log_stream)
  2. APScheduler periodic poll (catches any missed notifications)
The rumps menu bar app runs on the main thread (required by macOS AppKit).
"""
import logging
import os
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from src import storage
from src.caffeinate import start as caffeinate_start, stop as caffeinate_stop
from src.config import cfg
from src.notification_watcher import NotificationWatcher
from src.thread_organizer import ThreadOrganizer
from src.menu_bar import SlackOrganizerApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_PID_FILE = Path(__file__).resolve().parent / "data" / "slack_organizer.pid"


def _acquire_pid_lock() -> bool:
    """
    Write our PID to a lock file.
    Returns False (and exits) if another instance is already running.
    """
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        try:
            existing_pid = int(_PID_FILE.read_text().strip())
            # Check if that process is actually alive.
            os.kill(existing_pid, 0)
            logger.warning(
                "Another instance is already running (PID %d). Exiting.", existing_pid
            )
            return False
        except (ProcessLookupError, ValueError):
            pass  # stale PID file — safe to overwrite
    _PID_FILE.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _poll_job(watcher: NotificationWatcher, organizer: ThreadOrganizer) -> None:
    try:
        notifications = watcher.poll()
        if notifications:
            logger.info("Poll found %d new Slack notification(s)", len(notifications))
            organizer.process(notifications)
    except Exception:
        logger.exception("Error in poll job")


def main() -> None:
    if not _acquire_pid_lock():
        sys.exit(0)

    # Initialise the local database.
    storage.init_db()

    # Keep Mac awake so notifications are never missed.
    if cfg.get("prevent_sleep", True):
        caffeinate_start()

    organizer = ThreadOrganizer()
    watcher = NotificationWatcher()

    # Real-time: log stream triggers immediate Accessibility read + organizer.
    watcher.register_callback(organizer.process)
    watcher.start_log_stream()

    # Periodic safety net: catches any notifications missed by log stream.
    poll_interval = cfg.get("poll_interval", 5)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _poll_job,
        trigger="interval",
        seconds=poll_interval,
        args=[watcher, organizer],
        id="notification_poll",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Notification watcher started (log stream + %ds poll)", poll_interval)

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        caffeinate_stop()
        watcher.stop()
        scheduler.shutdown(wait=False)
        _release_pid_lock()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # rumps.App.run() must be called from the main thread.
    app = SlackOrganizerApp()
    try:
        app.run()
    finally:
        caffeinate_stop()
        _release_pid_lock()


if __name__ == "__main__":
    main()
