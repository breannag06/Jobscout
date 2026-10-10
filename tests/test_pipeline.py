from datetime import datetime, timezone

from helpers import make_job

from jobscout.pipeline import run
from jobscout.sources import FetchResult
from jobscout.storage import JobStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_run_records_matches_and_skips_failed_companies(config):
    good, bad = config.companies[0], config.companies[1]
    jobs = [
        make_job(
            "Sales Development Intern", "Remote - US", "prospecting", job_id="1", company=good.name
        ),
        make_job("Senior Sales Manager", "Remote - US", job_id="2", company=good.name),
    ]
    seen_options = []

    def fetcher(companies, options, handle):
        seen_options.append(options)
        return [handle(FetchResult(good, jobs)), handle(FetchResult(bad, error="HTTP 500"))]

    with JobStore(":memory:") as store:
        report = run(config, store, fetcher=fetcher, now=NOW)
        assert [m.job.id for m in report.matches] == ["1"]
        assert report.matches[0].job.description == ""  # descriptions are dropped after scoring
        assert all(result.jobs == [] for result in report.results)
        assert [f.company for f in report.failures] == [bad]
        assert report.sync.added == 1
        assert "1 new" in report.summary()
        assert store.get("2") is None

    options = seen_options[0]
    assert options.search_terms == config.workday_search_terms
    assert options.listing_filter("Summer 2027 Sales Internship", "Georgia - Atlanta")
    assert not options.listing_filter("Summer 2027 Sales Internship", "Ireland - Dublin")
    assert not options.listing_filter("Senior Sales Manager", "Georgia - Atlanta")


def test_workday_companies_are_not_closed_by_absence(config):
    wd = next(c for c in config.companies if c.source == "workday")
    first = [make_job("Sales Intern", "Remote - US", job_id="wd-1", company=wd.name)]
    with JobStore(":memory:") as store:
        run(config, store, fetcher=lambda cs, o, h: [h(FetchResult(wd, first))], now=NOW)
        report = run(config, store, fetcher=lambda cs, o, h: [h(FetchResult(wd, []))], now=NOW)
        assert report.sync.closed == 0
        assert store.get("wd-1")["closed_at"] is None
