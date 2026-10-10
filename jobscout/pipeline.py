"""One complete run: fetch every board, evaluate every posting, record the result."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from .config import Config
from .filters import JobFilter
from .models import Company, Evaluation, Job, ScoredJob
from .sources import AGGREGATORS, FetchOptions, FetchResult, map_fetch
from .storage import JobStore, SyncResult

R = TypeVar("R")
Evaluated = list[tuple[Job, Evaluation]]
# map_fetch, or a stand-in with the same shape in tests.
Fetcher = Callable[[Sequence[Company], FetchOptions, Callable[[FetchResult], R]], list[R]]


@dataclass(frozen=True)
class RunReport:
    results: list[FetchResult]
    matches: list[ScoredJob]
    sync: SyncResult

    @property
    def failures(self) -> list[FetchResult]:
        return [result for result in self.results if not result.ok]

    def summary(self) -> str:
        sync = self.sync
        return (
            f"Checked {len(self.results)} companies ({len(self.failures)} failed). "
            f"{len(self.matches)} postings match: {sync.added} new, {sync.rescored} re-scored, "
            f"{sync.closed} closed, {sync.dropped} no longer match. "
            f"{sync.near_misses_added} new internships need a housing check."
        )


def fetch_options(config: Config, job_filter: JobFilter) -> FetchOptions:
    return FetchOptions(
        search_terms=config.workday_search_terms,
        listing_filter=job_filter.listing_may_match,
        adzuna=config.adzuna,
        known_companies=tuple(c.name for c in config.companies if c.source not in AGGREGATORS),
    )


def evaluate(results: Sequence[FetchResult], job_filter: JobFilter) -> Evaluated:
    """Evaluate every posting from the companies that were fetched successfully."""
    return [
        (job, job_filter.evaluate(job)) for result in results if result.ok for job in result.jobs
    ]


def _slim(job: Job) -> Job:
    """The posting without its description, which is only needed for evaluation."""
    return dataclasses.replace(job, description="")


def _evaluate_and_slim(result: FetchResult, job_filter: JobFilter) -> tuple[FetchResult, Evaluated]:
    pairs = [(_slim(job), verdict) for job, verdict in evaluate([result], job_filter)]
    return dataclasses.replace(result, jobs=[]), pairs


def run(
    config: Config,
    store: JobStore,
    job_filter: JobFilter | None = None,
    fetcher: Fetcher[tuple[FetchResult, Evaluated]] = map_fetch,
    now: datetime | None = None,
    include_aggregators: bool = True,
) -> RunReport:
    """Fetch every board, evaluate each posting as its board arrives, and record the run.

    Scans pass ``include_aggregators=False`` to stay inside search engines' call limits.
    """
    jf = job_filter or JobFilter(config)
    companies = [c for c in config.companies if include_aggregators or c.source not in AGGREGATORS]
    outcomes = fetcher(
        companies, fetch_options(config, jf), lambda result: _evaluate_and_slim(result, jf)
    )
    results = [result for result, _ in outcomes]
    evaluated = [pair for _, pairs in outcomes for pair in pairs]
    sync = store.sync(
        evaluated,
        {result.company.name for result in results if result.ok},
        now,
        complete_companies={result.company.name for result in results if result.complete},
        title_rejection=jf.title_rejection,
    )
    matches = [ScoredJob(job, verdict) for job, verdict in evaluated if verdict.passed]
    return RunReport(results=results, matches=matches, sync=sync)
