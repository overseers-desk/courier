"""CLI tests for the ``send-draft`` subcommand.

Mocks the IMAP client and SMTP transport so no network is required. Verifies:
    - happy path: From parsed, identity resolved, transport invoked, FCC done,
      draft deleted, JSON output carries the expected fields.
    - unknown From in the draft -> hard error (the AI-safety win).
    - --dry-run stops before MAIL FROM and skips delete.
    - --keep-draft preserves the draft on success.
    - --bcc adds envelope-time RCPTs without rewriting the draft body.
"""

import json
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import (
    CourierConfig,
    Identity,
    ImapBlock,
    SmtpConfig,
)
from courier.imap_client import AppendResult

runner = CliRunner()


def _draft_bytes(from_addr: str = "alice@x.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "recipient@y.com"
    msg["Subject"] = "draft"
    msg["Message-ID"] = "<draft@local>"
    msg.set_content("body")
    return msg.as_bytes()


def _cfg_with_identities() -> CourierConfig:
    """Default test config: non-Gmail SMTP so FCC is exercised by save_sent='auto'.

    Switching to Fastmail-style means resolve_save_sent() returns True (the
    server doesn't auto-file outgoing), so the FCC code path runs and the
    tests can assert against fcc_folder/fcc_uid.
    """
    block = ImapBlock(
        host="imap.fastmail.com",
        port=993,
        username="login@x.com",
        password="p",
        default_smtp="fast",
    )
    return CourierConfig(
        imap_blocks={"acct": block},
        _default_imap="acct",
        identities={
            "alice": Identity(imap="acct", address="alice@x.com"),
            "bob": Identity(imap="acct", address="bob@x.com"),
        },
        smtp_blocks={
            "fast": SmtpConfig(
                host="smtp.fastmail.com",
                port=587,
                username="login@x.com",
                password="p",
            )
        },
    )


def _build_send_result() -> dict:
    return {
        "message_id_local": "<draft@local>",
        "message_id_sent": "<draft@local>",
        "smtp_response": "OK",
        "accepted_recipients": ["recipient@y.com"],
        "refused_recipients": [],
    }


def _client_with_draft(from_addr: str = "alice@x.com") -> MagicMock:
    client = MagicMock()
    client.fetch_raw.return_value = {"raw": _draft_bytes(from_addr), "subject": "draft"}
    client.resolve_sent_folder.side_effect = lambda configured=None: (
        configured if configured is not None else "Sent"
    )
    client.append_raw.return_value = AppendResult(uid=999, uidvalidity=1)
    client.delete_email.return_value = True
    return client


class TestSendDraftHappyPath:
    def test_sends_and_deletes_draft(self):
        cfg = _cfg_with_identities()
        client = _client_with_draft()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(_draft_bytes(), _build_send_result()),
            ) as send_mock,
        ):
            result = runner.invoke(app, ["send-draft", "-f", "Drafts", "-u", "42"])
        assert result.exit_code == 0, result.output
        assert send_mock.called
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["identity"] == "alice@x.com"
        assert out["fcc_folder"] == "Sent"
        assert out["fcc_uid"] == 999
        assert out["draft_removed"] is True
        client.delete_email.assert_called_once_with(42, "Drafts")

    def test_keep_draft_skips_delete(self):
        cfg = _cfg_with_identities()
        client = _client_with_draft()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(_draft_bytes(), _build_send_result()),
            ),
        ):
            result = runner.invoke(
                app,
                ["send-draft", "-f", "Drafts", "-u", "42", "--keep-draft"],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["draft_removed"] is False
        client.delete_email.assert_not_called()


class TestSendDraftUnknownFrom:
    def test_hard_error_on_unknown_from(self):
        cfg = _cfg_with_identities()
        client = _client_with_draft(from_addr="impostor@evil.com")
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(app, ["send-draft", "-f", "Drafts", "-u", "42"])
        assert result.exit_code == 1
        assert "impostor@evil.com" in result.output or "impostor@evil.com" in (
            result.stderr if result.stderr else ""
        )
        send_mock.assert_not_called()  # never reached transport
        client.delete_email.assert_not_called()


class TestSendDraftDryRun:
    def test_dry_run_does_not_invoke_transport(self):
        cfg = _cfg_with_identities()
        client = _client_with_draft()
        # Fake SMTP factory used by --dry-run
        smtp_calls: list = []

        class _FakeSMTP:
            def __init__(self, host, port):
                smtp_calls.append(("init", host, port))

            def ehlo(self):
                smtp_calls.append(("ehlo",))

            def starttls(self):
                smtp_calls.append(("starttls",))

            def login(self, u, p):
                smtp_calls.append(("login", u, p))

            def quit(self):
                smtp_calls.append(("quit",))

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport._pick_default_transport",
                return_value=_FakeSMTP,
            ),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                ["send-draft", "-f", "Drafts", "-u", "42", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["dry_run"] is True
        assert out["identity"] == "alice@x.com"
        send_mock.assert_not_called()  # transport.send never called
        client.delete_email.assert_not_called()
        # Sanity: connect/ehlo/starttls/login/quit were exercised
        assert any(c[0] == "ehlo" for c in smtp_calls)
        assert any(c[0] == "login" for c in smtp_calls)


class TestSendDraftBcc:
    def test_bcc_added_to_envelope_without_rewriting_draft(self):
        cfg = _cfg_with_identities()
        client = _client_with_draft()
        captured_msg: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured_msg.append(msg)
            return (_draft_bytes(), _build_send_result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "send-draft",
                    "-f",
                    "Drafts",
                    "-u",
                    "42",
                    "--bcc",
                    "audit@x.com",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 0, result.output
        # The MIME passed to send() must carry the new Bcc.
        bcc_value = str(captured_msg[0].get("Bcc", "") or "")
        assert "audit@x.com" in bcc_value
