"""Local dashboard for working through matches and applying quickly.

Run with ``python3 dashboard.py``. It only listens on 127.0.0.1, and every
form carries a per-process token so other websites cannot change your data.
"""

from __future__ import annotations

import secrets
import socket
import sqlite3
import threading
import webbrowser
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from .config import ConfigError, load_config
from .profile import Profile, ProfileError, load_profile
from .storage import REACH_OUT, STATUSES, VIEWS, JobStore, manual_id, reasons_of, utc_now
from .tracker import render as render_tracker

HOST, PORT = "127.0.0.1", 5050
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FOLLOW_UP_DAYS = 7

VIEW_LABELS = {
    "new": "New",
    "housing": "Check housing",
    REACH_OUT: "Reach out",
    "applied": "Applied",
    "skip": "Skipped",
    "closed": "Closed",
    "all": "All",
}

EMPTY_MESSAGES = {
    "new": "You're all caught up. New jobs show up every few hours.",
    "housing": "Nothing to check right now.",
    REACH_OUT: "No reach-outs due. Nice.",
    "applied": "Nothing marked applied yet.",
    "skip": "Nothing skipped.",
    "closed": "Nothing closed.",
    "all": "No jobs yet.",
}

# Score thresholds for the plain-language fit label shown on each card.
FIT_LABELS = ((40, "Great fit"), (25, "Good fit"), (12, "Worth a look"))
_ACRONYMS = frozenset({"sdr", "bdr", "gtm", "sql", "ai", "llm", "crm", "ux", "a/b"})


def fit_label(score: int) -> str:
    return next((label for floor, label in FIT_LABELS if score >= floor), "Long shot")


def _words(term: str) -> str:
    return " ".join(word.upper() if word in _ACRONYMS else word for word in term.split())


def explain(reasons: Sequence[str]) -> str:
    """Turn score reasons ("title: business development +16", "sql +3") into a short
    sentence: "business development role, internship, mentions SQL"."""
    titles: list[str] = []
    others: list[str] = []
    cautions: list[str] = []
    for reason in reasons:
        label, _, weight = reason.rpartition(" ")
        try:
            value = int(weight)
        except ValueError:
            continue
        if value < 0:
            cautions.append(_words(label))
        elif label.startswith("title: "):
            phrase = _words(label.removeprefix("title: "))
            # Keep the most specific wording: "sales development" over "sales".
            titles = [kept for kept in titles if kept not in phrase]
            if not any(phrase in kept for kept in titles):
                titles.append(phrase)
        elif label in {"internship", "part-time"}:
            others.append(label)
        else:
            others.append(f"mentions {_words(label)}")
    # Drop mentions the role or the internship tag already says.
    said = " ".join(titles[:1])
    covered = {"mentions intern", "mentions internship"} if "internship" in others else set()
    others = [o for o in others if o not in covered and o.removeprefix("mentions ") not in said]
    parts = [f"{title} role" for title in titles[:1]] + others
    sentence = ", ".join(parts[:3])
    if cautions:
        sentence += f"{'; ' if sentence else ''}heads up, it mentions {', '.join(cautions[:2])}"
    return sentence


def safe_url(url: str | None) -> str:
    """Only allow web links so a bad posting URL can never run script."""
    return url if url and urlparse(url).scheme in {"http", "https"} else "#"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age(value: str | None, now: datetime) -> str:
    """Human-friendly age of an ISO date or timestamp: "today", "3 days ago"."""
    moment = parse_time(value)
    if moment is None:
        return ""
    days = (now.date() - moment.date()).days
    if days <= 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def _card(
    row: sqlite3.Row, profile: Profile, follow_up: timedelta, now: datetime
) -> dict[str, Any]:
    applied = parse_time(row["applied_at"])
    reach_out_by = applied + follow_up if applied else None
    return {
        **dict(row),
        "apply_url": safe_url(row["apply_url"]),
        "url_safe": safe_url(row["url"]),
        "why": explain(reasons_of(row)),
        "fit": fit_label(int(row["score"] or 0)),
        "posted": age(row["posted_at"] or row["first_seen"], now),
        "applied_ago": age(row["applied_at"], now),
        "awaiting_answer": bool(row["opened_at"]) and row["status"] == "new",
        "follow_up_due": row["status"] == "applied"
        and reach_out_by is not None
        and reach_out_by <= now,
        "reach_out_by": f"{reach_out_by:%b %d}" if reach_out_by else "",
        "resume": Path(resume).name if (resume := profile.resume_for(row["track"])) else "",
        "track_label": "" if row["track"] == "other" else str(row["track"]).title(),
    }


