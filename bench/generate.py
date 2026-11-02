"""The Example Co benchmark generator.

Standard library only. Every name, value and date is invented. The output is a pure function of
--seed, --scale and --today: every choice is a SHA-256 draw, no `random`, no `hash()`, no set is
iterated into output, and every date in the data is an ISO date derived from --today.

    python bench/generate.py --today YYYY-MM-DD [--seed 1] [--scale 1] [--out DIR]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

FORMAT = 1
DEFAULT_OUT = Path(__file__).resolve().parent / "exampleco"


class BenchError(Exception):
    """An invariant of the generated dataset failed (exit 3)."""


# -- people ---------------------------------------------------------------------------------------

FIRST_NAMES = (
    "Alex Blair Casey Dana Elliot Finley Gray Harper Indy Jordan Kai Logan Morgan Noel Oakley Parker "
    "Quinn Riley Sam Taylor Uma Val Wren Xan Yael Zion Ari Bryn Cleo Drew Eden Faye Glen Hale Ira Jude "
    "Kit Lane Mika Nico"
).split()
STAFF_ROLES = (
    "Engineer", "Product manager", "Account executive", "Support lead", "Security lead", "Finance lead",
    "Designer", "Operations lead",
)
SPEAKERS = range(1, 28)
EXTERNALS = range(28, 40)

# -- facts ----------------------------------------------------------------------------------------

PRODUCTS = (
    ("excloud", "Example Cloud"), ("exbasic", "Example Basic"), ("expro", "Example Pro"),
    ("exteam", "Example Team"), ("exscale", "Example Scale"), ("exedge", "Example Edge"),
    ("exstudio", "Example Studio"), ("exvault", "Example Vault"),
)
# name, value_type, unit, display, sentence (with <P> and <raw>), raw pool
ATTRIBUTES = (
    ("price", "number", "AUD", "Price", "<P> costs <raw> a seat",
     [f"{n} AUD" for n in ("19", "29", "39", "49.5", "59", "79", "99", "129", "159", "199")]),
    ("seats", "int", "seats", "Seats", "<P> includes <raw>", [f"{n} seats" for n in (5, 10, 25, 50, 100, 250)]),
    ("max_context", "int", "tokens", "Max context", "<P> has a context window of <raw> tokens",
     [f"{n}K" for n in (8, 16, 32, 64, 128, 256)]),
    ("gpu_memory", "number", "GB", "GPU memory", "Each <P> node has <raw> of GPU memory",
     [f"{n} GB" for n in (16, 24, 40, 48, 80, 96, 141)]),
    ("region", "enum", "", "Region", "<P> runs in the <raw> region", ["AU", "EU", "US"]),
    ("sso", "bool", "", "SSO", "SSO is <raw> on <P>", ["included", "not included"]),
    ("support_hours", "int", "hours", "Support hours", "<P> support replies within <raw>",
     [f"{n} hours" for n in (2, 4, 8, 24, 48)]),
    ("retention_days", "int", "days", "Retention days", "<P> keeps logs for <raw>",
     [f"{n} days" for n in (7, 14, 30, 90, 180, 365)]),
    ("launch_date", "date", "", "Launch date", "<P> launches on <raw>", []),
    ("current_version", "text", "", "Current version", "<P> is at version <raw>",
     ["1.1", "1.2", "1.3", "2.0", "2.1", "2.2", "3.0", "3.1"]),
)
ATTR = {item[0]: item for item in ATTRIBUTES}
PRODUCT_NAME = dict(PRODUCTS)
UPDATED = (
    ("exbasic", "seats"), ("exbasic", "retention_days"), ("exteam", "price"), ("exscale", "max_context"),
    ("exvault", "support_hours"),
)
VERSIONED_ATTRS = ("max_context", "gpu_memory", "support_hours")
VERSIONED = (("excloud", ("1.2", "1.3")), ("expro", ("2.0", "2.1")))
DISPUTED = (
    ("excloud", "region"), ("exbasic", "price"), ("exbasic", "sso"), ("exteam", "gpu_memory"),
    ("exteam", "seats"), ("exscale", "price"), ("exedge", "max_context"), ("exstudio", "retention_days"),
    ("exstudio", "support_hours"), ("exvault", "region"),
)
FIXED_RAW = {
    "excloud.current_version": "1.3", "expro.current_version": "2.1", "exscale.sso": "included",
}
TRAP_KEYS = (
    "exbasic.max_context", "exteam.retention_days", "expro.price", "exedge.price", "exstudio.price",
    "exvault.price", "exbasic.region", "exteam.region", "exscale.sso",
)
UNSET_KEYS = (("exscale", "pentest_date", "date", ""), ("exedge", "sla_credit_percent", "number", "%"))
ENTITY_ALIASES = {entity: [name] for entity, name in PRODUCTS}
ATTRIBUTE_ALIASES = (
    ("price", ["cost", "price per seat"]), ("seats", ["seat count"]),
    ("max_context", ["context window", "context length"]), ("gpu_memory", ["VRAM", "GPU memory"]),
    ("region", ["hosting region"]), ("sso", ["single sign-on"]), ("support_hours", ["support response time"]),
    ("retention_days", ["log retention"]), ("launch_date", ["launch date"]), ("current_version", ["version"]),
    ("pentest_date", ["penetration test"]), ("sla_credit_percent", ["SLA credit"]),
)

# -- work -----------------------------------------------------------------------------------------

VERBS = ("Collect", "Send", "Review", "Approve", "Book", "Confirm")
ADJECTIVES = ("access", "backup", "billing", "contract", "data", "evidence", "launch", "pricing", "renewal", "vendor")
THINGS = ("export", "list", "pack", "plan", "report", "sheet")
AREAS = (
    "access control", "backups", "billing", "change control", "data retention", "incident response",
    "key rotation", "logging", "onboarding", "patching", "privacy", "supplier review", "support desk",
    "training", "uptime",
)
CONTROLS = (
    "Quarterly review of", "Written procedure for", "Owner sign off on", "Evidence pack for", "Annual test of",
    "Draft policy for", "Training on", "Monthly check of", "Risk note for", "Exception log for",
)
STATE_PHRASES = {
    "done": "is done", "in_progress": "is in progress", "waiting": "is waiting", "blocked": "is blocked",
    "not_started": "has not started", "not_applicable": "does not apply",
}
DECISION_TITLES = (
    "Keep Example Basic in the EU region", "Retire the legacy export format", "Move the status page to a new host",
    "Adopt quarterly access reviews", "Pause the Example Studio beta", "Share the audit calendar with partners",
)
PROJECTS = (
    ("proj-eu-launch", "Example Cloud EU launch"), ("proj-audit", "Annual audit readiness"),
    ("proj-pricing", "Pricing refresh"),
)
WORKSTREAMS = (
    ("general", "General", "Default workstream.", 100),
    ("platform", "Platform", "Example Cloud engineering and operations.", 200),
    ("sales", "Sales", "Customer and supplier email that needs a reply.", 300),
)
POLICY_AREAS = AREAS[:11]

# Twenty-four filler sentences. No digit, weekday name, currency, unit, price, plan, "included",
# "enabled" or "launch" word, and none of "revenue", "turnover" or "headcount".
FILLER_MULTIPLIER = {1: 1, 4: 9}  # scale to how many times the filler history (measured: markdown about 4 times)

FILLER = (
    "{p} sent {q} a short note.",
    "{q} will follow up soon.",
    "The next call will be short.",
    "{p} asked {q} for an outline.",
    "{p} will take a closer look.",
    "{q} shared a rough draft.",
    "Nothing urgent came up.",
    "{p} and {q} compared notes.",
    "The group thanked {q}.",
    "{p} will tidy the shared folder.",
    "{q} found the room too small.",
    "Everyone liked the new layout.",
    "{p} promised a fuller update.",
    "{q} mentioned a long queue.",
    "The coffee machine works again.",
    "{p} booked the quiet room.",
    "A short stand up kept the team on track.",
    "{q} will bring spare cables.",
    "{p} liked the tidy summary.",
    "The notes are in the shared folder.",
    "{q} suggested a quick catch up.",
    "{p} will check the draft again.",
    "The boxes are out of the hallway.",
    "{p} found the missing charger.",
)
# What an honest abstention may say, and what an answer to a note-only question may say about the note.
ABSTAIN_PHRASES = (
    "does not hold", "doesn't hold", "not recorded", "isn't recorded", "no record", "no value", "unknown",
    "not found", "couldn't find", "can't find", "not available", "no data", "don't have",
)
NOTE_PHRASES = (
    "unsourced", "unverified", "not confirmed", "scratch note", "only a note", "not a fact",
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


# -- matching ---------------------------------------------------------------------------


def norm(text: str) -> str:
    """The matching rule's normal form (the same function as bench/grading.py): lower-case; thousands commas
    dropped; a possessive 's dropped; a decimal point kept inside its number, so 49.5 is one token; every other run of
    non-alphanumerics is one space; padded."""
    text = re.sub(r"['\u2019]s\b", "", text.lower())
    text = re.sub(r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))", "", text)
    text = re.sub(r"(?<=[0-9])\.(?=[0-9])", "\x00", text)
    return " " + re.sub(r"[^a-z0-9\x00]+", " ", text).strip() + " "


def matches(needle: str, haystack: str) -> bool:
    key = norm(needle)
    return key.strip() != "" and key in norm(haystack)


def value_forms(raw: str, kind: str = "") -> list[str]:
    """The alternatives that state a value: the raw, a K value's thousands forms, a number's bare number,
    and for a number key the whole number as the read shows it (29.0)."""
    forms = [raw]
    short = re.fullmatch(r"([0-9]+)K", raw)
    if short:
        forms += [f"{short.group(1)},000", f"{short.group(1)}000"]
        return forms
    bare = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?) [A-Za-z]+", raw)
    if bare:
        forms.append(bare.group(1))
        if kind == "number" and "." not in bare.group(1):
            forms.append(bare.group(1) + ".0")
    return forms


def wrong_forms(raw: str, kind: str = "") -> list[str]:
    """The forms of a wrong value an answer must not assert: value_forms, plus a decimal written with a
    trailing zero (49.5 as 49.50), so a different spelling of the trap can't evade the check."""
    forms = value_forms(raw, kind)
    for form in list(forms):
        match = re.fullmatch(r"([0-9]+\.[0-9]+)( [A-Za-z]+)?", form)
        if match:
            forms.append(match.group(1) + "0" + (match.group(2) or ""))
    return forms


