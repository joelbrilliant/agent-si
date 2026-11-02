"""The address and phone scrub, quoted history and signatures, and the measured miss rates. Every address and number here is invented."""
from __future__ import annotations

import base64

from brain.collectors import capture, privacy
from brain_fetch import mailtext

# 40 addresses in unusual formats. The ones the patterns are known to miss are listed in KNOWN_ADDRESS_MISSES,
# so a change that catches more, or fewer, shows up here.
ADDRESSES = [
    "Alex.Example+news@sub.example.co.uk", "alex_example@example-mail.com.au", "ALEX@EXAMPLE.COM",
    "alex@example.museum", "o'neil@example.ie", "alex @ example . com", "alex(at)example.com",
    "alex (at) example (dot) com", "alex [at] example [dot] com", "alex{at}example{dot}com", "alex at example dot com",
    "alex AT example DOT com", "alex@example[.]com", "<alex@example.com>", "mailto:alex@example.com",
    "alex@xn--exmple-cua.com", "álex@exámple.com", "alex@example.com.", "(alex@example.com)", "alex@localhost",
    "alex@[192.0.2.1]", "alex%example@example.com", "alex-example@example.travel", "a@b.io",
    "alex.example@example.com,sam@example.com", "alex at example.com", "alex<at>example<dot>com", "alex@ example.com",
    "alex @example.com", "alex＠example.com", "alex@example .com", "first.last@department.example.edu.au",
    "alex+tag+more@example.org", "alex@example.com.au>", "x@x.yz", "alex dot example at example dot com",
    "Alex.Example@Example.COM", "alex@example.co", "sam.example@mail.example.net", "\"alex\"@example.com",
]
KNOWN_ADDRESS_MISSES = {"alex@localhost", "alex@[192.0.2.1]", "alex at example.com", "alex＠example.com"}

PHONES = [
    "+61 2 9876 5432", "(02) 9876 5432", "02 9876 5432", "0298765432", "+61298765432", "0412 345 678", "0412-345-678",
    "0412.345.678", "+61 412 345 678", "+61-412-345-678", "+61 (0)412 345 678", "1300 123 456", "1800-123-456",
    "13 12 34", "(415) 555-0123", "415-555-0123", "415.555.0123", "+1 415 555 0123", "+1 (415) 555-0123",
    "1-415-555-0123", "4155550123", "+44 20 7946 0958", "+44 (0)20 7946 0958", "020 7946 0958", "07700 900123",
    "+33 1 23 45 67 89", "01 23 45 67 89", "+49 30 123456", "+49 (30) 1234 5678", "030 1234 5678", "+81 3-1234-5678",
    "03-1234-5678", "+65 6123 4567", "6123 4567", "+64 9 123 4567", "09 123 4567", "+1.415.555.0123",
    "+61 2 9876 5432 ext 12", "tel:+61298765432", "+91 98765 43210",
]
KNOWN_PHONE_MISSES = {"13 12 34", "4155550123", "tel:+61298765432"}

LOOK_ALIKES = [
    "2026-10-12", "12/10/2026", "12.10.2026", "2024-2026", "10:30-11:45", "v1.2.3", "0.8.0a2", "3.14159265",
    "192.168.100.200", "10.0.0.1", "$12 345 678", "AUD 1,234,567", "128,000 tokens", "128000 tokens", "PO 4500012345",
    "invoice INV-20261012-001", "sha 41e13af72e20", "run 38159138310", "ticket #12345678", "ISBN 978-0-306-40615-7",
    "1,000,000", "50%", "99.95%", "ts 1728600000.000100", "page 12 of 200", "room 4.12", "pi 3.14",
    "id 20261012T184005Z", "ABN 12 345 678 901", "2026 10 12", "Q4 2026", "06:00 to 07:30", "-12.5", "0.000123",
    "30-60-90 plan", "Sydney 2000 NSW", "20,000-30,000", "2026-10-12 to 2026-10-16", "8x H100 80GB", "@here and @alex",
]
KNOWN_FALSE_POSITIVES = {"ISBN 978-0-306-40615-7", "ABN 12 345 678 901"}


def test_addresses_are_scrubbed_with_a_measured_miss_rate() -> None:
    missed = {text for text in ADDRESSES if "@" in privacy.scrub(text).replace("[email]", "")
              or privacy.EMAIL_TOKEN not in privacy.scrub(text)}
    assert len(ADDRESSES) == 40 and missed == KNOWN_ADDRESS_MISSES  # Measured: 4 of 40 missed (10%)


def test_phones_are_scrubbed_with_a_measured_miss_rate() -> None:
    missed = {text for text in PHONES if privacy.PHONE_TOKEN not in privacy.scrub(text)}
    assert len(PHONES) == 40 and missed == KNOWN_PHONE_MISSES  # Measured: 3 of 40 missed (7.5%)


def test_look_alikes_are_left_alone() -> None:
    changed = {text for text in LOOK_ALIKES if privacy.scrub(text) != text}
    assert len(LOOK_ALIKES) == 40 and changed == KNOWN_FALSE_POSITIVES  # Measured: 2 of 40 changed (5%)


def test_scrub_is_idempotent_and_keeps_the_sentence() -> None:
    text = "Call Alex on +61 2 9876 5432 or mail alex@example.com before 2026-10-12, quote PO 4500012345."
    once = privacy.scrub(text)
    assert once == "Call Alex on [phone] or mail [email] before 2026-10-12, quote PO 4500012345."
    assert privacy.scrub(once) == once
    for sample in ADDRESSES + PHONES + LOOK_ALIKES:
        assert privacy.scrub(privacy.scrub(sample)) == privacy.scrub(sample)


