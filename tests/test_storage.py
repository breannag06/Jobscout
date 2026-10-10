from datetime import datetime, timedelta, timezone

import pytest
from helpers import make_job, passed

from jobscout.models import Evaluation
from jobscout.storage import JobStore, manual_id, reasons_of

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def store():
    with JobStore(":memory:") as s:
        yield s


def job(job_id: str, company: str = "Acme"):
    return make_job(f"Role {job_id}", "Remote", job_id=job_id, company=company)


def match(job_id: str, score: int, company: str = "Acme", **fields):
    return job(job_id, company), passed(score, **fields)


def test_sync_inserts_new_matches_once(store):
    first = store.sync([match("a", 5), match("b", 9)], {"Acme"}, T0)
    again = store.sync([match("a", 5)], {"Acme"}, T0)
    assert first.added == 2 and again.added == 0


def test_sync_rescores_existing_rows(store):
    store.sync([match("a", 5)], {"Acme"}, T0)
    result = store.sync([match("a", 8), match("b", 1)], {"Acme"}, T0)
    assert result.rescored == 1
    assert store.get("a")["score"] == 8


def test_rows_that_stop_matching_are_dropped_from_alerts(store):
    store.sync([match("a", 5)], {"Acme"}, T0)
    result = store.sync([(job("a"), Evaluation.reject("outside the US"))], {"Acme"}, T0)
    assert result.dropped == 1
    assert store.pending_alerts(10) == []
    assert store.get("a")["rejected_reason"] == "outside the US"
    assert [row["id"] for row in store.list_jobs("closed")] == ["a"]


def test_rows_missing_from_their_board_are_closed_and_can_reopen(store):
    store.sync([match("a", 5), match("b", 3)], {"Acme"}, T0)
    result = store.sync([match("b", 3)], {"Acme"}, T0)
    assert result.closed == 1
    assert store.get("a")["closed_at"] is not None
    assert store.sync([match("b", 3)], {"Acme"}, T0).closed == 0  # counted once
    store.sync([match("a", 5), match("b", 3)], {"Acme"}, T0)
    assert store.get("a")["closed_at"] is None


def test_companies_that_failed_to_fetch_are_left_alone(store):
    store.sync([match("a", 5, company="Acme"), match("z", 4, company="Zeta")], {"Acme", "Zeta"}, T0)
    store.sync([match("a", 5, company="Acme")], {"Acme"}, T0)  # Zeta failed this run
    assert store.get("z")["closed_at"] is None


def test_manual_rows_are_never_closed(store):
    store.add_manual(manual_id("https://x/1"), "Acme", "SDR Intern", "https://x/1", T0)
    store.sync([match("a", 5)], {"Acme"}, T0)
    assert store.get(manual_id("https://x/1"))["closed_at"] is None


def test_alerts_sorted_by_score_and_limited(store):
    store.sync([match("a", 5), match("b", 9), match("c", 7)], {"Acme"}, T0)
    assert [row["id"] for row in store.pending_alerts(limit=2)] == ["b", "c"]


def test_alerted_and_non_new_rows_are_not_sent(store):
    store.sync([match("a", 5), match("b", 4)], {"Acme"}, T0)
    store.mark_alerted(["a"])
    store.set_status("b", "applied")
    assert store.pending_alerts(limit=10) == []


def test_alert_rows_carry_apply_link_and_details(store):
    item = make_job("SDR Intern", "Remote", job_id="a")
    item = type(item)(**{**item.__dict__, "apply_url": "https://apply/a"})
    store.sync(
        [(item, passed(9, track="sales", internship=True, reasons=("title: sdr +18",)))],
        {"Acme"},
        T0,
    )
    (row,) = store.pending_alerts(10)
    assert row["apply_url"] == "https://apply/a"
    assert row["track"] == "sales" and row["is_internship"] == 1
    assert reasons_of(row) == ["title: sdr +18"]


def test_set_status_validates_and_logs_first_applied_date(store):
    store.sync([match("a", 5)], {"Acme"}, T0)
    assert store.set_status("a", "applied", T0) is True
    assert store.set_status("a", "applied", T0 + timedelta(days=3)) is True
    assert store.get("a")["applied_at"] == T0.isoformat(timespec="seconds")
    assert store.set_status("missing", "applied") is False
    with pytest.raises(ValueError):
        store.set_status("a", "maybe")