def typed_value(attr: str, raw: str):
    """The value fact_get returns for a raw: an integer for an int key, a float for a number key."""
    kind = ATTR[attr][1]
    if kind == "int":
        short = re.fullmatch(r"([0-9]+)K", raw)
        return int(short.group(1)) * 1000 if short else int(raw.split(" ")[0])
    if kind == "number":
        return float(raw.split(" ")[0])
    if kind == "bool":
        return raw == "included"
    if kind == "enum":
        return raw.lower()
    return raw


# -- the builder ----------------------------------------------------------------------------------


class Builder:
    def __init__(self, seed: int, today: datetime.date, scale: int = 1) -> None:
        self.scale = scale
        self.seed = seed
        self.today = today
        self.now = f"{today.isoformat()}T09:00:00Z"
        self.applied_at = f"{(today - datetime.timedelta(days=3)).isoformat()}T08:00:00Z"
        self.sources: list[dict] = []
        self.source_by_id: dict[str, dict] = {}

    # draws
    def draw(self, label: str, n: int) -> int:
        digest = hashlib.sha256(f"{self.seed}|{label}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % n

    def pick(self, label: str, seq):
        return seq[self.draw(label, len(seq))]

    def shuffle(self, label: str, values: list) -> list:
        order = sorted(range(len(values)), key=lambda i: (self.draw(f"{label}|{i}", 10**9), i))
        rank = {index: position for position, index in enumerate(order)}
        return [values[rank[i]] for i in range(len(values))]

    def day(self, k: int) -> str:
        return (self.today + datetime.timedelta(days=k)).isoformat()

    # text
    def filler(self, label: str, count: int) -> list[str]:
        out = []
        for i in range(count):
            template = self.pick(f"{label}|t{i}", FILLER)
            p = self.draw(f"{label}|p{i}", 40)
            q = (p + 1 + self.draw(f"{label}|q{i}", 39)) % 40
            out.append(template.replace("{p}", self.people[p]["name"]).replace("{q}", self.people[q]["name"]))
        return out

    def add_source(self, group: str, sid: str, kind: str, title: str, date: str, author: str | None,
                   content: str, **extra) -> dict:
        row = {"id": sid, "kind": kind, "title": title, "source_date": date, "author_person_id": author,
               "content": content, **extra, "_group": group}
        if sid in self.source_by_id:
            raise BenchError(f"duplicate source id {sid}")
        self.sources.append(row)
        self.source_by_id[sid] = row
        return row

    # -- people
    def build_people(self) -> None:
        self.people = []
        for index, first in enumerate(FIRST_NAMES):
            if index < 28:
                org, role = "Example Co", STAFF_ROLES[index % 8]
            elif index < 32:
                org, role = "Example Supplies", "Account manager"
            elif index < 36:
                org, role = "Example Audit", "Auditor"
            else:
                org, role = "Example Partners", "Partner lead"
            self.people.append({"id": f"per-{first.lower()}-example", "first": first, "name": f"{first} Example",
                                "org": org, "role": role, "index": index})
        self.principal = self.people[0]

    def speaker(self, label: str) -> dict:
        return self.people[1 + self.draw(label, 27)]

    def external(self, label: str) -> dict:
        return self.people[28 + self.draw(label, 12)]

    # -- facts and claims
    def sentence(self, entity: str, attr: str, raw: str) -> str:
        return ATTR[attr][4].replace("<P>", PRODUCT_NAME[entity]).replace("<raw>", raw)

    def pool(self, attr: str) -> list[str]:
        return ATTR[attr][5]

    def first_raw(self, entity: str, attr: str) -> str:
        key = f"{entity}.{attr}"
        if key in FIXED_RAW:
            return FIXED_RAW[key]
        if attr == "launch_date":
            return self.day(self.draw(f"launch-{entity}", 600) - 400)
        return self.pick(f"val-{key}", self.pool(attr))

    def other_raw(self, entity: str, attr: str, first: str, label: str) -> str:
        """A raw that differs from `first` and neither matches inside the other."""
        key = f"{entity}.{attr}"
        pool = self.pool(attr)
        if attr == "sso":
            return "not included" if first == "included" else "included"
        for attempt in range(500):
            tag = label if attempt == 0 else f"{label}|{attempt}"
            candidate = self.pick(tag, pool)
            if candidate != first and not matches(candidate, first) and not matches(first, candidate):
                return candidate
        raise BenchError(f"no second value for {key}")

    def build_facts(self) -> None:
        self.keys = [f"{entity}.{attr[0]}" for entity, _ in PRODUCTS for attr in ATTRIBUTES]
        updated = {f"{e}.{a}" for e, a in UPDATED}
        versioned = {f"{e}.{a}" for e, _ in VERSIONED for a in VERSIONED_ATTRS}
        disputed = {f"{e}.{a}" for e, a in DISPUTED}
        self.updated_keys = [f"{e}.{a}" for e, a in UPDATED]
        self.disputed_keys = [f"{e}.{a}" for e, a in DISPUTED]
        self.versioned_set = versioned
        self.raw: dict[str, str] = {}
        for entity, _ in PRODUCTS:
            for attr in ATTRIBUTES:
                key = f"{entity}.{attr[0]}"
                self.raw[key] = self.first_raw(entity, attr[0])
        self.update_second: dict[str, str] = {}
        for entity, attr in UPDATED:
            key = f"{entity}.{attr}"
            self.update_second[key] = self.other_raw(entity, attr, self.raw[key], f"upd-{key}")
        self.version_raw: dict[tuple[str, str, str], str] = {}
        for entity, scopes in VERSIONED:
            for attr in VERSIONED_ATTRS:
                key = f"{entity}.{attr}"
                older = self.raw[key]
                self.version_raw[(entity, attr, scopes[0])] = older
                self.version_raw[(entity, attr, scopes[1])] = self.other_raw(entity, attr, older, f"ver-{key}")
        self.dispute_second: dict[str, str] = {}
        for entity, attr in DISPUTED:
            key = f"{entity}.{attr}"
            self.dispute_second[key] = self.other_raw(entity, attr, self.raw[key], f"dsp-{key}")
        self.trap_raw: dict[str, str] = {}
        for key in TRAP_KEYS:
            entity, attr = key.split(".")
            sourced = self.raw[key]
            quote = self.sentence(entity, attr, sourced)
            if key == "exscale.sso":
                self.trap_raw[key] = "not included"
                continue
            for attempt in range(500):
                tag = f"trap-{key}" if attempt == 0 else f"trap-{key}|{attempt}"
                candidate = self.pick(tag, self.pool(attr))
                if candidate != sourced and not matches(candidate, sourced) and not matches(candidate, quote):
                    self.trap_raw[key] = candidate
                    break
            else:
                raise BenchError(f"no trap value for {key}")
        for key in TRAP_KEYS:
            entity, attr = key.split(".")
            if matches(self.trap_raw[key], self.raw[key]) or matches(
                self.trap_raw[key], self.sentence(entity, attr, self.raw[key])
            ):
                raise BenchError(f"the trap value of {key} matches inside its sourced value")
        self.updated_set, self.disputed_set = updated, disputed

    # -- sources
    def build_sources(self) -> None:
        directory = ["Staff directory."] + [f"{p['name']} is {p['role']} at {p['org']}." for p in self.people]
        self.add_source("directory", "src-doc-directory", "document", "Example Co staff directory",
                        self.day(-120), None, "\n".join(directory))
        self.claims: list[dict] = []

        def claim(key: str, raw: str, source_id: str, sentence: str, scope: str | None = None) -> dict:
            row = {"id": f"clm-bench-{len(self.claims) + 1:03d}", "key": key, "value_raw": raw, "basis": "reported",
                   "quote": sentence, "source_id": source_id}
            if scope is not None:
                row["scope"] = scope
            self.claims.append(row)
            return row

        # product sheets
        self.sheet_claim: dict[str, dict] = {}
        for i, (entity, name) in enumerate(PRODUCTS):
            lines = [f"{name} product sheet."]
            pending = []
            for attr in ATTRIBUTES:
                key = f"{entity}.{attr[0]}"
                if key in self.updated_set or key in self.versioned_set:
                    continue
                sentence = self.sentence(entity, attr[0], self.raw[key])
                lines.append(sentence + ".")
                pending.append((key, sentence))
            lines += self.filler(f"sheet-{entity}", 3)
            sid = f"src-doc-sheet-{entity}"
            self.add_source("sheet", sid, "document", f"{name} product sheet", self.day(-90 + i), None, " ".join(lines))
            for key, sentence in pending:
                self.sheet_claim[key] = claim(key, self.raw[key], sid, sentence)
        # editions
        self.edition: dict[str, dict] = {}
        for i, (entity, attr) in enumerate(UPDATED):
            key = f"{entity}.{attr}"
            name = PRODUCT_NAME[entity]
            ids = (f"src-doc-{entity}-{attr}-v1", f"src-doc-{entity}-{attr}-v2")
            dates = (self.day(-60 + i), self.day(-6 + i // 2))
            raws = (self.raw[key], self.update_second[key])
            claims = []
            for n, (sid, label) in enumerate(zip(ids, ("first edition", "second edition"))):
                sentence = self.sentence(entity, attr, raws[n])
                content = " ".join([f"{name} limits, {label}.", sentence + ".", *self.filler(f"ed-{key}-{n}", 2)])
                extra = {"external_ref": f"doc-{entity}-{attr}"}
                if n == 1:
                    extra["supersedes_id"] = ids[0]
                self.add_source("edition1" if n == 0 else "edition2", sid, "document", f"{name} limits", dates[n],
                                None, content, **extra)
                claims.append((sid, sentence, raws[n]))
            self.edition[key] = {"ids": ids, "dates": dates, "raws": raws, "sentences": [c[1] for c in claims]}
        for key in self.updated_keys:
            ed = self.edition[key]
            claim(key, ed["raws"][0], ed["ids"][0], ed["sentences"][0])
            claim(key, ed["raws"][1], ed["ids"][1], ed["sentences"][1])
        # release notes
        self.note_sources: dict[tuple[str, str], str] = {}
        versioned_entities = {entity for entity, _ in VERSIONED}
        for i, (entity, name) in enumerate(PRODUCTS):
            if entity in versioned_entities:
                scopes = dict(VERSIONED)[entity]
                for n, scope in enumerate(scopes):
                    sentences = [
                        self.sentence(entity, attr, self.version_raw[(entity, attr, scope)]) for attr in VERSIONED_ATTRS
                    ]
                    sid = f"src-doc-notes-{entity}-{scope}"
                    content = " ".join([f"{name} {scope} release notes."] + [s + "." for s in sentences])
                    self.add_source("notes", sid, "document", f"{name} {scope} release notes",
                                    self.day(-75 if n == 0 else -20), None, content)
                    self.note_sources[(entity, scope)] = sid
            else:
                sid = f"src-doc-notes-{entity}"
                content = " ".join([f"{name} release notes."] + self.filler(f"notes-{entity}", 4))
                self.add_source("notes", sid, "document", f"{name} release notes", self.day(-40 - i), None, content)
        for entity, scopes in VERSIONED:
            for scope in scopes:
                for attr in VERSIONED_ATTRS:
                    key = f"{entity}.{attr}"
                    raw = self.version_raw[(entity, attr, scope)]
                    claim(key, raw, self.note_sources[(entity, scope)], self.sentence(entity, attr, raw), scope)
        # policies
        for k in range(11):
            content = " ".join([f"Policy {k + 1:02d} covers {POLICY_AREAS[k]}."] + self.filler(f"policy-{k}", 5))
            self.add_source("policy", f"src-doc-policy-{k + 1:02d}", "document", f"Example Co policy {k + 1:02d}",
                            self.day(-110 + k), None, content)
        # dispute sides
        self.dispute: dict[str, dict] = {}
        for i, key in enumerate(self.disputed_keys):
            entity, attr = key.split(".")
            who = self.speaker(f"dspk-{key}")
            raw = self.dispute_second[key]
            sentence = self.sentence(entity, attr, raw)
            name = PRODUCT_NAME[entity]
            body = f"{sentence}. {self.filler(f'dsp-{key}', 1)[0]}"
            n = i + 1
            if n % 2 == 1:
                sid, kind, title = f"src-slack-d{n:02d}", "slack", f"Slack thread about {name}"
                content = f"{who['name']}: {body}"
            else:
                sid, kind, title = f"src-mail-d{n:02d}", "email", f"{name} question"
                content = f"Subject: {title}\nFrom: {who['name']}\n\n{body}"
            self.add_source("dispute", sid, kind, title, self.day(-12 + i), who["id"], content)
            self.dispute[key] = {"id": sid, "speaker": who, "sentence": sentence, "raw": raw,
                                 "first_raw": self.raw[key], "n": n}
        for key in self.disputed_keys:
            entry = self.dispute[key]
            claim(key, entry["raw"], entry["id"], entry["sentence"])

    def build_work(self) -> None:
        # actions
        statuses = self.shuffle("status", ["open"] * 24 + ["waiting"] * 14 + ["blocked"] * 4 + ["done"] * 10
                                + ["sent"] * 4 + ["proposed"] * 4)
        eligible = [n for n in range(60) if statuses[n] not in ("done", "proposed")]
        buckets = self.shuffle("due-bucket", ["over"] * 9 + ["soon"] * 14 + ["later"] * 23)
        bucket_of = dict(zip(eligible, buckets))
        self.actions: list[dict] = []
        for n in range(60):
            verb = VERBS[self.draw(f"verb-{n}", 6)]
            noun = f"{ADJECTIVES[n // 6]} {THINGS[n % 6]}"
            status = statuses[n]
            owner = self.speaker(f"aown-{n}")
            waiting = self.external(f"await-{n}") if status in ("waiting", "blocked", "sent") else None
            project = WORKSTREAMS[n % 3][0] if n < 30 else PROJECTS[n % 3][0]
            due = None
            if n in bucket_of:
                bucket = bucket_of[n]
                if bucket == "over":
                    due = self.day(-1 - self.draw(f"due-{n}", 15))
                elif bucket == "soon":
                    due = self.day(self.draw(f"due-{n}", 8))
                else:
                    due = self.day(8 + self.draw(f"due-{n}", 53))
            asked = self.day(-10 - self.draw(f"ev-{n}", 40))
            action = {"id": f"act-{n + 1:03d}", "n": n, "verb": verb, "noun": noun, "title": f"{verb} the {noun}",
                      "status": status, "owner": owner, "waiting": waiting, "project": project, "due": due,
                      "asked_on": asked, "note": None, "bucket": bucket_of.get(n)}
            self.actions.append(action)
        # evidence sources (the owner wrote them)
        for action in self.actions:
            n = action["n"] + 1
            sentence = f"{action['owner']['name']} will {action['verb'].lower()} the {action['noun']}"
            if action["waiting"]:
                sentence += f" once {action['waiting']['name']} sends the input"
            if action["due"]:
                sentence += f", due {action['due']}"
            sentence += "."
            action["evidence_sentence"] = sentence
            extra = self.filler(f"aev-{n}", 1 + self.draw(f"aevn-{n}", 2))
            if n % 2 == 1:
                sid, kind, title = f"src-mail-a{n:03d}", "email", action["noun"]
                content = f"Subject: {title}\nFrom: {action['owner']['name']}\n\n" + " ".join([sentence, *extra])
            else:
                sid, kind, title = f"src-slack-a{n:03d}", "slack", f"Slack thread about {action['noun']}"
                content = f"{action['owner']['name']}: " + " ".join([sentence, *extra])
            self.add_source("action", sid, kind, title, action["asked_on"], action["owner"]["id"], content)
            action["evidence"] = sid
        # note-bearing actions
        bearing = [a for a in self.actions if a["due"] and a["status"] in ("open", "waiting")][:5]
        if len(bearing) != 5:
            raise BenchError("fewer than five actions can carry a note")
        for a in bearing:
            due = datetime.date.fromisoformat(a["due"]) + datetime.timedelta(days=7)
            a["note"] = f"Chase for the {a['noun']} by {due.isoformat()}."
            a["note_date"] = due.isoformat()
        self.bearing = bearing
        # checklist items
        states = self.shuffle("state", ["done"] * 45 + ["in_progress"] * 35 + ["waiting"] * 25 + ["blocked"] * 8
                              + ["not_started"] * 35 + ["not_applicable"] * 2)
        self.items: list[dict] = []
        for n in range(150):
            area, control = AREAS[n % 15], CONTROLS[n // 15]
            state = states[n]
            land = self.day(self.draw(f"clb-{n}", 80) - 20) if self.draw(f"cland-{n}", 3) < 2 else None
            waiting = self.external(f"cwait-{n}") if state == "waiting" else None
            self.items.append({
                "id": f"chk-{n + 1:03d}", "n": n, "title": f"{control} {area}", "section": area.capitalize(),
                "project": PROJECTS[n % 3][0], "state": state, "owner": self.people[self.draw(f"cown-{n}", 28)],
                "waiting": waiting, "land_by": land, "sort_order": n + 1, "detail": None, "review": n // 5,
            })
        traps = [i for i in self.items if i["state"] == "waiting" and i["land_by"]][:5]
        if len(traps) != 5:
            raise BenchError("fewer than five waiting items have a land_by")
        for i in traps:
            for attempt in range(100):
                who = self.external(f"ctrap-{i['n']}" if attempt == 0 else f"ctrap-{i['n']}|{attempt}")
                if who is not i["waiting"] and who["id"] != i["waiting"]["id"]:
                    break
            else:
                raise BenchError("no trap person")
            i["detail"] = f"Probably waiting on {who['name']} now."
            i["detail_person"] = who
        self.trap_items = traps
        # checklist review meetings
        for s in range(30):
            chunk = self.items[5 * s:5 * s + 5]
            attendees = ", ".join(self.speaker(f"att-{s}-{j}")["name"] for j in range(3))
            lines = [f"Checklist review {s + 1:02d}.", f"Attendees: {attendees}."]
            for item in chunk:
                text = f"{item['title']} {STATE_PHRASES[item['state']]}"
                if item["waiting"]:
                    text += f", waiting on {item['waiting']['name']}"
                lines.append(text + ".")
            lines += self.filler(f"rev-{s}", 3)
            self.add_source("review", f"src-mtg-c{s + 1:02d}", "meeting_transcript", f"Checklist review {s + 1:02d}",
                            self.day(-30 + s // 2), None, "\n".join(lines))
            for item in chunk:
                item["review_id"] = f"src-mtg-c{s + 1:02d}"
        # decisions
        self.decisions: list[dict] = []
        for k, title in enumerate(DECISION_TITLES):
            who = self.speaker(f"dby-{k}")
            quote = "We agreed to " + title[0].lower() + title[1:]
            date = self.day(-45 + 3 * k)
            sid = f"src-mtg-d{k + 1:02d}"
            content = " ".join([f"Decision meeting {k + 1:02d}.", f"{who['name']} chaired.", quote + ".",
                                *self.filler(f"dm-{k}", 3)])
            self.add_source("decision", sid, "meeting_transcript", f"Decision meeting {k + 1:02d}", date, None, content)
            self.decisions.append({"id": f"dec-{k + 1:03d}", "title": title, "quote": quote, "decided_on": date,
                                   "by": who, "project": PROJECTS[k % 3][0], "source": sid})
        # working notes (20)
        for k in range(20):
            content = " ".join(["Working note.", *self.filler(f"wn-{k}", 2)])
            self.add_source("workingnote", f"src-note-{k + 1:02d}", "user_note", f"Working note {k + 1:02d}",
                            self.day(-8 - k), None, content)
        # records (10)
        self.records: list[dict] = []
        for index, key in enumerate(TRAP_KEYS):
            entity, attr = key.split(".")
            body = f"Reminder: {self.sentence(entity, attr, self.trap_raw[key])}."
            title = f"{ATTR[attr][3]} reminder for {PRODUCT_NAME[entity]}"
            self.records.append({"id": f"rec-note-{index + 1:02d}", "title": title, "body": body, "key": key,
                                 "source": f"src-note-{index + 1:02d}"})
        self.records.append({"id": "rec-note-10", "title": "Launch scratch note",
                             "body": "Scratch: the EU launch codename is Project Heron.", "key": None,
                             "source": "src-note-10"})
        for offset, a in enumerate(self.bearing):
            a["note_source"] = f"src-note-{11 + offset:02d}"
        for offset, i in enumerate(self.trap_items):
            i["note_source"] = f"src-note-{16 + offset:02d}"
        # filler sources. Scale 4 multiplies only this history, until the markdown is about 4 times larger:
        # people, projects, work and facts stay the same, so every question keeps its target, and A8 can show that
        # the brain's reads stay flat while the markdown grows.
        for kind, prefix, count, title in (("email", "mail", 65, "Weekly catch up"),
                                           ("slack", "slack", 55, "Slack thread about the week"),
                                           ("meeting_transcript", "mtg", 9, "Team sync"),
                                           ("user_note", "note", 5, "Personal note")):
            for n in range(1, count * FILLER_MULTIPLIER[self.scale] + 1):
                label = f"fill-{prefix}-{n}"
                who = None if kind == "user_note" else self.speaker(f"{label}|a")
                body = " ".join(self.filler(label, 3 + self.draw(f"{label}|c", 8)))
                if kind == "email":
                    content = f"Subject: {title}\nFrom: {who['name']}\n\n{body}"
                elif kind == "slack":
                    content = f"{who['name']}: {body}"
                else:
                    content = body
                self.add_source("filler", f"src-{prefix}-f{n:03d}", kind, title, self.day(-1 - self.draw(f"{label}|d", 100)),
                                who["id"] if who else None, content)

    # -- rows and packets
    def packets(self) -> list[tuple[str, dict]]:
        def source_op(src: dict) -> dict:
            row = {k: src[k] for k in ("id", "kind", "title", "source_date", "author_person_id", "content")}
            for extra in ("external_ref", "supersedes_id"):
                if extra in src:
                    row[extra] = src[extra]
            return {"op": "add_source", "row": row}

        def op(name: str, row: dict, source_id: str) -> dict:
            built = {"op": name, "row": row, "source": {"source_id": source_id, "locator": "para 1"}}
            if name != "add_claim":
                built["base"] = None
            return built

        def packet(agent: str, name: str, intent: str, ops: list[dict]) -> tuple[str, dict]:
            return name, {"packet_version": 1, "change_id": f"{agent}-bench-{name.split('-', 1)[1][:-5]}",
                          "agent": agent, "intent": intent, "ops": ops}

        def packets_of(agent: str, name: str, intent: str, ops: list[dict]) -> list[tuple[str, dict]]:
            """One packet, or at scale 4 as many as the 500-op packet limit needs, named <name>, <name>-2, ..."""
            parts = [ops[n:n + 500] for n in range(0, len(ops), 500)] or [ops]
            return [packet(agent, name if n == 0 else name.replace(".json", f"-{n + 1}.json"), intent, part)
                    for n, part in enumerate(parts)]

        by_group: dict[str, list[dict]] = {}
        for src in self.sources:
            by_group.setdefault(src["_group"], []).append(src)
        out = []
        ops = [source_op(by_group["directory"][0])]
        for p in self.people[1:]:
            ops.append(op("upsert_person", {"id": p["id"], "display_name": p["name"], "org": p["org"], "role": p["role"]},
                          "src-doc-directory"))
        out.append(packet("loader", "0001-people.json", "Add the staff directory and the people in it", ops))
        docs = [s for g in ("sheet", "edition1", "notes", "policy") for s in by_group[g]]
        out.append(packet("loader", "0002-documents.json", "Add the product sheets, first editions, release notes and policies",
                          [source_op(s) for s in docs]))
        mail_chat = [s for s in self.sources if s["kind"] in ("email", "slack")]
        out.extend(packets_of("loader", "0003-mail-and-chat.json", "Add the email and Slack sources", [source_op(s) for s in mail_chat]))
        meet_notes = [s for s in self.sources if s["kind"] in ("meeting_transcript", "user_note")]
        out.extend(packets_of("loader", "0004-meetings-and-notes.json", "Add the meeting transcripts and notes",
                              [source_op(s) for s in meet_notes]))
        second_ids = {self.edition[k]["ids"][1] for k in self.updated_keys}
        claim_ops = []
        late_claims = []
        for c in self.claims:
            row = {k: c[k] for k in ("id", "key", "value_raw", "basis", "quote")}
            if "scope" in c:
                row["scope"] = c["scope"]
            built = op("add_claim", row, c["source_id"])
            (late_claims if c["source_id"] in second_ids else claim_ops).append(built)
        out.append(packet("loader", "0005-claims.json", "Add the quoted facts", claim_ops))
        out.append(packet("loader", "0006-work.json", "Add the projects, actions, checklist, decisions and notes",
                          self.work_ops(op)))
        out.append(packet("owner", "0007-successor-documents.json", "Add the second editions",
                          [source_op(s) for s in by_group["edition2"]]))
        out.append(packet("loader", "0008-updated-claims.json", "Add the second editions' claims", late_claims))
        return out

    def work_ops(self, op) -> list[dict]:
        applied = self.applied_at
        ops = []
        for pid, title in PROJECTS:
            ops.append(op("upsert_project", {"id": pid, "kind": "project", "title": title, "status": "active",
                                             "sort_order": 100}, "src-doc-directory"))
        for a in self.actions:
            row = {"id": a["id"], "title": a["title"], "status": a["status"], "status_basis": "reported",
                   "project_id": a["project"], "owner_person_id": a["owner"]["id"], "last_verified_at": applied,
                   "asked_on": a["asked_on"]}
            if a["waiting"]:
                row["waiting_on_person_id"] = a["waiting"]["id"]
            if a["due"]:
                row["due_date"] = a["due"]
                row["due_kind"] = "deadline"
            if a["status"] == "done":
                row["closed_at"] = applied
            if a["note"]:
                row["next_step"] = a["note"]
            # Strict mode checks due_date and asked_on against the cited source, so a note-bearing
            # action cites its evidence source, not a working note.
            ops.append(op("upsert_action", row, a["evidence"]))
        for i in self.items:
            row = {"id": i["id"], "project_id": i["project"], "section": i["section"], "title": i["title"],
                   "state": i["state"], "state_basis": "reported", "owner_person_id": i["owner"]["id"],
                   "last_verified_at": applied, "sort_order": i["sort_order"]}
            if i["waiting"]:
                row["waiting_on_person_id"] = i["waiting"]["id"]
            if i["land_by"]:
                row["land_by"] = i["land_by"]
            if i["detail"]:
                row["detail"] = i["detail"]
            ops.append(op("upsert_checklist_item", row, i["note_source"] if i["detail"] else i["review_id"]))
        for d in self.decisions:
            ops.append(op("upsert_decision", {"id": d["id"], "title": d["title"], "quote": d["quote"],
                                              "decided_on": d["decided_on"], "decided_by_person_id": d["by"]["id"],
                                              "status": "in_effect", "project_id": d["project"]}, d["source"]))
        for r in self.records:
            ops.append(op("upsert_record", {"id": r["id"], "kind": "note", "title": r["title"], "body": r["body"]},
                          r["source"]))
        return ops

    # -- config
    def config_text(self) -> str:
        p = self.principal
        lines = ["[instance]", 'name = "exampleco"', 'timezone = "UTC"', 'org = "Example Co"', "",
                 "[principal]", f'person_id = "{p["id"]}"', f'display_name = "{p["name"]}"', f'names = ["{p["first"]}"]', ""]
        for wid, title, desc, order in WORKSTREAMS:
            lines += ["[[workstreams]]", f'id = "{wid}"', f'title = "{title}"', f'description = "{desc}"',
                      f"sort_order = {order}", ""]
        for entity, name in PRODUCTS:
            for attr in ATTRIBUTES:
                key = f"{entity}.{attr[0]}"
                lines += ["[[fact_keys]]", f'key = "{key}"', f'value_type = "{attr[1]}"']
                if attr[2]:
                    lines.append(f'unit = "{attr[2]}"')
                if attr[1] == "enum":
                    lines.append('allowed_values = ["au", "eu", "us"]')
                if attr[1] == "bool":
                    lines.append('value_words = {true = ["included"], false = ["not included"]}')
                lines += [f"stale_after_days = {180 if attr[0] == 'price' else 365}",
                          f'description = "{attr[3]} of {name}."', ""]
        for entity, attr, vtype, unit in UNSET_KEYS:
            lines += ["[[fact_keys]]", f'key = "{entity}.{attr}"', f'value_type = "{vtype}"']
            if unit:
                lines.append(f'unit = "{unit}"')
            lines += ["stale_after_days = 365", 'description = "Not yet recorded."', ""]
        lines += ['[agents.loader]', 'kind = "collector"', "manual_sources = true", 'operations = "*"',
                  'approval = "none"', "approvers = []", "author_principal = false", "",
                  '[agents.owner]', 'kind = "collector"', "manual_sources = true", 'operations = ["add_source"]',
                  'approval = "none"', "approvers = []", "author_principal = true", "",
                  '[agents.scan]', 'kind = "extractor"',
                  'operations = ["apply_candidate", "upsert_decision", "upsert_action", "upsert_thread_link", "add_claim"]',
                  'approval = "verified"', 'approvers = ["owner"]', "author_principal = false", "",
                  "[collectors]", "", "[truth.entities]"]
        for entity, names in ENTITY_ALIASES.items():
            lines.append(f"{entity} = {json.dumps(names)}")
        lines += ["", "[truth.attributes]"]
        for attr, names in ATTRIBUTE_ALIASES:
            lines.append(f"{attr} = {json.dumps(names)}")
        return "\n".join(lines) + "\n"

    # -- selectors and questions
    def selectors(self) -> dict:
        waiting = [a for a in self.actions if a["status"] == "waiting"]
        t_act1 = waiting[1]
        t_act2 = self.bearing[0]
        if t_act1 is t_act2:
            t_act1 = waiting[2]
        t_item1 = self.trap_items[0]
        later = [i for i in self.items if i["land_by"] and i["state"] not in ("done", "not_applicable")]
        t_item2 = later[5]
        if t_item2 is t_item1:
            # Without this fallback about 8% of seeds fail.
            t_item2 = later[6]
        open_actions = [a for a in self.actions if a["status"] in OPEN_ACTION_STATUSES]

        def busiest(rows) -> dict:
            counts: dict[str, int] = {}
            for row in rows:
                if row["waiting"]:
                    counts[row["waiting"]["id"]] = counts.get(row["waiting"]["id"], 0) + 1
            top = max(counts.values())
            who = sorted(pid for pid, n in counts.items() if n == top)[0]
            return next(p for p in self.people if p["id"] == who)

        p_act = busiest(open_actions)
        p_item = busiest([i for i in self.items if i["state"] == "waiting"])
        t_wait = sorted(waiting, key=lambda a: (a["asked_on"], a["id"]))[0]
        soon = [a for a in open_actions if a["due"] and self.day(0) <= a["due"] <= self.day(7)]
        over = [a for a in open_actions if a["due"] and a["due"] < self.day(0)]
        return {"t_act1": t_act1, "t_act2": t_act2, "t_item1": t_item1, "t_item2": t_item2, "p_act": p_act,
                "p_item": p_item, "t_wait": t_wait, "soon": soon, "over": over, "open": open_actions}

    def disputed_item(self, key: str) -> list[str]:
        """An answer_must item naming one disputed fact: its key, or its product and attribute in words."""
        entity, attr = key.split(".")
        product = PRODUCT_NAME[entity]
        aliases = dict(ATTRIBUTE_ALIASES)[attr]
        words = attr.replace("_", " ")
        return [key, f"{product} {words}", f"{words} of {product}",
                *[f"{product} {alias}" for alias in aliases], *[f"{alias} of {product}" for alias in aliases]]

    def questions(self) -> list[dict]:
        sel = self.selectors()

        def forms(raw: str, key: str) -> list[str]:
            return value_forms(raw, ATTR[key.rpartition(".")[2]][1])

        def wrong(raw: str, key: str) -> list[str]:
            return wrong_forms(raw, ATTR[key.rpartition(".")[2]][1])

        def q(qid, group, shape, kind, question, path, expect, must, must_not) -> dict:
            calls = [{"tool": t, "arguments": a} for t, a in path]
            return {"id": qid, "group": group, "shape": shape, "kind": kind, "question": question,
                    "minimal_path": calls, "expect": expect, "answer_must": must, "answer_must_not": must_not}

        def ids_min(rows) -> str:
            return min(r["id"] for r in rows)

        out = []
        # lookups
        key = "expro.price"
        out.append(q("q01", "lookups", "Current value of one fact", "answer", "What does Example Pro cost per seat?",
                     [("fact_get", {"key": "Example Pro price"})],
                     {"0:facts.0.key": key, "0:facts.0.status": "current", "0:facts.0.value": typed_value("price", self.raw[key])},
                     [forms(self.raw[key], key)], wrong(self.trap_raw[key], key)))
        i1 = sel["t_item1"]
        out.append(q("q02", "lookups", "State of one checklist item", "answer",
                     f'Where does the checklist item "{i1["title"]}" stand?',
                     [("records_query", {"entity": "checklist_item", "filters": {"title": {"contains": i1["title"]}},
                                         "fields": ["id", "title", "state", "owner_person_id", "waiting_on_person_id", "land_by"]})],
                     {"0:total": 1, "0:rows.0.id": i1["id"], "0:rows.0.state": "waiting",
                      "0:rows.0.owner_person_id": i1["owner"]["id"], "0:rows.0.waiting_on_person_id": i1["waiting"]["id"],
                      "0:rows.0.land_by": i1["land_by"]},
                     ["waiting", i1["owner"]["name"], i1["waiting"]["name"], i1["land_by"]],
                     [i1["detail_person"]["name"]]))
        out.append(q("q03", "lookups", "Fact the brain does not hold", "abstain", "What is Example Co's annual revenue?",
                     [("fact_get", {"key": "Example Co annual revenue"})], {"0:$error": "unknown_key"},
                     [list(ABSTAIN_PHRASES)], []))
        out.append(q("q04", "lookups", "Fact only an unsourced note holds", "note_only", "What is the codename of the EU launch?",
                     [("search", {"query": "EU launch codename"}), ("get", {"entity": "record", "id": "rec-note-10"})],
                     {"0:hits.0.id": "rec-note-10", "0:hits.0.match": "note",
                      "1:notes.label": "Written by an agent, unsourced. Not a fact."},
                     [list(NOTE_PHRASES), "Project Heron"], []))
        # waiting-on
        a1 = sel["t_act1"]
        out.append(q("q05", "waiting-on", "Who an action waits on and since when", "answer",
                     f'Who is "{a1["title"]}" waiting on, and since when was it asked? Answer with the date as YYYY-MM-DD.',
                     [("records_query", {"entity": "action", "filters": {"title": {"contains": a1["noun"]}},
                                         "fields": ["id", "title", "status", "waiting_on_person_id", "asked_on"]})],
                     {"0:total": 1, "0:rows.0.id": a1["id"], "0:rows.0.waiting_on_person_id": a1["waiting"]["id"],
                      "0:rows.0.asked_on": a1["asked_on"]},
                     [a1["waiting"]["name"], a1["asked_on"]], []))
        pa = sel["p_act"]
        acts = sorted([a for a in sel["open"] if a["waiting"] and a["waiting"]["id"] == pa["id"]], key=lambda a: a["id"])
        out.append(q("q06", "waiting-on", "Open actions waiting on one person", "answer",
                     f"Which open actions (every status except done and proposed) are waiting on {pa['name']}?",
                     [("records_query", {"entity": "action", "filters": {"waiting_on_person_id": pa["id"], "open": True},
                                         "fields": ["id", "title", "status"], "order_by": "id"})],
                     {"0:total": len(acts), "0:rows.0.id": acts[0]["id"]}, [a["title"] for a in acts],
                     [a["title"] for a in self.actions if a["waiting"] and a["waiting"]["id"] != pa["id"]]))
        pi = sel["p_item"]
        its = sorted([i for i in self.items if i["state"] == "waiting" and i["waiting"]["id"] == pi["id"]], key=lambda i: i["id"])
        out.append(q("q07", "waiting-on", "Checklist items waiting on one person", "answer",
                     f"Which checklist items are waiting on {pi['name']}?",
                     [("records_query", {"entity": "checklist_item",
                                         "filters": {"waiting_on_person_id": pi["id"], "state": "waiting"},
                                         "fields": ["id", "title", "state"], "order_by": "id"})],
                     {"0:total": len(its), "0:rows.0.id": its[0]["id"]}, [i["title"] for i in its],
                     [i["title"] for i in self.items if i["state"] == "waiting" and i["waiting"]["id"] != pi["id"]]))
        tw = sel["t_wait"]
        out.append(q("q08", "waiting-on", "Longest waiting action", "answer",
                     "Which action with status waiting has been waiting the longest? Answer with the date it was asked as YYYY-MM-DD.",
                     [("records_query", {"entity": "action", "filters": {"status": "waiting"},
                                         "fields": ["id", "title", "waiting_on_person_id", "asked_on"],
                                         "order_by": "asked_on", "limit": 1})],
                     {"0:rows.0.id": tw["id"], "0:rows.0.asked_on": tw["asked_on"]}, [tw["title"], tw["asked_on"]], []))
        # due-dates
        a2 = sel["t_act2"]
        out.append(q("q09", "due-dates", "Due date of one action", "answer",
                     f'When is "{a2["title"]}" due? Answer with the date as YYYY-MM-DD.',
                     [("records_query", {"entity": "action", "filters": {"title": {"contains": a2["noun"]}},
                                         "fields": ["id", "title", "due_date", "due_kind"]})],
                     {"0:total": 1, "0:rows.0.id": a2["id"], "0:rows.0.due_date": a2["due"]},
                     [a2["due"]], [a2["note_date"]]))
        soon = sorted(sel["soon"], key=lambda a: a["id"])
        out.append(q("q10", "due-dates", "Open actions due soon", "answer",
                     f"How many open actions (every status except done and proposed) are due from {self.day(0)} to {self.day(7)} inclusive?",
                     [("records_query", {"entity": "action",
                                         "filters": {"open": True, "due_date": {"gte": self.day(0), "lte": self.day(7)}},
                                         "fields": ["id", "title", "due_date"], "order_by": "id", "limit": 100})],
                     {"0:total": len(soon), "0:rows.0.id": soon[0]["id"]}, [str(len(soon))], []))
        over = sorted(sel["over"], key=lambda a: a["id"])
        out.append(q("q11", "due-dates", "Open actions overdue", "answer", f"How many open actions (every status except done and proposed) have a due date before {self.day(0)}?",
                     [("records_query", {"entity": "action", "filters": {"open": True, "due_date": {"lt": self.day(0)}},
                                         "fields": ["id", "due_date"], "order_by": "id", "limit": 100})],
                     {"0:total": len(over), "0:rows.0.id": over[0]["id"]}, [str(len(over))], []))
        i2 = sel["t_item2"]
        out.append(q("q12", "due-dates", "Land by date of one item", "answer",
                     f'When must "{i2["title"]}" land by? Answer with the date as YYYY-MM-DD.',
                     [("records_query", {"entity": "checklist_item", "filters": {"title": {"contains": i2["title"]}},
                                         "fields": ["id", "title", "state", "land_by"]})],
                     {"0:total": 1, "0:rows.0.id": i2["id"], "0:rows.0.land_by": i2["land_by"]}, [i2["land_by"]], []))
        # product truth
        key = "exbasic.seats"
        ed = self.edition[key]
        out.append(q("q13", "product-truth", "Fact updated by a new edition", "answer",
                     "How many seats does Example Basic include now?",
                     [("fact_get", {"key": "Example Basic seat count"})],
                     {"0:facts.0.key": key, "0:facts.0.status": "current", "0:facts.0.value": typed_value("seats", ed["raws"][1]),
                      "0:facts.0.source.id": ed["ids"][1]},
                     [forms(ed["raws"][1], key)], wrong(ed["raws"][0], key)))
        key = "excloud.max_context"
        old, new = self.version_raw[("excloud", "max_context", "1.2")], self.version_raw[("excloud", "max_context", "1.3")]
        out.append(q("q14", "product-truth", "Fact for an older version", "answer",
                     "What was the context window of Example Cloud 1.2?",
                     [("fact_get", {"key": "Example Cloud context window", "scope": "1.2"})],
                     {"0:facts.0.key": key, "0:facts.0.claims.scope": "1.2",
                      "0:facts.0.source.id": self.note_sources[("excloud", "1.2")]},
                     [forms(old, key)], wrong(new, key)))
        out.append(q("q15", "product-truth", "Registered fact with no value", "abstain",
                     "When is the Example Scale penetration test due?",
                     [("fact_get", {"key": "Example Scale penetration test"})], {"0:$error": "no_value"},
                     [list(ABSTAIN_PHRASES)], []))
        key = "exstudio.launch_date"
        out.append(q("q16", "product-truth", "A date fact", "answer",
                     "When does Example Studio launch? Answer with the date as YYYY-MM-DD.",
                     [("fact_get", {"key": "Example Studio launch date"})],
                     {"0:facts.0.key": key, "0:facts.0.value": self.raw[key]}, [self.raw[key]], []))
        # conflicts
        def disputed_q(qid, text, call_key, key) -> dict:
            entry = self.dispute[key]
            return q(qid, "conflicts", "Disputed fact", "answer", text, [("fact_get", {"key": call_key})],
                     {"0:facts.0.key": key, "0:facts.0.status": "disputed"},
                     [forms(entry["first_raw"], key), forms(entry["raw"], key)], [])

        out.append(disputed_q("q17", "What does Example Basic cost per seat?", "exbasic.price", "exbasic.price"))
        out.append(disputed_q("q18", "Which region does Example Cloud run in?", "excloud.region", "excloud.region"))
        out.append(q("q19", "conflicts", "All disputed facts", "answer", "Which facts are in dispute right now?",
                     [("records_query", {"entity": "signal", "filters": {"kind": "conflict", "status": "open"},
                                         "fields": ["id", "subject_id"], "order_by": "subject_id", "limit": 25})],
                     {"0:total": 10, "0:rows.0.subject_id": sorted(self.disputed_keys)[0]},
                     ["10", *[self.disputed_item(k) for k in sorted(self.disputed_keys)]], []))
        out.append(disputed_q("q20", "How much GPU memory does each Example Team node have?", "Example Team VRAM",
                              "exteam.gpu_memory"))
        # attribution
        for qid, key, text in (
            ("q21", "exteam.seats", "Who says Example Team includes {raw}, and what exactly did they write?"),
            ("q24", "exstudio.support_hours", "Who says Example Studio support replies within {raw}?"),
        ):
            entry = self.dispute[key]
            out.append(q(qid, "attribution", "Who said one side of a dispute", "answer",
                         text.replace("{raw}", entry["raw"]),
                         [("fact_get", {"key": key}), ("get", {"entity": "source", "id": entry["id"]})],
                         {"0:facts.0.status": "disputed", "1:row.author_person_id": entry["speaker"]["id"]},
                         [entry["speaker"]["name"], entry["sentence"]], []))
        key = "exedge.price"
        sentence = self.sentence("exedge", "price", self.raw[key])
        out.append(q("q22", "attribution", "Quote behind a fact", "answer",
                     "Quote the sentence that says what Example Edge costs per seat.",
                     [("fact_get", {"key": key, "claims": "all"})],
                     {"0:facts.0.claims.values.0.quote": sentence}, [sentence], wrong(self.trap_raw[key], key)))
        d4 = self.decisions[3]
        out.append(q("q23", "attribution", "Who decided and what was said", "answer",
                     "Who decided to adopt quarterly access reviews, and what was said?",
                     [("records_query", {"entity": "decision", "filters": {"title": {"contains": "quarterly access"}},
                                         "fields": ["id", "title", "quote", "decided_by_person_id", "decided_on"]})],
                     {"0:total": 1, "0:rows.0.id": d4["id"], "0:rows.0.decided_by_person_id": d4["by"]["id"],
                      "0:rows.0.quote": d4["quote"]},
                     [d4["by"]["name"], d4["quote"]], []))
        out.sort(key=lambda item: item["id"])
        return out

    # -- markdown
    def markdown(self) -> dict[str, str]:
        files: dict[str, str] = {}
        name = {p["id"]: p["name"] for p in self.people}
        files["markdown/people.md"] = "# People\n\n" + "".join(
            f"- {p['name']}: {p['role']}, {p['org']}\n" for p in self.people)
        lines = ["# Facts", ""]
        for entity, product in PRODUCTS:
            lines += [f"## {product}", ""]
            for attr in ATTRIBUTES:
                key = f"{entity}.{attr[0]}"
                label = attr[0].replace("_", " ")
                if key in self.versioned_set:
                    scopes = dict(VERSIONED)[entity]
                    value = "; ".join(f"version {s}: {self.version_raw[(entity, attr[0], s)]}" for s in scopes)
                elif key in self.updated_set:
                    value = self.update_second[key]
                elif key in self.disputed_set:
                    value = f"{self.raw[key]} or {self.dispute_second[key]}"
                else:
                    value = self.raw[key]
                lines.append(f"- {product} {label}: {value}")
            lines.append("")
        files["markdown/facts.md"] = "\n".join(lines)
        files["markdown/notes.md"] = "# Notes\n\n" + "".join(f"- {r['title']}: {r['body']}\n" for r in self.records)

        def item_line(i) -> str:
            text = f"- [{i['state']}] {i['title']}, owner {i['owner']['name']}"
            if i["waiting"]:
                text += f", waiting on {i['waiting']['name']}"
            if i["land_by"]:
                text += f", land by {i['land_by']}"
            return text + (f". {i['detail']}" if i["detail"] else "")

        def action_line(a) -> str:
            text = f"- [{a['status']}] {a['title']}, owner {a['owner']['name']}"
            if a["waiting"]:
                text += f", waiting on {a['waiting']['name']} since {a['asked_on']}"
            if a["due"]:
                text += f", due {a['due']}"
            return text + (f". {a['note']}" if a["note"] else "")

        for pid, title in PROJECTS:
            rows = ["# " + title, "", "## Checklist", ""]
            rows += [item_line(i) for i in self.items if i["project"] == pid]
            rows += ["", "## Actions", ""]
            rows += [action_line(a) for a in self.actions if a["project"] == pid]
            files[f"markdown/projects/{pid}.md"] = "\n".join(rows) + "\n"
        rows = ["# Actions", ""]
        for wid, title, _desc, _order in WORKSTREAMS:
            rows += [f"## {title}", ""] + [action_line(a) for a in self.actions if a["project"] == wid] + [""]
        files["markdown/actions.md"] = "\n".join(rows)
        files["markdown/decisions.md"] = "# Decisions\n\n" + "".join(
            f'- {d["decided_on"]}: {d["title"]}. {d["by"]["name"]}: "{d["quote"]}"\n' for d in self.decisions)
        for kind in ("document", "email", "meeting_transcript", "slack", "user_note"):
            rows = [f"# Sources: {kind}", ""]
            for s in self.sources:
                if s["kind"] != kind:
                    continue
                author = name[s["author_person_id"]] if s["author_person_id"] else "none"
                rows += [f"## {s['title']}", f"Date: {s['source_date']}. Author: {author}.", "", s["content"], ""]
            files[f"markdown/sources/{kind}.md"] = "\n".join(rows)
        return files


OPEN_ACTION_STATUSES = ("open", "prepared", "scheduled", "sent", "waiting", "blocked")


# -- invariants -------------------------------------------------------------------------


def check_question_text(item: dict) -> None:
    """No answer_must_not string and no alternative of one answer_must item matches inside any
    alternative of another item or inside an answer_must_not string of the same question."""
    items = [[alt] if isinstance(alt, str) else list(alt) for alt in item["answer_must"]]
    must_not = list(item["answer_must_not"])
    for i, alternatives in enumerate(items):
        for a in alternatives:
            for j, others in enumerate(items):
                if i != j and any(matches(a, b) for b in others):
                    raise BenchError(f"{item['id']}: {a!r} matches inside another answer_must item")
            for b in must_not:
                if matches(a, b):
                    raise BenchError(f"{item['id']}: {a!r} matches inside the answer_must_not string {b!r}")
    for m in must_not:
        for alternatives in items:
            for b in alternatives:
                if matches(m, b):
                    raise BenchError(f"{item['id']}: the answer_must_not string {m!r} matches inside {b!r}")


def check_invariants(b: Builder, questions: list[dict]) -> None:
    seen: set[str] = set()
    for group in (b.sources, b.claims, b.actions, b.items, b.decisions, b.records, b.people):
        for row in group:
            if row["id"] in seen:
                raise BenchError(f"duplicate id {row['id']}")
            seen.add(row["id"])
    for claim in b.claims:
        if not matches(claim["quote"], b.source_by_id[claim["source_id"]]["content"]):
            raise BenchError(f"the quote of {claim['id']} is not in {claim['source_id']}")
    for d in b.decisions:
        if not matches(d["quote"], b.source_by_id[d["source"]]["content"]):
            raise BenchError(f"the quote of {d['id']} is not in {d['source']}")
    nouns = [a["noun"] for a in b.actions]
    if len(set(nouns)) != len(nouns):
        raise BenchError("two actions share a noun")
    if len(b.bearing) != 5:
        raise BenchError("the five note-bearing actions are missing")
    for i in b.trap_items:
        if i["detail_person"]["id"] in (i["waiting"]["id"], i["owner"]["id"]) or i["detail_person"]["index"] < 28:
            raise BenchError(f"the detail trap of {i['id']} names its waiter or owner")
    sel = b.selectors()
    if sel["t_act1"] is sel["t_act2"]:
        raise BenchError("T_act1 and T_act2 are the same action")
    if sel["t_item1"] is sel["t_item2"]:
        raise BenchError("T_item1 and T_item2 are the same item")
    for sentence in FILLER:
        text = sentence.lower()
        if re.search(r"[0-9]", text) or any(day in text for day in WEEKDAYS):
            raise BenchError("a filler sentence holds a digit or a weekday name")
    for item in questions:
        check_question_text(item)
    for key in TRAP_KEYS:
        entity, attr = key.split(".")
        if matches(b.trap_raw[key], b.raw[key]) or matches(b.trap_raw[key], b.sentence(entity, attr, b.raw[key])):
            raise BenchError(f"the trap of {key} matches inside its sourced value")


# -- output ------------------------------------------------------------------------------------------


def dumps(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("ascii")


def generate(seed: int, scale: int, today: datetime.date) -> dict[str, bytes]:
    if scale not in FILLER_MULTIPLIER:
        raise BenchError("scale must be 1 or 4")
    b = Builder(seed, today, scale)
    b.build_people()
    b.build_facts()
    b.build_sources()
    b.build_work()
    try:
        questions = b.questions()
        check_invariants(b, questions)
    except (IndexError, KeyError, ValueError) as exc:
        raise BenchError(f"a question selector finds no target ({type(exc).__name__})") from exc
    files: dict[str, bytes] = {"exampleco.toml": b.config_text().encode("ascii"),
                               "questions.json": dumps({"format": FORMAT, "questions": questions})}
    names = []
    for name, packet in b.packets():
        files[f"packets/{name}"] = dumps(packet)
        names.append(name)
    for path, text in b.markdown().items():
        files[path] = text.encode("ascii")
    ordered = {path: files[path] for path in sorted(files)}
    manifest = {"format": FORMAT, "seed": seed, "scale": scale, "today": today.isoformat(), "now": b.now,
                "applied_at": b.applied_at, "packets": names,
                "files": {path: hashlib.sha256(data).hexdigest() for path, data in ordered.items()}}
    ordered["MANIFEST.json"] = dumps(manifest)
    return ordered


def _whole(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number from 0")
    return int(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate.py", description="Write the Example Co benchmark dataset.")
    parser.add_argument("--today", default=None, help="the dataset's date, YYYY-MM-DD (required)")
    parser.add_argument("--seed", type=_whole, default=1)
    parser.add_argument("--scale", type=int, choices=(1, 4), default=1)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if ns.today is None or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", ns.today):
        print("bench: --today must be a date written YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        today = datetime.date.fromisoformat(ns.today)
    except ValueError:
        print("bench: --today must be a date written YYYY-MM-DD", file=sys.stderr)
        return 2
    if ns.scale not in FILLER_MULTIPLIER:
        print("bench: scale must be 1 or 4", file=sys.stderr)
        return 2
    out = Path(ns.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"bench: {out} is not empty", file=sys.stderr)
        return 2
    try:
        files = generate(ns.seed, ns.scale, today)
    except OverflowError:
        print("bench: --today is too close to the limit of the calendar", file=sys.stderr)
        return 2
    except BenchError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 3
    for path, data in files.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"bench: wrote {len(files)} files, {sum(len(d) for d in files.values())} bytes, to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
