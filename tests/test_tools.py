"""Tests for MCP tools implementation."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import Context, FastMCP

from mailroom.imap_client import ImapClient
from mailroom.models import Email, EmailAddress, EmailContent
from mailroom.query_parser import parse_query
from mailroom.tools import register_tools


# Patch the get_client_from_context function to use our mock client
@pytest.fixture(autouse=True)
def patch_get_client():
    with patch("mailroom.tools.get_client_from_context") as mock_get_client:
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
        # Configure default return values
        client.move_email.return_value = True
        client.mark_email.return_value = True
        client.delete_email.return_value = True
        client.list_folders.return_value = ["INBOX", "Sent", "Archive", "Trash"]
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

        # Test failure case
        mock_client.mark_email.return_value = False
        result = await mark_read("INBOX", 123, mock_context)
        assert "Failed to mark email as read" in result

    @pytest.mark.asyncio
    async def test_mark_unread(self, tools, mock_client, mock_context):
        """Test marking an email as unread."""
        # Get the mark_unread function
        mark_unread = tools["mark_unread"]

        # Reset mock for this test
        mock_client.mark_email.reset_mock()
        mock_client.mark_email.return_value = True

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
        mock_client.mark_email.return_value = True

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

        # Test failure case
        mock_client.delete_email.return_value = False
        result = await delete("INBOX", 123, mock_context)
        assert "Failed to delete" in result

        # Test error handling
        mock_client.delete_email.side_effect = Exception("Permission denied")
        result = await delete("INBOX", 123, mock_context)
        assert "Error" in result

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
        from mailroom.models import EmailAttachment

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
        """Folders tool returns the JSON list from list_folders()."""
        folders = tools["folders"]
        result = await folders(mock_context)
        data = json.loads(result)
        assert data == ["INBOX", "Sent", "Archive", "Trash"]

    @pytest.mark.asyncio
    async def test_folders_exception_returns_error_json(
        self, tools, mock_client, mock_context
    ):
        """When list_folders raises, the tool surfaces it as an error JSON."""
        folders = tools["folders"]
        mock_client.list_folders.side_effect = RuntimeError("connection lost")
        result = await folders(mock_context)
        data = json.loads(result)
        assert "error" in data
        assert "connection lost" in data["error"]


class TestRawImapPassthrough:
    """Test the imap: escape hatch via parse_query."""

    def test_parse_simple_single_keyword(self):
        """Test parsing simple single-keyword queries."""
        assert parse_query("imap:ALL") == "ALL"
        assert parse_query("imap:UNSEEN") == "UNSEEN"
        assert parse_query("imap:SEEN") == "SEEN"

    def test_parse_simple_text_search(self):
        """Test parsing simple TEXT searches."""
        result = parse_query("imap:TEXT Edinburgh")
        assert result == ["TEXT", "Edinburgh"]

        result = parse_query('imap:TEXT "booking confirmation"')
        assert result == ["TEXT", "booking confirmation"]

    def test_parse_simple_or_expression(self):
        """Test parsing simple OR expressions."""
        result = parse_query('imap:OR TEXT "Edinburgh" TEXT "Berlin"')
        assert result == ["OR", "TEXT", "Edinburgh", "TEXT", "Berlin"]

    def test_parse_nested_or_expression(self):
        """Test parsing nested OR expressions."""
        result = parse_query('imap:OR TEXT "Edinburgh" OR TEXT "Berlin" TEXT "Munich"')
        assert result == [
            "OR",
            "TEXT",
            "Edinburgh",
            "OR",
            "TEXT",
            "Berlin",
            "TEXT",
            "Munich",
        ]

    def test_parse_complex_travel_query(self):
        """Test parsing the complex travel booking query."""
        query = 'imap:OR TEXT "Edinburgh" OR TEXT "Berlin" OR TEXT "Munich" OR TEXT "Vienna" OR TEXT "Warsaw" OR TEXT "itinerary" OR TEXT "booking confirmation" OR TEXT "e-ticket" OR TEXT "reservation" OR TEXT "receipt" OR TEXT "ticket" TEXT "order"'
        result = parse_query(query)

        assert isinstance(result, list)
        assert "OR" in result
        assert "TEXT" in result
        assert "Edinburgh" in result
        assert "Berlin" in result
        assert "booking confirmation" in result
        assert "order" in result

    def test_parse_from_subject(self):
        """Test parsing FROM and SUBJECT via raw passthrough."""
        result = parse_query('imap:FROM "john@example.com"')
        assert result == ["FROM", "john@example.com"]

        result = parse_query('imap:SUBJECT "meeting"')
        assert result == ["SUBJECT", "meeting"]

    def test_parse_combined(self):
        """Test parsing combined criteria without OR."""
        result = parse_query("imap:SEEN FROM gmail")
        assert result == ["SEEN", "FROM", "gmail"]
