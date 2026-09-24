"""Tests for ``read``/``reply --message-id``: resolving a Message-ID to a UID.

The lookup runs a live ``msgid:`` search on the account named by
``--imap`` and refuses, rather than guesses, whenever that search cannot
give one trustworthy answer.
"""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.config import CourierConfig, ImapBlock
from courier.errors import PermanentError

runner = CliRunner()


def _cfg() -> CourierConfig:
    block = ImapBlock(host="imap.example.com", port=993, username="u", password="p")
    return CourierConfig(imap_blocks={"acct": block}, _default_imap="acct")


def _hit(folder: str, uid: int, message_id: str = "<abc@host>") -> dict:
    return {"uid": uid, "folder": folder, "message_id": message_id}


def _envelope(hits: list, **extra: object) -> dict:
    env = {
        "results": hits,
        "provenance": {"source": "remote"},
        "total_count": len(hits),
    }
    env.update(extra)
    return env


def _client(envelope: dict) -> MagicMock:
    c = MagicMock()
    c.search_emails.return_value = envelope
    c.fetch_email.return_value = None
    return c


def _invoke(args: list, client: MagicMock):
    with (
        patch("courier.__main__.load_config", return_value=_cfg()),
        patch("courier.__main__._make_client", return_value=client),
        patch(
            "courier.__main__._fetch_email_result",
            side_effect=lambda cl, folder, uid, no_cache=False: {
                "uid": uid,
                "folder": folder,
            },
        ) as fetch,
    ):
        result = runner.invoke(app, args)
    return result, fetch


def _err(result) -> str:
    return (result.output + (result.stderr or "")).lower()


class TestReadByMessageId:

    def test_resolves_single_hit_and_reads_it(self):
        client = _client(_envelope([_hit("INBOX", 15591)]))
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 0, result.output
        fetch.assert_called_once()
        assert fetch.call_args.args[1:3] == ("INBOX", 15591)

    def test_search_is_live_and_uses_msgid_operator(self):
        client = _client(_envelope([_hit("INBOX", 1)]))
        _invoke(["--imap", "acct", "read", "--message-id", "abc@host"], client)
        kwargs = client.search_emails.call_args.kwargs
        query = client.search_emails.call_args.args[0]
        assert query == "msgid:<abc@host>"
        assert kwargs["no_cache"] is True

    def test_bare_and_bracketed_ids_match_each_other(self):
        client = _client(_envelope([_hit("INBOX", 3, message_id="abc@host")]))
        result, _ = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 0, result.output

    def test_refuses_without_explicit_imap(self):
        client = _client(_envelope([_hit("INBOX", 1)]))
        result, fetch = _invoke(["read", "--message-id", "<abc@host>"], client)
        assert result.exit_code == 2
        assert "--imap" in _err(result)
        client.search_emails.assert_not_called()
        fetch.assert_not_called()

    def test_refuses_when_search_raises(self):
        client = _client(_envelope([]))
        client.search_emails.side_effect = PermanentError("SEARCH BAD")
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 1
        assert "cannot look up" in _err(result)
        assert "search bad" in _err(result)
        fetch.assert_not_called()

    def test_refuses_when_server_ignores_the_key(self):
        # HEADER is a substring match and some servers ignore it outright;
        # a hit whose Message-ID differs means the answer cannot be trusted.
        client = _client(
            _envelope([_hit("INBOX", 1), _hit("INBOX", 2, message_id="<other@x>")])
        )
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 1
        assert "cannot look up" in _err(result)
        fetch.assert_not_called()

    def test_refuses_when_a_folder_failed(self):
        client = _client(
            _envelope([], folders_failed=[{"folder": "Archive", "error": "NO"}])
        )
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 1
        assert "cannot look up" in _err(result)
        assert "archive" in _err(result)
        fetch.assert_not_called()

    def test_not_found(self):
        client = _client(_envelope([]))
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 1
        assert "not found" in _err(result)
        fetch.assert_not_called()

    def test_copies_in_several_folders_ask_for_folder(self):
        client = _client(_envelope([_hit("INBOX", 1), _hit("Sent", 9)]))
        result, fetch = _invoke(
            ["--imap", "acct", "read", "--message-id", "<abc@host>"], client
        )
        assert result.exit_code == 1
        assert "inbox" in _err(result) and "sent" in _err(result)
        assert "-f" in _err(result)
        fetch.assert_not_called()

    def test_folder_narrows_the_search(self):
        client = _client(_envelope([_hit("Sent", 9)]))
        result, fetch = _invoke(
            ["--imap", "acct", "read", "-f", "Sent", "--message-id", "<abc@host>"],
            client,
        )
        assert result.exit_code == 0, result.output
        assert client.search_emails.call_args.kwargs["folder"] == "Sent"
        assert fetch.call_args.args[1:3] == ("Sent", 9)

    def test_uid_and_message_id_are_exclusive(self):
        client = _client(_envelope([]))
        result, _ = _invoke(
            [
                "--imap",
                "acct",
                "read",
                "-f",
                "INBOX",
                "-u",
                "1",
                "--message-id",
                "<abc@host>",
            ],
            client,
        )
        assert result.exit_code == 2

    def test_neither_uid_nor_message_id(self):
        result, _ = _invoke(["read", "-f", "INBOX"], _client(_envelope([])))
        assert result.exit_code == 2

    def test_uid_still_needs_folder(self):
        result, _ = _invoke(["read", "-u", "5"], _client(_envelope([])))
        assert result.exit_code == 2
        assert "-f" in _err(result)


class TestReplyByMessageId:

    def test_reply_fetches_the_resolved_message(self):
        client = _client(_envelope([_hit("[Gmail]/All Mail", 1133421)]))
        with (
            patch("courier.__main__.load_config", return_value=_cfg()),
            patch("courier.__main__._make_client", return_value=client),
        ):
            runner.invoke(
                app,
                [
                    "--imap",
                    "acct",
                    "reply",
                    "--message-id",
                    "<abc@host>",
                    "--body",
                    "thanks",
                ],
            )
        client.fetch_email.assert_called_once_with(1133421, "[Gmail]/All Mail")

    def test_reply_refuses_without_explicit_imap(self):
        client = _client(_envelope([_hit("INBOX", 1)]))
        with (
            patch("courier.__main__.load_config", return_value=_cfg()),
            patch("courier.__main__._make_client", return_value=client),
        ):
            result = runner.invoke(
                app, ["reply", "--message-id", "<abc@host>", "--body", "thanks"]
            )
        assert result.exit_code == 2
        client.search_emails.assert_not_called()
