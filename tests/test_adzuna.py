from urllib.parse import parse_qs, urlparse

import pytest

from jobscout.models import Company
from jobscout.sources import AdzunaQuery, FetchOptions, SourceError, adzuna

COMPANY = Company("Adzuna", "adzuna", "us")
QUERY = AdzunaQuery(
    app_id="ID123",
    app_key="SECRETKEY",
    where="Albuquerque, New Mexico",
    distance_km=20,
    max_days_old=21,
    searches=((("category", "sales-jobs"),), (("what", "sales"), ("part_time", "1"))),
    pages_per_search=2,
)


def result(job_id, title="Sales Associate", employer="Local Shop", contract="part_time"):
    return {
        "id": job_id,
        "title": f"<strong>{title}</strong>",
        "description": "Help customers. No experience required. Paid training.",
        "redirect_url": f"https://www.adzuna.com/land/ad/{job_id}",
        "created": "2026-10-08T12:00:00Z",
        "location": {
            "display_name": "Albuquerque, Bernalillo County",
            "area": ["US", "New Mexico", "Bernalillo County", "Albuquerque"],
        },
        "company": {"display_name": employer},
        "contract_time": contract,
    }


def test_parse_result():
    (job,) = adzuna.parse({"results": [result("42")]})
    assert job.id == "az-42" and job.company == "Local Shop"
    assert job.title == "Sales Associate"  # highlighting removed
    assert job.location == "Albuquerque, Bernalillo County, New Mexico"
    assert job.country == "US" and job.employment_type == "part-time"
    assert job.posted_at == "2026-10-08" and job.apply_url.endswith("/42")


def test_fetch_needs_credentials():
    with pytest.raises(SourceError, match=r"secrets\.json"):
        adzuna.fetch(COMPANY, FetchOptions())


def test_fetch_runs_each_search_skips_known_employers_and_dedupes(monkeypatch):
    calls = []
    pages = {
        ("sales-jobs", "1"): [result(str(n)) for n in range(50)],
        ("sales-jobs", "2"): [result("50", employer="Verizon Wireless"), result("51")],
        ("sales", "1"): [result("0"), result("60", employer="Lowe's Home Improvement")],
    }

    def fake_get(url, *, body=None):
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        calls.append(params)
        key = (params.get("category") or params.get("what"), parsed.path.rsplit("/", 1)[1])
        return {"results": pages[key]}

    monkeypatch.setattr(adzuna, "get_board_json", fake_get)
    options = FetchOptions(adzuna=QUERY, known_companies=("Verizon", "Lowe's"))
    jobs = adzuna.fetch(COMPANY, options)

    assert len(calls) == 3  # page 2 of the first search, then one page of the second
    assert calls[0]["where"] == "Albuquerque, New Mexico" and calls[0]["distance"] == "20"
    assert calls[0]["max_days_old"] == "21" and calls[2]["part_time"] == "1"
    assert {job.id for job in jobs} == {f"az-{n}" for n in range(50)} | {"az-51"}


def test_errors_never_show_the_api_key(monkeypatch):
    def fake_get(url, *, body=None):
        raise SourceError(f"HTTP 401: {url}")

    monkeypatch.setattr(adzuna, "get_board_json", fake_get)
    with pytest.raises(SourceError) as info:
        adzuna.fetch(COMPANY, FetchOptions(adzuna=QUERY))
    assert "SECRETKEY" not in str(info.value) and "ID123" not in str(info.value)
    assert "***" in str(info.value)


def test_adzuna_job_in_albuquerque_passes_the_filter(job_filter):
    (job,) = adzuna.parse({"results": [result("7", title="Retail Sales Associate")]})
    verdict = job_filter.evaluate(job)
    assert verdict.passed and verdict.part_time and verdict.track == "sales"
