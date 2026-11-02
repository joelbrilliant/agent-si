"""Config rules for the services, the network guard, and setup and check."""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain_fetch import net
from tests import fetch_net as fn
from tests.phase2a import custom_config, load_text
from tests.test_fetch_gmail import AGENTS as MAIL_AGENTS, COLLECTORS as MAIL, world as mail_world
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

CAL_AGENT = {"workcal": {"kind": "collector", "operations": ["add_source", "upsert_thread", "upsert_meeting",
                                                             "record_collector_run"]}}
SLACK_AGENT = {"workslack": {"kind": "collector", "operations": ["add_source", "record_collector_run"]}}


def _load(tmp_path, collectors, agents):
    return load_text(tmp_path, custom_config(extra_collectors=collectors, agents=agents))


def _error(tmp_path, collectors, agents) -> str:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, collectors, agents)
    return str(caught.value)


def _mail(**changes) -> dict:
    return {"work-mail": {**MAIL["work-mail"], **changes}, "work-mail-threads": MAIL["work-mail-threads"]}


def _slack(**changes) -> dict:
    return {"work-slack": {"type": "capture", "agent": "workslack", "origin": "work-slack", "kinds": ["slack"],
                           "service": "slack", "live_channels": ["C0PILOT01"], **changes}}


def test_a_good_config_loads(tmp_path) -> None:
    cfg = _load(tmp_path, {**MAIL, "work-calendar": {"type": "calendar", "agent": "workcal", "account": "work",
                                                     "live_calendar_id": "team@group.calendar.google.com"}},
                {**MAIL_AGENTS, **CAL_AGENT})
    assert cfg.collectors["work-mail"].options["service"] == "gmail"
    assert cfg.collectors["work-mail-threads"].options["with_subject"] is False
    assert cfg.collectors["work-calendar"].options == {"account": "work", "live_calendar_id": "team@group.calendar.google.com",
                                                       "live_window_days": 14}


@pytest.mark.parametrize("collectors, agents, message", [
    (_mail(kinds=["document"]), MAIL_AGENTS, 'collectors.work-mail.kinds must be ["email"] for the gmail service'),
    (_mail(live_folder="~/Mail"), MAIL_AGENTS, "collectors.work-mail." "live_folder is only for the documents service"),
    (_mail(repo="example-co/handbook"), MAIL_AGENTS, "collectors.work-mail.repo is only for the github service"),
    (_mail(live_channels=["C0PILOT01"]), MAIL_AGENTS,
     "collectors.work-mail." "live_channels and include_dms are only for the slack service"),
    (_mail(threads="nobody"), MAIL_AGENTS, "collectors.work-mail.threads must name a gmail collector"),
    ({**_mail(), "work-mail-threads": {**MAIL["work-mail-threads"], "with_subject": True}}, MAIL_AGENTS,
     "collectors.work-mail-threads.with_subject must be false: with capture on, subjects live only in the capture source"),
    ({"work-mail": {k: v for k, v in MAIL["work-mail"].items() if k != "threads"}, "other": {"type": "capture", "agent": "workmailthreads",
      "origin": "other", "live_query": "in:inbox"}},
     {**MAIL_AGENTS, "workmailthreads": {"kind": "collector", "operations": ["add_source", "record_collector_run"]}},
     "collectors.other." "live_query is not used by the documents service"),
    (_slack(live_channels=[]), SLACK_AGENT, "collectors.work-slack." "live_channels must be 1 to 100 different Slack channel ids, such as C0123ABCD"),
    (_slack(live_channels=["general"]), SLACK_AGENT, "collectors.work-slack." "live_channels must be 1 to 100 different Slack channel ids, such as C0123ABCD"),
    (_slack(live_channels=[f"C{n:08d}" for n in range(101)]), SLACK_AGENT,
     "collectors.work-slack." "live_channels must be 1 to 100 different Slack channel ids, such as C0123ABCD"),
    (_slack(threads="work-mail-threads"), SLACK_AGENT, "collectors.work-slack.threads must name a slack collector"),
    (_slack(service="github", kinds=["document"]), SLACK_AGENT,
     "collectors.work-slack.repo is required for the github service"),
    (_slack(live_query="xoxp-123456789012-example"), SLACK_AGENT,  # release-check: allow (an invented token the test refuses)
     "collectors.work-slack." "live_query looks like a credential; credentials never go in brain.toml"),
    ({"work-calendar": {"type": "calendar", "agent": "workcal", "account": "work", "live_window_days": 91}}, CAL_AGENT,
     "collectors.work-calendar." "live_window_days must be a whole number from 1 to 90"),
    ({"work-calendar": {"type": "calendar", "agent": "workcal", "account": "work", "live_calendar_id": "a\nb"}}, CAL_AGENT,
     "collectors.work-calendar." "live_calendar_id must be one line of 1 to 500 characters"),
])
def test_config_refuses_what_doesnt_fit(tmp_path, collectors, agents, message) -> None:
    assert _error(tmp_path, collectors, agents) == message


