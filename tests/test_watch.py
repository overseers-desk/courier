"""Tests for courier.watch — the IMAP IDLE watch seam.

Scripted-mock style: the underlying imapclient.IMAPClient constructor is
patched to return a MagicMock whose idle_check responses are scripted
per test; watch() builds its own ImapClient on top of it.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import IMAPClientAbortError
from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import ImapBlock
from courier.errors import CapabilityMissing
from courier.watch import WatchEvent, watch
from tests.conftest import patch_default_cli_config

runner = CliRunner()


def _block() -> ImapBlock:
    return ImapBlock(
        host="imap.example.com", port=993, username="u@example.com", password="x"
    )


def _mock_server(uidvalidity: int = 111, exists: int = 3) -> MagicMock:
    """A MagicMock standing in for imapclient.IMAPClient."""
    inst = MagicMock()
    inst.has_capability.return_value = True
    inst.select_folder.return_value = {b"UIDVALIDITY": uidvalidity, b"EXISTS": exists}
    inst.idle_check.return_value = []
    return inst


class RecordingStop(threading.Event):
    """A stop event whose wait() returns immediately and records delays."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


class TestWatchEvents:
    """idle_check untagged responses map to typed events."""

    def test_idle_responses_become_events(self):
        stop = threading.Event()
        inst = _mock_server(uidvalidity=111, exists=3)

        def checks(timeout):
            stop.set()
            return [
                (b"OK", b"Still here"),
                (4, b"EXISTS"),
                (2, b"EXPUNGE"),
                (3, b"FETCH", (b"FLAGS", (b"\\Seen",))),
            ]

        inst.idle_check.side_effect = checks
        with patch("imapclient.IMAPClient", return_value=inst):
            events = list(watch(_block(), stop=stop, poll_interval=0.01))

        assert [e.kind for e in events] == ["started", "exists", "expunge", "flags"]
        started = events[0]
        assert started.uidvalidity == 111
        assert started.count == 3  # EXISTS total at SELECT time
        assert events[1].count == 4  # new mailbox total
        assert events[2].count == 2  # expunged sequence number
        assert events[3].count == 3  # fetched sequence number
        assert all(e.folder == "INBOX" for e in events)
        inst.select_folder.assert_called_once_with("INBOX", readonly=True)

    def test_abort_reconnects_with_backoff_and_new_uidvalidity(self):
        stop = RecordingStop()
        inst1 = _mock_server(uidvalidity=111)
        inst1.idle_check.side_effect = IMAPClientAbortError("socket EOF")
        inst2 = _mock_server(uidvalidity=222, exists=7)

        def checks(timeout):
            stop.set()
            return []

        inst2.idle_check.side_effect = checks
        with patch("imapclient.IMAPClient", side_effect=[inst1, inst2]):
            events = list(watch(_block(), stop=stop, poll_interval=0.01))

        assert [e.kind for e in events] == ["started", "reconnected"]
        assert events[0].uidvalidity == 111
        assert events[1].uidvalidity == 222
        assert stop.waits == [1.0]  # first backoff step

    def test_reissue_after_elapsed_triggers_idle_done_and_reidle(self, monkeypatch):
        stop = threading.Event()
        inst = _mock_server()
        calls = {"n": 0}

        def checks(timeout):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop.set()
            return []

        inst.idle_check.side_effect = checks
        # Scripted monotonic readings: idle_since=0, first check sees
        # 1000s (>900 -> reissue), new idle_since=2000, second check
        # sees 2500 (500s elapsed -> no reissue).
        readings = [0, 1000, 2000, 2500]

        def fake_monotonic():
            return readings.pop(0) if len(readings) > 1 else readings[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)
        with patch("imapclient.IMAPClient", return_value=inst):
            events = list(watch(_block(), stop=stop, poll_interval=0.01))

        assert [e.kind for e in events] == ["started"]
        assert inst.idle.call_count == 2  # initial + reissue
        assert inst.idle_done.call_count == 2  # reissue + stop exit

    def test_stop_terminates_generator(self):
        stop = threading.Event()
        inst = _mock_server()

        def checks(timeout):
            stop.set()
            return []

        inst.idle_check.side_effect = checks
        with patch("imapclient.IMAPClient", return_value=inst):
            events = list(watch(_block(), stop=stop, poll_interval=0.01))

        assert [e.kind for e in events] == ["started"]
        inst.idle_done.assert_called_once()
        inst.logout.assert_called()

    def test_missing_idle_capability_raises(self):
        inst = _mock_server()
        inst.has_capability.return_value = False
        with patch("imapclient.IMAPClient", return_value=inst):
            with pytest.raises(CapabilityMissing):
                list(watch(_block()))
        inst.idle.assert_not_called()


class TestWatchCli:
    """courier watch prints one ndjson line per event."""

    def test_watch_prints_ndjson(self):
        events = [
            WatchEvent(
                kind="started", folder="INBOX", uidvalidity=111, count=3, raw="s"
            ),
            WatchEvent(kind="exists", folder="INBOX", count=4, raw="(4, b'EXISTS')"),
        ]
        with (
            patch_default_cli_config(),
            patch(
                "courier.__main__.watch_folder", return_value=iter(events)
            ) as mock_watch,
        ):
            result = runner.invoke(app, ["watch"])
        assert result.exit_code == 0, result.output
        lines = [json.loads(ln) for ln in result.output.strip().splitlines()]
        assert [ln["kind"] for ln in lines] == ["started", "exists"]
        assert lines[0]["uidvalidity"] == 111
        assert lines[1]["count"] == 4
        assert mock_watch.call_args[0][1] == "INBOX"

    def test_capability_missing_exits_1(self):
        with (
            patch_default_cli_config(),
            patch(
                "courier.__main__.watch_folder",
                side_effect=CapabilityMissing("server lacks IDLE"),
            ),
        ):
            result = runner.invoke(app, ["watch"])
        assert result.exit_code == 1
        assert "Error:" in result.output


class TestWatchWorldBound:
    """watch refuses under WORLD_AS_OF: a live tail of the future."""

    def test_watch_refuses_eagerly_under_bound(self, monkeypatch):
        from courier.errors import WorldBoundRefused

        monkeypatch.setenv("WORLD_AS_OF", "2026-07-12T17:07:00+10:00")
        # Eager: the refusal fires at call time, before any iteration
        # (and before any connection is attempted).
        with pytest.raises(WorldBoundRefused, match="WORLD_AS_OF"):
            watch(_block())

    def test_watch_unbounded_still_yields(self, monkeypatch):
        monkeypatch.delenv("WORLD_AS_OF", raising=False)
        stop = threading.Event()
        inst = _mock_server()

        def checks(timeout):
            stop.set()
            return []

        inst.idle_check.side_effect = checks
        with patch("imapclient.IMAPClient", return_value=inst):
            events = list(watch(_block(), stop=stop, poll_interval=0.01))
        assert [e.kind for e in events] == ["started"]

    def test_cli_watch_exits_one_under_bound(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", "2026-07-12T17:07:00+10:00")
        with patch_default_cli_config():
            result = runner.invoke(app, ["watch"])
        assert result.exit_code == 1
        assert "WORLD_AS_OF" in result.output
