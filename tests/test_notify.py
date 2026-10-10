import sqlite3
import urllib.error

import pytest

from jobscout import notify
from jobscout.notify import (
    PRIORITY_HIGH,
    DailySummary,
    NtfyNotifier,
    build_daily_message,
    build_failure_message,
)


def rows(*specs):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE r (id, company, title, location, score, apply_url, track, "
        "is_internship, is_part_time)"
    )
    conn.executemany("INSERT INTO r VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", specs)
    return conn.execute("SELECT * FROM r").fetchall()


MATCHES = rows(
    ("1", "Samsara", "ADR Intern", "Phoenix, AZ", 45, "https://a/1", "sales", 1, 0),
    ("2", "Figma", "Data Science Intern", "", 40, "https://a/2", "data", 1, 0),
    ("3", "Celonis", "BDR Intern", "Remote", 38, "https://a/3", "sales", 1, 1),
    (
        "4",
        "A Very Long Company Name Incorporated",
        "Intern",
        "Remote",
        30,
        "https://a/4",
        "other",
        1,
        0,
    ),
)


def reach_out_rows(*specs):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (company, title, applied_at, due_at)")
    conn.executemany("INSERT INTO r VALUES (?, ?, ?, ?)", specs)
    return conn.execute("SELECT * FROM r").fetchall()


DUE = reach_out_rows(
    ("Gartner", "Sales Intern", "2026-10-01T15:00:00+00:00", "2026-10-08 15:00:00")
)
SOON = reach_out_rows(("Adobe", "BDR Intern", "2026-10-06T15:00:00+00:00", "2026-10-13 15:00:00"))


def test_daily_message_leads_with_counts_reach_outs_and_new_matches():
    message = build_daily_message(
        DailySummary(
            to_apply=7,
            new_matches=MATCHES[:2],
            top_matches=MATCHES,
            awaiting_answer=1,
            housing_checks=34,
            new_housing_checks=2,
            reach_outs_due=DUE,
            reach_outs_upcoming=SOON,
        )
    )
    assert message.title == "7 jobs to apply · 1 reach-out due · 2 new"
    lines = message.body.splitlines()
    assert lines[:4] == [
        "Waiting for you to apply: 7",
        "Opened but not marked Applied or Skip: 1",
        "Reach-outs due (7 days after applying): 1",
        "Internships to check for housing: 34 (2 new)",
    ]
    assert (
        "Reach out now, then mark it Followed up:\n- Gartner: Sales Intern (applied Oct 1, due Oct 8)"
        in message.body
    )
    assert "Reach-outs coming up:\n- Adobe: BDR Intern (applied Oct 6, due Oct 13)" in message.body
    assert message.body.index("Reach out now") < message.body.index("New since the last alert")
    assert "1. Samsara: ADR Intern (Phoenix, AZ) [45 · Sales, internship]" in message.body
    assert message.click_url == "https://a/1"
    assert message.tags == ("bell",) and message.priority == PRIORITY_HIGH


def test_no_reach_outs_keeps_normal_priority():
    message = build_daily_message(DailySummary(to_apply=2, new_matches=MATCHES[:1]))
    assert message.priority == 3 and message.tags == ("briefcase",)
    assert "Reach out now" not in message.body


def test_daily_message_without_new_matches_still_links_top_jobs():
    message = build_daily_message(DailySummary(to_apply=1, top_matches=MATCHES[2:3]))
    assert message.title == "1 job to apply"
    assert message.click_url == "https://a/3"
    assert "New since the last alert" not in message.body


def test_problems_raise_priority_and_are_listed():
    message = build_daily_message(
        DailySummary(to_apply=0, problems=("2 company feeds failed: A, B.",))
    )
    assert message.title == "0 jobs to apply · 1 problem"
    assert "Needs attention:\n- 2 company feeds failed: A, B." in message.body
    assert message.priority == PRIORITY_HIGH and message.tags == ("warning",)


def test_failure_message():
    message = build_failure_message("KeyError: 'jobs'", "/logs/jobscout.log")
    assert message.title == "Job finder failed"
    assert "KeyError" in message.body and "/logs/jobscout.log" in message.body
    assert message.priority == PRIORITY_HIGH


def test_payload_uses_json_publishing_fields():
    notifier = NtfyNotifier("https://ntfy.sh/", "topic-1")
    payload = notifier.payload(build_daily_message(DailySummary(1, new_matches=MATCHES[:1])))
    assert payload["topic"] == "topic-1" and payload["priority"] == 3
    assert payload["click"] == "https://a/1"
    assert "actions" not in payload  # no buttons on the alert


def test_send_retries_network_errors(monkeypatch):
    attempts = []
    sleeps = []

    class Done:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout=20):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise urllib.error.URLError("offline")
        return Done()

    monkeypatch.setattr(notify, "open_url", fake_open)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    NtfyNotifier("https://ntfy.sh", "t").send(build_failure_message("x"))
    assert len(attempts) == 3 and sleeps == [10, 20]


def test_send_gives_up_after_the_last_attempt(monkeypatch):
    def fake_open(request, timeout=20):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(notify, "open_url", fake_open)
    monkeypatch.setattr(notify.time, "sleep", lambda _: None)
    with pytest.raises(urllib.error.URLError):
        NtfyNotifier("https://ntfy.sh", "t").send(build_failure_message("x"))


@pytest.mark.parametrize("topic", ["", "YOUR_NTFY_TOPIC"])
def test_placeholder_topic_rejected(topic):
    with pytest.raises(ValueError):
        NtfyNotifier("https://ntfy.sh", topic)


def test_long_digests_stay_under_the_ntfy_size_limit():
    many = rows(
        *[
            (
                str(n),
                f"Company {n}",
                "Business Development Representative Intern " * 3,
                "San Francisco, CA; New York, NY; Austin, TX",
                40,
                f"https://jobs.example/{'x' * 120}/{n}",
                "sales",
                1,
                0,
            )
            for n in range(30)
        ]
    )
    message = build_daily_message(DailySummary(to_apply=30, new_matches=many))
    assert len(message.body.encode("utf-8")) <= notify.MAX_BODY_BYTES
    assert "more in the dashboard." in message.body
    assert message.body.endswith("Open the dashboard to apply.")


def test_adzuna_jobs_are_credited():
    from jobscout.notify import format_match

    (row,) = rows(
        ("az-5", "Local Shop", "Sales Associate", "Albuquerque", 20, "https://a/5", "sales", 0, 1)
    )
    assert "(Albuquerque) via Adzuna [20" in format_match(1, row)
