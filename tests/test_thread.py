"""Tests for ``ImapClient.find_thread`` and the ``thread`` CLI verb."""

import email
from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import CourierConfig, ImapBlock
from courier.imap_client import ImapClient
from courier.models import Email, EmailAddress

runner = CliRunner()


def _email(
    uid: int,
    message_id: str,
    day: int,
    in_reply_to: Optional[List[str]] = None,
    references: Optional[List[str]] = None,
) -> Email:
    return Email(
        message_id=message_id,
        subject="Loan facility",
        from_=EmailAddress(name="", address="alice@example.com"),
        to=[EmailAddress(name="", address="bob@example.com")],
        date=datetime(2026, 10, day, 9, 0, tzinfo=timezone.utc),
        uid=uid,
        in_reply_to=in_reply_to or [],
        references=references or [],
    )


def _client(capabilities: List[str]) -> ImapClient:
    block = ImapBlock(host="imap.example.com", port=993, username="u", password="p")
    client = ImapClient(block)
    client.ensure_connected = MagicMock()  # type: ignore[method-assign]
    client.select_folder = MagicMock()  # type: ignore[method-assign]
    client.get_capabilities = MagicMock(  # type: ignore[method-assign]
        return_value=capabilities
    )
    return client


class TestFindThreadGmail:
    """Gmail answers with one search on the anchor's thread id."""

    def _setup(self) -> ImapClient:
        client = _client(["IMAP4REV1", "X-GM-EXT-1"])
        client.fetch_email = MagicMock(  # type: ignore[method-assign]
            return_value=_email(7, "<a@x>", 20)
        )
        raw = MagicMock()
        raw.fetch.return_value = {7: {b"X-GM-THRID": 1799}}
        client._client_or_raise = MagicMock(  # type: ignore[method-assign]
            return_value=raw
        )
        client.find_special_use_folder = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda role: "[Gmail]/All Mail" if role == b"\\All" else None
        )
        client.search = MagicMock(return_value=[101, 102])  # type: ignore[method-assign]
        client.fetch_emails = MagicMock(  # type: ignore[method-assign]
            return_value={
                101: _email(101, "<a@x>", 20),
                102: _email(102, "<c@x>", 22),
            }
        )
        return client

    def test_searches_thread_id_in_all_mail(self) -> None:
        client = self._setup()
        client.find_thread(7, "INBOX")
        client.search.assert_called_once_with(  # type: ignore[attr-defined]
            ["X-GM-THRID", 1799], "[Gmail]/All Mail"
        )

    def test_results_newest_first_with_message_id(self) -> None:
        client = self._setup()
        results = client.find_thread(7, "INBOX")
        assert [r["uid"] for r in results] == [102, 101]
        assert results[0]["message_id"] == "<c@x>"
        assert results[0]["folder"] == "[Gmail]/All Mail"

    def test_anchor_folder_when_all_mail_not_advertised(self) -> None:
        client = self._setup()
        client.find_special_use_folder = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        client.find_thread(7, "INBOX")
        assert client.search.call_args.args[1] == "INBOX"  # type: ignore[attr-defined]


