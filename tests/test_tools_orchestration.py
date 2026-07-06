"""Tests for the accept-invite orchestration workflow."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from courier.imap_client import AppendResult
from courier.models import Email, EmailAddress, EmailContent
from courier.workflows.meeting_reply import process_meeting_invite_workflow


class TestAcceptInviteWorkflow:
    """Tests for the accept-invite tool (process_meeting_invite_workflow)."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock IMAP client."""
        client = MagicMock()
        client.config = MagicMock()
        client.block.username = "test@example.com"
        client.save_draft_mime = MagicMock(
            return_value=AppendResult(uid=123, uidvalidity=1)
        )
        client.resolve_drafts_folder = MagicMock(return_value="Drafts")
        return client

    @pytest.fixture
    def invite_email(self):
        """Create a meeting invite email."""
        return Email(
            message_id="<invite123@example.com>",
            subject="Meeting Invitation: Team Sync",
            from_=EmailAddress(name="Organizer", address="organizer@example.com"),
            to=[EmailAddress(name="Me", address="test@example.com")],
            date=datetime(2025, 4, 1, 10, 0, 0),
            content=EmailContent(
                text="You are invited to a team sync meeting.\n"
                "When: Tuesday, April 1, 2025 10:00 AM - 11:00 AM"
            ),
            headers={"Content-Type": "text/calendar; method=REQUEST"},
        )

    @pytest.fixture
    def non_invite_email(self):
        """Create a regular (non-invite) email."""
        return Email(
            message_id="<msg123@example.com>",
            subject="Regular Email",
            from_=EmailAddress(name="Sender", address="sender@example.com"),
            to=[EmailAddress(name="Me", address="test@example.com")],
            date=datetime(2025, 4, 1, 9, 0, 0),
            content=EmailContent(text="This is a regular email."),
            headers={},
        )

    def test_success_accept(self, mock_client, invite_email):
        mock_client.fetch_email.return_value = invite_email

        result = process_meeting_invite_workflow(
            mock_client,
            "INBOX",
            456,
            availability_mode="always_available",
        )

        assert result["status"] == "success"
        assert result["draft_uid"] == 123
        assert result["draft_folder"] == "Drafts"
        assert result["availability"] is True
        mock_client.fetch_email.assert_called_once_with(456, "INBOX")
        mock_client.save_draft_mime.assert_called_once()

    def test_success_decline(self, mock_client, invite_email):
        mock_client.fetch_email.return_value = invite_email

        result = process_meeting_invite_workflow(
            mock_client,
            "INBOX",
            456,
            availability_mode="always_busy",
        )

        assert result["status"] == "success"
        assert result["availability"] is False
        assert "decline" in result["message"]

    def test_not_invite(self, mock_client, non_invite_email):
        mock_client.fetch_email.return_value = non_invite_email

        result = process_meeting_invite_workflow(mock_client, "INBOX", 456)

        assert result["status"] == "not_invite"
        assert "not a meeting invite" in result["message"].lower()

    def test_email_not_found(self, mock_client):
        mock_client.fetch_email.return_value = None

        result = process_meeting_invite_workflow(mock_client, "INBOX", 456)

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_save_draft_failure(self, mock_client, invite_email):
        mock_client.fetch_email.return_value = invite_email
        mock_client.save_draft_mime.return_value = AppendResult(
            uid=None, uidvalidity=None
        )

        result = process_meeting_invite_workflow(
            mock_client,
            "INBOX",
            456,
            availability_mode="always_available",
        )

        assert result["status"] == "error"
        assert "Failed to save draft" in result["message"]
