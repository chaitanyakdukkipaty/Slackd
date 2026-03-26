"""
Thread organizer — groups incoming Slack notifications into conversation threads
and scores each thread for urgency/priority.

Priority = rule_score + (llm_score × llm_weight)

Rule-based signals (configured in config.yaml):
  - Direct message  → +dm_bonus
  - @mention        → +mention_bonus
  - Urgency keyword → +keyword_bonus each

LLM signals:
  1. Cluster new messages into threads.
  2. Score each thread urgency 0–10.
"""
import json
import logging
import re
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.config import cfg
from src.llm.base import BackendFactory
from src.notification_watcher import SlackNotification
from src import storage

logger = logging.getLogger(__name__)

_SCORING = cfg.get("scoring", {})
_DM_BONUS = _SCORING.get("dm_bonus", 3)
_MENTION_BONUS = _SCORING.get("mention_bonus", 2)
_KEYWORD_BONUS = _SCORING.get("keyword_bonus", 2)
_LLM_WEIGHT = _SCORING.get("llm_weight", 1.0)
_KEYWORDS = [kw.lower() for kw in cfg.get("urgency_keywords", [])]


@dataclass
class MessageBundle:
    notification_id: str
    sender: str
    channel: str
    workspace: str
    body: str
    timestamp: str
    rule_score: float = 0.0
    thread_id: Optional[str] = None


def _compute_rule_score(notif: SlackNotification) -> float:
    score = 0.0
    channel_lower = notif.channel.lower()
    body_lower = notif.body.lower()

    if channel_lower.startswith("dm") or "direct message" in channel_lower:
        score += _DM_BONUS

    if "@" in notif.body or "mentioned you" in body_lower:
        score += _MENTION_BONUS

    for kw in _KEYWORDS:
        if kw in body_lower:
            score += _KEYWORD_BONUS

    return score


def _extract_json(text: str) -> any:
    """Try to pull a JSON object/array out of LLM output that may have prose around it."""
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


