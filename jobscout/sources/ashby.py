"""Ashby job boards: jobs.ashbyhq.com/<token>."""

from __future__ import annotations

from typing import Any

from ..models import Company, Job
from ..text import normalize_employment_type, strip_html
from .base import FetchOptions, get_board_json

URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def location(item: dict[str, Any]) -> str:
    """Join the primary and secondary locations, marking fully remote jobs.

    Ashby's ``isRemote`` flag is also set on hybrid jobs, so only
    ``workplaceType == "Remote"`` counts as remote.
    """
    places = [item.get("location") or ""]
    places += [extra.get("location") or "" for extra in item.get("secondaryLocations") or []]
    joined = "; ".join(place for place in places if place)
    if item.get("workplaceType") == "Remote" and "remote" not in joined.lower():
        joined = f"Remote, {joined}" if joined else "Remote"
    return joined


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    return [
        Job(
            id=f"ab-{company.token}-{item['id']}",
            company=company.name,
            title=item.get("title", ""),
            location=location(item),
            url=item.get("jobUrl", ""),
            description=item.get("descriptionPlain") or strip_html(item.get("descriptionHtml")),
            apply_url=item.get("applyUrl", ""),
            posted_at=(item.get("publishedAt") or "")[:10],
            employment_type=normalize_employment_type(item.get("employmentType")),
            country=str(
                ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
            ),
        )
        for item in payload.get("jobs", [])
        if item.get("isListed", True)
    ]


def fetch(company: Company, options: FetchOptions) -> list[Job]:
    return parse(company, get_board_json(URL.format(token=company.token)))
