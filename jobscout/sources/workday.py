"""Workday career sites: <tenant>.wd<N>.myworkdayjobs.com/<site>.

Workday has no endpoint that lists every posting with its description, and
big tenants have thousands of postings. So this module runs a few keyword
searches, keeps titles that pass the caller's title filter, and only then
downloads each full posting.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..models import Company, Job
from ..text import normalize_employment_type, strip_html
from .base import FetchOptions, SourceError, get_board_json

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # the largest page Workday accepts
MAX_RESULTS_PER_TERM = 100  # results past this point are rarely relevant
DETAIL_WORKERS = 4  # parallel posting downloads per company, kept low to be polite

_BOARD_RE = re.compile(
    r"^https://(?P<tenant>[\w-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?/(?P<site>[\w-]+)/?$"
)


@dataclass(frozen=True)
class Board:
    tenant: str
    host: str
    site: str

    @property
    def api_root(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_board(url: str) -> Board:
    """Parse a careers URL such as
    https://salesforce.wd12.myworkdayjobs.com/External_Career_Site."""
    match = _BOARD_RE.match(url.strip())
    if not match:
        raise ValueError(
            f"not a Workday careers URL: {url!r} "
            "(expected https://<company>.wd<N>.myworkdayjobs.com/<site>)"
        )
    tenant, pod, site = match.group("tenant", "pod", "site")
    return Board(tenant=tenant, host=f"{tenant}.{pod}.myworkdayjobs.com", site=site)


@dataclass(frozen=True)
class Listing:
    """A search result: enough to decide whether the full posting is worth fetching."""

    path: str
    title: str
    location: str  # Workday's summary, such as "Ireland - Dublin" or "2 Locations"


def search(board: Board, term: str) -> dict[str, Listing]:
    """Return {posting path: listing} for up to ``MAX_RESULTS_PER_TERM`` results."""
    found: dict[str, Listing] = {}
    total = MAX_RESULTS_PER_TERM
    for offset in range(0, MAX_RESULTS_PER_TERM, PAGE_SIZE):
        payload = get_board_json(
            f"{board.api_root}/jobs",
            body={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": term},
        )
        postings = payload.get("jobPostings") or []
        if offset == 0:  # Workday only reports the total on the first page
            total = int(payload.get("total") or 0)
        for posting in postings:
            path = posting.get("externalPath")
            if path and path not in found:
                found[path] = Listing(
                    path, posting.get("title", ""), posting.get("locationsText", "")
                )
        if len(postings) < PAGE_SIZE or offset + PAGE_SIZE >= total:
            break
    return found


_US_NAMES = frozenset({"united states of america", "united states", "usa", "us"})


def _location(info: dict[str, Any]) -> str:
    places = [info.get("location") or "", *(info.get("additionalLocations") or [])]
    joined = "; ".join(place for place in places if place)
    country = (info.get("country") or {}).get("descriptor", "")
    if country and country.lower() not in _US_NAMES and country.lower() not in joined.lower():
        joined = f"{joined}, {country}" if joined else country  # foreign: make it visible
    if "remote" in (info.get("remoteType") or "").lower() and "remote" not in joined.lower():
        joined = f"Remote, {joined}"
    return joined


def _country(info: dict[str, Any]) -> str:
    requisition = (info.get("jobRequisitionLocation") or {}).get("country") or {}
    return str(requisition.get("alpha2Code") or (info.get("country") or {}).get("descriptor", ""))


def parse_posting(company: Company, board: Board, payload: dict[str, Any]) -> Job:
    info = payload.get("jobPostingInfo") or {}
    url = info.get("externalUrl", "")
    return Job(
        id=f"wd-{board.tenant}-{info.get('jobReqId') or info.get('id')}",
        company=company.name,
        title=info.get("title", ""),
        location=_location(info),
        url=url,
        description=strip_html(info.get("jobDescription")),
        apply_url=f"{url}/apply" if url else "",
        posted_at=(info.get("startDate") or "")[:10],
        employment_type=normalize_employment_type(info.get("timeType")),
        country=_country(info),
    )


def fetch(company: Company, options: FetchOptions) -> list[Job]:
    try:
        board = parse_board(company.token)
    except ValueError as exc:
        raise SourceError(str(exc)) from exc
    terms = company.search_terms or options.search_terms
    if not terms:
        raise SourceError("Workday needs workday_search_terms in config.json")

    listings: dict[str, Listing] = {}
    for term in terms:
        for path, listing in search(board, term).items():
            listings.setdefault(path, listing)
    wanted = [
        listing
        for listing in listings.values()
        if options.listing_filter(listing.title, listing.location)
    ]

    def download(listing: Listing) -> Job | None:
        try:
            payload = get_board_json(f"{board.api_root}{listing.path}")
        except SourceError as exc:  # one broken posting should not hide the rest
            log.warning("skipping %s posting %s: %s", company.name, listing.path, exc)
            return None
        return parse_posting(company, board, payload)

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        return [job for job in pool.map(download, wanted) if job is not None]
