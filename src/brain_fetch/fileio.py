"""Read one file once, from one descriptor."""
from __future__ import annotations

import hashlib
import os

from brain_fetch import readers, transcripts

MAX_BYTES = 64 * 1024 * 1024
# The readers turn what they know into ReadError. Anything else a damaged file can raise is still a
# content failure of that one file, never a crash of the run.
BACKSTOP = (ValueError, LookupError, ArithmeticError, UnicodeError, RecursionError, MemoryError)


def read_file(path: str, tag: str):
    """(text or ReadError, tag, sha256, first stat) for a file; raises OSError for an I/O failure, which
    includes a file that changes while it is read (fstat before and after, from the one descriptor) and a
    read that stalls past the time limit."""
    try:
        with readers.time_limit():
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                before = os.fstat(fd)
                limit = readers.TEXT_LIMIT + 1 if tag not in ("docx", "pdf") else MAX_BYTES + 1
                chunks, total = [], 0
                while total < limit:
                    chunk = os.read(fd, min(1 << 20, limit - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
    except readers.ReadError:
        raise OSError("took too long reading") from None
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise OSError("file changed while it was read")
    data = b"".join(chunks)
    sha = hashlib.sha256(data).hexdigest()
    try:
        with readers.time_limit():
            if tag == "text":
                text = readers.read_text(data, total < limit)
            elif tag == "docx":
                text = readers.read_docx(data)
            elif tag == "pdf":
                text, tag = readers.read_pdf(data)
            else:
                text = transcripts.read_transcript(data, tag)
    except readers.ReadError as exc:
        return exc, tag, sha, before
    except BACKSTOP:
        return readers.ReadError("unreadable"), tag, sha, before
    return text, tag, sha, before
