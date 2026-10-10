import json

import pytest
from helpers import CONFIG_PATH

from jobscout.config import ConfigError, load_config, parse_config


def raw_config(**changes):
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return raw


def test_real_config_loads(config):
    assert "intern" in config.internship_keywords
    assert {"greenhouse", "lever", "ashby", "workday", "adzuna"} >= {
        c.source for c in config.companies
    }
    assert config.adzuna is not None and config.adzuna.where == "Albuquerque, New Mexico"
    assert "sales" in config.tracks


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.json")


def test_old_key_name_is_reported():
    with pytest.raises(ConfigError, match="foreign_locations"):
        parse_config(raw_config(intern_exclude_locations=["london"]))


@pytest.mark.parametrize(
    "key", ["internship_keywords", "title_score_keywords", "us_markers", "tracks", "companies"]
)
def test_missing_key_rejected(key):
    with pytest.raises(ConfigError, match=key):
        parse_config(raw_config(**{key: None}))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"title_score_keywords": {"sales": "high"}}, "integer"),
        ({"role_keywords": ["sales", " "]}, "empty"),
        ({"require_subsidy_for_away_internships": "no"}, "true or false"),
        ({"follow_up_after_days": 0}, "at least 1"),
        ({"daily_alert_time": "2:05pm"}, "24-hour time"),
        ({"daily_alert_time": "24:00"}, "24-hour time"),
        ({"timezone": "Mountain"}, "time zone"),
        ({"top_n": "ten"}, "whole number"),
        ({"tracks": {"sales": []}}, "at least one"),
        (
            {"companies": [{"name": "X", "source": "smartrecruiters", "token": "x"}]},
            "unknown source",
        ),
        ({"companies": [{"name": "X", "source": "workday", "token": "x"}]}, "Workday careers URL"),
        ({"companies": [{"name": "X", "source": "greenhouse"}]}, "missing a token"),
    ],
)
def test_bad_values_rejected(changes, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config(**changes))


def test_duplicate_companies_rejected():
    twice = [
        {"name": "Acme", "source": "greenhouse", "token": "acme"},
        {"name": "ACME", "source": "lever", "token": "acme2"},
    ]
    with pytest.raises(ConfigError, match="listed twice"):
        parse_config(raw_config(companies=twice))


def test_daily_alert_time_parses(config):
    assert config.daily_alert_time == (14, 5)
    assert parse_config(raw_config(daily_alert_time="9:30")).daily_alert_time == (9, 30)


def test_workday_companies_can_have_their_own_searches(config):
    target = next(c for c in config.companies if c.name == "Target")
    assert target.source == "workday" and target.search_terms == ("albuquerque",)
    with pytest.raises(ConfigError, match="only apply to Workday"):
        parse_config(
            raw_config(
                companies=[
                    {
                        "name": "X",
                        "source": "greenhouse",
                        "token": "x",
                        "search_terms": ["albuquerque"],
                    }
                ]
            )
        )


def test_adzuna_keys_come_from_secrets_file_or_environment(tmp_path, monkeypatch):
    import json as _json

    (tmp_path / "config.json").write_text(CONFIG_PATH.read_text(encoding="utf-8"))
    (tmp_path / "secrets.json").write_text(
        _json.dumps({"adzuna_app_id": "a", "adzuna_app_key": "b"})
    )
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    adzuna = load_config(tmp_path / "config.json").adzuna
    assert adzuna is not None and (adzuna.app_id, adzuna.app_key) == ("a", "b")
    monkeypatch.setenv("ADZUNA_APP_KEY", "from-env")
    assert load_config(tmp_path / "config.json").adzuna.app_key == "from-env"


@pytest.mark.parametrize(
    "block, message",
    [
        ({"where": "Albuquerque", "searches": [{"salary": "1"}]}, "may only use"),
        ({"where": "Albuquerque", "searches": []}, "non-empty"),
        ({"searches": [{"what": "sales"}]}, "adzuna.where"),
    ],
)
def test_bad_adzuna_block_rejected(block, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config(adzuna=block))


def test_adzuna_company_needs_its_block():
    with pytest.raises(ConfigError, match="'adzuna' block"):
        parse_config(raw_config(adzuna=None))


def test_ntfy_topic_comes_from_secrets_or_environment(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert parse_config(raw_config(), {"ntfy_topic": "from-secrets"}).ntfy_topic == "from-secrets"
    monkeypatch.setenv("NTFY_TOPIC", "from-env")
    assert parse_config(raw_config(), {"ntfy_topic": "from-secrets"}).ntfy_topic == "from-env"


def test_published_config_has_no_topic():
    assert "ntfy_topic" not in json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
