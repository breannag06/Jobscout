"""Fetch postings from public job board APIs.

Each board type lives in its own module and exposes ``fetch(company, options)``.
``map_fetch`` checks every company in parallel, processes each board as soon as
it arrives, and never lets one broken board stop the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TypeVar

from ..models import Company, Job
from . import adzuna, ashby, greenhouse, lever, workday
from .base import AdzunaQuery, FetchOptions, SourceError

__all__ = [
    "AGGREGATORS",
    "PARTIAL_SOURCES",
    "SOURCES",
    "AdzunaQuery",
    "FetchOptions",
    "FetchResult",
    "SourceError",
    "fetch_jobs",
    "map_fetch",
]

log = logging.getLogger(__name__)

SOURCES: dict[str, Callable[[Company, FetchOptions], list[Job]]] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workday": workday.fetch,
    "adzuna": adzuna.fetch,
}

# Boards that only return search results, so a missing posting may still be open.
PARTIAL_SOURCES = frozenset({"workday", "adzuna"})
# Search engines covering many employers. They run once a day (call limits), and
# their postings belong to no single company board.
AGGREGATORS = frozenset({"adzuna"})

DEFAULT_WORKERS = 6
R = TypeVar("R")


@dataclass(frozen=True)
class FetchResult:
    """Every posting from one company, or the error that stopped the fetch."""

    company: Company
    jobs: list[Job] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def complete(self) -> bool:
        """True when the fetch succeeded and returned every posting on the board."""
        return self.ok and self.company.source not in PARTIAL_SOURCES


def fetch_jobs(company: Company, options: FetchOptions | None = None) -> list[Job]:
    """Return every current posting for ``company``. Raises ``SourceError``."""
    return SOURCES[company.source](company, options or FetchOptions())


def _fetch_one(company: Company, options: FetchOptions) -> FetchResult:
    try:
        return FetchResult(company, fetch_jobs(company, options))
    except SourceError as exc:
        log.warning("could not fetch %s: %s", company.name, exc)
        return FetchResult(company, error=str(exc))


def map_fetch(
    companies: Sequence[Company],
    options: FetchOptions | None,
    handle: Callable[[FetchResult], R],
    workers: int = DEFAULT_WORKERS,
) -> list[R]:
    """Fetch every company concurrently and pass each result to ``handle`` inside
    its worker thread. Results keep the order of ``companies``.

    Boards are large (one can return 900 postings with full descriptions), so
    ``handle`` should reduce a result to what the caller needs. Then only a few
    boards are ever in memory at once, instead of all of them.
    """
    opts = options or FetchOptions()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda company: handle(_fetch_one(company, opts)), companies))
