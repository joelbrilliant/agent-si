from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain import schemas
from brain import truth as truth_mod
from brain.evidence_gate import match_key
from brain.text import NotOneLine, has_line_break, single_line
from brain.collectors import REGISTRY
from brain.collectors import github as github_collectors
from brain.collectors import vanta as vanta_collectors

RESERVED_AGENT = "brain"

_AGENT_NAME = re.compile(schemas.AGENT)
_PROJECT_ID = re.compile(schemas.PROJECT_ID)
_PERSON_ID = re.compile(schemas.PERSON_ID)
_FACT_KEY = re.compile(schemas.FACT_KEY)
_PRINCIPAL_NAME = re.compile(schemas.PRINCIPAL_NAME)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    person_id: str
    display_name: str
    names: tuple[str, ...]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Agent:
    name: str
    operations: str | tuple[str, ...]
    approval: str = "none"
    approvers: tuple[str, ...] = ()
    author_principal: bool = False
    scope_projects: tuple[str, ...] | None = None
    scope_fact_prefixes: tuple[str, ...] | None = None
    kind: str = "agent"
    manual_sources: bool = False
    legacy_import: bool = False
    # 1.9.0: the person row this agent writes as. An observation it adds without author_person_id is written by it.
    person_id: str | None = None

    def allows(self, op_name: str) -> bool:
        if op_name in _UNGRANTABLE:
            return False
        if op_name in schemas.COLLECTOR_OPS and self.kind != "collector":
            return False
        if op_name in schemas.EXTRACTOR_OPS and self.kind != "extractor":
            return False
        if self.operations == "*":
            return op_name in schemas.OPS
        return op_name in self.operations


@dataclass(frozen=True)
class Workstream:
    id: str
    title: str
    description: str = ""
    sort_order: int = 100


@dataclass(frozen=True)
class FactKey:
    key: str
    value_type: str
    unit: str = ""
    allowed_values: tuple[str, ...] | None = None
    stale_after_days: int | None = None
    description: str = ""
    value_words: dict | None = None  # value -> tuple of words


@dataclass(frozen=True)
class Collector:
    name: str
    type: str
    agent: str
    options: dict


@dataclass(frozen=True)
class Writes:
    """[writes]: the note switch and the note cap."""

    notes: bool = True
    note_max_chars: int = 500


DEFAULT_RANK_KINDS = truth_mod.DEFAULT_RANK_KINDS


@dataclass(frozen=True)
class Truth:
    """[truth]: aliases (target to a tuple of aliases) and source ranking."""

    entities: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)
    rank_kinds: tuple[str, ...] = DEFAULT_RANK_KINDS
    rank_uri_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fetch:
    """[fetch]: how many missed scheduled runs raise the Intake line, and the
    schedule's interval. Named Fetch so the source scan of tests/test_collectors.py isn't tripped; the
    loader is _load_fetch_table."""

    stale_after_runs: int = 3
    every_minutes: int = 30


@dataclass(frozen=True)
class Usage:
    """[usage]: the content-free read log. On by default, because it holds no content and stays local."""

    record: bool = True


@dataclass(frozen=True)
class Sessions:
    """[sessions]: the record of past agent sessions beside the brain. On by default: code files what was said, on
    this machine only. hermes_profiles names extra Hermes homes, as (name, path to state.db) pairs."""

    record: bool = True
    hermes_profiles: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Updates:
    """[updates]: the release channel, automatic updates, and what the agent may do
    when they're off. Nothing in this build fetches a release, so auto stays off unless a person turns it on
    for a release source of their own."""

    channel: str = "stable"
    auto: bool = False
    policy: str = "ask"


@dataclass(frozen=True)
class Config:
    name: str
    timezone: str
    principal: Principal | None
    workstreams: tuple[Workstream, ...]
    fact_keys: tuple[FactKey, ...]
    agents: dict[str, Agent]
    collectors: dict[str, Collector]
    org: str = ""
    truth: Truth = field(default_factory=Truth)
    writes: Writes = field(default_factory=Writes)
    fetch: Fetch = field(default_factory=Fetch)
    updates: Updates = field(default_factory=Updates)
    usage: Usage = field(default_factory=Usage)
    sessions: Sessions = field(default_factory=Sessions)


_UNGRANTABLE = frozenset({"upsert_fact_key", "set_aliases"})
_TOP_KEYS = {
    "instance",
    "principal",
    "workstreams",
    "fact_keys",
    "agents",
    "collectors",
    "extraction",
    "truth",
    "writes",
    "fetch",
    "updates",
    "usage",
    "sessions",
}
_SESSIONS_KEYS = {"record", "hermes_profiles"}
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_UPDATES_KEYS = {"channel", "auto", "policy"}
_FETCH_KEYS = {"stale_after_runs", "every_minutes"}
_TRUTH_KEYS = {"entities", "attributes", "source_rank"}
_SOURCE_RANK_KEYS = {"kinds", "uri_prefixes"}
_WRITES_KEYS = {"notes", "note_max_chars"}
_EXTRACTOR_DERIVED = {"upsert_decision", "upsert_action", "upsert_thread_link", "add_claim"}
EXTRACTION_REFUSED = (
    "extraction is replaced by [truth] in 0.7.0: remove [extraction], then register fact keys under "
    "[[fact_keys]] and their aliases under [truth]"
)
_INSTANCE_KEYS = {"name", "timezone", "org"}
_PRINCIPAL_KEYS = {"person_id", "display_name", "names", "aliases"}
_WORKSTREAM_KEYS = {"id", "title", "description", "sort_order"}
_FACT_KEY_KEYS = {
    "key",
    "value_type",
    "unit",
    "allowed_values",
    "stale_after_days",
    "description",
    "value_words",
}
_AGENT_KEYS = {
    "operations",
    "approval",
    "approvers",
    "author_principal",
    "scope",
    "kind",
    "manual_sources",
    "legacy_import",
    "person_id",
}
_SCOPE_KEYS = {"projects", "fact_prefixes"}
_VALUE_TYPES = set(schemas.VALUE_TYPES)
_COLLECTOR_NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_FACT_PREFIX = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_FACT_SPECS = {
    "github": github_collectors.FACTS,
    "vanta": vanta_collectors.FACTS,
}
_FILES_ONLY = {"capture", "authored_by_principal"}


