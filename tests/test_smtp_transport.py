"""Tests for the SMTP transport: post-DATA capture, SES rewrite, Bcc handling.

The send() function is exercised through an injected fake SMTP class so that
no network is required. The fake records the sequence of calls and supplies
the post-DATA response that drives the SES Message-ID rewrite logic; DATA
itself runs through smtplib's own code over a scripted socket.
"""

import email
import re
import smtplib
from email.message import EmailMessage
from typing import Optional

import pytest

from courier.config import SmtpConfig
from courier.errors import CourierError, PermanentError, TransientError
from courier.models import EmailAddress
from courier.smtp_client import create_mime
from courier.smtp_transport import (
    parse_ses_token,
    rewrite_message_id,
    send,
)

SES_TOKEN = "010f01a8e6c8e7d2-9eaeb4d6-fcb5-4b04-bc89-3d8e36f5db7e-000000"


class _FakeSocket:
    """The server end of the socket a real smtplib session runs DATA over.

    send() drives DATA with smtplib's own ``docmd``, ``send`` and
    ``getreply``, so this socket stands where the network would: the
    library's reading of replies, and its handling of a read that times
    out, run for real. It answers DATA with ``data_reply`` and the
    end-of-data dot with ``final_reply``; a ``final_reply`` of ``None``
    makes that read time out, and ``b""`` closes the connection instead.
    ``upload_error``, when set, is raised by the first write of the
    message. ``log`` records each ``sendall`` and ``readline`` with the
    timeout in effect when it ran; ``wire`` keeps every byte sent after
    the 354.
    """

    def __init__(self, timeout, final_reply: Optional[bytes]):
        self.timeout = timeout
        self.data_reply = b"354 End data with <CR><LF>.<CR><LF>\r\n"
        self.final_reply = final_reply
        self.upload_error: Optional[BaseException] = None
        self.wire = b""
        self.log: list = []
        self._replies: list = []
        self._in_data = False

    def settimeout(self, value) -> None:
        self.timeout = value

    def sendall(self, data) -> None:
        data = bytes(data)
        self.log.append(("sendall", len(data), self.timeout))
        if not self._in_data:
            assert data == b"data\r\n", data
            self._replies.append(self.data_reply)
            self._in_data = self.data_reply.startswith(b"354")
            return
        if self.upload_error is not None:
            raise self.upload_error
        self.wire += data
        if self.wire.endswith(b"\r\n.\r\n"):
            self._in_data = False
            self._replies.append(self.final_reply)

    def makefile(self, mode: str) -> "_FakeSocket":
        return self

    def readline(self, limit: int = -1) -> bytes:
        self.log.append(("readline", self.timeout))
        reply = self._replies.pop(0)
        if reply is None:
            raise TimeoutError("timed out")
        return reply

    def close(self) -> None:
        pass


class _FakeSMTPBase(smtplib.SMTP):
    """A real smtplib session over a :class:`_FakeSocket`; records every call.

    EHLO, STARTTLS, login, MAIL, RCPT and QUIT are answered here without
    touching the socket; DATA runs through smtplib's own code over
    ``server``. ``refuse`` maps an RCPT TO address to the
    ``(code, reply)`` the fake answers with; addresses not listed are
    accepted with 250. ``features`` are the EHLO extensions the fake
    server lists, ``mail_reply`` is its answer to MAIL FROM, and
    ``final_reply`` is the reply line it sends after the message.
    ``body`` is the message as the server received it.
    """

    features = frozenset({"8bitmime"})
    mail_reply: tuple = (250, b"OK")
    final_reply: Optional[bytes] = b"250 OK\r\n"

    def __init__(self, host: str, port: int, timeout=None):
        super().__init__(local_hostname="fake.test", timeout=timeout)
        self.host = host
        self.port = port
        self.calls: list = []
        self.refuse: dict = {}
        self.server = _FakeSocket(timeout, self.final_reply)
        self.sock = self.server  # type: ignore[assignment]

    def ehlo(self, name: str = "") -> tuple:
        self.calls.append(("ehlo",))
        return (250, b"OK")

    def starttls(self, *args, **kwargs) -> tuple:
        self.calls.append(("starttls",))
        return (220, b"OK")

    def login(self, user: str, password: str, **kwargs) -> tuple:
        self.calls.append(("login", user, password))
        return (235, b"OK")

    def has_extn(self, opt: str) -> bool:
        return opt.lower() in self.features

    def mail(self, sender: str, options=()) -> tuple:
        self.calls.append(("mail", sender, tuple(options)))
        return self.mail_reply

    def rcpt(self, recip: str, options=()) -> tuple:
        self.calls.append(("rcpt", recip))
        if recip in self.refuse:
            return self.refuse[recip]
        return (250, b"OK")

    def putcmd(self, cmd: str, args: str = "") -> None:
        self.calls.append((cmd.lower(),))
        super().putcmd(cmd, args)

    def quit(self) -> tuple:
        self.calls.append(("quit",))
        return (221, b"Bye")

    @property
    def body(self) -> bytes:
        """The message as received: end-of-data dot dropped, dot-stuffing undone."""
        assert self.server.wire.endswith(b"\r\n.\r\n")
        return re.sub(rb"(?m)^\.\.", b".", self.server.wire[:-3])


