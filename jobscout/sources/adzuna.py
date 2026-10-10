"""Adzuna: a job search engine that covers local employers the company boards miss.

It finds beginner sales jobs around Albuquerque from employers that have no
public job board of their own. Adzuna returns a short snippet of each
description, which is enough for the filter's level and experience checks.

Free accounts allow 250 calls a day, so Adzuna runs only in the daily alert
run, using a few calls. Its terms ask that results credit Adzuna, so jobs from
here are labelled "via Adzuna". Error messages never include the API key.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..models import Company, Job
from ..text import normalize_employment_type, strip_html, term_pattern
from .base import AdzunaQuery, FetchOptions, SourceError, get_board_json

URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
PAGE_SIZE = 50
ID_PREFIX = "az-"


def _location(item: dict[str, Any]) -> tuple[str, str]:
    """(display location, country) from Adzuna's location object."""
    location = item.get("location") or {}
    display = location.get("display_name") or ""
    area = [str(part) for part in location.get("area") or []]
    country = area[0] if area else ""
    state = area[1] if len(area) > 1 else ""
    if state and state.lower() not in display.lower():
        display = f"{display}, {state}" if display else state
    return display, country


def parse(payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload.get("results") or []:
        location, country = _location(item)
        url = item.get("redirect_url", "")
        jobs.append(
            Job(
                id=f"{ID_PREFIX}{item['id']}",
                company=(item.get("company") or {}).get("display_name") or "Unknown employer",
                title=strip_html(item.get("title")),
                location=location,
                url=url,
                description=strip_html(item.get("description")),
                apply_url=url,
                posted_at=(item.get("created") or "")[:10],
                employment_type=normalize_employment_type(item.get("contract_time")),
                country=country,
            )
        )
    return jobs


def _redacted(message: str, query: AdzunaQuery) -> str:
    for secret in (query.app_key, query.app_id):
        if secret:
            message = message.replace(secret, "***")
    return message


def fetch(company: Company, options: FetchOptions) -> list[Job]:
    query = options.adzuna
    if query is None or not query.app_id or not query.app_key:
        raise SourceError("Adzuna needs adzuna_app_id and adzuna_app_key in secrets.json")
    already_checked = term_pattern(name.lower() for name in options.known_companies)

    found: dict[str, Job] = {}
    for search in query.searches:
        for page in range(1, query.pages_per_search + 1):
            params = {
                "app_id": query.app_id,
                "app_key": query.app_key,
                "results_per_page": PAGE_SIZE,
                "where": query.where,
                "distance": query.distance_km,
                "max_days_old": query.max_days_old,
                "sort_by": "date",
                "content-type": "application/json",
                **dict(search),
            }
            url = f"{URL.format(country=company.token, page=page)}?{urlencode(params)}"
            try:
                payload = get_board_json(url)
            except SourceError as exc:
                raise SourceError(_redacted(str(exc), query)) from None
            jobs = parse(payload)
            for job in jobs:
                if job.id not in found and not already_checked.search(job.company.lower()):
                    found[job.id] = job
            if len(payload.get("results") or []) < PAGE_SIZE:
                break
    return list(found.values())