def _unknown(path: str) -> ConfigError:
    return ConfigError(f"unknown key {path}")


def _require_str(value, path: str, *, min_len: int = 1, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string")
    if len(value) < min_len or (max_len is not None and len(value) > max_len):
        raise ConfigError(f"{path} length is invalid")
    return value


def _no_at(value: str, path: str) -> str:
    if "@" in value:
        raise ConfigError(f"{path} must not contain @")
    return value


def _one_line(value: str, path: str) -> str:
    """value as it must be written, one line of printable text, or a ConfigError that says what to change."""
    try:
        return single_line(value, refuse=True, required=True)
    except NotOneLine as exc:
        raise ConfigError(f"{path} {exc}") from exc


def _option_value(name: str, key: str, option, value, workstream_ids: set[str]):
    path = f"collectors.{name}.{key}"
    if option.kind == "label":
        if not isinstance(value, str) or _COLLECTOR_NAME.fullmatch(value) is None:
            raise ConfigError(f"{path} must be a label")
        return value
    if option.kind == "enum":
        if value not in option.allowed:
            raise ConfigError(f"{path} must be one of {', '.join(option.allowed)}")
        return value
    if option.kind == "bool":
        if type(value) is not bool:
            raise ConfigError(f"{path} must be a boolean")
        return value
    if option.kind == "fact_prefix":
        if (
            not isinstance(value, str)
            or len(value) > 80
            or _FACT_PREFIX.fullmatch(value) is None
        ):
            raise ConfigError(f"{path} must be a fact prefix")
        return value
    if option.kind == "repo":
        if not isinstance(value, str) or _REPO.fullmatch(value) is None:
            raise ConfigError(f"{path} must be a repo")
        return value
    if option.kind == "workstream":
        if not isinstance(value, str) or value not in workstream_ids:
            raise ConfigError(f"{path} must be a configured workstream")
        return value
    if option.kind == "path":
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 400
            or not (value.startswith("/") or value.startswith("~/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or has_line_break(value)
        ):
            raise ConfigError(f"{path} must be an absolute path")
        return value
    if option.kind == "query":
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 500
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or has_line_break(value)
        ):
            raise ConfigError(f"{path} must be one line of 1 to 500 characters")
        return value
    if option.kind == "channels":
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 100
            or any(not isinstance(item, str) or _CHANNEL.fullmatch(item) is None for item in value)
            or len(set(value)) != len(value)
        ):
            raise ConfigError(f"{path} must be 1 to 100 different Slack channel ids, such as C0123ABCD")
        return list(value)
    if option.kind == "days":
        if type(value) is not int or not 1 <= value <= 90:
            raise ConfigError(f"{path} must be a whole number from 1 to 90")
        return value
    if option.kind == "strings":
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 10
            or any(
                not isinstance(item, str)
                or not 1 <= len(item) <= 40
                or has_line_break(item)
                for item in value
            )
        ):
            raise ConfigError(
                f"{path} must be 1 to 10 one-line strings of 1 to 40 characters"
            )
        if option.allowed and (
            any(item not in option.allowed for item in value) or len(set(value)) != len(value)
        ):
            raise ConfigError(f"{path} must be from {', '.join(option.allowed)}")
        return list(value)
    raise ConfigError(f"{path} is invalid")


_CHANNEL = re.compile(r"[CGD][A-Z0-9]{2,20}")
_FETCH_ONLY = ("live_query", "live_channels", "include_dms", "repo", "threads")
_SERVICE_KINDS = {"gmail": ["email"], "slack": ["slack"], "github": ["document"]}


