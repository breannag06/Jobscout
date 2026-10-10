"""Decide whether a posting is relevant and how well it matches.

Rules, in order:
  1. Reject titles with an excluded keyword (seniority, graduate degree,
     engineering) or that only name past years.
  2. Require an internship, student, or entry-level signal in the title, the
     start of the description, or the board's employment type.
  3. Require a target role keyword in the title. Some (such as "operations")
     count only for internships, so retail stock jobs do not match.
  4. Location, for a full-time student with no car living in Albuquerque:
       - outside the US: rejected for every role
       - Albuquerque: any role
       - remote or unlisted: internships, and other roles only when part-time
       - elsewhere in the US: internships that mention housing, relocation, or
         a stipend (the subsidy check can be turned off in config). Internships
         that fail only this check are kept as near misses for a manual look.
  5. Reject postings that ask for more experience than allowed.

Keyword lists match whole words. Subsidy and part-time phrases match anywhere
but are ignored when negated ("no relocation", "not a part-time role"), and
part-time phrases in the benefits section do not count.
"""

from __future__ import annotations

import re
from datetime import date

from .config import Config
from .models import Evaluation, Job
from .text import find_unnegated, role_section, term_pattern, years_required

INTERNSHIP_BONUS = 5
PART_TIME_BONUS = 4  # a job that fits around classes
NO_SUBSIDY = "internship away from Albuquerque with no housing, relocation, or stipend"
_DESCRIPTION_HEAD_CHARS = 600
_MAX_REASONS = 6
_STUDENT_EMPLOYMENT_TYPES = frozenset({"internship", "part-time"})
_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)(?!\d)")
_US_COUNTRIES = frozenset(
    {"us", "usa", "united states", "united states of america", "pr", "puerto rico"}
)

_Weighted = list[tuple[str, re.Pattern[str], int]]


def _weighted(weights: dict[str, int]) -> _Weighted:
    return [(word, term_pattern((word,)), weight) for word, weight in weights.items()]


