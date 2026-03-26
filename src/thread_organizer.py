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
        backend_name = cfg.get("llm", {}).get("backend", "copilot")
        self._llm = BackendFactory.get(backend_name)

    def process(self, notifications: list[SlackNotification]) -> None:
        """Process a batch of new notifications end-to-end."""
        if not notifications:
            return

        # Filter already-seen notifications.
        new = [n for n in notifications if not storage.message_exists(n.notification_id)]
        if not new:
            return

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

        # Step 2 — LLM thread clustering.
        bundles = self._cluster_threads(bundles)

        # Step 3 — LLM urgency scoring per thread.
        thread_scores = self._score_threads(bundles)

        # Step 4 — Persist.
        self._persist(bundles, thread_scores)

    # ------------------------------------------------------------------ #
    #  LLM helpers                                                         #
    # ------------------------------------------------------------------ #

    def _cluster_threads(self, bundles: list[MessageBundle]) -> list[MessageBundle]:
        """Ask the LLM to group messages into thread buckets."""
        if len(bundles) == 1:
            b = bundles[0]
            b.thread_id = b.thread_id or self._stable_thread_id(b.channel, b.sender, b.workspace)
            return bundles

        messages_text = "\n".join(
            f"{i}: sender={b.sender!r} workspace={b.workspace!r} channel={b.channel!r} body={b.body!r}"
            for i, b in enumerate(bundles)
        )
        prompt = textwrap.dedent(f"""
            You are a Slack message organiser.
            Below are {len(bundles)} new Slack notifications (indexed 0-based).
            Group them into conversation threads — messages that are likely replies or
            continuations of the same conversation should share a thread_id.

            Respond ONLY with a JSON array like:
            [
              {{"index": 0, "thread_id": "a-short-descriptive-slug"}},
              {{"index": 1, "thread_id": "a-short-descriptive-slug"}},
              ...
            ]

            Rules:
            - Use the same thread_id string for messages in the same conversation.
            - thread_id must be a slug (lowercase, hyphens, no spaces), max 40 chars.
            - If a message stands alone, give it a unique thread_id.
            - Do not include any explanation.

            Messages:
            {messages_text}
        """).strip()

        try:
            raw = self._llm.ask(prompt)
            parsed = _extract_json(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    idx = item.get("index")
                    tid = item.get("thread_id")
                    if idx is not None and tid and 0 <= idx < len(bundles):
                        bundles[idx].thread_id = str(tid)[:40]
        except Exception as exc:
            logger.warning("Thread clustering LLM call failed: %s", exc)

        # Fall back: any unassigned bundle gets its own thread.
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
            raw = self._llm.ask(prompt)
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
    ) -> None:
        # Compute per-thread aggregates.
        thread_bundles: dict[str, list[MessageBundle]] = {}
        for b in bundles:
            thread_bundles.setdefault(b.thread_id, []).append(b)

        for tid, msgs in thread_bundles.items():
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
