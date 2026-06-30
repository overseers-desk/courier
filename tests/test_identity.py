"""Tests for identity and SMTP resolution.

Covers the rules documented in courier/config.sample.toml: identities
filtered by their [imap.NAME] back-reference, the SendDisabled error
when no identities point at a block, explicit --from matching, the
hard-error path for unknown From (the AI-safety win), and the SMTP
resolution chain (identity.smtp -> imap_block.default_smtp -> lone smtp).
"""

from types import SimpleNamespace

import pytest

from courier.config import CourierConfig, Identity, ImapBlock, SmtpConfig
from courier.identity import (
    IdentityNotFound,
    SendDisabled,
    SmtpUnresolved,
    identities_for_imap,
    resolve_identity_for_reply,
    resolve_identity_for_send,
    resolve_smtp_for_identity,
)


def _block(username: str = "login@gmail.com") -> ImapBlock:
    return ImapBlock(
        host="imap.gmail.com",
        port=993,
        username=username,
        password="p",
    )


def _cfg_with_identities(identities: dict[str, Identity]) -> CourierConfig:
    """Build a one-block CourierConfig with the given identities dict."""
    return CourierConfig(
        imap_blocks={"acct": _block()},
        identities=identities,
        _default_imap="acct",
    )


def _email_stub(to=None, cc=None):
    """Stand-in for the Email model: an object exposing .to and .cc lists.

    Each list element is a SimpleNamespace exposing .address, mirroring
    the EmailAddress shape that the resolver reads.
    """
    return SimpleNamespace(
        to=[SimpleNamespace(address=a) for a in (to or [])],
        cc=[SimpleNamespace(address=a) for a in (cc or [])],
    )


class TestIdentitiesForImap:
    def test_filters_by_imap(self):
        cfg = CourierConfig(
            imap_blocks={"a": _block(), "b": _block(username="other@x.com")},
            identities={
                "alice": Identity(imap="a", address="alice@x.com"),
                "bob": Identity(imap="b", address="bob@x.com"),
                "alice2": Identity(imap="a", address="alice+sales@x.com"),
            },
            _default_imap="a",
        )
        a_idents = identities_for_imap(cfg, "a")
        assert [i.address for i in a_idents] == ["alice@x.com", "alice+sales@x.com"]
        b_idents = identities_for_imap(cfg, "b")
        assert [i.address for i in b_idents] == ["bob@x.com"]

    def test_empty_when_no_identities_point_at_block(self):
        cfg = CourierConfig(
            imap_blocks={"a": _block()},
            identities={},
            _default_imap="a",
        )
        assert identities_for_imap(cfg, "a") == []


class TestResolveIdentityForSend:
    def test_default_first_identity(self):
        cfg = _cfg_with_identities(
            {
                "primary": Identity(imap="acct", address="primary@x.com"),
                "other": Identity(imap="acct", address="other@x.com"),
            }
        )
        ident = resolve_identity_for_send(cfg, "acct")
        assert ident.address == "primary@x.com"

    def test_explicit_from_match(self):
        cfg = _cfg_with_identities(
            {
                "primary": Identity(imap="acct", address="primary@x.com"),
                "other": Identity(imap="acct", address="other@x.com"),
            }
        )
        ident = resolve_identity_for_send(cfg, "acct", from_addr="other@x.com")
        assert ident.address == "other@x.com"

    def test_explicit_from_case_insensitive(self):
        cfg = _cfg_with_identities(
            {"alice": Identity(imap="acct", address="alice@example.com")}
        )
        ident = resolve_identity_for_send(cfg, "acct", from_addr="ALICE@EXAMPLE.COM")
        assert ident.address == "alice@example.com"

    def test_unknown_from_raises(self):
        """The AI-safety hard error: unrecognised From cannot fall through."""
        cfg = _cfg_with_identities(
            {
                "a": Identity(imap="acct", address="a@x.com"),
                "b": Identity(imap="acct", address="b@x.com"),
            }
        )
        with pytest.raises(IdentityNotFound) as excinfo:
            resolve_identity_for_send(cfg, "acct", from_addr="impostor@x.com")
        assert excinfo.value.from_addr == "impostor@x.com"
        assert excinfo.value.available == ["a@x.com", "b@x.com"]

    def test_no_identities_raises_send_disabled(self):
        """The (ii) protection: empty identity list rejects sends."""
        cfg = _cfg_with_identities({})
        with pytest.raises(SendDisabled) as excinfo:
            resolve_identity_for_send(cfg, "acct")
        assert excinfo.value.imap_name == "acct"

    def test_no_identities_with_explicit_from_still_raises_send_disabled(self):
        """Explicit --from on a block with zero identities still fails fast."""
        cfg = _cfg_with_identities({})
        with pytest.raises(SendDisabled):
            resolve_identity_for_send(cfg, "acct", from_addr="anything@x.com")