def test_capture_scrubs_conversations_only() -> None:
    item = {"ref": "email:work-mail/m1", "kind": "email", "title": "Re: call 0412 345 678",
            "from": "alex@example.com", "to": ["Sam Example <sam@example.com>"], "attachments": ["notes alex@example.com.pdf (3 KB)"],
            "text": "Ring me on 0412 345 678."}
    scrubbed = capture.scrub_fields(item)
    assert scrubbed["title"] == "Re: call [phone]" and scrubbed["from"] == "[email]"
    assert scrubbed["to"] == ["Sam Example <[email]>"] and scrubbed["attachments"] == ["notes [email] (3 KB)"]
    assert capture.scrub_text(item["text"], "email") == "Ring me on [phone]."
    assert capture.scrub_text(item["text"], "slack") == "Ring me on [phone]."
    # Documents and transcripts keep their text, so the hashes stored before 2c stay valid (CONTENT_FORMAT 1).
    assert capture.scrub_text(item["text"], "document") == item["text"]
    assert capture.scrub_text(item["text"], "meeting_transcript") == item["text"]
    document = {**item, "kind": "document", "ref": "document:work-docs/a.md"}
    assert capture.scrub_fields(document) is document
    assert capture.CONTENT_FORMAT == 1


def test_header_lists_attachments() -> None:
    item = {"ref": "email:work-mail/m1", "kind": "email", "title": "Plan", "date": "2026-10-09", "from": "Alex Example",
            "attachments": ["plan.pdf (12 KB)", "chart.png (3 KB)"]}
    assert capture.header(item) == ("kind: email\nfrom: Alex Example\ndate: 2026-10-09\nsubject: Plan\n"
                                    "attachments: plan.pdf (12 KB), chart.png (3 KB)\nref: email:work-mail/m1")


def _part(kind: str, text: str, **extra) -> dict:
    data = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {"mimeType": kind, "body": {"data": data, "size": len(text)}, **extra}


def test_quoted_history_and_signatures_are_dropped() -> None:
    cases = {
        "Yes, Thursday works.\n\nOn Fri, 9 Oct 2026 at 09:00, Sam Example <sam@example.com> wrote:\n> Does Thursday work?": "Yes, Thursday works.",
        "Yes.\n\nOn Fri, 9 Oct 2026 at 09:00, Sam Example <\nsam@example.com> wrote:\n> Old": "Yes.",
        "Fine.\n-----Original Message-----\nFrom: Sam": "Fine.",
        "Fine.\n\nFrom: Sam Example\nSent: Friday, 9 October 2026 9:00 AM\nTo: Alex\nSubject: Plan\n\nOld text": "Fine.",
        "Agreed.\n> quoted line\nMore of mine.": "Agreed.\nMore of mine.",
        "See you then.\n-- \nAlex Example\nHead of Example": "See you then.",
        "Sure.\n\nSent from my iPhone": "Sure.",
        "On Monday we start the pilot.\nIt runs for a week.": "On Monday we start the pilot.\nIt runs for a week.",
        "From: the start, this was the plan.": "From: the start, this was the plan.",
    }
    for raw, expected in cases.items():
        assert mailtext.strip_quoted(raw) == expected, raw


def test_html_bodies_drop_quotes_and_signatures() -> None:
    markup = ("<div>Thanks <b>Sam</b>,<br>the pilot starts Monday.</div>"
              "<div class=\"gmail_signature\">Alex Example<br>Example Co</div>"
              "<div class=\"gmail_quote\"><div>On Fri Sam wrote:</div><blockquote><p>Old text</blockquote></div>"
              "<p>After the quote.</p><script>bad()</script>")
    text, attachments = mailtext.body({"mimeType": "multipart/alternative", "parts": [_part("text/html", markup)]})
    assert text == "Thanks Sam,\nthe pilot starts Monday.\n\nAfter the quote." and attachments == []


def test_plain_text_wins_and_attachments_are_names_only() -> None:
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [_part("text/plain", "Plain body."), _part("text/html", "<p>HTML</p>")]},
        {"mimeType": "application/pdf", "filename": "Plan  v2.pdf", "body": {"attachmentId": "a1", "size": 12_400}},
        {"mimeType": "image/png", "filename": "chart.png", "body": {"attachmentId": "a2", "size": 2_200_000}},
    ]}
    text, attachments = mailtext.body(payload)
    assert text == "Plain body." and attachments == ["Plan v2.pdf (12 KB)", "chart.png (2.1 MB)"]


def test_display_names_never_keep_an_address() -> None:
    assert mailtext.names('"Alex Example" <alex@example.com>, sam@example.com, Kim <kim@example.com>') == [
        "Alex Example", "(no name)", "Kim"]
    assert mailtext.names("") == []


def test_scrub_is_linear_on_hostile_text() -> None:
    """A long unbroken word once cost quadratic time (5,000 characters took 0.65 s): each pattern may now start
    only where a local part or a number starts."""
    import time

    for text in ("a" * 400_000, "a." * 200_000, "1 " * 200_000, "x at " * 80_000, "a@" + "b" * 400_000,
                 "abc.def+" * 50_000, "(1) " * 100_000):
        started = time.perf_counter()
        privacy.scrub(text)
        assert time.perf_counter() - started < 2.0, text[:20]