def create_app(
    db_path: Path,
    profile_path: Path,
    follow_up_days: int = DEFAULT_FOLLOW_UP_DAYS,
    timezone_name: str = "America/Denver",
) -> Flask:
    """Build the dashboard. Run it with a single process: the form token lives in memory."""
    app = Flask(__name__)
    token = secrets.token_urlsafe(32)
    follow_up = timedelta(days=follow_up_days)

    zone = ZoneInfo(timezone_name)

    @app.context_processor
    def inject() -> dict[str, Any]:
        today = datetime.now(zone)
        return {
            "csrf_token": token,
            "view_labels": VIEW_LABELS,
            "today": f"{today:%A, %B} {today.day}",
        }

    @app.before_request
    def check_token() -> None:
        if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf", ""), token
        ):
            abort(403)

    def back(default: str = "new") -> Response:
        view = request.form.get("view", default)
        return redirect(url_for("index", view=view if view in VIEWS else default))

    @app.get("/")
    def index() -> str:
        view = request.args.get("view", "new")
        if view not in VIEWS:
            view = "new"
        profile, profile_error = _profile(profile_path)
        now = utc_now()
        with JobStore(db_path) as store:
            reach_outs = store.reach_outs(follow_up, now=now)
            rows = reach_outs if view == REACH_OUT else store.list_jobs(view)
            cards = [_card(row, profile, follow_up, now) for row in rows]
            counts = {**store.view_counts(), REACH_OUT: len(reach_outs)}
            due = len(reach_outs)
            awaiting = store.awaiting_answer_count()
        return render_template(
            "index.html",
            view=view,
            cards=cards,
            counts=counts,
            reach_outs_due=due,
            empty_message=EMPTY_MESSAGES[view],
            awaiting=awaiting,
            statuses=STATUSES,
            profile_error=profile_error,
        )

    @app.get("/open/<job_id>")
    def open_job(job_id: str) -> Response:
        """Record that the Apply link was opened, then send the browser to it."""
        with JobStore(db_path) as store:
            row = store.get(job_id)
            if row is None:
                abort(404)
            store.record_opened(job_id)
        return redirect(safe_url(row["apply_url"]))

    @app.post("/status")
    def set_status() -> Response:
        job_id, status = request.form.get("job_id", ""), request.form.get("status", "")
        if status not in STATUSES or not job_id:
            abort(400)
        with JobStore(db_path) as store:
            if not store.set_status(job_id, status):
                abort(404)
            if status == "follow_up":  # confirmed: move it to the tracker right away
                store.archive_applications()
        return back()

    @app.post("/notes")
    def set_notes() -> Response:
        job_id = request.form.get("job_id", "")
        with JobStore(db_path) as store:
            if not job_id or store.get(job_id) is None:
                abort(404)
            store.set_notes(job_id, request.form.get("notes", "").strip())
        return back()

    @app.post("/add")
    def add_job() -> Response:
        url = request.form.get("url", "").strip()
        title = request.form.get("title", "").strip()
        company = request.form.get("company", "").strip()
        if safe_url(url) == "#" or not title or not company:
            abort(400)
        with JobStore(db_path) as store:
            store.add_manual(manual_id(url), company, title, url)
        return redirect(url_for("index", view="new"))

    @app.get("/tracker.pdf")
    def tracker() -> Response:
        """The Application Tracker, opened in the browser's PDF viewer."""
        with JobStore(db_path) as store:
            pdf = render_tracker(store.applications(), follow_up)
        return Response(
            pdf,
            mimetype="application/pdf",
            headers={"Content-Disposition": 'inline; filename="application_tracker.pdf"'},
        )

    @app.get("/kit")
    def kit() -> str:
        profile, profile_error = _profile(profile_path)
        return render_template(
            "kit.html", profile=profile, profile_error=profile_error, profile_path=profile_path
        )

    return app


def _profile(path: Path) -> tuple[Profile, str]:
    try:
        return load_profile(path), ""
    except ProfileError as exc:
        return Profile(), str(exc)


def is_running(host: str = HOST, port: int = PORT) -> bool:
    """True when something is already listening on the dashboard's address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def create_default_app() -> Flask:
    """The dashboard for this project's own config.json, jobs.db, and profile.json."""
    try:
        config = load_config(BASE_DIR / "config.json")
    except ConfigError:
        return create_app(BASE_DIR / "jobs.db", BASE_DIR / "profile.json")
    return create_app(
        BASE_DIR / "jobs.db",
        BASE_DIR / "profile.json",
        config.follow_up_after_days,
        config.timezone,
    )


def main() -> None:
    """Run the dashboard on this computer (for working offline from a local copy)."""
    url = f"http://{HOST}:{PORT}"
    if is_running():
        print(f"The dashboard is already running. Opening {url}")
        webbrowser.open(url)
        return
    app = create_default_app()
    print(f"Job dashboard: {url}\nIt opens in your browser. Keep this window open while you")
    print("apply, and press Ctrl+C here when you are done.")
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=HOST, port=PORT, debug=False)
