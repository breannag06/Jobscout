import sqlite3
from datetime import datetime, timedelta, timezone

from jobscout.tracker import latin1, render

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def rows(*specs):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE r (company, title, location, url, status, applied_at, notes, archived_at)"
    )
    conn.executemany("INSERT INTO r VALUES (?, ?, ?, ?, ?, ?, ?, ?)", specs)
    return conn.execute("SELECT * FROM r").fetchall()


def test_tracker_is_a_pdf_listing_every_application():
    pdf = render(
        rows(
            (
                "Gartner",
                "North America Summer 2027 Sales Internship – Irving",
                "Irving, TX",
                "https://g/1",
                "applied",
                "2026-10-18T12:00:00+00:00",
                "Recruiter: Sam",
                None,
            ),
            (
                "Adobe",
                "Sales Academy BDR Intern",
                "San Jose",
                "https://a/1",
                "follow_up",
                "2026-10-01T12:00:00+00:00",
                "",
                "2026-10-09T00:00:00+00:00",
            ),
        ),
        timedelta(days=7),
        NOW,
    )
    assert pdf.startswith(b"%PDF")
    assert b"/URI (https://g/1)" in pdf  # titles link to the posting
    assert len(pdf) > 1000


def test_empty_tracker_still_renders():
    assert render([], timedelta(days=7), NOW).startswith(b"%PDF")


def test_latin1_keeps_accents_and_replaces_the_rest():
    assert latin1("Sales – São Paulo “remote”") == 'Sales - São Paulo "remote"'
    assert latin1("Tokyo 東京") == "Tokyo ??"
