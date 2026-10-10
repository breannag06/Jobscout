"""HTTPS requests with verified certificates, JSON decoding, and retries.

Python installed from python.org on macOS ships without root certificates,
which makes every HTTPS request fail with CERTIFICATE_VERIFY_FAILED. Using
the ``certifi`` bundle when available avoids that.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (jobscout personal job finder)"

# Statuses that will not change on retry, so failing fast is kinder to the server.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 422})
_MAX_RETRY_AFTER_SECONDS = 30.0


class HttpError(RuntimeError):
    """A request failed permanently or ran out of retries."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:  # certifi is optional; fall back to the system store
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL_CONTEXT = _context()


def open_url(request: urllib.request.Request, timeout: float = 20) -> Any:
    """Open ``request`` over HTTPS with verified certificates."""
    return urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT)


def wait_for_network(url: str, max_wait: float = 180, interval: float = 15) -> bool:
    """Wait until ``url`` answers, up to ``max_wait`` seconds. Returns False on timeout.

    A scheduled run can start the moment the Mac wakes, before Wi-Fi reconnects.
    """
    deadline = time.monotonic() + max_wait
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    while True:
        try:
            with open_url(request, timeout=10):
                return True
        except urllib.error.HTTPError:
            return True  # the server answered, so the network is up
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)


def _retry_after(headers: Message | None) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return min(float(value), _MAX_RETRY_AFTER_SECONDS) if value else None
    except ValueError:
        return None


def request_json(
    url: str,
    *,
    body: Any = None,
    retries: int = 3,
    timeout: float = 20,
    backoff: float = 1.5,
) -> Any:
    """GET ``url`` (or POST ``body`` as JSON) and decode the JSON response.

    Network errors, timeouts, and server errors are retried with exponential
    backoff, honouring ``Retry-After``. Client errors such as 404 raise
    immediately because a wrong board name will not fix itself.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="GET" if data is None else "POST"
    )

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        delay: float | None = None
        try:
            with open_url(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in _PERMANENT_STATUSES:
                raise HttpError(f"HTTP {exc.code}: {url}", exc.code) from exc
            last_error, delay = exc, _retry_after(exc.headers)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            wait = delay if delay is not None else backoff**attempt
            log.debug("retrying %s in %.1fs after %s", url, wait, last_error)
            time.sleep(wait)
    raise HttpError(f"failed after {retries} attempts: {url} ({last_error})")
