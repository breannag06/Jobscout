"""Push notifications through ntfy (https://ntfy.sh).

One summary is sent every day at the scheduled time: how many jobs are waiting
for an application, what is new, and anything that needs attention. Crashes
send their own alert. Messages are published as JSON, which keeps non-ASCII
text intact. Tapping an alert opens the best match.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .http import USER_AGENT, open_url

log = logging.getLogger(__name__)

SEND_ATTEMPTS = 3
# ntfy.sh turns messages over 4096 bytes into file attachments; stay safely under.
MAX_BODY_BYTES = 3800
PLACEHOLDER_TOPICS = frozenset({"", "YOUR_NTFY_TOPIC"})

PRIORITY_DEFAULT = 3
PRIORITY_HIGH = 4


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    click_url: str = ""
    tags: tuple[str, ...] = ()
    priority: int = PRIORITY_DEFAULT


class NtfyNotifier:
    def __init__(self, server: str, topic: str) -> None:
        if topic in PLACEHOLDER_TOPICS:
            raise ValueError("set ntfy_topic in secrets.json before sending alerts")
        self._server = server.rstrip("/")
        self._topic = topic

    def payload(self, message: Message) -> dict[str, object]:
        data: dict[str, object] = {
            "topic": self._topic,
            "title": message.title,
            "message": message.body,
            "priority": message.priority,
        }
        if message.click_url:
            data["click"] = message.click_url
        if message.tags:
            data["tags"] = list(message.tags)
        return data

    def send(self, message: Message, attempts: int = SEND_ATTEMPTS, backoff: float = 10) -> None:
        """Publish ``message``, retrying network errors (for example right after
        the Mac wakes and Wi-Fi is still connecting)."""
        request = urllib.request.Request(
            self._server,
            data=json.dumps(self.payload(message)).encode("utf-8"),
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, attempts + 1):
            try:
                with open_url(request):
                    return
            except (urllib.error.URLError, OSError) as exc:
                if attempt == attempts:
                    raise
                log.warning("sending the alert failed (%s); retrying", exc)
                time.sleep(backoff * attempt)


@dataclass(frozen=True)
class DailySummary:
    """Everything the daily alert reports."""

    to_apply: int  # open matches not yet applied to or skipped
    new_matches: Sequence[sqlite3.Row] = ()  # matches never alerted before
    top_matches: Sequence[sqlite3.Row] = ()  # best open matches; tapping opens the first
    awaiting_answer: int = 0  # opened in the dashboard but not marked
    housing_checks: int = 0  # near misses waiting in the Check housing view
    new_housing_checks: int = 0
    reach_outs_due: Sequence[sqlite3.Row] = ()  # applications to reach out on now
    reach_outs_upcoming: Sequence[sqlite3.Row] = ()  # due within the next week
    problems: Sequence[str] = field(default_factory=tuple)


def _day(value: str) -> str:
    """'Oct 16' from an ISO date or timestamp."""
    try:
        return datetime.fromisoformat(value).strftime("%b %d").replace(" 0", " ")
    except (TypeError, ValueError):
        return ""


def format_reach_out(row: sqlite3.Row) -> str:
    return (
        f"- {row['company']}: {row['title']} "
        f"(applied {_day(row['applied_at'])}, due {_day(row['due_at'])})"
    )


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _badges(row: sqlite3.Row) -> str:
    badges = [str(row["track"]).title()] if row["track"] != "other" else []
    if row["is_internship"]:
        badges.append("internship")
    if row["is_part_time"]:
        badges.append("part-time")
    return f" · {', '.join(badges)}" if badges else ""


def format_match(number: int, row: sqlite3.Row) -> str:
    place = row["location"] or "location n/a"
    via = " via Adzuna" if str(row["id"]).startswith("az-") else ""
    return (
        f"{number}. {row['company']}: {row['title']} ({place}){via} "
        f"[{row['score']}{_badges(row)}]\n{row['apply_url']}"
    )


def build_daily_message(summary: DailySummary) -> Message:
    """The 2:05 pm summary: counts, reach-outs, new matches, then problems."""
    due = summary.reach_outs_due
    title_parts = [f"{_plural(summary.to_apply, 'job')} to apply"]
    if due:
        title_parts.append(f"{_plural(len(due), 'reach-out')} due")
    if summary.new_matches:
        title_parts.append(f"{len(summary.new_matches)} new")
    if summary.problems:
        title_parts.append(_plural(len(summary.problems), "problem"))

    counts = [
        f"Waiting for you to apply: {summary.to_apply}",
        f"Opened but not marked Applied or Skip: {summary.awaiting_answer}",
        f"Reach-outs due (7 days after applying): {len(due)}",
        f"Internships to check for housing: {summary.housing_checks}"
        + (f" ({summary.new_housing_checks} new)" if summary.new_housing_checks else ""),
    ]
    head = ["\n".join(counts)]
    if due:
        head.append(
            "Reach out now, then mark it Followed up:\n"
            + "\n".join(format_reach_out(row) for row in due)
        )
    if summary.reach_outs_upcoming:
        head.append(
            "Reach-outs coming up:\n"
            + "\n".join(format_reach_out(row) for row in summary.reach_outs_upcoming)
        )
    tail = []
    if summary.problems:
        tail.append("Needs attention:\n" + "\n".join(f"- {p}" for p in summary.problems))
    tail.append("Open the dashboard to apply.")
    sections = head + _fit_matches(summary.new_matches, head + tail) + tail

    best = summary.new_matches or summary.top_matches
    return Message(
        title=" · ".join(title_parts),
        body="\n\n".join(sections),
        click_url=best[0]["apply_url"] if best else "",
        tags=("warning",) if summary.problems else ("bell",) if due else ("briefcase",),
        priority=PRIORITY_HIGH if summary.problems or due else PRIORITY_DEFAULT,
    )


def _size(sections: Sequence[str]) -> int:
    return len("\n\n".join(sections).encode("utf-8"))


def _fit_matches(matches: Sequence[sqlite3.Row], others: Sequence[str]) -> list[str]:
    """As many new matches as fit under ``MAX_BODY_BYTES``, then a count of the rest."""
    budget = MAX_BODY_BYTES - _size(others) - 100  # room for the heading and "more" line
    lines: list[str] = []
    for number, row in enumerate(matches, 1):
        entry = format_match(number, row)
        if _size([*lines, entry]) > budget:
            break
        lines.append(entry)
    if not matches:
        return []
    section = "New since the last alert:\n" + "\n\n".join(lines)
    if len(lines) < len(matches):
        section += f"\n\n...and {len(matches) - len(lines)} more in the dashboard."
    return [section]


def build_failure_message(error: str, log_path: str = "") -> Message:
    """Sent when the daily run crashes, so a silent failure cannot go unnoticed."""
    where = f" Details are in {log_path}." if log_path else ""
    return Message(
        title="Job finder failed",
        body=f"Today's run did not finish: {error}{where}",
        tags=("rotating_light",),
        priority=PRIORITY_HIGH,
    )
