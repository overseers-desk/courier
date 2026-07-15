"""SMTP transport: hand-driven smtplib session that captures the post-DATA reply.

Why hand-driven instead of ``smtplib.SMTP.sendmail``: ``sendmail()`` discards
the server response after DATA. Some smarthosts (notably AWS SES) carry a
tracking token in that line (``250 Ok 0102019...``). Driving
``mail()`` / ``rcpt()`` / ``data()`` ourselves preserves the
``(code, response)`` tuple from ``data()``.

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
from email.utils import getaddresses
from typing import Any, Callable, Dict, List, Optional, Tuple

from courier.config import SmtpConfig

# Socket timeout for the SMTP session, in seconds. Matches the IMAP
# side's connect timeout (imap_client passes timeout=10) so a stalled
# server cannot hang a send forever; a stall after the server queued
# the message would otherwise invite the caller to kill and re-send.
_SMTP_TIMEOUT = 10

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
    raw = mime.get(header)
    if not raw:
        return []
    return [addr for _name, addr in getaddresses([str(raw)]) if addr]


def _from_address(mime: Any) -> str:
    """Return the bare address from the From header."""
    raw = str(mime.get("From", "") or "")
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].rsplit(">", 1)[0].strip()
    return raw.strip()


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
    return raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(
        b"\n", b"\r\n"
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


def _pick_default_transport(port: int) -> Callable[..., smtplib.SMTP]:
    """Choose smtplib.SMTP_SSL for port 465, smtplib.SMTP otherwise."""
    return smtplib.SMTP_SSL if port == 465 else smtplib.SMTP


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
        transport: Optional ``smtplib.SMTP``-shaped factory. Defaults to
            ``smtplib.SMTP_SSL`` for port 465 and ``smtplib.SMTP``
            otherwise. Tests inject a fake here.

    Returns:
        ``(fcc_bytes, result)`` where ``result`` is a dict with the keys
        documented at the top of this module (``refused_recipients``
        carries any RCPT TO rejections; the message was still delivered
        to the accepted subset). ``fcc_bytes`` has Bcc stripped, CRLF
        line endings, and Message-ID rewritten if the SMTP block
        requested it; pass to ``ImapClient.append_raw`` for the FCC step.

    Raises:
        ValueError: On a malformed MIME message (no From, no recipients).
        smtplib.SMTPRecipientsRefused: Every recipient was refused at
            RCPT TO. Raised before DATA, so nothing was transmitted.
        smtplib.SMTPException: On any other SMTP-layer failure.
    """
    factory = transport or _pick_default_transport(smtp_cfg.port)

    from_addr = _from_address(mime_msg)
    if not from_addr:
        raise ValueError("MIME message has no From header")
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

    smtp = factory(smtp_cfg.host, smtp_cfg.port, timeout=_SMTP_TIMEOUT)
    try:
        smtp.ehlo()
        if smtp_cfg.port in (587, 2587):
            smtp.starttls()
            smtp.ehlo()
        if smtp_cfg.username and smtp_cfg.password:
            smtp.login(smtp_cfg.username, smtp_cfg.password)
        smtp.mail(from_addr)
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
        code, response = smtp.data(on_wire)
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