def _check_service_settings(collectors: dict) -> None:
    """A capture collector's fetch settings fit its service, and threads names the right
    metadata collector."""
    for name, collector in collectors.items():
        if collector.type != "capture":
            continue
        options, path = collector.options, f"collectors.{name}"
        service = options["service"]
        if service == "documents":
            for key in _FETCH_ONLY:
                if options[key] not in ("", False, []):
                    raise ConfigError(f"{path}.{key} is not used by the documents service")
            continue
        if options["live_folder"]:
            raise ConfigError(f"{path}." + "live_folder is only for the documents service")
        if options["kinds"] != _SERVICE_KINDS[service]:
            raise ConfigError(f"{path}.kinds must be {json.dumps(_SERVICE_KINDS[service])} for the {service} service")
        if service == "github" and not options["repo"]:
            raise ConfigError(f"{path}.repo is required for the github service")
        if service != "github" and options["repo"]:
            raise ConfigError(f"{path}.repo is only for the github service")
        if service != "gmail" and options["live_query"]:
            raise ConfigError(f"{path}." + "live_query is only for the gmail service")
        if service != "slack" and (options["live_channels"] or options["include_dms"]):
            raise ConfigError(f"{path}." + "live_channels and include_dms are only for the slack service")
        if service == "slack" and not options["live_channels"] and not options["include_dms"]:
            raise ConfigError(f"{path}." + "live_channels is required for the slack service, unless include_dms is true")
        threads = options["threads"]
        if not threads:
            continue
        if service == "github":
            raise ConfigError(f"{path}.threads is only for the gmail and slack services")
        other = collectors.get(threads)
        if other is None or other.type != service:
            raise ConfigError(f"{path}.threads must name a {service} collector")
        if service == "gmail" and other.options.get("with_subject") is not False:
            raise ConfigError(
                f"collectors.{threads}.with_subject must be false: with capture on, subjects live only in "
                "the capture source"
            )


_CREDENTIAL_KEYS = frozenset(
    {"token", "secret", "password", "api_key", "apikey", "access_token", "refresh_token", "client_secret", "bearer"}
)
_CREDENTIAL_ANY = ("xox", "ghp_", "ya29.")
_CREDENTIAL_LONG = ("github_pat_", "gho_", "ghu_", "ghs_", "ghr_", "glpat-", "sk-", "AIza", "AKIA", "eyJ", "1//")


def _credential_shaped(value) -> bool:
    if isinstance(value, list):
        return any(_credential_shaped(item) for item in value)
    if not isinstance(value, str):
        return False
    if value.lower().startswith(_CREDENTIAL_ANY):
        return True
    return len(value) >= 16 and value.startswith(_CREDENTIAL_LONG)


def _refuse_credentials(name: str, block: dict) -> None:
    """Credentials never go in brain.toml, whatever the collector type."""
    for key, value in block.items():
        if str(key).lower() in _CREDENTIAL_KEYS or _credential_shaped(value):
            raise ConfigError(
                f"collectors.{name}.{key} looks like a credential; credentials never go in brain.toml"
            )


def _scope_matches(agent: Agent, type_name: str, options: dict) -> bool:
    if type_name == "vanta":
        return (
            agent.scope_fact_prefixes == (options["fact_prefix"],)
            and agent.scope_projects == (options["project"],)
        )
    if type_name == "github":
        return (
            agent.scope_fact_prefixes == (options["fact_prefix"],) and agent.scope_projects is None
        )
    return agent.scope_fact_prefixes is None and agent.scope_projects is None


def _scope_expected(type_name: str, options: dict) -> str:
    if type_name == "vanta":
        payload = {
            "fact_prefixes": [options["fact_prefix"]],
            "projects": [options["project"]],
        }
    elif type_name == "github":
        payload = {"fact_prefixes": [options["fact_prefix"]]}
    else:
        return "none"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_collectors(raw, agents: dict[str, Agent], workstream_ids: set[str], fact_keys: list[FactKey]):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("collectors must be a table")
    collectors: dict[str, Collector] = {}
    seen_agents: dict[str, str] = {}
    for name, block in raw.items():
        if not isinstance(block, dict):
            raise ConfigError(
                f"collectors.{name} must be a table "
                "(collectors are [collectors.<name>] tables)"
            )
        if _COLLECTOR_NAME.fullmatch(name) is None:
            raise ConfigError(f"collector name {name} is invalid")
        if "type" not in block:
            raise ConfigError(f"collectors.{name}.type is required")
        type_name = block["type"]
        ctype = REGISTRY.get(type_name) if isinstance(type_name, str) else None
        if ctype is None:
            raise ConfigError(f"collectors.{name}.type is unknown: {type_name}")
        _refuse_credentials(name, block)
        live_keys = set(getattr(getattr(ctype, "live"), "config_keys"))
        allowed = {"type", "agent", *ctype.options}
        for key in block:
            if key in live_keys:
                raise ConfigError(
                    f"collectors.{name}.{key} is for the live adapter, "
                    "which is disabled until approved"
                )
            if ctype.name == "files" and key in _FILES_ONLY:
                raise ConfigError(
                    f"collectors.{name}.{key} is not available in this version: "
                    "files collects metadata only"
                )
            if key not in allowed:
                raise ConfigError(f"unknown key collectors.{name}.{key}")
        options = {}
        for key, option in ctype.options.items():
            if key not in block:
                if option.required:
                    raise ConfigError(f"collectors.{name}.{key} is required")
                options[key] = list(option.default) if isinstance(option.default, list) else option.default
            else:
                options[key] = _option_value(name, key, option, block[key], workstream_ids)
        if "agent" not in block:
            raise ConfigError(f"collectors.{name}.agent is required")
        agent_name = block["agent"]
        agent = agents.get(agent_name) if isinstance(agent_name, str) else None
        if agent is None:
            raise ConfigError(
                f"collectors.{name}.agent {agent_name} is not a configured agent"
            )
        if agent.kind != "collector":
            raise ConfigError(
                f'collectors.{name}.agent {agent_name} must have kind = "collector"'
            )
        if agent.manual_sources:
            raise ConfigError(
                f"agents.{agent_name}: manual_sources = true is not allowed "
                f"on collector {name}'s agent"
            )
        if agent_name in seen_agents:
            raise ConfigError(
                f"agent {agent_name} is used by collectors {seen_agents[agent_name]} and {name}"
            )
        seen_agents[agent_name] = name
        if agent.operations == "*" or set(agent.operations) != set(ctype.ops):
            raise ConfigError(
                f"collectors.{name}: agent {agent_name} must have operations exactly "
                f"{json.dumps(list(ctype.ops))}"
            )
        if agent.approval != "none":
            raise ConfigError(
                f"collectors.{name}: agent {agent_name} must not need approval"
            )
        if not _scope_matches(agent, ctype.name, options):
            raise ConfigError(
                f"collectors.{name}: agent {agent_name} must have scope "
                f"{_scope_expected(ctype.name, options)}"
            )
        if agent.author_principal and not ctype.principal_author:
            raise ConfigError(
                f"collectors.{name}: agent {agent_name} must not have author_principal"
            )
        for suffix, value_type, allowed_values in _FACT_SPECS.get(ctype.name, ()):
            full = f"{options['fact_prefix']}.{suffix}"
            match = next((item for item in fact_keys if item.key == full), None)
            if (
                match is None
                or match.value_type != value_type
                or (
                    allowed_values is not None
                    and tuple(match.allowed_values or ()) != tuple(allowed_values)
                )
            ):
                raise ConfigError(
                    f"collectors.{name} needs fact key {full} ({value_type})"
                )
        collectors[name] = Collector(name, ctype.name, agent_name, options)
    seen_accounts: list[tuple[str, str, object]] = []
    for name, collector in collectors.items():
        account = collector.options.get("account")
        if account is None:
            continue
        for previous, previous_type, previous_account in seen_accounts:
            if previous_type == collector.type and previous_account == account:
                raise ConfigError(
                    f"collectors {previous} and {name} both collect {collector.type} "
                    f"account {account}"
                )
        seen_accounts.append((name, collector.type, account))
    origins: dict[str, str] = {}
    for name, collector in collectors.items():
        origin = collector.options.get("origin")
        if collector.type != "capture" or origin is None:
            continue
        if origin in origins:
            raise ConfigError(f"collectors {origins[origin]} and {name} both capture origin {origin}")
        origins[origin] = name
    _check_service_settings(collectors)
    return collectors