class ThreadOrganizer:
    def __init__(self) -> None:
        # LLM backend — lazily instantiated when first needed.
        self._llm = None
        self._llm_backend_name: Optional[str] = None

    def _get_llm(self):
        """Return the LLM backend, re-instantiating if the configured backend changed."""
        backend_name = cfg.get("llm", {}).get("backend", "copilot")
        if self._llm is None or self._llm_backend_name != backend_name:
            self._llm = BackendFactory.get(backend_name)
            self._llm_backend_name = backend_name
        return self._llm

    def process(self, notifications: list[SlackNotification]) -> None:
        """Process a batch of new notifications end-to-end."""
        if not notifications:
            return

        # Filter already-seen notifications.
        new = [n for n in notifications if not storage.message_exists(n.notification_id)]
        if not new:
            return

        no_ai: bool = cfg.get("no_ai", False)

        # Step 1 — rule-based scoring.
        bundles = [
            MessageBundle(
                notification_id=n.notification_id,
                sender=n.sender,
                channel=n.channel,
                workspace=n.workspace,
                body=n.body,
                timestamp=n.timestamp,
                rule_score=_compute_rule_score(n),
            )
            for n in new
        ]

        if no_ai:
            # No-AI: group by channel slug, skip all LLM calls.
            bundles = self._cluster_by_channel(bundles)
            thread_scores: dict[str, float] = {}
        else:
            # Step 2 — LLM thread clustering.
            bundles = self._cluster_threads(bundles)
            # Step 3 — LLM urgency scoring per thread.
            thread_scores = self._score_threads(bundles)

        # Step 4 — Persist.
        self._persist(bundles, thread_scores, no_ai=no_ai)

    # ------------------------------------------------------------------ #
    #  LLM helpers                                                         #
    # ------------------------------------------------------------------ #

    def _cluster_by_channel(self, bundles: list[MessageBundle]) -> list[MessageBundle]:
        """
        No-AI grouping: every message in the same workspace+channel goes into
        one thread. Uses a deterministic slug — no LLM call made.
        """
        for b in bundles:
            b.thread_id = self._channel_thread_id(b.channel, b.workspace)
        return bundles

    @staticmethod
    def _channel_thread_id(channel: str, workspace: str = "") -> str:
        """Deterministic slug based on workspace+channel only (no sender)."""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{workspace}-{channel}".lower()).strip("-")
        return slug[:40] or "general"

    def _cluster_threads(self, bundles: list[MessageBundle]) -> list[MessageBundle]:
        """
        Ask the LLM to group incoming messages into threads.

        Also passes existing thread context so new messages can be merged
        into threads already in the DB rather than always creating new ones.
        """
        # Load recent existing threads as context (cap at 30 to stay within token budget).
        existing_threads = storage.get_threads_by_priority(limit=30)
        existing_context = "\n".join(
            f"  EXISTING id={t['id']!r} channel={t['channel']!r} "
            f"workspace={t['workspace']!r} last_msg={t['last_body'][:80]!r}"
            for t in existing_threads
        )

        def _flags(b: MessageBundle) -> str:
            flags = []
            if b.channel.lower().startswith(("dm", "direct")):
                flags.append("DM")
            if "@" in b.body:
                flags.append("@mention")
            if b.sender.lower().endswith(("bot", "(bot)")):
                flags.append("bot")
            if not b.sender:
                flags.append("automated")
            return ",".join(flags) or "normal"

        messages_text = "\n".join(
            f"{i}: channel={b.channel!r} workspace={b.workspace!r} "
            f"sender={b.sender!r} flags={_flags(b)} "
            f"body={b.body[:120]!r}"
            for i, b in enumerate(bundles)
        )

        existing_section = (
            f"\nExisting threads already in the system (you may reuse their id):\n{existing_context}\n"
            if existing_context else ""
        )

        prompt = textwrap.dedent(f"""
            You are a Slack thread organiser. Your job is to assign each new message
            to a conversation thread — either an EXISTING thread or a NEW one.

            RULES:
            1. If a new message clearly continues an existing thread (same channel,
               same topic, or a direct reply), assign it the EXISTING thread id.
            2. If multiple new messages belong together, give them the SAME new thread_id.
            3. Messages in DIFFERENT channels are NEVER in the same thread.
            4. Automated/bot messages (flags: automated, bot) should each get their
               own thread unless they are clearly the same alert repeating.
            5. Calendar reminders are always standalone threads.
            6. thread_id must be a lowercase hyphenated slug, max 40 chars.
               Make new slugs descriptive, e.g. "preprod-deploy-failure".
            7. Respond ONLY with a JSON array — no explanation.

            Output format:
            [
              {{"index": 0, "thread_id": "existing-or-new-slug"}},
              {{"index": 1, "thread_id": "existing-or-new-slug"}},
              ...
            ]
            {existing_section}
            New messages to classify:
            {messages_text}
        """).strip()

        try:
            raw = self._get_llm().ask(prompt)
            parsed = _extract_json(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    idx = item.get("index")
                    tid = item.get("thread_id")
                    if idx is not None and tid and 0 <= idx < len(bundles):
                        bundles[idx].thread_id = str(tid)[:40]
        except Exception as exc:
            logger.warning("Thread clustering LLM call failed: %s", exc)

        # Fallback: any unassigned bundle gets a deterministic slug.
        for b in bundles:
            if not b.thread_id:
                b.thread_id = self._stable_thread_id(b.channel, b.sender, b.workspace)

        return bundles

    def _score_threads(self, bundles: list[MessageBundle]) -> dict[str, float]:
        """Ask the LLM to score each unique thread 0–10 for urgency."""
        thread_map: dict[str, list[MessageBundle]] = {}
        for b in bundles:
            thread_map.setdefault(b.thread_id, []).append(b)

        threads_text = ""
        for tid, msgs in thread_map.items():
            threads_text += f"\nThread '{tid}':\n"
            for m in msgs:
                threads_text += f"  - [{m.workspace}#{m.channel}] {m.sender}: {m.body}\n"

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

        scores: dict[str, float] = {}
        try:
            raw = self._get_llm().ask(prompt)
            parsed = _extract_json(raw)
            if isinstance(parsed, dict):
                for tid, score in parsed.items():
                    try:
                        scores[tid] = max(0.0, min(10.0, float(score)))
                    except (TypeError, ValueError):
                        pass
        except Exception as exc:
            logger.warning("Urgency scoring LLM call failed: %s", exc)

        return scores

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _persist(
        self,
        bundles: list[MessageBundle],
        thread_scores: dict[str, float],
        no_ai: bool = False,
    ) -> None:
        # Compute per-thread aggregates.
        thread_bundles: dict[str, list[MessageBundle]] = {}
        for b in bundles:
            thread_bundles.setdefault(b.thread_id, []).append(b)

        for tid, msgs in thread_bundles.items():
            if no_ai:
                priority = 0.0
                llm_score = 0.0
                max_rule = 0.0
            else:
                llm_score = thread_scores.get(tid, 0.0)
                max_rule = max(m.rule_score for m in msgs)
                priority = max_rule + (llm_score * _LLM_WEIGHT)
            latest = max(msgs, key=lambda m: m.timestamp)

            storage.upsert_thread(
                thread_id=tid,
                channel=latest.channel,
                workspace=latest.workspace,
                sender=latest.sender,
                last_body=latest.body[:200],
                nc_group_desc=getattr(latest, "nc_group_desc", ""),
                priority=priority,
                rule_score=max_rule,
                llm_score=llm_score,
            )

            for m in msgs:
                storage.upsert_message(
                    msg_id=f"{m.notification_id}",
                    thread_id=tid,
                    sender=m.sender,
                    channel=m.channel,
                    body=m.body,
                    timestamp=m.timestamp,
                    notification_id=m.notification_id,
                )

    @staticmethod
    def _stable_thread_id(channel: str, sender: str, workspace: str = "") -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", f"{workspace}-{channel}-{sender}".lower()).strip("-")
        return slug[:40] or str(uuid.uuid4())[:8]
