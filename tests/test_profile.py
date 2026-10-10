import json

import pytest

from jobscout.profile import Profile, ProfileError, load_profile


def test_missing_profile_is_empty(tmp_path):
    assert load_profile(tmp_path / "none.json") == Profile()


def test_resume_for_falls_back_to_other(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "contact": {"Email": ""},
                "resumes": {"sales": "", "data": "/r/data.pdf", "other": "/r/general.pdf"},
            }
        )
    )
    profile = load_profile(path)
    assert profile.sections == {"contact": {"Email": ""}}
    assert profile.resume_for("data") == "/r/data.pdf"
    assert profile.resume_for("sales") == "/r/general.pdf"


def test_bad_profile_shape_rejected(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"contact": {"Email": 5}}))
    with pytest.raises(ProfileError, match=r"contact\.Email"):
        load_profile(path)
