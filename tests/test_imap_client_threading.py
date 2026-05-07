"""Tests for email threading functionality in ImapClient."""

import email
import email.mime.application
import email.mime.audio
import email.mime.image
import email.mime.multipart
import email.mime.text
import unittest
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

from imapclient.response_types import SearchIds

from mailroom.config import ImapBlock
from mailroom.imap_client import ImapClient


class TestImapClientThreading(unittest.TestCase):
    """Test cases for email threading functionality."""

    def setUp(self) -> None:
        """Set up test environment."""
        self.config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        self.mock_client = MagicMock()

        # Create patcher for IMAPClient
        self.imapclient_patcher = patch("mailroom.imap_client.imapclient.IMAPClient")
        self.mock_imapclient = self.imapclient_patcher.start()
        self.mock_imapclient.return_value = self.mock_client

        # Initialize ImapClient with mock
        self.imap_client = ImapClient(self.config)
        self.imap_client.connected = True
        self.imap_client.client = self.mock_client

    def tearDown(self) -> None:
        """Clean up after tests."""
        self.imapclient_patcher.stop()

    def create_mock_email(
        self,
        uid: int,
        message_id: str,
        subject: str,
        sender: str,
        to: str,
        date: datetime,
        body_text: str,
        body_html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
    ) -> Dict:
        """Create a mock email message for testing.

        Args:
            uid: Email UID
            message_id: Message-ID header value
            subject: Subject line
            sender: From email address
            to: To email address
            date: Date the email was sent
            body_text: Plain text body content
            body_html: HTML body content (optional)
            in_reply_to: In-Reply-To header (optional)
            references: References header values (optional)
            attachments: List of attachment metadata (optional)

        Returns:
            Mock email data dictionary for use in tests
        """
        # Create email message
        msg = email.mime.multipart.MIMEMultipart()
        msg["Message-ID"] = message_id
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        msg["Date"] = email.utils.formatdate(
            (date - datetime(1970, 1, 1)).total_seconds()
        )

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to

        if references:
            msg["References"] = " ".join(references)

        # Add text part
        text_part = email.mime.text.MIMEText(body_text, "plain", "utf-8")
        msg.attach(text_part)

        # Add HTML part if provided
        if body_html:
            html_part = email.mime.text.MIMEText(body_html, "html", "utf-8")
            msg.attach(html_part)

        # Add attachments if provided
        if attachments:
            # Add attachments
            for attachment in attachments:
                # Parse the content type to get main type and subtype
                content_type_parts = attachment["content_type"].split("/")
                main_type = content_type_parts[0]
                sub_type = content_type_parts[1] if len(content_type_parts) > 1 else ""

                # Create the appropriate MIME part based on content type
                if main_type == "image":
                    att_part = email.mime.image.MIMEImage(
                        attachment["content"], _subtype=sub_type
                    )
                elif main_type == "application":
                    att_part = email.mime.application.MIMEApplication(
                        attachment["content"], _subtype=sub_type
                    )
                elif main_type == "audio":
                    att_part = email.mime.audio.MIMEAudio(
                        attachment["content"], _subtype=sub_type
                    )
                else:
                    # Default to application
                    att_part = email.mime.application.MIMEApplication(
                        attachment["content"], _subtype=sub_type
                    )

                att_part.add_header(
                    "Content-Disposition",
                    f"attachment; filename=\"{attachment['filename']}\"",
                )
                if attachment.get("content_id"):
                    att_part.add_header("Content-ID", f"<{attachment['content_id']}>")

                # Ensure content-type is preserved
                att_part.replace_header("Content-Type", attachment["content_type"])

                msg.attach(att_part)

        # Mock IMAP response format
        return {b"BODY[]": msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

    def test_fetch_email_with_all_body_parts(self):
        """Test that fetch_email retrieves all body parts correctly."""
        # Create mock email with both text and HTML parts
        uid = 123
        message_id = "<message1@example.com>"
        text_content = "This is the plain text content"
        html_content = "<div>This is the <b>HTML</b> content</div>"

        # Set up mock response
        mock_email = self.create_mock_email(
            uid=uid,
            message_id=message_id,
            subject="Test Subject",
            sender="sender@example.com",
            to="recipient@example.com",
            date=datetime.now(),
            body_text=text_content,
            body_html=html_content,
        )

        self.mock_client.fetch.return_value = {uid: mock_email}

        # Call method being tested
        email_obj = self.imap_client.fetch_email(uid)

        # Assert that both text and HTML content are retrieved
        assert email_obj is not None
        assert email_obj.content.text == text_content
        assert email_obj.content.html == html_content
        assert email_obj.message_id == message_id

    def test_fetch_email_with_attachments(self):
        """Test that fetch_email retrieves attachment metadata correctly."""
        # Create mock email with attachments
        uid = 123

        # Directly construct a multipart MIME message
        msg = email.mime.multipart.MIMEMultipart()
        msg["Message-ID"] = "<message1@example.com>"
        msg["Subject"] = "Email with attachments"
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Date"] = email.utils.formatdate(
            (datetime.now() - datetime(1970, 1, 1)).total_seconds()
        )

        # Add text part
        text_part = email.mime.text.MIMEText(
            "Please see attached files", "plain", "utf-8"
        )
        msg.attach(text_part)

        # Add PDF attachment
        pdf_attachment = email.mime.application.MIMEApplication(
            b"PDF content", _subtype="pdf"
        )
        pdf_attachment.add_header(
            "Content-Disposition", 'attachment; filename="document.pdf"'
        )
        pdf_attachment.add_header("Content-ID", "<pdf1>")
        pdf_attachment.replace_header("Content-Type", "application/pdf")
        msg.attach(pdf_attachment)

        # Add JPEG attachment
        jpg_attachment = email.mime.image.MIMEImage(b"Image data", _subtype="jpeg")
        jpg_attachment.add_header(
            "Content-Disposition", 'attachment; filename="image.jpg"'
        )
        jpg_attachment.replace_header("Content-Type", "image/jpeg")
        msg.attach(jpg_attachment)

        # Set up mock response
        mock_email = {b"BODY[]": msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

        self.mock_client.fetch.return_value = {uid: mock_email}

        # Call method being tested
        email_obj = self.imap_client.fetch_email(uid)

        # Assert that attachment metadata is retrieved correctly
        assert email_obj is not None
        assert len(email_obj.attachments) == 2

        # Find each attachment by filename
        pdf_attachment = next(
            (a for a in email_obj.attachments if a.filename == "document.pdf"), None
        )
        jpg_attachment = next(
            (a for a in email_obj.attachments if a.filename == "image.jpg"), None
        )

        # Check first attachment
        assert pdf_attachment is not None
        assert pdf_attachment.filename == "document.pdf"
        assert pdf_attachment.content_type == "application/pdf"
        assert pdf_attachment.content_id == "pdf1"
        assert pdf_attachment.size > 0

        # Check second attachment
        assert jpg_attachment is not None
        assert jpg_attachment.filename == "image.jpg"
        assert jpg_attachment.content_type == "image/jpeg"
        assert jpg_attachment.size > 0

    def test_fetch_thread_by_message_id(self):
        """Test fetching all emails in a thread using Message-ID."""
        # Create a series of mock emails in a thread
        initial_uid = 100
        initial_message_id = "<thread1-initial@example.com>"
        reply1_uid = 101
        reply1_message_id = "<thread1-reply1@example.com>"
        reply2_uid = 102
        reply2_message_id = "<thread1-reply2@example.com>"

        # Directly create email objects to use in the mock responses
        # We need to manually construct the headers for the test assertions
        initial_headers = {
            "Message-ID": initial_message_id,
            "Subject": "Thread Subject",
            "From": "person1@example.com",
            "To": "person2@example.com",
            "Date": email.utils.formatdate(
                (
                    datetime.now() - timedelta(hours=2) - datetime(1970, 1, 1)
                ).total_seconds()
            ),
        }

        reply1_headers = {
            "Message-ID": reply1_message_id,
            "Subject": "Re: Thread Subject",
            "From": "person2@example.com",
            "To": "person1@example.com",
            "Date": email.utils.formatdate(
                (
                    datetime.now() - timedelta(hours=1) - datetime(1970, 1, 1)
                ).total_seconds()
            ),
            "In-Reply-To": initial_message_id,
            "References": initial_message_id,
        }

        reply2_headers = {
            "Message-ID": reply2_message_id,
            "Subject": "Re: Thread Subject",
            "From": "person1@example.com",
            "To": "person2@example.com",
            "Date": email.utils.formatdate(
                (datetime.now() - datetime(1970, 1, 1)).total_seconds()
            ),
            "In-Reply-To": reply1_message_id,
            "References": f"{initial_message_id} {reply1_message_id}",
        }

        # Create the email messages with proper headers

        # Create multipart messages with proper headers
        initial_msg = email.mime.multipart.MIMEMultipart()
        for name, value in initial_headers.items():
            initial_msg[name] = value
        initial_text = email.mime.text.MIMEText("Initial message", "plain", "utf-8")
        initial_msg.attach(initial_text)

        reply1_msg = email.mime.multipart.MIMEMultipart()
        for name, value in reply1_headers.items():
            reply1_msg[name] = value
        reply1_text = email.mime.text.MIMEText("First reply", "plain", "utf-8")
        reply1_msg.attach(reply1_text)

        reply2_msg = email.mime.multipart.MIMEMultipart()
        for name, value in reply2_headers.items():
            reply2_msg[name] = value
        reply2_text = email.mime.text.MIMEText("Second reply", "plain", "utf-8")
        reply2_msg.attach(reply2_text)

        # Create the mock responses
        initial_email = {b"BODY[]": initial_msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

        reply1_email = {b"BODY[]": reply1_msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

        reply2_email = {b"BODY[]": reply2_msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

        # Configure mocks
        self.mock_client.fetch.side_effect = [
            # First fetch for the initial email
            {initial_uid: initial_email},
            # Later fetch for all thread emails
            {
                initial_uid: initial_email,
                reply1_uid: reply1_email,
                reply2_uid: reply2_email,
            },
        ]

        # Mock search results
        self.mock_client.search.side_effect = [
            # Results for Message-ID search
            SearchIds([]),
            # Results for References search
            SearchIds([reply1_uid, reply2_uid]),
            # Results for In-Reply-To search
            SearchIds([reply1_uid, reply2_uid]),
        ]

        # Call method being tested
        thread_emails = self.imap_client.fetch_thread(initial_uid)

        # Verify correct thread behavior
        assert len(thread_emails) == 3

        # Verify chronological ordering
        assert thread_emails[0].uid == initial_uid
        assert thread_emails[1].uid == reply1_uid
        assert thread_emails[2].uid == reply2_uid

        # Verify thread headers are preserved
        assert thread_emails[1].headers.get("In-Reply-To") == initial_message_id
        assert initial_message_id in thread_emails[2].headers.get("References", "")

        # Verify in_reply_to and references properties are set correctly
        assert thread_emails[1].in_reply_to == initial_message_id
        assert thread_emails[2].in_reply_to == reply1_message_id
        assert initial_message_id in thread_emails[2].references

    def test_fetch_thread_by_subject(self):
        """Test fetching thread by subject when proper headers are missing."""
        # Create thread without proper In-Reply-To/References headers
        initial_uid = 200
        initial_message_id = "<thread2-initial@example.com>"
        reply_uid = 201

        # Create mock emails
        initial_email = self.create_mock_email(
            uid=initial_uid,
            message_id=initial_message_id,
            subject="Thread Subject",
            sender="person1@example.com",
            to="person2@example.com",
            date=datetime.now() - timedelta(hours=1),
            body_text="Initial message",
        )

        reply_email = self.create_mock_email(
            uid=reply_uid,
            message_id="<thread2-reply@example.com>",
            subject="Re: Thread Subject",  # Subject-based threading only
            sender="person2@example.com",
            to="person1@example.com",
            date=datetime.now(),
            body_text="Reply message",
            # No In-Reply-To or References headers
        )

        # Configure mocks
        self.mock_client.fetch.side_effect = [
            # First fetch for the initial email
            {initial_uid: initial_email},
            # Later fetch for all thread emails
            {initial_uid: initial_email, reply_uid: reply_email},
        ]

        # Mock search results - empty for header searches, results for subject search
        self.mock_client.search.side_effect = [
            # Results for Message-ID search
            SearchIds([]),
            # Results for References search
            SearchIds([]),
            # Results for In-Reply-To search
            SearchIds([]),
            # Results for Subject search
            SearchIds([reply_uid]),
        ]

        # Call method being tested
        thread_emails = self.imap_client.fetch_thread(initial_uid)

        # Verify correct thread behavior
        assert len(thread_emails) == 2
        assert thread_emails[0].uid == initial_uid
        assert thread_emails[1].uid == reply_uid

    def test_fetch_thread_with_many_messages(self):
        """Test performance when fetching threads with many messages."""
        initial_uid = 300
        message_ids = [f"<thread3-{i}@example.com>" for i in range(25)]
        uids = [initial_uid + i for i in range(25)]

        # Create initial email
        mock_emails = {}
        initial_email = self.create_mock_email(
            uid=initial_uid,
            message_id=message_ids[0],
            subject="Large Thread",
            sender="person1@example.com",
            to="person2@example.com",
            date=datetime.now() - timedelta(hours=24),
            body_text="Initial message",
        )
        mock_emails[initial_uid] = initial_email

        # Create 24 reply emails
        for i in range(1, 25):
            reply_email = self.create_mock_email(
                uid=uids[i],
                message_id=message_ids[i],
                subject="Re: Large Thread",
                sender="person2@example.com" if i % 2 else "person1@example.com",
                to="person1@example.com" if i % 2 else "person2@example.com",
                date=datetime.now() - timedelta(hours=24 - i),
                body_text=f"Reply {i}",
                in_reply_to=message_ids[i - 1],
                references=message_ids[:i],
            )
            mock_emails[uids[i]] = reply_email

        # Configure mocks
        self.mock_client.fetch.side_effect = [
            # First fetch for the initial email
            {initial_uid: initial_email},
            # Later fetch for all thread emails
            mock_emails,
        ]

        # Mock search results - return all UIDs except the initial one
        self.mock_client.search.return_value = SearchIds(uids[1:])

        # Call method being tested
        thread_emails = self.imap_client.fetch_thread(initial_uid)

        # Verify results
        assert len(thread_emails) == 25
        # Check chronological order
        for i in range(25):
            assert thread_emails[i].uid == uids[i]

    def test_fetch_thread_with_missing_message(self):
        """Test error handling if some messages in thread are inaccessible."""
        initial_uid = 400
        initial_message_id = "<thread4-initial@example.com>"
        reply1_uid = 401
        reply1_message_id = "<thread4-reply1@example.com>"
        reply2_uid = 402
        reply2_message_id = "<thread4-reply2@example.com>"

        # Create mock emails
        initial_email = self.create_mock_email(
            uid=initial_uid,
            message_id=initial_message_id,
            subject="Thread With Missing Message",
            sender="person1@example.com",
            to="person2@example.com",
            date=datetime.now() - timedelta(hours=2),
            body_text="Initial message",
        )

        # Skip reply1 (simulating inaccessible message)

        reply2_email = self.create_mock_email(
            uid=reply2_uid,
            message_id=reply2_message_id,
            subject="Re: Thread With Missing Message",
            sender="person1@example.com",
            to="person2@example.com",
            date=datetime.now(),
            body_text="Second reply",
            in_reply_to=reply1_message_id,
            references=[initial_message_id, reply1_message_id],
        )

        # Configure mocks
        self.mock_client.fetch.side_effect = [
            # First fetch for the initial email
            {initial_uid: initial_email},
            # Later fetch for thread emails - missing reply1
            {
                initial_uid: initial_email,
                reply2_uid: reply2_email,
                # reply1_uid is missing
            },
        ]

        # Mock search results
        self.mock_client.search.return_value = SearchIds([reply1_uid, reply2_uid])

        # Call method being tested
        thread_emails = self.imap_client.fetch_thread(initial_uid)

        # Verify results - should still return available messages
        assert len(thread_emails) == 2
        assert thread_emails[0].uid == initial_uid
        assert thread_emails[1].uid == reply2_uid

    def test_fetch_thread_strict_subject_fallback_filters_unrelated(self):
        """When SUBJECT search returns >= 20 results, fetch_thread falls back
        to strict-subject filtering: only candidates whose subject is in the
        ``Re:/Fwd:/FW:`` family of the canonical subject join the thread.
        Unrelated messages that share a substring with the subject are
        excluded."""
        initial_uid = 600
        initial_message_id = "<thread6-initial@example.com>"

        initial_email = self.create_mock_email(
            uid=initial_uid,
            message_id=initial_message_id,
            subject="Quarterly Review",
            sender="person1@example.com",
            to="person2@example.com",
            date=datetime.now() - timedelta(hours=2),
            body_text="Initial message",
        )

        # 20 candidates: half are valid Re:/Fwd: variants, half are unrelated
        # but happened to match the SUBJECT search (e.g. broader keyword hit).
        valid_uids = list(range(610, 620))  # 10 valid
        unrelated_uids = list(range(620, 630))  # 10 unrelated
        all_candidate_uids = valid_uids + unrelated_uids

        candidate_emails = {initial_uid: initial_email}
        for uid in valid_uids:
            candidate_emails[uid] = self.create_mock_email(
                uid=uid,
                message_id=f"<reply-{uid}@example.com>",
                subject="Re: Quarterly Review",
                sender="person2@example.com",
                to="person1@example.com",
                date=datetime.now() - timedelta(hours=1),
                body_text=f"Reply {uid}",
            )
        for uid in unrelated_uids:
            candidate_emails[uid] = self.create_mock_email(
                uid=uid,
                message_id=f"<unrelated-{uid}@example.com>",
                subject="About the Quarterly Review process changes",
                sender="ops@example.com",
                to="staff@example.com",
                date=datetime.now() - timedelta(hours=3),
                body_text="Unrelated body",
            )

        # First fetch is the initial email; second fetch is the candidate set
        # (during strict filtering); third fetch is the final thread set.
        self.mock_client.fetch.side_effect = [
            {initial_uid: initial_email},
            candidate_emails,
            {uid: candidate_emails[uid] for uid in [initial_uid] + valid_uids},
        ]

        # References / In-Reply-To searches return empty so thread_uids stays
        # at {initial_uid} and the SUBJECT branch fires.
        self.mock_client.search.side_effect = [
            SearchIds([]),  # References from message_id
            SearchIds([]),  # In-Reply-To from message_id
            SearchIds(all_candidate_uids),  # SUBJECT search: 20 hits
        ]

        thread_emails = self.imap_client.fetch_thread(initial_uid)

        # Expect: initial + 10 valid replies, unrelated 10 excluded.
        thread_uids = [e.uid for e in thread_emails]
        assert initial_uid in thread_uids
        for uid in valid_uids:
            assert uid in thread_uids
        for uid in unrelated_uids:
            assert uid not in thread_uids

    def test_thread_with_different_encodings(self):
        """Test handling of different encodings within thread messages."""
        # Create emails with different encodings
        uid1 = 500
        uid2 = 501

        # Mock an email with UTF-8 encoding
        utf8_email = self.create_mock_email(
            uid=uid1,
            message_id="<encoding-test1@example.com>",
            subject="Encoding Test",
            sender="sender@example.com",
            to="recipient@example.com",
            date=datetime.now() - timedelta(hours=1),
            body_text="UTF-8 text with special chars: é, ñ, 你好",
        )

        # Create a second email with Latin-1 encoding
        latin1_part = email.mime.text.MIMEText(
            "Latin-1 text with special chars: é, ñ, ç", "plain", "latin-1"
        )

        # Create full message
        latin1_msg = email.mime.multipart.MIMEMultipart()
        latin1_msg["Message-ID"] = "<encoding-test2@example.com>"
        latin1_msg["Subject"] = "Re: Encoding Test"
        latin1_msg["From"] = "recipient@example.com"
        latin1_msg["To"] = "sender@example.com"
        latin1_msg["Date"] = email.utils.formatdate(
            (datetime.now() - datetime(1970, 1, 1)).total_seconds()
        )
        latin1_msg["In-Reply-To"] = "<encoding-test1@example.com>"
        latin1_msg["References"] = "<encoding-test1@example.com>"
        latin1_msg.attach(latin1_part)

        latin1_email = {b"BODY[]": latin1_msg.as_bytes(), b"FLAGS": (b"\\Seen",)}

        # Configure mocks
        self.mock_client.fetch.side_effect = [
            # First fetch for the initial email
            {uid1: utf8_email},
            # Later fetch for all thread emails
            {uid1: utf8_email, uid2: latin1_email},
        ]

        # Mock search results
        self.mock_client.search.return_value = SearchIds([uid2])

        # Call method being tested
        thread_emails = self.imap_client.fetch_thread(uid1)

        # Verify correct thread behavior
        assert len(thread_emails) == 2
        # Verify both encodings were handled properly
        assert "UTF-8 text with special chars" in thread_emails[0].content.text
        assert "Latin-1 text with special chars" in thread_emails[1].content.text


if __name__ == "__main__":
    unittest.main()