class TestFindThreadHeaders:
    """Without Gmail the thread is the messages the headers link."""

    def _setup(self) -> ImapClient:
        client = _client(["IMAP4REV1"])
        client.fetch_email = MagicMock(  # type: ignore[method-assign]
            return_value=_email(7, "<b@x>", 21, ["<a@x>"], ["<a@x>"])
        )
        client.find_special_use_folder = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda role: "Sent" if role == b"\\Sent" else None
        )
        hits: Dict[str, List[int]] = {"INBOX": [5, 7, 9], "Sent": [3]}
        client.search = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda criteria, folder: hits[folder]
        )
        mails = {
            "INBOX": {
                5: _email(5, "<a@x>", 20),
                7: _email(7, "<b@x>", 21),
                9: _email(9, "<d@x>", 23),
            },
            "Sent": {3: _email(3, "<c@x>", 22)},
        }
        client.fetch_emails = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda uids, folder, **kw: {u: mails[folder][u] for u in uids}
        )
        return client

    def test_links_every_known_id_in_one_search_per_folder(self) -> None:
        client = self._setup()
        client.find_thread(7, "INBOX")
        calls = client.search.call_args_list  # type: ignore[attr-defined]
        assert [c.args[1] for c in calls] == ["INBOX", "Sent"]
        criteria = calls[0].args[0]
        for message_id in ("<a@x>", "<b@x>"):
            for header in ("Message-ID", "References", "In-Reply-To"):
                assert any(
                    criteria[i : i + 3] == ["HEADER", header, message_id]
                    for i in range(len(criteria))
                ), (header, message_id)

    def test_each_id_the_anchor_names_is_searched_on_its_own(self) -> None:
        """A comment after the Message-ID and a second parent in
        In-Reply-To each leave one clean identifier to search for."""
        client = self._setup()
        raw = (
            b"From: alice@example.com\r\n"
            b"To: bob@example.com\r\n"
            b"Subject: Loan facility\r\n"
            b"Message-ID: <b@x> (sent by Example Mail)\r\n"
            b"In-Reply-To: <p1@x> (first parent)\r\n"
            b" <p2@x>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        anchor = Email.from_message(email.message_from_bytes(raw), 7, "INBOX")
        client.fetch_email = MagicMock(  # type: ignore[method-assign]
            return_value=anchor
        )
        client.find_thread(7, "INBOX")
        criteria = client.search.call_args_list[0].args[0]  # type: ignore[attr-defined]
        searched = {
            (criteria[i + 1], criteria[i + 2])
            for i in range(len(criteria))
            if criteria[i] == "HEADER"
        }
        assert searched == {
            (header, message_id)
            for message_id in ("<b@x>", "<p1@x>", "<p2@x>")
            for header in ("Message-ID", "References", "In-Reply-To")
        }

    def test_merges_folders_newest_first(self) -> None:
        client = self._setup()
        results = client.find_thread(7, "INBOX")
        assert [(r["folder"], r["uid"]) for r in results] == [
            ("INBOX", 9),
            ("Sent", 3),
            ("INBOX", 7),
            ("INBOX", 5),
        ]

    def test_search_failure_propagates(self) -> None:
        client = self._setup()
        client.search = MagicMock(  # type: ignore[method-assign]
            side_effect=ConnectionError("dropped")
        )
        with pytest.raises(ConnectionError):
            client.find_thread(7, "INBOX")


def test_missing_anchor_raises() -> None:
    client = _client(["IMAP4REV1"])
    client.fetch_email = MagicMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="UID 7 not found in INBOX"):
        client.find_thread(7, "INBOX")


def _cfg() -> CourierConfig:
    block = ImapBlock(host="imap.example.com", port=993, username="u", password="p")
    return CourierConfig(imap_blocks={"acct": block}, _default_imap="acct")


def _invoke(args: List[str], client: MagicMock):
    with (
        patch("courier.__main__.load_config", return_value=_cfg()),
        patch("courier.__main__._make_client", return_value=client),
    ):
        return runner.invoke(app, args)


class TestThreadCli:

    def test_prints_thread_under_op_key_and_block(self) -> None:
        client = MagicMock()
        client.find_thread.return_value = [{"uid": 9, "folder": "INBOX"}]
        result = _invoke(["--imap", "acct", "thread", "-f", "INBOX", "-u", "7"], client)
        assert result.exit_code == 0, result.output
        client.find_thread.assert_called_once_with(7, "INBOX", no_cache=False)
        assert '"thread -f INBOX --uid 7"' in result.output
        assert '"status": "complete"' in result.output

    def test_message_id_resolves_anchor(self) -> None:
        client = MagicMock()
        client.find_thread.return_value = []
        with patch(
            "courier.__main__._resolve_message_id", return_value=("INBOX", 7, None)
        ):
            result = _invoke(
                ["--imap", "acct", "thread", "--message-id", "<b@x>"], client
            )
        assert result.exit_code == 0, result.output
        client.find_thread.assert_called_once_with(7, "INBOX", no_cache=False)

    def test_failure_is_one_error_line(self) -> None:
        client = MagicMock()
        client.find_thread.side_effect = ConnectionError("dropped")
        result = _invoke(["--imap", "acct", "thread", "-f", "INBOX", "-u", "7"], client)
        assert result.exit_code == 1
        assert "Error:" in (result.stderr or result.output)
