from __future__ import annotations

import re
from dataclasses import dataclass

from brain import advice, schemas
from brain.text import single_line

MIN_QUOTE_WORDS = 3
ATTRIBUTION_MIN_QUOTE_WORDS = 5
ATTRIBUTION_WINDOW = 6
ATTRIBUTION_VERBS = frozenset({
    "said", "says", "decided", "decides", "chose", "chooses", "agreed", "agrees", "approved", "approves",
    "accepted", "accepts", "wants", "wanted", "ordered", "orders", "told", "tells", "confirmed", "confirms",
    "prefers", "preferred", "insisted", "promised", "committed",
    "asked", "asks", "instructed", "instructs", "needs", "needed",
    "wrote", "writes", "replied", "replies", "thinks", "thought", "suggested", "suggests",
    "requested", "requests", "mentioned", "mentions",
})
# A pronoun after a clause break carries the name on, so "Avery seeks the ask: He wants ..." keeps the verb
# (1.6.0 stopped flagging that shape, because the capitalised pronoun looked like a new subject).
PRONOUNS = frozenset({"he", "she", "they", "his", "her", "their", "him", "them"})
POSSESSIVE_PHRASES = ("words", "decision", "call", "view", "ask", "preference")
NAME_COLON = r"^\s*(?:[-*" "\u2022" r"]\s*)?{name}\s*:"
BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d)")
CLOSING_QUOTES = "\"\u201d'\u2019"
SNIPPET = 80
CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})
EDGE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")
MATCH_WORD = re.compile(r"[\w']+")
# A colon or semicolon before a space, or a sentence end before a capital, digit, opening mark or the end of
# the text. A full stop inside "1.45pm" or "i.e. the owner", after an initial ("Avery B. Smith") or after a title
# ("Dr. Lee") is not a break.
_AFTER_BREAK = r"[\"')\]]*(?=\s+[A-Z0-9\"'(\[]|\s*$)"
CLAUSE_BREAK = re.compile(
    r"[:;](?=\s|$)"
    r"|(?<!\b[A-Za-z])(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bJr)(?<!\bSr)(?<!\bSt)(?<!\bMrs)\." + _AFTER_BREAK
    + r"|[!?]" + _AFTER_BREAK
)

# The text fields the gate reads, per op. The one list is each op's gate_fields in schemas.OPS.
GATE_FIELDS = {name: spec["gate_fields"] for name, spec in schemas.OPS.items() if spec["gate_fields"]}


@dataclass(frozen=True)
class Principal:
    person_id: str
    display_name: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class CitedSource:
    id: str
    content: str
    author_person_id: str | None
    # 1.9.0: a speaker-lines excerpt holds one person's lines lifted from a thread or transcript, so a quote must
    # sit inside one of its paragraphs. Read as one run of words, two lines would join across whatever someone
    # else said between them.
    by_paragraph: bool = False


# 1.9.0: a source whose external_ref is this prefix and another source's id holds only its author's own lines from
# that source, each copied exactly, one paragraph per line. The gate reads it whenever the other source is cited.
SPEAKER_LINES = "speaker-lines:"
PARAGRAPH_BREAK = re.compile(r"\n[ \t\r]*\n")


def paragraphs(text) -> list[str]:
    """The non-blank paragraphs of `text`, each stripped: the units of a speaker-lines excerpt."""
    return [part.strip() for part in PARAGRAPH_BREAK.split(str(text or "")) if part.strip()]


def source_keys(item: CitedSource) -> list[str]:
    """The match keys a quote is looked for in: the whole source, or each paragraph of a speaker-lines excerpt."""
    if item.by_paragraph:
        return [key for key in (match_key(part) for part in paragraphs(item.content)) if key]
    return [match_key(item.content)]


def is_speaker_lines(row: dict) -> bool:
    return str(row.get("external_ref") or "").startswith(SPEAKER_LINES)


def holds(row: dict, quote_key: str) -> bool:
    """A stored source holds the quote: in its whole content, or in one paragraph of a speaker-lines excerpt. Every
    check that a quote is "in the source" reads it this way, so no path joins two of the excerpt's lines."""
    item = CitedSource(row["id"], row["content"], row.get("author_person_id"), by_paragraph=is_speaker_lines(row))
    return any(contains(key, quote_key) for key in source_keys(item))


def normalise(text) -> str:
    text = str(text or "").lower().translate(CURLY)
    return EDGE_PUNCTUATION.sub("", " ".join(text.split()))  # not one-line: a matching key, never shown


def match_key(text) -> str:
    words = (token.strip("'") for token in MATCH_WORD.findall(str(text or "").lower().translate(CURLY)))
    return " ".join(token for token in words if token)


def contains(source_key: str, span_key: str) -> bool:
    return f" {span_key} " in f" {source_key} "


