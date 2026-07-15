"""Tests for the optional local-cache search backend (mu)."""

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict
from unittest.mock import patch

import pytest

from courier.config import ImapBlock, LocalCacheConfig
from courier.local_cache import MuBackend, MuFailure
from courier.query import parse


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
        monkeypatch.setattr("courier.local_cache.shutil.which", lambda _: None)

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
        monkeypatch.setattr("courier.local_cache.shutil.which", lambda _: "/usr/bin/mu")

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
        monkeypatch.setattr("courier.local_cache.shutil.which", lambda _: "/usr/bin/mu")

        result = backend.is_eligible(_make_block())

        assert result.eligible is False
        assert result.reason == "stale"

    def test_eligible(self, tmp_path, monkeypatch):
        """A fresh xapian dir plus mu on PATH yields eligibility."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        backend = MuBackend(cfg)
        monkeypatch.setattr("courier.local_cache.shutil.which", lambda _: "/usr/bin/mu")

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
        monkeypatch.setattr("courier.local_cache.shutil.which", lambda _: "/usr/bin/mu")
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

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(account_cfg, parse("from:alice"), limit=10)

        argv = captured["argv"]
        assert argv[0] == "mu"
        assert "find" in argv
        # --muhome is a subcommand flag and must follow ``find`` in the
        # argv, not precede it; mu's outer driver rejects it otherwise.
        assert argv.index(f"--muhome={muhome}") > argv.index("find")
        assert "--format=json" in argv
        assert "--maxnum" in argv
        # One extra record is requested so truncation is detectable.
        assert "11" in argv
        assert "--sortfield" in argv
        assert "date" in argv
        assert "--reverse" in argv
        # The scoped query must AND the translated query with the maildir.
        assert argv[-1] == '(from:alice) AND maildir:"/work/"'

    def test_parses_mu_json_output(self, tmp_path):
        """A single mu json record round-trips into the courier result shape."""
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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(sample),
                stderr="",
            ),
        ):
            results, _report, _truncated = backend.search(
                account_cfg, parse("from:alice"), limit=5
            )

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

    def test_exit_code_2_raises_mufailure_with_reason(self, tmp_path):
        """mu exits 2 both for a genuinely empty result and for a query
        it could not parse (verified on mu 1.12.14: an unknown field
        like ``filename:`` reaches Xapian as a term that matches
        nothing, same exit, same message). An empty that cannot be told
        apart from a rejected query must not be served as authoritative
        absence; it surfaces as a MuFailure so the caller falls back to
        IMAP with a named reason (issue #64)."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="no matches\n"
            ),
        ):
            with pytest.raises(MuFailure, match="exited 2") as excinfo:
                backend.search(account_cfg, parse("from:nobody"), limit=10)

        assert excinfo.value.fell_back_reason == "mu_no_matches"

    def test_timeout_raises_mufailure(self, tmp_path):
        """A subprocess timeout becomes a MuFailure for the caller to fall back."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        with patch("courier.local_cache.subprocess.run", side_effect=boom):
            with pytest.raises(MuFailure, match="timed out"):
                backend.search(account_cfg, parse("from:alice"), limit=10)

    def test_nonzero_exit_other_than_2_raises(self, tmp_path):
        """Any non-zero exit other than 2 is a real failure."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="permission denied"
            ),
        ):
            with pytest.raises(MuFailure, match="exited 1"):
                backend.search(account_cfg, parse("from:alice"), limit=10)

    def test_malformed_json_raises_mufailure(self, tmp_path):
        """Garbage stdout becomes a MuFailure rather than a JSONDecodeError."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not json", stderr=""
            ),
        ):
            with pytest.raises(MuFailure, match="decode"):
                backend.search(account_cfg, parse("from:alice"), limit=10)

    def test_imap_prefix_raises_untranslatable(self, tmp_path):
        """The imap: escape surfaces the emitter's refusal to the caller
        (importable under its old name UntranslatableQuery)."""
        from courier.local_cache import UntranslatableQuery

        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        # subprocess.run should never be called for an untranslatable query.
        with patch("courier.local_cache.subprocess.run") as mock_run:
            with pytest.raises(UntranslatableQuery):
                backend.search(account_cfg, parse("imap:UNSEEN"), limit=10)
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
            backend.search(block, parse("from:alice"), limit=10)

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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

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

    def test_root_equals_block_maildir_layout_served_from_store_root(self, tmp_path):
        """When mu's store root IS the block maildir (the layout
        LOCAL_CACHE.md's own init instructions produce), records report
        ``:maildir`` as ``/`` and ``/Archive``; the scope and folder
        derivation must work from the store root, not from a basename
        prefix that matches nothing (issue #64)."""
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        sample = [
            {
                ":path": "/var/local/mail/work/cur/1,U=5,FMD5=a:2,S",
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "at the root",
                ":date-unix": 1700000000,
                ":flags": [],
                ":message-id": "<root@x>",
                ":maildir": "/",
            },
            {
                ":path": "/var/local/mail/work/Archive/cur/2,U=6,FMD5=b:2,S",
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "archived",
                ":date-unix": 1700000001,
                ":flags": [],
                ":message-id": "<arch@x>",
                ":maildir": "/Archive",
            },
        ]
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=json.dumps(sample), stderr=""
            )

        with (
            patch.object(MuBackend, "_store_maildir_root", return_value=block.maildir),
            patch("courier.local_cache.subprocess.run", side_effect=fake_run),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert captured["argv"][-1] == "(from:alice) AND maildir:/*"
        assert [r["folder"] for r in results] == ["INBOX", "Archive"]

    def test_redact_block_skips_dead_path_keeps_rest(self, tmp_path, caplog):
        """One stale on-disk path (the syncer renamed the file since the
        last index) must not discard the block's whole result set on a
        redact block: the dead hit is skipped with a warning and the
        rest are served (issue #64)."""
        import logging

        backend = self._backend(tmp_path)
        live_file = tmp_path / "msg,U=44,FMD5=abc:2,S"
        live_file.write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: still here\r\n"
            b"Message-ID: <live@x>\r\n"
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
        dead_path = str(tmp_path / "renamed-away,U=45,FMD5=abc:2,S")
        sample = [
            {
                ":path": dead_path,
                ":from": [{":email": "a@b.com"}],
                ":to": [],
                ":subject": "gone",
                ":date-unix": 1700000000,
                ":flags": [],
                ":message-id": "<dead@x>",
                ":maildir": "/work",
            },
            {
                ":path": str(live_file),
                ":from": [{":email": "alice@example.com"}],
                ":to": [],
                ":subject": "still here",
                ":date-unix": 1700000001,
                ":flags": [],
                ":message-id": "<live@x>",
                ":maildir": "/work",
            },
        ]
        with (
            patch(
                "courier.local_cache.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(sample), stderr=""
                ),
            ),
            caplog.at_level(logging.WARNING, logger="courier.local_cache"),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert [r["message_id"] for r in results] == ["<live@x>"]
        assert any(dead_path in rec.message for rec in caplog.records)

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
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(sample), stderr=""
            ),
        ):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert len(results) == 1
        rec = results[0]
        assert "redacted_by" not in rec
        assert rec["subject"] == "keep me"
        assert rec["path"] == str(maildir_file)
        assert rec["uid"] == 43


