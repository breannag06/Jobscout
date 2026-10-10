import json

import pytest
from helpers import CONFIG_PATH

from jobscout.config import Config, parse_config
from jobscout.filters import JobFilter

# Tests never read the developer's secrets.json, so they behave the same everywhere.
TEST_SECRETS = {"ntfy_topic": "jobscout-tests", "adzuna_app_id": "", "adzuna_app_key": ""}


@pytest.fixture(scope="session")
def config() -> Config:
    return parse_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")), TEST_SECRETS)


@pytest.fixture(scope="session")
def job_filter(config: Config) -> JobFilter:
    return JobFilter(config)
