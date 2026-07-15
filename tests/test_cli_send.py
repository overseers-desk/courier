"""CLI tests for ``compose --send`` and ``reply --send`` under the
two-mode design.

Mode A: ``--identity NAME`` resolves From, display name, IMAP block, SMTP,
and fcc from a configured ``[identity.NAME]``.
Mode B: ``--smtp NAME --from EMAIL [--name N] [--fcc IMAP:FOLDER]`` sends
through a named SMTP block with a free-form From; no ``[identity.*]`` is
consulted. The SMTP block must carry its own credentials.

Drafting (no ``--send``) keeps the previous convenience defaults.
"""

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import (
    CourierConfig,
    Identity,
    ImapBlock,
    SmtpConfig,
)
from courier.imap_client import AppendResult

runner = CliRunner()


def _cfg() -> CourierConfig:
    """Two-identity block on a Fastmail-style SMTP so FCC actually runs."""
    block = ImapBlock(
        host="imap.fastmail.com",
        port=993,
        username="login@x.com",
        password="p",
        default_smtp="fast",
    )
    return CourierConfig(
        imap_blocks={"acct": block},
        _default_imap="acct",
        identities={
            "primary": Identity(imap="acct", address="primary@x.com", name="Primary"),
            "alias": Identity(imap="acct", address="alias@x.com", name="Alias"),
        },
        smtp_blocks={
            "fast": SmtpConfig(
                host="smtp.fastmail.com",
                port=587,
                username="login@x.com",
                password="p",
            )
        },
    )


def _cfg_with_relay() -> CourierConfig:
    """Adds an SES-style relay block with its own credentials, plus a
    second IMAP block usable as an --fcc target.
    """
    cfg = _cfg()
    cfg.imap_blocks["work"] = ImapBlock(
        host="imap.work.com",
        port=993,
        username="login@work.com",
        password="p",
    )
    cfg.smtp_blocks["ses"] = SmtpConfig(
        host="email-smtp.eu-west-1.amazonaws.com",
        port=587,
        username="AKIA",
        password="ses-secret",
    )
    cfg.smtp_blocks["template"] = SmtpConfig(host="smtp.fastmail.com", port=587)
    return cfg


def _result() -> dict:
    return {
        "message_id_local": "<x@local>",
        "message_id_sent": "<x@local>",
        "smtp_response": "OK",
        "accepted_recipients": ["alice@y.com"],
        "refused_recipients": [],
    }


def _client(default_sent: str = "Sent") -> MagicMock:
    c = MagicMock()
    c.resolve_sent_folder.side_effect = lambda configured=None: (
        configured if configured is not None else default_sent
    )
    c.resolve_drafts_folder.return_value = "Drafts"
    c.append_raw.return_value = AppendResult(uid=999, uidvalidity=1)
    c.save_draft_mime.return_value = AppendResult(uid=42, uidvalidity=1)
    return c


class TestComposeSendModeA:
    """``--identity NAME``: resolves everything from the configured block."""

    def test_send_with_identity(self):
        cfg = _cfg()
        client = _client()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--subject",
                    "T",
                    "--send",
                    "--identity",
                    "alias",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["identity"] == "alias@x.com"
        assert out["fcc_folder"] == "Sent"
        from_hdr = str(captured[0].get("From"))
        assert "alias@x.com" in from_hdr
        assert "Alias" in from_hdr  # identity's display name preserved

    def test_unknown_identity_errors(self):
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--identity",
                    "ghost",
                ],
            )
        assert result.exit_code == 1
        assert "ghost" in (result.output + (result.stderr or ""))
        send_mock.assert_not_called()

    def test_refused_recipients_downgrade_status_to_partial(self):
        """A send the server part-refused at RCPT must not report
        status "success"; the refusals ride along in the envelope."""
        cfg = _cfg()
        client = _client()
        refused = [{"addr": "gone@y.com", "code": 550, "reply": "5.1.1 unknown"}]

        def fake_send(msg, smtp_cfg, transport=None):
            result = _result()
            result["refused_recipients"] = refused
            return (msg.as_bytes(), result)

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--to",
                    "gone@y.com",
                    "--body",
                    "hi",
                    "--subject",
                    "T",
                    "--send",
                    "--identity",
                    "alias",
                ],
            )
        assert result.exit_code == 0, result.output
        # result.stdout: the refusal warning goes to stderr.
        out = json.loads(result.stdout)
        assert out["status"] == "partial"
        assert out["refused_recipients"] == refused
        assert out["accepted_recipients"] == ["alice@y.com"]
        assert "gone@y.com" in result.stderr

    def test_fcc_failure_downgrades_status_to_partial(self):
        """The message left but the Sent copy failed: status "partial"
        with fcc_error present, per the result-envelope contract."""
        from courier.errors import PermanentError

        cfg = _cfg()
        client = _client()
        client.append_raw.side_effect = PermanentError("mailbox full")

        def fake_send(msg, smtp_cfg, transport=None):
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--subject",
                    "T",
                    "--send",
                    "--identity",
                    "alias",
                ],
            )
        assert result.exit_code == 0, result.output
        # result.stdout: the FCC warning goes to stderr.
        out = json.loads(result.stdout)
        assert out["status"] == "partial"
        assert out["fcc_error"] == "mailbox full"
        assert out["fcc_uid"] is None

    def test_no_save_sent_skips_fcc(self):
        cfg = _cfg()
        client = _client()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                    "--no-save-sent",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["fcc_folder"] is None
        client.append_raw.assert_not_called()


