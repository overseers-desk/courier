"""Data models for email handling."""

import base64
import codecs
import email
import email.utils
import html
import logging
import os
import quopri
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.header import Header, decode_header
from email.headerregistry import Address, HeaderRegistry
from email.message import Message
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote

logger = logging.getLogger(__name__)


def sanitize_and_save(content: bytes | str, save_path: str, mode: str = "wb") -> str:
    """Sanitise *save_path*, create parent directories, and write *content*.

    Args:
        content: Bytes or string to write.
        save_path: Destination path (path-traversal fragments are stripped).
        mode: File open mode (``"wb"`` for bytes, ``"w"`` for text).

    Returns:
        The sanitised path that was actually written to.
    """
    sanitized = save_path.replace("../", "").replace("..\\", "")
    parent = os.path.dirname(sanitized) or "."
    os.makedirs(parent, exist_ok=True)
    with open(sanitized, mode) as fh:
        fh.write(content)
    return sanitized


_FOLD = re.compile(r"\r?\n(?=[ \t])")

# The RFC 5322 header parser; its address headers split a list into
# mailboxes with the display names decoded.
_HEADERS = HeaderRegistry()

# An HTML src attribute holding a cid: URL (RFC 2392), quoted with
# either quote or bare, the scheme in any letter case.
_CID_SRC = re.compile(r"""src\s*=\s*(["']?)cid:([^"'\s>]+)\1""", re.IGNORECASE)

# Characters no filename may carry on some platform: path separators,
# the Windows reserved set, and control characters.
_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def _filename_stem(text: str, limit: int = 80) -> str:
    """Return *text* made safe as the stem of a filename.

    Args:
        text: Free text, such as a Subject.
        limit: The longest stem returned.

    Returns:
        The text with unsafe characters replaced by ``_``, runs of
        whitespace collapsed to one space, leading and trailing dots and
        spaces removed, and cut to *limit*; empty when nothing is left.
    """
    stem = _UNSAFE_IN_FILENAME.sub("_", text)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:limit].rstrip(" .")


# One msg-id of RFC 5322 section 3.6.4: the angle brackets and what they
# enclose. Comments, obsolete phrases and folding between identifiers
# fall outside every match.
_MSG_ID = re.compile(r"<[^>]+>")


def _text_codec(charset: Optional[str]) -> str:
    """Return the codec a text part's charset label is read with.

    Courier checks for UTF-8 and guesses no other encoding: a missing
    or unknown label, and ``us-ascii``, which UTF-8 contains, read as
    UTF-8. A label the host knows is read as itself, except
    ``iso-8859-1`` and its aliases, which read as ``windows-1252`` as the
    WHATWG Encoding Standard maps that label, since most such mail
    holds windows-1252 text and bytes 0x80-0x9F are then its quotes
    and dashes rather than control characters.

    Args:
        charset: The label from the Content-Type, or ``None``.

    Returns:
        A codec name :meth:`bytes.decode` accepts.
    """
    if not charset:
        return "utf-8"
    try:
        name = codecs.lookup(charset).name
    except LookupError:
        return "utf-8"
    if name in ("iso8859-1", "latin-1"):
        return "windows-1252"
    if name == "ascii":
        return "utf-8"
    return name


def _decode_text(part: Message) -> Optional[str]:
    """Return a text part's decoded content.

    Bytes that do not fit the codec :func:`_text_codec` picks become
    replacement marks, so a body is never replaced by an error.

    Args:
        part: A ``text/*`` leaf part.

    Returns:
        The text, or ``None`` when the part holds no bytes.
    """
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return None
    return payload.decode(_text_codec(part.get_content_charset()), errors="replace")


