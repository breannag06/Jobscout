import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from helpers import make_job, passed

from jobscout.storage import JobStore, manual_id
from jobscout.web import age, create_app, explain, fit_label, safe_url


@pytest.fixture
def paths(tmp_path):
    db = tmp_path / "jobs.db"
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "contact": {"Full name": "Test Student", "Phone": ""},
                "resumes": {"sales": "/docs/sales-resume.pdf"},
            }
        )
    )
    with JobStore(db) as store:
        item = make_job("SDR Intern", "Remote", job_id="gh-acme-1")
        item = type(item)(**{**item.__dict__, "apply_url": "https://apply.example/1"})
        store.sync(
            [(item, passed(40, track="sales", internship=True, reasons=("title: sdr +18",)))],
            {"Acme"},
        )
    return db, profile


@pytest.fixture
def client(paths):
    app = create_app(*paths)
    app.testing = True
    return app.test_client()


def token(client) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").get_data(as_text=True)).group(
        1
    )


def test_index_shows_match_with_reasons_and_resume(client):
    page = client.get("/").get_data(as_text=True)
    assert "SDR Intern" in page
    assert 'href="https://example.com/gh-acme-1"' in page  # title links straight to the posting
    assert "<b>1</b> job to apply to" in page
    assert "Why it's here: SDR role." in page
    assert "sales-resume.pdf" in page


def test_apply_link_records_open_and_redirects(client, paths):
    response = client.get("/open/gh-acme-1")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://apply.example/1"
    assert "You opened this one. Did you apply?" in client.get("/").get_data(as_text=True)


def test_status_change_requires_token(client, paths):
    assert (
        client.post("/status", data={"job_id": "gh-acme-1", "status": "applied"}).status_code == 403
    )
    response = client.post(
        "/status", data={"job_id": "gh-acme-1", "status": "applied", "csrf": token(client)}
    )
    assert response.status_code == 302
    with JobStore(paths[0]) as store:
        assert store.get("gh-acme-1")["status"] == "applied"


def test_unknown_status_and_job_rejected(client):
    csrf = token(client)
    assert (
        client.post(
            "/status", data={"job_id": "gh-acme-1", "status": "x", "csrf": csrf}
        ).status_code
        == 400
    )
    assert (
        client.post("/status", data={"job_id": "nope", "status": "skip", "csrf": csrf}).status_code
        == 404
    )


def test_add_job_and_notes(client, paths):
    csrf = token(client)
    url = "https://handshake.example/job/9"
    assert (
        client.post(
            "/add",
            data={"url": url, "title": "Campus Ambassador", "company": "Notion", "csrf": csrf},
        ).status_code
        == 302
    )
    assert (
        client.post(
            "/add", data={"url": "javascript:alert(1)", "title": "x", "company": "y", "csrf": csrf}
        ).status_code
        == 400
    )
    client.post("/notes", data={"job_id": manual_id(url), "notes": "Due Oct 30", "csrf": csrf})
    with JobStore(paths[0]) as store:
        assert store.get(manual_id(url))["notes"] == "Due Oct 30"


def test_kit_shows_profile_with_copy_buttons(client):
    page = client.get("/kit").get_data(as_text=True)
    assert "Test Student" in page and "data-copy" in page
    assert "Not filled in yet" in page


def test_safe_url_and_age():
    assert safe_url("javascript:alert(1)") == "#"
    assert safe_url("https://ok") == "https://ok"
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert age("2026-10-09", now) == "today"
    assert age((now - timedelta(days=3)).isoformat(), now) == "3 days ago"
    assert age("", now) == ""


def test_is_running_detects_a_listening_port():
    import socket

    from jobscout.web import is_running

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert is_running("127.0.0.1", port)
    assert not is_running("127.0.0.1", port)


def test_tracker_pdf_opens_in_the_browser(client, paths):
    with JobStore(paths[0]) as store:
        store.set_status("gh-acme-1", "applied")
    response = client.get("/tracker.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline")
    assert response.data.startswith(b"%PDF")
    assert "Applications (PDF)" in client.get("/").get_data(as_text=True)


def test_reach_out_tab_and_followed_up_moves_to_tracker(client, paths):
    old = datetime.now(timezone.utc) - timedelta(days=8)
    with JobStore(paths[0]) as store:
        store.set_status("gh-acme-1", "applied", old)
    page = client.get("/?view=reach_out").get_data(as_text=True)
    assert "SDR Intern" in page and "Time to reach out" in page and ">I followed up<" in page
    assert "1 reach-out due" in client.get("/").get_data(as_text=True)

    csrf = token(client)
    client.post("/status", data={"job_id": "gh-acme-1", "status": "follow_up", "csrf": csrf})
    with JobStore(paths[0]) as store:
        assert store.get("gh-acme-1") is None
        assert [row["title"] for row in store.applications()] == ["SDR Intern"]
    assert "SDR Intern" not in client.get("/?view=all").get_data(as_text=True)


def test_new_cards_offer_applied_and_skip_only(client):
    page = client.get("/").get_data(as_text=True)
    assert 'value="applied"' in page and 'value="skip"' in page
    assert 'value="follow_up"' not in page


def test_explain_reads_like_a_sentence():
    reasons = [
        "title: sales +12",
        "title: sales development +4",
        "internship +5",
        "sql +3",
        "rising senior -5",
    ]
    assert explain(reasons) == (
        "sales development role, internship, mentions SQL; heads up, it mentions rising senior"
    )
    assert explain([]) == ""


def test_fit_labels():
    assert [fit_label(s) for s in (45, 30, 15, 5)] == [
        "Great fit",
        "Good fit",
        "Worth a look",
        "Long shot",
    ]


def test_every_tab_has_a_friendly_empty_state(client):
    for view in ("housing", "reach_out", "applied", "skip", "closed"):
        page = client.get(f"/?view={view}").get_data(as_text=True)
        assert 'class="empty"' in page


def test_explain_skips_repeats():
    reasons = [
        "title: business development +16",
        "business development +3",
        "intern +4",
        "internship +5",
        "sales +2",
    ]
    assert explain(reasons) == "business development role, internship, mentions sales"
