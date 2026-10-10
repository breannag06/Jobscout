import pytest

from jobscout.models import Company
from jobscout.sources import FetchOptions, SourceError, ashby, greenhouse, lever, map_fetch, workday


def test_parse_greenhouse_payload():
    company = Company("Acme", "greenhouse", "acme")
    payload = {
        "jobs": [
            {
                "id": 7,
                "title": "Data Intern",
                "location": {"name": "Remote"},
                "absolute_url": "https://example.com/7",
                "content": "<p>Python</p>",
                "first_published": "2026-09-30T12:00:00-04:00",
            }
        ]
    }
    (job,) = greenhouse.parse(company, payload)
    assert (job.id, job.location, job.description) == ("gh-acme-7", "Remote", "Python")
    assert job.apply_url == "https://example.com/7"
    assert job.posted_at == "2026-09-30"


def test_greenhouse_downloads_descriptions_only_for_possible_matches(monkeypatch):
    company = Company("Acme", "greenhouse", "acme")
    listing = {
        "jobs": [
            {
                "id": 1,
                "title": "Sales Intern",
                "location": {"name": "Remote"},
                "absolute_url": "https://g/1",
            },
            {
                "id": 2,
                "title": "Senior Engineer",
                "location": {"name": "Remote"},
                "absolute_url": "https://g/2",
            },
        ]
    }
    requested = []

    def fake_get(url, *, body=None):
        requested.append(url)
        if url.endswith("/jobs"):
            return listing
        return {"content": "&lt;p&gt;Prospect customers.&lt;/p&gt;"}

    monkeypatch.setattr(greenhouse, "get_board_json", fake_get)
    options = FetchOptions(listing_filter=lambda title, where: "Senior" not in title)
    intern, senior = greenhouse.fetch(company, options)
    assert intern.description == "Prospect customers."
    assert senior.description == "" and senior.id == "gh-acme-2"  # still listed, not downloaded
    assert requested == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
    ]


def test_greenhouse_detail_failure_fails_the_board(monkeypatch):
    company = Company("Acme", "greenhouse", "acme")

    def fake_get(url, *, body=None):
        if url.endswith("/jobs"):
            return {"jobs": [{"id": 1, "title": "Sales Intern", "location": {"name": "Remote"}}]}
        raise SourceError("HTTP 500")

    monkeypatch.setattr(greenhouse, "get_board_json", fake_get)
    with pytest.raises(SourceError):
        greenhouse.fetch(company, FetchOptions())


def test_parse_lever_payload():
    company = Company("Acme", "lever", "acme")
    payload = [
        {
            "id": "x1",
            "text": "SDR Intern",
            "categories": {"commitment": "Part-time"},
            "hostedUrl": "https://example.com/x1",
            "applyUrl": "https://example.com/x1/apply",
            "descriptionPlain": "Sales",
            "createdAt": 1790000000000,
        }
    ]
    (job,) = lever.parse(company, payload)
    assert job.id == "lv-acme-x1" and job.location == ""
    assert job.employment_type == "part-time"
    assert job.apply_url.endswith("/apply")
    assert job.posted_at == "2026-09-21"


def test_parse_ashby_payload():
    company = Company("Acme", "ashby", "acme")
    payload = {
        "jobs": [
            {
                "id": "a1",
                "title": "SDR Intern",
                "location": "US-NY-New York",
                "secondaryLocations": [{"location": "Austin, TX"}],
                "workplaceType": "OnSite",
                "employmentType": "Intern",
                "jobUrl": "https://example.com/a1",
                "applyUrl": "https://example.com/a1/application",
                "publishedAt": "2026-10-01T00:00:00Z",
                "descriptionPlain": "Prospect.",
                "isListed": True,
            },
            {"id": "a2", "title": "Hidden", "isListed": False},
        ]
    }
    (job,) = ashby.parse(company, payload)
    assert job.id == "ab-acme-a1"
    assert job.location == "US-NY-New York; Austin, TX"
    assert job.employment_type == "internship"
    assert job.posted_at == "2026-10-01"
    assert job.country == ""


def test_ashby_marks_only_fully_remote_jobs_as_remote():
    remote = {"location": "United States", "workplaceType": "Remote", "isRemote": True}
    hybrid = {"location": "Denver, CO", "workplaceType": "Hybrid", "isRemote": True}
    assert ashby.location(remote) == "Remote, United States"
    assert ashby.location(hybrid) == "Denver, CO"


# ---- Workday -------------------------------------------------------------------------

BOARD_URL = "https://acme.wd5.myworkdayjobs.com/External"


