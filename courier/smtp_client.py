"""SMTP client implementation for sending emails."""

import email.utils
import html
import logging
import mimetypes
import os
import sys
from datetime import datetime
from email import encoders
from email.charset import QP, Charset
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Union

from courier.errors import CourierError
from courier.markdown_render import needs_html, render_html
from courier.models import Email, EmailAddress

logger = logging.getLogger(__name__)


# Text formats outside text/* whose line endings are only whitespace, so the
# wire's CRLF conversion leaves them intact when sent unencoded.
_LINE_SAFE_APPLICATION_TYPES = frozenset(
    {
        "application/postscript",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/toml",
        "application/rtf",
        "image/svg+xml",
    }
)

# RFC 5322 §2.1.1, counted in bytes, without the CRLF.
_MAX_LINE_OCTETS = 998


def _first_unsafe_line(payload: bytes, max_octets: int = _MAX_LINE_OCTETS) -> int:
    """Find the first line that bars sending *payload* as 7bit or 8bit.

    A line fails on a NUL byte, on a CR outside a CRLF pair (the wire's
    CRLF conversion would turn it into a line break), or on running past
    *max_octets* bytes.

    Args:
        payload: Raw file content.
        max_octets: Longest line allowed, in bytes.

    Returns:
        The 1-based number of the first failing line, or 0 when every
        line is safe.
    """
    for number, line in enumerate(payload.replace(b"\r\n", b"\n").split(b"\n"), 1):
        if b"\0" in line or b"\r" in line or len(line) > max_octets:
            return number
    return 0


def _is_utf8(payload: bytes) -> bool:
    """Return whether *payload* decodes as UTF-8."""
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _set_unencoded_payload(part: MIMEBase, payload: bytes) -> None:
    """Carry *payload* in *part* byte for byte as 7bit or 8bit.

    Args:
        part: A non-``message/*`` part with no payload yet.
        payload: Line-safe file content; ``charset=utf-8`` is declared on
            an 8bit ``text/*`` part.
    """
    cte = "7bit" if payload.isascii() else "8bit"
    if part.get_content_maintype() == "text" and cte == "8bit":
        part.set_param("charset", "utf-8")
    # The generator writes a surrogate-escaped str back out as the original
    # bytes, and the wire's CRLF pass restores the line endings.
    part.set_payload(payload.replace(b"\r\n", b"\n").decode("ascii", "surrogateescape"))
    part["Content-Transfer-Encoding"] = cte


def _attach_file(container: MIMEMultipart, path: str) -> None:
    """Attach a file to a multipart/mixed container.

    An attached email (``message/*``) goes out unencoded, since RFC 2046
    §5.2.1 forbids base64 on it. Text formats go out unencoded when they
    are line-safe; a UTF-8 ``text/*`` file whose only fault is line length
    goes out quoted-printable. Everything else is base64.

    Args:
        container: Outer ``multipart/mixed`` message to attach into.
        path: Filesystem path to the file.

    Raises:
        ValueError: If *path* is not a readable regular file, or is an
            attached email that cannot go out unencoded.
    """
    if not os.path.isfile(path):
        raise ValueError(f"Attachment not found or not a regular file: {path}")

    ctype, encoding = mimetypes.guess_type(path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    filename = os.path.basename(path)

    with open(path, "rb") as fh:
        payload = fh.read()

    unsafe_line = _first_unsafe_line(payload)
    utf8_text = maintype == "text" and _is_utf8(payload)
    part = MIMEBase(maintype, subtype)
    if maintype == "message":
        if unsafe_line:
            raise ValueError(
                f"Attachment {filename} cannot be sent as {ctype}: line "
                f"{unsafe_line} has a NUL byte, a stray CR or more than "
                f"{_MAX_LINE_OCTETS} bytes, and RFC 2046 forbids encoding "
                "this type"
            )
        # A str payload on a message/* part is written as strict ASCII, so
        # the file goes in parsed.
        part = MIMEMessage(email.message_from_bytes(payload), subtype)
        part["Content-Transfer-Encoding"] = "7bit" if payload.isascii() else "8bit"
    elif not unsafe_line and (utf8_text or ctype in _LINE_SAFE_APPLICATION_TYPES):
        _set_unencoded_payload(part, payload)
    elif utf8_text and not _first_unsafe_line(payload, max_octets=sys.maxsize):
        charset = Charset("utf-8")
        charset.body_encoding = QP
        part.set_payload(payload.decode("utf-8"), charset)
    else:
        part.set_payload(payload)
        encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename=("utf-8", "", filename),
    )
    container.attach(part)


