"""Tests for MCP tools implementation."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import Context, FastMCP

from courier.errors import CourierError, PermanentError, TransientError
from courier.imap_client import ImapClient
from courier.models import Email, EmailAddress, EmailContent
from courier.query import parse
from courier.query.emit_imap import emit as emit_imap
from courier.tools import register_tools


def _mutation_ok(uid) -> dict:
    """The mutation outcome when every requested UID was present."""
    uids = uid if isinstance(uid, list) else [uid]
    return {"matched_uids": list(uids), "not_found_uids": []}


# Patch the get_client_from_context function to use our mock client
@pytest.fixture(autouse=True)
def patch_get_client():
    with patch("courier.tools.get_client_from_context") as mock_get_client:
        yield mock_get_client


class TestTools:
    """Test class for MCP tools."""

    @pytest.fixture
    def mock_email(self):
        """Create a mock email object."""
        email = Email(
            message_id="<test123@example.com>",
            subject="Test Email",
            from_=EmailAddress(name="Sender", address="sender@example.com"),
            to=[EmailAddress(name="Recipient", address="recipient@example.com")],
            cc=[],
            bcc=[],
            date=datetime.now(),
            content=EmailContent(text="Test content", html="<p>Test content</p>"),
            attachments=[],
            flags=["\\Seen"],
            headers={},
            folder="INBOX",
            uid=1,
        )
        return email

    @pytest.fixture
    def mock_client(self, mock_email):
        """Create a mock IMAP client."""
        client = MagicMock(spec=ImapClient)
        # Configure default return values (mutations echo the requested
        # UIDs as matched — the issue #63 contract — and raise on failure)
        client.move_email.side_effect = lambda uid, src, dst: _mutation_ok(uid)
        client.mark_email.side_effect = (
            lambda uid, folder, flag, value=True: _mutation_ok(uid)
        )
        client.delete_email.side_effect = lambda uid, folder: _mutation_ok(uid)
        client.trash_email.side_effect = lambda uid, folder: {
            "trash_folder": "Trash",
            **_mutation_ok(uid),
        }
        client.list_folders.return_value = ["INBOX", "Sent", "Archive", "Trash"]
        client.folders_result.return_value = ["INBOX", "Sent", "Archive", "Trash"]
        client.search.return_value = [1, 2, 3]
        client.fetch_emails.return_value = {1: mock_email, 2: mock_email, 3: mock_email}
        client.fetch_email.return_value = mock_email
        return client

    @pytest.fixture
    def tools(self, mock_client):
        """Set up tools for testing."""
        # Create a mock MCP server
        mcp = MagicMock(spec=FastMCP)

        # Make tool decorator store and return the decorated function
        stored_tools = {}

        def mock_tool_decorator(**kwargs):
            def decorator(func):
                stored_tools[func.__name__] = func
                return func

            return decorator

        mcp.tool = mock_tool_decorator

        # Register tools with our mock
        register_tools(mcp, mock_client)

        # Return the tools dictionary
        return stored_tools

    @pytest.fixture
    def mock_context(self, mock_client, patch_get_client):
        """Create a mock context and configure get_client_from_context."""
        context = MagicMock(spec=Context)
        patch_get_client.return_value = mock_client
        return context

    @pytest.mark.asyncio
    async def test_move(self, tools, mock_client, mock_context):
        """Test moving an email from one folder to another."""
        # Get the move function
        move = tools["move"]

        # Call the move function
        result = await move("INBOX", 123, "Archive", mock_context)

        # Check the client was called correctly
        mock_client.move_email.assert_called_once_with(123, "INBOX", "Archive")

        # Check the result
        assert "Email moved from INBOX to Archive" in result

        # Test error handling
        mock_client.move_email.side_effect = Exception("Connection error")
        result = await move("INBOX", 123, "Archive", mock_context)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_mark_read(self, tools, mock_client, mock_context):
        """Test marking an email as read."""
        # Get the mark_read function
        mark_read = tools["mark_read"]

        # Call the function
        result = await mark_read("INBOX", 123, mock_context)

        # Check the client was called correctly
        mock_client.mark_email.assert_called_once_with(123, "INBOX", "\\Seen", True)

        # Check the result
        assert "Email marked as read" in result

        # Test failure case: typed errors map to the failure message
        mock_client.mark_email.side_effect = CourierError("NO STORE failed")
        result = await mark_read("INBOX", 123, mock_context)
        assert "Failed to mark email as read" in result
        mock_client.mark_email.side_effect = None

    @pytest.mark.asyncio
    async def test_mark_unread(self, tools, mock_client, mock_context):
        """Test marking an email as unread."""
        # Get the mark_unread function
        mark_unread = tools["mark_unread"]

        # Reset mock for this test
        mock_client.mark_email.reset_mock()
        mock_client.mark_email.side_effect = None
        mock_client.mark_email.return_value = _mutation_ok(123)

        # Call the function
        result = await mark_unread("INBOX", 123, mock_context)

        # Check the client was called correctly
        mock_client.mark_email.assert_called_once_with(123, "INBOX", "\\Seen", False)

        # Check the result
        assert "Email marked as unread" in result

        # Test error handling
        mock_client.mark_email.side_effect = Exception("Server error")
        result = await mark_unread("INBOX", 123, mock_context)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_flag(self, tools, mock_client, mock_context):
        """Test flagging and unflagging an email."""
        # Get the flag function
        flag = tools["flag"]

        # Reset mock for this test
        mock_client.mark_email.reset_mock()
        mock_client.mark_email.side_effect = None
        mock_client.mark_email.return_value = _mutation_ok(123)

        # Test flagging
        result = await flag("INBOX", 123, mock_context, True)
        mock_client.mark_email.assert_called_once_with(123, "INBOX", "\\Flagged", True)
        assert "Email flagged" in result

        # Reset mock
        mock_client.mark_email.reset_mock()

        # Test unflagging
        result = await flag("INBOX", 123, mock_context, False)
        mock_client.mark_email.assert_called_once_with(123, "INBOX", "\\Flagged", False)
        assert "Email unflagged" in result

    @pytest.mark.asyncio
    async def test_delete(self, tools, mock_client, mock_context):
        """Test deleting an email."""
        # Get the delete function
        delete = tools["delete"]

        # Call the function
        result = await delete("INBOX", 123, mock_context)

        # Check the client was called correctly
        mock_client.delete_email.assert_called_once_with(123, "INBOX")

        # Check the result
        assert "Email deleted" in result

        # Test failure case: typed errors map to the failure message
        mock_client.delete_email.side_effect = CourierError("NO EXPUNGE failed")
        result = await delete("INBOX", 123, mock_context)
        assert "Failed to delete" in result

        # Test error handling
        mock_client.delete_email.side_effect = Exception("Permission denied")
        result = await delete("INBOX", 123, mock_context)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_mutation_tools_name_a_not_found_uid(
        self, tools, mock_client, mock_context
    ):
        """A UID the server no longer has must not answer with the
        success string (issue #63): the text names the UID and says
        nothing happened."""
        missing = {"matched_uids": [], "not_found_uids": [123]}
        mock_client.move_email.side_effect = None
        mock_client.move_email.return_value = missing
        mock_client.mark_email.side_effect = None
        mock_client.mark_email.return_value = missing
        mock_client.delete_email.side_effect = None
        mock_client.delete_email.return_value = missing
        mock_client.trash_email.side_effect = None
        mock_client.trash_email.return_value = {"trash_folder": "Trash", **missing}

        for name in ("move", "mark_read", "mark_unread", "flag", "trash", "delete"):
            args = (
                ("INBOX", 123, "Archive", mock_context)
                if name == "move"
                else ("INBOX", 123, mock_context)
            )
            result = await tools[name](*args)
            assert "UID 123 not found in INBOX" in result, name
            assert "Email" not in result, name

    @pytest.mark.asyncio
    async def test_mutation_failure_text_carries_reason_and_class(
        self, tools, mock_client, mock_context
    ):
        """The failure string carries the server's stated reason and
        the retryable/permanent marker (issue #63 comment): a caller
        must be able to tell a missing target folder from a dropped
        socket."""
        mock_client.move_email.side_effect = PermanentError(
            "[TRYCREATE] no such mailbox Recibos"
        )
        result = await tools["move"]("INBOX", 123, "Recibos", mock_context)
        assert result == (
            "Failed to move email (permanent): [TRYCREATE] no such mailbox Recibos"
        )

        mock_client.mark_email.side_effect = TransientError("connection reset")
        result = await tools["mark_read"]("INBOX", 123, mock_context)
        assert result == ("Failed to mark email as read (transient): connection reset")

    @pytest.mark.asyncio
    async def test_search(self, tools, mock_client, mock_context, mock_email):
        """Test searching for emails via the MCP tool wrapper."""
        search = tools["search"]

        # Configure client.search_emails to return wrapped sample results
        sample_results = {
            "results": [
                {
                    "uid": 1,
                    "folder": "INBOX",
                    "from": "sender@example.com",
                    "to": ["recipient@example.com"],
                    "subject": "Test Email",
                    "date": "2025-04-01T10:00:00",
                    "flags": ["\\Seen"],
                    "has_attachments": False,
                },
            ],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": None,
            },
        }
        mock_client.search_emails.return_value = sample_results

        # Test default parameters — bare words become TEXT search
        result = await search("test query", mock_context)
        result_data = json.loads(result)
        assert isinstance(result_data, dict)
        assert "results" in result_data
        assert "provenance" in result_data
        assert len(result_data["results"]) == 1
        assert result_data["results"][0]["subject"] == "Test Email"
        assert result_data["provenance"]["source"] == "remote"
        mock_client.search_emails.assert_called_once_with(
            "test query",
            folder=None,
            limit=50,
            no_cache=False,
        )

        # Test with specific folder and Gmail-style query
        mock_client.search_emails.reset_mock()
        result = await search("from:sender@example.com", mock_context, folder="INBOX")
        mock_client.search_emails.assert_called_once_with(
            "from:sender@example.com",
            folder="INBOX",
            limit=50,
            no_cache=False,
        )

        # Test with invalid query — client.search_emails raises ValueError
        mock_client.search_emails.reset_mock()
        mock_client.search_emails.side_effect = ValueError(
            "Unknown is: keyword: 'bogus'"
        )
        result = await search("is:bogus", mock_context)
        assert "Unknown is: keyword" in result
        mock_client.search_emails.side_effect = None

        # Test numeric query is coerced to string
        mock_client.search_emails.reset_mock()
        mock_client.search_emails.return_value = sample_results
        result = await search(69172700, mock_context, folder="INBOX")
        mock_client.search_emails.assert_called_once_with(
            "69172700",
            folder="INBOX",
            limit=50,
            no_cache=False,
        )

    @pytest.mark.asyncio
    async def test_search_raw_imap(self, tools, mock_client, mock_context, mock_email):
        """Test searching with raw IMAP via imap: prefix delegates to client.search_emails."""
        search = tools["search"]

        sample_results = {
            "results": [
                {
                    "uid": 1,
                    "folder": "INBOX",
                    "from": "sender@example.com",
                    "to": ["recipient@example.com"],
                    "subject": "Edinburgh trip",
                    "date": "2025-04-01T10:00:00",
                    "flags": [],
                    "has_attachments": False,
                },
            ],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": None,
            },
        }
        mock_client.search_emails.return_value = sample_results

        result = await search("imap:TEXT Edinburgh", mock_context, folder="INBOX")
        result_data = json.loads(result)
        assert isinstance(result_data, dict)
        assert "results" in result_data
        mock_client.search_emails.assert_called_once_with(
            "imap:TEXT Edinburgh",
            folder="INBOX",
            limit=50,
            no_cache=False,
        )

    @pytest.mark.asyncio
    async def test_triage(self, tools, mock_client, mock_context):
        """Test processing an email with multiple actions."""
        triage = tools["triage"]

        # Test move action — delegates to process_email_action
        mock_client.process_email_action.return_value = (
            "Email moved from INBOX to Archive"
        )
        result = await triage(
            "INBOX", 123, "move", mock_context, target_folder="Archive"
        )
        mock_client.process_email_action.assert_called_with(
            123, "INBOX", "move", target_folder="Archive"
        )
        assert "Email moved" in result

        # Test read action
        mock_client.process_email_action.return_value = "Email marked as read"
        result = await triage("INBOX", 123, "read", mock_context)
        mock_client.process_email_action.assert_called_with(
            123, "INBOX", "read", target_folder=None
        )
        assert "Email marked as read" in result

        # Test move without target folder — ValueError from domain
        mock_client.process_email_action.side_effect = ValueError(
            "target_folder is required for move action"
        )
        result = await triage("INBOX", 123, "move", mock_context)
        assert "target_folder" in result
        mock_client.process_email_action.side_effect = None

        # Test invalid action — ValueError from domain
        mock_client.process_email_action.side_effect = ValueError(
            "Unknown action 'invalid_action'"
        )
        result = await triage("INBOX", 123, "invalid_action", mock_context)
        assert "Unknown action" in result
        mock_client.process_email_action.side_effect = None

        # Test email not found
        mock_client.fetch_email.return_value = None
        result = await triage("INBOX", 123, "read", mock_context)
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_tool_error_handling(self, tools, mock_client, mock_context):
        """Test error handling in tools."""
        # Get tools to test
        move = tools["move"]
        mark_read = tools["mark_read"]
        search = tools["search"]

        # Test move error handling
        mock_client.move_email.side_effect = Exception("Network error")
        result = await move("INBOX", 123, "Archive", mock_context)
        assert "Error" in result

        # Test mark_read error handling
        mock_client.mark_email.side_effect = Exception("Server timeout")
        result = await mark_read("INBOX", 123, mock_context)
        assert "Error" in result

        # Test search error handling — client.search_emails raises ValueError
        mock_client.search_emails.side_effect = ValueError("Search failed")
        result = await search("test", mock_context)
        assert "Search failed" in result
        mock_client.search_emails.side_effect = None

    @pytest.mark.asyncio
    async def test_tool_parameter_validation(self, tools, mock_client, mock_context):
        """Test parameter validation in tools."""
        # Get tools to test
        search = tools["search"]
        triage = tools["triage"]

        # Test search with invalid query — client raises ValueError
        mock_client.search_emails.side_effect = ValueError(
            "Unknown is: keyword: 'bogus'"
        )
        result = await search("is:bogus", mock_context)
        assert "Unknown is: keyword" in result
        mock_client.search_emails.side_effect = None

        # Test triage with missing target folder for move action
        mock_client.process_email_action.side_effect = ValueError(
            "target_folder is required for move action"
        )
        result = await triage("INBOX", 123, "move", ctx=mock_context)
        assert "target_folder" in result

        # Test triage with invalid action
        mock_client.process_email_action.side_effect = ValueError(
            "Unknown action 'nonexistent_action'"
        )
        result = await triage("INBOX", 123, "nonexistent_action", ctx=mock_context)
        assert "Unknown action" in result
        mock_client.process_email_action.side_effect = None

    @pytest.mark.asyncio
    async def test_read(self, tools, mock_client, mock_context, mock_email):
        """Read tool returns JSON with envelope, body, and conditional fields."""
        read = tools["read"]
        # mock_email has both html and text content; html should win
        result = await read("INBOX", 1, mock_context)
        mock_client.fetch_email.assert_called_with(1, "INBOX", no_cache=False)
        data = json.loads(result)
        assert data["uid"] == 1
        assert data["folder"] == "INBOX"
        assert data["from"] == "Sender <sender@example.com>"
        assert data["to"] == ["Recipient <recipient@example.com>"]
        assert data["subject"] == "Test Email"
        assert data["content_type"] == "text/html"
        assert "<p>Test content</p>" in data["body"]
        assert data["flags"] == ["\\Seen"]
        # cc and attachments are absent on this fixture
        assert "cc" not in data
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_read_no_cache_forwarded(self, tools, mock_client, mock_context):
        """The read tool forwards ``no_cache`` to the client."""
        read = tools["read"]
        await read("INBOX", 1, mock_context, no_cache=True)
        mock_client.fetch_email.assert_called_with(1, "INBOX", no_cache=True)

    @pytest.mark.asyncio
    async def test_search_no_cache_forwarded(self, tools, mock_client, mock_context):
        """The search tool forwards ``no_cache`` to the client."""
        search = tools["search"]
        mock_client.search_emails.return_value = {
            "results": [],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": "no_cache",
            },
        }
        await search("from:alice", mock_context, no_cache=True)
        mock_client.search_emails.assert_called_once_with(
            "from:alice",
            folder=None,
            limit=50,
            no_cache=True,
        )

    @pytest.mark.asyncio
    async def test_read_email_not_found(self, tools, mock_client, mock_context):
        """Read tool returns an error JSON when fetch_email returns None."""
        read = tools["read"]
        mock_client.fetch_email.return_value = None
        result = await read("INBOX", 9999, mock_context)
        data = json.loads(result)
        assert "error" in data
        assert "9999" in data["error"]

    @pytest.mark.asyncio
    async def test_read_text_only_body(self, tools, mock_client, mock_context):
        """When only text content is present, content_type is text/plain."""
        read = tools["read"]
        text_only = Email(
            message_id="<text@example.com>",
            subject="Plain",
            from_=EmailAddress(name="", address="x@example.com"),
            to=[],
            cc=[],
            bcc=[],
            date=datetime.now(),
            content=EmailContent(text="just text", html=None),
            attachments=[],
            flags=[],
            headers={},
            folder="INBOX",
            uid=2,
        )
        mock_client.fetch_email.return_value = text_only
        result = await read("INBOX", 2, mock_context)
        data = json.loads(result)
        assert data["content_type"] == "text/plain"
        assert data["body"] == "just text"

    @pytest.mark.asyncio
    async def test_read_includes_cc_and_attachments(
        self, tools, mock_client, mock_context
    ):
        """When the email carries cc / attachments, both surface in the result."""
        from courier.models import EmailAttachment

        read = tools["read"]
        with_extras = Email(
            message_id="<extras@example.com>",
            subject="Extras",
            from_=EmailAddress(name="", address="x@example.com"),
            to=[EmailAddress(name="", address="to@example.com")],
            cc=[EmailAddress(name="", address="cc@example.com")],
            bcc=[],
            date=datetime.now(),
            content=EmailContent(text="body", html=None),
            attachments=[
                EmailAttachment(
                    filename="report.pdf",
                    content_type="application/pdf",
                    size=1234,
                    content=b"",
                )
            ],
            flags=[],
            headers={},
            folder="INBOX",
            uid=3,
        )
        mock_client.fetch_email.return_value = with_extras
        result = await read("INBOX", 3, mock_context)
        data = json.loads(result)
        assert data["cc"] == ["cc@example.com"]
        assert isinstance(data["attachments"], list)
        assert data["attachments"][0]["filename"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_read_exception_returns_error_json(
        self, tools, mock_client, mock_context
    ):
        """When the IMAP fetch raises, the tool surfaces it as an error JSON."""
        read = tools["read"]
        mock_client.fetch_email.side_effect = Exception("boom")
        result = await read("INBOX", 1, mock_context)
        data = json.loads(result)
        assert data == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_folders(self, tools, mock_client, mock_context):
        """Folders tool returns the JSON list from folders_result()."""
        folders = tools["folders"]
        result = await folders(mock_context)
        data = json.loads(result)
        assert data == ["INBOX", "Sent", "Archive", "Trash"]

    @pytest.mark.asyncio
    async def test_folders_exception_returns_error_json(
        self, tools, mock_client, mock_context
    ):
        """When folders_result raises, the tool surfaces it as an error JSON."""
        folders = tools["folders"]
        mock_client.folders_result.side_effect = RuntimeError("connection lost")
        result = await folders(mock_context)
        data = json.loads(result)
        assert "error" in data
        assert "connection lost" in data["error"]


class TestRawImapPassthrough:
    """The imap: escape ships expressions through the generic emitter."""

    NOW = datetime(2026, 7, 15, 12, 0, 0)

    def _criteria(self, query: str):
        return emit_imap(parse(query), now=self.NOW).criteria

    def test_simple_single_keyword(self):
        assert self._criteria("imap:ALL") == [b"ALL"]
        assert self._criteria("imap:UNSEEN") == [b"UNSEEN"]

    def test_simple_text_search(self):
        assert self._criteria("imap:TEXT Edinburgh") == [b"TEXT", b"Edinburgh"]
        assert self._criteria('imap:TEXT "booking confirmation"') == [
            b"TEXT",
            b"booking confirmation",
        ]

    def test_or_expression(self):
        assert self._criteria('imap:OR TEXT "Edinburgh" TEXT "Berlin"') == [
            b"OR",
            b"TEXT",
            b"Edinburgh",
            b"TEXT",
            b"Berlin",
        ]

    def test_complex_travel_query(self):
        query = (
            'imap:OR TEXT "Edinburgh" OR TEXT "Berlin" OR TEXT "Munich" '
            'OR TEXT "Vienna" OR TEXT "Warsaw" OR TEXT "itinerary" '
            'OR TEXT "booking confirmation" OR TEXT "e-ticket" '
            'OR TEXT "reservation" OR TEXT "receipt" OR TEXT "ticket" '
            'TEXT "order"'
        )
        criteria = self._criteria(query)
        assert b"OR" in criteria
        assert b"Edinburgh" in criteria
        assert b"booking confirmation" in criteria
        assert b"order" in criteria

    def test_combined_criteria(self):
        assert self._criteria("imap:SEEN FROM gmail") == [
            b"SEEN",
            b"FROM",
            b"gmail",
        ]


class TestSearchToolDescription:
    """The tool description renders from the registry and documents the
    failure-visible envelope; refusing operators must be documented at
    the same moment the dispatch lands, since an undocumented refusal
    reads as breakage."""

    def test_operator_inventory_is_the_registry_rendering(self):
        from courier.query.registry import render_operator_help
        from courier.tools import _SEARCH_TOOL_DESCRIPTION

        assert render_operator_help() in _SEARCH_TOOL_DESCRIPTION

    def test_new_operator_rows_present(self):
        from courier.tools import _SEARCH_TOOL_DESCRIPTION

        for token in (
            "bcc:ADDR",
            "smaller:SIZE",
            "filename:NAME",
            "list:ID",
            "deliveredto:ADDR",
            "label:NAME",
            "category:NAME",
            "in:PLACE",
            "before:DATE",
            "Sent before DATE, exclusive",
        ):
            assert token in _SEARCH_TOOL_DESCRIPTION, token

    def test_envelope_fields_documented(self):
        from courier.tools import _SEARCH_TOOL_DESCRIPTION

        for token in (
            "folders_failed",
            "provenance.query",
            "total_count",
            "truncated",
            "treated_as_text",
        ):
            assert token in _SEARCH_TOOL_DESCRIPTION, token
