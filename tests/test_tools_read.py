"""Tests for the `read` CLI command's threading-header surfacing.

aesop SPAR-A captures the parent's Message-ID, In-Reply-To, and References
into the approach YAML at draft time so T3 can build a threaded reply locally
at send time (see overseers-desk/aesop#79). The data is already on the Email
model; these tests pin the CLI output contract.
"""

import json
from datetime import datetime
from typing import List, Optional
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import CourierConfig, ImapBlock
from courier.models import Email, EmailAddress, EmailAttachment, EmailContent


def _patch_config(imap_name: str = "default"):
    block = ImapBlock(
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        use_ssl=True,
    )
    cfg = CourierConfig(imap_blocks={imap_name: block}, _default_imap=imap_name)
    return patch("courier.__main__.load_config", return_value=cfg)


def _make_email(
    message_id: str = "<parent@example.com>",
    in_reply_to: Optional[List[str]] = None,
    references: list = None,
) -> Email:
    return Email(
        message_id=message_id,
        subject="Test",
        from_=EmailAddress(name="Sender", address="sender@example.com"),
        to=[EmailAddress(name="Recipient", address="recipient@example.com")],
        cc=[],
        date=datetime(2026, 4, 1, 10, 0, 0),
        content=EmailContent(text="body", html=None),
        attachments=[],
        flags=["\\Seen"],
        headers={},
        folder="INBOX",
        uid=42,
        in_reply_to=in_reply_to or [],
        references=references or [],
    )


class TestReadCLIThreadingHeaders:

    def _read_inner(self, result_output: str) -> dict:
        """Extract the email dict from the chain-wrapped read output.

        Output shape: ``{op_key: {account_name: email_dict}}``.
        """
        wrapped = json.loads(result_output)
        # There is exactly one op_key, one account.
        return next(iter(next(iter(wrapped.values())).values()))

    def test_message_id_always_present(self):
        client = MagicMock()
        client.fetch_email.return_value = _make_email("<solo@example.com>")

        runner = CliRunner()
        with (
            patch("courier.__main__._make_client", return_value=client),
            _patch_config(),
        ):
            result = runner.invoke(
                app,
                ["read", "-f", "INBOX", "-u", "42"],
            )

        assert result.exit_code == 0
        out = self._read_inner(result.output)
        assert out["message_id"] == "<solo@example.com>"
        # When the parent is not itself a reply, in_reply_to/references are absent.
        assert "in_reply_to" not in out
        assert "references" not in out

    def test_in_reply_to_and_references_when_present(self):
        client = MagicMock()
        client.fetch_email.return_value = _make_email(
            "<child@example.com>",
            in_reply_to=["<root@example.com>"],
            references=["<root@example.com>", "<mid@example.com>"],
        )

        runner = CliRunner()
        with (
            patch("courier.__main__._make_client", return_value=client),
            _patch_config(),
        ):
            result = runner.invoke(
                app,
                ["read", "-f", "INBOX", "-u", "42"],
            )

        assert result.exit_code == 0
        out = self._read_inner(result.output)
        assert out["message_id"] == "<child@example.com>"
        assert out["in_reply_to"] == ["<root@example.com>"]
        assert out["references"] == ["<root@example.com>", "<mid@example.com>"]

    def test_empty_references_omitted(self):
        client = MagicMock()
        client.fetch_email.return_value = _make_email(
            "<m@example.com>",
            in_reply_to=[],
            references=[],
        )

        runner = CliRunner()
        with (
            patch("courier.__main__._make_client", return_value=client),
            _patch_config(),
        ):
            result = runner.invoke(
                app,
                ["read", "-f", "INBOX", "-u", "42"],
            )

        assert result.exit_code == 0
        out = self._read_inner(result.output)
        assert "in_reply_to" not in out
        assert "references" not in out

    def test_output_is_the_emails_to_dict(self):
        email_obj = _make_email(
            "<child@example.com>",
            in_reply_to=["<root@example.com>"],
            references=["<root@example.com>"],
        )
        email_obj.cc = [EmailAddress(name="", address="cc@example.com")]
        email_obj.attachments = [
            EmailAttachment(filename="a.pdf", content_type="application/pdf", size=3)
        ]
        client = MagicMock()
        client.fetch_email.return_value = email_obj

        runner = CliRunner()
        with (
            patch("courier.__main__._make_client", return_value=client),
            _patch_config(),
        ):
            result = runner.invoke(
                app,
                ["read", "-f", "INBOX", "-u", "42"],
            )

        assert result.exit_code == 0
        assert self._read_inner(result.output) == email_obj.to_dict()

    def test_no_cache_forwarded_to_client(self):
        client = MagicMock()
        client.fetch_email.return_value = _make_email("<solo@example.com>")

        runner = CliRunner()
        with (
            patch("courier.__main__._make_client", return_value=client),
            _patch_config(),
        ):
            result = runner.invoke(
                app,
                ["read", "-f", "INBOX", "-u", "42", "--no-cache"],
            )

        assert result.exit_code == 0
        client.fetch_email.assert_called_once_with(42, "INBOX", no_cache=True)
