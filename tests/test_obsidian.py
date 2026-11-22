import hashlib
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import unquote

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools import obsidian
from unified_agent.tools import vault as vault_tools


def _response(payload=None, text="", headers=None, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = text
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "00 - Inbox").mkdir(parents=True)
    (root / "Logs").mkdir()
    (root / "Wiki").mkdir()
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    monkeypatch.setenv("UA_OBSIDIAN_API_URL", "https://127.0.0.1:27124")
    monkeypatch.setenv("UA_OBSIDIAN_API_KEY", "test-key")
    return root


class TestObsidianTools:
    def test_not_configured_is_clean(self, monkeypatch):
        monkeypatch.delenv("UA_OBSIDIAN_API_URL", raising=False)
        monkeypatch.delenv("UA_OBSIDIAN_API_KEY", raising=False)

        assert obsidian.obsidian_status() == {
            "configured": False,
            "reason": "not-configured",
            "missing": ["UA_OBSIDIAN_API_URL", "UA_OBSIDIAN_API_KEY"],
        }

    @pytest.mark.parametrize("url", ["https://example.com", "https://0.0.0.0:27124"])
    def test_rejects_remote_api_url(self, vault, monkeypatch, url):
        monkeypatch.setenv("UA_OBSIDIAN_API_URL", url)

        with pytest.raises(ToolError, match="remote endpoints are not supported"):
            obsidian.obsidian_status()

    def test_status_returns_plugin_payload(self, vault, monkeypatch):
        response = _response({"ok": "OK", "authenticated": True})
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        result = obsidian.obsidian_status()

        assert result == {"configured": True, "ok": "OK", "authenticated": True}
        client.__enter__.return_value.request.assert_called_once_with(
            "GET",
            "https://127.0.0.1:27124/",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
        )

    def test_active_note_returns_content_location_and_body(self, vault, monkeypatch):
        response = _response(
            text="# Active note\n",
            headers={"Content-Location": "00 - Inbox/active.md"},
        )
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        assert obsidian.obsidian_active_note() == {
            "path": "00 - Inbox/active.md",
            "content": "# Active note\n",
        }

    def test_search_returns_plugin_results(self, vault, monkeypatch):
        response = _response([{"filename": "00 - Inbox/note.md", "score": 1}])
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        assert obsidian.obsidian_search("runtime bridge") == {
            "query": "runtime bridge",
            "results": [{"filename": "00 - Inbox/note.md", "score": 1}],
        }
        client.__enter__.return_value.request.assert_called_once_with(
            "POST",
            "https://127.0.0.1:27124/search/simple/",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
            params={"query": "runtime bridge"},
        )

    def test_append_uses_vault_write_boundary_and_structured_patch(self, vault, monkeypatch):
        response = _response({"path": "00 - Inbox/note.md"})
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        result = obsidian.obsidian_append("00 - Inbox/note.md", "new content")

        assert result == {"path": "00 - Inbox/note.md", "response": {"path": "00 - Inbox/note.md"}}
        client.__enter__.return_value.request.assert_called_once_with(
            "PATCH",
            "https://127.0.0.1:27124/vault/00%20-%20Inbox/note.md",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
            json={
                "operation": "append",
                "target": [],
                "targetType": "heading",
                "content": "new content",
            },
        )

    @pytest.mark.parametrize(
        ("target_type", "target", "expected_target"),
        [
            ("heading", "90-Day Roadmap", ["90-Day Roadmap"]),
            ("heading", None, []),
            ("frontmatter", "status", "status"),
            ("block", "^ref-block", "^ref-block"),
        ],
    )
    def test_append_nested_path_preserves_slashes(
        self, vault, monkeypatch, target_type, target, expected_target
    ):
        response = _response({"path": "Wiki/90-Day-Roadmap.md"})
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        result = obsidian.obsidian_append(
            "Wiki/90-Day-Roadmap.md",
            "line to append",
            target_type=target_type,
            target=target,
        )

        assert result == {
            "path": "Wiki/90-Day-Roadmap.md",
            "response": {"path": "Wiki/90-Day-Roadmap.md"},
        }
        called_args, called_kwargs = client.__enter__.return_value.request.call_args
        called_method, called_url = called_args
        assert called_method == "PATCH"
        assert called_url == "https://127.0.0.1:27124/vault/Wiki/90-Day-Roadmap.md"
        assert "Wiki/90-Day-Roadmap.md" in called_url
        assert "Wiki%2F90-Day-Roadmap.md" not in called_url
        assert called_kwargs["json"] == {
            "operation": "append",
            "target": expected_target,
            "targetType": target_type,
            "content": "line to append",
        }

    def test_open_nested_path_preserves_slashes(self, vault, monkeypatch):
        response = _response({"opened": True})
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        result = obsidian.obsidian_open("Wiki/90-Day-Roadmap.md")

        assert result == {
            "path": "Wiki/90-Day-Roadmap.md",
            "opened": True,
            "response": {"opened": True},
        }
        called_args, _ = client.__enter__.return_value.request.call_args
        called_method, called_url = called_args
        assert called_method == "POST"
        assert called_url == "https://127.0.0.1:27124/open/Wiki/90-Day-Roadmap.md"
        assert "Wiki/90-Day-Roadmap.md" in called_url
        assert "Wiki%2F90-Day-Roadmap.md" not in called_url

    def test_open_rejects_curated_note(self, vault):
        with pytest.raises(ToolError, match="restricted to"):
            obsidian.obsidian_open("01 - Projects/curated.md")


