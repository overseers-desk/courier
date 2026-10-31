"""Tool-wide options work on either side of the verb and show in every help."""

import sys
from pathlib import Path
from typing import Dict, List

import click
import pytest
import typer
from typer.testing import CliRunner

from courier.__main__ import (
    _HIDDEN_SPELLINGS,
    _TOOL_WIDE_BOOL,
    _TOOL_WIDE_VALUE,
    _print_eager_warnings_if_relevant,
    _rewrite_argv,
    _split_chain_argv,
    app,
)

runner = CliRunner()
_GROUP = typer.main.get_command(app)
assert isinstance(_GROUP, click.Group)
_COMMANDS = sorted(_GROUP.commands)
_TOOL_WIDE = _TOOL_WIDE_VALUE | _TOOL_WIDE_BOOL


def _help(*args: str) -> str:
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def _spellings(params: List[click.Parameter]) -> set:
    return {s for p in params for s in p.opts + p.secondary_opts}


@pytest.mark.parametrize(
    "flag",
    [
        ["-a", "acct"],
        ["--account", "acct"],
        ["--account=acct"],
        ["-A"],
        ["--all-accounts"],
        ["-c", "/tmp/x.toml"],
        ["--config=/tmp/x.toml"],
        ["-v"],
        ["--no-cache"],
    ],
)
def test_tool_wide_flag_after_the_verb_moves_in_front_of_it(flag: List[str]) -> None:
    assert _rewrite_argv(["save", "-f", "INBOX", *flag, "-u", "3"]) == [
        *flag,
        "save",
        "-f",
        "INBOX",
        "-u",
        "3",
    ]


@pytest.mark.parametrize(
    "old, new",
    [
        (["--imap", "x"], ["--account", "x"]),
        (["--imap=x"], ["--account=x"]),
        (["--all-imap"], ["--all-accounts"]),
    ],
)
def test_former_spellings_still_work_on_either_side(
    old: List[str], new: List[str]
) -> None:
    assert _rewrite_argv([*old, "folders"]) == [*new, "folders"]
    assert _rewrite_argv(["folders", *old]) == [*new, "folders"]


def test_copy_former_source_spelling_still_works() -> None:
    assert _rewrite_argv(["copy", "--from-imap", "src", "-u", "1"]) == [
        "copy",
        "--from-account",
        "src",
        "-u",
        "1",
    ]


def test_config_after_the_verb_reaches_the_command(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        '[imap.acct]\nhost = "imap.example.com"\nusername = "u@x"\npassword = "p"\n'
    )
    result = runner.invoke(app, ["list", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert "imap.example.com" in result.output


def test_format_before_the_verb_with_account_after_it() -> None:
    argv = _rewrite_argv(["--format", "oneline", "search", "q", "-a", "x"])
    split = _split_chain_argv(argv)
    assert split is not None
    global_argv, verbs, out_format, _ = split
    assert (global_argv, verbs, out_format) == (
        ["-a", "x"],
        [("search", ["q"])],
        "oneline",
    )


@pytest.mark.parametrize("command", _COMMANDS)
def test_every_command_help_lists_the_tool_wide_options(command: str) -> None:
    section = _help(command).split("Tool-wide options:", 1)[1]
    for shown in ("-a, --account", "-A, --all-accounts", "-c, --config"):
        assert shown in section
    assert "-v, --verbose" in section and "--no-cache" in section
    assert "--version" not in section


@pytest.mark.parametrize("args", [[]] + [[c] for c in _COMMANDS])
def test_no_help_names_a_former_spelling(args: List[str]) -> None:
    text = _help(*args)
    assert not [old for old in _HIDDEN_SPELLINGS if old in text]


def test_tool_wide_table_matches_the_callback() -> None:
    stateful = [
        p
        for p in _GROUP.params
        if isinstance(p, click.Option) and p.expose_value and not p.is_eager
    ]
    assert _spellings(stateful) == _TOOL_WIDE


@pytest.mark.parametrize("command", _COMMANDS)
def test_no_command_declares_a_tool_wide_spelling(command: str) -> None:
    # The argv rewrite would move such an option in front of the verb, and
    # the verb would never see it (#36).
    assert not _spellings(_GROUP.commands[command].params) & _TOOL_WIDE


@pytest.mark.parametrize("lead", [["-A"], ["-v"], ["--no-cache"], ["-a", "x"]])
def test_help_after_tool_wide_flags_prints_config_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, lead: List[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["courier", *lead, "--help"])
    monkeypatch.setattr(
        "courier.__main__.load_config_with_warnings", lambda path: (None, ["w"])
    )
    _print_eager_warnings_if_relevant()
    assert "warn: w" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args", [["-c", "F", "mcp"], ["mcp", "-c", "F"], ["mcp", "--config", "F"]]
)
def test_mcp_takes_the_tool_wide_config(
    monkeypatch: pytest.MonkeyPatch, args: List[str]
) -> None:
    from courier import mcp_server

    seen: Dict[str, object] = {}
    monkeypatch.setattr(mcp_server, "setup_server_logging", lambda: None)
    monkeypatch.setattr(
        mcp_server, "create_server", lambda config, debug: seen.update(config=config)
    )
    monkeypatch.setattr(mcp_server, "run_server", lambda server: None)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen == {"config": "F"}
