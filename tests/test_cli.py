from jobscout.cli import main
from jobscout.storage import JobStore, manual_id


def test_add_and_mark_from_the_command_line(tmp_path, capsys):
    db = str(tmp_path / "jobs.db")
    url = "https://handshake.example/1"
    assert main(["--db", db, "--add", url, "Research Assistant", "UNM BBER"]) == 0
    assert main(["--db", db, "--add", url, "Research Assistant", "UNM BBER"]) == 0
    assert "already tracked" in capsys.readouterr().out
    assert main(["--db", db, "--mark", manual_id(url), "applied"]) == 0
    assert main(["--db", db, "--mark", manual_id(url), "maybe"]) == 2
    assert main(["--db", db, "--mark", "missing", "skip"]) == 1
    with JobStore(db) as store:
        row = store.get(manual_id(url))
        assert row["status"] == "applied" and row["applied_at"]


def test_bad_config_exits_with_code_2(tmp_path, capsys):
    bad = tmp_path / "config.json"
    bad.write_text("{}")
    assert main(["--config", str(bad), "--check"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def _config_copy(tmp_path, **changes):
    import json

    from helpers import CONFIG_PATH

    raw = json.loads(CONFIG_PATH.read_text())
    raw.update(changes)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    (tmp_path / "secrets.json").write_text(json.dumps({"ntfy_topic": "test-topic"}))
    return path


def test_scheduled_run_crash_sends_failure_alert(tmp_path, monkeypatch):
    from jobscout import cli

    sent = []
    monkeypatch.setattr(cli, "run_daily", lambda *a, **k: (_ for _ in ()).throw(KeyError("jobs")))
    monkeypatch.setattr(cli.NtfyNotifier, "send", lambda self, message: sent.append(message))
    assert main(["--config", str(_config_copy(tmp_path)), "--db", str(tmp_path / "j.db")]) == 1
    assert sent and sent[0].title == "Job finder failed" and "KeyError" in sent[0].body


def test_broken_config_still_sends_failure_alert(tmp_path, monkeypatch):
    from jobscout import cli

    sent = []
    monkeypatch.setattr(cli.NtfyNotifier, "send", lambda self, message: sent.append(message))
    path = _config_copy(tmp_path, daily_alert_time="2:05pm")
    assert main(["--config", str(path), "--db", str(tmp_path / "j.db")]) == 2
    assert "daily_alert_time" in sent[0].body


def test_crash_exits_1_when_the_failure_alert_cannot_be_sent(tmp_path, monkeypatch):
    from jobscout import cli

    def broken_send(self, message):
        raise OSError("offline")

    monkeypatch.setattr(cli, "run_daily", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    monkeypatch.setattr(cli.NtfyNotifier, "send", broken_send)
    assert main(["--config", str(_config_copy(tmp_path)), "--db", str(tmp_path / "j.db")]) == 1


def test_dry_run_never_sends_failure_alerts(tmp_path, monkeypatch):
    from jobscout import cli

    sent = []
    monkeypatch.setattr(cli.NtfyNotifier, "send", lambda self, message: sent.append(message))
    path = _config_copy(tmp_path, daily_alert_time="nope")
    assert main(["--config", str(path), "--dry"]) == 2
    assert sent == []


def test_find_problems_reports_failed_feeds_only(config):
    from jobscout.cli import find_problems
    from jobscout.pipeline import RunReport
    from jobscout.sources import FetchResult
    from jobscout.storage import SyncResult

    good, bad = config.companies[:2]
    report = RunReport([FetchResult(good), FetchResult(bad, error="HTTP 404")], [], SyncResult())
    (problem,) = find_problems(report)
    assert problem.startswith(f"1 company feed failed: {bad.name}.")
    assert find_problems(RunReport([FetchResult(good)], [], SyncResult())) == []

    everything_failed = RunReport([FetchResult(bad, error="offline")], [], SyncResult())
    assert find_problems(everything_failed) == [
        "Every company feed failed. Check the internet connection."
    ]


def test_daily_run_lists_reach_outs_until_confirmed(tmp_path, monkeypatch, config):
    from datetime import datetime, timedelta, timezone

    from helpers import make_job, passed

    from jobscout import cli
    from jobscout.pipeline import RunReport
    from jobscout.storage import JobStore, SyncResult

    db = tmp_path / "jobs.db"
    long_ago = datetime.now(timezone.utc) - timedelta(days=10)
    with JobStore(db) as store:
        store.sync(
            [
                (make_job("Old SDR Intern", "Remote", job_id="old"), passed(5)),
                (make_job("Applied SDR Intern", "Remote", job_id="app"), passed(9)),
            ],
            {"Acme"},
            long_ago,
        )
        store.set_status("app", "applied", long_ago)

    sent = []
    monkeypatch.setattr(cli, "wait_for_network", lambda url: True)
    monkeypatch.setattr(cli, "run", lambda config, store: RunReport([], [], SyncResult()))
    monkeypatch.setattr(cli.NtfyNotifier, "send", lambda self, message: sent.append(message))

    assert cli.run_daily(config, db, dry=False) == 0
    assert cli.run_daily(config, db, dry=False) == 0  # the next day: still listed
    assert (tmp_path / "backups").is_dir()
    for message in sent:
        assert "Waiting for you to apply: 0" in message.body  # the untouched job was removed
        assert "- Acme: Applied SDR Intern" in message.body
        assert "1 reach-out due" in message.title
    with JobStore(db) as store:
        assert store.get("old") is None
        assert store.get("app") is not None  # not archived until confirmed

    assert main(["--db", str(db), "--mark", "app", "follow_up"]) == 0
    with JobStore(db) as store:
        assert store.get("app") is None
        assert [row["title"] for row in store.applications()] == ["Applied SDR Intern"]


def test_scan_records_without_sending(tmp_path, monkeypatch, config):
    from jobscout import cli
    from jobscout.pipeline import RunReport
    from jobscout.storage import SyncResult

    sent, calls = [], []

    def fake_run(config, store, **options):
        calls.append(options)
        return RunReport([], [], SyncResult())

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.NtfyNotifier, "send", lambda self, message: sent.append(message))
    assert cli.scan(config, tmp_path / "jobs.db") == 0
    assert sent == []
    assert calls == [{"include_aggregators": False}]  # Adzuna only runs once a day


def test_tracker_command_writes_a_pdf(tmp_path, capsys):
    target = tmp_path / "tracker.pdf"
    assert main(["--db", str(tmp_path / "jobs.db"), "--tracker", str(target)]) == 0
    assert target.read_bytes().startswith(b"%PDF")


def test_missing_api_keys_get_their_own_instruction(config):
    from jobscout.cli import find_problems
    from jobscout.models import Company
    from jobscout.pipeline import RunReport
    from jobscout.sources import FetchResult
    from jobscout.storage import SyncResult

    adzuna = Company("Adzuna", "adzuna", "us")
    error = "Adzuna needs adzuna_app_id and adzuna_app_key in secrets.json"
    good = config.companies[0]
    report = RunReport([FetchResult(good), FetchResult(adzuna, error=error)], [], SyncResult())
    assert find_problems(report) == [
        "Adzuna is not set up yet: add your App ID and App Key to secrets.json."
    ]


def test_check_only_one_company(monkeypatch, config, capsys):
    from jobscout import cli

    seen = []

    def fake_map_fetch(companies, options, handle):
        seen.extend(c.name for c in companies)
        return []

    monkeypatch.setattr(cli, "map_fetch", fake_map_fetch)
    assert cli.check_feeds(config, ["adzuna"]) == 0
    assert seen == ["Adzuna"]
    assert cli.check_feeds(config, ["Nope"]) == 2
