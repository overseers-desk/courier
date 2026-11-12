"""Tests for ``resend``: a stored message sent unchanged to new recipients.

Covers the Resent-* block built over the raw bytes, the transport's
raw-bytes send (envelope given, message untouched), and the CLI verb:
preview without ``--send``, route resolution, and the send result.
"""

import json
import re
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import SmtpConfig
from courier.imap_client import AppendResult
from courier.smtp_client import make_resent
from courier.smtp_transport import send_raw
from tests.test_send_draft import _cfg_with_identities
from tests.test_smtp_transport import _factory

runner = CliRunner()

# Folded header, tab continuation, an over-long line and a Bcc: the
# shapes a parse-and-reserialise round trip is liable to rewrite.
STORED = (
    b"Received: from mx.example.com\r\n\tby mx.x.com; Mon, 2 Mar 2026 10:00:00 +1000\r\n"
    b"From: Alice <alice@x.com>\r\n"
    b"To: recipient@y.com\r\n"
    b"Bcc: secret@hidden.com,\r\n auditor@hidden.com\r\n"
    b"Subject: " + b"long " * 30 + b"\r\n"
    b"Message-ID: <orig@x.com>\r\n"
    b"\r\n"
    b"Body line\r\nBcc: this body line stays\r\n"
)


def _without_bcc(raw: bytes) -> bytes:
    return re.sub(rb"Bcc: secret@hidden.com,\r?\n auditor@hidden.com\r?\n", b"", raw)


class TestMakeResent:
    def test_prepends_the_resent_block_and_keeps_the_rest(self):
        out = make_resent(STORED, "Alice <alice@x.com>", ["carol@z.com"])
        head, rest = out.split(b"Received:", 1)
        assert b"Received:" + rest == _without_bcc(STORED)
        names = re.findall(rb"^([\w-]+):", head, re.M)
        assert names == [
            b"Resent-From",
            b"Resent-To",
            b"Resent-Date",
            b"Resent-Message-ID",
        ]
        assert b"Resent-To: carol@z.com\r\n" in head

    def test_bcc_line_and_its_continuation_are_dropped(self):
        out = make_resent(STORED, "alice@x.com", ["carol@z.com"])
        assert b"secret@hidden.com" not in out
        assert b"auditor@hidden.com" not in out
        assert b"Bcc: this body line stays" in out

    def test_lf_only_input_keeps_its_bytes(self):
        raw = STORED.replace(b"\r\n", b"\n")
        out = make_resent(raw, "alice@x.com", ["carol@z.com"])
        assert out.endswith(_without_bcc(raw))


class TestSendRaw:
    def test_envelope_comes_from_arguments_and_bytes_are_untouched(self):
        captured: list = []
        cfg = SmtpConfig(host="smtp.x.com", port=587, username="u", password="p")
        raw = make_resent(STORED, "alice@x.com", ["carol@z.com"])
        fcc_bytes, result = send_raw(
            raw, "alice@x.com", ["carol@z.com"], cfg, transport=_factory(captured)
        )
        calls = captured[0].calls
        assert ("mail", "alice@x.com", ()) in calls
        assert [c[1] for c in calls if c[0] == "rcpt"] == ["carol@z.com"]
        assert captured[0].body == raw
        assert fcc_bytes == raw
        assert result["accepted_recipients"] == ["carol@z.com"]
        assert result["message_id_local"].startswith("<")
        assert result["message_id_local"] != "<orig@x.com>"


def _client(raw: bytes = STORED) -> MagicMock:
    client = MagicMock()
    client.fetch_raw.return_value = {"raw": raw, "subject": "s"}
    client.resolve_sent_folder.side_effect = lambda configured=None: (
        configured if configured is not None else "Sent"
    )
    client.append_raw.return_value = AppendResult(uid=7, uidvalidity=1)
    return client


def _invoke(args: list, client: MagicMock):
    sent: list = []

    def fake_send_raw(raw, sender, recipients, smtp_cfg, transport=None):
        sent.append((raw, sender, recipients))
        return raw, {
            "message_id_local": "<r@x.com>",
            "message_id_sent": "<r@x.com>",
            "smtp_response": "OK",
            "accepted_recipients": list(recipients),
            "refused_recipients": [],
        }

    with (
        patch("courier.__main__.load_config", return_value=_cfg_with_identities()),
        patch("courier.__main__._make_client", return_value=client),
        patch("courier.smtp_transport.send_raw", side_effect=fake_send_raw),
        patch("courier.smtp_transport.send") as send_mock,
    ):
        result = runner.invoke(app, ["resend", *args])
    assert not send_mock.called
    return result, sent


class TestResendCli:
    def test_send_as_the_messages_own_from(self):
        client = _client()
        result, sent = _invoke(
            ["-f", "Sent", "-u", "42", "--to", "carol@z.com", "--send"], client
        )
        assert result.exit_code == 0, result.output
        raw, sender, recipients = sent[0]
        assert sender == "alice@x.com"
        assert recipients == ["carol@z.com"]
        assert raw.startswith(b"Resent-From: alice@x.com\r\n")
        assert raw.endswith(_without_bcc(STORED))
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["identity"] == "alice@x.com"
        assert out["fcc_uid"] == 7

    def test_identity_names_the_resender(self):
        result, sent = _invoke(
            ["-f", "Sent", "-u", "42", "--to", "carol@z.com", "-i", "bob", "--send"],
            _client(),
        )
        assert result.exit_code == 0, result.output
        raw, sender, _ = sent[0]
        assert sender == "bob@x.com"
        assert raw.startswith(b"Resent-From: bob@x.com\r\n")
        assert b"From: Alice <alice@x.com>\r\n" in raw

    def test_without_send_prints_a_preview_and_sends_nothing(self):
        result, sent = _invoke(
            ["-f", "Sent", "-u", "42", "--to", "carol@z.com"], _client()
        )
        assert result.exit_code == 0, result.output
        assert sent == []
        out = json.loads(result.output)
        assert out["status"] == "preview"
        assert out["resent_from"] == "alice@x.com"
        assert out["to"] == ["carol@z.com"]
        assert "--send" in out["message"]

    def test_foreign_from_without_a_route_names_dash_i(self):
        raw = STORED.replace(b"Alice <alice@x.com>", b"stranger@else.com")
        result, sent = _invoke(
            ["-f", "INBOX", "-u", "42", "--to", "carol@z.com", "--send"], _client(raw)
        )
        assert result.exit_code == 1
        assert sent == []
        assert "-i" in result.output

    def test_redacted_message_is_refused(self):
        client = _client(b"")
        client.fetch_raw.return_value = {"raw": b"", "redacted_by": "r1"}
        result, sent = _invoke(
            ["-f", "INBOX", "-u", "42", "--to", "carol@z.com", "--send"], client
        )
        assert result.exit_code == 1
        assert sent == []
        assert "redact" in result.output


def test_top_level_help_lists_resend_in_full():
    result = runner.invoke(app, ["--help"])
    assert "Resend an email unchanged to new recipients." in result.output
