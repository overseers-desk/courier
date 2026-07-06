"""Send + FCC orchestration, lifted out of the CLI.

The CLI keeps identity and flag *policy* (which folder is configured,
whether a copy must be retained). This module owns the mechanical
sequence a send-with-copy always follows:

1. Resolve and verify the FCC target BEFORE opening SMTP, so a bogus
   Sent folder refuses the send instead of transmitting and then losing
   the only local copy. A miss raises :class:`FccUnresolved`.
2. Transmit via ``smtp_transport.send``.
3. APPEND the sent bytes to the FCC folder. A failure here is reported
   in the result dict (``fcc_error``), never raised: the message has
   already left, so raising would be a lie about whether it was sent.
"""

from typing import Any, Callable, Optional

from courier import smtp_transport
from courier.config import SmtpConfig
from courier.errors import FccUnresolved
from courier.imap_client import SENT_FOLDER_CANDIDATES, ImapClient


def send_with_fcc(
    mime_message: Any,
    smtp: SmtpConfig,
    fcc_client: Optional[ImapClient] = None,
    fcc_folder: Optional[str] = None,
    fcc_flags: tuple = (r"\Seen",),
    transport: Optional[Callable] = None,
) -> dict:
    """Send *mime_message* via SMTP and file a copy (FCC) when requested.

    Args:
        mime_message: Built MIME message ready to serialise.
        smtp: Resolved ``SmtpConfig`` with credentials filled in.
        fcc_client: Connected ``ImapClient`` for the FCC step, or ``None``
            to skip FCC entirely.
        fcc_folder: Configured Sent folder name (from ``identity.fcc`` or
            ``--sent-folder``). ``None`` means auto-discover.
        fcc_flags: Flags for the appended copy. Defaults to ``\\Seen``.
        transport: Optional ``smtplib.SMTP``-shaped factory, forwarded to
            the transport (tests inject a fake).

    Returns:
        The transport result (``message_id_local``, ``message_id_sent``,
        ``smtp_response``, ``accepted_recipients``) plus ``fcc_folder``,
        ``fcc_uid``, ``fcc_uidvalidity``, and ``fcc_error`` (``None`` on a
        clean append; the exception text on a post-send append failure).

    Raises:
        FccUnresolved: The FCC target cannot be resolved. Raised before
            SMTP opens, so nothing was sent.
        Exception: Any SMTP-layer failure from the transport propagates.
    """
    fcc_target: Optional[str] = None
    if fcc_client is not None:
        fcc_target = fcc_client.resolve_sent_folder(configured=fcc_folder)
        if fcc_target is None:
            raise FccUnresolved(configured=fcc_folder, tried=SENT_FOLDER_CANDIDATES)

    fcc_bytes, send_result = smtp_transport.send(
        mime_message, smtp, transport=transport
    )

    result: dict = {
        **send_result,
        "fcc_folder": fcc_target,
        "fcc_uid": None,
        "fcc_uidvalidity": None,
        "fcc_error": None,
    }
    if fcc_client is not None and fcc_target is not None:
        try:
            appended = fcc_client.append_raw(fcc_target, fcc_bytes, flags=fcc_flags)
            result["fcc_uid"] = appended.uid
            result["fcc_uidvalidity"] = appended.uidvalidity
        except Exception as exc:  # message already sent; report, do not raise
            result["fcc_error"] = str(exc)
    return result