class _FakeSESSMTP(_FakeSMTPBase):
    """Returns a SES-shaped post-DATA token."""

    final_reply = f"250 Ok {SES_TOKEN}\r\n".encode()


class _FakeGmailSMTP(_FakeSMTPBase):
    """Returns the Gmail-shaped post-DATA reply."""

    final_reply = b"250 2.0.0 OK 1234567890 ab123456abc12345abc12.100 - gsmtp\r\n"


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


class TestSenderRefused:
    """A non-2xx reply to MAIL FROM ends the send (RFC 5321 section 4.1.1.2).

    smtplib's ``mail()`` returns the reply instead of raising it, so an
    unchecked refusal would carry on to RCPT TO, and the user would see
    the server's ``503 need MAIL`` in place of its real reason.
    """

    @pytest.mark.parametrize(
        "reply", [(550, b"5.7.1 sender blocked"), (451, b"4.3.0 try later")]
    )
    def test_refused_sender_raises_before_rcpt_and_data(self, reply):
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.mail_reply = reply
            captured.append(f)
            return f

        with pytest.raises(smtplib.SMTPSenderRefused) as info:
            send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert info.value.smtp_code == reply[0]
        assert info.value.smtp_error == reply[1]
        assert info.value.sender == "sender@example.com"
        # Nothing was transmitted: the failure fired before RCPT TO.
        assert not any(c[0] in ("rcpt", "data") for c in captured[0].calls)


class TestEightBitDeclaration:
    """8-bit content is declared with BODY=8BITMIME (RFC 6152), or refused."""

    @staticmethod
    def _capture(captured: list, features=frozenset({"8bitmime"})):
        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.features = features
            captured.append(f)
            return f

        return factory

    @staticmethod
    def _eight_bit_msg() -> EmailMessage:
        msg = _build_msg()
        msg.set_content("付款安排已确认。\n")
        return msg

    def test_eight_bit_message_declares_8bitmime(self):
        captured: list = []
        send(
            self._eight_bit_msg(),
            SmtpConfig(host="h"),
            transport=self._capture(captured),
        )
        assert ("mail", "sender@example.com", ("BODY=8BITMIME",)) in captured[0].calls

    def test_ascii_message_declares_nothing(self):
        captured: list = []
        send(_build_msg(), SmtpConfig(host="h"), transport=self._capture(captured))
        assert ("mail", "sender@example.com", ()) in captured[0].calls

    def test_eight_bit_message_refused_when_server_lacks_8bitmime(self):
        captured: list = []
        factory = self._capture(captured, features=frozenset())
        with pytest.raises(smtplib.SMTPNotSupportedError, match="8BITMIME"):
            send(self._eight_bit_msg(), SmtpConfig(host="h"), transport=factory)
        assert not any(c[0] in ("mail", "data") for c in captured[0].calls)

    def test_attached_email_reaches_the_wire_intact(self, tmp_path):
        """The body arrives byte for byte; headers arrive equal once unfolded.

        The generator refolds header lines over 78 characters at existing
        whitespace, which RFC 5322 §2.2.3 unfolding undoes.
        """
        eml_bytes = (
            b"Received: from mx.example.com by mx2.example.com\r\n"
            b"\twith ESMTPS id abc123; Sun, 18 Oct 2026 16:54:34 -0700\r\n"
            b"ARC-Seal: i=1; a=rsa-sha256; t=1760831674; cv=none; d=example.com; "
            + b"b="
            + b"A" * 120
            + b"\r\n"
            b"From: QRO <a@example.com>\r\n"
            b"Subject: Payment arrangement\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b"\r\n" + "付款安排已确认。\r\n".encode("utf-8")
        )
        eml = tmp_path / "forwarded.eml"
        eml.write_bytes(eml_bytes)
        msg = create_mime(
            from_addr=EmailAddress(name="", address="sender@example.com"),
            to=[EmailAddress(name="", address="alice@x.com")],
            subject="s",
            body="see attached",
            attachments=[str(eml)],
        )
        captured: list = []
        send(msg, SmtpConfig(host="h"), transport=self._capture(captured))

        wire = captured[0].body
        assert eml_bytes.split(b"\r\n\r\n", 1)[1] in wire
        sent = email.message_from_bytes(wire).get_payload(1).get_payload(0)
        original = email.message_from_bytes(eml_bytes)

        def unfold(m):
            return [(k, re.sub(r"\r?\n", "", v)) for k, v in m.items()]

        assert unfold(sent) == unfold(original)


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


