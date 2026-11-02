"""Hand-built files for the brain-fetch tests: Word, PDF and transcript fixtures. Invented text only."""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def para(text: str = "", style: str | None = None, *, runs: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    body = runs if runs is not None else f"<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r>"
    return f"<w:p>{props}{body}</w:p>"


def table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join(
        "<w:tr>" + "".join(f"<w:tc>{para(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    ) + "</w:tbl>"


def docx_xml(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'


def make_docx(body: str = "", *, extra: dict[str, str] | None = None, raw_xml: bytes | None = None, flag: int = 0,
              stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        info = zipfile.ZipInfo("word/document.xml")
        info.compress_type = method
        archive.writestr(info, raw_xml if raw_xml is not None else docx_xml(body))
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    data = bytearray(buffer.getvalue())
    if flag:  # zipfile resets the flag bits it writes, so set bit 0 (encrypted) in the headers by hand
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = data.find(signature)
            while start != -1:
                data[start + offset] |= flag
                start = data.find(signature, start + 4)
    return bytes(data)


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small valid PDF: one content stream per page, Helvetica, one line of text per string."""
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * n} 0 R" for n in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number, lines in enumerate(pages):
        content_id = 5 + 2 * number
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        drawing = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET" if lines else ""
        stream = drawing.encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)


def encrypted_pdf() -> bytes:
    import warnings

    import pypdf

    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf([["Secret Example text"]]))))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        writer.encrypt("example-password", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


VTT = """WEBVTT

NOTE This is a note
that spans lines

STYLE
::cue { color: red }

intro
00:00:01.500 --> 00:00:04.000
<v Alex Example>Welcome everyone to the review.</v>

00:00:05.250 --> 00:00:09.000
No speaker on this cue.

1:02:03.999 --> 1:02:05.000
Kit Example: Speaker from a prefix.
"""

SRT = """1
00:00:02,000 --> 00:00:03,500
Sam Example: First line
continues here.

2
01:15:00,900 --> 01:15:02,000
<i>No speaker</i>
"""
