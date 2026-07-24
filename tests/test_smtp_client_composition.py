"""Tests for SMTP client MIME composition functionality."""

import email.utils
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from courier.models import Email, EmailAddress, EmailContent
from courier.smtp_client import create_mime, parse_message_date


class TestCreateReplyMime:
    """Tests for create_mime function."""

    @pytest.fixture
    def sample_email(self) -> Email:
        """Create a sample email for testing."""
        return Email(
            message_id="<test123@example.com>",
            subject="Test Subject",
            from_=EmailAddress(name="Sender Name", address="sender@example.com"),
            to=[EmailAddress(name="Recipient Name", address="recipient@example.com")],
            cc=[EmailAddress(name="CC Recipient", address="cc@example.com")],
            date=datetime.now(),
            content=EmailContent(
                text="Original message content\nOn multiple lines.",
                html="<p>Original message content</p><p>On multiple lines.</p>",
            ),
            headers={"References": "<previous@example.com>"},
        )

    def test_create_basic_reply(self, sample_email: Email):
        """Test creating a basic reply."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        subject = "Re: Test Subject"
        body = "This is a reply."

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            subject=subject,
            body=body,
            reply_all=False,
        )

        # Check basic properties
        assert mime_message["To"] == "Sender Name <sender@example.com>"
        assert mime_message["Subject"] == "Re: Test Subject"
        assert mime_message["From"] == "Reply To <sender@example.com>"
        assert mime_message["In-Reply-To"] == "<test123@example.com>"
        assert "<test123@example.com>" in mime_message["References"]
        assert "<previous@example.com>" in mime_message["References"]

        # Check content - handle both multipart and non-multipart payloads
        if mime_message.is_multipart():
            payload = mime_message.get_payload(0).get_payload(decode=True).decode()
        else:
            payload = mime_message.get_payload(decode=True).decode()

        assert "This is a reply." in payload
        assert "Original message content" in payload

    def test_reply_honours_reply_to_over_from(self):
        """When Reply-To is set on the parent, the reply's To uses it.

        Forwarder scenarios (Google Groups, alias services) rewrite
        From to the list address and preserve the original sender in
        Reply-To; replying to From would loop back into the user's
        own mailbox rather than reach the original correspondent.
        """
        parent = Email(
            message_id="<forwarded@example.com>",
            subject="Test",
            from_=EmailAddress(name="List Forwarder", address="list@group.example"),
            to=[EmailAddress(name="Me", address="me@example.com")],
            reply_to=[
                EmailAddress(name="Original Sender", address="original@vendor.example")
            ],
            date=datetime.now(),
            content=EmailContent(text="hello", html=""),
        )
        from_addr = EmailAddress(name="Me", address="me@example.com")

        mime_message = create_mime(
            original_email=parent, from_addr=from_addr, body="thanks"
        )

        assert mime_message["To"] == "Original Sender <original@vendor.example>"
        assert "list@group.example" not in (mime_message["To"] or "")

    def test_reply_all_with_reply_to_puts_from_in_cc(self):
        """Reply-all with Reply-To: To=Reply-To, original From moves to Cc."""
        parent = Email(
            message_id="<forwarded2@example.com>",
            subject="Test",
            from_=EmailAddress(name="List Forwarder", address="list@group.example"),
            to=[
                EmailAddress(name="Me", address="me@example.com"),
                EmailAddress(name="Other", address="other@example.com"),
            ],
            cc=[EmailAddress(name="CC One", address="cc1@example.com")],
            reply_to=[
                EmailAddress(name="Original Sender", address="original@vendor.example")
            ],
            date=datetime.now(),
            content=EmailContent(text="hello", html=""),
        )
        from_addr = EmailAddress(name="Me", address="me@example.com")

        mime_message = create_mime(
            original_email=parent,
            from_addr=from_addr,
            body="thanks all",
            reply_all=True,
        )

        # To = Reply-To + parent.To minus us
        assert mime_message["To"] == (
            "Original Sender <original@vendor.example>, Other <other@example.com>"
        )
        # Cc = parent.Cc minus us + original From (diverted by Reply-To)
        assert mime_message["Cc"] == (
            "CC One <cc1@example.com>, List Forwarder <list@group.example>"
        )

    def test_create_reply_all(self, sample_email: Email):
        """Test creating a reply-all."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        subject = "Re: Test Subject"
        body = "This is a reply to all."

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            subject=subject,
            body=body,
            reply_all=True,
        )

        # Check recipients - should include original CCs and sender
        assert (
            mime_message["To"]
            == "Sender Name <sender@example.com>, Recipient Name <recipient@example.com>"
        )
        assert mime_message["Cc"] == "CC Recipient <cc@example.com>"

    def test_create_reply_with_custom_cc(self, sample_email: Email):
        """Test creating a reply with custom CC recipients."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        subject = "Re: Test Subject"
        body = "This is a reply with custom CC."
        cc = [
            EmailAddress(name="Custom CC", address="custom@example.com"),
            EmailAddress(name="Another CC", address="another@example.com"),
        ]

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            subject=subject,
            body=body,
            cc=cc,
        )

        # Check CC recipients
        assert (
            mime_message["Cc"]
            == "Custom CC <custom@example.com>, Another CC <another@example.com>"
        )

    def test_create_reply_with_subject_prefix(self, sample_email: Email):
        """Test creating a reply with a custom subject prefix."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        body = "This is a reply with custom subject prefix."

        # No prefix provided, but original doesn't start with Re:
        mime_message = create_mime(
            original_email=sample_email, from_addr=reply_to, body=body
        )

        assert mime_message["Subject"].startswith("Re: ")

        # Custom subject provided
        custom_subject = "Custom: Test Subject"
        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body=body,
            subject=custom_subject,
        )

        assert mime_message["Subject"] == custom_subject

        # Original already has Re: prefix
        sample_email.subject = "Re: Already Prefixed"
        mime_message = create_mime(
            original_email=sample_email, from_addr=reply_to, body=body
        )

        assert mime_message["Subject"] == "Re: Already Prefixed"

    def test_create_html_reply(self, sample_email: Email):
        """Test creating a reply with HTML content."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        body = "This is a plain text reply."
        html_body = "<p>This is an <b>HTML</b> reply.</p>"

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body=body,
            html_body=html_body,
        )

        # Should be multipart with at least 2 parts
        assert mime_message.is_multipart()
        alternative = mime_message.get_payload(0)
        assert alternative.is_multipart()

        # Check HTML part
        html_part = alternative.get_payload(1)
        html_text = html_part.get_payload(decode=True).decode()
        assert "<p>This is an <b>HTML</b> reply.</p>" in html_text

    def test_quoting_original_content(self, sample_email: Email):
        """Test proper quoting of original content."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        body = "This is a reply with original content quoted."

        mime_message = create_mime(
            original_email=sample_email, from_addr=reply_to, body=body
        )

        # Check content
        if mime_message.is_multipart():
            payload = mime_message.get_payload(0).get_payload(decode=True).decode()
        else:
            payload = mime_message.get_payload(decode=True).decode()

        # Should have quoting prefix (>) and original content
        assert "This is a reply with original content quoted." in payload

        # Check for proper quoting
        lines = payload.split("\n")
        quoted_lines = [line for line in lines if line.startswith(">")]
        assert any("> Original message content" in line for line in quoted_lines)

    def test_reply_with_single_attachment_is_multipart_mixed(
        self, sample_email: Email, tmp_path: Path
    ):
        """Attaching one file produces multipart/mixed with body + attachment."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="see attached",
            attachments=[str(pdf)],
        )

        assert mime_message.get_content_type() == "multipart/mixed"
        parts = mime_message.get_payload()
        assert len(parts) == 2
        assert parts[0].get_content_type() == "text/plain"
        assert parts[1].get_content_type() == "application/pdf"
        assert parts[1].get_filename() == "report.pdf"

    def test_reply_with_multiple_attachments(self, sample_email: Email, tmp_path: Path):
        """Two attachments appear as siblings of the body."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        f2 = tmp_path / "b.log"
        f2.write_text("bravo")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="two files",
            attachments=[str(f1), str(f2)],
        )

        parts = mime_message.get_payload()
        filenames = [p.get_filename() for p in parts if p.get_filename()]
        assert filenames == ["a.txt", "b.log"]

    def test_reply_with_attachment_and_html_body(
        self, sample_email: Email, tmp_path: Path
    ):
        """html_body + attachments → mixed[alternative, attachment]."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"pdf")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="plain",
            html_body="<p>html</p>",
            attachments=[str(pdf)],
        )

        assert mime_message.get_content_type() == "multipart/mixed"
        parts = mime_message.get_payload()
        assert parts[0].get_content_type() == "multipart/alternative"
        assert parts[1].get_content_type() == "application/pdf"

    def test_reply_with_attachment_missing_file_raises_value_error(
        self, sample_email: Email, tmp_path: Path
    ):
        """Missing path surfaces a clear ValueError with the offending path."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        missing = str(tmp_path / "does_not_exist.pdf")

        with pytest.raises(ValueError) as excinfo:
            create_mime(
                original_email=sample_email,
                from_addr=reply_to,
                body="x",
                attachments=[missing],
            )
        assert missing in str(excinfo.value)

    def test_reply_with_attachment_directory_raises_value_error(
        self, sample_email: Email, tmp_path: Path
    ):
        """Passing a directory path is rejected."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")

        with pytest.raises(ValueError):
            create_mime(
                original_email=sample_email,
                from_addr=reply_to,
                body="x",
                attachments=[str(tmp_path)],
            )

    def test_attachment_mime_type_defaults_to_octet_stream_for_unknown_ext(
        self, sample_email: Email, tmp_path: Path
    ):
        """Unknown extensions fall back to application/octet-stream."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        blob = tmp_path / "mystery.zzz"
        blob.write_bytes(b"\x00\x01")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="x",
            attachments=[str(blob)],
        )

        att = mime_message.get_payload()[1]
        assert att.get_content_type() == "application/octet-stream"

    def test_attachment_filename_uses_basename_only(
        self, sample_email: Email, tmp_path: Path
    ):
        """Full paths never leak into the Content-Disposition filename."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        nested = tmp_path / "nested"
        nested.mkdir()
        f = nested / "inner.txt"
        f.write_text("hi")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="x",
            attachments=[str(f)],
        )

        assert mime_message.get_payload()[1].get_filename() == "inner.txt"

    def test_attachment_non_ascii_filename_uses_rfc2231_encoding(
        self, sample_email: Email, tmp_path: Path
    ):
        """Non-ASCII filenames serialise as RFC 2231 filename*=utf-8''..."""
        reply_to = EmailAddress(name="Reply To", address="sender@example.com")
        f = tmp_path / "报告.pdf"
        f.write_bytes(b"pdf")

        mime_message = create_mime(
            original_email=sample_email,
            from_addr=reply_to,
            body="x",
            attachments=[str(f)],
        )

        raw = mime_message.as_string()
        assert "filename*=utf-8''" in raw

    def test_auto_html_when_body_contains_table(self):
        """Markdown table in body with body_html=None → multipart/alternative."""
        from_addr = EmailAddress(name="Sender", address="sender@example.com")
        to = [EmailAddress(name="Recipient", address="recipient@example.com")]
        body = "Summary:\n\n| col | val |\n|-----|-----|\n| a   | 1   |\n"

        mime_message = create_mime(
            from_addr=from_addr, to=to, subject="report", body=body
        )

        assert mime_message.is_multipart()
        alternative = mime_message.get_payload(0)
        assert alternative.get_content_type() == "multipart/alternative"
        plain_part = alternative.get_payload(0)
        html_part = alternative.get_payload(1)
        assert plain_part.get_content_type() == "text/plain"
        assert html_part.get_content_type() == "text/html"
        html_text = html_part.get_payload(decode=True).decode()
        assert '<table border="1">' in html_text
        assert "<th>col</th>" in html_text
        assert "<td>a</td>" in html_text
        # Plain text part is the original body verbatim.
        plain_text = plain_part.get_payload(decode=True).decode()
        assert "| col | val |" in plain_text

    def test_auto_html_when_body_contains_heading(self):
        """ATX heading in body with body_html=None → multipart/alternative."""
        from_addr = EmailAddress(name="Sender", address="sender@example.com")
        to = [EmailAddress(name="Recipient", address="recipient@example.com")]
        body = "# Status\n\nAll systems nominal.\n"

        mime_message = create_mime(
            from_addr=from_addr, to=to, subject="status", body=body
        )

        assert mime_message.is_multipart()
        alternative = mime_message.get_payload(0)
        assert alternative.get_content_type() == "multipart/alternative"
        html_part = alternative.get_payload(1)
        html_text = html_part.get_payload(decode=True).decode()
        assert "<h1>Status</h1>" in html_text

    def test_empty_body_html_suppresses_auto_render(self):
        """body_html='' forces text/plain only even with table/heading present."""
        from_addr = EmailAddress(name="Sender", address="sender@example.com")
        to = [EmailAddress(name="Recipient", address="recipient@example.com")]
        body = "# Heading\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"

        mime_message = create_mime(
            from_addr=from_addr,
            to=to,
            subject="x",
            body=body,
            html_body="",
        )

        # No multipart wrapping when no html and no attachments.
        assert not mime_message.is_multipart()
        assert mime_message.get_content_type() == "text/plain"

    def test_no_auto_render_for_plain_prose_with_bullets(self):
        """Bullets and links alone do not trigger auto-render."""
        from_addr = EmailAddress(name="Sender", address="sender@example.com")
        to = [EmailAddress(name="Recipient", address="recipient@example.com")]
        body = (
            "Hi there,\n\n"
            "- item one\n"
            "- item two\n\n"
            "See https://example.com for details.\n"
        )

        mime_message = create_mime(from_addr=from_addr, to=to, subject="x", body=body)

        assert not mime_message.is_multipart()
        assert mime_message.get_content_type() == "text/plain"

    def test_caller_supplied_html_used_verbatim_even_with_triggers(self):
        """A caller-supplied body_html is used as-is; auto-render does not run."""
        from_addr = EmailAddress(name="Sender", address="sender@example.com")
        to = [EmailAddress(name="Recipient", address="recipient@example.com")]
        body = "# Heading in plain\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        caller_html = "<p>hand-written</p>"

        mime_message = create_mime(
            from_addr=from_addr,
            to=to,
            subject="x",
            body=body,
            html_body=caller_html,
        )

        alternative = mime_message.get_payload(0)
        html_part = alternative.get_payload(1)
        html_text = html_part.get_payload(decode=True).decode()
        assert html_text.strip() == caller_html
        assert "<table>" not in html_text
        assert "<h1>" not in html_text


class TestDateHeader:
    """The ``date`` parameter of ``create_mime`` and its parser."""

    @staticmethod
    def _addrs():
        return (
            EmailAddress(name="Sender", address="sender@example.com"),
            [EmailAddress(name="Recipient", address="recipient@example.com")],
        )

    def test_default_date_is_now(self):
        """Without *date* the header carries the current instant."""
        from_addr, to = self._addrs()

        mime_message = create_mime(from_addr=from_addr, to=to, subject="x", body="b")

        header_dt = email.utils.parsedate_to_datetime(mime_message["Date"])
        assert abs((header_dt - datetime.now(timezone.utc)).total_seconds()) < 300

    def test_explicit_aware_date_is_used_verbatim(self):
        """An aware datetime reaches the header at its own offset."""
        from_addr, to = self._addrs()
        when = datetime(2019, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=10)))

        mime_message = create_mime(
            from_addr=from_addr, to=to, subject="x", body="b", date=when
        )

        assert mime_message["Date"] == "Mon, 04 Mar 2019 05:06:07 +1000"

    def test_naive_date_is_read_as_local_time(self):
        """A naive datetime binds to the local zone, not to ``-0000``."""
        from_addr, to = self._addrs()
        when = datetime(2019, 3, 4, 5, 6, 7)

        mime_message = create_mime(
            from_addr=from_addr, to=to, subject="x", body="b", date=when
        )

        header_dt = email.utils.parsedate_to_datetime(mime_message["Date"])
        assert header_dt == when.astimezone()
        assert not mime_message["Date"].endswith("-0000")

    def test_only_one_date_header(self):
        """Serialising yields a single Date field, whatever the branch."""
        from_addr, to = self._addrs()
        when = datetime(2019, 3, 4, 5, 6, 7, tzinfo=timezone.utc)

        for kwargs in ({}, {"html_body": "<p>h</p>"}):
            mime_message = create_mime(
                from_addr=from_addr,
                to=to,
                subject="x",
                body="b",
                date=when,
                **kwargs,
            )
            assert mime_message.get_all("Date") == ["Mon, 04 Mar 2019 05:06:07 +0000"]

    def test_reply_accepts_date(self):
        """The reply branch honours *date* as the fresh-message branch does."""
        original = Email(
            message_id="<orig@example.com>",
            subject="Test Subject",
            from_=EmailAddress(name="S", address="s@example.com"),
            to=[EmailAddress(name="R", address="r@example.com")],
            date=datetime.now(),
            content=EmailContent(text="body", html=None),
            headers={},
        )
        when = datetime(2019, 3, 4, 5, 6, 7, tzinfo=timezone.utc)

        mime_message = create_mime(
            original_email=original,
            from_addr=EmailAddress(name="R", address="r@example.com"),
            body="reply",
            date=when,
        )

        assert mime_message["Date"] == "Mon, 04 Mar 2019 05:06:07 +0000"


class TestParseMessageDate:
    """Tests for ``parse_message_date``."""

    def test_offset_form_preserved(self):
        assert parse_message_date("2019-03-04T05:06:07+10:00") == datetime(
            2019, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=10))
        )

    def test_naive_form_accepted(self):
        assert parse_message_date("2019-03-04T05:06:07") == datetime(
            2019, 3, 4, 5, 6, 7
        )

    def test_date_only_form_accepted(self):
        assert parse_message_date("2019-03-04") == datetime(2019, 3, 4)

    def test_rejects_unparseable(self):
        with pytest.raises(ValueError) as exc:
            parse_message_date("last Tuesday")
        assert "2019-03-04" in str(exc.value)