def join_wrapped(text) -> list[str]:
    lines = str(text or "").splitlines()
    out: list[str] = []
    for line in lines:
        if (
            out
            and out[-1].strip()
            and line.lstrip()[:1].islower()
            and not out[-1].rstrip().endswith((".", "?", "!", ":", *CLOSING_QUOTES))
            and not BULLET.match(out[-1])
            and not BULLET.match(line)
        ):
            out[-1] = out[-1].rstrip() + " " + line.lstrip()
        else:
            out.append(line)
    return out


def sentences(text) -> list[str]:
    out: list[str] = []
    for line in join_wrapped(text):
        buf: list[str] = []
        closer = None
        for i, ch in enumerate(line):
            buf.append(ch)
            if closer is None:
                if ch == '"':
                    closer = '"'
                elif ch == "“":
                    closer = "”"
            elif ch == closer:
                closer = None
            if closer is None and ch in ".?!" and (i + 1 == len(line) or line[i + 1].isspace()):
                out.append("".join(buf).strip())
                buf = []
        if "".join(buf).strip():
            out.append("".join(buf).strip())
    return [sentence for sentence in out if sentence]


def quoted_spans(sentence: str) -> list[str]:
    spans = [match.group(1) for match in re.finditer(r'"([^"]*)"', sentence)]
    spans += [match.group(1) for match in re.finditer("“([^”]*)”", sentence)]
    i, n = 0, len(sentence)
    while i < n:
        ch = sentence[i]
        opens = ch == "‘" or (ch == "'" and not (i > 0 and sentence[i - 1].isalnum()))
        if opens and i + 1 < n and not sentence[i + 1].isspace():
            j = i + 1
            while j < n:
                if (
                    sentence[j] in "'’"
                    and not sentence[j - 1].isspace()
                    and not (j + 1 < n and sentence[j + 1].isalnum())
                ):
                    break
                j += 1
            if j < n:
                spans.append(sentence[i + 1:j])
                i = j + 1
                continue
        i += 1
    return spans


def attributes_to(sentence: str, names, *, labels=()) -> bool:
    lowered = sentence.lower().translate(CURLY)
    for name in (*names, *labels):
        if re.match(NAME_COLON.format(name=re.escape(str(name).lower().translate(CURLY))), lowered):
            return True
    cased = sentence.translate(CURLY)
    found = list(MATCH_WORD.finditer(cased))
    words = [match.group().lower().strip("'") for match in found]
    for name in names:
        name = str(name).lower()
        possessive = name + "'s"
        for i, word in enumerate(words):
            if word == name and (
                words[max(0, i - 2):i] == ["according", "to"]
                or words[max(0, i - 1):i] == ["per"]
            ):
                return True
            if word not in (name, possessive):
                continue
            if word == possessive and words[i + 1:i + 2] and words[i + 1] in POSSESSIVE_PHRASES:
                return True
            window = _clause_window(cased, found, words, i)
            if any(token in ATTRIBUTION_VERBS for token in window):
                return True
            if any(
                token == "signed" and window[offset + 1:offset + 2] == ["off"]
                for offset, token in enumerate(window)
            ):
                return True
    return False


def _clause_window(cased: str, found, words: list[str], i: int) -> list[str]:
    """The words after the name at index `i` that can carry its verb: up to ATTRIBUTION_WINDOW of them,
    and none past a colon, a semicolon or a sentence end that follows the name, when a new subject starts
    there. After such a break the verb is someone else's ("Open question for Avery: Dana wrote the plan").
    The break only counts when the next word is capitalised and is neither an attribution verb nor the name
    itself, so a label like "Notes, Avery: approved" or "Avery: Approved." still carries Avery's verb."""
    window = words[i + 1:i + 1 + ATTRIBUTION_WINDOW]
    stop = CLAUSE_BREAK.search(cased, found[i].end())
    if stop is None:
        return window
    after = next((n for n, match in enumerate(found) if match.start() >= stop.end()), None)
    if after is None or not found[after].group()[:1].isupper() or words[after] in ATTRIBUTION_VERBS \
            or words[after] == words[i] or words[after] in PRONOUNS:
        return window
    return [word for word, match in zip(window, found[i + 1:]) if match.start() < stop.start()]


def cited_list(sources) -> list[CitedSource]:
    """The sources an op cites, as a list: one CitedSource (the gate's old call) or a sequence of them, the
    primary first. Duplicate ids are read once."""
    items = [sources] if isinstance(sources, CitedSource) else list(sources or ())
    seen: set[str] = set()
    out: list[CitedSource] = []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out


def snippet(text: str) -> str:
    return single_line(text, max_chars=SNIPPET)


FLAG_REASONS = ("quote_not_in_source", "unquoted_attribution")