class TestObsidianAppendLock:
    """obsidian_append honours `locked: true` like vault_write (#1817)."""

    @staticmethod
    def _client(monkeypatch):
        client = MagicMock()
        client.__enter__.return_value.request.return_value = _response({"ok": True})
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)
        return client.__enter__.return_value.request

    def test_locked_note_refused_without_patch(self, vault, monkeypatch):
        (vault / "Wiki" / "X.md").write_text("---\nlocked: true\n---\nbody\n")
        request = self._client(monkeypatch)

        with pytest.raises(ToolError, match="locked"):
            obsidian.obsidian_append("Wiki/X.md", "x")

        request.assert_not_called()
        assert (vault / "Wiki" / "X.md").read_text() == "---\nlocked: true\n---\nbody\n"

    def test_locked_note_refused_for_every_target_type(self, vault, monkeypatch):
        (vault / "Wiki" / "X.md").write_text("---\nlocked: true\n---\nbody\n")
        request = self._client(monkeypatch)

        for target_type, target in (("heading", "H"), ("block", "^b"), ("frontmatter", "status")):
            with pytest.raises(ToolError, match="locked"):
                obsidian.obsidian_append("Wiki/X.md", "x", target_type=target_type, target=target)

        request.assert_not_called()

    def test_unlocked_note_still_appended(self, vault, monkeypatch):
        (vault / "Wiki" / "Y.md").write_text("---\nlocked: false\n---\nbody\n")
        request = self._client(monkeypatch)

        result = obsidian.obsidian_append("Wiki/Y.md", "x")

        assert result["path"] == "Wiki/Y.md"
        assert request.call_count == 1
        assert request.call_args.args[0] == "PATCH"

    def test_missing_note_behaviour_unchanged(self, vault, monkeypatch):
        request = self._client(monkeypatch)

        result = obsidian.obsidian_append("Wiki/New.md", "x")

        assert result["path"] == "Wiki/New.md"
        assert request.call_count == 1

    @pytest.mark.parametrize("target", ["locked", " Locked ", ["locked"]])
    def test_frontmatter_patch_of_locked_key_refused(self, vault, monkeypatch, target):
        (vault / "Wiki" / "Z.md").write_text("---\ntitle: z\n---\nbody\n")
        request = self._client(monkeypatch)

        with pytest.raises(ToolError, match="`locked` frontmatter key"):
            obsidian.obsidian_append("Wiki/Z.md", "true", target_type="frontmatter", target=target)
        with pytest.raises(ToolError, match="`locked` frontmatter key"):
            obsidian.obsidian_append(
                "Wiki/Missing.md", "true", target_type="frontmatter", target=target
            )

        request.assert_not_called()

    def test_frontmatter_patch_of_other_key_allowed(self, vault, monkeypatch):
        (vault / "Wiki" / "Z.md").write_text("---\ntitle: z\n---\nbody\n")
        request = self._client(monkeypatch)

        obsidian.obsidian_append("Wiki/Z.md", "done", target_type="frontmatter", target="status")

        assert request.call_count == 1

    def test_cloud_caller_still_refused_before_any_request(self, vault, monkeypatch):
        (vault / "Wiki" / "Y.md").write_text("body\n")
        request = self._client(monkeypatch)
        monkeypatch.setattr(obsidian.profiles, "cloud_hosted_caller", lambda: True)

        with pytest.raises(ToolError, match="not available to cloud-hosted callers"):
            obsidian.obsidian_append("Wiki/Y.md", "x")

        request.assert_not_called()

    def test_lock_added_during_the_patch_is_reported(self, vault, monkeypatch):
        note = vault / "Wiki" / "R.md"
        note.write_text("body\n")

        def lock_then_respond(*args, **kwargs):
            # Another writer locks the note after the pre-check, before the PATCH lands.
            note.write_text("---\nlocked: true\n---\nbody\n")
            return _response({"ok": True})

        client = MagicMock()
        client.__enter__.return_value.request.side_effect = lock_then_respond
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        result = obsidian.obsidian_append("Wiki/R.md", "x")

        assert result["locked_after_write"] is True
        assert "became locked" in result["warning"]
        assert result["path"] == "Wiki/R.md"

    def test_no_race_fields_when_the_note_stays_unlocked(self, vault, monkeypatch):
        (vault / "Wiki" / "R.md").write_text("body\n")
        request = self._client(monkeypatch)

        result = obsidian.obsidian_append("Wiki/R.md", "x")

        assert request.call_count == 1
        assert "locked_after_write" not in result
        assert "warning" not in result


