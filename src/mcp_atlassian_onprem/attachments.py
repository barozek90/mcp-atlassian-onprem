"""Turn attachment bytes into MCP content."""

from __future__ import annotations

import io

from mcp.server.fastmcp import Image
from pypdf import PdfReader

from .http import AtlassianError

MAX_TEXT_CHARS = 100_000

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _image_format(data: bytes) -> str | None:
    for signature, name in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _pdf_text(data: bytes) -> str:
    try:
        parts: list[str] = []
        length = 0
        for page in PdfReader(io.BytesIO(data)).pages:
            parts.append(page.extract_text() or "")
            length += len(parts[-1])
            if length > MAX_TEXT_CHARS:
                break
        return "\n".join(parts)
    except Exception as exc:  # pypdf raises many unrelated types on broken files
        raise AtlassianError(f"Could not read the PDF: {type(exc).__name__}") from None


def _decode(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data:
        return None
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def render_attachment(filename: str, data: bytes, offset: int = 0) -> list:
    header = f"Attachment {filename!r}, {len(data)} bytes."
    image = _image_format(data)
    if image:
        return [header, Image(data=data, format=image)]

    if data.startswith(b"%PDF-"):
        text = _pdf_text(data)
    else:
        text = _decode(data)
    if text is None:
        return [header + " Binary format: only text, PDF and image files can be read."]

    offset = max(0, offset)
    end = offset + MAX_TEXT_CHARS
    if end < len(text):
        header += f" Truncated, continue with offset={end}."
    return [header, text[offset:end]]