@pytest.mark.parametrize(
    "url", [BOARD_URL, BOARD_URL + "/", "https://acme.wd5.myworkdayjobs.com/en-US/External"]
)
def test_parse_board(url):
    board = workday.parse_board(url)
    assert board.api_root == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"


@pytest.mark.parametrize(
    "url", ["acme", "https://example.com/jobs", "http://acme.wd5.myworkdayjobs.com/x"]
)
def test_parse_board_rejects_other_urls(url):
    with pytest.raises(ValueError):
        workday.parse_board(url)


def _detail(title: str, req: str, **extra: object) -> dict:
    return {
        "jobPostingInfo": {
            "title": title,
            "jobReqId": req,
            "location": "California - San Francisco",
            "country": {"descriptor": "United States of America"},
            "timeType": "Full time",
            "startDate": "2026-10-01",
            "externalUrl": f"{BOARD_URL}/job/{req}",
            "jobDescription": "<p>Sell things. Housing provided.</p>",
            **extra,
        }
    }


def test_workday_fetch_searches_filters_titles_and_downloads_details(monkeypatch):
    company = Company("Acme", "workday", BOARD_URL)
    pages = {
        0: {
            "total": 22,
            "jobPostings": [
                {
                    "title": f"Sales Intern {n}",
                    "externalPath": f"/job/s{n}",
                    "locationsText": "Texas",
                }
                for n in range(20)
            ],
        },
        20: {
            "total": 0,
            "jobPostings": [
                {"title": "Senior Sales Manager", "externalPath": "/job/senior"},
                {"title": "Sales Intern", "externalPath": "/job/dublin", "locationsText": "Dublin"},
                {"title": "Sales Intern 0", "externalPath": "/job/s0"},
            ],
        },
    }
    requests: list[str] = []

    def fake_get(url: str, *, body: dict | None = None) -> dict:
        requests.append(url)
        if url.endswith("/jobs"):
            assert body is not None
            return pages[body["offset"]]
        path = url.rsplit("/job/", 1)[1]
        if path == "s3":
            raise SourceError("HTTP 500")
        return _detail(f"Sales Intern {path[1:]}", f"R{path}")

    monkeypatch.setattr(workday, "get_board_json", fake_get)
    options = FetchOptions(
        search_terms=("internship",),
        listing_filter=lambda title, where: "senior" not in title.lower() and where != "Dublin",
    )
    jobs = workday.fetch(company, options)

    assert len(jobs) == 19  # 20 unique listings kept, one detail download failed
    assert not any("senior" in url or "dublin" in url for url in requests)
    job = jobs[0]
    assert job.id == "wd-acme-Rs0"
    assert job.location == "California - San Francisco"
    assert job.employment_type == "full-time"
    assert job.country == "United States of America"
    assert job.apply_url == f"{BOARD_URL}/job/Rs0/apply"
    assert job.description == "Sell things. Housing provided."


def test_workday_remote_type_marks_location_remote():
    job = workday.parse_posting(
        Company("Acme", "workday", BOARD_URL),
        workday.parse_board(BOARD_URL),
        _detail("Sales Intern", "R1", remoteType="Remote", timeType="Part time"),
    )
    assert job.location.startswith("Remote, ")
    assert job.employment_type == "part-time"


def test_workday_requires_search_terms():
    with pytest.raises(SourceError, match="workday_search_terms"):
        workday.fetch(Company("Acme", "workday", BOARD_URL), FetchOptions())


# ---- Fetching many companies ---------------------------------------------------------


def test_fetch_all_isolates_failures(monkeypatch):
    from jobscout import sources

    def fake_fetch(company, options):
        if company.token == "broken":
            raise SourceError("board not found")
        return []

    monkeypatch.setitem(sources.SOURCES, "greenhouse", fake_fetch)
    companies = [Company("Good", "greenhouse", "good"), Company("Bad", "greenhouse", "broken")]
    good, bad = map_fetch(companies, None, lambda result: result)
    assert good.ok and good.jobs == []
    assert not bad.ok and bad.error == "board not found"


def test_workday_company_search_terms_replace_the_global_ones(monkeypatch):
    company = Company("Shop", "workday", BOARD_URL, search_terms=("albuquerque",))
    searched = []

    def fake_get(url, *, body=None):
        if url.endswith("/jobs"):
            searched.append(body["searchText"])
            return {"total": 0, "jobPostings": []}
        raise AssertionError("no details expected")

    monkeypatch.setattr(workday, "get_board_json", fake_get)
    workday.fetch(company, FetchOptions(search_terms=("internship", "university")))
    assert searched == ["albuquerque"]
