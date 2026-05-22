"""Tests for the optional local-cache search backend (mu)."""

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict
from unittest.mock import patch

import pytest

from mailroom.config import ImapBlock, LocalCacheConfig
from mailroom.local_cache import MuBackend, MuFailure
from mailroom.query_parser import UntranslatableQuery


def _make_block(maildir: str = "/var/local/mail/work") -> ImapBlock:
    """Build an ImapBlock with a configured maildir for tests."""
    return ImapBlock(
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="password",
        use_ssl=True,
        maildir=maildir,
    )


def _make_xapian_dir(tmp_path) -> str:
    """Create a fake mu store layout under tmp_path and return muhome."""
    muhome = tmp_path / "muhome"
    xapian = muhome / "xapian"
    xapian.mkdir(parents=True)
    return str(muhome)


class TestMuBackendIsEligible:
    """Eligibility check mirrors provenance.fell_back_reason vocabulary."""

    def test_mu_missing(self, tmp_path, monkeypatch):
        """When the mu binary is not on PATH the backend declines with
        reason ``mu_missing``."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
        backend = MuBackend(cfg)
        monkeypatch.setattr("mailroom.local_cache.shutil.which", lambda _: None)

        result = backend.is_eligible(_make_block())

        assert result.eligible is False
        assert result.reason == "mu_missing"

    def test_db_missing(self, tmp_path, monkeypatch):
        """When the xapian directory is absent the backend declines with
        reason ``db_missing``."""
        muhome = tmp_path / "muhome"
        muhome.mkdir()
        # No xapian subdir created.
        cfg = LocalCacheConfig(mu_index=str(muhome))
        backend = MuBackend(cfg)
        monkeypatch.setattr(
            "mailroom.local_cache.shutil.which", lambda _: "/usr/bin/mu"
        )

        result = backend.is_eligible(_make_block())

        assert result.eligible is False
        assert result.reason == "db_missing"

    def test_stale(self, tmp_path, monkeypatch):
        """A xapian dir whose mtime is older than max_staleness_seconds
        triggers a stale fallback."""
        muhome = _make_xapian_dir(tmp_path)
        xapian = os.path.join(muhome, "xapian")
        # Backdate xapian mtime to two hours ago.
        old_ts = datetime.now().timestamp() - 7200
        os.utime(xapian, (old_ts, old_ts))
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=3600)
        backend = MuBackend(cfg)
        monkeypatch.setattr(
            "mailroom.local_cache.shutil.which", lambda _: "/usr/bin/mu"
        )

        result = backend.is_eligible(_make_block())

        assert result.eligible is False
        assert result.reason == "stale"

    def test_eligible(self, tmp_path, monkeypatch):
        """A fresh xapian dir plus mu on PATH yields eligibility."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        backend = MuBackend(cfg)
        monkeypatch.setattr(
            "mailroom.local_cache.shutil.which", lambda _: "/usr/bin/mu"
        )

        result = backend.is_eligible(_make_block())

        assert result.eligible is True
        assert result.reason is None

    def test_redact_policy_block_remains_eligible(self, tmp_path, monkeypatch):
        """A block with a redact policy stays cache-eligible; the policy
        is applied against the on-disk maildir file at search time, not
        by forcing an IMAP round-trip."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        backend = MuBackend(cfg)
        monkeypatch.setattr(
            "mailroom.local_cache.shutil.which", lambda _: "/usr/bin/mu"
        )
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="user@example.com",
            password="password",
            use_ssl=True,
            maildir="/var/local/mail/work",
            redact_policy=lambda email_obj: True,
        )

        result = backend.is_eligible(block)

        assert result.eligible is True
        assert result.reason is None


class TestMuBackendSearch:
    """search() invokes mu, parses output, and surfaces failures."""

    def _backend(self, tmp_path) -> MuBackend:
        """Build a MuBackend with a real (empty) xapian dir at tmp_path."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def test_invokes_mu_with_correct_argv(self, tmp_path):
        """Argv must include muhome, find, --format=json, sort/limit/scope."""
        backend = self._backend(tmp_path)
        muhome = backend.muhome
        account_cfg = _make_block("/tmp/foo/work")

        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[]", stderr=""
            )

        with patch("mailroom.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(account_cfg, "from:alice", limit=10)

        argv = captured["argv"]
        assert argv[0] == "mu"
        assert "find" in argv
        # --muhome is a subcommand flag and must follow ``find`` in the
        # argv, not precede it; mu's outer driver rejects it otherwise.
        assert argv.index(f"--muhome={muhome}") > argv.index("find")
        assert "--format=json" in argv
        assert "--maxnum" in argv
        assert "10" in argv
        assert "--sortfield" in argv
        assert "date" in argv
        assert "--reverse" in argv
        # The scoped query must AND the translated query with the maildir.
        assert argv[-1] == "(from:alice) AND maildir:/work/"

    def test_parses_mu_json_output(self, tmp_path):
        """A single mu json record round-trips into the mailroom result shape."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block("/var/local/mail/work")
        sample = [
            {
                ":path": "/var/local/mail/work/cur/123",
                "size": 174217,
                ":from": [{":email": "a@b.com", ":name": "Alice"}],
                ":to": [{":email": "c@d.com"}],
                ":subject": "Hi",
                ":date-unix": 1700000000,
                ":flags": ["seen", "attach"],
                ":message-id": "<m@x>",
                ":maildir": "/work",
            }
        ]

        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(sample),
                stderr="",
            ),
        ):
            results = backend.search(account_cfg, "from:alice", limit=5)

        assert len(results) == 1
        rec = results[0]
        assert rec["message_id"] == "<m@x>"
        assert rec["path"] == "/var/local/mail/work/cur/123"
        assert rec["folder"] == "INBOX"
        assert rec["from"] == "Alice <a@b.com>"
        assert rec["to"] == ["c@d.com"]
        assert rec["subject"] == "Hi"
        # _format_date renders in the host's local zone; assert the instant
        # rather than the wall-clock so the test holds in any timezone.
        from datetime import datetime, timezone

        assert datetime.fromisoformat(rec["date"]) == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
        )
        assert rec["flags"] == ["seen", "attach"]
        assert rec["has_attachments"] is True

    def test_exit_code_2_returns_empty(self, tmp_path):
        """mu's exit code 2 (no matches) is not an error — return []."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="no matches\n"
            ),
        ):
            results = backend.search(account_cfg, "from:nobody", limit=10)

        assert results == []

    def test_timeout_raises_mufailure(self, tmp_path):
        """A subprocess timeout becomes a MuFailure for the caller to fall back."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        with patch("mailroom.local_cache.subprocess.run", side_effect=boom):
            with pytest.raises(MuFailure, match="timed out"):
                backend.search(account_cfg, "from:alice", limit=10)

    def test_nonzero_exit_other_than_2_raises(self, tmp_path):
        """Any non-zero exit other than 2 is a real failure."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="permission denied"
            ),
        ):
            with pytest.raises(MuFailure, match="exited 1"):
                backend.search(account_cfg, "from:alice", limit=10)

    def test_malformed_json_raises_mufailure(self, tmp_path):
        """Garbage stdout becomes a MuFailure rather than a JSONDecodeError."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not json", stderr=""
            ),
        ):
            with pytest.raises(MuFailure, match="decode"):
                backend.search(account_cfg, "from:alice", limit=10)

    def test_imap_prefix_raises_untranslatable(self, tmp_path):
        """An imap: token in the query surfaces UntranslatableQuery to the
        caller (re-raised from parse_query_to_mu)."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        # subprocess.run should never be called for an untranslatable query.
        with patch("mailroom.local_cache.subprocess.run") as mock_run:
            with pytest.raises(UntranslatableQuery):
                backend.search(account_cfg, "imap:UNSEEN", limit=10)
            mock_run.assert_not_called()

    def test_no_maildir_raises_value_error(self, tmp_path):
        """A block without maildir cannot be scoped; ValueError protects us."""
        backend = self._backend(tmp_path)
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="x@example.com",
            password="password",
            use_ssl=True,
            maildir=None,
        )

        with pytest.raises(ValueError, match="maildir"):
            backend.search(block, "from:alice", limit=10)

    def test_search_surfaces_uid_from_filename(self, tmp_path):
        """When the maildir filename embeds ``,U=N,`` (mbsync convention)
        the result carries ``uid: N`` as an int so search→read piping
        works uniformly with IMAP-served hits."""
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        sample = [
            {
                ":path": (
                    "/var/local/mail/work/INBOX/cur/"
                    "1775099737_0.37144.yoga,U=691,FMD5=abc:2,S"
                ),
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "Hi",
                ":date-unix": 1700000000,
                ":flags": ["seen"],
                ":message-id": "<m@x>",
                ":maildir": "/work",
            }
        ]
        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results = backend.search(block, "from:alice", limit=5)

        assert len(results) == 1
        assert results[0]["uid"] == 691
        assert isinstance(results[0]["uid"], int)

    def test_search_omits_uid_when_filename_lacks_U_field(self, tmp_path):
        """A non-mbsync filename (no ``,U=N,``) yields a result without a
        ``uid`` key; the rest of the shape is unaffected."""
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        sample = [
            {
                ":path": "/var/local/mail/work/INBOX/cur/12345.mbox",
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "Hi",
                ":date-unix": 1700000000,
                ":flags": ["seen"],
                ":message-id": "<m@x>",
                ":maildir": "/work",
            }
        ]
        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results = backend.search(block, "from:alice", limit=5)

        assert len(results) == 1
        assert "uid" not in results[0]
        assert results[0]["message_id"] == "<m@x>"

    def test_search_skips_file_read_when_no_policy(self, tmp_path):
        """Fast-path discipline: when the block has no redact policy the
        backend never opens the maildir file. The path may be entirely
        fictitious; if the code attempted to read it the test would
        raise."""
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        # The path is intentionally non-existent; reading it would raise.
        sample = [
            {
                ":path": "/nonexistent/path/that/does/not/exist,U=1,FMD5=x:2,S",
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "Hi",
                ":date-unix": 1700000000,
                ":flags": [],
                ":message-id": "<m@x>",
                ":maildir": "/work",
            }
        ]
        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results = backend.search(block, "from:alice", limit=5)

        assert len(results) == 1
        assert results[0]["subject"] == "Hi"
        assert "redacted_by" not in results[0]

    def test_search_applies_redact_to_matching_hit(self, tmp_path):
        """When the block carries a redact policy that matches, the
        result carries ``redacted_by="redacted"``, blanked content
        fields, and no ``path`` (closing the maildir-path leakage on
        redacted records)."""
        backend = self._backend(tmp_path)
        # Real RFC 822 message on disk so the policy can be applied.
        maildir_file = tmp_path / "msg,U=42,FMD5=abc:2,S"
        maildir_file.write_bytes(
            b"From: alice@example.com\r\n"
            b"To: bob@example.com\r\n"
            b"Cc: carol@example.com\r\n"
            b"Subject: confidential\r\n"
            b"Date: Thu, 01 Jan 2023 12:00:00 +0000\r\n"
            b"Message-ID: <m@x>\r\n"
            b"\r\n"
            b"secret body\r\n"
        )
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="user@example.com",
            password="password",
            use_ssl=True,
            maildir="/var/local/mail/work",
            redact_policy=lambda email_obj: True,
        )
        sample = [
            {
                ":path": str(maildir_file),
                ":from": [{":email": "alice@example.com"}],
                ":to": [{":email": "bob@example.com"}],
                ":subject": "confidential",
                ":date-unix": 1672574400,
                ":flags": ["seen"],
                ":message-id": "<m@x>",
                ":maildir": "/work",
            }
        ]
        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results = backend.search(block, "from:alice", limit=5)

        assert len(results) == 1
        rec = results[0]
        assert rec["redacted_by"] == "redacted"
        assert rec["from"] == "[redacted]"
        assert rec["to"] == []
        assert rec["subject"].startswith("[redacted")
        assert rec["has_attachments"] is False
        assert "path" not in rec
        assert rec["uid"] == 42
        assert rec["message_id"] == "<m@x>"

    def test_search_passthrough_when_policy_does_not_match(self, tmp_path):
        """A policy that returns False yields the normal record shape,
        unredacted, even though the file was read."""
        backend = self._backend(tmp_path)
        maildir_file = tmp_path / "msg,U=43,FMD5=abc:2,S"
        maildir_file.write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: keep me\r\n"
            b"Message-ID: <n@x>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="user@example.com",
            password="password",
            use_ssl=True,
            maildir="/var/local/mail/work",
            redact_policy=lambda email_obj: False,
        )
        sample = [
            {
                ":path": str(maildir_file),
                ":from": [{":email": "alice@example.com"}],
                ":to": [],
                ":subject": "keep me",
                ":date-unix": 1700000000,
                ":flags": [],
                ":message-id": "<n@x>",
                ":maildir": "/work",
            }
        ]
        with patch(
            "mailroom.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results = backend.search(block, "from:alice", limit=5)

        assert len(results) == 1
        rec = results[0]
        assert "redacted_by" not in rec
        assert rec["subject"] == "keep me"
        assert rec["path"] == str(maildir_file)
        assert rec["uid"] == 43


class TestScopeQuery:
    """_scope_query maps a folder to an exact, quoted maildir predicate."""

    def test_no_folder_scopes_whole_block_recursively(self):
        """Without a folder the predicate matches the block recursively
        (trailing slash) and ANDs the translated query."""
        assert (
            MuBackend._scope_query("/var/local/mail/work", "from:alice")
            == "(from:alice) AND maildir:/work/"
        )

    def test_inbox_matches_root_and_inbox_subdir(self):
        """INBOX is matched both at the block root and at an INBOX subdir,
        mirroring the two cases _derive_folder collapses to INBOX."""
        assert (
            MuBackend._scope_query("/var/local/mail/work", "", "INBOX")
            == '(maildir:"/work" OR maildir:"/work/INBOX")'
        )

    def test_subfolder_is_exact_no_trailing_slash(self):
        """A named subfolder is matched exactly (no trailing slash) so
        its own subfolders are not swept in."""
        assert (
            MuBackend._scope_query("/var/local/mail/work", "from:alice", "Work Done")
            == '(from:alice) AND maildir:"/work/Work Done"'
        )

    def test_special_char_folder_is_quoted(self):
        """A folder carrying Xapian metacharacters ([Gmail]/Sent Mail) is
        quoted so it survives query parsing intact."""
        assert (
            MuBackend._scope_query("/var/local/mail/acct", "", "[Gmail]/Sent Mail")
            == 'maildir:"/acct/[Gmail]/Sent Mail"'
        )