class TestComposeSendModeB:
    """``--smtp NAME --from EMAIL``: free-form From through a named SMTP."""

    def test_send_with_smtp_and_from_no_fcc(self):
        cfg = _cfg_with_relay()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append((msg, smtp_cfg))
            return (msg.as_bytes(), _result())

        # No --fcc, no BCC self-copy => --allow-no-copy required.
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["identity"] == "one-off@example.com"
        assert out["fcc_folder"] is None
        make_client_mock.assert_not_called()  # mode B + no --fcc => no IMAP
        # The SMTP block passed to send() is the SES one, with its own creds.
        assert captured[0][1].host == "email-smtp.eu-west-1.amazonaws.com"
        assert captured[0][1].username == "AKIA"

    def test_smtp_no_fcc_without_override_refuses(self):
        """Free-form --smtp send with no --fcc has no IMAP block to file
        into, so no copy can be retained. The SMTP block's save_sent
        preference (true for any non-Gmail relay like SES) must not be
        read as an FCC that will happen: with no self-BCC and no
        --allow-no-copy, the send is refused."""
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()
        make_client_mock.assert_not_called()
        err = result.output + (result.stderr or "")
        assert "--allow-no-copy" in err

    def test_smtp_no_fcc_self_bcc_sends(self):
        """A free-form --smtp send with no --fcc still has a record when the
        BCC includes the sender's own address: no --allow-no-copy needed."""
        cfg = _cfg_with_relay()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append((msg, smtp_cfg))
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--bcc",
                    "one-off@example.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["fcc_folder"] is None
        make_client_mock.assert_not_called()
        bcc_hdr = str(captured[0][0].get("Bcc", "") or "")
        assert "one-off@example.com" in bcc_hdr

    def test_bcc_only_send_without_to_or_cc(self):
        """--to is optional: a send carrying only --bcc (no visible
        recipient, e.g. to a distribution list) is allowed. The self-BCC
        also satisfies the copy-retention guard."""
        cfg = _cfg_with_relay()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--bcc",
                    "one-off@example.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                ],
            )
        assert result.exit_code == 0, result.output
        # No To/Cc headers; only Bcc carries the recipient.
        assert captured[0].get("To") is None
        assert captured[0].get("Cc") is None
        bcc_hdr = str(captured[0].get("Bcc", "") or "")
        assert "one-off@example.com" in bcc_hdr
        make_client_mock.assert_not_called()

    def test_no_recipient_at_all_rejected(self):
        """No --to, --cc, or --bcc: nothing to send to, rejected before any
        SMTP work."""
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 2
        send_mock.assert_not_called()
        err = result.output + (result.stderr or "")
        assert "at least one recipient" in err

    def test_send_with_smtp_from_fcc_opens_named_block(self):
        cfg = _cfg_with_relay()
        client = _client()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client) as mc,
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--fcc",
                    "work:Archive",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["fcc_folder"] == "Archive"
        # _make_client was called with imap_override="work" for FCC.
        mc.assert_called_once_with(imap_override="work")
        client.append_raw.assert_called_once()

    def test_send_with_name_override(self):
        cfg = _cfg_with_relay()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--name",
                    "One Off",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 0, result.output
        from_hdr = str(captured[0].get("From"))
        assert "one-off@example.com" in from_hdr
        assert "One Off" in from_hdr

    def test_smtp_without_from_errors(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--smtp",
                    "ses",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()

    def test_credential_less_smtp_rejected(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--smtp",
                    "template",
                    "--from",
                    "one-off@example.com",
                ],
            )
        assert result.exit_code == 1
        assert "template" in (result.output + (result.stderr or ""))
        send_mock.assert_not_called()

    def test_invalid_display_name_rejected(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--name",
                    "Bad, Name",
                ],
            )
        assert result.exit_code == 1
        assert "RFC 5322" in (result.output + (result.stderr or ""))
        send_mock.assert_not_called()

    def test_fcc_unknown_imap_block_rejected(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--fcc",
                    "ghost:Sent",
                ],
            )
        assert result.exit_code == 1
        assert "ghost" in (result.output + (result.stderr or ""))
        send_mock.assert_not_called()

    def test_fcc_missing_colon_rejected(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--smtp",
                    "ses",
                    "--from",
                    "one-off@example.com",
                    "--fcc",
                    "Sent",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()


class TestComposeSendNoRoute:
    def test_no_identity_no_smtp_errors(self):
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                ],
            )
        assert result.exit_code == 1
        msg = result.output + (result.stderr or "")
        assert "--identity" in msg and "--smtp" in msg
        send_mock.assert_not_called()

    def test_identity_and_smtp_mutually_exclusive(self):
        cfg = _cfg_with_relay()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "a@y.com",
                    "--body",
                    "x",
                    "--send",
                    "--identity",
                    "primary",
                    "--smtp",
                    "ses",
                ],
            )
        assert result.exit_code == 1
        assert "mutually exclusive" in (result.output + (result.stderr or ""))
        send_mock.assert_not_called()


