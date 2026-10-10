"""SQLite persistence for postings and the application workflow.

The ``jobs`` table layout is fixed: its columns must not change. Everything
added since lives in side tables keyed by job id:

- ``job_details``: why a posting matched, its apply link, whether it has
  closed or stopped matching the filters, and whether it is a near miss
  (an internship whose posting does not mention housing or a stipend).
- ``application_log``: when each posting was first marked applied.
- ``job_activity``: when the Apply link was opened, and notes. (Its
  ``reminded_at`` column is from an earlier version and is no longer used.)
- ``archive``: postings that left the dashboard. Applications I confirmed I
  followed up on are kept here, in a few hundred bytes each, for the PDF
  tracker; untouched postings are remembered only so later scans do not add
  them back.

The dashboard and the daily run can use the database at the same time, so it
runs in write-ahead-log mode with a busy timeout.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Evaluation, Job

MANUAL_SCORE = 100  # hand-added jobs sort to the top of the dashboard
MANUAL_PREFIX = "manual-"
AGGREGATOR_PREFIX = "az-"  # search-engine postings: never closed by a company board's absence
STATUSES = ("new", "applied", "follow_up", "skip")
REACH_OUT = "reach_out"  # applications whose reach-out is due; depends on the clock
VIEWS = ("new", "housing", REACH_OUT, "applied", "skip", "closed", "all")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    company    TEXT,
    title      TEXT,
    location   TEXT,
    url        TEXT,
    score      INTEGER,
    first_seen TEXT,
    status     TEXT DEFAULT 'new',
    alerted    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_details (
    job_id          TEXT PRIMARY KEY,
    track           TEXT,
    is_internship   INTEGER DEFAULT 0,
    is_part_time    INTEGER DEFAULT 0,
    reasons         TEXT DEFAULT '[]',
    apply_url       TEXT DEFAULT '',
    posted_at       TEXT DEFAULT '',
    last_seen       TEXT,
    closed_at       TEXT,
    rejected_reason TEXT,
    near_miss       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS application_log (
    job_id     TEXT PRIMARY KEY,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS job_activity (
    job_id      TEXT PRIMARY KEY,
    opened_at   TEXT,
    notes       TEXT DEFAULT '',
    reminded_at TEXT
);
CREATE TABLE IF NOT EXISTS archive (
    job_id      TEXT PRIMARY KEY,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    url         TEXT,
    first_seen  TEXT,
    status      TEXT,
    applied_at  TEXT,
    reminded_at TEXT,
    notes       TEXT,
    reason      TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs (company);
"""

_STATUS = "COALESCE(NULLIF(j.status, ''), 'new')"
_OPEN = "d.closed_at IS NULL AND d.rejected_reason IS NULL"
_NEAR_MISS = "d.closed_at IS NULL AND COALESCE(d.near_miss, 0) = 1"

_VIEW_FILTERS = {
    "new": f"{_STATUS} = 'new' AND {_OPEN}",
    "housing": f"{_STATUS} = 'new' AND {_NEAR_MISS}",
    "applied": f"{_STATUS} = 'applied'",
    "skip": f"{_STATUS} = 'skip'",
    "closed": f"{_STATUS} = 'new' AND NOT ({_OPEN}) AND NOT ({_NEAR_MISS})",
    "all": "1 = 1",
}

