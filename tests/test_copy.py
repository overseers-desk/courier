"""Tests for cross-account email copy: fetch_raw, append_raw, and copy orchestration."""

from datetime import datetime
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import Context, FastMCP

from courier.config import ImapBlock
from courier.errors import PermanentError
from courier.imap_client import (
    AppendResult,
    ImapClient,
    copy_email_between_imap_blocks,
)
from courier.tools import register_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG = ImapBlock(
    host="imap.example.com",
    port=993,
    username="test@example.com",
    password="password",
    use_ssl=True,
)


def _make_raw_message(subject: str = "Test Subject") -> bytes:
    """Build minimal RFC 822 bytes with a known Subject header."""
    msg = MIMEText("body")
    msg["Subject"] = subject
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    return msg.as_bytes()


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
# A. fetch_raw
# ---------------------------------------------------------------------------


class TestFetchRaw:
    """Unit tests for ImapClient.fetch_raw."""

    def test_fetch_raw_success(self, mock_imap_client):
        raw_bytes = _make_raw_message("Hello")
        dt = datetime(2026, 3, 1, 12, 0, 0)
        mock_imap_client.fetch.return_value = {
            42: {
                b"BODY[]": raw_bytes,
                b"FLAGS": (b"\\Seen",),
                b"INTERNALDATE": dt,
            }
        }
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.fetch_raw(42, "INBOX")

        mock_imap_client.select_folder.assert_called_with("INBOX", readonly=True)
        assert result is not None
        assert result["raw"] == raw_bytes
        assert result["flags"] == (b"\\Seen",)
        assert result["date"] == dt
        assert result["subject"] == "Hello"

    def test_fetch_raw_not_found_empty(self, mock_imap_client):
        mock_imap_client.fetch.return_value = {}
        mock_imap_client.select_folder.return_value = {b"EXISTS": 0}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.fetch_raw(42, "INBOX")

        assert result is None

    def test_fetch_raw_uid_mismatch(self, mock_imap_client):
        mock_imap_client.fetch.return_value = {
            99: {b"BODY[]": b"x", b"FLAGS": (), b"INTERNALDATE": None}
        }
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.fetch_raw(42, "INBOX")

        assert result is None

    def test_fetch_raw_extracts_subject(self, mock_imap_client):
        raw_bytes = _make_raw_message("Quarterly Report")
        mock_imap_client.fetch.return_value = {
            1: {b"BODY[]": raw_bytes, b"FLAGS": (), b"INTERNALDATE": None}
        }
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.fetch_raw(1, "INBOX")

        assert result["subject"] == "Quarterly Report"


# ---------------------------------------------------------------------------
# B. append_raw
# ---------------------------------------------------------------------------


class TestAppendRaw:
    """Unit tests for ImapClient.append_raw."""

    def test_append_raw_with_appenduid(self, mock_imap_client):
        mock_imap_client.append.return_value = b"[APPENDUID 1234 5678] Success"
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.append_raw("Archive", b"raw message bytes")

        # Both APPENDUID groups are parsed: UIDVALIDITY first, UID second
        assert result == AppendResult(uid=5678, uidvalidity=1234)

    def test_append_raw_no_appenduid(self, mock_imap_client):
        mock_imap_client.append.return_value = b"OK"
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            result = client.append_raw("Archive", b"raw message bytes")

        assert result == AppendResult(uid=None, uidvalidity=None)

    def test_append_raw_passes_flags_and_time(self, mock_imap_client):
        mock_imap_client.append.return_value = b"OK"
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}
        dt = datetime(2026, 1, 1, 0, 0, 0)
        flags = ("\\Seen", "\\Flagged")

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            client.append_raw("Archive", b"raw", flags=flags, msg_time=dt)

        mock_imap_client.append.assert_called_once_with(
            "Archive", b"raw", flags=flags, msg_time=dt
        )

    def test_append_raw_propagates_exception(self, mock_imap_client):
        mock_imap_client.append.side_effect = Exception("server error")
        mock_imap_client.select_folder.return_value = {b"EXISTS": 1}

        client = ImapClient(_CONFIG)
        with patch("imapclient.IMAPClient") as cls:
            cls.return_value = mock_imap_client
            client.connect()
            with pytest.raises(Exception, match="server error"):
                client.append_raw("Archive", b"raw")


