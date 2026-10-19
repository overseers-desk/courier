"""A failure a CLI verb lets escape reaches the caller as one line, not a crash.

``main()`` is the choke point: a typed courier error or a dropped
connection that any verb leaves uncaught prints ``Error: <reason>`` on
stderr and exits with its retry class, PermanentError 1 and a
connection-layer failure 3, as the mutation verbs already do. Typer's
crash handler is never reached, so no traceback lands in an agent's
transcript.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

import courier.__main__ as cli
from courier.errors import FolderNotFound, PermanentError, TransientError


def _run(monkeypatch: pytest.MonkeyPatch, argv: list, client: MagicMock) -> int:
    """Run ``main()`` on *argv* against *client* and return the exit code."""
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "_make_client", lambda *a, **k: client)
    monkeypatch.setattr(sys, "argv", ["courier", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    code = exc.value.code
    return code if isinstance(code, int) else 1


def _fetch_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.fetch_email.side_effect = exc
    return client


SAVE_NOPE = ["save", "-f", "Nope", "-u", "1", "--attachment", "0", "-o", "/dev/null"]
SAVE_INBOX = ["save", "-f", "INBOX", "-u", "1", "--attachment", "0", "-o", "/dev/null"]
MISSING = FolderNotFound("Failed to select folder Nope: [NONEXISTENT] Unknown Mailbox")


class TestMainChokePoint:
    """Typed failures escaping a verb become one stderr line."""

    def test_permanent_error_is_one_line_exit_1(self, monkeypatch, capsys):
        code = _run(monkeypatch, SAVE_NOPE, _fetch_raising(MISSING))
        err = capsys.readouterr().err
        assert code == 1
        assert err.strip() == f"Error: {MISSING}"
        assert "Traceback" not in err

    def test_transient_error_exits_3(self, monkeypatch, capsys):
        exc = TransientError("connection reset mid-FETCH")
        code = _run(monkeypatch, SAVE_INBOX, _fetch_raising(exc))
        assert code == 3
        assert capsys.readouterr().err.strip() == f"Error: {exc}"

    def test_dropped_connection_exits_3(self, monkeypatch, capsys):
        exc = ConnectionError("FETCH of UID 1 in INBOX failed: socket timeout")
        code = _run(
            monkeypatch,
            ["triage", "-f", "INBOX", "-u", "1", "flag"],
            _fetch_raising(exc),
        )
        assert code == 3
        assert capsys.readouterr().err.strip() == f"Error: {exc}"

    def test_client_disconnects_after_failure(self, monkeypatch):
        client = _fetch_raising(MISSING)
        _run(monkeypatch, SAVE_NOPE, client)
        client.disconnect.assert_called_once_with()


class TestLocalFileErrors:
    """A local file a verb reads or writes fails as one line, exit 1."""

    def test_unwritable_save_target(self, monkeypatch, capsys, tmp_path):
        att = MagicMock()
        email_obj = MagicMock()
        email_obj.save_attachment.side_effect = PermissionError(
            13, "Permission denied", str(tmp_path / "out")
        )
        email_obj.attachments = [att]
        client = MagicMock()
        client.fetch_email.return_value = email_obj
        argv = [
            "save",
            "-f",
            "INBOX",
            "-u",
            "1",
            "--attachment",
            "0",
            "-o",
            str(tmp_path / "out"),
        ]
        assert _run(monkeypatch, argv, client) == 1
        assert capsys.readouterr().err.startswith("Error: [Errno 13]")


class TestAcceptInviteExitCode:
    """``accept-invite`` exits 1 when the workflow reports an error."""

    def test_error_status_exits_1(self, monkeypatch, capsys):
        from courier.workflows import meeting_reply

        monkeypatch.setattr(
            meeting_reply,
            "process_meeting_invite_workflow",
            lambda *a: {
                "status": "error",
                "message": "Error: no folder",
                "draft_uid": None,
            },
        )
        argv = ["accept-invite", "-f", "Nope", "-u", "1"]
        assert _run(monkeypatch, argv, MagicMock()) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_not_invite_exits_0(self, monkeypatch):
        from courier.workflows import meeting_reply

        monkeypatch.setattr(
            meeting_reply,
            "process_meeting_invite_workflow",
            lambda *a: {"status": "not_invite", "message": "", "draft_uid": None},
        )
        argv = ["accept-invite", "-f", "INBOX", "-u", "1"]
        assert _run(monkeypatch, argv, MagicMock()) == 0


class TestAttachmentsExitCodes:
    """``attachments`` keeps exit 1 for "the email has none" alone."""

    def test_no_attachments_exits_1(self, monkeypatch, capsys):
        client = MagicMock()
        client.fetch_email.return_value = MagicMock(attachments=[])
        code = _run(monkeypatch, ["attachments", "-f", "INBOX", "-u", "1"], client)
        assert code == 1
        assert json.loads(capsys.readouterr().out) == []

    def test_missing_folder_exits_2(self, monkeypatch, capsys):
        code = _run(
            monkeypatch,
            ["attachments", "-f", "Nope", "-u", "1"],
            _fetch_raising(MISSING),
        )
        assert code == 2
        assert capsys.readouterr().err.strip() == f"Error: {MISSING}"

    def test_server_refusal_exits_2(self, monkeypatch):
        exc = PermanentError("FETCH of UID 1 in INBOX refused: BAD")
        assert (
            _run(
                monkeypatch,
                ["attachments", "-f", "INBOX", "-u", "1"],
                _fetch_raising(exc),
            )
            == 2
        )

    def test_dropped_connection_exits_2(self, monkeypatch):
        exc = ConnectionError("FETCH of UID 1 in INBOX failed: socket timeout")
        assert (
            _run(
                monkeypatch,
                ["attachments", "-f", "INBOX", "-u", "1"],
                _fetch_raising(exc),
            )
            == 2
        )

    def test_missing_uid_exits_2(self, monkeypatch, capsys):
        client = MagicMock()
        client.fetch_email.return_value = None
        code = _run(monkeypatch, ["attachments", "-f", "INBOX", "-u", "999"], client)
        assert code == 2
        assert "not found" in json.loads(capsys.readouterr().out)["error"]
