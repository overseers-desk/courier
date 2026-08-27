"""CLI rendering checks for ``--help`` output."""

from typer.testing import CliRunner

from courier.__main__ import app


def test_search_help_lists_operator_inventory():
    """``courier search --help`` must surface the derived operator inventory.

    The sentinels are single tokens so rich's word-wrapping cannot split
    them across lines and hide them from a substring check.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    assert "msgid:" in result.output
    assert "subject:" in result.output
    assert "larger:" in result.output
    assert "has:" in result.output
    # Rich markup would swallow [imap.NAME] as a markup tag; the app runs
    # with markup interpretation off so the TOML table names stay visible.
    assert "[imap.NAME]" in result.output


def test_help_text_keeps_bracketed_config_names():
    """Bracketed TOML table names must survive help rendering verbatim.

    Help text names config blocks the way config.toml spells them:
    ``[imap.*]``, ``[smtp.*]``, ``[identity.*]``.  Under typer's default
    rich markup mode those tokens parse as markup tags and are deleted
    from the rendered output, leaving "List configured , , and  blocks".
    """
    runner = CliRunner()
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0
    assert "[imap.*]" in result.output
    assert "[smtp.*]" in result.output
    assert "[identity.*]" in result.output

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "[imap.NAME]" in result.output
