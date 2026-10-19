"""Secrets held in configuration never reach printed output.

Courier is run by AI agents, whose transcripts keep whatever a command
prints. A crash once printed an IMAP app password: Typer's pretty traceback
rendered the locals, and a config dataclass's repr carried the password.
"""

import ast
from pathlib import Path

from courier import __main__ as cli
from courier.config import CourierConfig, ImapBlock, OAuth2Config, SmtpConfig

SECRET = "s3cret-not-for-output"


def _oauth2() -> OAuth2Config:
    return OAuth2Config(
        client_id="client-id",
        client_secret=SECRET,
        refresh_token=SECRET,
        access_token=SECRET,
    )


def test_imap_block_repr_omits_password_and_oauth2_secrets() -> None:
    block = ImapBlock(
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password=SECRET,
        oauth2=_oauth2(),
    )
    assert SECRET not in repr(block)
    assert "user@example.com" in repr(block)


def test_smtp_config_repr_omits_password() -> None:
    smtp = SmtpConfig(host="smtp.example.com", username="user", password=SECRET)
    assert SECRET not in repr(smtp)


def test_oauth2_config_repr_omits_secrets() -> None:
    assert SECRET not in repr(_oauth2())


def test_courier_config_repr_omits_nested_secrets() -> None:
    block = ImapBlock(host="imap.example.com", port=993, username="u", password=SECRET)
    smtp = SmtpConfig(host="smtp.example.com", username="u", password=SECRET)
    cfg = CourierConfig(imap_blocks={"a": block}, smtp_blocks={"s": smtp})
    assert SECRET not in repr(cfg)


def test_cli_app_turns_off_traceback_locals_explicitly() -> None:
    # Typer's default differs by version (0.15 shows locals, later ones do
    # not), so the app must pass the setting itself, not inherit it.
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    calls = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "app" for t in node.targets)
        and isinstance(node.value, ast.Call)
    ]
    assert calls, "no `app = typer.Typer(...)` found"
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    value = keywords.get("pretty_exceptions_show_locals")
    assert isinstance(value, ast.Constant) and value.value is False
    assert cli.app.pretty_exceptions_show_locals is False
