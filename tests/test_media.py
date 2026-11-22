"""Tests for local-only media input (task 63 B).

read_image is workspace-sandboxed, stdlib-only, and never OCRs — description
is deterministic metadata. Dimension parsing is best-effort.
"""

import base64
import struct

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.safety import resolve_in_workspace
from unified_agent.tools.media import _sniff_image_mime, read_image


def _png(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x00" * 5


def _gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 4


def _write(rel: str, data: bytes) -> None:
    p = resolve_in_workspace(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class TestSniff:
    def test_known_formats(self):
        assert _sniff_image_mime(_png(1, 1)) == "image/png"
        assert _sniff_image_mime(_gif(1, 1)) == "image/gif"
        assert _sniff_image_mime(b"\xff\xd8\xff\xe0garbage") == "image/jpeg"
        assert _sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
        assert _sniff_image_mime(b"not an image") is None


class TestReadImage:
    def test_png_dimensions_and_description(self, isolated_env):
        _write("shot.png", _png(800, 600))
        out = read_image("shot.png")
        assert out["mime"] == "image/png"
        assert (out["width"], out["height"]) == (800, 600)
        assert "PNG image, 800x600" in out["description"]
        assert base64.b64decode(out["base64"]) == _png(800, 600)

    def test_gif_dimensions(self, isolated_env):
        _write("a.gif", _gif(20, 10))
        out = read_image("a.gif")
        assert (out["width"], out["height"]) == (20, 10)

    def test_non_image_rejected(self, isolated_env):
        _write("notes.txt", b"just text")
        with pytest.raises(ToolError, match="not a recognized image"):
            read_image("notes.txt")

    def test_missing_file(self, isolated_env):
        with pytest.raises(ToolError, match="no file at"):
            read_image("ghost.png")

    def test_over_cap_rejected(self, isolated_env, monkeypatch):
        monkeypatch.setattr(config, "FETCH_MAX_BYTES", 64)
        _write("big.png", _png(1, 1) + b"\x00" * 200)
        with pytest.raises(ToolError, match="over the"):
            read_image("big.png")

    def test_escapes_workspace_rejected(self, isolated_env):
        with pytest.raises(ToolError):
            read_image("../../etc/passwd")