class FakePlugin:
    """Stand-in for the Local REST API v5.1.0 markdown-patch 2.0 PATCH path.

    Mirrors `patchFileSectionMdp2`: `app.vault.read` the note (Obsidian 1.14.4
    drops one leading U+FEFF), check `ifMatch` against markdown-patch 2.0.0's
    `versionOf` (sha256 hex of that text, first 6 chars), then `vault.modify`
    the patched snapshot back (so the BOM is gone) with no further version
    check. `before_read` and `after_read` run another writer at those points.
    """

    def __init__(self, root, before_read=None, after_read=None):
        self.root, self.before_read, self.after_read = root, before_read, after_read
        self.instructions = []

    def __call__(self, method, url, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        self.instructions.append(json)
        note = self.root / unquote(url.split("/vault/", 1)[1])
        if self.before_read:
            self.before_read(note)
        if not note.is_file():
            return httpx.Response(404, request=request)
        text = note.read_bytes().decode("utf-8", "replace").removeprefix("\ufeff")  # vault.read
        if self.after_read:
            self.after_read(note)
        version = hashlib.sha256(text.encode()).hexdigest()[:6]
        if "ifMatch" in json and json["ifMatch"] != version:
            return httpx.Response(412, request=request, json={"message": "ifMatch failed"})
        patched = text + json["content"]
        note.write_bytes(patched.encode())  # vault.modify of the stale snapshot
        return httpx.Response(200, request=request, text=patched)

    def install(self, monkeypatch):
        client = MagicMock()
        client.__enter__.return_value.request.side_effect = self
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)
        return self


LOCKED_BODY = "---\nlocked: true\n---\nbody\n"


