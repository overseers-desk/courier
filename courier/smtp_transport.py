"""SMTP transport: hand-driven smtplib session that captures the post-DATA reply.

Why hand-driven instead of ``smtplib.SMTP.sendmail``: ``sendmail()`` discards
the server response after DATA. Some smarthosts (notably AWS SES) carry a
tracking token in that line (``250 Ok 0102019...``). Driving MAIL, RCPT
and DATA ourselves preserves the ``(code, response)`` reply to the
message, and lets the wait for that reply run under its own, longer
timeout (see ``_transmit_data``).

This module also implements the SES Message-ID rewrite: SES replaces the
client-set ``Message-ID`` header in transit with its own
``<token>@email.amazonses.com``. When ``rewrite_msgid_from_response`` is
true on the SmtpConfig, we mirror that rewrite locally so the bytes
APPENDed to Sent match the message the recipient actually receives. This is
the "FCC instead of BCC-self" capability discussed in the design.
"""

import re
import smtplib
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from typing import Any, Callable, Dict, List, Optional, Tuple

from courier.config import SmtpConfig, tls_context
from courier.errors import CourierError, PermanentError

# Socket timeout for each read or write in the SMTP session, in seconds:
# connecting, each command and its reply, and each chunk of the message
# upload. Matches the IMAP side's connect timeout (imap_client passes
# timeout=10) so a stalled server cannot hang a send forever.
_SMTP_TIMEOUT = 10

# Wait for the reply to the end-of-data dot, in seconds. RFC 5321
# section 4.5.3.2.6 gives the server 10 minutes here, which one that
# scans the message before replying may use; giving up sooner reports
# a failure for a message the server then queues, and a re-send
# delivers a duplicate.
_DATA_REPLY_TIMEOUT = 600

# Bytes per write of the message upload. A single sendall() of the whole
# message runs under one deadline, so a large attachment on a slow link
# would fail at 10 seconds; in chunks, each write gets its own. 64 KiB
# per 10 seconds fails only a link slower than about 50 kbit/s.
_DATA_CHUNK = 64 * 1024

# Post-DATA "Ok <token>" line from SES. Real tokens look like
# "010f01a8e6c8e7d2-9eaeb4d6-fcb5-4b04-bc89-3d8e36f5db7e-000000": hex chunks
# joined by dashes. The leading "Ok" gates this to providers that follow
# that shape; the rewrite is also gated per-SMTP-block by
# `rewrite_msgid_from_response`, so only SES-style endpoints try to match.
_SES_RESPONSE_RE = re.compile(rb"^\s*Ok\s+([0-9a-fA-F][0-9a-fA-F-]*)", re.IGNORECASE)


# send() returns (fcc_bytes, result_dict) where result_dict has these keys:
#   message_id_local      Message-ID header as written at send time.
#   message_id_sent       Message-ID the recipient sees. Equal to
#                         message_id_local unless the SMTP block requested
#                         rewrite_msgid_from_response and the server returned
#                         a recognisable tracking token (e.g. SES).
#   smtp_response         Decoded response after DATA, e.g. "Ok 0102019...".
#   accepted_recipients   Bare addresses the server accepted at RCPT TO.
#   refused_recipients    [{addr, code, reply}] for addresses the server
#                         refused at RCPT TO. Non-empty means the message
#                         was delivered to a subset only; callers report
#                         status "partial".


def _extract_addresses(mime: Any, header: str) -> List[str]:
    """Return the bare addresses from a recipient header.

    Parsed with :func:`email.utils.getaddresses` so a quoted display
    name containing a comma (``'"Smith, John" <js@x>'`` — a shape
    courier itself produces via ``formataddr``) yields one address, not
    junk fragments that the server would 501-reject or a relay would
    qualify and misdeliver.
    """
    values = mime.get_all(header) or []
    return [addr for _name, addr in getaddresses([str(v) for v in values]) if addr]


def _from_address(mime: Any) -> str:
    """Return the bare address from the From header.

    Parsed with :func:`email.utils.parseaddr` so a quoted display name
    containing ``<`` (``'"Acme <Billing>" <billing@acme.com>'``) yields
    the address, not a fragment of the name that would corrupt MAIL FROM.

    Returns:
        The address, or ``""`` when the header is absent or holds no
        single parseable address.
    """
    return parseaddr(str(mime.get("From", "") or ""))[1]


def _all_recipients(mime: Any) -> List[str]:
    """Combine To, Cc, Bcc into the RCPT TO list, deduplicated, order-preserving."""
    seen: set = set()
    out: List[str] = []
    for header in ("To", "Cc", "Bcc"):
        for addr in _extract_addresses(mime, header):
            lc = addr.lower()
            if lc and lc not in seen:
                seen.add(lc)
                out.append(addr)
    return out


