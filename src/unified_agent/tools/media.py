"""Multi-modal input: read local images and surface remote image/PDF payloads.

Local-only by design (the operator's 2026-07-18 decision): no cloud OCR, no new
runtime dependencies. Detection and dimension parsing use only the stdlib, so
the tools degrade with an explicit "could not read" note rather than guessing.
"""

import base64
import struct

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..safety import resolve_in_workspace

# magic-byte prefixes → mime. Enough to name the common raster formats an
# agent actually receives (screenshots, photos, forwarded images).
_IMAGE_MIME = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
}


def _sniff_image_mime(data: bytes) -> str | None:
    """Return the image mime from magic bytes, or None if not a known raster.

    WebP needs a two-part check (RIFF container + 'WEBP' at offset 8) that a
    simple prefix table can't express, so it is handled explicitly.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for prefix, mime in _IMAGE_MIME.items():
        if data.startswith(prefix):
            return mime
    return None


def _image_dimensions(mime: str, data: bytes) -> tuple[int | None, int | None]:
    """Best-effort (width, height) from header bytes; (None, None) if unknown.

    Pure struct parsing — no image library. Unparseable or truncated headers
    return None rather than raising: dimensions are a nicety, not a contract.
    """
    try:
        if mime == "image/png" and len(data) >= 24:
            # IHDR width/height are big-endian uint32 at offset 16.
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if mime == "image/gif" and len(data) >= 10:
            # Logical screen width/height are little-endian uint16 at offset 6.
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if mime == "image/bmp" and len(data) >= 26:
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
        if mime == "image/jpeg":
            return _jpeg_dimensions(data)
        if mime == "image/webp":
            return _webp_dimensions(data)
    except (struct.error, IndexError):
        return None, None
    return None, None


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Walk JPEG segment markers to the SOF frame header for height/width."""
    i = 2  # skip SOI (0xFFD8)
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15 carry frame size.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None, None


def _webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """VP8/VP8L/VP8X dimensions from a WebP RIFF container header."""
    if len(data) < 30:
        return None, None
    fmt = data[12:16]
    if fmt == b"VP8 ":
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return w, h
    if fmt == b"VP8L":
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
        return w, h
    if fmt == b"VP8X" and len(data) >= 30:
        w = (data[24] | data[25] << 8 | data[26] << 16) + 1
        h = (data[27] | data[28] << 8 | data[29] << 16) + 1
        return w, h
    return None, None


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def describe_image(mime: str, width: int | None, height: int | None, nbytes: int) -> str:
    """Deterministic one-line description — format, dimensions, size. No OCR."""
    fmt = mime.split("/", 1)[-1].upper()
    dims = f"{width}x{height}" if width and height else "unknown size"
    return f"{fmt} image, {dims}, {_human_bytes(nbytes)}"


def read_image(path: str) -> dict:
    """Read a workspace image and return its base64, mime, and metadata.

    Local-only: the `description` is deterministic metadata (format, pixel
    dimensions, byte size) — there is no OCR or content analysis. Sandboxed to
    the workspace; caps at 2 MB; unknown/non-image formats raise ToolError.
    Returns {base64, mime, width, height, bytes, description}.
    """
    p = resolve_in_workspace(path)
    if not p.exists() or not p.is_file():
        raise ToolError(f"no file at {path!r}")
    size = p.stat().st_size
    if size > config.FETCH_MAX_BYTES:
        raise ToolError(
            f"image is {_human_bytes(size)}, over the {_human_bytes(config.FETCH_MAX_BYTES)} cap"
        )
    data = p.read_bytes()
    mime = _sniff_image_mime(data)
    if mime is None:
        raise ToolError(
            f"{path!r} is not a recognized image (PNG/JPEG/GIF/WebP/BMP); "
            "read it with fs_read if it is text"
        )
    width, height = _image_dimensions(mime, data)
    return {
        "base64": base64.b64encode(data).decode(),
        "mime": mime,
        "width": width,
        "height": height,
        "bytes": size,
        "description": describe_image(mime, width, height, size),
    }
