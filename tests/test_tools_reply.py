"""Tests for the reply MCP tool and CLI command."""

import json
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import Context, FastMCP

from courier.imap_client import AppendResult
from courier.models import Email, EmailAddress, EmailContent
from courier.tools import register_tools
from tests.conftest import patch_default_cli_config

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_email():
    return Email(
        message_id="<test123@example.com>",
        subject="Test Email",
        from_=EmailAddress(name="Sender", address="sender@example.com"),
        to=[EmailAddress(name="Recipient", address="recipient@example.com")],
        cc=[],
        date=datetime(2026, 1, 15, 10, 0, 0),
        content=EmailContent(text="Original body", html=None),
        attachments=[],
        flags=["\\Seen"],
        headers={"References": "<earlier@example.com>"},
        folder="INBOX",
        uid=42,
    )


def _register_and_extract_tools():
    """Register tools against a mock MCP and return the captured functions."""
    mcp = MagicMock(spec=FastMCP)
    stored = {}

    def mock_tool_decorator(**kwargs):
        def decorator(func):
            key = kwargs.get("name", func.__name__)
            stored[key] = func
            return func

        return decorator

    mcp.tool = mock_tool_decorator
    imap_client = MagicMock()

    register_tools(mcp, imap_client)
    return stored, imap_client


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestDraftReplyTool:

    @pytest.fixture
    def tools(self):
        return _register_and_extract_tools()

    @pytest.fixture
    def ctx(self):
        return MagicMock(spec=Context)

    @pytest.mark.asyncio
    async def test_success_plain_text(self, tools, mock_email, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = mock_email
            imap_client.save_draft_mime.return_value = AppendResult(
                uid=100, uidvalidity=1
            )
            imap_client.resolve_drafts_folder.return_value = "Drafts"

            with patch("courier.smtp_client.create_mime") as mock_create:
                mime_msg = MagicMock()
                mock_create.return_value = mime_msg

                result = await draft_reply(
                    folder="INBOX",
                    uid=42,
                    reply_body="Thanks!",
                    ctx=ctx,
                )

        assert result["status"] == "success"
        assert result["draft_uid"] == 100
        assert result["draft_folder"] == "Drafts"
        imap_client.fetch_email.assert_called_once_with(42, folder="INBOX")
        mock_create.assert_called_once()
        imap_client.save_draft_mime.assert_called_once_with(mime_msg)

    @pytest.mark.asyncio
    async def test_reply_all_with_cc(self, tools, mock_email, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = mock_email
            imap_client.save_draft_mime.return_value = AppendResult(
                uid=101, uidvalidity=1
            )
            imap_client.resolve_drafts_folder.return_value = "Drafts"

            with patch("courier.smtp_client.create_mime") as mock_create:
                mock_create.return_value = MagicMock()

                result = await draft_reply(
                    folder="INBOX",
                    uid=42,
                    reply_body="Noted",
                    ctx=ctx,
                    reply_all=True,
                    cc=["extra@example.com"],
                )

        assert result["status"] == "success"
        # Verify create_mime received reply_all=True and cc as EmailAddress list
        kw = mock_create.call_args
        assert kw.kwargs["reply_all"] is True
        assert len(kw.kwargs["cc"]) == 1
        assert kw.kwargs["cc"][0].address == "extra@example.com"

    @pytest.mark.asyncio
    async def test_html_body(self, tools, mock_email, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = mock_email
            imap_client.save_draft_mime.return_value = AppendResult(
                uid=102, uidvalidity=1
            )
            imap_client.resolve_drafts_folder.return_value = "Drafts"

            with patch("courier.smtp_client.create_mime") as mock_create:
                mock_create.return_value = MagicMock()

                result = await draft_reply(
                    folder="INBOX",
                    uid=42,
                    reply_body="plain",
                    ctx=ctx,
                    body_html="<p>rich</p>",
                )

        assert result["status"] == "success"
        assert mock_create.call_args.kwargs["html_body"] == "<p>rich</p>"

    @pytest.mark.asyncio
    async def test_bcc_header_added(self, tools, mock_email, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]
        imap_client.block.username = "recipient@example.com"

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = mock_email
            imap_client.save_draft_mime.return_value = AppendResult(
                uid=103, uidvalidity=1
            )
            imap_client.resolve_drafts_folder.return_value = "Drafts"

            result = await draft_reply(
                folder="INBOX",
                uid=42,
                reply_body="Thanks",
                ctx=ctx,
                bcc=["copy@example.com"],
            )

        assert result["status"] == "success"
        saved_msg = imap_client.save_draft_mime.call_args[0][0]
        assert "copy@example.com" in saved_msg["Bcc"]

    @pytest.mark.asyncio
    async def test_email_not_found(self, tools, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = None

            result = await draft_reply(
                folder="INBOX",
                uid=999,
                reply_body="Hi",
                ctx=ctx,
            )

        assert result["status"] == "error"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_save_draft_failure(self, tools, mock_email, ctx):
        stored, imap_client = tools
        draft_reply = stored["reply"]

        with patch("courier.tools.get_client_from_context") as gc:
            gc.return_value = imap_client
            imap_client.fetch_email.return_value = mock_email
            imap_client.save_draft_mime.return_value = AppendResult(
                uid=None, uidvalidity=None
            )

            with patch("courier.smtp_client.create_mime") as mock_create:
                mock_create.return_value = MagicMock()

                result = await draft_reply(
                    folder="INBOX",
                    uid=42,
                    reply_body="Hi",
                    ctx=ctx,
                )

        assert result["status"] == "error"
        assert "failed to save" in result["message"].lower()


# ---------------------------------------------------------------------------
# CLI reply tests
# ---------------------------------------------------------------------------


class TestDraftReplyCLI:

    @pytest.fixture
    def mock_client(self, mock_email):
        client = MagicMock()
        client.fetch_email.return_value = mock_email
        client.block.username = "recipient@example.com"
        client.save_draft_mime.return_value = AppendResult(uid=200, uidvalidity=1)
        client.resolve_drafts_folder.return_value = "Drafts"
        return client

    def test_default_saves_draft(self, mock_client, mock_email, capsys):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            with patch("courier.smtp_client.create_mime") as mock_create:
                mock_create.return_value = MagicMock()
                result = runner.invoke(
                    app,
                    [
                        "--config",
                        "dummy.toml",
                        "reply",
                        "-f",
                        "INBOX",
                        "--uid",
                        "42",
                        "--body",
                        "Thanks",
                    ],
                )

        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["draft_uid"] == 200

    def test_output_to_file(self, mock_client, mock_email, tmp_path):
        from email.message import EmailMessage

        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        out_path = str(tmp_path / "reply.eml")

        mime_msg = EmailMessage()
        mime_msg.set_content("Hello")
        mime_msg["Subject"] = "Re: Test"

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            with patch("courier.smtp_client.create_mime", return_value=mime_msg):
                result = runner.invoke(
                    app,
                    [
                        "--config",
                        "dummy.toml",
                        "reply",
                        "-f",
                        "INBOX",
                        "--uid",
                        "42",
                        "--body",
                        "Hello",
                        "-o",
                        out_path,
                    ],
                )

        assert result.exit_code == 0
        assert os.path.exists(out_path)
        raw = open(out_path, "rb").read()
        assert b"Re: Test" in raw

    def test_output_to_stdout(self, mock_client, mock_email):
        from email.message import EmailMessage

        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()

        mime_msg = EmailMessage()
        mime_msg.set_content("Stdout body")
        mime_msg["Subject"] = "Re: Stdout"

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            with patch("courier.smtp_client.create_mime", return_value=mime_msg):
                result = runner.invoke(
                    app,
                    [
                        "--config",
                        "dummy.toml",
                        "reply",
                        "-f",
                        "INBOX",
                        "--uid",
                        "42",
                        "--body",
                        "Stdout body",
                        "-o",
                        "-",
                    ],
                )

        assert result.exit_code == 0
        # The raw RFC 822 message should appear on stdout
        assert "Re: Stdout" in result.output

    def test_bcc_in_raw_output(self, mock_client, mock_email, tmp_path):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        out_path = str(tmp_path / "reply_bcc.eml")

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "reply",
                    "-f",
                    "INBOX",
                    "--uid",
                    "42",
                    "--body",
                    "Body",
                    "--bcc",
                    "copy@example.com",
                    "-o",
                    out_path,
                ],
            )

        assert result.exit_code == 0
        raw = open(out_path, "rb").read()
        assert b"Bcc: copy@example.com" in raw

    def test_email_not_found(self):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        client = MagicMock()
        client.fetch_email.return_value = None

        with (
            patch("courier.__main__._make_client", return_value=client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "reply",
                    "-f",
                    "INBOX",
                    "--uid",
                    "999",
                    "--body",
                    "Hi",
                ],
            )

        assert result.exit_code != 0

    def test_attach_forwarded_to_draft_path(self, mock_client, mock_email, tmp_path):
        """CLI --attach plumbs through to create_mime in draft branch."""
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        f = tmp_path / "file.txt"
        f.write_text("x")

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            with patch("courier.smtp_client.create_mime") as mock_create:
                mock_create.return_value = MagicMock()
                result = runner.invoke(
                    app,
                    [
                        "--config",
                        "dummy.toml",
                        "reply",
                        "-f",
                        "INBOX",
                        "--uid",
                        "42",
                        "--body",
                        "See attached",
                        "--attach",
                        str(f),
                    ],
                )

        assert result.exit_code == 0
        assert mock_create.call_args.kwargs["attachments"] == [str(f)]

    def test_attach_in_output_branch_produces_attachment_part(
        self, mock_client, mock_email, tmp_path
    ):
        """--attach with --output emits a real MIME part into stdout."""
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        f = tmp_path / "doc.txt"
        f.write_text("hello")

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "reply",
                    "-f",
                    "INBOX",
                    "--uid",
                    "42",
                    "--body",
                    "Body",
                    "--attach",
                    str(f),
                    "-o",
                    "-",
                ],
            )

        assert result.exit_code == 0
        assert "Content-Disposition" in result.output
        assert 'filename="doc.txt"' in result.output or "doc.txt" in result.output


class TestMcpReplyAttachments:
    """MCP reply tool forwards attachments through to compose_and_save_reply_draft."""

    @pytest.mark.asyncio
    async def test_attachments_forwarded(self, mock_email, tmp_path):
        mcp = MagicMock(spec=FastMCP)
        stored = {}

        def mock_tool_decorator(**kwargs):
            def decorator(func):
                stored[kwargs.get("name", func.__name__)] = func
                return func

            return decorator

        mcp.tool = mock_tool_decorator
        imap_client = MagicMock()
        register_tools(mcp, imap_client)
        draft_reply = stored["reply"]

        ctx = MagicMock(spec=Context)
        f = tmp_path / "a.txt"
        f.write_text("hi")

        with patch("courier.tools.get_client_from_context", return_value=imap_client):
            with patch(
                "courier.smtp_client.compose_and_save_reply_draft"
            ) as mock_compose:
                mock_compose.return_value = {"status": "success", "draft_uid": 1}
                await draft_reply(
                    folder="INBOX",
                    uid=42,
                    reply_body="hi",
                    ctx=ctx,
                    attachments=[str(f)],
                )

        assert mock_compose.call_args.kwargs["attachments"] == [str(f)]
