"""Tests for the trash action: recoverable removal via the server's Trash/Bin.

`trash_email` resolves the Trash folder (SPECIAL-USE ``\\Trash`` first, then
common Bin/Trash names) and moves the message there, which is the removal
path that actually works on Gmail. A message already in the Trash is expunged
in place; a server with no resolvable Trash raises rather than silently
expunging.
"""

from unittest.mock import patch

import pytest

from courier.config import ImapBlock
from courier.errors import FolderNotFound
from courier.imap_client import ImapClient


def _client() -> ImapClient:
    return ImapClient(
        ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
    )


def test_resolve_trash_prefers_special_use():
    client = _client()
    with patch.object(client, "find_special_use_folder", return_value="[Gmail]/Bin"):
        assert client.resolve_trash_folder() == "[Gmail]/Bin"


def test_resolve_trash_falls_back_to_known_name():
    client = _client()
    client.folder_cache = {"INBOX": [], "Trash": []}
    with patch.object(client, "find_special_use_folder", return_value=None):
        assert client.resolve_trash_folder() == "Trash"


def test_resolve_trash_returns_none_when_absent():
    client = _client()
    client.folder_cache = {"INBOX": [], "Archive": []}
    with patch.object(client, "find_special_use_folder", return_value=None):
        assert client.resolve_trash_folder() is None


def test_trash_moves_to_resolved_bin():
    client = _client()
    with (
        patch.object(client, "ensure_connected"),
        patch.object(client, "resolve_trash_folder", return_value="[Gmail]/Bin"),
        patch.object(client, "move_email") as mock_move,
        patch.object(client, "delete_email") as mock_delete,
    ):
        assert client.trash_email(12345, "INBOX") == "[Gmail]/Bin"
        mock_move.assert_called_once_with(12345, "INBOX", "[Gmail]/Bin")
        mock_delete.assert_not_called()


def test_trash_of_message_already_in_bin_expunges_in_place():
    client = _client()
    with (
        patch.object(client, "ensure_connected"),
        patch.object(client, "resolve_trash_folder", return_value="[Gmail]/Bin"),
        patch.object(client, "move_email") as mock_move,
        patch.object(client, "delete_email") as mock_delete,
    ):
        assert client.trash_email(12345, "[Gmail]/Bin") == "[Gmail]/Bin"
        mock_delete.assert_called_once_with(12345, "[Gmail]/Bin")
        mock_move.assert_not_called()


def test_trash_raises_when_no_bin_resolves():
    client = _client()
    with (
        patch.object(client, "ensure_connected"),
        patch.object(client, "resolve_trash_folder", return_value=None),
    ):
        with pytest.raises(FolderNotFound, match="No Trash/Bin folder"):
            client.trash_email(12345, "INBOX")


def test_triage_trash_action_routes_to_trash_email():
    client = _client()
    with patch.object(client, "trash_email", return_value="Trash") as mock_trash:
        message = client.process_email_action(12345, "INBOX", "trash")
        assert message == "Email trashed"
        mock_trash.assert_called_once_with(12345, "INBOX")
