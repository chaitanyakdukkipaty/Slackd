"""
Thread organizer — groups incoming Slack notifications into conversation threads
and optionally scores each thread for urgency/priority.

Ingestion (always fast, no LLM):
  New notifications → channel-slug grouping → stored in DB immediately.

On-demand / scheduled LLM operations:
  cluster_all() — re-clusters ALL messages in DB using LLM, reassigns thread_ids.
  score_all()   — scores ALL threads in DB using LLM, updates priorities.

Interval config (config.yaml):
   0  = manual only (button press)
  -1  = run after every new notification batch
   N  = run every N minutes (via APScheduler)
"""
import json
import logging
import re
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from src.config import cfg
from src.llm.base import BackendFactory
from src.notification_watcher import SlackNotification
from src import storage

logger = logging.getLogger(__name__)

_SCORING = cfg.get("scoring", {})
_LLM_WEIGHT = _SCORING.get("llm_weight", 1.0)


@dataclass
class MessageBundle:
    notification_id: str
    sender: str
    channel: str
    workspace: str
    body: str
    timestamp: str
    thread_id: Optional[str] = None


def _extract_json(text: str):
    """Try to pull a JSON object/array out of LLM output that may have prose around it."""
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


@staticmethod
def _channel_thread_id(channel: str, workspace: str = "") -> str:
    """Deterministic slug based on workspace+channel only (no sender)."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{workspace}-{channel}".lower()).strip("-")
    return slug[:40] or "general"


class ThreadOrganizer:
    def __init__(self) -> None:
        # LLM backend — lazily instantiated when first needed.
        self._llm = None
        self._llm_backend_name: Optional[str] = None
        # Optional hook called after process() stores new notifications.
        # Used to trigger on-notification cluster/score runs.
        self._post_process_hooks: list[Callable] = []

    def add_post_process_hook(self, fn: Callable) -> None:
        """Register a callable to invoke after each batch of new notifications is stored."""
        self._post_process_hooks.append(fn)

    def _get_llm(self):
        """Return the LLM backend, re-instantiating if the configured backend changed."""
        backend_name = cfg.get("llm", {}).get("backend", "copilot")
        if self._llm is None or self._llm_backend_name != backend_name:
            self._llm = BackendFactory.get(backend_name)
            self._llm_backend_name = backend_name
        return self._llm

    # ------------------------------------------------------------------ #
    #  Ingestion (no LLM)                                                  #
    # ------------------------------------------------------------------ #

    def process(self, notifications: list[SlackNotification]) -> None:
        """
        Ingest a batch of new notifications using channel-slug grouping.
        No LLM calls are made here. Post-process hooks (e.g. auto cluster/score)
        are invoked after storage if any new notifications were saved.
        """
        if not notifications:
            return

        new = [n for n in notifications if not storage.message_exists(n.notification_id)]
        if not new:
            return

        bundles = [
            MessageBundle(
                notification_id=n.notification_id,
                sender=n.sender,
                channel=n.channel,
                workspace=n.workspace,
                body=n.body,
                timestamp=n.timestamp,
                thread_id=_channel_thread_id(n.channel, n.workspace),
            )
            for n in new
        ]

        self._persist(bundles)

        for hook in self._post_process_hooks:
            try:
                hook()
            except Exception:
                logger.exception("Post-process hook failed")

    # ------------------------------------------------------------------ #
    #  On-demand / scheduled LLM operations                               #
    # ------------------------------------------------------------------ #

    def cluster_all(self) -> None:
        """
        Re-cluster ALL messages in the DB using the LLM.
        Moves messages between threads, creates new threads as needed.
        """
        all_msgs = storage.get_all_messages()
        if not all_msgs:
            logger.info("cluster_all: no messages to cluster")
            return

        logger.info("cluster_all: clustering %d messages", len(all_msgs))

        existing_threads = storage.get_threads_by_priority(limit=50)
        existing_context = "\n".join(
            f"  EXISTING id={t['id']!r} channel={t['channel']!r} "
            f"workspace={t['workspace']!r} last_msg={t['last_body'][:80]!r}"
            for t in existing_threads
        )

        messages_text = "\n".join(
            f"{i}: id={m['id']!r} channel={m['channel']!r} "
            f"workspace={m['workspace'] or ''!r} sender={m['sender']!r} "
            f"body={m['body'][:120]!r}"
            for i, m in enumerate(all_msgs)
        )

        existing_section = (
            f"\nExisting threads (you may reuse their id):\n{existing_context}\n"
            if existing_context else ""
        )

        prompt = textwrap.dedent(f"""
            You are a Slack thread organiser. Re-cluster ALL messages below into
            conversation threads.

            RULES:
            1. Reuse an EXISTING thread id if the message clearly belongs there.
            2. Messages in DIFFERENT channels are NEVER in the same thread.
            3. Bot/automated messages get their own thread unless they repeat the same alert.
            4. Calendar reminders are always standalone threads.
            5. thread_id must be a lowercase hyphenated slug, max 40 chars.
               Make slugs descriptive, e.g. "preprod-deploy-failure".
            6. Respond ONLY with a JSON array — no explanation.

            Output format:
            [
              {{"index": 0, "thread_id": "slug"}},
              {{"index": 1, "thread_id": "slug"}},
              ...
            ]
            {existing_section}
            Messages:
            {messages_text}
        """).strip()

        try:
            raw = self._get_llm().ask(prompt)
            parsed = _extract_json(raw)
            if not isinstance(parsed, list):
                logger.warning("cluster_all: LLM returned non-list, aborting")
                return
        except Exception as exc:
            logger.warning("cluster_all LLM call failed: %s", exc)
            return

        # Apply reassignments.
        for item in parsed:
            idx = item.get("index")
            tid = item.get("thread_id")
            if idx is None or not tid or not (0 <= idx < len(all_msgs)):
                continue
            tid = str(tid)[:40]
            msg = all_msgs[idx]
            old_tid = msg["thread_id"]
            if tid == old_tid:
                continue
            # Ensure the target thread exists (create stub if new slug).
            if not any(t["id"] == tid for t in existing_threads):
                storage.upsert_thread(
                    thread_id=tid,
                    channel=msg["channel"],
                    workspace=msg["workspace"] or "",
                    sender=msg["sender"] or "",
                    last_body=msg["body"][:200],
                    nc_group_desc="",
                    priority=0.0,
                    rule_score=0.0,
                    llm_score=0.0,
                )
            storage.reassign_message_thread(msg["id"], tid)

        # Clean up orphaned threads (no messages left).
        storage.delete_empty_threads()
        logger.info("cluster_all: complete")

    def score_all(self) -> None:
        """
        Score ALL threads in the DB using the LLM.
        Updates priority and llm_score for each thread.
        """
        threads = storage.get_threads_by_priority()
        if not threads:
            logger.info("score_all: no threads to score")
            return

        logger.info("score_all: scoring %d threads", len(threads))

        threads_text = ""
        for t in threads:
            threads_text += f"\nThread '{t['id']}':\n"
            msgs = storage.get_messages_for_thread(t["id"])
            for m in msgs[:10]:  # cap per thread to stay within token budget
                threads_text += (
                    f"  - [{t['workspace']}#{t['channel']}] "
                    f"{m['sender']}: {m['body'][:120]}\n"
                )

        prompt = textwrap.dedent(f"""
            You are a workplace assistant helping prioritise Slack messages.
            Score each conversation thread below for urgency on a scale of 0–10
            (10 = needs immediate attention, 0 = purely informational).

            Consider: direct mentions, action items, deadlines, outages, blockers,
            questions directed at me, and time-sensitive content.

            Respond ONLY with JSON like:
            {{"thread-id-1": 8, "thread-id-2": 3, ...}}

            No explanation. Use the exact thread IDs provided.

            Threads:
            {threads_text}
        """).strip()

        try:
            raw = self._get_llm().ask(prompt)
            parsed = _extract_json(raw)
            if not isinstance(parsed, dict):
                logger.warning("score_all: LLM returned non-dict, aborting")
                return
        except Exception as exc:
            logger.warning("score_all LLM call failed: %s", exc)
            return

        for tid, score in parsed.items():
            try:
                llm_score = max(0.0, min(10.0, float(score)))
                priority = llm_score * _LLM_WEIGHT
                storage.update_thread_priority(tid, priority, llm_score)
            except (TypeError, ValueError):
                pass

        logger.info("score_all: complete")

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _persist(self, bundles: list[MessageBundle]) -> None:
        thread_bundles: dict[str, list[MessageBundle]] = {}
        for b in bundles:
            thread_bundles.setdefault(b.thread_id, []).append(b)

        for tid, msgs in thread_bundles.items():
            latest = max(msgs, key=lambda m: m.timestamp)
            storage.upsert_thread(
                thread_id=tid,
                channel=latest.channel,
                workspace=latest.workspace,
                sender=latest.sender,
                last_body=latest.body[:200],
                nc_group_desc="",
                priority=0.0,
                rule_score=0.0,
                llm_score=0.0,
            )
            for m in msgs:
                storage.upsert_message(
                    msg_id=m.notification_id,
                    thread_id=tid,
                    sender=m.sender,
                    channel=m.channel,
                    body=m.body,
                    timestamp=m.timestamp,
                    notification_id=m.notification_id,
                )

