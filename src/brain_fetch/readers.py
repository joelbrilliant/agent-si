"""File readers: text, Word and PDF here, transcripts in transcripts.py. Every
reader takes bytes and returns text; ReadError means the file itself can't be read, which lasts until it
changes. Word and PDF contents are hostile input."""
from __future__ import annotations

import codecs
import contextlib
import io
import logging
import re
import signal
import struct
import warnings
import zipfile
import zlib
import xml.etree.ElementTree as ET

from brain.text import single_line
from brain_fetch.state import transcripts_allowed  # noqa: F401  (the gate lives with the state helpers)

PYPDF_PIN = "pypdf==6.19.0"  # mirrors pyproject.toml's pdf and test extras (test 70b)
TIME_LIMIT = 60  # seconds of wall time for one Word, PDF or transcript read
TEXT_LIMIT = 4 * 1024 * 1024
WORD_LIMIT = 30 * 1024 * 1024
PDF_PAGES = 2000
TEXT_CAP = 4_000_000
NO_PDF = "pdf text needs the pdf extra"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"[Hh]eading ?([1-9])")
_CORRUPT = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError, ValueError,
            LookupError, ArithmeticError, struct.error, RecursionError)


class ReadError(Exception):
    pass


def clean(text: str) -> str:
    """A reader's output: no lone surrogates, at most 4,000,000 characters."""
    return text.encode("utf-8", "replace").decode("utf-8")[:TEXT_CAP]


@contextlib.contextmanager
def time_limit():
    """signal.setitimer on POSIX: a read that takes TIME_LIMIT seconds is unreadable ('took too long'), so one
    pathological file can't hold the run lock."""
    if not hasattr(signal, "setitimer"):
        yield
        return

    def expired(_signum, _frame):
        raise ReadError("took too long")

    try:
        previous = signal.signal(signal.SIGALRM, expired)
    except ValueError:  # not the main thread
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, TIME_LIMIT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def read_text(data: bytes, complete: bool = True) -> str:
    """UTF-8 (with or without a BOM) by an incremental decoder, so a character split by the 4 MiB cut is
    dropped rather than failing the whole file; cp1252 only if a complete invalid sequence is found."""
    if b"\x00" in data:
        raise ReadError("binary file")
    try:
        return clean(codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=complete))
    except UnicodeDecodeError:
        pass
    try:
        return clean(data.decode("cp1252", errors="strict"))
    except UnicodeDecodeError:
        raise ReadError("not text") from None


# -- Word ------------------------------------------------------------------------------------------


def _runs(node, parts: list[str]) -> None:
    for child in node:
        tag = child.tag
        if tag == _W + "t":
            parts.append(child.text or "")
        elif tag == _W + "tab":
            parts.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            parts.append("\n")
        elif tag in (_W + "del", _W + "txbxContent", _W + "pPr", _W + "rPr", _W + "instrText"):
            continue
        else:
            _runs(child, parts)


def _paragraph(node) -> str:
    parts: list[str] = []
    _runs(node, parts)
    text = "".join(parts).strip()
    style = node.find(f"{_W}pPr/{_W}pStyle")
    match = _HEADING.fullmatch(style.get(_W + "val", "")) if style is not None else None
    return f"{'#' * int(match.group(1))} {text}" if match and text else text


def _table(node) -> str:
    rows = []
    for row in node.iter(_W + "tr"):
        cells = []
        for cell in row.findall(_W + "tc"):
            cells.append(single_line(" ".join(_paragraph(p) for p in cell.iter(_W + "p"))))
        rows.append("\t".join(cells))
    return "\n".join(rows)


def _blocks(parent, out: list[str]) -> None:
    for child in parent:
        if child.tag == _W + "p":
            text = _paragraph(child)
        elif child.tag == _W + "tbl":
            text = _table(child)
        elif child.tag == _W + "sdt":
            content = child.find(_W + "sdtContent")
            if content is not None:
                _blocks(content, out)
            continue
        else:
            continue
        if text.strip():
            out.append(text)


_NOT_UTF8 = re.compile(rb"<\?xml[^>]*encoding\s*=\s*[\"'](?!utf-8[\"'])", re.I)


def read_docx(data: bytes) -> str:
    """word/document.xml only. It never reaches the parser unless it is plain UTF-8 without a DOCTYPE or
    ENTITY anywhere, so a billion-laughs file (in any encoding) is refused before expat sees it."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.flag_bits & 1:
                raise ReadError("word file is encrypted")
            if info.file_size > WORD_LIMIT:
                raise ReadError("word file is too large")
            with archive.open(info) as handle:
                raw = handle.read(WORD_LIMIT + 1)
    except KeyError:
        raise ReadError("not a word file") from None
    except _CORRUPT:
        raise ReadError("word file is corrupt") from None
    if len(raw) > WORD_LIMIT:
        raise ReadError("word file is too large")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError("word file is not UTF-8") from None
    if b"\x00" in raw or _NOT_UTF8.search(raw[:300]):
        raise ReadError("word file is not UTF-8")
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ReadError("word file holds a DOCTYPE")
    try:
        root = ET.fromstring(raw)
    except _CORRUPT + (ET.ParseError,):
        raise ReadError("word file is corrupt") from None
    body = root.find(_W + "body")
    blocks: list[str] = []
    try:
        _blocks(root if body is None else body, blocks)
    except _CORRUPT:
        raise ReadError("word file is corrupt") from None
    return clean("\n\n".join(blocks))


# -- PDF -------------------------------------------------------------------------------------------


def read_pdf(data: bytes) -> tuple[str, str]:
    """(text, reader tag). Without pypdf the text is a stub and the tag is pdf-none."""
    try:
        import pypdf
    except ImportError:
        return NO_PDF, "pdf-none"
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise ReadError("pdf is encrypted")
            pages = []
            for number, page in enumerate(reader.pages, start=1):
                if number > PDF_PAGES:
                    break
                pages.append(page.extract_text() or "")
    except ReadError:
        raise
    except Exception:  # noqa: BLE001 - pypdf raises many types for damaged files
        raise ReadError("pdf is unreadable") from None
    if not any(page.strip() for page in pages):
        return f"(no text layer: {len(pages)} pages)", "pdf"
    return clean("\n".join(f"--- page {n} ---\n{text}" for n, text in enumerate(pages, start=1))), "pdf"


def pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True
