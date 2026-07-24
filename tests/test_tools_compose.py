"""Tests for the compose MCP tool, CLI command, and create_mime new-email path."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import Context, FastMCP

from courier.imap_client import AppendResult
from courier.models import Email, EmailAddress, EmailContent
from courier.smtp_client import (
    compose_and_save_draft,
    create_mime,
)
from courier.tools import register_tools
from tests.conftest import patch_default_cli_config


@pytest.fixture
def from_addr():
    return EmailAddress(name="Me", address="me@example.com")


@pytest.fixture
def sample_original_email() -> Email:
    """An original email for reply-mode regression tests."""
    return Email(
        message_id="<orig@example.com>",
        subject="Hello",
        from_=EmailAddress(name="Sender", address="sender@example.com"),
        to=[EmailAddress(name="Me", address="me@example.com")],
        cc=[],
        date=datetime(2026, 1, 15, 10, 0, 0),
        content=EmailContent(text="Original body"),
        headers={},
    )


class TestCreateMimeNewEmail:
    """create_mime with original_email=None produces a fresh email."""

    def test_basic_new_email(self, from_addr):
        mime_message = create_mime(
            from_addr=from_addr,
            body="Hello there",
            to=[EmailAddress(name="R", address="r@example.com")],
            subject="Greeting",
        )
        assert mime_message["From"] == "Me <me@example.com>"
        assert mime_message["To"] == "R <r@example.com>"
        assert mime_message["Subject"] == "Greeting"
        assert mime_message["In-Reply-To"] is None
        assert mime_message["References"] is None
        # Plain text, no attachments => EmailMessage path
        assert mime_message.get_content_type() == "text/plain"
        payload = mime_message.get_payload(decode=True).decode()
        assert payload.strip() == "Hello there"

    def test_no_recipients_raises(self, from_addr):
        with pytest.raises(ValueError) as excinfo:
            create_mime(from_addr=from_addr, body="x", subject="y")
        assert "to" in str(excinfo.value).lower()

    def test_with_cc_and_bcc(self, from_addr):
        mime_message = create_mime(
            from_addr=from_addr,
            body="x",
            to=[EmailAddress(name="", address="r@example.com")],
            subject="s",
            cc=[EmailAddress(name="", address="c@example.com")],
            bcc=[EmailAddress(name="", address="b@example.com")],
        )
        assert "c@example.com" in mime_message["Cc"]
        assert "b@example.com" in mime_message["Bcc"]

    def test_with_attachment(self, from_addr, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("hi")
        mime_message = create_mime(
            from_addr=from_addr,
            body="see attached",
            to=[EmailAddress(name="", address="r@example.com")],
            subject="s",
            attachments=[str(f)],
        )
        assert mime_message.get_content_type() == "multipart/mixed"
        filenames = [
            p.get_filename() for p in mime_message.get_payload() if p.get_filename()
        ]
        assert filenames == ["note.txt"]

    def test_with_html_body(self, from_addr):
        mime_message = create_mime(
            from_addr=from_addr,
            body="plain",
            html_body="<p>rich</p>",
            to=[EmailAddress(name="", address="r@example.com")],
            subject="s",
        )
        assert mime_message.get_content_type() == "multipart/mixed"
        alt = mime_message.get_payload(0)
        assert alt.get_content_type() == "multipart/alternative"


class TestCreateMimeReplyRegression:
    """create_mime with original_email preserves prior reply behaviour."""

    def test_threading_headers_present(self, from_addr, sample_original_email):
        mime_message = create_mime(
            from_addr=from_addr,
            body="thanks",
            original_email=sample_original_email,
        )
        assert mime_message["In-Reply-To"] == "<orig@example.com>"
        assert "<orig@example.com>" in mime_message["References"]

    def test_subject_gets_re_prefix(self, from_addr, sample_original_email):
        mime_message = create_mime(
            from_addr=from_addr,
            body="thanks",
            original_email=sample_original_email,
        )
        assert mime_message["Subject"] == "Re: Hello"

    def test_original_is_quoted(self, from_addr, sample_original_email):
        mime_message = create_mime(
            from_addr=from_addr,
            body="my reply",
            original_email=sample_original_email,
        )
        payload = mime_message.get_payload(decode=True).decode()
        assert "> Original body" in payload


class TestComposeAndSaveDraft:
    """compose_and_save_draft writes a new draft via ImapClient."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.config = MagicMock()
        client.block.username = "me@example.com"
        client.save_draft_mime = MagicMock(
            return_value=AppendResult(uid=55, uidvalidity=1)
        )
        client.resolve_drafts_folder = MagicMock(return_value="Drafts")
        return client

    def test_success(self, mock_client):
        result = compose_and_save_draft(
            mock_client,
            to=["r@example.com"],
            subject="Hi",
            body="Hello",
        )
        assert result["status"] == "success"
        assert result["draft_uid"] == 55
        assert result["draft_folder"] == "Drafts"
        mock_client.save_draft_mime.assert_called_once()

    def test_empty_recipients_returns_error(self, mock_client):
        result = compose_and_save_draft(
            mock_client,
            to=[],
            subject="x",
            body="y",
        )
        assert result["status"] == "error"
        assert "recipient" in result["message"].lower()
        mock_client.save_draft_mime.assert_not_called()

    def test_save_draft_failure(self, mock_client):
        mock_client.save_draft_mime.return_value = AppendResult(
            uid=None, uidvalidity=None
        )
        result = compose_and_save_draft(
            mock_client,
            to=["r@example.com"],
            subject="s",
            body="b",
        )
        assert result["status"] == "error"
        assert "Failed to save" in result["message"]

    def test_forwards_attachments(self, mock_client, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        result = compose_and_save_draft(
            mock_client,
            to=["r@example.com"],
            subject="s",
            body="b",
            attachments=[str(f)],
        )
        assert result["status"] == "success"
        mime_msg = mock_client.save_draft_mime.call_args[0][0]
        assert mime_msg.get_content_type() == "multipart/mixed"
        filenames = [
            p.get_filename() for p in mime_msg.get_payload() if p.get_filename()
        ]
        assert "a.txt" in filenames


class TestComposeCLI:
    """CLI `courier compose`."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.block.username = "me@example.com"
        client.save_draft_mime.return_value = AppendResult(uid=77, uidvalidity=1)
        client.resolve_drafts_folder.return_value = "Drafts"
        return client

    def test_default_saves_draft(self, mock_client):
        import json

        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "compose",
                    "--to",
                    "r@example.com",
                    "--subject",
                    "Hi",
                    "--body",
                    "Hello",
                ],
            )

        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["draft_uid"] == 77

    def test_output_to_stdout_contains_subject(self, mock_client):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "compose",
                    "--to",
                    "r@example.com",
                    "--subject",
                    "Stdout",
                    "--body",
                    "Body",
                    "-o",
                    "-",
                ],
            )

        assert result.exit_code == 0
        assert "Subject: Stdout" in result.output
        assert "To: r@example.com" in result.output

    def test_output_to_file_with_attachment(self, mock_client, tmp_path):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()
        out_path = tmp_path / "msg.eml"
        attach_path = tmp_path / "doc.txt"
        attach_path.write_text("payload")

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "compose",
                    "--to",
                    "r@example.com",
                    "--subject",
                    "Attached",
                    "--body",
                    "Body",
                    "--attach",
                    str(attach_path),
                    "-o",
                    str(out_path),
                ],
            )

        assert result.exit_code == 0
        raw = out_path.read_bytes()
        assert b"Content-Disposition: attachment" in raw
        assert b"doc.txt" in raw

    def test_multiple_recipients(self, mock_client):
        from typer.testing import CliRunner

        from courier.__main__ import app

        runner = CliRunner()

        with (
            patch("courier.__main__._make_client", return_value=mock_client),
            patch_default_cli_config(),
        ):
            result = runner.invoke(
                app,
                [
                    "--config",
                    "dummy.toml",
                    "compose",
                    "--to",
                    "a@example.com",
                    "--to",
                    "b@example.com",
                    "--subject",
                    "M",
                    "--body",
                    "B",
                    "-o",
                    "-",
                ],
            )

        assert result.exit_code == 0
        assert "a@example.com" in result.output
        assert "b@example.com" in result.output


class TestComposeMCP:
    """MCP compose tool forwards to compose_and_save_draft."""

    @pytest.mark.asyncio
    async def test_forwards_to_compose_and_save_draft(self, tmp_path):
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
        compose_tool = stored["compose"]

        ctx = MagicMock(spec=Context)
        f = tmp_path / "a.txt"
        f.write_text("x")

        with patch("courier.tools.get_client_from_context", return_value=imap_client):
            with patch("courier.smtp_client.compose_and_save_draft") as mock_compose:
                mock_compose.return_value = {"status": "success", "draft_uid": 1}
                await compose_tool(
                    to=["r@example.com"],
                    body="hi",
                    ctx=ctx,
                    subject="S",
                    attachments=[str(f)],
                )

        kwargs = mock_compose.call_args.kwargs
        positional = mock_compose.call_args.args
        # Signature: compose_and_save_draft(client, to, subject, body, cc=, bcc=, ...)
        assert positional[1] == ["r@example.com"]
        assert positional[2] == "S"
        assert positional[3] == "hi"
        assert kwargs["attachments"] == [str(f)]

    @pytest.mark.asyncio
    async def test_date_string_is_parsed_before_forwarding(self):
        """The MCP tool takes ISO 8601 text and hands down a datetime."""
        from datetime import timedelta, timezone

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
        compose_tool = stored["compose"]
        ctx = MagicMock(spec=Context)

        with patch("courier.tools.get_client_from_context", return_value=imap_client):
            with patch("courier.smtp_client.compose_and_save_draft") as mock_compose:
                mock_compose.return_value = {"status": "success", "draft_uid": 1}
                await compose_tool(
                    to=["r@example.com"],
                    body="hi",
                    ctx=ctx,
                    date="2019-03-04T05:06:07+10:00",
                )

        assert mock_compose.call_args.kwargs["date"] == datetime(
            2019, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=10))
        )