def message_ids(field_value: Optional[str]) -> List[str]:
    """Return each message identifier a Message-ID-style field holds.

    Message-ID, In-Reply-To and References carry ``<...>`` identifiers
    (RFC 5322 section 3.6.4), the last two possibly several, and any of
    them may carry comments and be folded. Each identifier keeps its
    angle brackets and is unfolded; everything outside the brackets is
    dropped.

    Args:
        field_value: The header field value as parsed, or ``None``.

    Returns:
        The identifiers in field order; empty when the field holds none.
    """
    if not field_value:
        return []
    return [_FOLD.sub("", found) for found in _MSG_ID.findall(field_value)]


def _raw_header(header_value: Union[str, Header, None]) -> str:
    """Return a header value as the one-line text the message carries.

    The parser hands a header holding bytes outside ASCII (raw UTF-8,
    RFC 6532) over as a :class:`Header` rather than a string. Its bytes
    are read as UTF-8, replacement marks standing for any that do not
    fit; encoded words inside it are left for :func:`decode_mime_header`.
    Folding is removed as it is for a string value.

    Args:
        header_value: The value as the parser returned it, or ``None``.

    Returns:
        The unfolded value with its raw bytes read as UTF-8.
    """
    if header_value is None:
        return ""
    if isinstance(header_value, Header):
        raw = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            for chunk, _ in decode_header(header_value)
        )
        header_value = raw.decode("utf-8", errors="replace")
    return _FOLD.sub("", header_value)


def decode_mime_header(header_value: Union[str, Header, None]) -> str:
    """Decode a MIME header value and unfold it onto one line.

    A long header arrives folded (RFC 5322 section 2.2.3): a line break
    followed by whitespace. The parser keeps the break in the value, so
    it is removed here, leaving the whitespace, before decoding. A raw
    UTF-8 header (RFC 6532), which the parser returns as a
    :class:`Header`, reads as its text. A broken encoded word never
    raises: a charset the host lacks is read as UTF-8, bytes that do
    not decode become replacement marks, and an encoded word that
    cannot be unpacked at all is left as written.

    Args:
        header_value: MIME header value

    Returns:
        Decoded header value, unfolded
    """
    unfolded = _raw_header(header_value)
    if not unfolded:
        return ""

    try:
        parts = decode_header(unfolded)
    except HeaderParseError:
        return unfolded

    decoded_parts = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or "utf-8"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


def _address_list(message: Message, name: str) -> List["EmailAddress"]:
    """Return every address an address field of *message* names.

    Each occurrence of the field is read by the RFC 5322 address-list
    parser of :mod:`email.headerregistry`, so a quoted display name
    keeps its commas, a group yields its members and an empty group
    none, a comment is left out (RFC 5322 section 3.4), and an encoded
    display name is decoded once the list is split. A field the message
    carries twice contributes the addresses of every occurrence. A
    value that parser cannot read at all falls back to
    :func:`email.utils.getaddresses`, which returns no address for a
    value it cannot parse, so only that value's addresses are lost.

    Args:
        message: The parsed message.
        name: The field name, such as ``"To"``.

    Returns:
        The addresses in field order; those with no addr-spec are left
        out, so an empty or unparsable field contributes nothing.
    """
    found: List[EmailAddress] = []
    for value in message.get_all(name, []):
        raw = _raw_header(value)
        try:
            addresses: Tuple[Address, ...] = getattr(
                _HEADERS(name, raw), "addresses", ()
            )
            pairs = [(a.display_name, a.addr_spec) for a in addresses]
        except Exception:
            pairs = [
                (decode_mime_header(display), address)
                for display, address in email.utils.getaddresses([raw])
            ]
        found.extend(
            EmailAddress(name=display, address=address)
            for display, address in pairs
            if address
        )
    return found