# ---------------------------------------------------------------------------
# C. copy_email_between_imap_blocks (shared orchestration function)
# ---------------------------------------------------------------------------


class TestCopyEmailBetweenAccounts:
    """Unit tests for the shared copy_email_between_imap_blocks function."""

    @pytest.fixture
    def source(self):
        client = MagicMock(spec=ImapClient)
        client.fetch_raw.return_value = {
            "raw": _make_raw_message("Test"),
            "flags": (b"\\Seen",),
            "date": datetime(2026, 3, 1, 12, 0, 0),
            "subject": "Test",
        }
        return client

    @pytest.fixture
    def dest(self):
        client = MagicMock(spec=ImapClient)
        client.append_raw.return_value = AppendResult(uid=999, uidvalidity=1)
        return client

    def test_copy_success(self, source, dest):
        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX")

        assert result["success"] is True
        assert result["subject"] == "Test"
        assert result["new_uid"] == 999
        assert result["moved"] is False
        assert result["error"] is None
        source.delete_email.assert_not_called()

    def test_copy_with_move(self, source, dest):
        source.delete_email.return_value = {
            "matched_uids": [42],
            "not_found_uids": [],
        }
        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX", move=True)

        assert result["success"] is True
        assert result["moved"] is True
        source.delete_email.assert_called_once_with(42, "INBOX")

    def test_copy_with_move_raced_source_delete_is_not_moved(self, source, dest):
        """A UID gone before the delete no-ops silently; moved must not lie."""
        source.delete_email.return_value = {
            "matched_uids": [],
            "not_found_uids": [42],
        }
        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX", move=True)

        assert result["success"] is True
        assert result["moved"] is False
        assert "was gone from the source" in result["error"]

    def test_copy_with_move_source_delete_failure_is_reported(self, source, dest):
        """A failed source-delete after a successful append must not propagate."""
        source.delete_email.side_effect = PermanentError("EXPUNGE failed: NO")

        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX", move=True)

        assert result["success"] is True
        assert result["new_uid"] == 999
        assert result["moved"] is False
        assert "failed to delete source" in result["error"]

    def test_copy_preserve_flags_filters_recent(self, source, dest):
        source.fetch_raw.return_value["flags"] = (
            b"\\Seen",
            b"\\Recent",
            b"\\Flagged",
        )
        copy_email_between_imap_blocks(source, dest, 42, "INBOX", preserve_flags=True)

        call_kwargs = dest.append_raw.call_args
        assert call_kwargs.kwargs["flags"] == ("\\Seen", "\\Flagged")

    def test_copy_no_preserve_flags(self, source, dest):
        copy_email_between_imap_blocks(source, dest, 42, "INBOX", preserve_flags=False)

        call_kwargs = dest.append_raw.call_args
        assert call_kwargs.kwargs["flags"] == ()

    def test_copy_source_not_found(self, source, dest):
        source.fetch_raw.return_value = None

        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX")

        assert result["success"] is False
        assert "not found" in result["error"]
        dest.append_raw.assert_not_called()

    def test_copy_append_failure_propagates(self, source, dest):
        dest.append_raw.side_effect = Exception("append failed")

        with pytest.raises(Exception, match="append failed"):
            copy_email_between_imap_blocks(source, dest, 42, "INBOX")

    def test_copy_preserves_date(self, source, dest):
        dt = datetime(2026, 6, 15, 8, 30, 0)
        source.fetch_raw.return_value["date"] = dt

        copy_email_between_imap_blocks(source, dest, 42, "INBOX")

        call_kwargs = dest.append_raw.call_args
        assert call_kwargs.kwargs["msg_time"] == dt

    def test_copy_filters_recent_str_form(self, source, dest):
        source.fetch_raw.return_value["flags"] = ("\\Recent", "\\Seen")

        copy_email_between_imap_blocks(source, dest, 42, "INBOX", preserve_flags=True)

        call_kwargs = dest.append_raw.call_args
        assert call_kwargs.kwargs["flags"] == ("\\Seen",)

    def _redact_source(self, source):
        """Make the source serve fetch_raw's redacted shape (issue #61)."""
        source.fetch_raw.return_value = {
            "raw": b"",
            "flags": (b"\\Seen",),
            "date": datetime(2026, 3, 1, 12, 0, 0),
            "subject": "[redacted by rule private]",
            "redacted_by": "private",
        }
        return source

    def test_copy_refuses_redacted_source_before_append(self, source, dest):
        """A policy-withheld message must never be APPENDed as an empty
        payload and reported as a success (issue #61)."""
        self._redact_source(source)

        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX")

        assert result["success"] is False
        assert "redact" in result["error"]
        assert "private" in result["error"]
        dest.append_raw.assert_not_called()

    def test_copy_with_move_never_deletes_redacted_source(self, source, dest):
        """move=True on a redacted source must not expunge the only
        real copy: the refusal happens before any destructive leg."""
        self._redact_source(source)

        result = copy_email_between_imap_blocks(source, dest, 42, "INBOX", move=True)

        assert result["success"] is False
        assert result["moved"] is False
        dest.append_raw.assert_not_called()
        source.delete_email.assert_not_called()


