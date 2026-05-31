"""SMTP client implementation for sending emails."""

import email.utils
import logging
import mimetypes
import os
from datetime import datetime
from email import encoders
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Union

from mailroom.markdown_render import needs_html, render_html
from mailroom.models import Email, EmailAddress

logger = logging.getLogger(__name__)


def _attach_file(container: MIMEMultipart, path: str) -> None:
    """Attach a file to a multipart/mixed container.

    Args:
        container: Outer ``multipart/mixed`` message to attach into.
        path: Filesystem path to the file.

    Raises:
        ValueError: If *path* is not a readable regular file.
    """
    if not os.path.isfile(path):
        raise ValueError(f"Attachment not found or not a regular file: {path}")

    ctype, encoding = mimetypes.guess_type(path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    with open(path, "rb") as fh:
        payload = fh.read()

    part = MIMEBase(maintype, subtype)
    part.set_payload(payload)
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename=("utf-8", "", os.path.basename(path)),
    )
    container.attach(part)


def _quoted_reply_text(body: str, original_email: Email) -> str:
    """Append a quoted copy of the original plain-text body to *body*."""
    if not original_email.content.text:
        return body
    quoted_original = "\n".join(
        f"> {line}" for line in original_email.content.text.split("\n")
    )
    attribution = (
        f"On {email.utils.format_datetime(original_email.date or datetime.now())}, "
        f"{original_email.from_} wrote:"
    )
    return f"{body}\n\n{attribution}\n{quoted_original}"


def _quoted_reply_html(html_body: str, original_email: Email) -> str:
    """Append a quoted copy of the original HTML (or escaped plain text) to *html_body*."""
    attribution = (
        f"On {email.utils.format_datetime(original_email.date or datetime.now())}, "
        f"{original_email.from_} wrote:"
    )
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
) -> Union[EmailMessage, MIMEMultipart]:
    """Create an RFC 822 MIME message.

    When *original_email* is None the function produces a fresh email with the
    given *to* / *subject* / *body*. When it is provided the function produces
    a reply: threading headers (``In-Reply-To``, ``References``) are added, the
    subject is prefixed with ``"Re: "`` if needed, and the original body is
    quoted under an attribution line. The primary recipient list follows
    RFC 5322 §3.6.2: the parent's ``Reply-To`` is used when present, otherwise
    the parent's ``From``. *reply_all* extends recipients to include the
    original To/Cc (minus the sender); when ``Reply-To`` diverts the primary
    list, the original ``From`` is added to Cc so the original sender still
    sees the reply-all.

    Args:
        from_addr: Address that appears in the ``From`` header.
        body: Plain-text body.
        to: Recipients (required when *original_email* is None).
        subject: Subject (required when *original_email* is None; optional for
            replies — defaults to the original subject with ``"Re: "`` prefix).
        cc: CC recipients. For replies with *reply_all*, defaults to the
            original email's Cc list.
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

    Returns:
        An ``EmailMessage`` (plain text, no attachments) or ``MIMEMultipart``
        (otherwise), ready to serialise with ``.as_bytes()``.

    Raises:
        ValueError: If *original_email* is None and *to* is missing/empty, or
            if any attachment path is not a readable regular file.
    """
    is_reply = original_email is not None
    if not is_reply and not to:
        raise ValueError("to is required when original_email is not provided")

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
                if recipient.address != from_addr.address
            )
        if to:
            to_recipients.extend(to)
    else:
        to_recipients = list(to or [])
    msg["To"] = ", ".join(str(recipient) for recipient in to_recipients)

    cc_recipients: List[EmailAddress] = []
    if cc:
        cc_recipients.extend(cc)
    elif is_reply and reply_all and original_email:
        if original_email.cc:
            cc_recipients.extend(
                recipient
                for recipient in original_email.cc
                if recipient.address != from_addr.address
            )
        # When Reply-To diverts the primary recipients, the original
        # sender is no longer in To. Include them in Cc so the original
        # sender still sees the reply-all.
        if original_email.reply_to:
            reply_to_addrs = {a.address for a in original_email.reply_to}
            if (
                original_email.from_.address not in reply_to_addrs
                and original_email.from_.address != from_addr.address
            ):
                cc_recipients.append(original_email.from_)
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
        references = []
        if "References" in original_email.headers:
            references.append(" ".join(original_email.headers["References"].split()))
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

    msg["Date"] = email.utils.formatdate(localtime=True)
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
) -> Dict[str, Any]:
    """Fetch an email, compose a reply, and save it as a draft.

    Args:
        client: An ``ImapClient`` instance (duck-typed to avoid circular import).
        folder: IMAP folder containing the original email.
        uid: UID of the original email.
        reply_body: Plain-text reply body.
        reply_all: Reply to all recipients.
        cc: Optional CC addresses as strings.
        bcc: Optional BCC addresses as strings.
        body_html: Optional HTML reply body.
        attachments: Optional list of filesystem paths to attach to the draft.

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
        )

        draft_uid = client.save_draft_mime(mime_message)
        if draft_uid:
            drafts_folder = client._get_drafts_folder()
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
        )

        draft_uid = client.save_draft_mime(mime_message)
        if draft_uid:
            drafts_folder = client._get_drafts_folder()
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
