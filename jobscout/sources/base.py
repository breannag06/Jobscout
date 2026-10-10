"""Shared pieces for the job board modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..http import HttpError, request_json


class SourceError(RuntimeError):
    """Raised when a job board cannot be fetched or parsed."""


def _accept_all(title: str, location: str) -> bool:
    return True


@dataclass(frozen=True)
class AdzunaQuery:
    """Credentials and searches for the Adzuna job search API."""

    app_id: str
    app_key: str
    where: str
    distance_km: int
    max_days_old: int
    searches: tuple[tuple[tuple[str, str], ...], ...]  # each search: (param, value) pairs
    pages_per_search: int = 2


@dataclass(frozen=True)
class FetchOptions:
    """Hints for boards that cannot return every posting in one request.

    Workday only supports keyword search, so it runs ``search_terms`` and
    downloads full descriptions only for listings whose title and location
    pass ``listing_filter``. Adzuna, a search engine covering many employers,
    needs ``adzuna`` and skips employers in ``known_companies``, which are
    already checked directly. The other boards ignore these options.
    """

    search_terms: tuple[str, ...] = ()
    listing_filter: Callable[[str, str], bool] = _accept_all
    adzuna: AdzunaQuery | None = None
    known_companies: tuple[str, ...] = ()


def get_board_json(url: str, *, body: Any = None) -> Any:
    """Fetch a board's JSON, turning HTTP failures into ``SourceError``."""
    try:
        return request_json(url, body=body)
    except HttpError as exc:
        if exc.status == 404:
            raise SourceError(f"board not found (HTTP 404): {url}") from exc
        raise SourceError(str(exc)) from exc
