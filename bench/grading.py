"""The reference answer-grading rules of the Example Co benchmark.

Standard library only, no model. The Tier B grader reuses these functions. `bench/generate.py` keeps its own
copy of `norm` (it must run as a bare script); tests/test_bench_gate.py proves the two agree.

A question holds `answer_must` (a list; each item is a string or a list of alternatives) and
`answer_must_not` (a list of wrong strings). `grade` reports a strict score and a lenient one:

- strict: every `answer_must` item is met, and no wrong string is mentioned anywhere in the answer;
- lenient: every item is met, and no wrong string is asserted (a sentence that mentions it with a
  staleness marker, such as "a stale note says ...", doesn't count, unless the same sentence negates a
  right answer). The lenient score is an upper bound: it is never quoted alone;
- `flagged_trap`: a wrong string is mentioned at all.

An abstention (`kind` "abstain") also fails when it holds a digit run the question text lacks, unless that
number is plainly about something else: its sentence names a person or product the question doesn't, or it
counts the records searched ("80 fact keys").

Rules applied the same way to both arms:
- a count asked as a digit (2 to 20) is also met by its word ("10" by "ten");
- an abstention is also met by a plain "doesn't have", "don't record", "not yet recorded" and the like;
- an abstention doesn't fail on a count of what was searched ("all 80 fact keys") or a figure for another named
  thing ("Example Cloud at 159 AUD"); a bare "it is $5m" still fails.
"""
from __future__ import annotations

import re

