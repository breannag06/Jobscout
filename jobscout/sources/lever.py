"""Lever job boards: jobs.lever.co/<token>."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import Company, Job
from ..text import normalize_employment_type
from .base import FetchOptions, get_board_json

URL = "https://api.lever.co/v0/postings/{token}?mode=json"


def _iso_date(epoch_ms: Any) -> str:
    if not isinstance(epoch_ms, (int, float)):
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).date().isoformat()


def parse(company: Company, payload: list[dict[str, Any]]) -> list[Job]:
    jobs = []
    for item in payload:
        categories = item.get("categories") or {}
        jobs.append(
            Job(
                id=f"lv-{company.token}-{item['id']}",
                company=company.name,
                title=item.get("text", ""),
                location=categories.get("location") or "",
                url=item.get("hostedUrl", ""),
                description=item.get("descriptionPlain") or "",
                apply_url=item.get("applyUrl", ""),
                posted_at=_iso_date(item.get("createdAt")),
                employment_type=normalize_employment_type(categories.get("commitment")),
            )
        )
    return jobs


def fetch(company: Company, options: FetchOptions) -> list[Job]:
    return parse(company, get_board_json(URL.format(token=company.token)))
