"""CLI tests for the batch mutation verbs.

``--uid/-u`` is repeatable: a single ``-u`` and several ``-u`` flags emit
the same JSON shape, with one success covering the whole batch. Failures
map to typed exit codes: PermanentError -> 1, TransientError -> 3.
"""

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from courier.__main__ import app
from courier.errors import PermanentError, TransientError

runner = CliRunner()


def _invoke(args: list):
    client = MagicMock()
    with patch("courier.__main__._make_client", return_value=client):
        result = runner.invoke(app, args)
    return result, client


class TestBatchUid:
    """One multi-uid invocation per repeatable verb."""

    def test_move_batch(self):
        result, client = _invoke(
            ["move", "-f", "INBOX", "-u", "10", "-u", "20", "-t", "Archive"]
        )
        assert result.exit_code == 0, result.output
        client.move_email.assert_called_once_with([10, 20], "INBOX", "Archive")
        assert json.loads(result.output)["success"] is True

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
        assert json.loads(result.output)["success"] is True

    def test_delete_batch(self):
        result, client = _invoke(["delete", "-f", "INBOX", "-u", "10", "-u", "20"])
        assert result.exit_code == 0, result.output
        client.delete_email.assert_called_once_with([10, 20], "INBOX")
        assert json.loads(result.output)["success"] is True


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
