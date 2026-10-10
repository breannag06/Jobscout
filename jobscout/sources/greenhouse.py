"""Greenhouse job boards: boards.greenhouse.io/<token>.

The board's list endpoint can return every posting with its full description,
which for a large board is tens of megabytes. Instead, the light list is
fetched first, and descriptions are downloaded only for postings whose title
and location could match. Postings that cannot match keep an empty
description: they are still returned (so closed postings can be detected),
and the filter rejects them on their title or location alone.
"""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..models import Company, Job
from ..text import strip_html
from .base import FetchOptions, get_board_json

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}"
DETAIL_WORKERS = 4


def _job(company: Company, item: dict[str, Any]) -> Job:
    url = item.get("absolute_url", "")
    return Job(
        id=f"gh-{company.token}-{item['id']}",
        company=company.name,
        title=item.get("title", ""),
        location=(item.get("location") or {}).get("name", ""),
        url=url,
        description=strip_html(item.get("content")),
        apply_url=url,  # the application form is on the posting page
        posted_at=(item.get("first_published") or item.get("updated_at") or "")[:10],
    )


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    """Jobs from a list payload (with or without descriptions)."""
    return [_job(company, item) for item in payload.get("jobs", [])]


def fetch(company: Company, options: FetchOptions) -> list[Job]:
    listing = parse(company, get_board_json(LIST_URL.format(token=company.token)))
    wanted = [job for job in listing if options.listing_filter(job.title, job.location)]

    def describe(job: Job) -> Job:
        posting_id = job.id.rsplit("-", 1)[1]
        detail = get_board_json(DETAIL_URL.format(token=company.token, id=posting_id))
        return dataclasses.replace(job, description=strip_html(detail.get("content")))

    # A failed download raises SourceError for the whole board: a posting without
    # its description could be wrongly rejected, so the board is skipped this run.
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        described = {job.id: job for job in pool.map(describe, wanted)}
    return [described.get(job.id, job) for job in listing]