class TestComposeNonSendIgnoresModeFlags:
    def test_send_and_output_mutually_exclusive(self):
        cfg = _cfg()
        with patch("courier.__main__.load_config", return_value=cfg):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "-o",
                    "-",
                ],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in (result.output or "") + (result.stderr or "")

    def test_default_save_draft_uses_identity_from(self):
        """Drafting still uses the legacy default-resolution path: the
        first identity on the [imap.NAME] block (--imap) is the From.
        """
        cfg = _cfg()
        client = _client()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
        ):
            result = runner.invoke(
                app,
                ["compose", "--to", "alice@y.com", "--body", "hi"],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["identity"] == "primary@x.com"
        client.save_draft_mime.assert_called_once()

    def test_mode_flags_rejected_in_drafting(self):
        cfg = _cfg()
        with patch("courier.__main__.load_config", return_value=cfg):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 1
        assert "--send" in (result.output + (result.stderr or ""))


class TestReplySend:
    @staticmethod
    def _parent_alias():
        from courier.models import Email, EmailAddress, EmailContent

        return Email(
            uid=10,
            from_=EmailAddress(name="Sender", address="sender@y.com"),
            to=[EmailAddress(name="", address="alias@x.com")],
            subject="hi",
            content=EmailContent(text="body", html=None),
            message_id="<parent@y.com>",
        )

    @staticmethod
    def _parent_unrelated():
        from courier.models import Email, EmailAddress, EmailContent

        return Email(
            uid=10,
            from_=EmailAddress(name="Sender", address="sender@y.com"),
            to=[EmailAddress(name="", address="someone-else@y.com")],
            subject="hi",
            content=EmailContent(text="body", html=None),
            message_id="<parent@y.com>",
        )

    def test_reply_send_recipient_match_succeeds(self):
        cfg = _cfg()
        client = _client()
        client.fetch_email.return_value = self._parent_alias()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "reply",
                    "-f",
                    "INBOX",
                    "-u",
                    "10",
                    "--body",
                    "thanks",
                    "--send",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["identity"] == "alias@x.com"
        from_hdr = str(captured[0].get("From"))
        assert "alias@x.com" in from_hdr

    def test_reply_send_no_recipient_match_errors(self):
        """The silent-default closure: no recipient match plus no --identity
        and no --smtp must error rather than picking identities[0]."""
        cfg = _cfg()
        client = _client()
        client.fetch_email.return_value = self._parent_unrelated()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "reply",
                    "-f",
                    "INBOX",
                    "-u",
                    "10",
                    "--body",
                    "thanks",
                    "--send",
                ],
            )
        assert result.exit_code == 1
        assert "no recipient" in (result.output + (result.stderr or "")).lower()
        send_mock.assert_not_called()

    def test_reply_send_with_explicit_identity(self):
        cfg = _cfg()
        client = _client()
        client.fetch_email.return_value = self._parent_alias()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "reply",
                    "-f",
                    "INBOX",
                    "-u",
                    "10",
                    "--body",
                    "thanks",
                    "--send",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["identity"] == "primary@x.com"

    def test_reply_drafting_recipient_match_unchanged(self):
        """Drafting (no --send) keeps the legacy fallback semantics."""
        cfg = _cfg()
        client = _client()
        client.fetch_email.return_value = self._parent_unrelated()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
        ):
            result = runner.invoke(
                app,
                [
                    "reply",
                    "-f",
                    "INBOX",
                    "-u",
                    "10",
                    "--body",
                    "thanks",
                ],
            )
        # No --send: falls back to identities[0] just like the old path.
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["identity"] == "primary@x.com"


