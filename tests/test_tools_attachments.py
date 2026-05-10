"""Tests for attachment-related MCP tools."""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from mailroom.models import Email, EmailAddress, EmailAttachment, EmailContent
from mailroom.tools import register_tools


@pytest.fixture
def mock_imap_client():
    """Create a mock IMAP client."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_context(mock_imap_client):
    """Create a mock MCP context with multi-account support."""
    context = MagicMock()
    context.app_state = {}
    # Set up lifespan_context as a real dict so get_client_from_context works
    context.request_context.lifespan_context = {
        "imap_clients": {"default": mock_imap_client},
        "default_imap": "default",
    }
    return context


@pytest.fixture
def email_with_attachments():
    """Create a test email with multiple attachments."""
    email = Email(
        uid=123,
        folder="INBOX",
        from_=EmailAddress(name="John Doe", address="john@example.com"),
        to=[EmailAddress(name="Jane Smith", address="jane@example.com")],
        subject="Email with attachments",
        date=None,
        message_id="<test@example.com>",
        in_reply_to=None,
        headers={},
        content=EmailContent(text="This email has attachments", html=None),
        attachments=[
            EmailAttachment(
                filename="document.pdf",
                content_type="application/pdf",
                size=1024,
                content_id="pdf1",
                content=b"PDF content here",
            ),
            EmailAttachment(
                filename="image.jpg",
                content_type="image/jpeg",
                size=2048,
                content_id=None,
                content=b"JPEG data here",
            ),
            EmailAttachment(
                filename="Life: A Test of Courage.pdf",
                content_type="application/pdf",
                size=3072,
                content_id=None,
                content=b"Another PDF with colon in filename",
            ),
        ],
        flags=[],
    )
    return email


@pytest.fixture
def email_without_attachments():
    """Create a test email without attachments."""
    email = Email(
        uid=456,
        folder="INBOX",
        from_=EmailAddress(name="John Doe", address="john@example.com"),
        to=[EmailAddress(name="Jane Smith", address="jane@example.com")],
        subject="Email without attachments",
        date=None,
        message_id="<test2@example.com>",
        in_reply_to=None,
        headers={},
        content=EmailContent(text="This email has no attachments", html=None),
        attachments=[],
        flags=[],
    )
    return email


class TestAttachments:
    """Tests for attachments tool."""

    @pytest.mark.asyncio
    async def test_attachments_with_multiple_attachments(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test listing attachments for an email with multiple attachments."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        attachments_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "attachments":
                attachments_tool = tool
                break

        assert attachments_tool is not None

        # Call the tool
        result = await attachments_tool.fn(folder="INBOX", uid=123, ctx=mock_context)

        # Parse result
        attachments = json.loads(result)

        # Assertions
        assert len(attachments) == 3

        assert attachments[0]["index"] == 0
        assert attachments[0]["filename"] == "document.pdf"
        assert attachments[0]["size"] == 1024
        assert attachments[0]["content_type"] == "application/pdf"
        assert attachments[0]["content_id"] == "pdf1"

        assert attachments[1]["index"] == 1
        assert attachments[1]["filename"] == "image.jpg"
        assert attachments[1]["size"] == 2048
        assert attachments[1]["content_type"] == "image/jpeg"
        assert "content_id" not in attachments[1]

        assert attachments[2]["index"] == 2
        assert attachments[2]["filename"] == "Life: A Test of Courage.pdf"
        assert attachments[2]["size"] == 3072
        assert attachments[2]["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_attachments_with_no_attachments(
        self, mock_context, mock_imap_client, email_without_attachments
    ):
        """Test listing attachments for an email without attachments."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_without_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        attachments_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "attachments":
                attachments_tool = tool
                break

        assert attachments_tool is not None

        # Call the tool
        result = await attachments_tool.fn(folder="INBOX", uid=456, ctx=mock_context)

        # Parse result
        attachments = json.loads(result)

        # Assertions
        assert isinstance(attachments, list)
        assert len(attachments) == 0

    @pytest.mark.asyncio
    async def test_attachments_email_not_found(self, mock_context, mock_imap_client):
        """Test listing attachments for a non-existent email."""
        # Setup
        mock_imap_client.fetch_email.return_value = None
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        attachments_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "attachments":
                attachments_tool = tool
                break

        assert attachments_tool is not None

        # Call the tool
        result = await attachments_tool.fn(folder="INBOX", uid=999, ctx=mock_context)

        # Parse result
        response = json.loads(result)

        # Assertions
        assert "error" in response
        assert "not found" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_attachments_with_exception(self, mock_context, mock_imap_client):
        """Test listing attachments when an exception occurs."""
        # Setup
        mock_imap_client.fetch_email.side_effect = Exception("Connection error")
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        attachments_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "attachments":
                attachments_tool = tool
                break

        assert attachments_tool is not None

        # Call the tool
        result = await attachments_tool.fn(folder="INBOX", uid=123, ctx=mock_context)

        # Parse result
        response = json.loads(result)

        # Assertions
        assert "error" in response
        assert "Connection error" in response["error"]


