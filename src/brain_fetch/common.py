"""What a service pull hands back to the shared run."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from brain.text import single_line

CAPTURE = "agent-brain.capture.v1"
CALENDAR = "agent-brain.calendar.v1"


@dataclass
class Ctx:
    instance: Path
    cfg: object
    collector: object
    secret: str
    cursor: dict
    fetched: str

    @property
    def options(self) -> dict:
        return self.collector.options

    def threads(self):
        """The metadata collector this pull also feeds, or None."""
        name = self.options.get("threads") or ""
        return self.cfg.collectors.get(name) if name else None


@dataclass
class Pulled:
    docs: list = field(default_factory=list)  # (collector name, export) in collect order
    cursor: dict = field(default_factory=dict)
    complete: bool = True
    items: int = 0
    note: str = ""


def service_of(collector) -> str:
    if collector.type == "calendar":
        return "calendar"
    return collector.options.get("service", "documents") if collector.type == "capture" else ""


def capture_doc(ctx: Ctx, items: list, *, complete: bool = True, incremental: bool = True, present=None) -> dict:
    doc = {"format": CAPTURE, "fetched_at": ctx.fetched, "complete": complete, "origin": ctx.options["origin"],
           "items": items}
    if incremental:
        doc["incremental"] = True
    if present is not None:
        doc["present"] = sorted(set(present))
    return doc


def line(text, limit: int) -> str:
    """A one-line export field: control characters out, whitespace collapsed, then cut.
    One odd character must not fail the export's schema on every run and hold the collector for good."""
    return single_line(text, max_chars=limit)