# ---------------------------------------------------------------------------
# D. MCP copy tool
# ---------------------------------------------------------------------------


class TestCopyTool:
    """Tests for the MCP copy tool wrapper."""

    @pytest.fixture
    def setup(self):
        stored, _ = _register_and_extract_tools()
        ctx = MagicMock(spec=Context)
        return stored, ctx

    @pytest.mark.asyncio
    async def test_copy_tool_success(self, setup):
        stored, ctx = setup
        copy_fn = stored["copy"]

        success_result = {
            "success": True,
            "subject": "Hello",
            "new_uid": 100,
            "moved": False,
            "error": None,
        }

        with (
            patch("courier.tools.get_client_from_context") as gc,
            patch(
                "courier.imap_client.copy_email_between_imap_blocks",
                return_value=success_result,
            ),
        ):
            source_mock = MagicMock()
            dest_mock = MagicMock()
            gc.side_effect = lambda ctx, acct=None: (
                source_mock if acct == "src" else dest_mock
            )

            result = await copy_fn("src", "INBOX", 42, ctx)

        assert "Hello" in result
        assert "100" in result

    @pytest.mark.asyncio
    async def test_copy_tool_with_move(self, setup):
        stored, ctx = setup
        copy_fn = stored["copy"]

        move_result = {
            "success": True,
            "subject": "Moved",
            "new_uid": 200,
            "moved": True,
            "error": None,
        }

        with (
            patch("courier.tools.get_client_from_context") as gc,
            patch(
                "courier.imap_client.copy_email_between_imap_blocks",
                return_value=move_result,
            ),
        ):
            gc.return_value = MagicMock()
            result = await copy_fn("src", "INBOX", 42, ctx, move=True)

        assert "removed from source" in result.lower() or "Moved" in result

    @pytest.mark.asyncio
    async def test_copy_tool_not_found(self, setup):
        stored, ctx = setup
        copy_fn = stored["copy"]

        fail_result = {
            "success": False,
            "subject": "",
            "new_uid": None,
            "moved": False,
            "error": "UID 42 not found in INBOX",
        }

        with (
            patch("courier.tools.get_client_from_context") as gc,
            patch(
                "courier.imap_client.copy_email_between_imap_blocks",
                return_value=fail_result,
            ),
        ):
            gc.return_value = MagicMock()
            result = await copy_fn("src", "INBOX", 42, ctx)

        assert "Error" in result

    @pytest.mark.asyncio
    async def test_copy_tool_exception(self, setup):
        stored, ctx = setup
        copy_fn = stored["copy"]

        with (
            patch("courier.tools.get_client_from_context") as gc,
            patch(
                "courier.imap_client.copy_email_between_imap_blocks",
                side_effect=Exception("connection lost"),
            ),
        ):
            gc.return_value = MagicMock()
            result = await copy_fn("src", "INBOX", 42, ctx)

        assert "Error" in result