def _load_usage(raw) -> Usage:
    if raw is None:
        return Usage()
    if not isinstance(raw, dict):
        raise ConfigError("usage must be a table")
    extra = set(raw) - {"record"}
    if extra:
        raise _unknown("usage." + sorted(extra)[0])
    if type(raw.get("record", True)) is not bool:
        raise ConfigError("usage.record must be true or false")
    return Usage(raw.get("record", True))


def _load_sessions(raw) -> Sessions:
    if raw is None:
        return Sessions()
    if not isinstance(raw, dict):
        raise ConfigError("sessions must be a table")
    extra = set(raw) - _SESSIONS_KEYS
    if extra:
        raise _unknown("sessions." + sorted(extra)[0])
    record = raw.get("record", True)
    if type(record) is not bool:
        raise ConfigError("sessions.record must be true or false")
    profiles = raw.get("hermes_profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError('sessions.hermes_profiles must be a table of name = "path to state.db"')
    for name, path in profiles.items():
        if not _PROFILE_NAME.fullmatch(name) or name == "default":
            raise ConfigError(
                f"sessions.hermes_profiles.{name}: a profile name is 1 to 40 lower-case letters, digits, hyphens "
                'and underscores, starting with a letter, and not "default"'
            )
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"sessions.hermes_profiles.{name} must be the path to that profile's state.db")
    return Sessions(record, tuple(sorted(profiles.items())))


def _load_updates(raw) -> Updates:
    if raw is None:
        return Updates()
    if not isinstance(raw, dict):
        raise ConfigError("updates must be a table")
    extra = set(raw) - _UPDATES_KEYS
    if extra:
        raise _unknown("updates." + sorted(extra)[0])
    channel, auto, policy = raw.get("channel", "stable"), raw.get("auto", False), raw.get("policy", "ask")
    if channel not in ("stable", "beta"):
        raise ConfigError('updates.channel must be "stable" or "beta"')
    if type(auto) is not bool:
        raise ConfigError("updates.auto must be true or false")
    if policy not in ("ask", "agent"):
        raise ConfigError('updates.policy must be "ask" or "agent"')
    return Updates(channel, auto, policy)


def _load_fetch_table(raw) -> Fetch:
    if raw is None:
        return Fetch()
    if not isinstance(raw, dict):
        raise ConfigError("fetch must be a table")
    extra = set(raw) - _FETCH_KEYS
    if extra:
        raise _unknown("fetch." + sorted(extra)[0])
    stale = raw.get("stale_after_runs", 3)
    if type(stale) is not int or not 1 <= stale <= 20:
        raise ConfigError("fetch.stale_after_runs must be a whole number from 1 to 20")
    every = raw.get("every_minutes", 30)
    if type(every) is not int or not 5 <= every <= 1440:
        raise ConfigError("fetch.every_minutes must be a whole number from 5 to 1440")
    return Fetch(stale, every)


