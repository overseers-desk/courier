"""CLI tests for the batch mutation verbs.

``--uid/-u`` is repeatable: a single ``-u`` and several ``-u`` flags emit
the same JSON shape. The envelope carries ``matched_uids`` and
``not_found_uids`` from the client (issue #63): any not-found UID flips
``success`` to false and exits 1 naming the UIDs on stderr. Failures map
to typed exit codes: PermanentError -> 1, TransientError -> 3.
"""

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.errors import PermanentError, TransientError

runner = CliRunner()


def _ok(uids) -> dict:
    """The mutation outcome when every requested UID was present."""
    uid_list = uids if isinstance(uids, list) else [uids]
    return {"matched_uids": list(uid_list), "not_found_uids": []}


def _client(result: dict = None) -> MagicMock:
    """A client whose mutations echo the requested UIDs as matched,
    unless *result* pins a specific outcome."""
    client = MagicMock()
    client.mark_email.side_effect = lambda uid, folder, flag, value=True: (
        result or _ok(uid)
    )
    client.move_email.side_effect = lambda uid, src, dst: result or _ok(uid)
    client.delete_email.side_effect = lambda uid, folder: result or _ok(uid)
    client.trash_email.side_effect = lambda uid, folder: {
        "trash_folder": "Trash",
        **(result or _ok(uid)),
    }
    return client


def _invoke(args: list, result: dict = None):
    client = _client(result)
    with patch("courier.__main__._make_client", return_value=client):
        outcome = runner.invoke(app, args)
    return outcome, client


class TestBatchUid:
    """One multi-uid invocation per repeatable verb."""

    def test_move_batch(self):
        result, client = _invoke(
            ["move", "-f", "INBOX", "-u", "10", "-u", "20", "-t", "Archive"]
        )
        assert result.exit_code == 0, result.output
        client.move_email.assert_called_once_with([10, 20], "INBOX", "Archive")
        payload = json.loads(result.output)
        assert payload["success"] is True
        assert payload["matched_uids"] == [10, 20]
        assert payload["not_found_uids"] == []

    def test_mark_read_batch(self):
        result, client = _invoke(["mark-read", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.mark_email.assert_called_once_with([10, 20], "INBOX", r"\Seen", True)
        assert json.loads(result.output)["success"] is True

    def test_mark_unread_batch(self):
        result, client = _invoke(["mark-unread", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.mark_email.assert_called_once_with([10, 20], "INBOX", r"\Seen", False)
        assert json.loads(result.output)["success"] is True

    def test_flag_batch(self):
        result, client = _invoke(["flag", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.mark_email.assert_called_once_with([10, 20], "INBOX", r"\Flagged", True)
        out = json.loads(result.output)
        assert out["success"] is True and out["flagged"] is True

    def test_trash_batch(self):
        result, client = _invoke(["trash", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.trash_email.assert_called_once_with([10, 20], "INBOX")
        out = json.loads(result.output)
        assert out["success"] is True
        assert out["trash_folder"] == "Trash"

    def test_delete_batch(self):
        result, client = _invoke(["delete", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.delete_email.assert_called_once_with([10, 20], "INBOX")
        assert json.loads(result.output)["success"] is True


class TestNotFoundUids:
    """A UID the server never had must not report success (issue #63):
    exit 1, success false, and the missing UIDs named on stderr."""

    def test_partial_delete_exits_1_naming_missing(self):
        result, _client = _invoke(
            ["delete", "-f", "INBOX", "-u", "10", "-u", "20"],
            result={"matched_uids": [10], "not_found_uids": [20]},
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["matched_uids"] == [10]
        assert payload["not_found_uids"] == [20]
        assert "20" in result.stderr
        assert "not found" in result.stderr

    def test_partial_move_exits_1_without_claiming_the_move(self):
        result, _client = _invoke(
            ["move", "-f", "INBOX", "-u", "10", "-u", "20", "-t", "Archive"],
            result={"matched_uids": [10], "not_found_uids": [20]},
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert "message" not in payload
        assert "20" in result.stderr

    def test_all_missing_mark_read_exits_1(self):
        result, _client = _invoke(
            ["mark-read", "-f", "INBOX", "-u", "7"],
            result={"matched_uids": [], "not_found_uids": [7]},
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["success"] is False
        assert "7" in result.stderr

    def test_partial_trash_exits_1(self):
        result, _client = _invoke(
            ["trash", "-f", "INBOX", "-u", "10", "-u", "20"],
            result={"matched_uids": [20], "not_found_uids": [10]},
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["trash_folder"] == "Trash"
        assert "10" in result.stderr

    def test_partial_flag_exits_1(self):
        result, _client = _invoke(
            ["flag", "-f", "INBOX", "-u", "10", "-u", "20"],
            result={"matched_uids": [10], "not_found_uids": [20]},
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["flagged"] is True


class TestTypedExitCodes:
    """PermanentError -> exit 1, TransientError -> exit 3."""

    def test_permanent_error_exits_1(self):
        client = MagicMock()
        client.delete_email.side_effect = PermanentError("no such message")
        with patch("courier.__main__._make_client", return_value=client):
            result = runner.invoke(app, ["delete", "-f", "INBOX", "-u", "7"])
        assert result.exit_code == 1

    def test_transient_error_exits_3(self):
        client = MagicMock()
        client.move_email.side_effect = TransientError("connection reset")
        with patch("courier.__main__._make_client", return_value=client):
            result = runner.invoke(app, ["move", "-f", "INBOX", "-u", "7", "-t", "X"])
        assert result.exit_code == 3