class TestObsidianAppendBarrier:
    """A lock added between the hub's check and the plugin's write (#1890)."""

    @pytest.mark.parametrize("body", ["body\n", "---\r\ntitle: x\r\n---\r\nbody\r\n"])
    def test_unchanged_note_passes_if_match(self, vault, monkeypatch, body):
        note = vault / "Wiki" / "U.md"
        note.write_bytes(body.encode())
        plugin = FakePlugin(vault).install(monkeypatch)

        result = obsidian.obsidian_append("Wiki/U.md", "x")

        assert "ifMatch" in plugin.instructions[0]
        assert note.read_bytes() == (body + "x").encode()
        assert "locked_after_write" not in result

    def test_bom_note_is_versioned_without_its_bom(self, vault, monkeypatch):
        # Obsidian's vault.read strips one leading BOM before the plugin hashes
        # the text; hashing U+FEFF too would refuse every append with 412.
        body = "\ufeff---\r\ntitle: x\r\n---\r\nbody\r\n"
        note = vault / "Wiki" / "BOM.md"
        note.write_bytes(body.encode())
        plugin = FakePlugin(vault).install(monkeypatch)

        result = obsidian.obsidian_append("Wiki/BOM.md", "x")

        expected = hashlib.sha256(body[1:].encode()).hexdigest()[:6]
        assert plugin.instructions[0]["ifMatch"] == expected
        assert note.read_bytes() == (body[1:] + "x").encode()  # vault.modify drops it
        assert "locked_after_write" not in result

    def test_bom_locked_note_is_refused(self, vault, monkeypatch):
        note = vault / "Wiki" / "BL.md"
        note.write_bytes(("\ufeff" + LOCKED_BODY).encode())
        plugin = FakePlugin(vault).install(monkeypatch)

        with pytest.raises(ToolError, match="locked"):
            obsidian.obsidian_append("Wiki/BL.md", "x")

        assert plugin.instructions == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable_note_on_disk_is_a_tool_error(self, vault, monkeypatch):
        note = vault / "Wiki" / "P.md"
        note.write_text("body\n")
        note.chmod(0)
        plugin = FakePlugin(vault).install(monkeypatch)
        try:
            with pytest.raises(ToolError, match="cannot read note"):
                obsidian.obsidian_append("Wiki/P.md", "x")
        finally:
            note.chmod(0o600)

        assert plugin.instructions == []

    def test_unverifiable_lock_after_the_write_is_a_warning_not_an_error(self, vault, monkeypatch):
        # The append has landed; raising now would invite a duplicate retry.
        (vault / "Wiki" / "W.md").write_text("body\n")
        FakePlugin(vault).install(monkeypatch)
        real = obsidian._is_locked_path
        calls = []

        def flaky(p):
            calls.append(p)
            raise ToolError("cannot read note to check its lock: denied")

        monkeypatch.setattr(obsidian, "_is_locked_path", flaky)
        result = obsidian.obsidian_append("Wiki/W.md", "x")
        monkeypatch.setattr(obsidian, "_is_locked_path", real)

        assert calls
        assert "locked_after_write" not in result
        assert "could not re-check" in result["warning"]

    def test_unreadable_note_is_refused_as_a_tool_error(self, vault, monkeypatch):
        note = vault / "Wiki" / "E.md"
        note.write_text("body\n")
        plugin = FakePlugin(vault).install(monkeypatch)
        real = open
        monkeypatch.setattr(
            obsidian,
            "open",
            lambda p, *a, **k: (
                (_ for _ in ()).throw(PermissionError("denied"))
                if Path(p).name == "E.md"
                else real(p, *a, **k)
            ),
            raising=False,
        )

        with pytest.raises(ToolError, match="cannot read note"):
            obsidian.obsidian_append("Wiki/E.md", "x")

        assert plugin.instructions == []

    def test_lock_inserted_before_the_plugin_read_is_refused(self, vault, monkeypatch):
        # A writer the hub cannot fence (Obsidian UI, sync) locks the note after
        # the hub's check but before the plugin reads it.
        note = vault / "Wiki" / "B.md"
        note.write_text("body\n")
        FakePlugin(vault, before_read=lambda n: n.write_text(LOCKED_BODY)).install(monkeypatch)

        with pytest.raises(ToolError, match="changed or locked since check"):
            obsidian.obsidian_append("Wiki/B.md", "x")

        assert note.read_text() == LOCKED_BODY

    def test_hub_lock_writer_cannot_land_inside_the_plugin_window(self, vault, monkeypatch):
        # The worst window: a hub writer locks the note after the plugin's read,
        # and the plugin then writes its stale snapshot over the lock.
        note = vault / "Wiki" / "C.md"
        note.write_text("body\n")
        seen = {}

        def lock_after_read(_note):
            writer = threading.Thread(
                target=lambda: seen.setdefault(
                    "write", vault_tools.vault_write("Wiki/C.md", LOCKED_BODY)
                )
            )
            writer.start()
            writer.join(timeout=0.5)  # pause here: the writer may land now if unfenced
            seen["blocked"] = writer.is_alive()
            seen["writer"] = writer

        FakePlugin(vault, after_read=lock_after_read).install(monkeypatch)

        result = obsidian.obsidian_append("Wiki/C.md", "x")
        seen["writer"].join(timeout=5)

        assert seen["blocked"], "vault_write landed inside obsidian_append's write window"
        assert "write" in seen
        assert note.read_text() == LOCKED_BODY
        assert "locked_after_write" not in result

    def test_large_note_is_versioned_in_bounded_reads(self, vault, monkeypatch):
        # Devin, PR #1175: an unbounded note must not be read whole. A multibyte
        # character straddles the read boundary and an invalid byte is replaced,
        # as the plugin's UTF-8 decode does, so its ifMatch still agrees.
        limit = vault_tools._LOCK_READ_CEILING
        note = vault / "Wiki" / "L.md"
        note.write_bytes(b"a" * (limit - 1) + "é".encode() + b"\xff" + b"b" * (2 * limit))
        reads = []
        real = open

        def tracking_open(p, *args, **kwargs):
            f = real(p, *args, **kwargs)
            read = f.read
            f.read = lambda n=-1: reads.append(n) or read(n)
            return f

        monkeypatch.setattr(obsidian, "open", tracking_open, raising=False)
        plugin = FakePlugin(vault).install(monkeypatch)

        result = obsidian.obsidian_append("Wiki/L.md", "x")

        assert reads and all(0 < n <= limit for n in reads), reads
        assert "ifMatch" in plugin.instructions[0]
        assert note.read_bytes().endswith(b"bx")  # not refused with 412
        assert "locked_after_write" not in result


