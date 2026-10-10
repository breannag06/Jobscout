"""Plain data types shared across the package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Company:
    """A company whose public job board is checked on every run.

    ``token`` is the board name for Greenhouse, Lever, and Ashby, and the full
    careers URL for Workday. ``search_terms`` replaces the global Workday searches
    for this company, for example ("albuquerque",) for a retailer with stores
    everywhere.
    """

    name: str
    source: str
    token: str
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    """A single job posting as returned by a company's public job board."""

    id: str
    company: str
    title: str
    location: str
    url: str
    description: str
    apply_url: str = ""
    posted_at: str = ""  # ISO date, or "" when the board does not say
    employment_type: str = ""  # full-time, part-time, internship, contract, temporary, or ""
    country: str = ""  # the board's structured country (code or name), or "" if not given


@dataclass(frozen=True)
class Evaluation:
    """The filter's verdict on one posting.

    ``rejection`` explains a failed posting in plain words. ``reasons`` lists
    what contributed to the score, largest first. A ``near_miss`` is an
    internship that passed every rule except the housing or stipend check: it
    is scored and kept for review, but never alerted.
    """

    passed: bool
    score: int = 0
    rejection: str = ""
    track: str = "other"
    internship: bool = False
    part_time: bool = False
    reasons: tuple[str, ...] = ()
    near_miss: bool = False

    @classmethod
    def reject(cls, why: str) -> Evaluation:
        return cls(passed=False, rejection=why)


@dataclass(frozen=True)
class ScoredJob:
    """A posting that passed every filter, with its evaluation."""

    job: Job
    evaluation: Evaluation

    @property
    def score(self) -> int:
        return self.evaluation.score