class TestComposeSendFccVerification:
    """Pre-send Sent-folder verification (#22).

    Sends must verify the FCC target before opening SMTP and refuse to
    transmit when the target is missing, so the user is not silently left
    without a local copy of a message that has already gone out.
    """

    def test_picks_inbox_sent_without_configuration(self):
        """Dovecot-style server reporting only ``INBOX.Sent``: FCC lands there."""
        cfg = _cfg()
        client = _client(default_sent="INBOX.Sent")
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["fcc_folder"] == "INBOX.Sent"
        client.append_raw.assert_called_once()
        # Auto-discover path: configured arg is None.
        client.resolve_sent_folder.assert_called_once_with(configured=None)

    def test_no_sent_folder_exits_before_smtp(self):
        """No Sent-shaped folder anywhere: refuse to send, name the candidates."""
        cfg = _cfg()
        client = _client()
        client.resolve_sent_folder.side_effect = lambda configured=None: None
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 1
        # SMTP must not have been opened.
        send_mock.assert_not_called()
        client.append_raw.assert_not_called()
        # Error names the candidate tree so the user can configure.
        err = result.output + (result.stderr or "")
        assert "INBOX.Sent" in err
        assert "Sent Items" in err

    def test_configured_fcc_folder_must_exist(self):
        """A configured ``fcc`` folder that doesn't exist hard-fails by name.

        The auto-discover fallback must not silently rewrite away from a
        user-pinned value. The error message has to surface the
        configured value so the user knows what to fix.
        """
        cfg = _cfg()
        # Identity pins "Sent", but the server only has "INBOX.Sent" so
        # resolve_sent_folder(configured="Sent") returns None.
        cfg.identities["primary"] = Identity(
            imap="acct",
            address="primary@x.com",
            name="Primary",
            fcc="Sent",
        )
        client = _client()
        client.resolve_sent_folder.side_effect = lambda configured=None: (
            None if configured == "Sent" else "INBOX.Sent"
        )
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()
        client.append_raw.assert_not_called()
        err = result.output + (result.stderr or "")
        assert "'Sent'" in err  # quoted configured value


