"""Configuration loading and validation.

Every problem in config.json is reported as a ``ConfigError`` with a message
that names the key, so a typo fails loudly at startup instead of silently
changing which jobs match.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Company
from .sources import SOURCES, AdzunaQuery
from .sources.workday import parse_board

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Keys from older config files and what replaced them.
_RENAMED_KEYS = {
    "location_keywords": "local_keywords and remote_keywords",
    "intern_exclude_locations": "foreign_locations",
}


class ConfigError(ValueError):
    """Raised when config.json is missing a field or contains a bad value."""


@dataclass(frozen=True)
class Config:
    ntfy_server: str
    ntfy_topic: str
    top_n: int
    companies: tuple[Company, ...]
    level_keywords: tuple[str, ...]
    role_keywords: tuple[str, ...]
    internship_only_role_keywords: tuple[str, ...]  # role words that count only for internships
    exclude_title_keywords: tuple[str, ...]
    internship_keywords: tuple[str, ...]
    local_keywords: tuple[str, ...]
    remote_keywords: tuple[str, ...]
    part_time_keywords: tuple[str, ...]
    foreign_locations: tuple[str, ...]
    us_markers: tuple[str, ...]
    subsidy_keywords: tuple[str, ...]
    require_subsidy_for_away_internships: bool
    max_years_required: int
    score_keywords: dict[str, int]
    title_score_keywords: dict[str, int]
    tracks: dict[str, tuple[str, ...]]
    workday_search_terms: tuple[str, ...]
    follow_up_after_days: int
    remove_untouched_after_days: int
    daily_alert_time: tuple[int, int]  # (hour, minute) in ``timezone``
    timezone: str  # IANA name such as "America/Denver"; follows daylight saving time
    adzuna: AdzunaQuery | None = None  # None when config.json has no "adzuna" block


def _require(raw: dict[str, Any], key: str) -> Any:
    if key not in raw:
        raise ConfigError(f"config is missing required key: {key!r}")
    return raw[key]


def _string_list(value: Any, key: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{key!r} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key!r} contains an empty or non-string entry: {item!r}")
        items.append(item.strip().lower())
    if not items and not allow_empty:
        raise ConfigError(f"{key!r} must contain at least one entry")
    return tuple(items)


def _lower_tuple(raw: dict[str, Any], key: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    """Return ``raw[key]`` as a tuple of lowercase, stripped, non-blank strings."""
    return _string_list(_require(raw, key), key, allow_empty=allow_empty)


def _weights(raw: dict[str, Any], key: str) -> dict[str, int]:
    """Return ``raw[key]`` as a mapping of lowercase keyword to integer weight."""
    value = _require(raw, key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key!r} must map keywords to integer weights")
    weights: dict[str, int] = {}
    for word, weight in value.items():
        if not str(word).strip():
            raise ConfigError(f"{key!r} contains an empty keyword")
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise ConfigError(f"{key!r} weight for {word!r} must be an integer")
        weights[str(word).strip().lower()] = weight
    return weights


def _tracks(raw: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    value = _require(raw, "tracks")
    if not isinstance(value, dict) or not value:
        raise ConfigError("'tracks' must map a track name to a list of title keywords")
    return {
        str(name).strip().lower(): _string_list(terms, f"tracks.{name}", allow_empty=False)
        for name, terms in value.items()
    }


def _flag(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key!r} must be true or false")
    return value


def _positive_int(raw: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key!r} must be a whole number of at least {minimum}")
    return value


def _clock_time(raw: dict[str, Any], key: str, default: str) -> tuple[int, int]:
    value = raw.get(key, default)
    match = _TIME_RE.match(value) if isinstance(value, str) else None
    if not match:
        raise ConfigError(f'{key!r} must be a 24-hour time such as "14:05"')
    return int(match.group(1)), int(match.group(2))


def _timezone(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f'{key!r} must be a time zone name such as "America/Denver"') from exc
    return str(value)


_ADZUNA_SEARCH_KEYS = frozenset({"what", "what_or", "what_exclude", "category", "part_time"})


def _adzuna(raw: dict[str, Any], secrets: dict[str, Any]) -> AdzunaQuery | None:
    block = raw.get("adzuna")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ConfigError("'adzuna' must be an object")
    searches = block.get("searches")
    if not isinstance(searches, list) or not searches:
        raise ConfigError("'adzuna.searches' must be a non-empty list of objects")
    parsed = []
    for search in searches:
        if not isinstance(search, dict) or not search or not set(search) <= _ADZUNA_SEARCH_KEYS:
            raise ConfigError(
                f"each adzuna search may only use {sorted(_ADZUNA_SEARCH_KEYS)}: {search!r}"
            )
        parsed.append(tuple((str(k), "1" if v is True else str(v)) for k, v in search.items()))
    where = block.get("where")
    if not isinstance(where, str) or not where.strip():
        raise ConfigError("'adzuna.where' must be a place such as \"Albuquerque, New Mexico\"")
    return AdzunaQuery(
        app_id=str(os.environ.get("ADZUNA_APP_ID") or secrets.get("adzuna_app_id") or ""),
        app_key=str(os.environ.get("ADZUNA_APP_KEY") or secrets.get("adzuna_app_key") or ""),
        where=where.strip(),
        distance_km=_positive_int(block, "distance_km", 20),
        max_days_old=_positive_int(block, "max_days_old", 21),
        searches=tuple(parsed),
        pages_per_search=_positive_int(block, "pages_per_search", 2),
    )


def _parse_company(entry: Any) -> Company:
    if not isinstance(entry, dict):
        raise ConfigError(f"each company must be an object, got {entry!r}")
    name, source, token = entry.get("name"), entry.get("source", ""), entry.get("token")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"company entry is missing a name: {entry!r}")
    if not isinstance(token, str) or not token.strip():
        raise ConfigError(f"company {name!r} is missing a token")
    if source not in SOURCES:
        raise ConfigError(
            f"company {name!r} has unknown source {source!r}; expected one of {sorted(SOURCES)}"
        )
    if source == "workday":
        try:
            parse_board(token)
        except ValueError as exc:
            raise ConfigError(f"company {name!r}: {exc}") from exc
    search_terms: tuple[str, ...] = ()
    if "search_terms" in entry:
        if source != "workday":
            raise ConfigError(f"company {name!r}: search_terms only apply to Workday companies")
        search_terms = _string_list(
            entry["search_terms"], f"{name}.search_terms", allow_empty=False
        )
    return Company(name=name.strip(), source=source, token=token.strip(), search_terms=search_terms)


def _companies(raw: dict[str, Any]) -> tuple[Company, ...]:
    entries = _require(raw, "companies")
    if not isinstance(entries, list):
        raise ConfigError("'companies' must be a list")
    companies = tuple(_parse_company(entry) for entry in entries)
    if any(c.source == "adzuna" for c in companies) and "adzuna" not in raw:
        raise ConfigError("an adzuna company needs an 'adzuna' block with its searches")
    boards: set[tuple[str, str]] = set()
    names: set[str] = set()
    for company in companies:
        board, name = (company.source, company.token.lower()), company.name.lower()
        if board in boards or name in names:  # stored postings are matched by name
            raise ConfigError(f"company {company.name!r} is listed twice")
        boards.add(board)
        names.add(name)
    return companies


def parse_config(raw: dict[str, Any], secrets: dict[str, Any] | None = None) -> Config:
    """Validate an already-decoded config mapping. ``secrets`` holds API keys."""
    for old_key, new_keys in _RENAMED_KEYS.items():
        if old_key in raw:
            raise ConfigError(f"config key {old_key!r} was replaced by {new_keys}")

    return Config(
        ntfy_server=str(raw.get("ntfy_server", "https://ntfy.sh")).rstrip("/"),
        ntfy_topic=str(
            os.environ.get("NTFY_TOPIC")
            or (secrets or {}).get("ntfy_topic")
            or raw.get("ntfy_topic", "")
        ),
        top_n=_positive_int(raw, "top_n", 10),
        companies=_companies(raw),
        level_keywords=_lower_tuple(raw, "level_keywords", allow_empty=False),
        role_keywords=_lower_tuple(raw, "role_keywords", allow_empty=False),
        internship_only_role_keywords=_lower_tuple(raw, "internship_only_role_keywords"),
        exclude_title_keywords=_lower_tuple(raw, "exclude_title_keywords"),
        internship_keywords=_lower_tuple(raw, "internship_keywords", allow_empty=False),
        local_keywords=_lower_tuple(raw, "local_keywords", allow_empty=False),
        remote_keywords=_lower_tuple(raw, "remote_keywords", allow_empty=False),
        part_time_keywords=_lower_tuple(raw, "part_time_keywords"),
        foreign_locations=_lower_tuple(raw, "foreign_locations"),
        us_markers=_lower_tuple(raw, "us_markers", allow_empty=False),
        subsidy_keywords=_lower_tuple(raw, "subsidy_keywords"),
        require_subsidy_for_away_internships=_flag(
            raw, "require_subsidy_for_away_internships", default=True
        ),
        max_years_required=_positive_int(raw, "max_years_required", 2, minimum=0),
        score_keywords=_weights(raw, "score_keywords"),
        title_score_keywords=_weights(raw, "title_score_keywords"),
        tracks=_tracks(raw),
        workday_search_terms=_lower_tuple(raw, "workday_search_terms"),
        follow_up_after_days=_positive_int(raw, "follow_up_after_days", 7),
        remove_untouched_after_days=_positive_int(raw, "remove_untouched_after_days", 7),
        daily_alert_time=_clock_time(raw, "daily_alert_time", "14:05"),
        timezone=_timezone(raw, "timezone", "America/Denver"),
        adzuna=_adzuna(raw, secrets or {}),
    )


def load_config(path: Path) -> Config:
    """Read and validate the JSON config file at ``path``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a JSON object")
    return parse_config(raw, load_secrets(path.parent / SECRETS_FILE))


SECRETS_FILE = "secrets.json"


def load_secrets(path: Path) -> dict[str, Any]:
    """API keys kept out of config.json (and out of alerts and logs)."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name} must contain a JSON object")
    return value
