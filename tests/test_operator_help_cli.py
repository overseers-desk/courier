"""CLI rendering check for the search operator inventory in ``--help``."""

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
    # Rich markup would swallow an unescaped [imap.NAME] in the prose; the
    # escape in _SEARCH_CLI_HELP keeps it visible to the reader.
    assert "[imap.NAME]" in result.output