def _load_writes(raw) -> Writes:
    if raw is None:
        return Writes()
    if not isinstance(raw, dict):
        raise ConfigError("writes must be a table")
    extra = set(raw) - _WRITES_KEYS
    if extra:
        raise _unknown("writes." + sorted(extra)[0])
    notes = raw.get("notes", True)
    if type(notes) is not bool:
        raise ConfigError("writes.notes must be true or false")
    cap = raw.get("note_max_chars", 500)
    if type(cap) is not int or not 100 <= cap <= 2000:
        raise ConfigError("writes.note_max_chars must be a whole number from 100 to 2000")
    return Writes(notes, cap)


_BOOL_OWN_NAMES = {"true": ("true", "yes"), "false": ("false", "no")}


def value_words_error(value_type: str, allowed, value_words) -> str | None:
    """The first problem with a key's value_words, as the text after
    `fact_keys[<i>].`, or None. Also used for upsert_fact_key's row."""
    if value_type not in ("enum", "bool"):
        return "value_words is only for enum and bool keys"
    if not isinstance(value_words, dict) or any(
        not isinstance(words, list)
        or not 1 <= len(words) <= 20
        or any(
            not isinstance(word, str) or not 1 <= len(word) <= 80 or has_line_break(word)
            for word in words
        )
        for words in value_words.values()
    ):
        return "value_words must map values to lists of 1 to 20 words, each 1 to 80 characters on one line"
    values = ("true", "false") if value_type == "bool" else tuple(allowed or ())
    for value in value_words:
        if value not in values:
            return f"value_words names {value}, which is not a value of this key"
    own: dict[str, set[str]] = {}
    for value in values:
        names = _BOOL_OWN_NAMES[value] if value_type == "bool" else (value,)
        own[value] = {match_key(name) for name in names}
    seen: dict[str, str] = {}
    for value in sorted(value_words):
        for word in value_words[value]:
            key = match_key(word)
            if not key:
                continue
            first = seen.get(key)
            if first is not None and first != value:
                return f'value_words: "{word}" is listed for both {first} and {value}'
            seen.setdefault(key, value)
            for other in sorted(own):
                if other != value and key in own[other]:
                    first_v, second_v = sorted((value, other))
                    return f'value_words: "{word}" is listed for both {first_v} and {second_v}'
    for value in sorted(value_words):
        for word in value_words[value]:
            if not match_key(word):
                return f'value_words: "{word}" has no words'
    return None


def _truth_words(text: str) -> str:
    return truth_mod.words(text)


def _alias_lists(table: dict, kind: str) -> dict[str, tuple[str, ...]]:
    lists: dict[str, tuple[str, ...]] = {}
    for target, aliases in table.items():
        if (
            not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 20
            or any(
                not isinstance(item, str)
                or not 1 <= len(item) <= 80
                or has_line_break(item)
                # The table stores the normal form, which can be longer than the alias.
                or len(_truth_words(item)) > 80
                for item in aliases
            )
        ):
            raise ConfigError(
                f"truth.{kind}.{target} must be a list of 1 to 20 aliases, "
                "each 1 to 80 characters on one line"
            )
        lists[target] = tuple(aliases)
    return lists


def _clash_check(kind: str, lists: dict[str, tuple[str, ...]], implicit: set[str]) -> None:
    """A configured alias may not equal another target's configured alias or implicit name."""
    owners: dict[str, set[str]] = {}
    for target in implicit:
        owners.setdefault(_truth_words(target), set()).add(target)
    for target, aliases in lists.items():
        for alias in aliases:
            owners.setdefault(_truth_words(alias), set()).add(target)
    for target, aliases in lists.items():
        for alias in aliases:
            others = sorted(owners[_truth_words(alias)] - {target})
            if others:
                first, second = sorted((target, others[0]))
                raise ConfigError(f'truth.{kind}: "{alias}" is an alias of both {first} and {second}')


def truth_clash(truth: Truth, keys) -> str | None:
    """The clash rule against a set of registered keys, such as the store's."""
    entities = {key.rpartition(".")[0] for key in keys}
    attributes = {key.rpartition(".")[2] for key in keys}
    try:
        _clash_check("entities", truth.entities, entities)
        _clash_check("attributes", truth.attributes, attributes)
    except ConfigError as exc:
        return str(exc)
    return None


