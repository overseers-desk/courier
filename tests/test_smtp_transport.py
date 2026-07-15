"""Tests for the SMTP transport: post-DATA capture, SES rewrite, Bcc handling.

The send() function is exercised through an injected fake SMTP class so that
no network is required. The fake records the sequence of calls and supplies
the post-DATA response that drives the SES Message-ID rewrite logic.
"""

import smtplib
from email.message import EmailMessage

import pytest

from courier.config import SmtpConfig
from courier.smtp_transport import (
    parse_ses_token,
    rewrite_message_id,
    send,
)

SES_TOKEN = "010f01a8e6c8e7d2-9eaeb4d6-fcb5-4b04-bc89-3d8e36f5db7e-000000"


class _FakeSMTPBase:
    """Records every smtplib call. Subclasses override `data` to set the response.

    ``refuse`` maps an RCPT TO address to the ``(code, reply)`` the fake
    answers with; addresses not listed are accepted with 250. ``body``
    keeps the exact bytes handed to ``data()`` for wire assertions.
    """

    def __init__(self, host: str, port: int, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list = []
        self.refuse: dict = {}

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self) -> None:
        self.calls.append(("starttls",))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def mail(self, addr: str) -> tuple:
        self.calls.append(("mail", addr))
        return (250, b"OK")

    def rcpt(self, addr: str) -> tuple:
        self.calls.append(("rcpt", addr))
        if addr in self.refuse:
            return self.refuse[addr]
        return (250, b"OK")

    def data(self, body: bytes) -> tuple:
        self.body = body  # exact wire bytes, for line-ending assertions
        self.calls.append(("data", len(body)))
        return (250, b"OK")

    def quit(self) -> None:
        self.calls.append(("quit",))


class _FakeSESSMTP(_FakeSMTPBase):
    """Returns a SES-shaped post-DATA token."""

    def data(self, body: bytes) -> tuple:
        super().data(body)
        return (250, f"Ok {SES_TOKEN}".encode())


class _FakeGmailSMTP(_FakeSMTPBase):
    """Returns the Gmail-shaped post-DATA reply."""

    def data(self, body: bytes) -> tuple:
        super().data(body)
        return (
            250,
            b"2.0.0 OK 1234567890 ab123456abc12345abc12.100 - gsmtp",
        )


def _build_msg() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Sender <sender@example.com>"
    msg["To"] = "alice@x.com, Bob <bob@y.com>"
    msg["Cc"] = "carol@z.com"
    msg["Bcc"] = "secret@hidden.com"
    msg["Subject"] = "test"
    msg["Message-ID"] = "<original@local>"
    msg.set_content("body text")
    return msg


class TestParseSesToken:
    def test_matches_real_ses_token(self):
        assert parse_ses_token(f"Ok {SES_TOKEN}".encode()) == SES_TOKEN

    def test_rejects_postfix_response(self):
        assert parse_ses_token(b"Ok: queued as ABC123") is None

    def test_rejects_gmail_response(self):
        assert parse_ses_token(b"2.0.0 OK 1234567890 ab12345.gsmtp") is None

    def test_handles_none(self):
        assert parse_ses_token(None) is None

    def test_handles_str_input(self):
        assert parse_ses_token(f"Ok {SES_TOKEN}") == SES_TOKEN


class TestRewriteMessageId:
    def test_replaces_existing(self):
        original = _build_msg().as_bytes()
        new_bytes = rewrite_message_id(original, "<replaced@x>")
        assert b"<replaced@x>" in new_bytes
        assert b"<original@local>" not in new_bytes

    def test_adds_when_missing(self):
        msg = EmailMessage()
        msg["From"] = "a@b"
        msg["To"] = "c@d"
        msg.set_content("x")
        result = rewrite_message_id(msg.as_bytes(), "<new@x>")
        assert b"<new@x>" in result


class TestSendBccHandling:
    def test_bcc_in_rcpt_to_but_stripped_from_bytes(self):
        ses = SmtpConfig(
            host="email-smtp.example.com",
            port=587,
            username="AKIA",
            password="x",
            rewrite_msgid_from_response=True,
        )
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSESSMTP(host, port, timeout)
            captured.append(f)
            return f

        fcc_bytes, result = send(_build_msg(), ses, transport=factory)

        rcpts = [c[1] for c in captured[0].calls if c[0] == "rcpt"]
        assert "secret@hidden.com" in rcpts
        assert "secret@hidden.com" in result["accepted_recipients"]
        assert b"Bcc" not in fcc_bytes
        assert b"secret@hidden.com" not in fcc_bytes


class TestSendStartTLS:
    def test_starttls_invoked_on_587(self):
        smtp = SmtpConfig(host="smtp.example.com", port=587, username="u", password="p")
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        send(_build_msg(), smtp, transport=factory)
        assert any(c[0] == "starttls" for c in captured[0].calls)

    def test_no_starttls_on_other_ports(self):
        smtp = SmtpConfig(host="smtp.example.com", port=25)
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        send(_build_msg(), smtp, transport=factory)
        assert not any(c[0] == "starttls" for c in captured[0].calls)


