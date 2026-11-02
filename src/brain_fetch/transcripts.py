"""Meeting transcript readers: VTT, SRT, plain text and JSON become one line per cue,
`[hh:mm:ss] Name: text`. They stay off until redaction exists (readers.transcripts_allowed)."""
from __future__ import annotations

import json
import math
import re

from brain import ingress
from brain.text import single_line
from brain_fetch.readers import ReadError, clean

_VTT_TIME = re.compile(r"((?:[0-9]{1,}:)?[0-9]{1,2}:[0-9]{2})\.[0-9]{1,3}\s+-->")
_SRT_TIME = re.compile(r"([0-9]{1,}:[0-9]{2}:[0-9]{2}),[0-9]{1,3}\s+-->")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
_TAG = re.compile(r"<[^>]*>")
_PREFIX = re.compile(r"([A-Za-z][^:\n]{0,58}): (.+)")
_CLOCK = re.compile(r"([0-9]+:)?[0-9]{1,2}:[0-9]{2}")


def _seconds(clock: str) -> int:
    if len(clock) > 12:
        raise ReadError("unrecognised timestamp")
    parts = [int(part) for part in clock.split(":")]
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _stamp(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _line(seconds, speaker: str, text: str) -> str:
    body = f"{speaker}: {text}" if speaker else text
    return body if seconds is None else f"[{_stamp(seconds)}] {body}"


def _cue(seconds, lines: list[str]) -> str | None:
    text = " ".join(line.strip() for line in lines if line.strip())
    voice = _VOICE.search(text)
    speaker = voice.group(1).strip() if voice else ""
    text = _TAG.sub("", text).strip()
    if not speaker:
        prefixed = _PREFIX.fullmatch(text)
        if prefixed:
            speaker, text = prefixed.group(1).strip(), prefixed.group(2).strip()
    return _line(seconds, speaker, text) if text else None


def _blocks(text: str) -> list[list[str]]:
    blocks, current = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):  # not one-line: a transcript of many lines
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def read_vtt(text: str) -> str:
    blocks = _blocks(text.lstrip("﻿"))
    if not blocks or not blocks[0][0].startswith("WEBVTT"):
        raise ReadError("not a WebVTT file")
    out = []
    for block in blocks[1:]:
        if block[0].split(" ")[0] in ("NOTE", "STYLE", "REGION"):
            continue
        for index, line in enumerate(block):
            timing = _VTT_TIME.match(line)
            if timing:
                cue = _cue(_seconds(timing.group(1)), block[index + 1 :])
                if cue:
                    out.append(cue)
                break
    return "\n".join(out)


def read_srt(text: str) -> str:
    out = []
    for block in _blocks(text.lstrip("﻿")):
        for index, line in enumerate(block[:2]):
            timing = _SRT_TIME.match(line)
            if timing:
                cue = _cue(_seconds(timing.group(1)), block[index + 1 :])
                if cue:
                    out.append(cue)
                break
    return "\n".join(out)


def read_plain(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _start(entry: dict):
    for key in ("start", "start_time"):
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value < 10**9:
            return value
        if isinstance(value, str) and _CLOCK.fullmatch(value):
            return _seconds(value)
    return None


def read_json(text: str) -> str:
    try:
        data = ingress.loads(text, what="transcript")
    except ingress.IngressError:
        raise ReadError("unrecognised transcript JSON") from None
    if isinstance(data, dict):
        data = data.get("segments", data.get("transcript"))
    if not isinstance(data, list) or not data:
        raise ReadError("unrecognised transcript JSON")
    out = []
    for entry in data:
        body = entry.get("text", entry.get("content")) if isinstance(entry, dict) else None
        if not isinstance(body, str):
            raise ReadError("unrecognised transcript JSON")
        speaker = entry.get("speaker", entry.get("name", ""))
        speaker = speaker if isinstance(speaker, str) else ""
        if body.strip():
            out.append(_line(_start(entry), speaker.strip(), single_line(body)))
    return "\n".join(out)


def read_transcript(data: bytes, tag: str) -> str:
    """tag is vtt, srt, ttext or tjson."""
    from brain_fetch.readers import read_text

    text = read_text(data)
    reader = {"vtt": read_vtt, "srt": read_srt, "ttext": read_plain, "tjson": read_json}[tag]
    return clean(reader(text))
