"""The application profile: answers that every application asks for.

profile.json holds sections of label-to-text pairs (contact details,
education, eligibility, short answers) plus a ``resumes`` map from track name
to a resume file. The dashboard shows it with copy buttons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class ProfileError(ValueError):
    """Raised when profile.json is not in the expected shape."""


@dataclass(frozen=True)
class Profile:
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    resumes: dict[str, str] = field(default_factory=dict)

    def resume_for(self, track: str) -> str:
        """The resume to use for a track, falling back to the "other" resume."""
        return self.resumes.get(track) or self.resumes.get("other", "")


def _text_map(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where} must be an object of label: text pairs")
    result: dict[str, str] = {}
    for label, text in value.items():
        if not isinstance(text, str):
            raise ProfileError(f"{where}.{label} must be text")
        result[str(label)] = text
    return result


def load_profile(path: Path) -> Profile:
    """Read profile.json. A missing file gives an empty profile."""
    if not path.exists():
        return Profile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profile.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("profile.json must contain a JSON object")
    resumes = _text_map(raw.get("resumes", {}), "resumes")
    sections = {
        str(name): _text_map(value, str(name)) for name, value in raw.items() if name != "resumes"
    }
    return Profile(sections=sections, resumes=resumes)