@dataclass
class EmailAddress:
    """Email address representation."""

    name: str
    address: str

    @classmethod
    def parse(cls, address_str: str) -> "EmailAddress":
        """Parse email address string.

        The string holds one address, as a command-line argument or a
        From field does: a quoted display name keeps its commas and an
        encoded one is decoded (RFC 5322 section 3.4). A string the
        parser cannot read is kept whole as the address, so the caller's
        own check can name it.

        Args:
            address_str: Email address string (e.g., "John Doe <john@example.com>")

        Returns:
            EmailAddress object
        """
        name, address = email.utils.parseaddr(_FOLD.sub("", address_str))
        if not address:
            return cls(name="", address=address_str.strip())
        return cls(name=decode_mime_header(name), address=address)

    def __str__(self) -> str:
        """Return RFC 5322-compliant address.

        Uses ``email.utils.formataddr`` so display names containing specials
        (commas, parens, dots, etc.) are quoted and non-ASCII names are
        MIME-encoded. Plain f-string formatting would emit
        ``Smith, John <x@y>`` which an RFC 5322 parser splits at the
        comma into two addresses; ``formataddr`` produces
        ``"Smith, John" <x@y>``.
        """
        if self.name:
            return email.utils.formataddr((self.name, self.address))
        return self.address


@dataclass
class EmailAttachment:
    """Email attachment representation."""

    filename: str
    content_type: str
    size: int
    content_id: Optional[str] = None
    content: Optional[bytes] = None

    @classmethod
    def from_part(cls, part: Message) -> "EmailAttachment":
        """Create attachment from email part.

        A ``message/rfc822`` part (a forwarded message, RFC 2046 section
        5.2.1) holds the encapsulated message as its content and, when
        the part names no file, is named from that message's Subject
        plus ``.eml``, or ``forwarded.eml`` when it has none.

        Args:
            part: Email message part

        Returns:
            EmailAttachment object
        """
        content_type = part.get_content_type()
        filename = part.get_filename()
        content: Optional[bytes]
        if content_type == "message/rfc822":
            payload = part.get_payload()
            inner = (
                payload[0]
                if isinstance(payload, list) and isinstance(payload[0], Message)
                else None
            )
            cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
            if inner is not None and cte in ("base64", "quoted-printable"):
                # RFC 2046 section 5.2.1 forbids encoding this type, but such
                # mail exists; the parser read the encoded text as a message.
                encoded = inner.as_bytes()
                decoded = (
                    base64.b64decode(encoded)
                    if cte == "base64"
                    else quopri.decodestring(encoded)
                )
                content = re.sub(rb"\r?\n", b"\r\n", decoded)
                inner = email.message_from_bytes(content)
            elif inner is None:
                raw_payload = part.get_payload(decode=True)
                content = raw_payload if isinstance(raw_payload, bytes) else None
            else:
                content = inner.as_bytes(policy=inner.policy.clone(linesep="\r\n"))
            if not filename:
                subject = decode_mime_header(inner.get("Subject")) if inner else ""
                filename = f"{_filename_stem(subject) or 'forwarded'}.eml"
        else:
            if not filename:
                # Generate a filename based on content type
                ext = content_type.split("/")[-1]
                filename = f"attachment.{ext}"
            raw_payload = part.get_payload(decode=True)
            content = raw_payload if isinstance(raw_payload, bytes) else None

        # Extract Content-ID properly, removing angle brackets if present
        content_id = part.get("Content-ID")
        if content_id:
            content_id = content_id.strip("<>")

        # If there's no Content-ID but there is a Content-Disposition with filename,
        # the attachment might be referenced in HTML via the filename
        if not content_id and filename:
            cdisp = part.get("Content-Disposition", "")
            if "inline" in cdisp and filename:
                # Some clients use the filename as a reference
                content_id = filename

        return cls(
            filename=decode_mime_header(filename),
            content_type=content_type,
            size=len(content) if content else 0,
            content_id=content_id,
            content=content,
        )


@dataclass
class EmailContent:
    """Email content representation."""

    text: Optional[str] = None
    html: Optional[str] = None

    def get_best_content(self) -> str:
        """Return the best available content."""
        if self.text:
            return self.text
        if self.html:
            # Convert HTML to plain text (simple approach)
            text = re.sub(r"<[^>]*>", "", self.html)
            return html.unescape(text)
        return ""