def _strip_bcc(raw_bytes: bytes) -> bytes:
    """Drop the Bcc header from raw RFC 822 bytes.

    Bcc lives on the MIME object so we can derive RCPT TO from it, but it
    must not appear on the wire or in the FCC copy.
    """
    msg = BytesParser().parsebytes(raw_bytes)
    if "Bcc" in msg:
        del msg["Bcc"]
    return msg.as_bytes()


def _to_crlf(raw_bytes: bytes) -> bytes:
    """Normalise line endings to CRLF for the SMTP wire.

    ``as_bytes()`` serialises with bare LFs and ``smtplib.data()``
    transmits bytes as-is (its ``_fix_eols`` pass applies to str input
    only), so without this step bare LFs reach the wire in violation of
    RFC 5321 section 2.3.8 — a hard reject on qmail, hardened Postfix,
    and Exchange Online. The same bytes are APPENDed as the FCC copy,
    which strict IMAP servers also require to be CRLF.
    """
    return (
        raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    )


def parse_ses_token(response_bytes: Any) -> Optional[str]:
    """Extract the SES tracking token from a post-DATA "Ok <token>" reply.

    Returns the token string when the response matches the SES shape,
    otherwise None. The SES post-DATA reply is "Ok <hex-token>"; non-SES
    smarthosts return prose ("queued as ABC123" etc.) that this regex
    deliberately rejects.
    """
    if response_bytes is None:
        return None
    if isinstance(response_bytes, str):
        response_bytes = response_bytes.encode("utf-8", errors="replace")
    m = _SES_RESPONSE_RE.match(response_bytes)
    return m.group(1).decode("ascii") if m else None


def rewrite_message_id(raw_bytes: bytes, new_msgid: str) -> bytes:
    """Replace the Message-ID header in raw RFC 822 bytes.

    Used so the FCC copy carries the same Message-ID the recipient sees
    when an upstream rewrites the header (SES does this on every send).
    """
    msg = BytesParser().parsebytes(raw_bytes)
    if "Message-ID" in msg:
        del msg["Message-ID"]
    msg["Message-ID"] = new_msgid
    return msg.as_bytes()


def open_session(
    smtp_cfg: SmtpConfig,
    transport: Optional[Callable[..., smtplib.SMTP]] = None,
) -> smtplib.SMTP:
    """Open an SMTP session: connect, EHLO, TLS, and log in.

    The one opener behind ``send``, the ``courier status`` probe and
    send-draft ``--dry-run``, so each connects under ``_SMTP_TIMEOUT``
    and the same TLS rules. Port 465 is implicit TLS (``smtplib.SMTP_SSL``).
    Any other port connects plain and upgrades with STARTTLS whenever EHLO
    offers it, then sends EHLO again (RFC 3207 section 4.2). Every TLS
    context comes from :func:`courier.config.tls_context`: the server's
    certificate and host name are checked unless the block sets
    ``insecure_skip_verify``. A block with credentials logs in only over
    TLS, unless it sets ``insecure_plaintext``; a block without them
    (a template, or an open relay) proceeds either way.

    Args:
        smtp_cfg: Resolved SmtpConfig with credentials filled in.
        transport: Optional ``smtplib.SMTP``-shaped factory, called as
            ``transport(host, port, timeout=...)`` and, on port 465, with
            ``context=`` as well. Defaults to ``smtplib.SMTP_SSL`` on 465
            and ``smtplib.SMTP`` otherwise. Tests inject a fake here.

    Returns:
        The open connection, past EHLO, TLS and login, ready for MAIL
        FROM or QUIT.

    Raises:
        PermanentError: The block has credentials, the session has no
            TLS, and ``insecure_plaintext`` is not set; the socket is
            closed and no login was attempted.
        smtplib.SMTPException: On any SMTP-layer failure, STARTTLS and
            login included; the socket is closed.
        OSError: The connection could not be made, or the TLS handshake
            failed (``ssl.SSLCertVerificationError`` on a certificate
            that does not verify); the socket is closed.
    """
    context = tls_context(smtp_cfg.insecure_skip_verify)
    host, port = smtp_cfg.host, smtp_cfg.port
    implicit_tls = port == 465
    if transport is not None:
        if implicit_tls:
            smtp = transport(host, port, timeout=_SMTP_TIMEOUT, context=context)
        else:
            smtp = transport(host, port, timeout=_SMTP_TIMEOUT)
    elif implicit_tls:
        smtp = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
    try:
        smtp.ehlo()
        encrypted = implicit_tls
        if not encrypted and smtp.has_extn("starttls"):
            smtp.starttls(context=context)
            smtp.ehlo()
            encrypted = True
        if smtp_cfg.username and smtp_cfg.password:
            if not encrypted and not smtp_cfg.insecure_plaintext:
                raise PermanentError(
                    f"refusing to log in to {host}:{port} without TLS: the "
                    "server offers no STARTTLS, so the password would go out "
                    "in clear text. Use port 465 or a port with STARTTLS, or "
                    "set insecure_plaintext = true on the [smtp] block to "
                    "allow it."
                )
            smtp.login(smtp_cfg.username, smtp_cfg.password)
    except BaseException:
        smtp.close()
        raise
    return smtp