class TestDataReplyWait:
    """Only the wait for the reply to the message gets RFC 5321's ten minutes.

    Section 4.5.3.2.6 gives a server 10 minutes to answer the end-of-data
    dot, which one that scans the message before replying may use; every
    other read and write, each chunk of the upload included, keeps the
    10-second socket timeout. A reply that never comes leaves the outcome
    unknown, since the server may have queued the message.
    """

    @staticmethod
    def _capture(captured: list, final_reply: Optional[bytes] = b"250 OK\r\n"):
        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.server.final_reply = final_reply
            captured.append(f)
            return f

        return factory

    @staticmethod
    def _large_msg() -> EmailMessage:
        msg = _build_msg()
        msg.set_content(("y" * 70 + "\n") * 4000)
        return msg

    def test_only_the_reply_to_the_message_waits_600_seconds(self):
        captured: list = []
        send(self._large_msg(), SmtpConfig(host="h"), transport=self._capture(captured))

        log = captured[0].server.log
        reads = [entry for entry in log if entry[0] == "readline"]
        writes = [entry for entry in log if entry[0] == "sendall"]
        # The 354 is read under the session timeout; only the last read,
        # the reply to the end-of-data dot, waits 600 seconds.
        assert reads == [("readline", 10), ("readline", 600)]
        # The DATA command plus an upload of several writes, each bounded
        # on its own by the session timeout.
        assert len(writes) > 2
        assert all(timeout == 10 for _op, _size, timeout in writes)
        # QUIT and anything after it are back under the session timeout.
        assert captured[0].server.timeout == 10

    def test_line_starting_with_a_dot_is_stuffed_on_the_wire(self):
        msg = _build_msg()
        msg.set_content(".hidden line\n..two dots\n")
        captured: list = []
        send(msg, SmtpConfig(host="h"), transport=self._capture(captured))

        assert b"\r\n..hidden line\r\n...two dots\r\n" in captured[0].server.wire
        assert b"\r\n.hidden line\r\n..two dots\r\n" in captured[0].body

    def test_data_command_refused_raises_before_the_upload(self):
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.server.data_reply = b"554 5.5.1 no valid recipients\r\n"
            captured.append(f)
            return f

        with pytest.raises(smtplib.SMTPDataError) as info:
            send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert info.value.smtp_code == 554
        assert captured[0].server.wire == b""

    def test_message_refused_after_upload_raises_data_error(self):
        captured: list = []
        factory = self._capture(captured, final_reply=b"554 5.7.1 rejected\r\n")
        with pytest.raises(smtplib.SMTPDataError) as info:
            send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert info.value.smtp_code == 554

    @pytest.mark.parametrize(
        "final_reply, cause",
        [(None, "no reply came within 600 seconds"), (b"", "connection failed")],
        ids=["timeout", "connection-closed"],
    )
    def test_lost_reply_leaves_the_outcome_unknown(self, final_reply, cause):
        captured: list = []
        factory = self._capture(captured, final_reply=final_reply)
        with pytest.raises(CourierError) as info:
            send(_build_msg(), SmtpConfig(host="smtp.example.com"), transport=factory)

        # Neither retryable (a retry could deliver a duplicate) nor
        # permanent (the server may have accepted the message).
        assert not isinstance(info.value, (TransientError, PermanentError))
        text = str(info.value)
        assert text.startswith("send outcome unknown: ")
        assert "smtp.example.com" in text
        assert cause in text
        assert "may have accepted it" in text
        assert "Check the Sent folder before re-sending" in text
        assert "<original@local>" in text
        # The whole message, end-of-data dot included, went out first.
        assert captured[0].server.wire.endswith(b"\r\n.\r\n")
        assert captured[0].server.log[-1] == ("readline", 600)

    def test_upload_failure_is_a_plain_send_failure(self):
        """The server never saw the end-of-data dot, so it cannot have
        accepted the message: an ordinary failure, not an unknown outcome."""
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            f.server.upload_error = TimeoutError("timed out")
            captured.append(f)
            return f

        with pytest.raises(smtplib.SMTPServerDisconnected):
            send(_build_msg(), SmtpConfig(host="h"), transport=factory)
        assert not any(
            e[0] == "readline" and e[1] == 600 for e in captured[0].server.log
        )


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