_REDACTED_PLACEHOLDER = "[redacted]"


@dataclass
class Email:
    """Email message representation."""

    message_id: str
    subject: str
    from_: EmailAddress
    to: List[EmailAddress]
    cc: List[EmailAddress] = field(default_factory=list)
    bcc: List[EmailAddress] = field(default_factory=list)
    reply_to: List[EmailAddress] = field(default_factory=list)
    date: Optional[datetime] = None
    content: EmailContent = field(default_factory=EmailContent)
    attachments: List[EmailAttachment] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    folder: Optional[str] = None
    uid: Optional[int] = None
    in_reply_to: List[str] = field(default_factory=list)
    references: List[str] = field(default_factory=list)
    # When set, this Email has had its content fields replaced with
    # placeholders by a per-block redaction policy. The value is a short
    # rule label suitable for showing the agent which rule fired. Sensitive
    # fields (subject/from_/to/cc/bcc/headers/content/attachments) carry
    # placeholders rather than the original content; uid/folder/date/flags
    # remain authentic so the agent retains a referent for the message.
    redacted_by: Optional[str] = None

    def redact(self, rule: str) -> "Email":
        """Return a redacted copy of this email.

        Replaces every content-bearing field with a placeholder and tags
        the copy with the rule label that fired. Preserves uid, folder,
        date, flags, message_id, and threading headers so the agent can
        still reason about timing and referenced threads, but cannot read
        any party or any content.

        Args:
            rule: Short label for the rule that fired (e.g. ``"redacted"``
                or a future per-rule name). Surfaced via ``redacted_by``
                and embedded in the placeholder body so a casual reader
                of the body sees why it was hidden.

        Returns:
            A new Email instance carrying only the non-sensitive
            referent fields plus placeholders elsewhere.
        """
        placeholder_label = f"[redacted by rule {rule}]"
        return Email(
            message_id=self.message_id,
            subject=placeholder_label,
            from_=EmailAddress(name="", address=_REDACTED_PLACEHOLDER),
            to=[],
            cc=[],
            bcc=[],
            reply_to=[],
            date=self.date,
            content=EmailContent(text=placeholder_label, html=""),
            attachments=[],
            flags=list(self.flags),
            headers={},
            folder=self.folder,
            uid=self.uid,
            in_reply_to=list(self.in_reply_to),
            references=list(self.references),
            redacted_by=rule,
        )

    def as_search_result(
        self,
        folder: str,
        flags: List[Any],
        date_iso: Optional[str],
        has_attachments: bool,
    ) -> Dict[str, Any]:
        """Project this Email into the shape consumed by ``search`` callers.

        The shape mirrors the IMAP search-result dict and is also
        emitted by the local-cache backend when redact applies.
        ``flags`` and ``date_iso`` are accepted rather than read off
        the Email because callers often hold them pre-formatted in
        their source vocabulary (IMAP RFC 3501 vs mu keyword flags;
        upstream ISO date string vs ``datetime``).

        Args:
            folder: Folder name to attribute to this hit.
            flags: Flag list, passed through verbatim.
            date_iso: Pre-formatted ISO 8601 date string, or ``None``.
            has_attachments: Whether the message carries attachments.

        Returns:
            Dict with the search-result fields plus ``redacted_by``
            when the Email is redacted.
        """
        result: Dict[str, Any] = {
            "uid": self.uid,
            "folder": folder,
            "from": str(self.from_),
            "to": [str(t) for t in self.to],
            "subject": self.subject,
            "date": date_iso,
            "flags": flags,
            "has_attachments": has_attachments,
            "message_id": self.message_id,
        }
        if self.redacted_by is not None:
            result["redacted_by"] = self.redacted_by
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Render this Email as the object the ``read`` command prints for it.

        ``body`` is the HTML part when there is one, else the text part,
        and ``content_type`` names which; ``date`` is in the host's local
        zone. ``in_reply_to``, ``references``, ``cc`` and ``attachments``
        appear only when non-empty. The raw header map is left out, since
        every AI-agent read of the object would then carry the Received,
        DKIM and ARC chains; callers that want it read :attr:`headers`.

        Returns:
            The per-block dict of ``read`` and of the chain's ``read`` verb.
        """
        result: Dict[str, Any] = {
            "uid": self.uid,
            "folder": self.folder,
            "from": str(self.from_),
            "to": [str(to) for to in self.to],
            "subject": self.subject,
            "date": (self.date.astimezone().isoformat() if self.date else None),
            "flags": self.flags,
            "message_id": self.message_id,
            "content_type": ("text/html" if self.content.html else "text/plain"),
            "body": (
                str(self.content.html)
                if self.content.html
                else str(self.content.text) if self.content.text else None
            ),
        }
        if self.in_reply_to:
            result["in_reply_to"] = list(self.in_reply_to)
        if self.references:
            result["references"] = list(self.references)
        if self.cc:
            result["cc"] = [str(cc) for cc in self.cc]
        if self.attachments:
            result["attachments"] = [
                {
                    "index": i,
                    "filename": att.filename,
                    "size": att.size,
                    "content_type": att.content_type,
                }
                for i, att in enumerate(self.attachments)
            ]
        return result

    @classmethod
    def from_message(
        cls, message: Message, uid: Optional[int] = None, folder: Optional[str] = None
    ) -> "Email":
        """Create email from email.message.Message.

        Args:
            message: Email message
            uid: IMAP UID
            folder: IMAP folder

        Returns:
            Email object
        """
        # Parse headers
        subject = decode_mime_header(message.get("Subject", ""))
        date_str = _raw_header(message.get("Date"))
        raw_message_id = message.get("Message-ID", "")
        found_ids = message_ids(raw_message_id)
        # A Message-ID without angle brackets is malformed but still the
        # message's only identifier, so it is kept, unfolded.
        message_id = (
            found_ids[0] if found_ids else _FOLD.sub("", raw_message_id).strip()
        )
        in_reply_to = message_ids(message.get("In-Reply-To"))
        references = message_ids(message.get("References"))

        # Parse addresses
        senders = _address_list(message, "From")
        from_ = senders[0] if senders else EmailAddress(name="", address="")
        to = _address_list(message, "To")
        cc = _address_list(message, "Cc")
        bcc = _address_list(message, "Bcc")
        reply_to = _address_list(message, "Reply-To")

        # Parse date. A zone of -0000 or none at all names UTC (RFC 5322
        # sections 3.3 and 4.3); the parser returns such a time naive.
        date = None
        if date_str:
            try:
                date = email.utils.parsedate_to_datetime(date_str)
            except (ValueError, TypeError):
                pass
            else:
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)

        # Build headers dictionary
        headers = {}
        for name, value in message.items():
            headers[name] = decode_mime_header(value)

        # Parse content and attachments
        content = EmailContent()
        attachments: List[EmailAttachment] = []

        def process_part(part: Message) -> None:
            """Place one part as body or attachment, walking into multiparts.

            A ``message/rfc822`` part is one attachment, not walked
            into, so a forwarded message's text and files stay its own
            (listing them is issue #109). A ``text/plain`` or
            ``text/html`` part is the body, the first of each kind
            winning, unless its disposition is ``attachment`` or it
            carries a filename; ``inline`` alone marks a part for
            display (RFC 2183 section 2.1). Every other leaf is an
            attachment when it is marked as one, named, neither text
            nor multipart, or the whole single-part message, so a bare
            ``text/calendar`` message is still listed.
            """
            content_type = part.get_content_type()
            if content_type == "message/rfc822":
                attachments.append(EmailAttachment.from_part(part))
                return
            if part.is_multipart():
                for subpart in part.get_payload():
                    if isinstance(subpart, Message):
                        process_part(subpart)
                return
            disposition = part.get_content_disposition()
            named = part.get_filename() is not None
            if content_type in ("text/plain", "text/html") and not (
                disposition == "attachment" or named
            ):
                if content_type == "text/plain":
                    if not content.text:
                        content.text = _decode_text(part)
                elif not content.html:
                    content.html = _decode_text(part)
            elif (
                disposition in ("attachment", "inline")
                or named
                or not content_type.startswith("text/")
                or part is message
            ):
                attachments.append(EmailAttachment.from_part(part))

        process_part(message)

        return cls(
            message_id=message_id,
            subject=subject,
            from_=from_,
            to=to,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            date=date,
            content=content,
            attachments=attachments,
            headers=headers,
            folder=folder,
            uid=uid,
            in_reply_to=in_reply_to,
            references=references,
        )

    def summary(self) -> str:
        """Return a summary of the email."""
        date_str = f"{self.date:%Y-%m-%d %H:%M:%S}" if self.date else "Unknown date"
        thread_info = ""
        if self.in_reply_to or self.references:
            thread_info = "\nThread: " + (
                f"Reply to {' '.join(self.in_reply_to)}"
                if self.in_reply_to
                else f"References {len(self.references)} previous messages"
            )

        return (
            f"From: {self.from_}\n"
            f"To: {', '.join(str(a) for a in self.to)}\n"
            f"Date: {date_str}\n"
            f"Subject: {self.subject}\n"
            f"Attachments: {len(self.attachments)}"
            f"{thread_info}"
        )

    def html_with_embedded_images(self) -> str:
        """Return HTML content with cid: references replaced by base64 data URIs.

        Returns:
            HTML string with inline images embedded, or empty string if no HTML.
        """
        html_str = self.content.html
        if not html_str:
            return ""
        if not self.attachments:
            return html_str

        # A cid: URL is the Content-ID percent-encoded (RFC 2392), and
        # senders write the same ID in differing letter case, so both
        # sides are matched decoded and in lower case.
        cid_map = {}
        for att in self.attachments:
            if att.content_id and att.content:
                cid = att.content_id.strip("<>")
                cid_map[cid.lower()] = att

        if not cid_map:
            return html_str

        def replace_cid(match: re.Match[str]) -> str:
            quote = match.group(1)
            cid = unquote(match.group(2)).lower()
            if cid in cid_map:
                att = cid_map[cid]
                assert att.content is not None  # guarded by cid_map construction
                b64 = base64.b64encode(att.content).decode("ascii")
                data_uri = f"data:{att.content_type};base64,{b64}"
                return f"src={quote}{data_uri}{quote}"
            logger.warning(f"Inline image CID not found: {cid}")
            return str(match.group(0))

        return _CID_SRC.sub(replace_cid, html_str)

    def attachment_summaries(self) -> List[Dict[str, Any]]:
        """Return metadata for each attachment.

        Returns:
            List of dicts with keys: index, filename, size, content_type,
            and optionally content_id.
        """
        summaries = []
        for index, att in enumerate(self.attachments):
            info: Dict[str, Any] = {
                "index": index,
                "filename": att.filename,
                "size": att.size,
                "content_type": att.content_type,
            }
            if att.content_id:
                info["content_id"] = att.content_id
            summaries.append(info)
        return summaries

    def save_attachment(self, attachment: str, save_path: str) -> Dict[str, Any]:
        """Find an attachment by name or index, validate it, and save to disk.

        Args:
            attachment: Attachment filename or index (as string).
            save_path: Destination path (path-traversal fragments are stripped).

        Returns:
            Dict with ``filename``, ``size``, and ``saved`` (sanitised path).

        Raises:
            ValueError: If no attachments, attachment not found, or no content.
        """
        if not self.attachments:
            raise ValueError("Email has no attachments")
        att = self.find_attachment(attachment)
        if att is None:
            raise ValueError(
                f"Attachment '{attachment}' not found. "
                f"Use filename or numeric index (0-{len(self.attachments) - 1})."
            )
        if att.content is None:
            raise ValueError(f"Attachment '{att.filename}' has no content")
        saved = sanitize_and_save(att.content, save_path, mode="wb")
        return {
            "filename": att.filename,
            "size": att.size,
            "saved": saved,
        }

    def export_html_to_file(self, save_path: str) -> Dict[str, Any]:
        """Export HTML with embedded images to a file.

        Args:
            save_path: Destination path (path-traversal fragments are stripped).

        Returns:
            Dict with ``saved`` (sanitised path) and ``size`` (bytes written).

        Raises:
            ValueError: If the email has no HTML content.
        """
        if not self.content.html:
            raise ValueError("Email has no HTML content")
        html_content = self.html_with_embedded_images()
        saved = sanitize_and_save(html_content, save_path, mode="w")
        return {"saved": saved, "size": os.path.getsize(saved)}

    def find_attachment(self, attachment: str) -> Optional["EmailAttachment"]:
        """Find an attachment by filename or numeric index.

        Args:
            attachment: Attachment filename or index (as string).

        Returns:
            The matching EmailAttachment, or None if not found.
        """
        for att in self.attachments:
            if att.filename == attachment:
                return att
        try:
            index = int(attachment)
            if 0 <= index < len(self.attachments):
                return self.attachments[index]
        except ValueError:
            pass
        return None

    def extract_links(self) -> List[Dict[str, Any]]:
        """Extract deduplicated links from the email's HTML content.

        Returns:
            List of dicts with keys ``url``, ``anchor``, and ``position``.
        """
        html_str = self.content.html
        if not html_str:
            return []

        link_pattern = re.compile(
            r'<a\s+[^>]*?href=(["\'])([^"\']+)\1[^>]*?>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )

        links: List[Dict[str, Any]] = []
        seen_urls: set = set()
        position = 1

        for match in link_pattern.finditer(html_str):
            url = match.group(2)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            anchor_html = match.group(3)
            anchor_text = re.sub(r"<[^>]+>", "", anchor_html, flags=re.DOTALL)
            anchor_text = html.unescape(anchor_text)
            anchor_text = re.sub(r"\s+", " ", anchor_text).strip()

            links.append({"url": url, "anchor": anchor_text, "position": position})
            position += 1

        return links


def extract_links_batch(
    fetch_fn: Callable[[int, str], Optional["Email"]],
    folder: str,
    uids: List[int],
) -> List[Dict[str, Any]]:
    """Extract links from multiple emails, collecting per-UID errors.

    Args:
        fetch_fn: Callable ``(uid, folder) -> Optional[Email]`` (typically
            ``ImapClient.fetch_email``).
        folder: Folder name.
        uids: Email UIDs to process.

    Returns:
        List of dicts, one per UID, each with ``uid``, ``links``, and
        optionally ``error``.
    """
    results: List[Dict[str, Any]] = []
    for uid in uids:
        try:
            email_obj = fetch_fn(uid, folder)
            if not email_obj:
                results.append(
                    {
                        "uid": uid,
                        "error": f"Email with UID {uid} not found in folder {folder}",
                        "links": [],
                    }
                )
                continue
            if not email_obj.content.html:
                results.append(
                    {"uid": uid, "error": "Email has no HTML content", "links": []}
                )
                continue
            links = email_obj.extract_links()
            results.append({"uid": uid, "links": links})
            logger.info(
                f"Extracted {len(links)} unique links from email UID {uid} in folder {folder}"
            )
        except Exception as e:
            logger.error(f"Error extracting links from UID {uid}: {e}")
            results.append({"uid": uid, "error": str(e), "links": []})
    return results
