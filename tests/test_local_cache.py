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


_LIVE_SHAPED_RAW = (
    b'From: "Doe, Jane" <jane@example.com>\r\n'
    b"To: =?utf-8?q?Bj=C3=B6rn?= <bjorn@example.com>, plain@example.com\r\n"
    b"Subject: =?utf-8?q?Caf=C3=A9_menu?=\r\n"
    b"  for Friday\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    b"Message-ID: <menu-1@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="b1"\r\n'
    b"\r\n"
    b"--b1\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"See attached.\r\n"
    b"--b1\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="menu.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0K\r\n"
    b"--b1--\r\n"
)


def _mu_record(path: str, **overrides: Any) -> Dict[str, Any]:
    """A mu JSON record for *path*, worded the way mu words its fields."""
    record: Dict[str, Any] = {
        ":path": path,
        ":from": [{":email": "jane@example.com", ":name": "Doe, Jane"}],
        ":to": [{":email": "bjorn@example.com", ":name": "Björn"}],
        ":subject": "Café menu for Friday",
        ":date-unix": 1700000000,
        ":flags": ["seen", "replied", "attach"],
        ":message-id": "menu-1@example.com",
        ":maildir": "/work",
    }
    record.update(overrides)
    return record


def _file_notes(report) -> list:
    """The report's notes about hits whose file was gone after indexing."""
    return [note for note in report.approximations if "after indexing" in note]


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

    def test_eligible(self, tmp_path, monkeypatch):
        """A fresh xapian dir plus mu on PATH yields eligibility."""
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
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
        cfg = LocalCacheConfig(mu_index=muhome)
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
        cfg = LocalCacheConfig(mu_index=muhome)
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
        """A record whose file is not on disk round-trips from mu's fields."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block("/var/local/mail/work")
        sample = [
            {
                ":path": "/var/local/mail/work/cur/123:2,S",
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
            results, _report, _truncated, *_ = backend.search(
                account_cfg, parse("from:alice"), limit=5
            )

        assert len(results) == 1
        rec = results[0]
        assert rec["message_id"] == "<m@x>"
        assert rec["path"] == "/var/local/mail/work/cur/123:2,S"
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
        assert rec["flags"] == ["\\Seen"]
        assert rec["has_attachments"] is True

    def test_exit_code_2_is_a_served_miss(self, tmp_path):
        """mu exits 2 for a no-match result. It rejects no field: an
        unknown one is reinterpreted as free text (visible via ``mu
        find --analyze``), and courier's emitter has a closed field
        vocabulary, so for emitter-produced queries exit 2 means a
        genuine miss. The miss is served locally and disclosed in
        provenance rather than confirmed against IMAP (issue #95)."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="no matches\n"
            ),
        ):
            results, report, truncated, *_ = backend.search(
                account_cfg, parse("from:nobody"), limit=10
            )

        assert results == []
        assert truncated is False

    def test_other_nonzero_exit_raises_mufailure(self, tmp_path):
        """Exit codes past 2 stay failures for the caller to fall back."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block()

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="boom\n"
            ),
        ):
            with pytest.raises(MuFailure, match="exited 1"):
                backend.search(account_cfg, parse("from:nobody"), limit=10)

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

    def test_raw_control_character_in_mu_json_is_decoded(self, tmp_path):
        """mu writes a header's raw tab unescaped into its JSON (seen in a
        folded References id); the record is served, not refused."""
        backend = self._backend(tmp_path)
        account_cfg = _make_block("/var/local/mail/work")
        stdout = (
            '[{":path": "/var/local/mail/work/cur/123", ":subject": "Hi",'
            ' ":date-unix": 1700000000, ":maildir": "/work",'
            ' ":references": ["CAN_op-=Trz\t442Y@mail.gmail.com"]}]'
        )

        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr=""
            ),
        ):
            results, _report, _truncated, *_ = backend.search(
                account_cfg, parse("from:alice"), limit=5
            )

        assert [r["subject"] for r in results] == ["Hi"]

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
            results, _report, _truncated, *_ = backend.search(
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
            results, _report, _truncated, *_ = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert len(results) == 1
        assert "uid" not in results[0]
        assert results[0]["message_id"] == "<m@x>"

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
            results, _report, _truncated, *_ = backend.search(
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
            results, _report, _truncated, *_ = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert captured["argv"][-1] == "(from:alice) AND maildir:/*"
        assert [r["folder"] for r in results] == ["INBOX", "Archive"]

    def test_redact_block_skips_dead_path_keeps_rest(self, tmp_path, caplog):
        """One stale on-disk path (the syncer renamed the file since the
        last index) must not discard the block's whole result set on a
        redact block: the dead hit is skipped with a warning, the rest
        are served (issue #64), and the report counts what was left out."""
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
            results, report, _truncated, *_ = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert [r["message_id"] for r in results] == ["<live@x>"]
        assert any(dead_path in rec.message for rec in caplog.records)
        notes = _file_notes(report)
        assert len(notes) == 1
        note = notes[0]
        assert note.startswith("1 matching message is left out")
        assert "redact policy" in note
        assert note.endswith("; --no-cache asks the server")

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
            results, _report, _truncated, *_ = backend.search(
                block, parse("from:alice"), limit=5
            )

        assert len(results) == 1
        rec = results[0]
        assert "redacted_by" not in rec
        assert rec["subject"] == "keep me"
        assert rec["path"] == str(maildir_file)
        assert rec["uid"] == 43

    def _search_records(self, tmp_path, block: ImapBlock, records, limit: int = 5):
        """Run search() over canned mu records."""
        backend = self._backend(tmp_path)
        with patch(
            "courier.local_cache.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(records), stderr=""
            ),
        ):
            return backend.search(block, parse("from:jane"), limit=limit)

    def test_row_equals_the_live_projection_of_the_file(self, tmp_path):
        """Names, subject, Message-ID, flags, date and attachments come
        from the file through the live path's Email projection; ``path``
        is the one extra key."""
        from courier.imap_client import ImapClient

        path = tmp_path / "1700000000.1.host,U=42,FMD5=abc:2,RS"
        path.write_bytes(_LIVE_SHAPED_RAW)
        results, report, _, *_ = self._search_records(
            tmp_path, _make_block(), [_mu_record(str(path))]
        )

        flags = ["\\Answered", "\\Seen"]
        email_obj = ImapClient._email_from_bytes(_LIVE_SHAPED_RAW, 42, "INBOX", flags)
        expected = email_obj.as_search_result(
            folder="INBOX",
            flags=email_obj.flags,
            date_iso=email_obj.date.astimezone().isoformat(),
            has_attachments=len(email_obj.attachments) > 0,
        )
        expected["path"] = str(path)
        assert results == [expected]
        assert results[0]["message_id"] == "<menu-1@example.com>"
        assert results[0]["has_attachments"] is True
        assert _file_notes(report) == []

    def test_flags_use_the_imap_vocabulary_from_the_file_name(self, tmp_path):
        """Maildir flag letters map to IMAP flags; mu's words never leak."""
        path = tmp_path / "1700000000.1.host,U=9,FMD5=abc:2,DFT"
        path.write_bytes(_LIVE_SHAPED_RAW)
        results, _report, _, *_ = self._search_records(
            tmp_path,
            _make_block(),
            [_mu_record(str(path), **{":flags": ["draft", "flagged", "trashed"]})],
        )
        assert results[0]["flags"] == ["\\Draft", "\\Flagged", "\\Deleted"]

    def test_vanished_file_keeps_the_index_row_and_says_so(self, tmp_path):
        """A file moved after indexing still yields its row, from mu's
        fields (Message-ID bracketed, flags from the file name), and the
        report counts the rows served that way."""
        gone = str(tmp_path / "moved-away,U=7,FMD5=abc:2,S")
        results, report, _, *_ = self._search_records(
            tmp_path, _make_block(), [_mu_record(gone)]
        )
        assert len(results) == 1
        row = results[0]
        assert row["subject"] == "Café menu for Friday"
        assert row["from"] == "Doe, Jane <jane@example.com>"
        assert row["message_id"] == "<menu-1@example.com>"
        assert row["flags"] == ["\\Seen"]
        assert row["has_attachments"] is True
        assert row["uid"] == 7
        assert row["path"] == gone
        notes = _file_notes(report)
        assert len(notes) == 1
        note = notes[0]
        assert note.startswith("1 row comes from the index alone")
        assert note.endswith("; --no-cache asks the server")

    def test_vanished_files_are_counted_in_one_note(self, tmp_path):
        records = [
            _mu_record(str(tmp_path / f"gone{n},U={n}:2,S"), **{":date-unix": n})
            for n in range(1, 4)
        ]
        results, report, _, *_ = self._search_records(tmp_path, _make_block(), records)
        assert len(results) == 3
        notes = _file_notes(report)
        assert len(notes) == 1
        assert notes[0].startswith("3 rows come from the index")

    def _cut_search(self, tmp_path, gmail: bool, folder=None, limit: int = 2):
        """Search three hits, whose page *limit* 2 cuts so both the page
        run and the counting run happen.

        Returns ``(mu find argvs, outcome)``.
        """
        maildir = tmp_path / "mail" / "work"
        (maildir / "INBOX" / "cur").mkdir(parents=True)
        if gmail:
            (maildir / "[Gmail]" / "Sent Mail" / "cur").mkdir(parents=True)
        backend = self._backend(tmp_path)
        records = [
            _mu_record(str(maildir / f"gone{n}:2,S"), **{":maildir": "/work"})
            for n in range(3)
        ]
        argvs: list = []

        def fake_run(argv, **kwargs):
            if argv[:2] != ["mu", "find"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            argvs.append(argv)
            if "--fields=i" in argv:
                return subprocess.CompletedProcess(argv, 0, b"x\n" * 3, b"")
            return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            outcome = backend.search(
                _make_block(str(maildir)),
                parse("from:jane"),
                limit=limit,
                folder=folder,
            )
        return argvs, outcome

    def test_whole_gmail_block_shows_each_message_once(self, tmp_path):
        """A Gmail maildir holds one copy per label; the server answers a
        whole-account search from All Mail, one row per message, so both
        the page and its count skip copies sharing a Message-ID."""
        argvs, _ = self._cut_search(tmp_path, gmail=True)
        assert len(argvs) == 2
        assert all("--skip-dups" in argv for argv in argvs)

    def test_one_gmail_folder_keeps_every_row(self, tmp_path):
        argvs, _ = self._cut_search(tmp_path, gmail=True, folder="INBOX")
        assert argvs and not any("--skip-dups" in argv for argv in argvs)

    def test_other_layouts_keep_every_row(self, tmp_path):
        """Elsewhere a whole-account search covers every folder, so two
        folders' copies of a message are two rows live as well."""
        argvs, _ = self._cut_search(tmp_path, gmail=False)
        assert argvs and not any("--skip-dups" in argv for argv in argvs)

    def test_whole_gmail_block_says_what_the_cache_lacks(self, tmp_path):
        """No All Mail on disk: archived mail without a label is missing,
        and rows name a label folder and its UID."""
        _, outcome = self._cut_search(tmp_path, gmail=True)
        notes = [n for n in outcome.report.approximations if "All Mail" in n]
        assert len(notes) == 1
        assert "label" in notes[0]
        assert notes[0].endswith("; --no-cache asks the server")

    @pytest.mark.parametrize(
        "gmail, folder", [(True, "INBOX"), (False, None)], ids=["folder", "other"]
    )
    def test_other_searches_carry_no_gmail_note(self, tmp_path, gmail, folder):
        _, outcome = self._cut_search(tmp_path, gmail=gmail, folder=folder)
        assert not [n for n in outcome.report.approximations if "All Mail" in n]

    def test_cut_page_says_the_server_may_pick_other_rows(self, tmp_path):
        """The cache picks the newest by Date header, the server by the
        date it received each message."""
        _, outcome = self._cut_search(tmp_path, gmail=False)
        notes = [n for n in outcome.report.approximations if "page" in n]
        assert len(notes) == 1
        assert "Date header" in notes[0]
        assert notes[0].endswith("; --no-cache asks the server")

    def test_whole_page_carries_no_page_note(self, tmp_path):
        _, outcome = self._cut_search(tmp_path, gmail=False, limit=5)
        assert not outcome.truncated
        assert not [n for n in outcome.report.approximations if "page" in n]

    def test_rows_say_server_keywords_are_missing(self, tmp_path):
        """A maildir file name records the system flags only, so a keyword
        the server keeps never reaches a cached row."""
        _, outcome = self._cut_search(tmp_path, gmail=False, limit=5)
        notes = [n for n in outcome.report.approximations if "keywords" in n]
        assert len(notes) == 1
        assert notes[0].endswith("; --no-cache asks the server")

    def test_empty_answer_carries_no_keywords_note(self, tmp_path):
        records: list = []
        _, report, *_ = self._search_records(tmp_path, _make_block(), records)
        assert not [n for n in report.approximations if "keywords" in n]


class TestFolderExistenceCheck:
    """A folder-scoped search verifies the folder exists in the synced
    maildir before invoking mu (issue #64 remainder): on a selectively
    synced maildir, mu answers an absent folder with the same exit it
    uses for a genuine empty, so the check must run first and fall back
    to IMAP with an honest, named reason."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
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
            results, _report, truncated, *_ = backend.search(
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
            results, _report, _truncated, *_ = backend.search(
                block, parse("from:alice"), limit=10, folder="INBOX"
            )

        assert results == []

    def test_inbox_subdir_counts_as_present(self, tmp_path):
        """An INBOX subdirectory is the other layout _derive_folder
        collapses to INBOX; it must pass too."""
        backend = self._backend(tmp_path)
        block = _make_block(self._maildir(tmp_path, "INBOX"))

        with patch("courier.local_cache.subprocess.run", side_effect=self._mu_ok):
            results, _report, _truncated, *_ = backend.search(
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


class TestAllowedFoldersScope:
    """search() with allowed_folders restricts the mu query to the
    whitelist (issue #60): the query mu receives must never sweep the
    whole maildir on a whitelisted block."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
        return MuBackend(cfg)

    def test_allowed_folders_reach_the_mu_query(self, tmp_path):
        backend = self._backend(tmp_path)
        block = _make_block("/tmp/foo/work")
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[]", stderr=""
            )

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(
                block,
                parse("from:alice"),
                limit=10,
                allowed_folders=["INBOX", "Sent"],
            )

        assert captured["argv"][-1] == (
            '(from:alice) AND ((maildir:"/work" OR maildir:"/work/INBOX") '
            'OR maildir:"/work/Sent")'
        )

    def test_without_allowed_folders_scope_stays_recursive(self, tmp_path):
        backend = self._backend(tmp_path)
        block = _make_block("/tmp/foo/work")
        captured: Dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[]", stderr=""
            )

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            backend.search(block, parse("from:alice"), limit=10)

        assert captured["argv"][-1] == '(from:alice) AND maildir:"/work/"'


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

    def test_allowed_folders_restrict_the_recursive_scope(self):
        """A whitelisted block's whole-block search is scoped to the
        allowed set, not the recursive whole-maildir predicate: the
        whitelist must hold when the mu index serves the call
        (issue #60). INBOX keeps its dual root-or-subdir form."""
        assert MuBackend._scope_query(
            "/work", "from:alice", None, ["INBOX", "Sent"]
        ) == (
            '(from:alice) AND ((maildir:"/work" OR maildir:"/work/INBOX") '
            'OR maildir:"/work/Sent")'
        )

    def test_allowed_folders_single_entry(self):
        """One allowed folder still scopes exactly (no recursion)."""
        assert (
            MuBackend._scope_query("/work", "", None, ["Archive"])
            == '(maildir:"/work/Archive")'
        )

    def test_explicit_folder_wins_over_allowed_folders(self):
        """With an explicit folder the caller has already passed the
        whitelist gate; the exact-folder predicate stands alone."""
        assert (
            MuBackend._scope_query("/work", "", "Archive", ["INBOX", "Archive"])
            == 'maildir:"/work/Archive"'
        )


