"""A7 and A8 at scale 4: the core claim's cheapest proof. Markdown grows with
the company; the brain's startup bytes and shortest-path answers should stay flat (A8 fails above 25% growth).

Generates the Example Co dataset at scale 1 and scale 4 with the committed seed and date, builds both instances,
and prints the figures side by side as JSON. Bytes only; tokens would be bytes / 4, an estimate.

    python tools/scale_check.py [--no-timing]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import harness  # noqa: E402


def _markdown_bytes(dataset: Path) -> int:
    return sum(path.stat().st_size for path in (dataset / "markdown").rglob("*") if path.is_file())


def _measure(dataset: Path, parent: Path, timing: bool) -> dict:
    manifest = harness.load_manifest(dataset)
    instance = harness.build_instance(dataset, parent)
    questions = json.loads((dataset / "questions.json").read_text(encoding="utf-8"))
    questions = questions["questions"] if isinstance(questions, dict) else questions
    figures = harness.figures(instance, questions, manifest["now"], timing=timing)
    return {"markdown_bytes": _markdown_bytes(dataset), "S": figures["a1"]["exampleco"]["S"],
            "brief": figures["a1"]["exampleco"]["brief"], "a5": figures["a5"], "a7": figures.get("a7")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-timing", action="store_true")
    ns = parser.parse_args(argv)
    manifest = harness.load_manifest()
    results = {}
    with tempfile.TemporaryDirectory() as work:
        for scale in (1, 4):
            out = Path(work) / f"scale{scale}"
            subprocess.run([sys.executable, str(ROOT / "bench" / "generate.py"), "--today", manifest["today"],
                            "--seed", str(manifest["seed"]), "--scale", str(scale), "--out", str(out)], check=True,
                           cwd=ROOT, capture_output=True)
            dataset = out if (out / "MANIFEST.json").is_file() else out / "exampleco"
            built = Path(work) / f"inst{scale}"
            built.mkdir()
            results[scale] = _measure(dataset, built, not ns.no_timing)
    one, four = results[1], results[4]
    growth = {"S": round(four["S"] / one["S"] - 1, 4),
              "worst_answer": max(round(four["a5"][q]["bytes"] / one["a5"][q]["bytes"] - 1, 4) for q in one["a5"] if q in four["a5"]),
              "markdown": round(four["markdown_bytes"] / one["markdown_bytes"], 2)}
    report = {"scale_1": {k: v for k, v in one.items() if k != "a5"}, "scale_4": {k: v for k, v in four.items() if k != "a5"},
              "growth": growth, "a8_pass": growth["S"] <= 0.25 and growth["worst_answer"] <= 0.25}
    print(json.dumps(report, indent=2))
    return 0 if report["a8_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
