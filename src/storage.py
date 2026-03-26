"""
Local SQLite store for threads and messages.

Schema
------
threads:
    id          TEXT PK  — stable thread identifier (LLM-generated or rule-derived)
    channel     TEXT     — Slack channel name or "DM:<sender>"
    sender      TEXT     — most recent sender
    last_body   TEXT     — last message preview
    priority    REAL     — combined priority score (higher = more urgent)
    rule_score  REAL     — rule-based component
    llm_score   REAL     — LLM urgency component (0–10)
    unread      INTEGER  — 1 if not yet opened by user
    updated_at  TEXT     — ISO-8601 timestamp of last update

messages:
    id              TEXT PK  — notification database record id (string)
    thread_id       TEXT FK  — → threads.id
    sender          TEXT
    channel         TEXT
    body            TEXT
    timestamp       TEXT     — ISO-8601
    notification_id TEXT     — raw macOS notification id
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "notifications.db"


def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(_DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                id            TEXT PRIMARY KEY,
                channel       TEXT,
                workspace     TEXT DEFAULT '',
                sender        TEXT,
                last_body     TEXT,
                nc_group_desc TEXT DEFAULT '',
                priority      REAL DEFAULT 0,
                rule_score    REAL DEFAULT 0,
                llm_score     REAL DEFAULT 0,
                unread        INTEGER DEFAULT 1,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              TEXT PRIMARY KEY,
                thread_id       TEXT,
                sender          TEXT,
                channel         TEXT,
                body            TEXT,
                timestamp       TEXT,
                notification_id TEXT,
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_threads_priority ON threads(priority DESC);
        """)
        # Migrate existing DBs.
        for col, default in [("workspace", "''"), ("nc_group_desc", "''")]:
            try:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass

def upsert_message(
    *,
    msg_id: str,
    thread_id: str,
    sender: str,
    channel: str,
    body: str,
    timestamp: str,
    notification_id: str,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (id, thread_id, sender, channel, body, timestamp, notification_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, thread_id, sender, channel, body, timestamp, notification_id),
        )


def upsert_thread(
    *,
    thread_id: str,
    channel: str,
    workspace: str = "",
    sender: str,
    last_body: str,
    nc_group_desc: str = "",
    priority: float,
    rule_score: float,
    llm_score: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO threads (id, channel, workspace, sender, last_body, nc_group_desc,
                                 priority, rule_score, llm_score, unread, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                channel       = excluded.channel,
                workspace     = excluded.workspace,
                sender        = excluded.sender,
                last_body     = excluded.last_body,
                nc_group_desc = excluded.nc_group_desc,
                priority      = excluded.priority,
                rule_score    = excluded.rule_score,
                llm_score     = excluded.llm_score,
                unread        = 1,
                updated_at    = excluded.updated_at
            """,
            (thread_id, channel, workspace, sender, last_body, nc_group_desc,
             priority, rule_score, llm_score, now),
        )


def get_threads_by_priority(limit: int = 0) -> list[sqlite3.Row]:
    """Return threads ordered by latest message timestamp (most recent first)."""
    with db() as conn:
        query = "SELECT * FROM threads ORDER BY updated_at DESC"
        if limit > 0:
            query += f" LIMIT {limit}"
        return conn.execute(query).fetchall()


def get_messages_for_thread(thread_id: str) -> list[sqlite3.Row]:
    """Return all messages for a thread, newest first."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY timestamp DESC",
            (thread_id,),
        ).fetchall()


def delete_thread(thread_id: str) -> None:
    """Delete a thread and all its messages."""
    with db() as conn:
        conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))


def mark_thread_read(thread_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE threads SET unread = 0 WHERE id = ?", (thread_id,))


def mark_all_read() -> None:
    with db() as conn:
        conn.execute("UPDATE threads SET unread = 0")


def get_unread_count() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM threads WHERE unread = 1").fetchone()
        return row[0] if row else 0


def message_exists(notification_id: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE notification_id = ? LIMIT 1",
            (notification_id,),
        ).fetchone()
        return row is not None


def get_all_messages() -> list[sqlite3.Row]:
    """Return every message in the DB (for batch LLM clustering)."""
    with db() as conn:
        return conn.execute(
            """
            SELECT m.id, m.thread_id, m.sender, m.channel, m.body, m.timestamp,
                   m.notification_id, t.workspace
            FROM messages m
            LEFT JOIN threads t ON m.thread_id = t.id
            ORDER BY m.timestamp ASC
            """
        ).fetchall()


def reassign_message_thread(msg_id: str, new_thread_id: str) -> None:
    """Move a message to a different thread."""
    with db() as conn:
        conn.execute(
            "UPDATE messages SET thread_id = ? WHERE id = ?",
            (new_thread_id, msg_id),
        )


def update_thread_priority(
    thread_id: str,
    priority: float,
    llm_score: float,
) -> None:
    """Update priority and LLM score for an existing thread."""
    with db() as conn:
        conn.execute(
            "UPDATE threads SET priority = ?, llm_score = ? WHERE id = ?",
            (priority, llm_score, thread_id),
        )


def delete_empty_threads() -> None:
    """Remove threads that have no messages (orphaned after re-clustering)."""
    with db() as conn:
        conn.execute(
            "DELETE FROM threads WHERE id NOT IN (SELECT DISTINCT thread_id FROM messages)"
        )