class TestComposeSendCopyRetention:
    """FCC and BCC are independent. A ``--bcc`` (command line) or
    ``[identity.NAME].bcc`` never turns FCC off; the explicit lever for
    that is ``--no-save-sent`` (or ``fcc = false`` on the identity). A
    send must still retain a copy: via FCC, or via a self-inclusive BCC."""

    def test_bcc_does_not_skip_fcc(self):
        """A command-line --bcc adds a recipient but leaves FCC running."""
        cfg = _cfg()
        client = _client()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--bcc",
                    "primary@x.com",  # the sender's own address
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        # FCC still runs: the Sent copy is filed even with a self-BCC.
        assert out["fcc_folder"] == "Sent"
        client.append_raw.assert_called_once()

    def test_no_save_sent_with_self_bcc_sends(self):
        """--no-save-sent skips FCC; a self-BCC keeps the copy, so no
        --allow-no-copy is needed and no IMAP connection is opened."""
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--bcc",
                    "primary@x.com",  # the sender's own address
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                    "--no-save-sent",
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["fcc_folder"] is None
        make_client_mock.assert_not_called()

    def test_no_save_sent_third_party_bcc_refuses(self):
        """--no-save-sent off FCC, and a BCC to an auditor is not a
        self-copy: refuse."""
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--bcc",
                    "audit@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                    "--no-save-sent",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()
        err = result.output + (result.stderr or "")
        assert "--allow-no-copy" in err

    def test_no_save_sent_third_party_bcc_with_allow_no_copy_sends(self):
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch(
                "courier.smtp_transport.send",
                return_value=(b"raw", _result()),
            ) as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--bcc",
                    "audit@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                    "--no-save-sent",
                    "--allow-no-copy",
                ],
            )
        assert result.exit_code == 0, result.output
        assert send_mock.called
        make_client_mock.assert_not_called()

    def test_no_save_sent_without_bcc_or_override_refuses(self):
        cfg = _cfg()
        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.smtp_transport.send") as send_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "primary",
                    "--no-save-sent",
                ],
            )
        assert result.exit_code == 1
        send_mock.assert_not_called()
        err = result.output + (result.stderr or "")
        assert "--allow-no-copy" in err

    def test_identity_bcc_does_not_suppress_fcc(self):
        """FCC and BCC are independent: an identity that BCCs itself and
        leaves fcc at the default still files a Sent copy."""
        cfg = _cfg()
        cfg.identities["bccself"] = Identity(
            imap="acct",
            address="bccself@x.com",
            bcc=["bccself@x.com"],
        )
        client = _client()
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client", return_value=client),
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "bccself",
                ],
            )
        assert result.exit_code == 0, result.output
        bcc_hdr = str(captured[0].get("Bcc", "") or "")
        assert "bccself@x.com" in bcc_hdr
        # BCC no longer turns FCC off: the Sent copy is still filed.
        client.append_raw.assert_called_once()

    def test_identity_fcc_false_skips_fcc(self):
        """fcc = false disables the Sent copy; with a self-BCC providing the
        record, the send needs no IMAP connection and no --allow-no-copy."""
        cfg = _cfg()
        cfg.identities["listsender"] = Identity(
            imap="acct",
            address="listsender@x.com",
            bcc=["listsender@x.com"],
            fcc=False,
        )
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append(msg)
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "listsender",
                ],
            )
        assert result.exit_code == 0, result.output
        bcc_hdr = str(captured[0].get("Bcc", "") or "")
        assert "listsender@x.com" in bcc_hdr
        # fcc = false: no FCC, so no IMAP connection is opened.
        make_client_mock.assert_not_called()

    def test_identity_without_imap_can_still_send(self):
        """An identity with bcc but no imap is send-only and works without
        any IMAP block in scope."""
        cfg = _cfg_with_relay()
        cfg.identities["sendonly"] = Identity(
            imap=None,
            address="sendonly@example.com",
            smtp="ses",
            bcc=["sendonly@example.com"],
        )
        captured: list = []

        def fake_send(msg, smtp_cfg, transport=None):
            captured.append((msg, smtp_cfg))
            return (msg.as_bytes(), _result())

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._make_client") as make_client_mock,
            patch("courier.smtp_transport.send", side_effect=fake_send),
        ):
            result = runner.invoke(
                app,
                [
                    "compose",
                    "--to",
                    "alice@y.com",
                    "--body",
                    "hi",
                    "--send",
                    "--identity",
                    "sendonly",
                ],
            )
        assert result.exit_code == 0, result.output
        # SMTP went through the SES block.
        assert captured[0][1].host == "email-smtp.eu-west-1.amazonaws.com"
        # Self-BCC made it onto the wire.
        bcc_hdr = str(captured[0][0].get("Bcc", "") or "")
        assert "sendonly@example.com" in bcc_hdr
        # No IMAP connection ever opened (the identity has no imap).
        make_client_mock.assert_not_called()