def test_slack_needs_channels_or_dms(tmp_path) -> None:
    collectors = {"work-slack": {"type": "capture", "agent": "workslack", "origin": "work-slack", "kinds": ["slack"],
                                 "service": "slack"}}
    assert _error(tmp_path, collectors, SLACK_AGENT) == (
        "collectors.work-slack." "live_channels is required for the slack service, unless include_dms is true")
    collectors["work-slack"]["include_dms"] = True
    assert _load(tmp_path, collectors, SLACK_AGENT).collectors["work-slack"].options["include_dms"] is True


def test_gmail_live_keys_stay_refused_on_the_metadata_type(tmp_path) -> None:
    collectors = {**MAIL, "work-mail-threads": {**MAIL["work-mail-threads"], "live_query": "in:inbox"}}
    assert _error(tmp_path, collectors, MAIL_AGENTS) == (
        "collectors.work-mail-threads." "live_query is for the live adapter, which is disabled until approved")


# -- the network guard --------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://slack.com/api/auth.test", "https://example.com/api", "https://slack.com:8443/api",
                                 "https://user@slack.com/api", "https://slack.com.example.com/api", "file:///etc/passwd"])
def test_only_https_to_the_listed_hosts(monkeypatch, url) -> None:
    fake = fn.FakeNet(monkeypatch)
    with pytest.raises(net.NetError) as caught:
        net.request("GET", url, token="token-example")
    assert caught.value.error_class == "config" and fake.calls == []


@pytest.mark.parametrize("answer, cause, words", [
    ((302, b"", {"Location": "https://example.com/steal"}), "network", "never follows one"),
    ((401, {}), "auth", "refused the credential (401)"),
    ((403, {}), "auth", "refused the credential (403)"),
    ((400, {"error": "x"}), "other", "refused the request (400)"),
    ((500, b""), "network", "had an error (500)"),
    ((200, b"<html>not json</html>"), "network", "isn't JSON"),
    ((200, b"x" * (net.MAX_BYTES + 1)), "limit", "over 32 MiB"),
    (OSError("connection reset to https://slack.com/api?token=token-example"), "network", "connection failed (OSError)"),
])
def test_failures_have_a_class_and_never_the_token(monkeypatch, answer, cause, words) -> None:
    fake = fn.FakeNet(monkeypatch)
    fake.route("GET", r"slack\.com/api/auth\.test", answer)
    with pytest.raises(net.NetError) as caught:
        net.request("GET", "https://slack.com/api/auth.test", token="token-example")
    assert caught.value.error_class == cause and words in str(caught.value)
    assert "token-example" not in str(caught.value) and len(fake.calls) == 1


def test_redirects_are_never_followed_by_urllib() -> None:
    handler = net._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/") is None


