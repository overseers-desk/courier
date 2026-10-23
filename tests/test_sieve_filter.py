"""Tests for the per-block redact policy compiler and evaluator."""

import os

import pytest

from courier.models import Email, EmailAddress
from courier.sieve_filter import compile_policy


def _write_sieve(dirpath: str, body: str, name: str = "policy.sieve") -> str:
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def _email(
    *,
    from_addr: str = "alice@example.com",
    to_addrs: tuple = ("bob@example.com",),
    cc_addrs: tuple = (),
    subject: str = "hello",
) -> Email:
    return Email(
        message_id="<m@x>",
        subject=subject,
        from_=EmailAddress(name="", address=from_addr),
        to=[EmailAddress(name="", address=a) for a in to_addrs],
        cc=[EmailAddress(name="", address=a) for a in cc_addrs],
    )


class TestCompilePolicySupportedSubset:
    """Compile and evaluate the supported Sieve constructs."""

    def test_address_is_single_header(self, tmp_path):
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if address :is "from" "lawyer@firm.com" { redact; }\n',
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="lawyer@firm.com")) is True
        assert policy(_email(from_addr="alice@example.com")) is False

    def test_address_is_header_list_and_key_list(self, tmp_path):
        """address :is over multiple headers and multiple keys.

        Matches when any (header, key) pair lines up.
        """
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if address :is ["from","to","cc"] '
            '["lawyer@firm.com","counsel@firm.com"] { redact; }\n',
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="lawyer@firm.com")) is True
        assert policy(_email(to_addrs=("counsel@firm.com",))) is True
        assert policy(_email(cc_addrs=("lawyer@firm.com",))) is True
        assert policy(_email(from_addr="alice@example.com")) is False

    def test_not_inverts(self, tmp_path):
        """``not address :is`` redacts when the address is missing.

        This expresses the common 'restrict the agent to messages where
        my work address participates' pattern via Sieve negation.
        """
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if not address :is ["from","to","cc"] '
            '"director@example.com" { redact; }\n',
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="director@example.com")) is False
        assert policy(_email(from_addr="other@example.com")) is True

    def test_anyof_combinator(self, tmp_path):
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            "if anyof("
            '  address :is "from" "a@x",'
            '  header :contains "Subject" "secret"'
            ") { redact; }\n",
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="a@x")) is True
        assert policy(_email(subject="top secret memo")) is True
        assert policy(_email(from_addr="z@z", subject="invoice")) is False

    def test_allof_combinator(self, tmp_path):
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            "if allof("
            '  address :is "from" "a@x",'
            '  header :contains "Subject" "confidential"'
            ") { redact; }\n",
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="a@x", subject="confidential note")) is True
        assert policy(_email(from_addr="a@x", subject="public note")) is False
        assert policy(_email(from_addr="b@y", subject="confidential note")) is False

    def test_matches_glob_suffix(self, tmp_path):
        """``:matches "*@example.com"`` redacts every example.com address."""
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if address :matches "from" "*@example.com" { redact; }\n',
        )
        policy = compile_policy(path)
        assert policy(_email(from_addr="anyone@example.com")) is True
        assert policy(_email(from_addr="anyone@elsewhere.com")) is False


class TestCompilePolicyFailureClosed:
    """Out-of-subset and malformed inputs raise at compile time."""

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="cannot read sieve file"):
            compile_policy(str(tmp_path / "no-such.sieve"))

    def test_parse_error(self, tmp_path):
        path = _write_sieve(str(tmp_path), "this is not sieve syntax {}")
        with pytest.raises(ValueError, match="parse failed"):
            compile_policy(path)

    def test_no_if_blocks(self, tmp_path):
        path = _write_sieve(str(tmp_path), 'require ["courier-policy"];\n')
        with pytest.raises(ValueError, match="no `if"):
            compile_policy(path)

    def test_action_other_than_redact(self, tmp_path):
        path = _write_sieve(
            str(tmp_path),
            'require ["fileinto"];\n'
            'if address :is "from" "x@y" { fileinto "Junk"; }\n',
        )
        with pytest.raises(ValueError, match="single `redact;`"):
            compile_policy(path)

    def test_unsupported_test(self, tmp_path):
        """``body`` is in the deferred set, so it must be rejected."""
        path = _write_sieve(
            str(tmp_path),
            'require ["body","courier-policy"];\n'
            'if body :contains "secret" { redact; }\n',
        )
        with pytest.raises(ValueError, match="outside the supported subset"):
            compile_policy(path)

    def test_unsupported_match_type(self, tmp_path):
        """``:regex`` is deferred and must be rejected."""
        path = _write_sieve(
            str(tmp_path),
            'require ["regex","courier-policy"];\n'
            'if address :regex "from" ".*@firm\\\\.com" { redact; }\n',
        )
        with pytest.raises(ValueError, match="match-type"):
            compile_policy(path)

    def test_address_test_on_unsupported_header(self, tmp_path):
        """address-test on a non-address header is rejected by the walk."""
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if address :is "subject" "x@y" { redact; }\n',
        )
        with pytest.raises(ValueError, match="address test on header"):
            compile_policy(path)

    def test_interior_glob_rejected(self, tmp_path):
        path = _write_sieve(
            str(tmp_path),
            'require ["courier-policy"];\n'
            'if address :matches "from" "a*b@example.com" { redact; }\n',
        )
        with pytest.raises(ValueError, match="interior"):
            compile_policy(path)


class TestEmailRedact:
    """Email.redact returns a placeholder copy preserving referent fields."""

    def test_redact_blanks_sensitive_fields(self):
        original = Email(
            message_id="<m@x>",
            subject="Confidential",
            from_=EmailAddress("Alice", "alice@example.com"),
            to=[EmailAddress("", "bob@example.com")],
            cc=[EmailAddress("", "eve@example.com")],
        )
        redacted = original.redact("legal")
        assert redacted.redacted_by == "legal"
        assert redacted.subject == "[redacted by rule legal]"
        assert redacted.from_.address == "[redacted]"
        assert redacted.to == []
        assert redacted.cc == []
        assert redacted.content.text == "[redacted by rule legal]"

    def test_redact_preserves_uid_date_message_id_threading(self):
        """Date, UID, message_id, in_reply_to, and references survive.

        The agent retains a referent for the message: it can say "on
        day X, a redacted message arrived that referenced thread T".
        """
        from datetime import datetime, timezone

        original = Email(
            message_id="<m@x>",
            subject="x",
            from_=EmailAddress("", "a@x"),
            to=[EmailAddress("", "b@y")],
            uid=42,
            folder="INBOX",
            date=datetime(2026, 4, 1, 9, tzinfo=timezone.utc),
            in_reply_to=["<parent@x>"],
            references=["<root@x>", "<parent@x>"],
        )
        redacted = original.redact("rule")
        assert redacted.uid == 42
        assert redacted.folder == "INBOX"
        assert redacted.message_id == "<m@x>"
        assert redacted.date == original.date
        assert redacted.in_reply_to == ["<parent@x>"]
        assert redacted.references == ["<root@x>", "<parent@x>"]
