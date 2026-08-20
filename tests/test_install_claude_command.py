"""Tests for the ``courier install-claude-command`` installer.

The installer writes the Claude Code command file into the commands
directory of the configuration tree the session reads, ``CLAUDE_CONFIG_DIR``
when set and ``~/.claude`` otherwise. When an older version is already
present it prompts before replacing, except under ``--yes`` so that
non-interactive callers (Claude Code sessions, which cannot answer a
stdin prompt) can update without blocking.
"""

from pathlib import Path

from typer.testing import CliRunner

from courier import __version__
from courier.__main__ import app

runner = CliRunner()


def _command_file(home: Path) -> Path:
    return home / ".claude" / "commands" / "courier.md"


def _install_stale(home: Path) -> Path:
    """Write a command file stamped with an older version into ``home``."""
    dest = _command_file(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("version: 0.0.1\n# old\n")
    return dest


def test_fresh_install_writes_current_version(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert f"version: {__version__}" in _command_file(tmp_path).read_text()


def test_yes_replaces_stale_without_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_stale(tmp_path)
    # No stdin supplied: a prompt would abort, so success proves it never prompted.
    result = runner.invoke(app, ["install-claude-command", "--yes"])
    assert result.exit_code == 0
    assert f"version: {__version__}" in _command_file(tmp_path).read_text()


def test_stale_without_yes_prompts_and_aborts_on_no(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _install_stale(tmp_path)
    result = runner.invoke(app, ["install-claude-command"], input="n\n")
    assert result.exit_code == 1
    assert "version: 0.0.1" in _command_file(tmp_path).read_text()


def test_already_current_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = _command_file(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"version: {__version__}\n# current\n")
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert f"Already at {__version__}" in result.stdout


def test_install_follows_claude_config_dir(tmp_path, monkeypatch):
    """A session with CLAUDE_CONFIG_DIR set gets the command in that tree."""
    home = tmp_path / "home"
    configured = tmp_path / "elsewhere"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    result = runner.invoke(app, ["install-claude-command"])
    assert result.exit_code == 0
    assert f"version: {__version__}" in (
        configured / "commands" / "courier.md"
    ).read_text()
    assert not (home / ".claude").exists()
