"""Text helpers shared by the job board parsers and the filter."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

# "3+ years of experience", "2-4 years of relevant experience", "5 or more years
# in sales". The figure must be followed by "experience" within a few words so
# "founded 15 years ago" is ignored. For a range the lower bound is the bar.
_YEARS_RE = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:or more\s+)?years?"
    r"(?:\s+[^\s.;:!?]+){0,4}?\s+experience"
)

_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:no|not|non|without|isn't|aren't|doesn't|don't|never|nor|unable to)\b[^.;:!?]*$",
    re.IGNORECASE,
)
_NEGATION_AFTER_RE = re.compile(
    r"^[^.;:!?]*?\b(?:is not|are not|isn't|aren't|not (?:available|provided|offered|"
    r"included|eligible|supported)|unavailable)\b",
    re.IGNORECASE,
)
_NEGATION_WINDOW = 30

# Boilerplate that usually follows the role description. Phrases in it, such
# as "flexible schedule" under benefits, say nothing about the role itself.
_BOILERPLATE_RE = re.compile(
    r"\b(?:benefits|perks|what we offer|total rewards|compensation|base pay|pay range|"
    r"salary range|equal opportunity)\b",
    re.IGNORECASE,
)
_BOILERPLATE_MIN_FRACTION = 0.3

_EMPLOYMENT_TYPES = {
    "fulltime": "full-time",
    "parttime": "part-time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "temp": "temporary",
}


def strip_html(raw: str | None) -> str:
    """Convert an HTML fragment into collapsed plain text."""
    text = _TAG_RE.sub(" ", html.unescape(raw or ""))
    return _SPACE_RE.sub(" ", text).strip()


def normalize_employment_type(raw: str | None) -> str:
    """Map a board's employment type ("Part time", "PartTime", "Intern") to a
    standard label: full-time, part-time, internship, contract, temporary, or ""."""
    key = re.sub(r"[^a-z]", "", (raw or "").lower())
    return _EMPLOYMENT_TYPES.get(key, "")


def term_pattern(terms: Iterable[str]) -> re.Pattern[str]:
    """Compile a pattern matching any of ``terms`` as a whole word.

    Works like ``\\b`` on both sides, but also handles terms that start or end
    with punctuation such as "u.s.", where a plain ``\\b`` would fail at the end
    of the string. So "uk" does not match "Tukwila", "us" does not match
    "Austin", and "intern" does not match "International". An empty ``terms``
    produces a pattern that never matches.
    """
    longest_first = sorted(set(terms), key=len, reverse=True)
    if not longest_first:
        return re.compile(r"(?!)")
    alternatives = "|".join(re.escape(term) for term in longest_first)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def years_required(text: str) -> int:
    """Return the largest years-of-experience requirement in ``text`` (0 if none)."""
    return max((int(m) for m in _YEARS_RE.findall(text.lower())), default=0)


def role_section(description: str) -> str:
    """Return ``description`` without its trailing benefits and legal boilerplate.

    Cuts at the first boilerplate heading found after the first 30% of the
    text, so an early mention of "benefits" in the pitch does not cut the role.
    """
    start = int(len(description) * _BOILERPLATE_MIN_FRACTION)
    match = _BOILERPLATE_RE.search(description, start)
    return description[: match.start()] if match else description


def is_negated(text: str, start: int, end: int) -> bool:
    """True when the phrase at ``text[start:end]`` is negated in its sentence,
    as in "no relocation assistance" or "relocation is not available"."""
    before = text[max(0, start - _NEGATION_WINDOW) : start]
    after = text[end : end + _NEGATION_WINDOW]
    return bool(_NEGATION_BEFORE_RE.search(before) or _NEGATION_AFTER_RE.search(after))


def find_unnegated(text: str, phrases: Iterable[str]) -> str | None:
    """Return the first of ``phrases`` that appears in ``text`` without being
    negated, or ``None``. Phrases match anywhere, so "stipend" finds "stipends"."""
    for phrase in phrases:
        start = text.find(phrase)
        while start != -1:
            end = start + len(phrase)
            if not is_negated(text, start, end):
                return phrase
            start = text.find(phrase, end)
    return None