class TestSave:
    """Tests for save tool."""

    @pytest.mark.asyncio
    async def test_save_by_filename(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test downloading an attachment by filename."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        try:
            # Call the tool
            result = await save_tool.fn(
                folder="INBOX",
                uid=123,
                attachment="document.pdf",
                save_path=tmp_path,
                ctx=mock_context,
            )

            # Assertions
            assert "Success" in result
            assert "document.pdf" in result
            assert "1024 bytes" in result
            assert tmp_path in result

            # Verify file was written
            with open(tmp_path, "rb") as f:
                content = f.read()
            assert content == b"PDF content here"

        finally:
            # Cleanup
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @pytest.mark.asyncio
    async def test_save_by_index(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test downloading an attachment by index."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        try:
            # Call the tool - download second attachment (index 1)
            result = await save_tool.fn(
                folder="INBOX",
                uid=123,
                attachment="1",
                save_path=tmp_path,
                ctx=mock_context,
            )

            # Assertions
            assert "Success" in result
            assert "image.jpg" in result
            assert "2048 bytes" in result

            # Verify file was written
            with open(tmp_path, "rb") as f:
                content = f.read()
            assert content == b"JPEG data here"

        finally:
            # Cleanup
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @pytest.mark.asyncio
    async def test_save_with_colon_in_filename(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test downloading an attachment with colon in filename."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Create a temporary directory for testing
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "Life: A Test of Courage.pdf")

            # Call the tool
            result = await save_tool.fn(
                folder="INBOX",
                uid=123,
                attachment="Life: A Test of Courage.pdf",
                save_path=tmp_path,
                ctx=mock_context,
            )

            # Assertions
            assert "Success" in result
            assert "Life: A Test of Courage.pdf" in result
            assert "3072 bytes" in result

            # Verify file was written
            with open(tmp_path, "rb") as f:
                content = f.read()
            assert content == b"Another PDF with colon in filename"

    @pytest.mark.asyncio
    async def test_save_path_traversal_prevention(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test that path traversal attempts are sanitized."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Create a temporary directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Try to use path traversal
            malicious_path = os.path.join(tmp_dir, "../../../evil.txt")

            # Call the tool
            result = await save_tool.fn(
                folder="INBOX",
                uid=123,
                attachment="document.pdf",
                save_path=malicious_path,
                ctx=mock_context,
            )

            # The function should sanitize the path and succeed
            assert "Success" in result

            # Verify that the file was NOT written outside the tmp_dir
            # The sanitized path should remove the ../../../
            assert not os.path.exists("/evil.txt")
            assert not os.path.exists("/../evil.txt")

    @pytest.mark.asyncio
    async def test_save_email_not_found(self, mock_context, mock_imap_client):
        """Test downloading attachment from a non-existent email."""
        # Setup
        mock_imap_client.fetch_email.return_value = None
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Call the tool
        result = await save_tool.fn(
            folder="INBOX",
            uid=999,
            attachment="document.pdf",
            save_path="/tmp/test.pdf",
            ctx=mock_context,
        )

        # Assertions
        assert "Error" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_save_no_attachments(
        self, mock_context, mock_imap_client, email_without_attachments
    ):
        """Test downloading attachment from an email with no attachments."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_without_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Call the tool
        result = await save_tool.fn(
            folder="INBOX",
            uid=456,
            attachment="document.pdf",
            save_path="/tmp/test.pdf",
            ctx=mock_context,
        )

        # Assertions
        assert "Error" in result
        assert "no attachments" in result.lower()

    @pytest.mark.asyncio
    async def test_save_unknown_attachment(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test downloading attachment when the name does not match any."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Call the tool with non-existent filename
        result = await save_tool.fn(
            folder="INBOX",
            uid=123,
            attachment="nonexistent.txt",
            save_path="/tmp/test.txt",
            ctx=mock_context,
        )

        # Assertions
        assert "Error" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_save_invalid_index(
        self, mock_context, mock_imap_client, email_with_attachments
    ):
        """Test downloading attachment with invalid index."""
        # Setup
        mock_imap_client.fetch_email.return_value = email_with_attachments
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Call the tool with out-of-range index
        result = await save_tool.fn(
            folder="INBOX",
            uid=123,
            attachment="99",
            save_path="/tmp/test.txt",
            ctx=mock_context,
        )

        # Assertions
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_save_with_exception(self, mock_context, mock_imap_client):
        """Test downloading attachment when an exception occurs."""
        # Setup
        mock_imap_client.fetch_email.side_effect = Exception("Connection error")
        mock_context.app_state["imap_client"] = mock_imap_client

        # Create MCP instance and register tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test-server")
        register_tools(mcp, mock_imap_client)

        # Get the tool
        save_tool = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "save":
                save_tool = tool
                break

        assert save_tool is not None

        # Call the tool
        result = await save_tool.fn(
            folder="INBOX",
            uid=123,
            attachment="document.pdf",
            save_path="/tmp/test.pdf",
            ctx=mock_context,
        )

        # Assertions
        assert "Error" in result
        assert "Connection error" in result