class TestFolderExistenceCheck:
    """A folder-scoped search verifies the folder exists in the synced
    maildir before invoking mu (issue #64 remainder): on a selectively
    synced maildir, mu answers an absent folder with the same exit it
    uses for a genuine empty, so the check must run first and fall back
    to IMAP with an honest, named reason."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def _maildir(self, tmp_path, *folders: str) -> str:
        """Create ``<tmp>/mail/work`` with cur/ dirs for *folders*."""
        root = tmp_path / "mail" / "work"
        root.mkdir(parents=True, exist_ok=True)
        for folder in folders:
            (root / folder / "cur").mkdir(parents=True)
        return str(root)

    @staticmethod
    def _mu_ok(argv, **kwargs):
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="[]", stderr=""
        )

    def test_absent_folder_declines_with_named_reason(self, tmp_path):
        """A folder missing from the maildir raises MuFailure tagged
        ``folder_not_synced`` without invoking mu."""
        backend = self._backend(tmp_path)
        block = _make_block(self._maildir(tmp_path, "INBOX"))

        with patch("courier.local_cache.subprocess.run") as mock_run:
            with pytest.raises(MuFailure, match="Archive") as excinfo:
                backend.search(block, parse("from:alice"), limit=10, folder="Archive")
            mock_run.assert_not_called()

        assert excinfo.value.fell_back_reason == "folder_not_synced"

    def test_present_folder_proceeds_to_mu(self, tmp_path):
        """A synced folder passes the check and the search runs."""
        backend = self._backend(tmp_path)
        block = _make_block(self._maildir(tmp_path, "Archive"))

        with patch(
            "courier.local_cache.subprocess.run", side_effect=self._mu_ok
        ) as mock_run:
            results, _report, truncated = backend.search(
                block, parse("from:alice"), limit=10, folder="Archive"
            )

        assert results == []
        assert truncated is False
        assert mock_run.called

    def test_inbox_at_block_root_counts_as_present(self, tmp_path):
        """The block root being a maildir itself (cur/ present) is the
        root-as-INBOX layout; INBOX must count as synced."""
        backend = self._backend(tmp_path)
        root = tmp_path / "mail" / "work"
        (root / "cur").mkdir(parents=True)
        block = _make_block(str(root))

        with patch("courier.local_cache.subprocess.run", side_effect=self._mu_ok):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=10, folder="INBOX"
            )

        assert results == []

    def test_inbox_subdir_counts_as_present(self, tmp_path):
        """An INBOX subdirectory is the other layout _derive_folder
        collapses to INBOX; it must pass too."""
        backend = self._backend(tmp_path)
        block = _make_block(self._maildir(tmp_path, "INBOX"))

        with patch("courier.local_cache.subprocess.run", side_effect=self._mu_ok):
            results, _report, _truncated = backend.search(
                block, parse("from:alice"), limit=10, folder="INBOX"
            )

        assert results == []

    def test_inbox_absent_everywhere_declines(self, tmp_path):
        """No root cur/new and no INBOX subdir: INBOX is not synced."""
        backend = self._backend(tmp_path)
        block = _make_block(self._maildir(tmp_path, "Archive"))

        with patch("courier.local_cache.subprocess.run") as mock_run:
            with pytest.raises(MuFailure) as excinfo:
                backend.search(block, parse("from:alice"), limit=10, folder="INBOX")
            mock_run.assert_not_called()

        assert excinfo.value.fell_back_reason == "folder_not_synced"

    def test_unscoped_search_skips_the_check(self, tmp_path):
        """A whole-block search has no folder to verify; the historical
        maildir paths in older tests must keep working."""
        backend = self._backend(tmp_path)
        block = _make_block(str(tmp_path / "mail" / "does-not-exist"))

        with patch(
            "courier.local_cache.subprocess.run", side_effect=self._mu_ok
        ) as mock_run:
            backend.search(block, parse("from:alice"), limit=10)

        assert mock_run.called


class TestScopeQuery:
    """_scope_query maps a scope prefix + folder to a quoted maildir predicate."""

    def test_no_folder_scopes_whole_block_recursively(self):
        """Without a folder the predicate matches the block recursively
        (trailing slash) and ANDs the translated query. The scope is
        quoted: an unquoted prefix with whitespace matches nothing
        (issue #64)."""
        assert (
            MuBackend._scope_query("/work", "from:alice")
            == '(from:alice) AND maildir:"/work/"'
        )

    def test_recursive_scope_with_space_is_quoted(self):
        """A basename with whitespace survives query parsing only when
        quoted (verified on mu 1.12.14: the unquoted form returns no
        matches)."""
        assert (
            MuBackend._scope_query("/Work Account", "from:alice")
            == '(from:alice) AND maildir:"/Work Account/"'
        )

    def test_inbox_matches_root_and_inbox_subdir(self):
        """INBOX is matched both at the block root and at an INBOX subdir,
        mirroring the two cases _derive_folder collapses to INBOX."""
        assert (
            MuBackend._scope_query("/work", "", "INBOX")
            == '(maildir:"/work" OR maildir:"/work/INBOX")'
        )

    def test_subfolder_is_exact_no_trailing_slash(self):
        """A named subfolder is matched exactly (no trailing slash) so
        its own subfolders are not swept in."""
        assert (
            MuBackend._scope_query("/work", "from:alice", "Work Done")
            == '(from:alice) AND maildir:"/work/Work Done"'
        )

    def test_special_char_folder_is_quoted(self):
        """A folder carrying Xapian metacharacters ([Gmail]/Sent Mail) is
        quoted so it survives query parsing intact."""
        assert (
            MuBackend._scope_query("/acct", "", "[Gmail]/Sent Mail")
            == 'maildir:"/acct/[Gmail]/Sent Mail"'
        )

    def test_empty_prefix_no_folder_matches_whole_store(self):
        """mu root == block maildir: the recursive scope is the wildcard
        form, since maildir:"/" is an exact match on the root only
        (verified on mu 1.12.14)."""
        assert MuBackend._scope_query("", "from:alice") == "(from:alice) AND maildir:/*"

    def test_empty_prefix_inbox_matches_store_root(self):
        """mu root == block maildir: root messages report :maildir "/"."""
        assert (
            MuBackend._scope_query("", "", "INBOX")
            == '(maildir:"/" OR maildir:"/INBOX")'
        )

    def test_empty_prefix_subfolder(self):
        """mu root == block maildir: folders sit directly under "/"."""
        assert MuBackend._scope_query("", "", "Archive") == 'maildir:"/Archive"'


class TestScopePrefix:
    """_scope_prefix derives the block's scope from the mu store root."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def test_block_directly_under_root(self, tmp_path):
        backend = self._backend(tmp_path)
        with patch.object(
            MuBackend, "_store_maildir_root", return_value="/home/u/Maildir"
        ):
            prefix = backend._scope_prefix(_make_block("/home/u/Maildir/work"))
        assert prefix == "/work"

    def test_root_equals_block_maildir(self, tmp_path):
        """The documented-but-broken layout: mu init run on the block
        maildir itself (issue #64). The prefix is empty, not the
        basename that matches nothing."""
        backend = self._backend(tmp_path)
        with patch.object(
            MuBackend, "_store_maildir_root", return_value="/home/u/Maildir/work"
        ):
            prefix = backend._scope_prefix(_make_block("/home/u/Maildir/work"))
        assert prefix == ""

    def test_nested_block_maildir(self, tmp_path):
        """A block nested more than one level under the store root gets
        the full relative prefix, not just the basename."""
        backend = self._backend(tmp_path)
        with patch.object(
            MuBackend, "_store_maildir_root", return_value="/home/u/Maildir"
        ):
            prefix = backend._scope_prefix(_make_block("/home/u/Maildir/accounts/work"))
        assert prefix == "/accounts/work"

    def test_trailing_slashes_tolerated(self, tmp_path):
        backend = self._backend(tmp_path)
        with patch.object(
            MuBackend, "_store_maildir_root", return_value="/home/u/Maildir/"
        ):
            prefix = backend._scope_prefix(_make_block("/home/u/Maildir/work/"))
        assert prefix == "/work"

    def test_block_outside_root_declines_with_named_reason(self, tmp_path):
        """A block maildir mu does not index cannot be served locally;
        decline honestly instead of returning a clean empty."""
        backend = self._backend(tmp_path)
        with patch.object(
            MuBackend, "_store_maildir_root", return_value="/home/u/Maildir"
        ):
            with pytest.raises(MuFailure) as excinfo:
                backend._scope_prefix(_make_block("/srv/mail/work"))
        assert excinfo.value.fell_back_reason == "maildir_not_indexed"

    def test_unknown_root_keeps_legacy_basename_prefix(self, tmp_path):
        """When the store root cannot be read (old mu output), keep the
        historical assumption: the block sits directly under the root."""
        backend = self._backend(tmp_path)
        with patch.object(MuBackend, "_store_maildir_root", return_value=None):
            prefix = backend._scope_prefix(_make_block("/var/local/mail/work"))
        assert prefix == "/work"


class TestStoreMaildirRoot:
    """_store_maildir_root reads the maildir row from mu info store."""

    _INFO_STORE_OUTPUT = (
        "+-------------------+----------------------+\n"
        "| property          | value                |\n"
        "+-------------------+----------------------+\n"
        "| maildir           | /home/u/Maildir      |\n"
        "+-------------------+----------------------+\n"
        "| database-path     | /home/u/.mu/xapian   |\n"
        "+-------------------+----------------------+\n"
    )

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def test_parses_maildir_row(self, tmp_path):
        backend = self._backend(tmp_path)
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=self._INFO_STORE_OUTPUT, stderr=""
            )

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            root = backend._store_maildir_root()

        assert root == "/home/u/Maildir"
        assert captured["argv"][:3] == ["mu", "info", "store"]
        assert f"--muhome={backend.muhome}" in captured["argv"]

    def test_unparseable_output_returns_none_and_caches(self, tmp_path):
        backend = self._backend(tmp_path)
        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="no table here", stderr=""
            ),
        ) as mock_run:
            assert backend._store_maildir_root() is None
            assert backend._store_maildir_root() is None
        # The miss is cached: one subprocess call for two lookups.
        assert mock_run.call_count == 1


class TestWorldAsOfBoundsMuQuery:
    """Under WORLD_AS_OF the mu query gains a date:.. upper bound."""

    BOUND = datetime.fromisoformat("2026-07-12T17:07:00+10:00")

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def _capture_query(self, tmp_path, query: str) -> str:
        backend = self._backend(tmp_path)
        account_cfg = _make_block("/tmp/foo/work")
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[]", stderr=""
            )

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(account_cfg, parse(query), limit=10, world_as_of=self.BOUND)
        return captured["argv"][-1]

    def test_query_gains_date_upper_bound(self, tmp_path):
        expected_stamp = self.BOUND.astimezone().strftime("%Y%m%dT%H%M%S")
        scoped = self._capture_query(tmp_path, "from:alice")
        assert (
            scoped == f'((from:alice) AND date:..{expected_stamp}) AND maildir:"/work/"'
        )

    def test_empty_query_is_bound_alone(self, tmp_path):
        expected_stamp = self.BOUND.astimezone().strftime("%Y%m%dT%H%M%S")
        scoped = self._capture_query(tmp_path, "")
        assert scoped == f'(date:..{expected_stamp}) AND maildir:"/work/"'

    def test_relative_terms_resolve_against_bound(self, tmp_path):
        scoped = self._capture_query(tmp_path, "newer:7d")
        assert "date:20260705.." in scoped

    def test_unbounded_query_unchanged(self, tmp_path):
        backend = self._backend(tmp_path)
        account_cfg = _make_block("/tmp/foo/work")
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[]", stderr=""
            )

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(account_cfg, parse("from:alice"), limit=10)
        assert captured["argv"][-1] == '(from:alice) AND maildir:"/work/"'


class TestSearchTruncationProbe:
    """maxnum limit+1 tells a full page apart from a truncated one."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome, max_staleness_seconds=86400)
        return MuBackend(cfg)

    def _record(self, n: int) -> Dict[str, Any]:
        return {
            ":path": f"/var/local/mail/work/cur/{n}",
            ":from": [{":email": "a@b.com"}],
            ":to": [{":email": "c@d.com"}],
            ":subject": f"m{n}",
            ":date-unix": 1700000000 + n,
            ":flags": [],
            ":message-id": f"<m{n}@x>",
            ":maildir": "/work",
        }

    def _search(self, tmp_path, records, limit):
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(records), stderr=""
            ),
        ):
            return backend.search(block, parse("from:alice"), limit=limit)

    def test_extra_record_marks_truncated_and_is_cut(self, tmp_path):
        results, _report, truncated = self._search(
            tmp_path, [self._record(n) for n in range(3)], limit=2
        )
        assert truncated is True
        assert len(results) == 2

    def test_full_page_without_extra_is_not_truncated(self, tmp_path):
        results, _report, truncated = self._search(
            tmp_path, [self._record(n) for n in range(2)], limit=2
        )
        assert truncated is False
        assert len(results) == 2

    def test_report_carries_the_mu_dialect(self, tmp_path):
        _results, report, _truncated = self._search(
            tmp_path, [self._record(1)], limit=5
        )
        assert report.dialect == "mu"
