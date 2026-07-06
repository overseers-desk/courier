"""Library-level tests for send_with_fcc: the send+FCC orchestration.

Policy (which folder is configured, whether a copy must be kept) lives in
the CLI. This module owns the mechanical order a send-with-copy follows:
verify the FCC target before SMTP opens, transmit, then append the copy.
A post-send append failure is reported, never raised.
"""

from unittest.mock import MagicMock, patch

import pytest

from courier.errors import FccUnresolved, PermanentError
from courier.imap_client import AppendResult
from courier.sending import send_with_fcc


def _smtp_result() -> dict:
    return {
        "message_id_local": "<x@local>",
        "message_id_sent": "<x@local>",
        "smtp_response": "OK",
        "accepted_recipients": ["a@y.com"],
    }


def _fcc_client(resolved: str = "Sent") -> MagicMock:
    c = MagicMock()
    c.resolve_sent_folder.side_effect = lambda configured=None: (
        configured if configured is not None else resolved
    )
    c.append_raw.return_value = AppendResult(uid=999, uidvalidity=7)
    return c


def test_fcc_unresolved_raises_before_smtp():
    client = MagicMock()
    client.resolve_sent_folder.return_value = None
    with patch("courier.smtp_transport.send") as send_mock:
        with pytest.raises(FccUnresolved):
            send_with_fcc("msg", MagicMock(), fcc_client=client, fcc_folder="Ghost")
    send_mock.assert_not_called()  # nothing left SMTP when the target is bogus


def test_send_then_append_reports_uid_and_validity():
    client = _fcc_client(resolved="Sent")
    with patch("courier.smtp_transport.send", return_value=(b"raw", _smtp_result())):
        result = send_with_fcc("msg", MagicMock(), fcc_client=client)
    assert result["fcc_folder"] == "Sent"
    assert result["fcc_uid"] == 999
    assert result["fcc_uidvalidity"] == 7
    assert result["fcc_error"] is None
    args, kwargs = client.append_raw.call_args
    assert args[0] == "Sent"
    assert kwargs["flags"] == (r"\Seen",)


def test_append_failure_is_reported_not_raised():
    client = _fcc_client(resolved="Sent")
    client.append_raw.side_effect = PermanentError("mailbox full")
    with patch("courier.smtp_transport.send", return_value=(b"raw", _smtp_result())):
        result = send_with_fcc("msg", MagicMock(), fcc_client=client)
    assert result["fcc_error"] == "mailbox full"  # message already left
    assert result["fcc_uid"] is None
    assert result["fcc_uidvalidity"] is None


def test_no_fcc_client_skips_fcc_but_still_sends():
    with patch(
        "courier.smtp_transport.send", return_value=(b"raw", _smtp_result())
    ) as send_mock:
        result = send_with_fcc("msg", MagicMock())
    assert result["fcc_folder"] is None
    assert result["fcc_uid"] is None
    send_mock.assert_called_once()


def test_configured_folder_passed_to_resolver():
    client = _fcc_client()
    with patch("courier.smtp_transport.send", return_value=(b"raw", _smtp_result())):
        result = send_with_fcc(
            "msg", MagicMock(), fcc_client=client, fcc_folder="Archive"
        )
    client.resolve_sent_folder.assert_called_once_with(configured="Archive")
    assert result["fcc_folder"] == "Archive"


def test_transport_is_forwarded_to_smtp():
    client = _fcc_client()
    sentinel = object()
    with patch(
        "courier.smtp_transport.send", return_value=(b"raw", _smtp_result())
    ) as send_mock:
        send_with_fcc("msg", MagicMock(), fcc_client=client, transport=sentinel)
    _, kwargs = send_mock.call_args
    assert kwargs["transport"] is sentinel
