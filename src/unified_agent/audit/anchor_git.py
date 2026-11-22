"""Signed-git-commit anchor backend (task #1265, F2).

Commits a small JSON file (chain head digest + timestamp) into a *separately*
configured git repository (``UA_EVIDENCE_ANCHOR_GIT_REPO``) — never the
workspace repo, since a witness an operator can already rewrite is not
independent of them. Signing is whatever the operator's ambient git config
(``user.signingkey`` / ``commit.gpgsign``) already does; this module never
generates, stores, or even reads key material — it just runs ``git commit``
and later reports whether ``git verify-commit`` is happy.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from .anchor import AnchorBackend


def _run_git(
    repo: Path, args: list[str], *, check: bool = True, timeout_s: float = 30
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"git {' '.join(args)} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise ToolError(f"git {' '.join(args)} could not start: {exc}") from exc
    if check and proc.returncode != 0:
        raise ToolError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


class GitAnchorBackend(AnchorBackend):
    """Witness the chain head with a commit to a separately configured repo."""

    name = "git"

    def anchor(self, head_digest: str, *, day: str | None = None) -> dict[str, Any]:
        repo = config.evidence_anchor_git_repo()
        if repo is None:
            raise ToolError("UA_EVIDENCE_ANCHOR_GIT_REPO is not configured")
        if not (repo / ".git").exists():
            raise ToolError(f"not a git repository: {repo}")

        lock_name = _run_git(
            repo, ["rev-parse", "--git-path", "ua-evidence-anchor.lock"]
        ).stdout.strip()
        lock_path = Path(lock_name)
        if not lock_path.is_absolute():
            lock_path = repo / lock_path
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._anchor_locked(repo, head_digest, day)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _anchor_locked(self, repo: Path, head_digest: str, day: str | None) -> dict[str, Any]:

        stamp = day or datetime.now(UTC).date().isoformat()
        anchored_at = datetime.now(UTC).isoformat(timespec="seconds")
        anchor_dir = str(config.evidence_anchor_git_dir()).strip("/")
        rel_path = f"{anchor_dir}/{stamp}.json"
        file_path = (repo / rel_path).resolve()
        # The anchor file must live inside the witness repo: an absolute or
        # `..` UA_EVIDENCE_ANCHOR_GIT_DIR would otherwise write elsewhere
        # before git ever gets a chance to refuse.
        if not file_path.is_relative_to(repo.resolve()) or ".git" in Path(rel_path).parts:
            raise ToolError(
                f"UA_EVIDENCE_ANCHOR_GIT_DIR must be a relative path inside {repo}: {anchor_dir!r}"
            )

        # This is a dedicated witness repo. Refuse a pre-staged index so a
        # commit cannot accidentally carry unrelated operator material.
        staged = _run_git(repo, ["diff", "--cached", "--name-only"]).stdout.strip()
        if staged:
            raise ToolError("witness repository has staged changes; unstage them before anchoring")

        # Reuse a matching commit only if it satisfies the current signature
        # and signer pin. A failed first attempt must be repairable after the
        # operator corrects signing configuration.
        invalid_existing = False
        if file_path.is_file():
            try:
                existing = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            # A signed repair may be an allowed empty commit, which path-
            # limited Git history omits even though its tree contains this
            # anchor file. Check HEAD before path history to reuse it.
            head_commit = _run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
            tracked = _run_git(repo, ["log", "-1", "--format=%H", "--", rel_path], check=False)
            clean = not _run_git(
                repo, ["status", "--porcelain", "--", rel_path], check=False
            ).stdout.strip()
            if existing.get("head_hash") == head_digest and clean:
                for sha in dict.fromkeys((head_commit.stdout.strip(), tracked.stdout.strip())):
                    if not sha:
                        continue
                    candidate = {
                        "backend": self.name,
                        "repo": str(repo),
                        "path": rel_path,
                        "commit_sha": sha,
                        "head_hash": head_digest,
                        "anchored_at": existing.get("anchored_at", anchored_at),
                        "anchor_action": "unchanged",
                    }
                    if self.verify(candidate, head_digest).get("verified"):
                        return candidate
                invalid_existing = bool(tracked.stdout.strip())

        file_path.parent.mkdir(parents=True, exist_ok=True)
        prior_content = file_path.read_bytes() if file_path.exists() else None
        content = (
            json.dumps(
                {"head_hash": head_digest, "anchored_at": anchored_at}, indent=2, sort_keys=True
            )
            + "\n"
        )
        file_path.write_text(content, encoding="utf-8")

        commit_message = f"chore(evidence-anchor): chain head {head_digest[:16]} at {anchored_at}"
        try:
            _run_git(repo, ["add", "--", rel_path])
            status = _run_git(repo, ["status", "--porcelain", "--", rel_path], check=False)
            if not status.stdout.strip() and invalid_existing:
                # Same bytes, bad prior signature: make a new signed commit
                # without introducing unrelated staged files.
                _run_git(
                    repo,
                    [
                        "commit",
                        "--allow-empty",
                        "--only",
                        "-S",
                        "-m",
                        commit_message,
                        "--",
                        rel_path,
                    ],
                )
            elif status.stdout.strip():
                args = ["commit"] + (["-S"] if invalid_existing else [])
                _run_git(repo, [*args, "-m", commit_message, "--", rel_path])
            else:
                raise ToolError("anchor file did not change and no valid commit was found")
        except Exception:
            # The index was clean before this method staged its own path.
            # Restore only that path so a signing failure remains retryable.
            if _run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0:
                _run_git(repo, ["reset", "--", rel_path], check=False)
            else:
                _run_git(repo, ["rm", "--cached", "--ignore-unmatch", "--", rel_path], check=False)
            if file_path.is_file() and file_path.read_bytes() == content.encode("utf-8"):
                if prior_content is None:
                    file_path.unlink()
                else:
                    file_path.write_bytes(prior_content)
            raise
        sha = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        return {
            "backend": self.name,
            "repo": str(repo),
            "path": rel_path,
            "commit_sha": sha,
            "head_hash": head_digest,
            "anchored_at": anchored_at,
            "anchor_action": "created",
        }

    def verify(self, ref: dict[str, Any], head_digest: str) -> dict[str, Any]:
        repo_str = ref.get("repo")
        rel_path = ref.get("path")
        commit_sha = ref.get("commit_sha")
        if not repo_str or not rel_path or not commit_sha:
            return {
                "verified": False,
                "reason": "anchor ref missing repo/path/commit_sha",
                "detail": {},
            }
        if (
            not isinstance(repo_str, str)
            or not isinstance(rel_path, str)
            or not isinstance(commit_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha) is None
            or Path(rel_path).is_absolute()
            or ".." in Path(rel_path).parts
            or rel_path.startswith("-")
        ):
            return {"verified": False, "reason": "invalid git anchor reference", "detail": {}}
        configured_repo = config.evidence_anchor_git_repo()
        if configured_repo is None:
            return {"verified": False, "reason": "witness repo is not configured", "detail": {}}
        try:
            trusted_repo = configured_repo.resolve(strict=True)
        except (OSError, RuntimeError):
            return {"verified": False, "reason": "witness repo is not available", "detail": {}}
        try:
            repo = Path(repo_str).resolve(strict=True)
        except (OSError, RuntimeError):
            return {"verified": False, "reason": "anchor repo path does not exist", "detail": {}}
        if repo != trusted_repo:
            return {
                "verified": False,
                "reason": "anchor repo differs from configured witness",
                "detail": {},
            }
        if not repo.exists():
            return {
                "verified": False,
                "reason": "anchor repo path does not exist",
                "detail": {"repo": repo_str},
            }

        show = _run_git(repo, ["show", f"{commit_sha}:{rel_path}"], check=False, timeout_s=5)
        if show.returncode != 0:
            return {
                "verified": False,
                "reason": "commit does not contain the anchor file",
                "detail": {
                    "commit_sha": commit_sha,
                    "path": rel_path,
                    "stderr": show.stderr.strip(),
                },
            }
        try:
            payload = json.loads(show.stdout)
        except json.JSONDecodeError:
            return {
                "verified": False,
                "reason": "anchor file content is not valid JSON",
                "detail": {"commit_sha": commit_sha},
            }

        verify_commit = _run_git(repo, ["verify-commit", commit_sha], check=False, timeout_s=5)
        signature_status = "valid" if verify_commit.returncode == 0 else "unsigned_or_invalid"
        expected_signer = config.evidence_anchor_git_signer_fingerprint()
        signer = ""
        if signature_status == "valid":
            signer = _run_git(
                repo, ["log", "-1", "--format=%GF", commit_sha], check=False, timeout_s=5
            ).stdout.strip()
        detail = {
            "commit_sha": commit_sha,
            "path": rel_path,
            "signature_status": signature_status,
            "signature_detail": (verify_commit.stderr or verify_commit.stdout).strip(),
            "signer_fingerprint": signer,
        }

        if payload.get("head_hash") != head_digest:
            return {"verified": False, "reason": "head_hash mismatch", "detail": detail}
        # A matching file in an unsigned commit proves nothing an operator
        # couldn't rewrite: the witness is the signature.
        if signature_status != "valid":
            return {
                "verified": False,
                "reason": "commit content matches head hash but the commit signature does not "
                "verify (git verify-commit)",
                "detail": {**detail, "content_matches": True},
            }
        if not expected_signer or signer != expected_signer:
            return {
                "verified": False,
                "reason": "commit signer fingerprint is not pinned or does not match",
                "detail": {**detail, "content_matches": True},
            }
        return {
            "verified": True,
            "reason": "signed commit content matches head hash",
            "detail": {**detail, "content_matches": True},
        }

    def verification_recipe(self, ref: dict[str, Any]) -> str:
        repo = ref.get("repo", "?")
        sha = ref.get("commit_sha", "?")
        path = ref.get("path", "?")
        return (
            f"In `{repo}`, run `git show {sha}:{path}` and confirm head_hash matches "
            f"anchor.anchored_head, then `git verify-commit {sha}` and compare the signer fingerprint "
            "with UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT. "
            "A later chain_integrity.head_hash also requires a verified ledger link."
        )