def _load_truth(raw, fact_keys: list[FactKey]) -> Truth:
    if raw is None:
        return Truth()
    if not isinstance(raw, dict):
        raise ConfigError("truth must be a table")
    extra = set(raw) - _TRUTH_KEYS
    if extra:
        raise _unknown("truth." + sorted(extra)[0])
    entities_raw = raw.get("entities", {})
    attributes_raw = raw.get("attributes", {})
    if not isinstance(entities_raw, dict):
        raise ConfigError("truth.entities must be a table")
    if not isinstance(attributes_raw, dict):
        raise ConfigError("truth.attributes must be a table")
    entities = _alias_lists(entities_raw, "entities")
    attributes = _alias_lists(attributes_raw, "attributes")
    key_entities = {item.key.rpartition(".")[0] for item in fact_keys}
    key_attributes = {item.key.rpartition(".")[2] for item in fact_keys}
    for target in entities:
        if target not in key_entities:
            raise ConfigError(f"truth.entities.{target} names no fact key's entity")
    for target in attributes:
        if target not in key_attributes:
            raise ConfigError(f"truth.attributes.{target} names no fact key's attribute")
    for kind, lists in (("entities", entities), ("attributes", attributes)):
        for target, aliases in lists.items():
            for alias in aliases:
                if not _truth_words(alias):
                    raise ConfigError(f'truth.{kind}.{target}: alias "{alias}" has no words')
    _clash_check("entities", entities, key_entities)
    _clash_check("attributes", attributes, key_attributes)
    kinds: tuple[str, ...] = DEFAULT_RANK_KINDS
    prefixes: tuple[str, ...] = ()
    if "source_rank" in raw:
        rank = raw["source_rank"]
        if not isinstance(rank, dict):
            raise ConfigError("truth.source_rank must be a table")
        extra = set(rank) - _SOURCE_RANK_KEYS
        if extra:
            raise _unknown("truth.source_rank." + sorted(extra)[0])
        if "kinds" in rank:
            value = rank["kinds"]
            if (
                not isinstance(value, list)
                or any(not isinstance(item, str) or item not in schemas.SOURCE_KINDS for item in value)
                or len(set(value)) != len(value)
            ):
                raise ConfigError("truth.source_rank.kinds must be a list of unique source kinds")
            kinds = tuple(value)
        if "uri_prefixes" in rank:
            value = rank["uri_prefixes"]
            if (
                not isinstance(value, list)
                or len(value) > 20
                or any(
                    not isinstance(item, str)
                    or len(item) > 200
                    or not (item.startswith("http://") or item.startswith("https://"))
                    for item in value
                )
            ):
                raise ConfigError(
                    "truth.source_rank.uri_prefixes must be a list of up to 20 http(s) prefixes"
                )
            prefixes = tuple(value)
    return Truth(entities, attributes, kinds, prefixes)


# Parsed configs by the exact bytes of their file. A read and a write each load brain.toml, and parsing a large
# one costs milliseconds; the bytes are read every time, so an edited file is never served stale.
_PARSED: dict[bytes, Config] = {}
_PARSED_MAX = 8


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    cached = _PARSED.get(raw)
    if cached is not None:
        return cached
    config = _parse_config(raw)
    if len(_PARSED) >= _PARSED_MAX:
        _PARSED.clear()
    _PARSED[raw] = config
    return config


