"""Command line interface for the job finder."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
from datetime import timedelta
from pathlib import Path

from .config import SECRETS_FILE, Config, ConfigError, load_config, load_secrets
from .filters import JobFilter
from .http import wait_for_network
from .notify import (
    DailySummary,
    NtfyNotifier,
    build_daily_message,
    build_failure_message,
)
from .pipeline import RunReport, evaluate, fetch_options, run
from .sources import FetchResult, map_fetch
from .storage import STATUSES, JobStore, manual_id, utc_now
from .tracker import render as render_tracker

log = logging.getLogger("jobscout")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
DEFAULT_DB = BASE_DIR / "jobs.db"
_MAX_NAMES = 4
BACKUP_DIR = "backups"
REACH_OUT_PREVIEW = timedelta(days=7)  # list reach-outs coming up within a week


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job_finder.py",
        description="Find internships, part-time student jobs, and entry-level roles.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config.json")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to the SQLite database")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    parser.add_argument(
        "--only",
        action="append",
        metavar="COMPANY",
        help="with --check: test only this company (repeatable)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="test every company feed and report problems"
    )
    mode.add_argument(
        "--dry", action="store_true", help="run normally but print the alert instead of sending it"
    )
    mode.add_argument(
        "--scan", action="store_true", help="fetch and record new postings without sending an alert"
    )
    mode.add_argument(
        "--tracker",
        metavar="PDF",
        help="write the Application Tracker PDF to this path ('-' for standard output)",
    )
    mode.add_argument(
        "--mark",
        nargs=2,
        metavar=("JOB_ID", "STATUS"),
        help=f"set a posting's status ({', '.join(STATUSES)})",
    )
    mode.add_argument(
        "--add",
        nargs=3,
        metavar=("URL", "TITLE", "COMPANY"),
        help="track a posting you found by hand (Handshake, LinkedIn, a career page)",
    )
    return parser


def check_feeds(config: Config, only: list[str] | None = None) -> int:
    """Fetch every configured company (or just ``only``) once and print a health report."""
    job_filter = JobFilter(config)
    wanted = {name.lower() for name in only or []}
    companies = [c for c in config.companies if not wanted or c.name.lower() in wanted]
    if not companies:
        print(f"No company named {', '.join(only or [])} in config.json.", file=sys.stderr)
        return 2
    logging.getLogger("jobscout.sources").setLevel(logging.ERROR)  # FAIL lines say it already

    def summarize(result: FetchResult) -> str:
        if not result.ok:
            return f"FAIL  {result.company.name} ({result.company.token}): {result.error}"
        matches = sum(verdict.passed for _, verdict in evaluate([result], job_filter))
        return f"OK    {result.company.name}: {len(result.jobs)} postings, {matches} match filters"

    lines = map_fetch(companies, fetch_options(config, job_filter), summarize)
    print("\n".join(lines))
    failures = sum(line.startswith("FAIL") for line in lines)
    print(f"\n{len(lines) - failures} OK, {failures} failed.")
    return 1 if failures else 0


def _names(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_NAMES])
    return shown + (f", and {len(items) - _MAX_NAMES} more" if len(items) > _MAX_NAMES else "")


def find_problems(report: RunReport) -> list[str]:
    """Things that need fixing, phrased as instructions for the alert."""
    problems = []
    needs_keys = [r.company.name for r in report.failures if "secrets.json" in r.error]
    if needs_keys:
        problems.append(
            f"{_names(needs_keys)} is not set up yet: add your App ID and App Key to secrets.json."
        )
    failed = [
        result.company.name for result in report.failures if result.company.name not in needs_keys
    ]
    if failed and len(failed) == len(report.results):
        problems.append("Every company feed failed. Check the internet connection.")
    elif failed:
        problems.append(
            f"{len(failed)} company feed{'s' if len(failed) != 1 else ''} failed: "
            f"{_names(failed)}. Run python3 job_finder.py --check, then fix or remove "
            "them in config.json."
        )
    return problems


def scan(config: Config, db_path: Path) -> int:
    """Fetch and record postings between daily alerts so the dashboard stays fresh."""
    with JobStore(db_path) as store:
        report = run(config, store, include_aggregators=False)
        removed = store.remove_untouched(timedelta(days=config.remove_untouched_after_days))
    print(f"{report.summary()} Removed {removed} untouched postings.")
    return 0


def run_daily(config: Config, db_path: Path, dry: bool) -> int:
    """Fetch, record, and send the daily summary (or print it with ``dry``).

    A real run backs up the database first and removes untouched postings before
    the counts are taken, so the alert matches the dashboard. Every reach-out that
    is due is listed until I mark it followed up.
    """
    if not dry and not wait_for_network(config.ntfy_server):
        log.warning("network still unreachable; running anyway")

    with JobStore(db_path) as store:
        if not dry:
            store.backup(db_path.parent / BACKUP_DIR)
        report = run(config, store)
        print(report.summary())
        if not dry:
            removed = store.remove_untouched(timedelta(days=config.remove_untouched_after_days))
            print(f"Removed {removed} untouched postings.")
        new_matches = store.pending_alerts(config.top_n)
        follow_up = timedelta(days=config.follow_up_after_days)
        now = utc_now()
        reach_outs = store.reach_outs(follow_up, REACH_OUT_PREVIEW, now)
        due_cutoff = now.strftime("%Y-%m-%d %H:%M:%S")  # due_at is UTC "YYYY-MM-DD HH:MM:SS"
        summary = DailySummary(
            to_apply=store.view_counts()["new"],
            new_matches=new_matches,
            top_matches=store.list_jobs("new")[: config.top_n],
            awaiting_answer=store.awaiting_answer_count(),
            housing_checks=store.view_counts()["housing"],
            new_housing_checks=report.sync.near_misses_added,
            reach_outs_due=[row for row in reach_outs if row["due_at"] <= due_cutoff],
            reach_outs_upcoming=[row for row in reach_outs if row["due_at"] > due_cutoff],
            problems=find_problems(report),
        )
        message = build_daily_message(summary)
        if dry:
            print(f"\n{message.title}\n\n{message.body}")
            return 0

        try:
            NtfyNotifier(config.ntfy_server, config.ntfy_topic).send(message)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Could not send the alert: {exc}", file=sys.stderr)
            return 1
        store.mark_alerted(row["id"] for row in new_matches)
        store.archive_applications()  # anything marked followed up outside the dashboard
        store.compact()
        print(f"Sent: {message.title}.")
    return 0


def write_tracker(config_path: Path, db_path: Path, target: str) -> int:
    try:
        follow_up_days = load_config(config_path).follow_up_after_days
    except ConfigError:
        follow_up_days = 7
    with JobStore(db_path) as store:
        pdf = render_tracker(store.applications(), timedelta(days=follow_up_days))
    if target == "-":
        sys.stdout.buffer.write(pdf)
        sys.stdout.buffer.flush()
    else:
        Path(target).write_bytes(pdf)
        print(f"Wrote {target}")
    return 0


def notify_failure(config_path: Path, error: str) -> bool:
    """Best-effort failure alert. Reads the ntfy settings straight from the JSON files so
    it works even when the rest of config.json is invalid. Returns True if it was sent."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        secrets = load_secrets(config_path.parent / SECRETS_FILE)
        topic = os.environ.get("NTFY_TOPIC") or secrets.get("ntfy_topic") or raw.get("ntfy_topic")
        notifier = NtfyNotifier(str(raw.get("ntfy_server", "https://ntfy.sh")), str(topic or ""))
        notifier.send(build_failure_message(error))
    except Exception:  # never let the failure alert hide the original error
        log.exception("could not send the failure alert")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.mark:
        job_id, status = args.mark
        if status not in STATUSES:
            print(f"Status must be one of: {', '.join(STATUSES)}", file=sys.stderr)
            return 2
        with JobStore(args.db) as store:
            if not store.set_status(job_id, status):
                print(f"No job with id {job_id!r}", file=sys.stderr)
                return 1
            if status == "follow_up":
                store.archive_applications()
                print("Moved it to the Application Tracker.")
        print(f"Marked {job_id} as {status}.")
        return 0

    if args.tracker:
        return write_tracker(args.config, args.db, args.tracker)

    if args.add:
        url, title, company = args.add
        with JobStore(args.db) as store:
            added = store.add_manual(manual_id(url), company, title, url)
        print("Added to your dashboard." if added else "That link is already tracked.")
        return 0

    scheduled_run = not (args.check or args.dry or args.scan)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        if scheduled_run:
            notify_failure(args.config, f"config.json has an error: {exc}")
        return 2

    if args.check:
        return check_feeds(config, args.only)
    if args.scan:
        return scan(config, args.db)
    try:
        return run_daily(config, args.db, dry=args.dry)
    except ValueError as exc:  # e.g. ntfy topic not configured
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # last resort: a crash must still reach the phone
        log.exception("the daily run failed")
        if scheduled_run:
            notify_failure(args.config, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