class TestScopePrefix:
    """_scope_prefix derives the block's scope from the mu store root."""

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
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
        cfg = LocalCacheConfig(mu_index=muhome)
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

    def test_non_utf8_store_path_round_trips(self, tmp_path, monkeypatch):
        """A store path outside UTF-8 survives mu info store intact."""
        store = tmp_path / os.fsdecode(b"st\xffre")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        table = (
            b"| maildir           | " + os.fsencode(str(store)) + b"/Maildir |\n"
            b"| database-path     | " + os.fsencode(str(store)) + b"/xapian |\n"
        )
        (bindir / "table").write_bytes(table)
        mu = bindir / "mu"
        mu.write_text(f'#!/bin/sh\ncat "{bindir}/table"\n')
        mu.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        backend = MuBackend(LocalCacheConfig())

        assert backend.muhome == str(store)
        assert backend._store_maildir_root() == f"{store}/Maildir"


class TestWorldAsOfBoundsMuQuery:
    """Under WORLD_AS_OF the mu query gains a date:.. upper bound."""

    BOUND = datetime.fromisoformat("2026-07-12T17:07:00+10:00")

    def _backend(self, tmp_path) -> MuBackend:
        muhome = _make_xapian_dir(tmp_path)
        cfg = LocalCacheConfig(mu_index=muhome)
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
        cfg = LocalCacheConfig(mu_index=muhome)
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

    def _search(
        self, tmp_path, records, limit, count_stdout: bytes = b"", count_exit: int = 0
    ):
        """Run search() with mu faked; the ``--fields=i`` counting run
        answers *count_stdout*. Every ``mu find`` argv is kept on
        ``self.calls``."""
        backend = self._backend(tmp_path)
        block = _make_block("/var/local/mail/work")
        self.calls: list = []

        def fake_run(argv, **kwargs):
            if argv[:2] == ["mu", "find"]:
                self.calls.append(argv)
            if "--fields=i" in argv:
                return subprocess.CompletedProcess(argv, count_exit, count_stdout, b"")
            return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        with patch("courier.local_cache.subprocess.run", side_effect=fake_run):
            return backend.search(block, parse("from:alice"), limit=limit)

    def test_extra_record_marks_truncated_and_is_cut(self, tmp_path):
        results, _report, truncated, *_ = self._search(
            tmp_path,
            [self._record(n) for n in range(3)],
            limit=2,
            count_stdout=b"m@x\n" * 3,
        )
        assert truncated is True
        assert len(results) == 2

    def test_full_page_without_extra_is_not_truncated(self, tmp_path):
        results, _report, truncated, *_ = self._search(
            tmp_path, [self._record(n) for n in range(2)], limit=2
        )
        assert truncated is False
        assert len(results) == 2

    def test_cut_page_counts_every_match(self, tmp_path):
        """A cut page reports the index's whole match count, from a
        second run over the same scoped query with no limit."""
        outcome = self._search(
            tmp_path,
            [self._record(n) for n in range(3)],
            limit=2,
            count_stdout=b"m@x\n" * 57,
        )
        assert outcome.truncated is True
        assert outcome.total_count == 57
        page_run, count_run = self.calls
        assert count_run[-1] == page_run[-1]
        assert "--maxnum" not in count_run
        assert count_run[2] == page_run[2]
        assert count_run[2].startswith("--muhome=")

    def test_uncut_page_total_is_its_length(self, tmp_path):
        outcome = self._search(tmp_path, [self._record(n) for n in range(2)], limit=5)
        assert outcome.total_count == 2
        assert len(self.calls) == 1

    def test_failed_count_leaves_the_total_unknown(self, tmp_path):
        """A count that cannot be had is reported as unknown, never guessed."""
        outcome = self._search(
            tmp_path, [self._record(n) for n in range(3)], limit=2, count_exit=1
        )
        assert outcome.truncated is True
        assert outcome.total_count is None

    def test_report_carries_the_mu_dialect(self, tmp_path):
        _results, report, _truncated, *_ = self._search(
            tmp_path, [self._record(1)], limit=5
        )
        assert report.dialect == "mu"


class TestMuLaunchFailures:
    """A mu binary that cannot be launched is a MuFailure, so the caller
    falls back to IMAP as it does after a timeout or a bad exit."""

    def test_search_unlaunchable_mu_raises_mufailure(self, tmp_path):
        backend = MuBackend(LocalCacheConfig(mu_index=_make_xapian_dir(tmp_path)))
        cannot = PermissionError(13, "Permission denied")

        with patch("courier.local_cache.subprocess.run", side_effect=cannot):
            with pytest.raises(MuFailure, match="could not run") as info:
                backend.search(_make_block(), parse("from:alice"), limit=10)
        assert info.value.__cause__ is cannot

    def test_muhome_discovery_unlaunchable_mu_raises_mufailure(self, tmp_path):
        backend = MuBackend(LocalCacheConfig())
        cannot = PermissionError(13, "Permission denied")

        with (
            patch("courier.local_cache.shutil.which", return_value="/usr/bin/mu"),
            patch("courier.local_cache.subprocess.run", side_effect=cannot),
        ):
            with pytest.raises(MuFailure, match="could not run"):
                backend._discover_muhome()
