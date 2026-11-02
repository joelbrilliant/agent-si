"""B4: every quote given as evidence is in the cited source word for word, by script.

A quote is a span in straight or curly double quotes, or a Markdown blockquote line, of at least five words. It
counts as evidence in an attribution question, which asks for one, or in a paragraph that cites a source id;
quoted titles, notes and the brain's own brief lines elsewhere aren't evidence. Evidence is checked against the
source cited on its own line (a `src-...` id, the source's title included), or else every source and markdown note in the dataset,
because a table row or a list item names its own source. A span the question itself holds isn't a quote. Quote marks, apostrophes and runs of white space are normalised, and an ending
full stop or comma is dropped; nothing else is, so a paraphrase in quote marks fails.

    python bench/quotes.py bench/results/<run>    # checks a finished run's transcripts, prints one JSON line
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "bench" / "exampleco"
MIN_WORDS = 5

_QUOTED = re.compile(r"[\"“]([^\"“”\n]{8,600})[\"”]")
_BLOCK = re.compile(r"^\s*>\s?(.+)$", re.M)
_SOURCE_ID = re.compile(r"\bsrc-[a-z0-9][a-z0-9.-]*[a-z0-9]\b")
_CODE = re.compile(r"`[^`\n]*`")


def _flat(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def sources(dataset: Path = DATASET) -> dict[str, str]:
    found = {}
    for path in sorted((dataset / "packets").glob("*.json")):
        for op in json.loads(path.read_text(encoding="utf-8")).get("ops") or []:
            row = op.get("row") or {}
            if op.get("op") == "add_source" and row.get("id") and isinstance(row.get("content"), str):
                found[row["id"]] = _flat(f"{row.get('title') or ''}\n{row['content']}")
    # The markdown arm's notes are the dataset's own text too: quoting them word for word is accurate. Whether
    # the quoted value is right is graded separately.
    for path in sorted((dataset / "markdown").rglob("*.md")):
        found[f"markdown:{path.relative_to(dataset / 'markdown')}"] = _flat(path.read_text(encoding="utf-8"))
    return found


def quotes(answer: str, attribution: bool = False, question: str = "") -> list[tuple[str, list[str]]]:
    """Each evidence quote of at least five words, with the source ids cited on its line."""
    found = []
    asked = _flat(question).lower()
    for paragraph in re.split(r"\n\s*\n", answer):
        if not _SOURCE_ID.search(paragraph) and not attribution:
            continue
        for line in paragraph.splitlines():
            cited = _SOURCE_ID.findall(line)
            plain = _CODE.sub(" ", line)
            spans = [m.group(1) for m in _QUOTED.finditer(plain)]
            block = _BLOCK.match(plain)
            if block and not spans:
                spans.append(block.group(1).strip().strip('"“”'))
            for span in spans:
                text = _flat(span).rstrip(".,;:").strip()
                if len(text.split()) >= MIN_WORDS and text.lower() not in asked:
                    found.append((text, cited))
    return found


def check(answer: str, corpus: dict[str, str], attribution: bool = False, question: str = "") -> dict:
    results = []
    for text, cited in quotes(answer, attribution, question):
        pool = [corpus[sid] for sid in cited if sid in corpus] or list(corpus.values())
        results.append({"quote": text[:200], "cited": cited, "verbatim": any(text in body for body in pool)})
    return {"quotes": len(results), "verbatim": sum(r["verbatim"] for r in results),
            "not_verbatim": [r for r in results if not r["verbatim"]]}


def _answer(transcript: Path) -> str:
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            return event.get("result") or ""
    return ""


def main(argv: list[str]) -> int:
    run = Path(argv[0])
    corpus = sources()
    asked = {q["id"]: q for q in json.loads((DATASET / "questions.json").read_text())["questions"]}
    by_arm: dict[str, dict] = {}
    for transcript in sorted((run / "transcripts").glob("*.jsonl")):
        question, arm = transcript.stem.split("-")[:2]
        q = asked.get(question) or {}
        found = check(_answer(transcript), corpus, q.get("group") == "attribution", q.get("question", ""))
        entry = by_arm.setdefault(arm, {"sessions": 0, "quotes": 0, "verbatim": 0, "not_verbatim": []})
        entry["sessions"] += 1
        entry["quotes"] += found["quotes"]
        entry["verbatim"] += found["verbatim"]
        entry["not_verbatim"] += [{"session": transcript.stem, **item} for item in found["not_verbatim"]]
    print(json.dumps(by_arm, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
