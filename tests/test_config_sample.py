"""Tests for the ``courier config-sample`` command.

The command prints the bundled sample configuration to stdout so that
pip, pipx, and Homebrew users — who have no repository checkout — can
bootstrap a config with::

    courier config-sample > ~/.config/courier/config.toml

It writes nothing to disk and needs no existing config.
"""

import tomllib
from importlib.resources import files
from pathlib import Path

from typer.testing import CliRunner

from courier.__main__ import app

runner = CliRunner()


def test_config_sample_prints_documented_tables():
    """The sample surfaces the [imap], [smtp], and [identity] tables."""
    result = runner.invoke(app, ["config-sample"])
    assert result.exit_code == 0
    assert "default_imap" in result.stdout
    assert "[imap.personal]" in result.stdout
    assert "[smtp.gmail]" in result.stdout
    assert "[identity.personal]" in result.stdout


def test_config_sample_is_valid_toml():
    """The emitted template parses and carries the worked example values."""
    result = runner.invoke(app, ["config-sample"])
    assert result.exit_code == 0
    parsed = tomllib.loads(result.stdout)
    assert parsed["imap"]["personal"]["host"] == "imap.gmail.com"
    assert "gmail" in parsed["smtp"]
    assert parsed["identity"]["personal"]["address"] == "you@gmail.com"


def test_config_sample_emits_bundled_file_verbatim():
    """Output is exactly the package's bundled config.sample.toml."""
    expected = (
        files("courier").joinpath("config.sample.toml").read_text(encoding="utf-8")
    )
    result = runner.invoke(app, ["config-sample"])
    assert result.exit_code == 0
    assert result.stdout == expected


def test_config_sample_needs_no_existing_config(tmp_path, monkeypatch):
    """The command works with an empty HOME (no config.toml present)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = runner.invoke(app, ["config-sample"])
    assert result.exit_code == 0
    assert "[imap.personal]" in result.stdout
