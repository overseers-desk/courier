"""Tests for email models."""

import email
import unittest
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from courier.models import (
    Email,
    EmailAddress,
    EmailAttachment,
    EmailContent,
    decode_mime_header,
)


def _email(**fields) -> Email:
    base = dict(
        message_id="<m@example.com>",
        subject="Subject",
        from_=EmailAddress(name="Sender", address="sender@example.com"),
        to=[EmailAddress(name="", address="to@example.com")],
        date=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        content=EmailContent(text="plain body"),
        flags=["\\Seen"],
        headers={"Received": "from relay", "DKIM-Signature": "v=1"},
        folder="INBOX",
        uid=42,
    )
    base.update(fields)
    return Email(**base)


class TestModels(unittest.TestCase):
    """Test cases for email models."""

    def test_decode_mime_header(self):
        """Test MIME header decoding."""
        # Test ASCII header
        self.assertEqual(decode_mime_header("Hello"), "Hello")

        # Test encoded header
        encoded_header = Header("Héllö Wörld", "utf-8").encode()
        self.assertEqual(decode_mime_header(encoded_header), "Héllö Wörld")

        # Test empty header
        self.assertEqual(decode_mime_header(None), "")
        self.assertEqual(decode_mime_header(""), "")

    def test_decode_mime_header_unfolds_folded_value(self):
        """A folded header value (RFC 5322 line break before whitespace)
        comes out on one line, plain or encoded."""
        self.assertEqual(
            decode_mime_header("Client Number 1234567- Example Pty\r\n Ltd ATF"),
            "Client Number 1234567- Example Pty Ltd ATF",
        )
        self.assertEqual(decode_mime_header("one\n\ttwo"), "one\ttwo")
        encoded = Header("Héllö Wörld " * 8, "utf-8").encode()
        self.assertIn("\n", encoded)
        self.assertEqual(decode_mime_header(encoded), "Héllö Wörld " * 8)

    def test_email_address_parse(self):
        """Test email address parsing."""
        # Test name + address
        addr = EmailAddress.parse("John Doe <john@example.com>")
        self.assertEqual(addr.name, "John Doe")
        self.assertEqual(addr.address, "john@example.com")

        # Test quoted name
        addr = EmailAddress.parse('"Smith, John" <john@example.com>')
        self.assertEqual(addr.name, "Smith, John")
        self.assertEqual(addr.address, "john@example.com")

        # Test address only
        addr = EmailAddress.parse("jane@example.com")
        self.assertEqual(addr.name, "")
        self.assertEqual(addr.address, "jane@example.com")

        # Test string conversion
        addr = EmailAddress("Jane Smith", "jane@example.com")
        self.assertEqual(str(addr), "Jane Smith <jane@example.com>")
        addr = EmailAddress("", "jane@example.com")
        self.assertEqual(str(addr), "jane@example.com")

        # RFC 5322: display names with specials must be quoted on emit, or
        # an address parser splits at the comma into two addresses.
        addr = EmailAddress("Smith, John", "smith@example.com")
        self.assertEqual(str(addr), '"Smith, John" <smith@example.com>')
        addr = EmailAddress("Smith (work)", "smith@example.com")
        self.assertEqual(str(addr), '"Smith (work)" <smith@example.com>')

    def test_email_from_message(self):
        """Test creating email from message."""
        # Create a multipart email
        msg = MIMEMultipart()
        msg["From"] = "John Doe <john@example.com>"
        msg["To"] = "Jane Smith <jane@example.com>, bob@example.com"
        msg["Subject"] = "Test Email"
        msg["Message-ID"] = "<test123@example.com>"
        msg["Date"] = email.utils.formatdate()

        # Add plain text part
        text_part = MIMEText("Hello, this is a test email.", "plain")
        msg.attach(text_part)

        # Add HTML part
        html_part = MIMEText("<p>Hello, this is a <b>test</b> email.</p>", "html")
        msg.attach(html_part)

        # Parse email
        email_obj = Email.from_message(msg, uid=1234, folder="INBOX")

        # Check basic fields
        self.assertEqual(email_obj.message_id, "<test123@example.com>")
        self.assertEqual(email_obj.subject, "Test Email")
        self.assertEqual(str(email_obj.from_), "John Doe <john@example.com>")
        self.assertEqual(len(email_obj.to), 2)
        self.assertEqual(str(email_obj.to[0]), "Jane Smith <jane@example.com>")
        self.assertEqual(str(email_obj.to[1]), "bob@example.com")
        self.assertEqual(email_obj.folder, "INBOX")
        self.assertEqual(email_obj.uid, 1234)

        # Check content
        self.assertEqual(email_obj.content.text, "Hello, this is a test email.")
        self.assertEqual(
            email_obj.content.html, "<p>Hello, this is a <b>test</b> email.</p>"
        )

        # Check summary
        summary = email_obj.summary()
        self.assertIn("From: John Doe <john@example.com>", summary)
        self.assertIn("Subject: Test Email", summary)

    def test_email_from_message_parses_reply_to(self):
        """Reply-To header is parsed into Email.reply_to.

        Forwarders (Google Groups, alias services) rewrite From and
        preserve the original sender in Reply-To. The model must
        surface Reply-To so the reply path can route to the original
        correspondent rather than back through the forwarder.
        """
        msg = MIMEText("body")
        msg["From"] = "List Forwarder <list@group.example>"
        msg["To"] = "Me <me@example.com>"
        msg["Reply-To"] = (
            "Original Sender <original@vendor.example>, second@vendor.example"
        )
        msg["Subject"] = "fwd"
        msg["Message-ID"] = "<fwd@example.com>"

        email_obj = Email.from_message(msg)

        self.assertEqual(len(email_obj.reply_to), 2)
        self.assertEqual(
            str(email_obj.reply_to[0]), "Original Sender <original@vendor.example>"
        )
        self.assertEqual(str(email_obj.reply_to[1]), "second@vendor.example")

    def test_email_from_message_reply_to_absent(self):
        """Email.reply_to is an empty list when the header is absent."""
        msg = MIMEText("body")
        msg["From"] = "Sender <sender@example.com>"
        msg["To"] = "Me <me@example.com>"
        msg["Subject"] = "plain"
        msg["Message-ID"] = "<plain@example.com>"

        email_obj = Email.from_message(msg)

        self.assertEqual(email_obj.reply_to, [])

    def test_to_dict_plain_message_has_only_the_fixed_keys(self):
        email_obj = _email()

        self.assertEqual(
            email_obj.to_dict(),
            {
                "uid": 42,
                "folder": "INBOX",
                "from": "Sender <sender@example.com>",
                "to": ["to@example.com"],
                "subject": "Subject",
                "date": email_obj.date.astimezone().isoformat(),
                "flags": ["\\Seen"],
                "message_id": "<m@example.com>",
                "content_type": "text/plain",
                "body": "plain body",
            },
        )

    def test_to_dict_html_part_wins_over_text(self):
        out = _email(content=EmailContent(text="plain", html="<p>rich</p>")).to_dict()

        self.assertEqual(out["content_type"], "text/html")
        self.assertEqual(out["body"], "<p>rich</p>")

    def test_to_dict_no_body(self):
        out = _email(content=EmailContent(), date=None).to_dict()

        self.assertEqual(out["content_type"], "text/plain")
        self.assertIsNone(out["body"])
        self.assertIsNone(out["date"])

    def test_to_dict_optional_keys_appear_when_set(self):
        out = _email(
            cc=[EmailAddress(name="Smith, John", address="js@example.com")],
            attachments=[
                EmailAttachment(
                    filename="a.pdf", content_type="application/pdf", size=3
                )
            ],
            in_reply_to=["<root@example.com>"],
            references=["<root@example.com>", "<mid@example.com>"],
        ).to_dict()

        self.assertEqual(out["cc"], ['"Smith, John" <js@example.com>'])
        self.assertEqual(
            out["attachments"],
            [
                {
                    "index": 0,
                    "filename": "a.pdf",
                    "size": 3,
                    "content_type": "application/pdf",
                }
            ],
        )
        self.assertEqual(out["in_reply_to"], ["<root@example.com>"])
        self.assertEqual(out["references"], ["<root@example.com>", "<mid@example.com>"])

    def test_to_dict_unknown_uid_is_none(self):
        out = _email(uid=None).to_dict()

        self.assertIsNone(out["uid"])
        self.assertEqual(out["folder"], "INBOX")

    def test_to_dict_redacted_message_renders_its_placeholders(self):
        original = _email(
            cc=[EmailAddress(name="", address="cc@example.com")],
            attachments=[
                EmailAttachment(
                    filename="a.pdf", content_type="application/pdf", size=3
                )
            ],
            in_reply_to=["<root@example.com>"],
        )

        out = original.redact("legal").to_dict()

        self.assertEqual(out["subject"], "[redacted by rule legal]")
        self.assertEqual(out["from"], "[redacted]")
        self.assertEqual(out["to"], [])
        self.assertEqual(out["content_type"], "text/plain")
        self.assertEqual(out["body"], "[redacted by rule legal]")
        self.assertEqual(
            (out["uid"], out["folder"], out["message_id"], out["in_reply_to"]),
            (42, "INBOX", "<m@example.com>", ["<root@example.com>"]),
        )
        self.assertNotIn("cc", out)
        self.assertNotIn("attachments", out)
        self.assertNotIn("redacted_by", out)


