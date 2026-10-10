import io
import json
import urllib.error

import pytest

from jobscout import http


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    from email.message import Message

    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://x", code, "error", message, None)


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(http.time, "sleep", calls.append)
    return calls


def test_retries_server_errors_then_succeeds(monkeypatch, sleeps):
    outcomes = [
        _http_error(503, {"Retry-After": "2"}),
        FakeResponse(json.dumps({"ok": 1}).encode()),
    ]

    def fake_open(request, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http, "open_url", fake_open)
    assert http.request_json("https://x") == {"ok": 1}
    assert sleeps == [2.0]


def test_client_errors_fail_immediately(monkeypatch, sleeps):
    def fake_open(request, timeout):
        raise _http_error(404)

    monkeypatch.setattr(http, "open_url", fake_open)
    with pytest.raises(http.HttpError) as info:
        http.request_json("https://x")
    assert info.value.status == 404
    assert sleeps == []


def test_gives_up_after_retries(monkeypatch, sleeps):
    def fake_open(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(http, "open_url", fake_open)
    with pytest.raises(http.HttpError, match="failed after 3 attempts"):
        http.request_json("https://x", backoff=2)
    assert sleeps == [2, 4]


def test_post_sends_json(monkeypatch):
    seen = {}

    def fake_open(request, timeout):
        seen["method"], seen["body"] = request.get_method(), json.loads(request.data)
        seen["type"] = request.get_header("Content-type")
        return FakeResponse(b"{}")

    monkeypatch.setattr(http, "open_url", fake_open)
    http.request_json("https://x", body={"q": 1})
    assert seen == {"method": "POST", "body": {"q": 1}, "type": "application/json"}


def test_wait_for_network_retries_until_reachable(monkeypatch, sleeps):
    outcomes = [urllib.error.URLError("down"), urllib.error.URLError("down"), FakeResponse(b"")]

    def fake_open(request, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http, "open_url", fake_open)
    assert http.wait_for_network("https://ntfy.sh", max_wait=60, interval=5) is True
    assert sleeps == [5, 5]


def test_wait_for_network_gives_up(monkeypatch, sleeps):
    clock = iter(range(0, 1000, 30))
    monkeypatch.setattr(http.time, "monotonic", lambda: next(clock))

    def fake_open(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(http, "open_url", fake_open)
    assert http.wait_for_network("https://ntfy.sh", max_wait=60, interval=5) is False


def test_wait_for_network_treats_http_errors_as_online(monkeypatch, sleeps):
    def fake_open(request, timeout):
        raise _http_error(405)

    monkeypatch.setattr(http, "open_url", fake_open)
    assert http.wait_for_network("https://ntfy.sh") is True