def test_reach_outs_are_listed_every_day_until_followed_up(store):
    store.sync([match("a", 5), match("b", 5)], {"Acme"}, T0)
    store.set_status("a", "applied", T0)
    store.set_status("b", "applied", T0 + timedelta(days=5))
    week = timedelta(days=7)
    day8 = T0 + timedelta(days=8)

    (due,) = store.reach_outs(week, now=day8)
    assert due["id"] == "a" and due["due_at"] == "2026-10-08 12:00:00"
    assert [r["id"] for r in store.reach_outs(week, now=day8)] == ["a"]  # still there the next day
    assert [r["id"] for r in store.reach_outs(week, timedelta(days=7), day8)] == ["a", "b"]

    store.set_status("a", "follow_up", day8)
    assert store.archive_applications(day8) == 1
    assert store.reach_outs(week, now=day8 + timedelta(days=1)) == []


def test_opened_jobs_come_first_until_answered(store):
    store.sync([match("a", 9), match("b", 1)], {"Acme"}, T0)
    store.record_opened("b", T0)
    assert [row["id"] for row in store.list_jobs("new")] == ["b", "a"]
    assert store.awaiting_answer_count() == 1
    store.set_status("b", "applied")
    assert store.awaiting_answer_count() == 0


def test_view_counts_and_notes(store):
    store.sync([match("a", 9), match("b", 1), match("c", 1)], {"Acme"}, T0)
    store.set_status("b", "skip")
    store.sync([match("a", 9), match("b", 1)], {"Acme"}, T0)  # c closed
    store.set_notes("a", "Referral from Sam")
    counts = store.view_counts()
    assert counts == {
        "new": 1,
        "housing": 0,
        "applied": 0,
        "skip": 1,
        "closed": 1,
        "all": 3,
    }
    assert store.get("a")["notes"] == "Referral from Sam"


def test_manual_add_is_deduplicated_and_not_alerted(store):
    job_id = manual_id("https://x")
    assert store.add_manual(job_id, "Acme", "SDR Intern", "https://x") is True
    assert store.add_manual(job_id, "Acme", "SDR Intern", "https://x") is False
    assert store.pending_alerts(limit=10) == []
    assert manual_id(" https://x ") == job_id


def test_file_databases_use_write_ahead_logging(tmp_path):
    with JobStore(tmp_path / "jobs.db") as store:
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_legacy_rows_without_details_still_work(store):
    store._conn.execute(
        "INSERT INTO jobs (id, company, title, location, url, score, first_seen, status) "
        "VALUES ('old', 'Gone Co', 'SDR', '', 'https://old', 5, '2026-01-01', '')"
    )
    (row,) = store.list_jobs("new")
    assert row["apply_url"] == "https://old" and row["track"] == "other"


def test_near_misses_are_kept_for_review_but_never_alerted(store):
    near = Evaluation(
        passed=False, score=30, rejection="no housing", near_miss=True, internship=True
    )
    result = store.sync([(job("n"), near), match("a", 5)], {"Acme"}, T0)
    assert (result.added, result.near_misses_added) == (1, 1)
    assert [row["id"] for row in store.list_jobs("housing")] == ["n"]
    assert [row["id"] for row in store.list_jobs("new")] == ["a"]
    assert store.list_jobs("closed") == []
    assert [row["id"] for row in store.pending_alerts(10)] == ["a"]

    # The posting later mentions housing, so it becomes a normal match.
    store.sync([match("n", 31), match("a", 5)], {"Acme"}, T0)
    assert store.list_jobs("housing") == []
    assert {row["id"] for row in store.pending_alerts(10)} == {"a", "n"}


def test_partial_boards_never_close_postings(store):
    store.sync([match("w", 5, company="Big")], {"Big"}, T0)
    store.sync([], {"Big"}, T0, complete_companies=set())
    assert store.get("w")["closed_at"] is None