if __name__ == "__main__":
    unittest.main()


def test_repeated_recipient_fields_keep_every_address():
    """A message carrying To, Cc, Bcc or Reply-To twice keeps the
    addresses of every occurrence, not only the first."""
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Cc: d@example.com\r\n"
        b"Cc: e@example.com\r\n"
        b"Bcc: f@example.com\r\n"
        b"Bcc: g@example.com\r\n"
        b"Reply-To: h@example.com\r\n"
        b"Reply-To: i@example.com\r\n"
        b"Subject: s\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = Email.from_message(email.message_from_bytes(raw))
    assert [a.address for a in parsed.to] == ["b@example.com", "c@example.com"]
    assert [a.address for a in parsed.cc] == ["d@example.com", "e@example.com"]
    assert [a.address for a in parsed.bcc] == ["f@example.com", "g@example.com"]
    assert [a.address for a in parsed.reply_to] == ["h@example.com", "i@example.com"]


def test_folded_headers_parse_onto_one_line():
    """Subject, From and the headers dict of a message whose headers are
    folded carry no line breaks."""
    raw = (
        b"From: 'Example Agency' via Director\r\n <director@example.com>\r\n"
        b"To: b@example.com\r\n"
        b"Subject: Client Number 1234567- Example Development and\r\n"
        b" Conservation Pty Ltd\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = Email.from_message(email.message_from_bytes(raw))
    assert parsed.subject == (
        "Client Number 1234567- Example Development and Conservation Pty Ltd"
    )
    assert parsed.from_.address == "director@example.com"
    assert parsed.from_.name == "'Example Agency' via Director"
    assert "\n" not in parsed.headers["Subject"]
    assert "\n" not in parsed.headers["From"]


def test_threading_fields_hold_each_id_without_comments_or_folding():
    """Message-ID, In-Reply-To and References keep each <...> identifier
    on its own, with the comments and folding around them left out
    (RFC 5322 section 3.6.4)."""
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: s\r\n"
        b"Message-ID: <m@example.com> (sent by Example Mail)\r\n"
        b"In-Reply-To: <p1@example.com> (first parent)\r\n"
        b" <p2@example.com>\r\n"
        b"References: <r1@example.com>\r\n"
        b"\t<r2@example.com> (old)\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = Email.from_message(email.message_from_bytes(raw))
    assert parsed.message_id == "<m@example.com>"
    assert parsed.in_reply_to == ["<p1@example.com>", "<p2@example.com>"]
    assert parsed.references == ["<r1@example.com>", "<r2@example.com>"]
    assert parsed.to_dict()["in_reply_to"] == ["<p1@example.com>", "<p2@example.com>"]


def test_in_reply_to_holding_only_a_phrase_names_no_parent():
    """The obsolete In-Reply-To form with text and no <...> identifier
    gives no parent, rather than the text as one."""
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: s\r\n"
        b"Message-ID: <m@example.com>\r\n"
        b"In-Reply-To: Your message of Mon, 1 Jan 2001\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = Email.from_message(email.message_from_bytes(raw))
    assert parsed.in_reply_to == []
    assert "in_reply_to" not in parsed.to_dict()


def test_message_id_without_angle_brackets_is_kept_unfolded():
    """A Message-ID that lacks its angle brackets is malformed but is
    still the message's only identifier, so it is kept, unfolded."""
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: s\r\n"
        b"Message-ID:\r\n"
        b" m@example.com\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = Email.from_message(email.message_from_bytes(raw))
    assert parsed.message_id == "m@example.com"


def _parse(raw: bytes, **kwargs) -> Email:
    return Email.from_message(email.message_from_bytes(raw), **kwargs)


def _headed(*header_lines: bytes, body: bytes = b"body\r\n") -> bytes:
    return b"".join(line + b"\r\n" for line in header_lines) + b"\r\n" + body


def test_raw_utf8_headers_read_as_utf8():
    """A header carrying raw UTF-8 (RFC 6532) reads as the text it
    holds, in Subject, in an address display name and in the header
    map, instead of raising."""
    parsed = _parse(
        _headed(
            "From: José <j@example.com>".encode("utf-8"),
            "To: Café Crème <c@example.com>, b@example.com".encode("utf-8"),
            b"Subject: caf\xc3\xa9",
        )
    )
    assert parsed.subject == "café"
    assert parsed.headers["Subject"] == "café"
    assert (parsed.from_.name, parsed.from_.address) == ("José", "j@example.com")
    assert [(a.name, a.address) for a in parsed.to] == [
        ("Café Crème", "c@example.com"),
        ("", "b@example.com"),
    ]


def test_broken_encoded_words_do_not_raise():
    """A truncated or invalid encoded word leaves the header readable:
    the bytes that decode are kept and the rest is marked or left as
    written, and no exception escapes."""
    assert decode_mime_header("=?utf-8?q?caf=C3?=") == "caf�"
    assert decode_mime_header("=?utf-8?b?w6?=") == "\ufffd"
    assert decode_mime_header("=?utf-8?b?w?=") == "=?utf-8?b?w?="
    assert decode_mime_header("=?x-nonsense?q?hi?=") == "hi"
    parsed = _parse(
        _headed(
            b"From: a@example.com",
            b"To: =?utf-8?b?w?= <b@example.com>",
            b"Subject: =?utf-8?q?caf=C3?=",
        )
    )
    assert parsed.subject == "caf�"
    assert parsed.to[0].address == "b@example.com"


def test_quoted_comma_in_display_name_is_one_address():
    """A display name quoted around a comma is one address, and the
    address after it is the second (RFC 5322 section 3.4)."""
    parsed = _parse(
        _headed(
            b"From: a@example.com",
            b'To: "Doe, Jane" <d@x.com>, b@y.com',
            b"Subject: s",
        )
    )
    assert [(a.name, a.address) for a in parsed.to] == [
        ("Doe, Jane", "d@x.com"),
        ("", "b@y.com"),
    ]


def test_groups_comments_and_encoded_names_parse_in_every_address_field():
    """Groups yield their members and an empty group none, a comment is
    not part of the address, and an encoded display name is decoded
    after the list is split, in To, Cc, Bcc and Reply-To alike."""
    parsed = _parse(
        _headed(
            b'From: "Last, First" <f@example.com>',
            b'To: Team: a@x.com, "B, Bea" <b@x.com>;, undisclosed-recipients:;',
            b"Cc: c@x.com (Carol Comma, Esq.)",
            b"Bcc: =?utf-8?q?Doe=2C_Jos=C3=A9?= <jose@x.com>",
            b"Reply-To: Support: =?utf-8?b?Q2Fmw6k=?= <r@x.com>;",
            b"Subject: s",
        )
    )
    assert (parsed.from_.name, parsed.from_.address) == (
        "Last, First",
        "f@example.com",
    )
    assert [(a.name, a.address) for a in parsed.to] == [
        ("", "a@x.com"),
        ("B, Bea", "b@x.com"),
    ]
    assert [(a.name, a.address) for a in parsed.cc] == [("", "c@x.com")]
    assert [(a.name, a.address) for a in parsed.bcc] == [("Doe, José", "jose@x.com")]
    assert [(a.name, a.address) for a in parsed.reply_to] == [("Café", "r@x.com")]


def test_zone_less_and_minus_zero_dates_are_utc():
    """A Date with -0000 or no zone names UTC (RFC 5322 sections 3.3 and
    4.3), so it is timezone-aware and renders as that instant."""
    for date_header in (
        b"Date: Mon, 2 Mar 2026 10:00:00 -0000",
        b"Date: Mon, 2 Mar 2026 10:00:00",
    ):
        parsed = _parse(
            _headed(b"From: a@example.com", b"To: b@example.com", date_header)
        )
        assert parsed.date == datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
        assert datetime.fromisoformat(parsed.to_dict()["date"]) == parsed.date
    parsed = _parse(
        _headed(b"From: a@example.com", b"Date: Mon, 2 Mar 2026 10:00:00 +0200")
    )
    assert parsed.date == datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)


