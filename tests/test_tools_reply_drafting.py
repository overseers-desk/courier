"""Tests for compose_and_save_reply_draft in smtp_client."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from courier.imap_client import AppendResult
from courier.models import Email, EmailAddress, EmailContent
from courier.smtp_client import _find_reply_from_address, compose_and_save_reply_draft


class TestFindReplyFromAddress:
    """Tests for _find_reply_from_address helper."""

    @pytest.fixture
    def email_obj(self):
        return Email(
            message_id="<test@example.com>",
            subject="Test",
            from_=EmailAddress(name="Sender", address="sender@example.com"),
            to=[
                EmailAddress(name="Me", address="me@example.com"),
                EmailAddress(name="Other", address="other@example.com"),
            ],
            cc=[EmailAddress(name="CC", address="cc@example.com")],
            date=datetime(2025, 4, 1, 10, 0, 0),
            content=EmailContent(text="body"),
            headers={},
        )

    def test_matches_to_field(self, email_obj):
        result = _find_reply_from_address(email_obj, "me@example.com")
        assert result.address == "me@example.com"

    def test_matches_cc_field(self, email_obj):
        result = _find_reply_from_address(email_obj, "cc@example.com")
        assert result.address == "cc@example.com"

    def test_case_insensitive(self, email_obj):
        result = _find_reply_from_address(email_obj, "ME@EXAMPLE.COM")
        assert result.address == "me@example.com"

    def test_fallback_to_first_to(self, email_obj):
        result = _find_reply_from_address(email_obj, "unknown@example.com")
        assert result.address == "me@example.com"

    def test_fallback_no_to(self):
        email_obj = Email(
            message_id="<test@example.com>",
            subject="Test",
            from_=EmailAddress(name="Sender", address="sender@example.com"),
            to=[],
            date=datetime(2025, 4, 1, 10, 0, 0),
            content=EmailContent(text="body"),
            headers={},
        )
        result = _find_reply_from_address(email_obj, "me@example.com")
        assert result.address == "me@example.com"


class TestComposeAndSaveReplyDraft:
    """Tests for compose_and_save_reply_draft."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.config = MagicMock()
        client.block.username = "me@example.com"
        client.save_draft_mime = MagicMock(
            return_value=AppendResult(uid=42, uidvalidity=1)
        )
        client.resolve_drafts_folder = MagicMock(return_value="Drafts")
        return client

    @pytest.fixture
    def original_email(self):
        return Email(
            message_id="<orig@example.com>",
            subject="Hello",
            from_=EmailAddress(name="Sender", address="sender@example.com"),
            to=[EmailAddress(name="Me", address="me@example.com")],
            date=datetime(2025, 4, 1, 10, 0, 0),
            content=EmailContent(text="Original body"),
            headers={},
        )

    def test_success(self, mock_client, original_email):
        mock_client.fetch_email.return_value = original_email

        result = compose_and_save_reply_draft(
            mock_client,
            "INBOX",
            1,
            "Thanks!",
        )

        assert result["status"] == "success"
        assert result["draft_uid"] == 42
        assert result["draft_folder"] == "Drafts"
        mock_client.save_draft_mime.assert_called_once()

    def test_email_not_found(self, mock_client):
        mock_client.fetch_email.return_value = None

        result = compose_and_save_reply_draft(
            mock_client,
            "INBOX",
            999,
            "Reply",
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_save_draft_failure(self, mock_client, original_email):
        mock_client.fetch_email.return_value = original_email
        mock_client.save_draft_mime.return_value = AppendResult(
            uid=None, uidvalidity=None
        )

        result = compose_and_save_reply_draft(
            mock_client,
            "INBOX",
            1,
            "Reply",
        )

        assert result["status"] == "error"
        assert "Failed to save draft" in result["message"]

    def test_with_cc_and_bcc(self, mock_client, original_email):
        mock_client.fetch_email.return_value = original_email

        result = compose_and_save_reply_draft(
            mock_client,
            "INBOX",
            1,
            "Reply",
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
        )

        assert result["status"] == "success"
        mime_msg = mock_client.save_draft_mime.call_args[0][0]
        assert "bcc@example.com" in str(mime_msg.get("Bcc", ""))

    def test_with_attachments(self, mock_client, original_email, tmp_path):
        """Attachments are forwarded into the composed MIME."""
        mock_client.fetch_email.return_value = original_email
        f = tmp_path / "note.txt"
        f.write_text("hi")

        result = compose_and_save_reply_draft(
            mock_client,
            "INBOX",
            1,
            "Reply",
            attachments=[str(f)],
        )

        assert result["status"] == "success"
        mime_msg = mock_client.save_draft_mime.call_args[0][0]
        assert mime_msg.get_content_type() == "multipart/mixed"
        filenames = [
            p.get_filename() for p in mime_msg.get_payload() if p.get_filename()
        ]
        assert "note.txt" in filenames