_SELECT_JOBS = f"""
SELECT j.id, j.company, j.title, j.location, j.url, j.score, j.first_seen,
       {_STATUS} AS status,
       COALESCE(NULLIF(d.apply_url, ''), j.url) AS apply_url,
       COALESCE(d.track, 'other') AS track,
       COALESCE(d.is_internship, 0) AS is_internship,
       COALESCE(d.is_part_time, 0) AS is_part_time,
       COALESCE(d.reasons, '[]') AS reasons,
       COALESCE(d.posted_at, '') AS posted_at,
       d.closed_at, d.rejected_reason, COALESCE(d.near_miss, 0) AS near_miss,
       l.applied_at, a.opened_at, COALESCE(a.notes, '') AS notes
FROM jobs j
LEFT JOIN job_details d ON d.job_id = j.id
LEFT JOIN application_log l ON l.job_id = j.id
LEFT JOIN job_activity a ON a.job_id = j.id
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or utc_now()).isoformat(timespec="seconds")


ARCHIVE_APPLIED = "application"  # applied and confirmed followed up: kept for the tracker
ARCHIVE_UNTOUCHED = "untouched"  # never opened: kept only to stay dismissed
_BACKUP_PREFIX = "jobs-"


@dataclass(frozen=True)
class SyncResult:
    """What one run changed in the database."""

    added: int = 0
    rescored: int = 0
    closed: int = 0
    dropped: int = 0
    near_misses_added: int = 0


class JobStore:
    """Repository for postings and their application status. Use as a context manager."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- Syncing a run's results -------------------------------------------------

    def sync(
        self,
        evaluated: Iterable[tuple[Job, Evaluation]],
        fetched_companies: Collection[str],
        now: datetime | None = None,
        *,
        complete_companies: Collection[str] | None = None,
        title_rejection: Callable[[str], str] | None = None,
    ) -> SyncResult:
        """Record one run: every posting from the companies fetched successfully.

        New matches and near misses are inserted and existing ones re-scored
        with the current rules. Stored postings that now fail the filters are
        flagged with the reason. A stored posting missing from this run is
        flagged with ``title_rejection(title)`` when its title now fails, and
        otherwise flagged closed, but only for ``complete_companies``: boards
        that return every posting (defaults to all fetched companies). Search-
        based boards such as Workday omit postings that are still open.
        Companies whose fetch failed are left untouched.
        """
        stamp = _stamp(now)
        archived = self._archived_ids()
        pairs = [(job, verdict) for job, verdict in evaluated if job.id not in archived]
        previous = self._details_for_companies(fetched_companies)
        complete = set(fetched_companies if complete_companies is None else complete_companies)
        added = rescored = dropped = closed = near_misses_added = 0

        with self._conn:
            for job, verdict in pairs:
                if verdict.passed or verdict.near_miss:
                    if self._insert_job(job, verdict.score, stamp):
                        if verdict.near_miss:
                            near_misses_added += 1
                        else:
                            added += 1
                    else:
                        rescored += self._conn.execute(
                            "UPDATE jobs SET score = ? WHERE id = ? AND score IS NOT ?",
                            (verdict.score, job.id, verdict.score),
                        ).rowcount
                        self._refresh_posting(job)
                    self._save_match(job, verdict, stamp)
                elif job.id in previous:
                    was_open = previous[job.id]["rejected_reason"] is None
                    self._save_rejection(job.id, verdict.rejection, stamp)
                    dropped += int(was_open and previous[job.id]["status"] == "new")

            seen = {job.id for job, _ in pairs}
            for job_id, row in previous.items():
                if job_id in seen:
                    continue
                reason = title_rejection(row["title"] or "") if title_rejection else ""
                if reason:
                    dropped += int(row["rejected_reason"] is None and row["status"] == "new")
                    self._save_rejection(job_id, reason, stamp)
                    continue
                if row["company"] not in complete:
                    continue
                if row["closed_at"] is None:
                    closed += 1
                self._conn.execute(
                    "INSERT INTO job_details (job_id, closed_at) VALUES (?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET "
                    "closed_at = COALESCE(job_details.closed_at, excluded.closed_at)",
                    (job_id, stamp),
                )
        return SyncResult(
            added=added,
            rescored=rescored,
            closed=closed,
            dropped=dropped,
            near_misses_added=near_misses_added,
        )

    def _archived_ids(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT job_id FROM archive")}

    def _details_for_companies(self, companies: Collection[str]) -> dict[str, sqlite3.Row]:
        if not companies:
            return {}
        marks = ",".join("?" * len(companies))
        rows = self._conn.execute(
            f"SELECT j.id, j.title, j.company, {_STATUS} AS status, d.closed_at, "
            f"d.rejected_reason FROM jobs j "
            f"LEFT JOIN job_details d ON d.job_id = j.id "
            f"WHERE j.company IN ({marks}) AND j.id NOT LIKE ? AND j.id NOT LIKE ?",
            (*companies, f"{MANUAL_PREFIX}%", f"{AGGREGATOR_PREFIX}%"),
        ).fetchall()
        return {row["id"]: row for row in rows}

    def _insert_job(self, job: Job, score: int, stamp: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO jobs (id, company, title, location, url, score, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job.id, job.company, job.title, job.location, job.url, score, stamp),
        )
        return cursor.rowcount > 0

    def _refresh_posting(self, job: Job) -> None:
        """Keep the title, location, and link current when a company edits a posting."""
        self._conn.execute(
            "UPDATE jobs SET title = ?, location = ?, url = ? WHERE id = ? "
            "AND (title IS NOT ? OR location IS NOT ? OR url IS NOT ?)",
            (job.title, job.location, job.url, job.id, job.title, job.location, job.url),
        )

    def _save_match(self, job: Job, verdict: Evaluation, stamp: str) -> None:
        """Store a match or near miss. Near misses keep their reason so they stay out
        of alerts and the New view."""
        self._conn.execute(
            "INSERT INTO job_details (job_id, track, is_internship, is_part_time, reasons, "
            "apply_url, posted_at, last_seen, closed_at, rejected_reason, near_miss) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET track = excluded.track, "
            "is_internship = excluded.is_internship, is_part_time = excluded.is_part_time, "
            "reasons = excluded.reasons, apply_url = excluded.apply_url, "
            "posted_at = excluded.posted_at, last_seen = excluded.last_seen, "
            "closed_at = NULL, rejected_reason = excluded.rejected_reason, "
            "near_miss = excluded.near_miss",
            (
                job.id,
                verdict.track,
                int(verdict.internship),
                int(verdict.part_time),
                json.dumps(list(verdict.reasons)),
                job.apply_url or job.url,
                job.posted_at,
                stamp,
                verdict.rejection or None,
                int(verdict.near_miss),
            ),
        )

    def _save_rejection(self, job_id: str, reason: str, stamp: str) -> None:
        self._conn.execute(
            "INSERT INTO job_details (job_id, last_seen, rejected_reason) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET last_seen = excluded.last_seen, "
            "closed_at = NULL, rejected_reason = excluded.rejected_reason, near_miss = 0",
            (job_id, stamp, reason),
        )

    # ---- Cleanup, archive, and backups -----------------------------------------------

    def archive_applications(self, now: datetime | None = None) -> int:
        """Move applications I marked followed up off the dashboard and out of the
        active tables. A small record stays in the archive for the PDF tracker."""
        with self._conn:
            return self._archive_where(f"{_STATUS} = 'follow_up'", (), ARCHIVE_APPLIED, _stamp(now))

    def compact(self) -> None:
        """Reclaim the space left by removed and archived postings."""
        self._conn.execute("VACUUM")

    def remove_untouched(self, after: timedelta, now: datetime | None = None) -> int:
        """Remove postings still marked new ``after`` they were found that were never
        opened and have no notes. Hand-added postings are kept. Removed postings are
        remembered so later scans do not add them back."""
        moment = now or utc_now()
        condition = (
            f"{_STATUS} = 'new' AND a.opened_at IS NULL AND COALESCE(a.notes, '') = '' "
            "AND j.first_seen <= ? AND j.id NOT LIKE ?"
        )
        params = (_stamp(moment - after), f"{MANUAL_PREFIX}%")
        with self._conn:
            return self._archive_where(condition, params, ARCHIVE_UNTOUCHED, _stamp(moment))

    def _archive_where(
        self, condition: str, params: tuple[str, ...], reason: str, stamp: str
    ) -> int:
        ids = [
            row["id"]
            for row in self._conn.execute(
                "SELECT j.id FROM jobs j LEFT JOIN application_log l ON l.job_id = j.id "
                f"LEFT JOIN job_activity a ON a.job_id = j.id WHERE {condition}",
                params,
            )
        ]
        for job_id in ids:
            self._conn.execute(
                "INSERT OR REPLACE INTO archive (job_id, company, title, location, url, "
                "first_seen, status, applied_at, reminded_at, notes, reason, archived_at) "
                f"SELECT j.id, j.company, j.title, j.location, j.url, j.first_seen, {_STATUS}, "
                "l.applied_at, a.reminded_at, COALESCE(a.notes, ''), ?, ? FROM jobs j "
                "LEFT JOIN application_log l ON l.job_id = j.id "
                "LEFT JOIN job_activity a ON a.job_id = j.id WHERE j.id = ?",
                (reason, stamp, job_id),
            )
            for table, column in (
                ("jobs", "id"),
                ("job_details", "job_id"),
                ("job_activity", "job_id"),
                ("application_log", "job_id"),
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (job_id,))
        return len(ids)

    def applications(self) -> list[sqlite3.Row]:
        """Every application, archived or still on the dashboard, newest first."""
        return self._conn.execute(
            f"SELECT j.company, j.title, j.location, COALESCE(NULLIF(d.apply_url, ''), j.url) "
            f"AS url, {_STATUS} AS status, l.applied_at, COALESCE(a.notes, '') AS notes, "
            "NULL AS archived_at FROM jobs j "
            "LEFT JOIN job_details d ON d.job_id = j.id "
            "LEFT JOIN application_log l ON l.job_id = j.id "
            "LEFT JOIN job_activity a ON a.job_id = j.id "
            f"WHERE {_STATUS} IN ('applied', 'follow_up') "
            "UNION ALL SELECT company, title, location, url, status, applied_at, notes, "
            "archived_at FROM archive WHERE reason = ? "
            "ORDER BY applied_at DESC",
            (ARCHIVE_APPLIED,),
        ).fetchall()

    def backup(self, directory: Path, keep: int = 14, now: datetime | None = None) -> Path:
        """Write a consistent copy of the database and keep only the newest ``keep``."""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{_BACKUP_PREFIX}{(now or utc_now()).date().isoformat()}.db"
        with sqlite3.connect(str(target)) as copy:
            self._conn.backup(copy)
        copy.close()
        for old in sorted(directory.glob(f"{_BACKUP_PREFIX}*.db"))[:-keep]:
            old.unlink()
        return target

    # ---- Alerts and reminders ------------------------------------------------------

    def pending_alerts(self, limit: int) -> list[sqlite3.Row]:
        """Highest-scoring open postings that have not been sent yet."""
        return self._conn.execute(
            f"{_SELECT_JOBS} WHERE j.alerted = 0 AND {_VIEW_FILTERS['new']} "
            "ORDER BY j.score DESC, j.first_seen ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_alerted(self, job_ids: Iterable[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "UPDATE jobs SET alerted = 1 WHERE id = ?", [(i,) for i in job_ids]
            )

    def reach_outs(
        self, after: timedelta, within: timedelta = timedelta(0), now: datetime | None = None
    ) -> list[sqlite3.Row]:
        """Applications still waiting for a reach-out that is due by ``now + within``.

        A reach-out is due ``after`` the apply date and stays listed every day until
        the posting is marked followed up. Rows carry ``due_at`` (UTC), oldest first.
        """
        offset = f"+{int(after.total_seconds())} seconds"
        horizon = _stamp((now or utc_now()) + within)
        return self._conn.execute(
            f"SELECT q.*, datetime(q.applied_at, ?) AS due_at FROM ({_SELECT_JOBS} "
            f"WHERE {_STATUS} = 'applied' AND l.applied_at IS NOT NULL) q "
            "WHERE datetime(q.applied_at, ?) <= datetime(?) ORDER BY q.applied_at",
            (offset, offset, horizon),
        ).fetchall()

    # ---- Application workflow ------------------------------------------------------

    def get(self, job_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            f"{_SELECT_JOBS} WHERE j.id = ?", (job_id,)
        ).fetchone()
        return row

    def list_jobs(self, view: str) -> list[sqlite3.Row]:
        """Postings for a dashboard view. Opened-but-unanswered postings come first."""
        if view not in _VIEW_FILTERS:
            raise ValueError(f"unknown view {view!r}; expected one of {VIEWS}")
        return self._conn.execute(
            f"{_SELECT_JOBS} WHERE {_VIEW_FILTERS[view]} "
            "ORDER BY (a.opened_at IS NOT NULL AND l.applied_at IS NULL) DESC, "
            "j.score DESC, j.first_seen DESC"
        ).fetchall()

    def view_counts(self) -> dict[str, int]:
        select = ", ".join(
            f'SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS "{view}"'
            for view, condition in _VIEW_FILTERS.items()
        )
        row = self._conn.execute(
            f"SELECT {select} FROM jobs j LEFT JOIN job_details d ON d.job_id = j.id"
        ).fetchone()
        return {view: int(row[view] or 0) for view in _VIEW_FILTERS}

    def awaiting_answer_count(self) -> int:
        """Open postings whose Apply link was opened but that are still marked new."""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM jobs j LEFT JOIN job_details d ON d.job_id = j.id "
            f"JOIN job_activity a ON a.job_id = j.id "
            f"WHERE a.opened_at IS NOT NULL AND {_VIEW_FILTERS['new']}"
        ).fetchone()
        return int(row[0])

    def set_status(self, job_id: str, status: str, now: datetime | None = None) -> bool:
        """Update a posting's status. Returns False if the id does not exist.

        Marking a posting applied records the date the first time, which drives
        follow-up reminders.
        """
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self._conn:
            updated = self._conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
            ).rowcount
            if updated and status == "applied":
                self._conn.execute(
                    "INSERT OR IGNORE INTO application_log (job_id, applied_at) VALUES (?, ?)",
                    (job_id, _stamp(now)),
                )
        return updated > 0

    def record_opened(self, job_id: str, now: datetime | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO job_activity (job_id, opened_at) VALUES (?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET opened_at = excluded.opened_at",
                (job_id, _stamp(now)),
            )

    def set_notes(self, job_id: str, notes: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO job_activity (job_id, notes) VALUES (?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET notes = excluded.notes",
                (job_id, notes),
            )

    def add_manual(
        self, job_id: str, company: str, title: str, url: str, now: datetime | None = None
    ) -> bool:
        """Track a posting found by hand (for example on Handshake or LinkedIn)."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO jobs "
                "(id, company, title, location, url, score, first_seen, alerted) "
                "VALUES (?, ?, ?, '', ?, ?, ?, 1)",
                (job_id, company, title, url, MANUAL_SCORE, _stamp(now)),
            )
        return cursor.rowcount > 0


def manual_id(url: str) -> str:
    """Stable id for a hand-added posting, so adding the same link twice is a no-op."""
    return MANUAL_PREFIX + hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


def reasons_of(row: sqlite3.Row) -> Sequence[str]:
    """Decode the stored score reasons of a row from ``list_jobs`` or ``get``."""
    try:
        value = json.loads(row["reasons"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