def attribution_refusal(sentence: str, principal: Principal, ids=()) -> str:
    """The refusal for a sentence that puts words or a decision in the principal's mouth. It names the two
    routes that work: quote an existing source he wrote (an agent can't create one in his name), or reword."""
    return advice.attribution_error(snippet(sentence), principal.display_name, principal.person_id, ids)


@dataclass(frozen=True)
class Flag:
    sentence: str
    reasons: tuple[str, ...]
    messages: tuple[str, ...]


def flag_text(
    texts: list[str],
    known_text: str,
    source,
    principal: Principal | None,
) -> list[Flag]:
    """`source` is one CitedSource or every source the op cites. A quote passes when any of them holds it, and
    an attribution to the principal when a 5-word quote is held by one the principal wrote."""
    known = {normalise(sentence) for sentence in sentences(known_text)}
    cited = cited_list(source)
    ids = [item.id for item in cited]
    keys = [(key, item.author_person_id) for item in cited for key in source_keys(item)]
    flags: list[Flag] = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for sentence in sentences(text):
            if normalise(sentence) in known:
                continue
            found: list[str] = []
            reasons: list[str] = []
            failed = False
            supported = False
            for span in quoted_spans(sentence):
                key = match_key(span)
                words = len(key.split())
                if words < MIN_QUOTE_WORDS:
                    continue
                holders = [author for source_key, author in keys if contains(source_key, key)]
                if not holders:
                    failed = True
                    found.append(advice.quote_not_in_sources_error(snippet(span), ids))
                elif (
                    words >= ATTRIBUTION_MIN_QUOTE_WORDS
                    and principal is not None
                    and principal.person_id in holders
                ):
                    supported = True
            if failed:
                reasons.append("quote_not_in_source")
            if (
                principal is not None
                and attributes_to(sentence, principal.names, labels=(principal.display_name,))
                and not failed
                and not supported
            ):
                reasons.append("unquoted_attribution")
                found.append(attribution_refusal(sentence, principal, ids))
            if found:
                flags.append(Flag(sentence, tuple(reasons), tuple(found)))
    return flags


def check_text(
    texts: list[str],
    known_text: str,
    source,
    principal: Principal | None,
) -> list[str]:
    found = [message for flag in flag_text(texts, known_text, source, principal) for message in flag.messages]
    return list(dict.fromkeys(found))


def check_decision_quote(
    quote: str,
    decided_by: str | None,
    source,
    principal: Principal | None,
) -> list[str]:
    """The quote must be in a cited source; a decision by the principal needs it in a cited source the principal
    wrote. `source` is one CitedSource or every source the op cites."""
    cited = cited_list(source)
    quote_key = match_key(quote)
    holders = [item for item in cited if any(contains(key, quote_key) for key in source_keys(item))]
    findings: list[str] = []
    if not holders:
        findings.append(advice.decision_quote_missing_error([item.id for item in cited]))
    elif (
        principal is not None
        and decided_by == principal.person_id
        and not any(item.author_person_id == principal.person_id for item in holders)
    ):
        findings.append(
            advice.decision_author_error(
                principal.display_name, principal.person_id, [(item.id, item.author_person_id) for item in holders]
            )
        )
    return findings


def known_text(parts: list[str]) -> str:
    """The stored text a gated op starts from. It holds the fields read together, as the gate
    always did, and then each field on its own after a blank line, so a sentence stored in one
    field stays known even when reading the fields together joins it to the field before."""
    if not parts:
        return ""
    return "\n".join(parts) + "\n\n" + "\n\n".join(parts)


def flag_op(con, op_name: str, row: dict, current: dict | None, source,
            principal: Principal | None) -> list[Flag]:
    fields = GATE_FIELDS.get(op_name, ())
    texts = [row[field] for field in fields if field in row and isinstance(row[field], str)]
    known_parts = [str(current.get(field) or "") for field in fields] if current else []
    return flag_text(texts, known_text(known_parts), source, principal) if texts else []


def check_op(con, op_name: str, row: dict, current: dict | None, source,
             principal: Principal | None) -> list[str]:
    fields = GATE_FIELDS.get(op_name, ())
    texts = [row[field] for field in fields if field in row and isinstance(row[field], str)]
    stored = current
    if stored is None and con is not None:
        stored = current
    known_parts = []
    if stored:
        known_parts = [str(stored.get(field) or "") for field in fields]
    findings = check_text(texts, known_text(known_parts), source, principal) if texts else []
    if op_name == "upsert_decision" and ("quote" in row or "decided_by_person_id" in row):
        quote = row["quote"] if "quote" in row else (stored or {}).get("quote", "")
        decided_by = (
            row["decided_by_person_id"]
            if "decided_by_person_id" in row
            else (stored or {}).get("decided_by_person_id")
        )
        if isinstance(quote, str) and quote:
            findings.extend(check_decision_quote(quote, decided_by, source, principal))
    return list(dict.fromkeys(findings))