class TestSesRewriteRoundTrip:
    def test_ses_rewrites_message_id(self):
        ses = SmtpConfig(
            host="email-smtp.example.com",
            port=587,
            username="AKIA",
            password="x",
            rewrite_msgid_from_response=True,
        )
        fcc, result = send(_build_msg(), ses, transport=_FakeSESSMTP)
        expected = f"<{SES_TOKEN}@email.amazonses.com>"
        assert result["message_id_sent"] == expected
        assert result["message_id_local"] == "<original@local>"
        assert expected.encode() in fcc

    def test_gmail_passthrough(self):
        gmail = SmtpConfig(
            host="smtp.gmail.com",
            port=587,
            username="u@gmail.com",
            password="p",
        )
        fcc, result = send(_build_msg(), gmail, transport=_FakeGmailSMTP)
        assert result["message_id_local"] == result["message_id_sent"]
        assert result["message_id_sent"] == "<original@local>"

    def test_false_positive_guard(self):
        """Even if rewrite is wrongly enabled on a non-SES smarthost, the
        regex shape rejects Gmail-style replies so no rewrite happens.
        """
        forced = SmtpConfig(
            host="smtp.gmail.com",
            port=587,
            username="u@gmail.com",
            password="p",
            rewrite_msgid_from_response=True,
        )
        fcc, result = send(_build_msg(), forced, transport=_FakeGmailSMTP)
        assert result["message_id_sent"] == "<original@local>"


class TestRefusedRecipients:
    """RCPT refusals are surfaced, not silently dropped (issue #62)."""

    def _refusing_factory(self, refuse: dict, captured: list):
        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.refuse = refuse
            captured.append(f)
            return f

        return factory

    def test_refused_recipient_reported_with_code_and_reply(self):
        captured: list = []
        factory = self._refusing_factory(
            {"bob@y.com": (550, b"5.1.1 user unknown")}, captured
        )
        _, result = send(_build_msg(), SmtpConfig(host="h"), transport=factory)

        assert result["refused_recipients"] == [
            {"addr": "bob@y.com", "code": 550, "reply": "5.1.1 user unknown"}
        ]
        assert "bob@y.com" not in result["accepted_recipients"]
        assert "alice@x.com" in result["accepted_recipients"]
        # DATA still ran for the accepted subset.
        assert any(c[0] == "data" for c in captured[0].calls)

    def test_no_refusals_yields_empty_list(self):
        captured: list = []
        factory = self._refusing_factory({}, captured)
        _, result = send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert result["refused_recipients"] == []

    def test_all_refused_raises_before_data(self):
        captured: list = []
        refuse = {
            "alice@x.com": (550, b"no"),
            "bob@y.com": (550, b"no"),
            "carol@z.com": (550, b"no"),
            "secret@hidden.com": (550, b"no"),
        }
        factory = self._refusing_factory(refuse, captured)
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        # Nothing was transmitted: the failure fired before DATA.
        assert not any(c[0] == "data" for c in captured[0].calls)


class TestWireLineEndings:
    """DATA and FCC bytes are CRLF-normalised (RFC 5321 section 2.3.8)."""

    @staticmethod
    def _assert_crlf_only(raw: bytes) -> None:
        stripped = raw.replace(b"\r\n", b"")
        assert b"\n" not in stripped, "bare LF reached the wire"
        assert b"\r" not in stripped, "bare CR reached the wire"

    def test_data_payload_has_no_bare_lf(self):
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        msg = _build_msg()
        fcc_bytes, _ = send(msg, SmtpConfig(host="h"), transport=factory)

        body = captured[0].body
        assert b"\r\n" in body
        self._assert_crlf_only(body)
        # The FCC copy is the same wire bytes: also CRLF.
        self._assert_crlf_only(fcc_bytes)

    def test_fcc_bytes_stay_crlf_after_ses_rewrite(self):
        ses = SmtpConfig(
            host="email-smtp.example.com",
            port=587,
            username="AKIA",
            password="x",
            rewrite_msgid_from_response=True,
        )
        fcc_bytes, result = send(_build_msg(), ses, transport=_FakeSESSMTP)
        assert result["message_id_sent"] == f"<{SES_TOKEN}@email.amazonses.com>"
        self._assert_crlf_only(fcc_bytes)


class TestSocketTimeout:
    """The transport factory gets a bounded timeout, matching the IMAP side."""

    def test_factory_receives_ten_second_timeout(self):
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert captured[0].timeout == 10


class TestRecipientHeaderParsing:
    """Recipient headers are parsed with getaddresses, not split on commas."""

    def test_quoted_display_name_with_comma_yields_one_rcpt(self):
        msg = EmailMessage()
        msg["From"] = "Sender <sender@example.com>"
        msg["To"] = '"Smith, John" <js@x.com>'
        msg["Message-ID"] = "<original@local>"
        msg.set_content("body")
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        _, result = send(msg, SmtpConfig(host="h"), transport=factory)

        rcpts = [c[1] for c in captured[0].calls if c[0] == "rcpt"]
        assert rcpts == ["js@x.com"]
        assert result["accepted_recipients"] == ["js@x.com"]


class TestSendValidation:
    def test_no_from_raises(self):
        msg = EmailMessage()
        msg["To"] = "x@y"
        msg["Message-ID"] = "<a@b>"
        msg.set_content("x")
        with pytest.raises(ValueError, match="no From header"):
            send(msg, SmtpConfig(host="h"), transport=_FakeSMTPBase)

    def test_no_recipients_raises(self):
        msg = EmailMessage()
        msg["From"] = "x@y"
        msg["Message-ID"] = "<a@b>"
        msg.set_content("x")
        with pytest.raises(ValueError, match="no To/Cc/Bcc recipients"):
            send(msg, SmtpConfig(host="h"), transport=_FakeSMTPBase)
