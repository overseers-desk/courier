"""Tests for keeping the courier command for Claude Code current.

Every run rewrites the command file when it is missing or differs from
``SLASH_COMMAND``, in the commands directory of the configuration tree the
session reads (``CLAUDE_CONFIG_DIR`` when set, ``~/.claude`` otherwise), so an
upgraded courier carries its command along unprompted. ``install-claude-command``
does the same write explicitly and reports where.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import courier.__main__ as cli
from courier.__main__ import app
from courier._claude_command import SLASH_COMMAND

runner = CliRunner()

# Taken at import, before conftest's autouse stub replaces it for each test.
_REAL_REFRESH = cli._refresh_claude_command


@pytest.fixture(autouse=True)
def _live_refresh(monkeypatch):
    """Undo conftest's no-op stub: these tests exercise the real refresh."""
    monkeypatch.setattr(cli, "_refresh_claude_command", _REAL_REFRESH)


def _command_file(home: Path) -> Path:
    return home / ".claude" / "commands" / "courier.md"


def _write(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)


def test_any_run_replaces_a_stale_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = _command_file(tmp_path)
    _write(dest, "# old\n")
    result = runner.invoke(app, ["config-sample"])
    assert result.exit_code == 0
    assert dest.read_text() == SLASH_COMMAND


def test_any_run_installs_a_missing_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    runner.invoke(app, ["config-sample"])
    assert _command_file(tmp_path).read_text() == SLASH_COMMAND


def test_refresh_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(_command_file(tmp_path), "# old\n")
    result = runner.invoke(app, ["config-sample"])
    assert "courier.md" not in result.output
    assert "install-claude-command" not in result.output


def test_no_claude_code_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner.invoke(app, ["config-sample"])
    assert not (tmp_path / ".claude").exists()


def test_same_named_skill_supersedes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude" / "skills" / "courier").mkdir(parents=True)
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert "Skipped" in result.stdout
    assert not _command_file(tmp_path).exists()


def test_unwritable_tree_does_not_fail_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    commands = tmp_path / ".claude" / "commands"
    commands.mkdir(parents=True)
    commands.chmod(0o500)
    try:
        result = runner.invoke(app, ["config-sample"])
    finally:
        commands.chmod(0o700)
    assert result.exit_code == 0


def test_install_reports_where(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert f"Installed: {_command_file(tmp_path)}" in result.stdout
    assert _command_file(tmp_path).read_text() == SLASH_COMMAND


def test_install_follows_claude_config_dir(tmp_path, monkeypatch):
    """A session with CLAUDE_CONFIG_DIR set gets the command in that tree."""
    home = tmp_path / "home"
    configured = tmp_path / "elsewhere"
    configured.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert (configured / "commands" / "courier.md").read_text() == SLASH_COMMAND
    assert not (home / ".claude").exists()