# -- setup and check ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("service, words", [
    ("gmail", ["https://www.googleapis.com/auth/gmail.readonly", "expire after 7 days", "Workspace",
               "security add-generic-password -U -s agent-si/demo/work-mail -a work-mail -w", 'service = "gmail"',
               "with_subject = false", "author_principal = true"]),
    ("slack", ["channels:history, groups:history and users:read", "im:history and im:read only with include_dms",
               "Never distribute it", 'live_channels = ["C0123ABCD"]', "agent-si/demo/work-slack"]),
    ("calendar", ["https://www.googleapis.com/auth/calendar.readonly", 'type = "calendar"', 'live_calendar_id = "primary"',
                  "when you organised it"]),
    ("github", ["Contents: Read-only", 'service = "github"', 'repo = "example-co/handbook"', "4. brain apply-config"]),
    ("documents", ["No sign-in is needed", "live_folder"]),
])
def test_setup_prints_each_guide(tmp_path, monkeypatch, service, words) -> None:
    fn.no_secrets(monkeypatch, tmp_path)
    inst = fn.instance(tmp_path, {}, {})
    code, _, err = fn.bf("setup", "--instance", str(inst), service)
    assert code == 0 and err == ""
    import contextlib
    import io

    from brain_fetch.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["setup", "--instance", str(inst), service])
    for word in words:
        assert word in out.getvalue(), word


def test_setup_for_a_configured_collector_uses_its_service(tmp_path, monkeypatch) -> None:
    fn.no_secrets(monkeypatch, tmp_path)
    inst = fn.instance(tmp_path, MAIL, MAIL_AGENTS)
    import contextlib
    import io

    from brain_fetch.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["setup", "--instance", str(inst), "work-mail"]) == 0
    assert "Gmail for demo" in out.getvalue() and "agent-si/demo/work-mail " in out.getvalue()


def test_check_tests_the_credential(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = mail_world(tmp_path, monkeypatch)
    code, payload, _ = fn.bf("check", "--instance", str(inst), "work-mail")
    entry = payload["collectors"][0]
    assert (code, entry["service"], entry["credential"], entry["token"], entry["problems"]) == (0, "gmail", "found", "ok", [])
    fake.route("POST", r"oauth2\.googleapis\.com/token", (400, {"error": "invalid_grant"}))
    code, payload, _ = fn.bf("check", "--instance", str(inst), "work-mail")
    entry = payload["collectors"][0]
    assert code == 1 and entry["token"].startswith("failed (auth: Google refused the refresh token")
    (tmp_path / "empty").mkdir()
    fn.no_secrets(monkeypatch, tmp_path / "empty")
    code, payload, _ = fn.bf("check", "--instance", str(inst), "work-mail")
    entry = payload["collectors"][0]
    assert code == 1 and entry["credential"].startswith("missing (") and entry["token"] == "not tested"
    assert entry["problems"] == ["no credential; brain-fetch setup gmail says where it goes"]
    assert "refresh-example" not in str(payload) and fn.ACCESS not in str(payload)


@pytest.mark.parametrize("service", ["gmail", "slack", "calendar", "github"])
def test_the_config_each_guide_prints_loads(tmp_path, monkeypatch, service) -> None:
    from brain_fetch import guides

    text = guides.guide(service, "demo", "/tmp/demo", guides.DEFAULT_NAMES[service])
    lines = text.splitlines()
    start = next(i for i, item in enumerate(lines) if item.startswith("[agents."))
    end = next(i for i, item in enumerate(lines) if i > start and item.split(". ", 1)[-1].startswith("brain apply-config"))
    block = "\n".join(lines[start:end])
    cfg = load_text(tmp_path, custom_config(extra_toml=block))
    name = guides.DEFAULT_NAMES[service]
    assert cfg.collectors[name].type == ("calendar" if service == "calendar" else "capture")


def test_a_quiet_service_collector_shows_as_silent(tmp_path, monkeypatch, clock: Clock) -> None:
    from brain import config as config_mod, reads, store
    from tests.conftest import connect_readonly

    inst, fake = mail_world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-mail")[0] == 0
    clock.at("2026-10-09T03:00:00Z")
    cfg = config_mod.load_config(inst / "brain.toml")
    con = connect_readonly(inst / "brain.sqlite3")
    try:
        entries = reads.intake_status(con, store.utc_now(), cfg)
    finally:
        con.close()
    assert [(entry["collector"], entry["state"]) for entry in entries] == [("work-mail", "silent")]
