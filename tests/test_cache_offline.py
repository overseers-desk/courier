"""A call the local cache answers must not touch the network.

Each test builds a real maildir on disk, a real ``MuBackend`` whose
``mu`` subprocess is faked, and an IMAP layer that records and refuses
every connection attempt, then drives the CLI.  The assertion that
matters is ``world.attempts == []``: courier answered with the network
down.  Calls that do need the server are checked from the other side:
they attempt a connection and fail as they always did.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import CourierConfig, ImapBlock, LocalCacheConfig
from courier.local_cache import EligibilityResult, MuBackend

runner = CliRunner()

RAW = (
    "From: Alice <alice@example.com>\r\n"
    "To: me@example.com\r\n"
    "Subject: Offline hello\r\n"
    "Message-ID: <inbox-1@example.com>\r\n"
    "Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    "Content-Type: text/plain\r\n"
    "\r\n"
    "Body read from disk.\r\n"
)


@dataclass
class World:
    """A cache fixture plus a network that records and refuses connections."""

    root: Path
    block: ImapBlock
    backend: MuBackend
    records: List[Dict[str, Any]] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    attempts: List[str] = field(default_factory=list)

    def add_message(self, folder: str, name: str, raw: str = RAW) -> Path:
        """Write one message file under ``<maildir>/<folder>/cur``."""
        directory = self.root / "work" / folder / "cur"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(raw)
        return path

    def hit(self, path: Path, folder: str, message_id: str) -> Dict[str, Any]:
        """A mu JSON record for *path*, in the shape ``mu find`` prints."""
        return {
            ":path": str(path),
            ":maildir": "/work" if folder == "INBOX" else f"/work/{folder}",
            ":from": [{":email": "alice@example.com", ":name": "Alice"}],
            ":to": [{":email": "me@example.com"}],
            ":subject": "Offline hello",
            ":date-unix": 1700000000,
            ":flags": ["seen"],
            ":message-id": message_id,
        }

    def invoke(self, args: List[str]):
        """Run the CLI against this world's config, cache and network."""
        cfg = CourierConfig(
            imap_blocks={"acct": self.block},
            _default_imap="acct",
            local_cache=LocalCacheConfig(mu_index=self.backend.muhome),
        )

        def fake_mu(argv, **kwargs):
            if argv[:2] == ["mu", "find"]:
                self.queries.append(argv[-1])
                body = json.dumps(self.records) if self.records else ""
                return subprocess.CompletedProcess(
                    argv, 0 if self.records else 2, body, ""
                )
            return subprocess.CompletedProcess(argv, 1, "", "unexpected")

        def refuse(*a: Any, **kw: Any):
            self.attempts.append("connect")
            raise OSError("network is down")

        with (
            patch("courier.__main__.load_config", return_value=cfg),
            patch("courier.__main__._get_mu_backend", return_value=self.backend),
            patch.object(
                MuBackend, "is_eligible", return_value=EligibilityResult(True)
            ),
            patch.object(MuBackend, "index_mtime_iso", return_value="2026-10-16"),
            patch("courier.local_cache.subprocess.run", side_effect=fake_mu),
            patch("courier.imap_client.imapclient.IMAPClient", side_effect=refuse),
        ):
            return runner.invoke(app, ["--imap", "acct", *args])


@pytest.fixture
def world(tmp_path: Path) -> World:
    muhome = tmp_path / "muhome"
    (muhome / "xapian").mkdir(parents=True)
    maildir = tmp_path / "mail" / "work"
    (maildir / "INBOX" / "cur").mkdir(parents=True)
    backend = MuBackend(LocalCacheConfig(mu_index=str(muhome)))
    backend._mu_root = str(tmp_path / "mail")
    backend._mu_root_resolved = True
    block = ImapBlock(
        host="imap.example.com",
        port=993,
        username="u",
        password="p",
        maildir=str(maildir),
    )
    return World(root=tmp_path / "mail", block=block, backend=backend)


class TestLazyConnection:

    def test_cache_served_search_makes_no_connection(self, world: World):
        path = world.add_message("INBOX", "1700000000.1.host,U=7:2,S")
        world.records = [world.hit(path, "INBOX", "<inbox-1@example.com>")]

        result = world.invoke(["search", "hello"])

        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)["search hello"]["acct"]
        assert out["provenance"]["source"] == "local"
        assert out["provenance"]["fell_back_reason"] is None
        assert out["results"][0]["uid"] == 7
        assert world.attempts == []

    def test_read_by_uid_from_disk_makes_no_connection(self, world: World):
        world.add_message("INBOX", "1700000000.1.host,U=7:2,S")

        result = world.invoke(["read", "-f", "INBOX", "-u", "7"])

        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)["read -f INBOX --uid 7"]["acct"]
        assert "Body read from disk." in body["body"]
        assert world.attempts == []

    def test_search_no_cache_connects_and_reports_failure(self, world: World):
        result = world.invoke(["search", "--no-cache", "hello"])

        assert world.attempts == ["connect"]
        out = json.loads(result.stdout)["search hello"]["acct"]
        assert out == {"error": "connection failed"}
        assert result.exit_code != 0

    def test_read_with_body_not_on_disk_connects_and_exits_1(self, world: World):
        result = world.invoke(["read", "-f", "INBOX", "-u", "99"])

        assert world.attempts == ["connect"]
        assert result.exit_code == 1
        assert "failed to connect to IMAP server" in (result.output + result.stderr)

    def test_read_no_cache_connects_and_exits_1(self, world: World):
        world.add_message("INBOX", "1700000000.1.host,U=7:2,S")

        result = world.invoke(["read", "--no-cache", "-f", "INBOX", "-u", "7"])

        assert world.attempts == ["connect"]
        assert result.exit_code == 1

    def test_export_raw_connects_and_exits_1(self, world: World, tmp_path: Path):
        world.add_message("INBOX", "1700000000.1.host,U=7:2,S")

        result = world.invoke(
            ["export", "-f", "INBOX", "-u", "7", "--raw", "-o", str(tmp_path / "o")]
        )

        assert world.attempts == ["connect"]
        assert result.exit_code == 1

    def test_write_connects_eagerly(self, world: World):
        result = world.invoke(["mark-read", "-f", "INBOX", "-u", "7"])

        assert world.attempts == ["connect"]
        assert result.exit_code == 1