class TestObsidianAppendTimeout:
    """A PATCH that times out may still be committed by the plugin (Devin, PR #1175)."""

    def _timing_out_plugin(self, vault, monkeypatch, late_write_after=None, on_timeout=None):
        def patch(method, url, headers=None, json=None, **kwargs):
            note = vault / unquote(url.split("/vault/", 1)[1])
            text = note.read_text()
            if late_write_after is not None:
                threading.Timer(
                    late_write_after, lambda: note.write_text(text + json["content"])
                ).start()
            if on_timeout:
                on_timeout()
            raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))

        client = MagicMock()
        client.__enter__.return_value.request.side_effect = patch
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

    def test_note_lock_is_held_until_the_late_write_lands(self, vault, monkeypatch):
        note = vault / "Wiki" / "T.md"
        note.write_text("body\n")
        writers = []

        def lock_now():
            # A hub writer that locks the note as soon as the hub lets go.
            writer = threading.Thread(
                target=vault_tools.vault_write, args=("Wiki/T.md", LOCKED_BODY)
            )
            writer.start()
            writers.append(writer)

        self._timing_out_plugin(vault, monkeypatch, late_write_after=0.3, on_timeout=lock_now)

        with pytest.raises(ToolError, match="outcome unknown.*probably landed"):
            obsidian.obsidian_append("Wiki/T.md", "x")
        writers[0].join(timeout=5)

        assert note.read_text() == LOCKED_BODY  # the stale late write did not erase the lock

    def test_unobserved_late_write_is_reported_as_unknown(self, vault, monkeypatch):
        monkeypatch.setattr(obsidian, "APPEND_SETTLE_S", 0.2)
        note = vault / "Wiki" / "N.md"
        note.write_text("body\n")
        self._timing_out_plugin(vault, monkeypatch)

        with pytest.raises(ToolError, match="outcome unknown.*may still write"):
            obsidian.obsidian_append("Wiki/N.md", "x")

        assert note.read_text() == "body\n"

    def test_refused_connection_is_a_plain_failure(self, vault, monkeypatch):
        (vault / "Wiki" / "R.md").write_text("body\n")
        client = MagicMock()
        client.__enter__.return_value.request.side_effect = httpx.ConnectError("refused")
        monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)

        with pytest.raises(ToolError, match="Obsidian request failed") as info:
            obsidian.obsidian_append("Wiki/R.md", "x")

        assert "outcome unknown" not in str(info.value)
