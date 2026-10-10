"""Shared builders for the test suite."""

from __future__ import annotations

from pathlib import Path

from jobscout.models import Evaluation, Job

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


def make_job(
    title: str,
    location: str,
    description: str = "python sql data",
    *,
    job_id: str = "t-1",
    company: str = "Acme",
    employment_type: str = "",
) -> Job:
    return Job(
        job_id,
        company,
        title,
        location,
        f"https://example.com/{job_id}",
        description,
        employment_type=employment_type,
    )


def passed(score: int, **fields: object) -> Evaluation:
    return Evaluation(passed=True, score=score, **fields)  # type: ignore[arg-type]