def _parse_config(raw: bytes) -> Config:
    try:
        data = tomllib.loads(raw.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must be a table")
    extra = set(data) - _TOP_KEYS
    if extra:
        raise _unknown(sorted(extra)[0])

    instance = data.get("instance")
    if not isinstance(instance, dict):
        raise ConfigError("instance table is required")
    extra = set(instance) - _INSTANCE_KEYS
    if extra:
        raise _unknown("instance." + sorted(extra)[0])
    name = _require_str(instance.get("name"), "instance.name")
    if not _PROJECT_ID.fullmatch(name):
        raise ConfigError("instance.name is not a valid instance name")
    timezone = instance.get("timezone", "UTC")
    timezone = _require_str(timezone, "instance.timezone")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"instance.timezone is not a known IANA name: {timezone}") from exc
    org = ""
    if "org" in instance:
        org = _one_line(
            _no_at(_require_str(instance["org"], "instance.org", max_len=120), "instance.org"), "instance.org"
        )

    principal = None
    if "principal" in data:
        block = data["principal"]
        if not isinstance(block, dict):
            raise ConfigError("principal must be a table")
        extra = set(block) - _PRINCIPAL_KEYS
        if extra:
            raise _unknown("principal." + sorted(extra)[0])
        person_id = _require_str(block.get("person_id"), "principal.person_id")
        if not _PERSON_ID.fullmatch(person_id):
            raise ConfigError("principal.person_id is invalid")
        display_name = _one_line(
            _no_at(
                _require_str(block.get("display_name"), "principal.display_name", max_len=120),
                "principal.display_name",
            ),
            "principal.display_name",
        )
        names_raw = block.get("names")
        if not isinstance(names_raw, list) or not names_raw:
            raise ConfigError("principal.names must be a non-empty list")
        names: list[str] = []
        for i, item in enumerate(names_raw):
            word = _require_str(item, f"principal.names[{i}]")
            if not _PRINCIPAL_NAME.fullmatch(word):
                raise ConfigError("principal names must be single words")
            names.append(word)
        aliases_raw = block.get("aliases", [])
        if not isinstance(aliases_raw, list):
            raise ConfigError("principal.aliases must be a list")
        aliases = tuple(
            _no_at(_require_str(item, f"principal.aliases[{i}]"), f"principal.aliases[{i}]")
            for i, item in enumerate(aliases_raw)
        )
        principal = Principal(person_id, display_name, tuple(names), aliases)

    work_raw = data.get("workstreams")
    if not isinstance(work_raw, list) or not work_raw:
        raise ConfigError("at least one workstream is required")
    workstreams: list[Workstream] = []
    seen_ws: set[str] = set()
    for i, item in enumerate(work_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"workstreams[{i}] must be a table")
        extra = set(item) - _WORKSTREAM_KEYS
        if extra:
            raise _unknown(f"workstreams[{i}]." + sorted(extra)[0])
        ws_id = _require_str(item.get("id"), f"workstreams[{i}].id")
        if not _PROJECT_ID.fullmatch(ws_id):
            raise ConfigError(f"workstreams[{i}].id is invalid")
        if ws_id in seen_ws:
            raise ConfigError(f"workstream id {ws_id} is not unique")
        seen_ws.add(ws_id)
        title = _require_str(item.get("title"), f"workstreams[{i}].title")
        description = item.get("description", "")
        description = _require_str(description, f"workstreams[{i}].description", min_len=0)
        sort_order = item.get("sort_order", 100)
        if type(sort_order) is not int:
            raise ConfigError(f"workstreams[{i}].sort_order must be an integer")
        workstreams.append(Workstream(ws_id, title, description, sort_order))

    fact_keys: list[FactKey] = []
    seen_keys: set[str] = set()
    for i, item in enumerate(data.get("fact_keys") or []):
        if not isinstance(item, dict):
            raise ConfigError(f"fact_keys[{i}] must be a table")
        extra = set(item) - _FACT_KEY_KEYS
        if extra:
            raise _unknown(f"fact_keys[{i}]." + sorted(extra)[0])
        key = _require_str(item.get("key"), f"fact_keys[{i}].key")
        if not _FACT_KEY.fullmatch(key) or len(key) > 120:
            raise ConfigError(f"fact_keys[{i}].key is invalid")
        if key in seen_keys:
            raise ConfigError(f"fact key {key} is not unique")
        seen_keys.add(key)
        value_type = _require_str(item.get("value_type"), f"fact_keys[{i}].value_type")
        if value_type not in _VALUE_TYPES:
            raise ConfigError(f"fact_keys[{i}].value_type is invalid")
        allowed = item.get("allowed_values")
        if value_type == "enum":
            if not isinstance(allowed, list) or not allowed:
                raise ConfigError("enum fact key needs allowed_values")
            allowed_values = tuple(
                _require_str(v, f"fact_keys[{i}].allowed_values[{j}]", max_len=200)
                for j, v in enumerate(allowed)
            )
        else:
            if allowed is not None:
                raise ConfigError("allowed_values is only valid for enum fact keys")
            allowed_values = None
        unit = _require_str(item.get("unit", ""), f"fact_keys[{i}].unit", min_len=0, max_len=40)
        stale = item.get("stale_after_days")
        if stale is not None and type(stale) is not int:
            raise ConfigError(f"fact_keys[{i}].stale_after_days must be an integer")
        description = _require_str(
            item.get("description", ""), f"fact_keys[{i}].description", min_len=0, max_len=500
        )
        words = None
        if "value_words" in item:
            problem = value_words_error(value_type, allowed_values, item["value_words"])
            if problem:
                raise ConfigError(f"fact_keys[{i}].{problem}")
            words = {value: tuple(item["value_words"][value]) for value in sorted(item["value_words"])}
        fact_keys.append(FactKey(key, value_type, unit, allowed_values, stale, description, words))

    agents_raw = data.get("agents") or {}
    if not isinstance(agents_raw, dict):
        raise ConfigError("agents must be a table")
    agents: dict[str, Agent] = {}
    for agent_name, block in agents_raw.items():
        if not _AGENT_NAME.fullmatch(agent_name):
            raise ConfigError(f"agent name {agent_name} is invalid")
        if agent_name == RESERVED_AGENT:
            raise ConfigError("agent named brain is reserved")
        if not isinstance(block, dict):
            raise ConfigError(f"agents.{agent_name} must be a table")
        extra = set(block) - _AGENT_KEYS
        if extra:
            raise _unknown(f"agents.{agent_name}." + sorted(extra)[0])
        kind = block.get("kind", "agent")
        if kind not in {"agent", "collector", "extractor"}:
            raise ConfigError(f"agents.{agent_name}.kind is invalid")
        if "manual_sources" in block and type(block["manual_sources"]) is not bool:
            raise ConfigError(f"agents.{agent_name}.manual_sources must be a boolean")
        manual_sources = block.get("manual_sources", False)
        if "legacy_import" in block and type(block["legacy_import"]) is not bool:
            raise ConfigError(f"agents.{agent_name}.legacy_import must be a boolean")
        legacy_import = block.get("legacy_import", False)
        if legacy_import and (kind != "collector" or not manual_sources):
            raise ConfigError(
                f'agents.{agent_name}.legacy_import needs kind = "collector" and manual_sources = true'
            )
        operations = block.get("operations")
        if operations == "*":
            ops: str | tuple[str, ...] = "*"
        elif isinstance(operations, list) and operations:
            cleaned: list[str] = []
            for op in operations:
                if not isinstance(op, str) or (
                    op not in schemas.OPS and op not in schemas.EXTRACTOR_OPS
                ):
                    raise ConfigError(f"agents.{agent_name} has unknown operation {op}")
                if op in _UNGRANTABLE:
                    raise ConfigError(f"{op} cannot be granted")
                if op in schemas.COLLECTOR_OPS and kind != "collector":
                    raise ConfigError(
                        f"agents.{agent_name}: {op} is only for collectors "
                        '(kind = "collector")'
                    )
                if op in schemas.EXTRACTOR_OPS and kind != "extractor":
                    raise ConfigError(
                        f"agents.{agent_name}: apply_candidate is only for extractors "
                        '(kind = "extractor")'
                    )
                cleaned.append(op)
            ops = tuple(cleaned)
        else:
            raise ConfigError(f"agents.{agent_name}.operations is required")
        approval = block.get("approval", "verified" if kind == "extractor" else "none")
        if approval not in {"none", "required", "verified"}:
            raise ConfigError(f"agents.{agent_name}.approval is invalid")
        if kind == "extractor":
            if ops == "*":
                raise ConfigError(
                    f'agents.{agent_name}: kind = "extractor" needs an operations list'
                )
            if "apply_candidate" not in ops:
                raise ConfigError(
                    f"agents.{agent_name}: an extractor's operations must include apply_candidate"
                )
            for op in ops:
                if op != "apply_candidate" and op not in _EXTRACTOR_DERIVED:
                    raise ConfigError(
                        f"agents.{agent_name}: {op} is not allowed for an extractor"
                    )
            if approval not in ("required", "verified"):
                raise ConfigError(
                    f'agents.{agent_name}: an extractor must have approval = "verified" or "required"'
                )
        elif approval == "verified":
            raise ConfigError(f'agents.{agent_name}: approval = "verified" is only for extractors')
        approvers_raw = block.get("approvers", [])
        if not isinstance(approvers_raw, list):
            raise ConfigError(f"agents.{agent_name}.approvers must be a list")
        approvers = tuple(
            _require_str(item, f"agents.{agent_name}.approvers[{i}]")
            for i, item in enumerate(approvers_raw)
        )
        author_principal = block.get("author_principal", False)
        if type(author_principal) is not bool:
            raise ConfigError(f"agents.{agent_name}.author_principal must be a boolean")
        if author_principal and kind != "collector":
            raise ConfigError(
                f"agents.{agent_name}.author_principal needs kind = \"collector\""
            )
        if manual_sources and kind != "collector":
            raise ConfigError(
                f"agents.{agent_name}.manual_sources needs kind = \"collector\""
            )
        person_id = block.get("person_id")
        if person_id is not None:
            if not isinstance(person_id, str) or not _PERSON_ID.fullmatch(person_id):
                raise ConfigError(f"agents.{agent_name}.person_id is invalid: use a person id such as per-{agent_name}")
            if principal is not None and person_id == principal.person_id:
                raise ConfigError(
                    f"agents.{agent_name}.person_id is the principal's; an agent writes as its own person, "
                    f"such as per-{agent_name}"
                )
        scope_projects = None
        scope_prefixes = None
        if "scope" in block:
            scope = block["scope"]
            if not isinstance(scope, dict):
                raise ConfigError(f"agents.{agent_name}.scope must be a table")
            extra = set(scope) - _SCOPE_KEYS
            if extra:
                raise _unknown(f"agents.{agent_name}.scope." + sorted(extra)[0])
            if "projects" in scope:
                if not isinstance(scope["projects"], list):
                    raise ConfigError(f"agents.{agent_name}.scope.projects must be a list")
                scope_projects = tuple(scope["projects"])
            if "fact_prefixes" in scope:
                if not isinstance(scope["fact_prefixes"], list):
                    raise ConfigError(f"agents.{agent_name}.scope.fact_prefixes must be a list")
                scope_prefixes = tuple(scope["fact_prefixes"])
        agents[agent_name] = Agent(
            agent_name,
            ops,
            approval,
            approvers,
            author_principal,
            scope_projects,
            scope_prefixes,
            kind,
            manual_sources,
            legacy_import,
            person_id,
        )

    workstream_ids = {ws.id for ws in workstreams}
    for agent in agents.values():
        if agent.approval in ("required", "verified"):
            if not agent.approvers:
                raise ConfigError(f"agents.{agent.name} requires approvers")
            for approver in agent.approvers:
                if approver == agent.name or approver not in agents:
                    raise ConfigError(
                        f"agents.{agent.name} has unknown or self approver {approver}"
                    )
        if agent.scope_projects is not None:
            for project_id in agent.scope_projects:
                if project_id not in workstream_ids:
                    raise ConfigError(
                        f"agents.{agent.name}.scope.projects includes {project_id}, "
                        "which is not a workstream"
                    )

    collectors = _load_collectors(data.get("collectors"), agents, workstream_ids, fact_keys)
    named = {item.agent for item in collectors.values()}
    for agent in agents.values():
        if agent.kind == "collector" and not agent.manual_sources and agent.name not in named:
            raise ConfigError(
                f"agents.{agent.name}: kind = \"collector\" needs a [collectors] entry "
                "or manual_sources = true"
            )
    collector_of = {item.agent: item.name for item in collectors.values()}
    for agent in agents.values():
        for approver in agent.approvers:
            collector_name = collector_of.get(approver)
            if collector_name is not None:
                raise ConfigError(
                    f"agents.{agent.name}: approver {approver} is collector {collector_name}'s agent, "
                    "and a collector's agent can't approve"
                )
    for agent in agents.values():
        for approver in agent.approvers:
            other = agents.get(approver)
            if other is not None and other.kind == "extractor":
                raise ConfigError(
                    f"agents.{agent.name}: approver {approver} is an extractor, "
                    "and an extractor can't approve"
                )

    if "extraction" in data:
        # [extraction] is replaced by [truth]: a stored block gets one clear error,
        # raised where 0.7.0a1 loaded the block.
        raise ConfigError(EXTRACTION_REFUSED)
    return Config(
        name=name,
        timezone=timezone,
        principal=principal,
        workstreams=tuple(workstreams),
        fact_keys=tuple(fact_keys),
        agents=agents,
        collectors=collectors,
        org=org,
        truth=_load_truth(data.get("truth"), fact_keys),
        writes=_load_writes(data.get("writes")),
        fetch=_load_fetch_table(data.get("fetch")),
        updates=_load_updates(data.get("updates")),
        usage=_load_usage(data.get("usage")),
        sessions=_load_sessions(data.get("sessions")),
    )