def test_cid_references_match_case_insensitively_and_percent_decoded():
    """A cid: reference matches its part's Content-ID in any letter case,
    percent-decoded, and quoted or unquoted (RFC 2392)."""
    att = EmailAttachment(
        filename="a.png",
        content_type="image/png",
        size=4,
        content_id="<Image One@Example.com>",
        content=b"PNG!",
    )
    parsed = _email(
        content=EmailContent(
            html=(
                '<img SRC="CID:image%20one@example.com">'
                "<img src='cid:Image%20One@example.COM'>"
                "<img src=cid:image%20one@EXAMPLE.com alt=x>"
                '<img src="cid:other@example.com">'
            )
        ),
        attachments=[att],
    )
    rendered = parsed.html_with_embedded_images()
    assert rendered.count("data:image/png;base64,UE5HIQ==") == 3
    assert "cid:image" not in rendered.lower().replace("cid:other", "")
    assert '<img src="cid:other@example.com">' in rendered
    assert "<img src=data:image/png;base64,UE5HIQ== alt=x>" in rendered


def _text_message(content_type: bytes, body: bytes) -> bytes:
    return _headed(
        b"From: a@example.com",
        b"To: b@example.com",
        b"Subject: s",
        b"Content-Type: " + content_type,
        body=body,
    )