class TestResolveIdentityForReply:
    def test_matches_to(self):
        cfg = _cfg_with_identities(
            {
                "a": Identity(imap="acct", address="a@x.com"),
                "b": Identity(imap="acct", address="b@x.com"),
            }
        )
        parent = _email_stub(to=["b@x.com"], cc=[])
        assert resolve_identity_for_reply(cfg, "acct", parent).address == "b@x.com"

    def test_matches_cc(self):
        cfg = _cfg_with_identities(
            {
                "a": Identity(imap="acct", address="a@x.com"),
                "b": Identity(imap="acct", address="b@x.com"),
            }
        )
        parent = _email_stub(to=["other@x.com"], cc=["a@x.com"])
        assert resolve_identity_for_reply(cfg, "acct", parent).address == "a@x.com"

    def test_falls_back_to_first(self):
        cfg = _cfg_with_identities(
            {
                "a": Identity(imap="acct", address="a@x.com"),
                "b": Identity(imap="acct", address="b@x.com"),
            }
        )
        parent = _email_stub(to=["unrelated@x.com"])
        assert resolve_identity_for_reply(cfg, "acct", parent).address == "a@x.com"

    def test_no_identities_raises_send_disabled(self):
        cfg = _cfg_with_identities({})
        parent = _email_stub(to=["whoever@x.com"])
        with pytest.raises(SendDisabled):
            resolve_identity_for_reply(cfg, "acct", parent)


class TestResolveSmtpForIdentity:
    def test_identity_smtp_wins(self):
        smtps = {
            "gmail": SmtpConfig(host="smtp.gmail.com"),
            "ses": SmtpConfig(
                host="email-smtp.example.com",
                username="AKIA",
                password="x",
            ),
        }
        block = ImapBlock(
            host="imap.gmail.com",
            port=993,
            username="login@gmail.com",
            password="p",
            default_smtp="gmail",
        )
        ident = Identity(imap="acct", address="a@x.com", smtp="ses")
        smtp = resolve_smtp_for_identity(ident, block, "acct", smtps)
        assert smtp.host == "email-smtp.example.com"
        assert smtp.username == "AKIA"

    def test_block_default_smtp_when_identity_omits(self):
        smtps = {
            "gmail": SmtpConfig(host="smtp.gmail.com"),
            "ses": SmtpConfig(host="email-smtp.example.com"),
        }
        block = ImapBlock(
            host="imap.gmail.com",
            port=993,
            username="login@gmail.com",
            password="p",
            default_smtp="gmail",
        )
        ident = Identity(imap="acct", address="a@x.com")
        smtp = resolve_smtp_for_identity(ident, block, "acct", smtps)
        assert smtp.host == "smtp.gmail.com"

    def test_lone_smtp_fallback(self):
        smtps = {"gmail": SmtpConfig(host="smtp.gmail.com")}
        block = _block()
        ident = Identity(imap="acct", address="a@x.com")
        smtp = resolve_smtp_for_identity(ident, block, "acct", smtps)
        assert smtp.host == "smtp.gmail.com"

    def test_template_inherits_creds_from_block(self):
        smtps = {"gmail": SmtpConfig(host="smtp.gmail.com")}
        block = _block()
        smtp = resolve_smtp_for_identity(
            Identity(imap="acct", address="x@y.com"),
            block,
            "acct",
            smtps,
        )
        assert smtp.username == "login@gmail.com"
        assert smtp.password == "p"

    def test_concrete_smtp_preserves_its_own_creds(self):
        smtps = {
            "ses": SmtpConfig(
                host="email-smtp.example.com",
                username="AKIA",
                password="ses-secret",
            )
        }
        block = _block()
        smtp = resolve_smtp_for_identity(
            Identity(imap="acct", address="x@y.com"),
            block,
            "acct",
            smtps,
        )
        assert smtp.username == "AKIA"
        assert smtp.password == "ses-secret"

    def test_no_resolution_path_raises(self):
        smtps = {
            "a": SmtpConfig(host="smtp.a.com"),
            "b": SmtpConfig(host="smtp.b.com"),
        }
        block = _block()
        with pytest.raises(SmtpUnresolved) as excinfo:
            resolve_smtp_for_identity(
                Identity(imap="myacct", address="x@y.com"),
                block,
                "myacct",
                smtps,
            )
        assert excinfo.value.imap_name == "myacct"
        assert sorted(excinfo.value.available) == ["a", "b"]