def _transmit_data(
    smtp: smtplib.SMTP, on_wire: bytes, host: str, msgid: str
) -> Tuple[int, bytes]:
    """Run the DATA exchange and return the server's reply to the message.

    Driven with smtplib's ``docmd``, ``send`` and ``getreply`` rather than
    ``data()``, which uploads the message and reads the reply in one call
    under one timeout. Here the DATA command, its 354 reply and each
    ``_DATA_CHUNK`` of the upload keep the session's ``_SMTP_TIMEOUT``;
    only the wait for the reply to the end-of-data dot gets
    ``_DATA_REPLY_TIMEOUT``.

    Args:
        smtp: Session with MAIL FROM and at least one RCPT TO accepted.
        on_wire: The message as it goes on the wire, CRLF line endings.
        host: The server's host name, for the outcome-unknown message.
        msgid: The message's Message-ID, for the outcome-unknown message.

    Returns:
        ``(code, reply)``: the server's reply to the end-of-data dot.

    Raises:
        smtplib.SMTPDataError: The server answered DATA with other than
            354. Raised before the upload, so nothing was transmitted.
        smtplib.SMTPServerDisconnected: The connection failed before the
            whole message was sent. The server never saw the end-of-data
            dot, so it cannot have accepted the message.
        CourierError: The whole message was sent but its reply never
            came: none within ``_DATA_REPLY_TIMEOUT``, or the connection
            failed first. The outcome is unknown; see ``send``.
    """
    code, reply = smtp.docmd("data")
    if code != 354:
        raise smtplib.SMTPDataError(code, reply)
    # RFC 5321 section 4.5.2: a line starting with "." gets another.
    payload = re.sub(rb"(?m)^\.", b"..", on_wire)
    if not payload.endswith(b"\r\n"):
        payload += b"\r\n"
    payload += b".\r\n"
    for start in range(0, len(payload), _DATA_CHUNK):
        smtp.send(payload[start : start + _DATA_CHUNK])

    sock = smtp.sock
    assert sock is not None  # smtp.send() raises once the socket is gone
    sock.settimeout(_DATA_REPLY_TIMEOUT)
    try:
        code, reply = smtp.getreply()
    except OSError as exc:
        # getreply() reports a read that timed out as SMTPServerDisconnected
        # (an OSError), with the TimeoutError as its context.
        if isinstance(exc, TimeoutError) or isinstance(exc.__context__, TimeoutError):
            why = f"no reply came within {_DATA_REPLY_TIMEOUT} seconds"
        else:
            why = f"the connection failed before the reply came ({exc})"
        named = f" (Message-ID {msgid})" if msgid else ""
        raise CourierError(
            f"send outcome unknown: the whole message was sent to {host}, but "
            f"{why}, so the server may have accepted it. Check the Sent folder "
            f"before re-sending{named}: courier filed no copy there, but a "
            "server that files its own sent mail, as Gmail does, holds one if "
            "it accepted the message."
        ) from exc
    sock.settimeout(_SMTP_TIMEOUT)
    return code, reply


