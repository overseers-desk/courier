"""Tests for ``ImapClient.fetch_by_message_id`` and the client as a context manager.

The client is real; its seams to the mu index and the server are
replaced on the instance, so the cache-then-live lookup, and the choice
between the UID read and the file read it leads to, run as written.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from courier import AmbiguousMessageId
from courier.__main__ import app
from courier.config import CourierConfig, ImapBlock
from courier.errors import CourierError, PermanentError, TransientError
from courier.imap_client import ImapClient
from courier.models import Email, EmailAddress, EmailContent
from courier.query.ast import UntranslatableForBackend

MID = "abc@host"

RAW = (
    "From: Alice <alice@example.com>\r\n"
    "To: me@example.com\r\n"
    "Subject: On disk\r\n"
    f"Message-ID: <{MID}>\r\n"
    "Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    "Content-Type: text/plain\r\n"
    "\r\n"
    "Body read from disk.\r\n"
)


def _block() -> ImapBlock:
    return ImapBlock(host="imap.example.com", port=993, username="u", password="p")


def _email(folder: str = "INBOX", uid: int = 42) -> Email:
    return Email(
        message_id=f"<{MID}>",
        subject="Found",
        from_=EmailAddress(name="Alice", address="alice@example.com"),
        to=[EmailAddress(name="", address="me@example.com")],
        date=datetime(2026, 4, 1, 10, 0, 0),
        content=EmailContent(text="body"),
        flags=["\\Seen"],
        folder=folder,
        uid=uid,
    )


def _hit(folder: str, uid: int, message_id: str = f"<{MID}>") -> Dict[str, Any]:
    return {"uid": uid, "folder": folder, "message_id": message_id}


def _cache_hit(folder: str, path: Any, uid: Optional[int] = None) -> Dict[str, Any]:
    hit: Dict[str, Any] = {
        "folder": folder,
        "path": str(path),
        "message_id": f"<{MID}>",
    }
    if uid is not None:
        hit["uid"] = uid
    return hit


def _envelope(hits: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    env: Dict[str, Any] = {
        "results": hits,
        "provenance": {"source": "remote"},
        "total_count": len(hits),
        "truncated": False,
    }
    env.update(extra)
    return env


@pytest.fixture
def client() -> ImapClient:
    c = ImapClient(_block())
    c.local_message_id_hits = MagicMock(return_value=None)
    c.search_emails = MagicMock(return_value=_envelope([]))
    c.fetch_email = MagicMock(return_value=None)
    return c


def _read_by_message_id(client: ImapClient) -> Any:
    """Run ``read --message-id`` on the CLI with *client* as its account."""
    cfg = CourierConfig(imap_blocks={"acct": client.block}, _default_imap="acct")
    with (
        patch("courier.__main__.load_config", return_value=cfg),
        patch("courier.__main__._make_client", return_value=client),
    ):
        return CliRunner().invoke(
            app, ["--imap", "acct", "read", "--message-id", f"<{MID}>"]
        )


class TestOutcomes:

    def test_one_folder_gives_the_email(self, client: ImapClient):
        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
        client.fetch_email.return_value = _email()

        found = client.fetch_by_message_id(f"<{MID}>")

        assert found is client.fetch_email.return_value
        client.search_emails.assert_called_once_with(
            f"msgid:<{MID}>", folder=None, limit=20, no_cache=True
        )
        client.fetch_email.assert_called_once_with(42, "INBOX", no_cache=False)

    def test_no_folder_holding_it_gives_none(self, client: ImapClient):
        assert client.fetch_by_message_id(MID) is None
        client.fetch_email.assert_not_called()

    def test_a_failed_folder_is_transient(self, client: ImapClient):
        client.search_emails.return_value = _envelope(
            [], folders_failed=[{"folder": "Archive", "error": "NO"}]
        )

        with pytest.raises(TransientError, match="search failed in Archive"):
            client.fetch_by_message_id(MID)
        client.fetch_email.assert_not_called()

    def test_a_server_ignoring_the_search_is_permanent(self, client: ImapClient):
        client.search_emails.return_value = _envelope(
            [_hit("INBOX", 1), _hit("INBOX", 2, message_id="<other@x>")]
        )

        with pytest.raises(PermanentError, match="other Message-IDs"):
            client.fetch_by_message_id(MID)
        client.fetch_email.assert_not_called()

    def test_copies_in_several_folders_are_ambiguous(self, client: ImapClient):
        client.search_emails.return_value = _envelope(
            [_hit("Sent", 9), _hit("INBOX", 1), _hit("INBOX", 4)]
        )

        with pytest.raises(AmbiguousMessageId) as info:
            client.fetch_by_message_id(MID)

        assert info.value.matches == [("INBOX", 1), ("INBOX", 4), ("Sent", 9)]
        assert info.value.folders == ["INBOX", "Sent"]
        assert isinstance(info.value, CourierError)
        assert not isinstance(info.value, PermanentError)
        client.fetch_email.assert_not_called()

    def test_a_message_gone_after_the_search_is_transient(self, client: ImapClient):
        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])

        with pytest.raises(TransientError, match="INBOX"):
            client.fetch_by_message_id(MID)


class TestErrorMappings:

    def test_a_connection_failure_while_searching_is_transient(
        self, client: ImapClient
    ):
        refused = ConnectionError("Failed to connect to IMAP server: down")
        client.search_emails.side_effect = refused

        with pytest.raises(TransientError) as info:
            client.fetch_by_message_id(MID)
        assert info.value.__cause__ is refused

    def test_a_connection_failure_while_reading_is_transient(self, client: ImapClient):
        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
        client.fetch_email.side_effect = ConnectionError("down")

        with pytest.raises(TransientError):
            client.fetch_by_message_id(MID)

    def test_a_search_that_loses_its_connection_stays_transient(
        self, client: ImapClient
    ):
        dropped = TransientError("connection aborted")
        client.search_emails.side_effect = dropped

        with pytest.raises(TransientError) as info:
            client.fetch_by_message_id(MID)
        assert str(info.value) == (
            "cannot look up Message-ID on this account: connection aborted"
        )
        assert info.value.__cause__ is dropped

        result = _read_by_message_id(client)
        assert result.exit_code == 1
        assert result.stderr == (
            "Error: cannot look up Message-ID on this account: connection aborted\n"
        )

    @pytest.mark.parametrize(
        "refusal",
        [
            UntranslatableForBackend("imap", "msgid:", "no HEADER search"),
            PermanentError("SEARCH BAD"),
            CourierError("SEARCH BAD"),
        ],
        ids=["untranslatable", "permanent-error", "courier-error"],
    )
    def test_a_search_that_cannot_run_is_permanent(
        self, client: ImapClient, refusal: Exception
    ):
        client.search_emails.side_effect = refusal

        with pytest.raises(PermanentError) as info:
            client.fetch_by_message_id(MID)
        assert str(info.value) == (
            f"cannot look up Message-ID on this account: {refusal}"
        )

    def test_a_folder_not_allowed_propagates(self, client: ImapClient):
        client.search_emails.side_effect = ValueError("Folder 'X' is not allowed")

        with pytest.raises(ValueError, match="not allowed"):
            client.fetch_by_message_id(MID, folders=["X"])


class TestFolders:

    def test_each_folder_asked_is_searched_once(self, client: ImapClient):
        client.search_emails.side_effect = lambda q, folder, **kw: _envelope(
            [_hit("B", 5)] if folder == "B" else []
        )
        client.fetch_email.return_value = _email("B", 5)

        found = client.fetch_by_message_id(MID, folders=["A", "B"])

        assert found is client.fetch_email.return_value
        assert [c.kwargs["folder"] for c in client.search_emails.call_args_list] == [
            "A",
            "B",
        ]
        assert client.local_message_id_hits.call_args_list == [
            call(MID, "A"),
            call(MID, "B"),
        ]
        client.fetch_email.assert_called_once_with(5, "B", no_cache=False)

    def test_one_folder_name_in_place_of_a_list_is_refused(self, client: ImapClient):
        with pytest.raises(TypeError):
            client.fetch_by_message_id(MID, folders="INBOX")
        client.search_emails.assert_not_called()


class TestCache:

    def test_a_cache_hit_makes_no_live_call(self, client: ImapClient):
        client.local_message_id_hits.return_value = [
            _cache_hit("INBOX", "/mail/INBOX/cur/1,U=7:2,S", uid=7)
        ]
        client.fetch_email.return_value = _email(uid=7)

        client.fetch_by_message_id(MID)

        client.search_emails.assert_not_called()
        client.fetch_email.assert_called_once_with(7, "INBOX", no_cache=False)

    def test_cache_copies_in_several_folders_are_ambiguous_with_no_live_call(
        self, client: ImapClient
    ):
        client.local_message_id_hits.return_value = [
            _cache_hit("B", "/mail/B/cur/2,U=7:2,S", uid=7),
            _cache_hit("A", "/mail/A/cur/1,U=3:2,S", uid=3),
        ]

        with pytest.raises(AmbiguousMessageId) as info:
            client.fetch_by_message_id(MID)

        assert info.value.matches == [("A", 3), ("B", 7)]
        client.search_emails.assert_not_called()

    def test_cache_copies_lacking_a_uid_leave_it_to_the_live_search(
        self, client: ImapClient, tmp_path: Path
    ):
        client.local_message_id_hits.return_value = [
            _cache_hit("A", tmp_path / "a:2,S"),
            _cache_hit("B", "/mail/B/cur/2,U=7:2,S", uid=7),
        ]
        client.search_emails.return_value = _envelope([_hit("B", 7)])
        client.fetch_email.return_value = _email("B", 7)

        assert client.fetch_by_message_id(MID) is client.fetch_email.return_value
        client.search_emails.assert_called_once()

    def test_a_file_without_a_uid_is_read_from_disk(
        self, client: ImapClient, tmp_path: Path
    ):
        path = tmp_path / "1700000000.1.host:2,S"
        path.write_text(RAW)
        client.local_message_id_hits.return_value = [_cache_hit("INBOX", path)]

        found = client.fetch_by_message_id(MID)

        assert found is not None
        assert found.uid is None
        assert found.folder == "INBOX"
        assert "Body read from disk." in (found.content.text or "")
        client.search_emails.assert_not_called()
        client.fetch_email.assert_not_called()


class TestReadParity:

    @pytest.mark.parametrize("shape", ["uid", "path"])
    def test_to_dict_is_the_read_commands_object(
        self, client: ImapClient, tmp_path: Path, shape: str
    ):
        if shape == "uid":
            client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
            client.fetch_email.return_value = _email()
        else:
            path = tmp_path / "1700000000.1.host:2,S"
            path.write_text(RAW)
            client.local_message_id_hits.return_value = [_cache_hit("INBOX", path)]

        result = _read_by_message_id(client)

        assert result.exit_code == 0, result.output
        inner = next(iter(json.loads(result.stdout).values()))["acct"]
        found = client.fetch_by_message_id(f"<{MID}>")
        assert found is not None
        assert inner == found.to_dict()


class TestContextManager:

    def test_entering_does_not_connect(self):
        client = ImapClient(_block())
        with patch("courier.imap_client.imapclient.IMAPClient") as server:
            with client as entered:
                assert entered is client
                server.assert_not_called()
                assert not client.connected

    def test_leaving_disconnects_once(self):
        client = ImapClient(_block())
        with (
            patch("courier.imap_client.imapclient.IMAPClient") as server,
            patch.object(client, "disconnect", wraps=client.disconnect) as leave,
        ):
            with client:
                client.ensure_connected()
                server.return_value.login.assert_called_once_with("u", "p")
            leave.assert_called_once_with()
            server.return_value.logout.assert_called_once_with()
        assert not client.connected

    def test_an_exception_in_the_block_disconnects_and_propagates(self):
        client = ImapClient(_block())
        with (
            patch("courier.imap_client.imapclient.IMAPClient") as server,
            patch.object(client, "disconnect", wraps=client.disconnect) as leave,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                with client:
                    client.ensure_connected()
                    raise RuntimeError("boom")
            leave.assert_called_once_with()
            server.return_value.logout.assert_called_once_with()
        assert not client.connected


class TestRawErrorsFromTheServer:
    """A socket error, a timeout or an imapclient error escaping the
    search or the read lands in the taxonomy the docstring promises, as a
    ConnectionError does."""

    def test_a_timeout_while_reading_is_transient(self, client: ImapClient):
        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
        stalled = TimeoutError("The read operation timed out")
        client.fetch_email.side_effect = stalled

        with pytest.raises(TransientError) as info:
            client.fetch_by_message_id(MID)
        assert info.value.__cause__ is stalled

    def test_a_server_refusal_while_reading_is_permanent(self, client: ImapClient):
        from imapclient.exceptions import IMAPClientError

        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
        client.fetch_email.side_effect = IMAPClientError("FETCH failed: BAD")

        with pytest.raises(PermanentError, match="BAD"):
            client.fetch_by_message_id(MID)

    def test_a_socket_error_while_searching_is_transient(self, client: ImapClient):
        import ssl

        dropped = ssl.SSLError(1, "EOF occurred in violation of protocol")
        client.search_emails.side_effect = dropped

        with pytest.raises(TransientError) as info:
            client.fetch_by_message_id(MID)
        assert info.value.__cause__ is dropped


class TestNoCache:
    """``no_cache=True`` skips the local cache for the lookup and the read,
    as it does on fetch_email."""

    def test_no_cache_asks_the_server_for_both_steps(self, client: ImapClient):
        client.local_message_id_hits.return_value = [_cache_hit("INBOX", "/x", 42)]
        client.search_emails.return_value = _envelope([_hit("INBOX", 42)])
        client.fetch_email.return_value = _email()

        assert client.fetch_by_message_id(MID, no_cache=True) is not None
        client.local_message_id_hits.assert_not_called()
        client.fetch_email.assert_called_once_with(42, "INBOX", no_cache=True)

    def test_the_cache_is_asked_by_default(self, client: ImapClient):
        client.local_message_id_hits.return_value = [_cache_hit("INBOX", "/x", 42)]
        client.fetch_email.return_value = _email()

        client.fetch_by_message_id(MID)
        client.local_message_id_hits.assert_called()
        client.fetch_email.assert_called_once_with(42, "INBOX", no_cache=False)
        client.search_emails.assert_not_called()