def test_unknown_or_missing_charset_reads_as_utf8():
    """A text part whose charset is unknown or missing is read as UTF-8,
    with replacement marks for bytes that do not fit, never an error
    string; a part labelled us-ascii is read the same way."""
    assert (
        _parse(_text_message(b"text/plain; charset=x-foo", b"caf\xc3\xa9")).content.text
        == "café"
    )
    assert _parse(_text_message(b"text/plain", b"caf\xc3\xa9")).content.text == "café"
    assert (
        _parse(
            _text_message(b"text/plain; charset=us-ascii", b"caf\xc3\xa9")
        ).content.text
        == "café"
    )
    assert (
        _parse(
            _text_message(b"text/html; charset=x-foo", b"<p>caf\xc3\xa9</p>")
        ).content.html
        == "<p>café</p>"
    )
    assert (
        _parse(_text_message(b"text/plain; charset=x-foo", b"caf\xe9")).content.text
        == "caf�"
    )
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: s\r\n"
        b'Content-Type: multipart/alternative; boundary="b"\r\n'
        b"\r\n"
        b"--b\r\n"
        b"Content-Type: text/plain; charset=x-foo\r\n"
        b"\r\n"
        b"caf\xc3\xa9\r\n"
        b"--b\r\n"
        b"Content-Type: text/html; charset=x-foo\r\n"
        b"\r\n"
        b"<p>caf\xc3\xa9</p>\r\n"
        b"--b--\r\n"
    )
    parsed = _parse(raw)
    assert (parsed.content.text, parsed.content.html) == ("café", "<p>café</p>")


def test_iso_8859_1_reads_as_windows_1252():
    """A part labelled iso-8859-1, or an alias of it, is read as
    windows-1252 (the WHATWG Encoding Standard's mapping of that
    label), so bytes 0x80-0x9F are curly quotes and dashes."""
    body = b"\x93quoted\x94 \x96 caf\xe9"
    for label in (b"iso-8859-1", b"ISO-8859-1", b"latin1", b"latin-1", b"l1"):
        parsed = _parse(_text_message(b"text/plain; charset=" + label, body))
        assert parsed.content.text == "“quoted” – café", label
    parsed = _parse(_text_message(b"text/plain; charset=iso-8859-2", b"\xb3\xf3d\xbc"))
    assert parsed.content.text == "łódź"