def send(
    mime_msg: Any,
    smtp_cfg: SmtpConfig,
    transport: Optional[Callable[..., smtplib.SMTP]] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """Transmit *mime_msg* via SMTP and capture the post-DATA server response.

    Args:
        mime_msg: Built MIME message (``EmailMessage`` or ``MIMEMultipart``).
            Must already have From, at least one of To/Cc/Bcc, and Message-ID.
        smtp_cfg: Resolved SmtpConfig with credentials filled in (the caller
            is expected to use ``identity.resolve_smtp_for_identity`` first).
        transport: Optional ``smtplib.SMTP``-shaped factory, passed to
            :func:`open_session`. Tests inject a fake here.

    Returns:
        ``(fcc_bytes, result)`` where ``result`` is a dict with the keys
        documented at the top of this module (``refused_recipients``
        carries any RCPT TO rejections; the message was still delivered
        to the accepted subset). ``fcc_bytes`` has Bcc stripped, CRLF
        line endings, and Message-ID rewritten if the SMTP block
        requested it; pass to ``ImapClient.append_raw`` for the FCC step.

    Raises:
        ValueError: On a malformed MIME message (no From address, no
            recipients).
        PermanentError: The block has credentials but the session has no
            TLS and ``insecure_plaintext`` is not set; see
            :func:`open_session`. Nothing was transmitted.
        smtplib.SMTPSenderRefused: The server answered MAIL FROM with a
            non-2xx reply, carried with its code and text. Raised before
            RCPT TO and DATA, so nothing was transmitted.
        smtplib.SMTPRecipientsRefused: Every recipient was refused at
            RCPT TO. Raised before DATA, so nothing was transmitted.
        smtplib.SMTPNotSupportedError: The message carries 8-bit content
            and the server does not list 8BITMIME (RFC 6152). Raised
            before MAIL FROM, so nothing was transmitted.
        smtplib.SMTPDataError: The server refused DATA, or answered the
            message with a non-2xx reply.
        CourierError: The whole message was sent but its reply never
            came: none within ``_DATA_REPLY_TIMEOUT``, or the connection
            failed first. The outcome is unknown: the server may have
            accepted the message, so the message says to check the Sent
            folder before re-sending. A plain ``CourierError``, neither
            ``TransientError`` (a retry could deliver a duplicate) nor
            ``PermanentError`` (the send may have succeeded).
        smtplib.SMTPException: On any other SMTP-layer failure.
    """
    from_addr = _from_address(mime_msg)
    if not from_addr:
        from_header = mime_msg.get("From")
        if not from_header:
            raise ValueError("MIME message has no From header")
        raise ValueError(
            f"From header {str(from_header)!r} holds no single parseable address"
        )
    rcpts = _all_recipients(mime_msg)
    if not rcpts:
        raise ValueError("MIME message has no To/Cc/Bcc recipients")
    msgid = str(mime_msg.get("Message-ID", "") or "")

    raw = (
        mime_msg.as_bytes()
        if hasattr(mime_msg, "as_bytes")
        else mime_msg.as_string().encode("utf-8")
    )
    on_wire = _to_crlf(_strip_bcc(raw))

    smtp = open_session(smtp_cfg, transport)
    try:
        mail_options: List[str] = []
        if not on_wire.isascii():
            if not smtp.has_extn("8bitmime"):
                raise smtplib.SMTPNotSupportedError(
                    f"{smtp_cfg.host} does not list 8BITMIME, and the message "
                    "carries 8-bit content"
                )
            mail_options.append("BODY=8BITMIME")
        # mail() returns the reply rather than raising it; a refusal
        # unchecked here would surface as RCPT's "503 need MAIL" instead.
        code, reply = smtp.mail(from_addr, mail_options)
        if not (200 <= code < 300):
            raise smtplib.SMTPSenderRefused(code, reply, from_addr)
        accepted: List[str] = []
        refused: List[Dict[str, Any]] = []
        for rcpt in rcpts:
            code, reply = smtp.rcpt(rcpt)
            if 200 <= code < 300:
                accepted.append(rcpt)
            else:
                refused.append(
                    {
                        "addr": rcpt,
                        "code": code,
                        "reply": (
                            reply.decode("utf-8", errors="replace")
                            if isinstance(reply, bytes)
                            else str(reply)
                        ),
                    }
                )
        if not accepted:
            # Every recipient was refused; DATA would deliver to nobody
            # (and many servers answer it with a non-354 anyway). Fail
            # loudly with the per-address verdicts instead.
            raise smtplib.SMTPRecipientsRefused(
                {r["addr"]: (r["code"], r["reply"]) for r in refused}
            )
        code, response = _transmit_data(smtp, on_wire, smtp_cfg.host, msgid)
        if not (200 <= code < 300):
            raise smtplib.SMTPDataError(code, response)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    fcc_bytes = on_wire
    msgid_sent = msgid
    if smtp_cfg.rewrite_msgid_from_response:
        token = parse_ses_token(response)
        if token:
            msgid_sent = f"<{token}@email.amazonses.com>"
            # rewrite_message_id re-serialises with bare LFs; restore
            # CRLF so the FCC copy stays wire-clean.
            fcc_bytes = _to_crlf(rewrite_message_id(on_wire, msgid_sent))

    if isinstance(response, bytes):
        response_str = response.decode("utf-8", errors="replace")
    else:
        response_str = str(response)

    return fcc_bytes, {
        "message_id_local": msgid,
        "message_id_sent": msgid_sent,
        "smtp_response": response_str,
        "accepted_recipients": accepted,
        "refused_recipients": refused,
    }