_POSSESSIVE = re.compile(r"['\u2019]s\b")
_DECIMAL = re.compile(r"(?<=[0-9])\.(?=[0-9])")
_THOUSANDS = re.compile(r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}[Zz])?")
_ID = re.compile(r"\b[A-Za-z]+-[0-9]+\b")
_NUMERIC = re.compile(r"\$?[0-9][0-9,]*(?:\.[0-9]+)?")
# Staleness words only. "not", "was" and "were" are left out: they negate the right value as often as the trap.
_STALE = re.compile(
    r"\b(stale|outdated|old|older|wrong|wrongly|incorrect|incorrectly|earlier|previous|previously|former|"
    r"formerly|superseded|unsourced|unverified|reminder|ignore|ignored|disregard|disregarded|first edition|"
    r"up from|raised from|increased from|changed from|down from|grown from)\b",
    re.I,
)
_BEFORE = r"(?:not|no|never|isn t|aren t|wasn t|weren t)"
_AFTER = r"(?:(?:is|are|was|were) )?(?:wrong|incorrect|outdated|stale|false|not)"
_DIGITS = re.compile(r"[0-9]+")
_WORDS = {str(n): w for n, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                          "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
# A plain statement that the answer isn't held, on norm()'d text (an apostrophe becomes a space: "doesn t").
_ABSTAIN = re.compile(
    r" (?:(?:do|does|did|is|are|was|were|could|would|has|have) ?n t|can t|won t|do not|does not|did not|cannot|can not|"
    r"not yet|never|no longer) "
    r"(?:have|hold|holds|say|says|include|includes|record|recorded|mention|mentions|list|lists|contain|contains|show|"
    r"shows|give|cover|covers|know|find|found|state|states|specify|specifies|exist)\b"
    r"| (?:no|nothing|none) (?:record|mention|value|entry|data|information|figure|date|answer)s? ")
# Example Co's people ("Ari Example") and products ("Example Cloud").
_ENTITY = re.compile(r"\b(?:Example [A-Z][a-z]+|[A-Z][a-z]+ Example)\b")
_SEARCHED = re.compile(r"\b[0-9]+ (?:fact keys|facts?|keys|records?|sources?|results?|items?|actions?|products?|matches|"
                       r"hits|entries|threads|documents|messages|sessions|people)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def norm(text: str) -> str:
    """Lower-case; thousands commas dropped (32,000 is 32000); a decimal point kept inside its number
    (49.5 is one token, not 49 and 5); a possessive 's dropped (Example Basic's SSO is Example Basic SSO); every other run of non-alphanumerics is one space; padded."""
    text = _THOUSANDS.sub("", _POSSESSIVE.sub("", text.lower()))
    text = _DECIMAL.sub("\x00", text)
    return " " + re.sub(r"[^a-z0-9\x00]+", " ", text).strip() + " "


def strip_noise(text: str) -> str:
    """Drop ISO dates and timestamps, and ids such as act-013, so a bare number can't match inside them."""
    return _ID.sub(" ", _ISO.sub(" ", text))


def is_numeric(alternative: str) -> bool:
    """A digits-only or bare-number alternative (14, 5, 49.5, 32,000)."""
    return _NUMERIC.fullmatch(alternative.strip()) is not None


def mentions(alternative: str, answer: str) -> bool:
    key = norm(alternative)
    if key.strip() == "":
        return False
    haystack = strip_noise(answer) if is_numeric(alternative) else answer
    return key in norm(haystack)


def met(item, answer: str) -> bool:
    alternatives = [item] if isinstance(item, str) else item
    if any(mentions(alternative, answer) for alternative in alternatives):
        return True
    # A small count written as a word: "Ten facts are in dispute" meets "10".
    words = [_WORDS[a.strip()] for a in alternatives if a.strip() in _WORDS and int(a.strip()) >= 2]
    return any(f" {word} " in norm(answer) for word in words)


def is_abstention_item(item) -> bool:
    """The answer_must item of an abstention question: the "doesn't hold" family of phrases."""
    alternatives = [item] if isinstance(item, str) else item
    return any(norm(a).strip() in ("does not hold", "doesn t hold", "not recorded") for a in alternatives)


def abstains(answer: str) -> bool:
    return _ABSTAIN.search(norm(answer)) is not None


def negates_right(sentence: str, must_items) -> bool:
    """The sentence puts a right answer behind a negator ("not 29 AUD") or calls it wrong ("29 AUD is wrong")."""
    for item in must_items:
        for alternative in [item] if isinstance(item, str) else item:
            key = norm(alternative).strip()
            if not key:
                continue
            text = norm(strip_noise(sentence) if is_numeric(alternative) else sentence)
            if re.search(rf" {_BEFORE} {re.escape(key)} ", text) or re.search(rf" {re.escape(key)} {_AFTER} ", text):
                return True
    return False


def asserted(wrong: str, answer: str, must_items=()) -> bool:
    """Some sentence mentions the wrong string and either carries no staleness marker or negates a right answer."""
    return any(
        mentions(wrong, sentence) and (_STALE.search(sentence) is None or negates_right(sentence, must_items))
        for sentence in _SENTENCE.split(answer)
        if sentence.strip()
    )


def invented_numbers(question_text: str, answer: str) -> list[str]:
    """For an abstention: the digit runs the question text doesn't hold, except in a sentence that names a person
    or product the question doesn't, and except a count of the records searched."""
    known = set(_DIGITS.findall(question_text))
    asked = set(_ENTITY.findall(question_text))
    found = set()
    for sentence in _SENTENCE.split(answer):
        if set(_ENTITY.findall(sentence)) - asked:
            continue
        found |= {run for run in _DIGITS.findall(_SEARCHED.sub(" ", sentence)) if run not in known}
    return sorted(found)


def grade(question: dict, answer: str) -> dict:
    missing = [item for item in question["answer_must"] if not met(item, answer)
               and not (question["kind"] == "abstain" and is_abstention_item(item) and abstains(answer))]
    invented = invented_numbers(question["question"], answer) if question["kind"] == "abstain" else []
    mentioned = [wrong for wrong in question["answer_must_not"] if mentions(wrong, answer)]
    asserted_wrong = [wrong for wrong in mentioned if asserted(wrong, answer, question["answer_must"])]
    complete = not missing and not invented
    return {
        "strict": complete and not mentioned,
        "lenient": complete and not asserted_wrong,
        "flagged_trap": bool(mentioned),
        "missing": missing,
        "invented": invented,
    }