class TestFromAddressParsing:
    """MAIL FROM takes the From address with parseaddr, not a split on ``<``."""

    @staticmethod
    def _mail_from(msg) -> str:
        captured: list = []

        def factory(host, port, timeout=None):
            f = _FakeSMTPBase(host, port, timeout)
            captured.append(f)
            return f

        send(msg, SmtpConfig(host="h"), transport=factory)
        return next(c[1] for c in captured[0].calls if c[0] == "mail")

    def test_display_name_containing_angle_bracket(self):
        msg = _build_msg()
        del msg["From"]
        msg["From"] = '"Acme <Billing>" <billing@acme.com>'
        assert self._mail_from(msg) == "billing@acme.com"

    def test_parsed_draft_with_angle_bracket_in_display_name(self):
        msg = email.message_from_bytes(
            b'From: "Acme <Billing>" <billing@acme.com>\r\n'
            b"To: alice@x.com\r\n"
            b"Message-ID: <draft@local>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        assert self._mail_from(msg) == "billing@acme.com"

    def test_bare_address(self):
        msg = _build_msg()
        del msg["From"]
        msg["From"] = "sender@example.com"
        assert self._mail_from(msg) == "sender@example.com"

    def test_from_without_an_address_is_refused_by_name(self):
        msg = _build_msg()
        del msg["From"]
        msg["From"] = "Billing <>"
        with pytest.raises(ValueError, match="From header 'Billing <>' holds no"):
            send(msg, SmtpConfig(host="h"), transport=_FakeSMTPBase)


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


class TestRepeatedRecipientHeaders:
    """A message whose To, Cc or Bcc field occurs twice, as a draft from
    another mail client can, is delivered to every address in every
    occurrence, and every Bcc occurrence is stripped from the wire copy."""

    RAW = (
        b"From: me@example.com\r\n"
        b"To: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Cc: c@example.com\r\n"
        b"Cc: d@example.com\r\n"
        b"Bcc: e@example.com\r\n"
        b"Bcc: f@example.com\r\n"
        b"Subject: s\r\n"
        b"\r\n"
        b"body\r\n"
    )

    def test_every_occurrence_reaches_rcpt_to(self):
        import email

        from courier.smtp_transport import _all_recipients

        msg = email.message_from_bytes(self.RAW)
        assert _all_recipients(msg) == [
            "a@example.com",
            "b@example.com",
            "c@example.com",
            "d@example.com",
            "e@example.com",
            "f@example.com",
        ]

    def test_every_bcc_occurrence_is_stripped(self):
        from courier.smtp_transport import _strip_bcc

        assert b"Bcc" not in _strip_bcc(self.RAW)