def _attribution(original_email: Email) -> str:
    """Write the line that introduces the quoted original, as plain text.

    The sender appears as a reader sees it: the decoded display name and
    the address in angle brackets, or the bare address when there is no
    name. ``str(EmailAddress)`` is the header form, which RFC 2047-encodes
    a non-ASCII name, and encoded words do not belong in body text.

    Args:
        original_email: The email being replied to.

    Returns:
        ``On <date>, <name> <address> wrote:``.
    """
    sender = original_email.from_
    who = f"{sender.name} <{sender.address}>" if sender.name else sender.address
    when = email.utils.format_datetime(original_email.date or datetime.now())
    return f"On {when}, {who} wrote:"


def _quoted_reply_text(body: str, original_email: Email) -> str:
    """Append a quoted copy of the original plain-text body to *body*."""
    if not original_email.content.text:
        return body
    quoted_original = "\n".join(
        f"> {line}" for line in original_email.content.text.split("\n")
    )
    return f"{body}\n\n{_attribution(original_email)}\n{quoted_original}"


def _quoted_reply_html(html_body: str, original_email: Email) -> str:
    """Append a quoted copy of the original HTML (or escaped plain text) to *html_body*."""
    attribution = html.escape(_attribution(original_email))
    if original_email.content.html:
        quoted_block = original_email.content.html
    else:
        original_text = original_email.content.get_best_content()
        if not original_text:
            return html_body
        quoted_block = (
            original_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
    return (
        f"{html_body}\n"
        f'<div style="border-top: 1px solid #ccc; margin-top: 20px; padding-top: 10px;">\n'
        f"<p>{attribution}</p>\n"
        f'<blockquote style="margin: 0 0 0 .8ex; border-left: 1px solid #ccc; padding-left: 1ex;">\n'
        f"{quoted_block}\n"
        f"</blockquote>\n"
        f"</div>"
    )


def parse_message_date(value: str) -> datetime:
    """Parse an ISO 8601 timestamp for the ``Date`` header.

    Args:
        value: An ISO 8601 date or timestamp, with or without a UTC
            offset (e.g. ``2019-03-04``, ``2019-03-04T05:06:07``,
            ``2019-03-04T05:06:07+10:00``).

    Returns:
        The parsed datetime, naive when *value* carried no offset. A
        naive value is read as local time where the header is written.

    Raises:
        ValueError: When *value* is not ISO 8601.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(
            f"{value!r} is not an ISO 8601 timestamp (expected e.g. "
            f"2019-03-04, 2019-03-04T05:06:07, or "
            f"2019-03-04T05:06:07+10:00): {e}"
        ) from e


def _same_address(a: EmailAddress, b: EmailAddress) -> bool:
    """Compare two addresses as mail systems do, ignoring letter case.

    Args:
        a: First address.
        b: Second address.

    Returns:
        True when the two addr-specs match case-insensitively.
    """
    return a.address.lower() == b.address.lower()


def create_mime(
    from_addr: EmailAddress,
    body: str,
    to: Optional[List[EmailAddress]] = None,
    subject: Optional[str] = None,
    cc: Optional[List[EmailAddress]] = None,
    bcc: Optional[List[EmailAddress]] = None,
    html_body: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    original_email: Optional[Email] = None,
    reply_all: bool = True,
    date: Optional[datetime] = None,
) -> Union[EmailMessage, MIMEMultipart]:
    """Create an RFC 822 MIME message.

    Every message gets a new ``Message-ID`` (RFC 5322 §3.6.4), whatever is
    later done with it: sent, saved as a draft or written out.

    When *original_email* is None the function produces a fresh email with the
    given *to* / *subject* / *body*. When it is provided the function produces
    a reply: threading headers (``In-Reply-To``, ``References``) are added, the
    subject is prefixed with ``"Re: "`` if needed, and the original body is
    quoted under an attribution line. The primary recipient list follows
    RFC 5322 §3.6.2: the parent's ``Reply-To`` is used when present, otherwise
    the parent's ``From``. *reply_all* extends recipients to include the
    original To/Cc (minus the sender); when ``Reply-To`` diverts the primary
    list, the original ``From`` is added to Cc so the original sender still
    sees the reply-all. Explicit *to* and *cc* add to these lists rather
    than replace them; Cc is deduplicated, and addresses compare
    case-insensitively throughout.

    Args:
        from_addr: Address that appears in the ``From`` header.
        body: Plain-text body.
        to: Recipients. For a fresh message at least one of *to*, *cc*, or
            *bcc* must be present; *to* alone is the usual case, but a
            Bcc-only message (no visible recipient, e.g. a send to a
            distribution list) is permitted.
        subject: Subject (required when *original_email* is None; optional for
            replies — defaults to the original subject with ``"Re: "`` prefix).
        cc: CC recipients. For replies with *reply_all*, added after the
            original email's Cc list; otherwise the whole Cc list.
        bcc: BCC recipients. Written to the ``Bcc`` header; most sending agents
            strip it before transmission.
        html_body: Optional HTML body. Triggers a ``multipart/alternative``
            child inside ``multipart/mixed``. Three values are
            distinguished: ``None`` (default) auto-renders an HTML
            alternative if *body* contains a markdown table or heading,
            else sends text/plain only; ``""`` forces text/plain only
            even when triggers are present (caller has considered HTML
            and chose none); any other string is used verbatim and the
            auto-render path is not invoked.
        attachments: Optional list of filesystem paths to attach. Duplicate
            basenames are accepted but may confuse some clients on save.
        original_email: If given, produce a reply to this message.
        reply_all: Reply-only. Include original To/Cc in recipients.
        date: Instant written to the ``Date`` header. Defaults to the
            current local time. A naive value is read as local time. The
            header is the sender's claim about composition time; a
            receiving server records its own arrival time regardless, and
            a value far from the moment of submission is one of the
            signals spam filters weigh.

    Returns:
        An ``EmailMessage`` (plain text, no attachments) or ``MIMEMultipart``
        (otherwise), ready to serialise with ``.as_bytes()``.

    Raises:
        ValueError: If *original_email* is None and no recipient (to, cc, or
            bcc) is given, or if any attachment path is not a readable
            regular file.
    """
    is_reply = original_email is not None
    if not is_reply and not (to or cc or bcc):
        raise ValueError(
            "at least one recipient (to, cc, or bcc) is required when "
            "original_email is not provided"
        )

    if html_body is None and needs_html(body):
        html_body = render_html(body)

    multipart_mode = bool(html_body) or bool(attachments)
    msg: Union[EmailMessage, MIMEMultipart]
    if multipart_mode:
        msg = MIMEMultipart("mixed")
    else:
        msg = EmailMessage()

    msg["From"] = str(from_addr)

    if is_reply:
        assert original_email is not None  # for type checkers
        if original_email.reply_to:
            to_recipients = list(original_email.reply_to)
        else:
            to_recipients = [original_email.from_]
        if reply_all and original_email.to:
            to_recipients.extend(
                recipient
                for recipient in original_email.to
                if not _same_address(recipient, from_addr)
            )
        if to:
            to_recipients.extend(to)
    else:
        to_recipients = list(to or [])
    if to_recipients:
        msg["To"] = ", ".join(str(recipient) for recipient in to_recipients)

    cc_recipients: List[EmailAddress] = []
    if is_reply and reply_all and original_email:
        if original_email.cc:
            cc_recipients.extend(
                recipient
                for recipient in original_email.cc
                if not _same_address(recipient, from_addr)
            )
        # When Reply-To diverts the primary recipients, the original
        # sender is no longer in To. Include them in Cc so the original
        # sender still sees the reply-all.
        if original_email.reply_to:
            sender = original_email.from_
            if not any(
                _same_address(sender, a) for a in original_email.reply_to
            ) and not _same_address(sender, from_addr):
                cc_recipients.append(sender)
    if cc:
        cc_recipients.extend(cc)
    unique_cc: List[EmailAddress] = []
    for recipient in cc_recipients:
        if not any(_same_address(recipient, kept) for kept in unique_cc):
            unique_cc.append(recipient)
    cc_recipients = unique_cc
    if cc_recipients:
        msg["Cc"] = ", ".join(str(recipient) for recipient in cc_recipients)

    if bcc:
        msg["Bcc"] = ", ".join(str(recipient) for recipient in bcc)

    if subject:
        msg["Subject"] = subject
    elif is_reply:
        assert original_email is not None
        original_subject = " ".join(original_email.subject.split())
        if original_subject.startswith("Re:"):
            msg["Subject"] = original_subject
        else:
            msg["Subject"] = f"Re: {original_subject}"
    else:
        msg["Subject"] = ""

    if is_reply:
        assert original_email is not None
        references = list(original_email.references)
        if original_email.message_id:
            references.append(" ".join(original_email.message_id.split()))
        if references:
            msg["References"] = " ".join(references)
        if original_email.message_id:
            msg["In-Reply-To"] = " ".join(original_email.message_id.split())

    plain_text = (
        _quoted_reply_text(body, original_email)
        if is_reply and original_email
        else body
    )

    if html_body:
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(plain_text, "plain", "utf-8"))
        html_content = (
            _quoted_reply_html(html_body, original_email)
            if is_reply and original_email
            else html_body
        )
        alternative.attach(MIMEText(html_content, "html", "utf-8"))
        assert isinstance(msg, MIMEMultipart)
        msg.attach(alternative)
    elif multipart_mode:
        assert isinstance(msg, MIMEMultipart)
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    else:
        assert isinstance(msg, EmailMessage)
        msg.set_content(plain_text)

    if attachments:
        assert isinstance(msg, MIMEMultipart)
        for path in attachments:
            _attach_file(msg, path)

    if date is None:
        msg["Date"] = email.utils.formatdate(localtime=True)
    else:
        aware = date if date.tzinfo is not None else date.astimezone()
        msg["Date"] = email.utils.format_datetime(aware)
    msg["Message-ID"] = email.utils.make_msgid()
    return msg


def _find_reply_from_address(email_obj: Email, my_address: str) -> EmailAddress:
    """Find the best reply-from address by matching the account address.

    Searches the To and CC fields of *email_obj* for an address matching
    *my_address* (case-insensitive).  Falls back to the first To recipient
    or, if there are none, constructs an ``EmailAddress`` from *my_address*.
    """
    my_lower = my_address.lower()
    for recipient in (email_obj.to or []) + (email_obj.cc or []):
        if recipient.address and recipient.address.lower() == my_lower:
            return recipient
    if email_obj.to:
        return email_obj.to[0]
    return EmailAddress(name="", address=my_address)


def compose_and_save_reply_draft(
    client: Any,
    folder: str,
    uid: int,
    reply_body: str,
    reply_all: bool = True,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    body_html: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fetch an email, compose a reply, and save it as a draft.

    Args:
        client: An ``ImapClient`` instance (duck-typed to avoid circular import).
        folder: IMAP folder containing the original email.
        uid: UID of the original email.
        reply_body: Plain-text reply body.
        reply_all: Reply to all recipients.
        cc: Optional CC addresses as strings. With *reply_all*, added to
            the original Cc; otherwise the whole Cc list.
        bcc: Optional BCC addresses as strings.
        body_html: Optional HTML reply body.
        attachments: Optional list of filesystem paths to attach to the draft.
        date: Instant for the ``Date`` header. Defaults to the current
            local time; a naive value is read as local time.

    Returns:
        Dict with keys ``status``, ``message``, ``draft_uid``, ``draft_folder``.
    """
    result: Dict[str, Any] = {
        "status": "error",
        "message": "",
        "draft_uid": None,
        "draft_folder": None,
    }

    try:
        email_obj = client.fetch_email(uid, folder=folder)
        if not email_obj:
            result["message"] = f"Email with UID {uid} not found in folder {folder}"
            return result

        reply_from = _find_reply_from_address(email_obj, client.block.username)

        cc_addresses = None
        if cc:
            cc_addresses = [EmailAddress.parse(addr) for addr in cc]

        bcc_addresses = [EmailAddress.parse(addr) for addr in bcc] if bcc else None
        mime_message = create_mime(
            original_email=email_obj,
            from_addr=reply_from,
            body=reply_body,
            reply_all=reply_all,
            cc=cc_addresses,
            bcc=bcc_addresses,
            html_body=body_html,
            attachments=attachments,
            date=date,
        )

        # Compose callers have no exit-code channel; a failed draft save
        # degrades to uid None and the JSON result carries the outcome.
        try:
            draft_uid = client.save_draft_mime(mime_message).uid
        except CourierError:
            draft_uid = None
        if draft_uid:
            drafts_folder = client.resolve_drafts_folder()
            result["status"] = "success"
            result["message"] = "Draft reply saved"
            result["draft_uid"] = draft_uid
            result["draft_folder"] = drafts_folder
        else:
            result["message"] = "Failed to save draft"
    except Exception as e:
        logger.error(f"Error drafting reply: {e}")
        result["message"] = f"Error: {e}"

    return result


def compose_and_save_draft(
    client: Any,
    to: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    body_html: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compose a new email and save it as a draft.

    Unlike ``compose_and_save_reply_draft`` this does not thread off an
    existing message: no In-Reply-To/References headers, no quoted-original
    body. The From address is taken from ``client.block.username``.

    Args:
        client: An ``ImapClient`` instance (duck-typed to avoid circular import).
        to: Recipients as strings.
        subject: Subject line.
        body: Plain-text body.
        cc: Optional CC addresses as strings.
        bcc: Optional BCC addresses as strings.
        body_html: Optional HTML body.
        attachments: Optional list of filesystem paths to attach.
        date: Instant for the ``Date`` header. Defaults to the current
            local time; a naive value is read as local time.

    Returns:
        Dict with keys ``status``, ``message``, ``draft_uid``, ``draft_folder``.
    """
    result: Dict[str, Any] = {
        "status": "error",
        "message": "",
        "draft_uid": None,
        "draft_folder": None,
    }

    if not to:
        result["message"] = "At least one recipient is required"
        return result

    try:
        from_addr = EmailAddress(name="", address=client.block.username)
        to_addresses = [EmailAddress.parse(addr) for addr in to]
        cc_addresses = [EmailAddress.parse(addr) for addr in cc] if cc else None
        bcc_addresses = [EmailAddress.parse(addr) for addr in bcc] if bcc else None

        mime_message = create_mime(
            from_addr=from_addr,
            body=body,
            to=to_addresses,
            subject=subject,
            cc=cc_addresses,
            bcc=bcc_addresses,
            html_body=body_html,
            attachments=attachments,
            date=date,
        )

        # Compose callers have no exit-code channel; a failed draft save
        # degrades to uid None and the JSON result carries the outcome.
        try:
            draft_uid = client.save_draft_mime(mime_message).uid
        except CourierError:
            draft_uid = None
        if draft_uid:
            drafts_folder = client.resolve_drafts_folder()
            result["status"] = "success"
            result["message"] = "Draft saved"
            result["draft_uid"] = draft_uid
            result["draft_folder"] = drafts_folder
        else:
            result["message"] = "Failed to save draft"
    except Exception as e:
        logger.error(f"Error composing draft: {e}")
        result["message"] = f"Error: {e}"

    return result