class JobFilter:
    """Applies the criteria from config and scores matching postings."""

    def __init__(self, config: Config, today: date | None = None) -> None:
        self._config = config
        self._year = (today or date.today()).year
        self._body_weights = _weighted(config.score_keywords)
        self._title_weights = _weighted(config.title_score_keywords)
        self._exclude_re = term_pattern(config.exclude_title_keywords)
        self._level_re = term_pattern(config.level_keywords)
        self._role_re = term_pattern(config.role_keywords)
        internship_only = set(config.internship_only_role_keywords)
        self._job_role_re = term_pattern(
            term for term in config.role_keywords if term not in internship_only
        )
        self._intern_re = term_pattern(config.internship_keywords)
        self._local_re = term_pattern(config.local_keywords)
        self._remote_re = term_pattern(config.remote_keywords)
        self._foreign_re = term_pattern(config.foreign_locations)
        self._us_re = term_pattern(config.us_markers)
        self._tracks = [(name, term_pattern(terms)) for name, terms in config.tracks.items()]

    # Title-only checks: used before downloading full postings (Workday) and to
    # re-check stored postings that a run did not return.
    def title_rejection(self, title: str) -> str:
        """Why ``title`` fails the title-only rules (1 and 3), or "" if it passes."""
        lowered = title.lower()
        if exclusion := self._title_exclusion(lowered):
            return exclusion
        return "" if self._role_re.search(lowered) else "not a target role"

    def _title_exclusion(self, lowered: str) -> str:
        """Rule 1: an excluded keyword, or a title that only names past years
        ("Summer 2026 Intern" once it is 2027)."""
        if excluded := self._exclude_re.search(lowered):
            return f"excluded title keyword ({excluded.group()})"
        years = [int(year) for year in _YEAR_RE.findall(lowered)]
        if years and max(years) < self._year:
            return f"for a past year ({max(years)})"
        return ""

    def title_may_match(self, title: str) -> bool:
        """True when ``title`` passes the title-only rules (1 and 3)."""
        return not self.title_rejection(title)

    def listing_may_match(self, title: str, location: str) -> bool:
        """True unless the title fails, or the listing is clearly outside the US."""
        return self.title_may_match(title) and not self._is_foreign(f"{location} {title}".lower())

    def _is_foreign(self, place: str, country: str = "") -> bool:
        """Foreign when the place text or the board's country says so, and no US
        marker appears (multi-location postings that include the US stay US)."""
        if country:  # the board's structured country is authoritative
            return country.lower() not in _US_COUNTRIES and not self._us_re.search(place)
        return bool(self._foreign_re.search(place)) and not self._us_re.search(place)

    def is_internship(self, title: str, employment_type: str = "") -> bool:
        """True for an internship title or an internship employment type."""
        return employment_type == "internship" or bool(self._intern_re.search(title.lower()))

    def track(self, title: str) -> str:
        """The first configured track whose keywords appear in ``title``."""
        lowered = title.lower()
        return next((name for name, pattern in self._tracks if pattern.search(lowered)), "other")

    def score(self, job: Job) -> int | None:
        """Return the relevance score, or ``None`` if the posting is rejected."""
        verdict = self.evaluate(job)
        return verdict.score if verdict.passed else None

    def evaluate(self, job: Job) -> Evaluation:
        """Apply every rule to ``job`` and explain the outcome."""
        title = job.title.lower()
        location = job.location.lower()
        description = job.description.lower()
        employment = job.employment_type

        if exclusion := self._title_exclusion(title):
            return Evaluation.reject(exclusion)

        head = description[:_DESCRIPTION_HEAD_CHARS]
        if not (
            employment in _STUDENT_EMPLOYMENT_TYPES
            or self._level_re.search(title)
            or self._level_re.search(head)
        ):
            return Evaluation.reject("no internship, student, or entry-level signal")

        internship = self.is_internship(title, employment)
        role_re = self._role_re if internship else self._job_role_re
        if not role_re.search(title):
            return Evaluation.reject("not a target role")

        # The board's own employment type wins; text is only consulted when it is unknown.
        part_time = employment == "part-time" or (
            employment != "full-time"
            and find_unnegated(
                f"{title} {role_section(description)}", self._config.part_time_keywords
            )
            is not None
        )
        problem = self._location_problem(
            location, title, description, internship, part_time, job.country
        )
        if problem and problem != NO_SUBSIDY:
            return Evaluation.reject(problem)

        years = years_required(description)
        if years > self._config.max_years_required:
            return Evaluation.reject(f"asks for {years} years of experience")

        score, reasons = self._score(title, description, internship, part_time)
        return Evaluation(
            passed=not problem,
            score=score,
            rejection=problem,
            track=self.track(title),
            internship=internship,
            part_time=part_time,
            reasons=reasons,
            near_miss=bool(problem),
        )

    def _score(
        self, title: str, description: str, internship: bool, part_time: bool
    ) -> tuple[int, tuple[str, ...]]:
        text = f"{title} {description}"
        parts = [(f"title: {w}", wt) for w, p, wt in self._title_weights if p.search(title)]
        parts += [(w, wt) for w, p, wt in self._body_weights if p.search(text)]
        if internship:
            parts.append(("internship", INTERNSHIP_BONUS))
        if part_time:
            parts.append(("part-time", PART_TIME_BONUS))
        total = sum(weight for _, weight in parts)
        ranked = sorted(parts, key=lambda part: abs(part[1]), reverse=True)[:_MAX_REASONS]
        return total, tuple(f"{label} {weight:+d}" for label, weight in ranked)

    def _location_problem(
        self,
        location: str,
        title: str,
        description: str,
        internship: bool,
        part_time: bool,
        country: str = "",
    ) -> str:
        """Return why the location rules reject the posting, or "" if they pass."""
        if self._is_foreign(f"{location} {title}", country):
            return "outside the US"
        if self._local_re.search(location):
            return ""
        if not location or self._remote_re.search(location):
            return "" if internship or part_time else "remote or unlisted, but full-time"
        if not internship:
            return "outside Albuquerque and not an internship"
        if not self._config.require_subsidy_for_away_internships:
            return ""
        if find_unnegated(description, self._config.subsidy_keywords):
            return ""
        return NO_SUBSIDY
