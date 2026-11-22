"""Tests for RESUME.md session end card generator (Task #159)."""

from unified_agent import session_end, storage


def test_generate_resume_content_structure(tmp_path):
    """Verify that generate_resume_content returns a string with structured 3 bullets."""
    content = session_end.generate_resume_content(repo_path=tmp_path)

    assert "# RESUME" in content
    assert "• Done:" in content
    assert "• State / Broken:" in content
    assert "• Next Action:" in content

    lines = content.strip().splitlines()
    bullet_lines = [
        item
        for item in lines
        if item.startswith("• Done")
        or item.startswith("• State / Broken")
        or item.startswith("• Next Action")
    ]
    assert len(bullet_lines) == 3


def test_generate_resume_file_creates_resume_md(tmp_path):
    """Verify that generate_resume_file creates RESUME.md at project root."""
    resume_path = tmp_path / "RESUME.md"
    assert not resume_path.exists()

    result_path = session_end.generate_resume_file(repo_path=tmp_path)

    assert result_path == resume_path
    assert resume_path.exists()
    content = resume_path.read_text(encoding="utf-8")
    assert "• Done:" in content
    assert "• State / Broken:" in content
    assert "• Next Action:" in content


def test_generate_resume_with_completed_and_active_tasks(tmp_path):
    """Verify RESUME.md incorporates completed tasks and active tasks from SQLite."""
    ts = "2026-07-27T00:00:00+00:00"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at, completed_at, claimed_by) "
            "VALUES (101, 'Fix memory leak', 'done', ?, ?, ?, 'Desktop_agent3')",
            (ts, ts, ts),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at, claimed_by) "
            "VALUES (102, 'Refactor session handler', 'doing', ?, ?, 'Desktop_Agent1')",
            (ts, ts),
        )
        conn.commit()
    finally:
        conn.close()

    content = session_end.generate_resume_content(repo_path=tmp_path)

    assert "#101" in content or "Fix memory leak" in content
    assert "#102" in content or "Refactor session handler" in content


def test_generate_resume_cli_main(tmp_path, monkeypatch, capsys):
    """Verify session_end.main CLI execution creates RESUME.md and prints summary."""
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["unified-agent-session-end", "--path", str(tmp_path)])

    exit_code = session_end.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Wrote RESUME.md" in captured.out
    assert (tmp_path / "RESUME.md").exists()


def test_generate_resume_cli_dry_run(tmp_path, monkeypatch, capsys):
    """Verify session_end.main with --dry-run prints to stdout without writing file."""
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["unified-agent-session-end", "--path", str(tmp_path), "--dry-run"]
    )

    exit_code = session_end.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "• Done:" in captured.out
    assert not (tmp_path / "RESUME.md").exists()