def test_missing_postings_with_failing_titles_are_flagged_not_closed(store):
    store.sync([match("a", 5), match("b", 5)], {"Acme"}, T0)
    result = store.sync(
        [],
        {"Acme"},
        T0,
        complete_companies=set(),
        title_rejection=lambda title: "excluded title keyword (x)" if title == "Role a" else "",
    )
    assert result.dropped == 1 and result.closed == 0
    assert store.get("a")["rejected_reason"] == "excluded title keyword (x)"
    assert store.get("b")["rejected_reason"] is None and store.get("b")["closed_at"] is None


# ---- Cleanup, archive, tracker rows, and backups -----------------------------------------


def test_untouched_postings_are_removed_after_a_week_and_stay_gone(store):
    store.sync([match("old", 5), match("opened", 5), match("noted", 5)], {"Acme"}, T0)
    store.add_manual(manual_id("https://x/m"), "Acme", "Mine", "https://x/m", T0)
    store.record_opened("opened", T0)
    store.set_notes("noted", "ask Sam")
    later = T0 + timedelta(days=8)
    store.sync([match("fresh", 5)], {"Acme"}, later)

    assert store.remove_untouched(timedelta(days=7), later) == 1
    remaining = {row["id"] for row in store.list_jobs("all")}
    assert remaining == {"opened", "noted", "fresh", manual_id("https://x/m")}

    # The next scan sees the posting again but does not add it back.
    result = store.sync([match("old", 5), match("fresh", 5)], {"Acme"}, later)
    assert result.added == 0 and store.get("old") is None


def test_recent_untouched_postings_are_kept(store):
    store.sync([match("a", 5)], {"Acme"}, T0)
    assert store.remove_untouched(timedelta(days=7), T0 + timedelta(days=6)) == 0


def test_only_confirmed_follow_ups_move_to_the_tracker(store):
    store.sync([match("a", 5), match("b", 5)], {"Acme"}, T0)
    store.set_status("a", "applied", T0)
    store.set_status("b", "applied", T0)
    store.set_notes("a", "Recruiter: Sam")
    store.set_status("a", "follow_up", T0 + timedelta(days=8))

    assert store.archive_applications(T0 + timedelta(days=8)) == 1
    assert {row["id"] for row in store.list_jobs("all")} == {"b"}  # overdue but not confirmed
    tracked = {row["title"]: row for row in store.applications()}
    assert set(tracked) == {"Role a", "Role b"}
    assert tracked["Role a"]["notes"] == "Recruiter: Sam"
    assert tracked["Role a"]["archived_at"] is not None
    assert tracked["Role b"]["archived_at"] is None
    # Nothing but the small archive record is left for an archived application.
    for table in ("jobs", "job_details", "job_activity", "application_log"):
        column = "id" if table == "jobs" else "job_id"
        assert store._conn.execute(f"SELECT 1 FROM {table} WHERE {column} = 'a'").fetchone() is None
    store.compact()


def test_backup_keeps_the_newest_copies(tmp_path):
    with JobStore(tmp_path / "jobs.db") as store:
        store.sync([match("a", 5)], {"Acme"}, T0)
        for day in range(5):
            store.backup(tmp_path / "backups", keep=3, now=T0 + timedelta(days=day))
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert names == ["jobs-2026-10-03.db", "jobs-2026-10-04.db", "jobs-2026-10-05.db"]
    with JobStore(tmp_path / "backups" / names[-1]) as copy:
        assert copy.get("a") is not None


def test_edited_postings_are_refreshed(store):
    store.sync([match("a", 5)], {"Acme"}, T0)
    edited = make_job("Role a (Remote)", "Remote, US", job_id="a")
    store.sync([(edited, passed(5))], {"Acme"}, T0)
    row = store.get("a")
    assert (row["title"], row["location"]) == ("Role a (Remote)", "Remote, US")


def test_search_engine_postings_are_not_closed_by_a_company_board(store):
    adzuna_job = make_job("Sales Associate", "Albuquerque, NM", job_id="az-1", company="Acme")
    store.sync([(adzuna_job, passed(5)), match("gh-1", 5)], {"Acme", "Adzuna"}, T0)
    store.sync([match("gh-1", 5)], {"Acme"}, T0)  # Acme's own board does not list it
    assert store.get("az-1")["closed_at"] is None
