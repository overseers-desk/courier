"""Data models for email handling."""

import base64
import email
import email.utils
import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from email.message import Message
from typing import Any, Callable, Dict, List, Optional

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


def decode_mime_header(header_value: Optional[str]) -> str:
    """Decode a MIME header value.

    Args:
        header_value: MIME header value

    Returns:
        Decoded header value
    """
    if not header_value:
        return ""

    decoded_parts = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            if encoding:
                try:
                    decoded_parts.append(part.decode(encoding))
                except LookupError:
                    # If the encoding is not recognized, try with utf-8
                    decoded_parts.append(part.decode("utf-8", errors="replace"))
            else:
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


@dataclass
class EmailAddress:
    """Email address representation."""

    name: str
    address: str

    @classmethod
    def parse(cls, address_str: str) -> "EmailAddress":
        """Parse email address string.

        Args:
            address_str: Email address string (e.g., "John Doe <john@example.com>")

        Returns:
            EmailAddress object
        """
        # For the special case of just an email address without brackets
        if "@" in address_str and "<" not in address_str:
            return cls(name="", address=address_str.strip())

        # Extract name and address with angle brackets
        match = re.match(r'"?([^"<]*)"?\s*<([^>]*)>', address_str.strip())
        if match:
            name, address = match.groups()
            return cls(name=name.strip(), address=address.strip())

        # Fallback: treat the whole string as an address
        return cls(name="", address=address_str.strip())

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

        Args:
            part: Email message part

        Returns:
            EmailAttachment object
        """
        filename = part.get_filename()
        if not filename:
            # Generate a filename based on content type
            ext = part.get_content_type().split("/")[-1]
            filename = f"attachment.{ext}"

        raw_payload = part.get_payload(decode=True)
        content: Optional[bytes] = (
            raw_payload if isinstance(raw_payload, bytes) else None
        )
        content_type = part.get_content_type()

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
    date: Optional[datetime] = None
    content: EmailContent = field(default_factory=EmailContent)
    attachments: List[EmailAttachment] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    folder: Optional[str] = None
    uid: Optional[int] = None
    in_reply_to: Optional[str] = None
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
            date=self.date,
            content=EmailContent(text=placeholder_label, html=""),
            attachments=[],
            flags=list(self.flags),
            headers={},
            folder=self.folder,
            uid=self.uid,
            in_reply_to=self.in_reply_to,
            references=list(self.references),
            redacted_by=rule,
        )

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
        from_str = decode_mime_header(message.get("From", ""))
        to_str = decode_mime_header(message.get("To", ""))
        cc_str = decode_mime_header(message.get("Cc", ""))
        bcc_str = decode_mime_header(message.get("Bcc", ""))
        date_str = message.get("Date")
        message_id = message.get("Message-ID", "")
        if message_id:
            message_id = message_id.strip()

        # Get thread-related headers
        in_reply_to = message.get("In-Reply-To", "")
        if in_reply_to:
            in_reply_to = in_reply_to.strip()

        references_str = message.get("References", "")
        references = []
        if references_str:
            # Extract all message IDs from References header
            references = [ref.strip() for ref in re.findall(r"<[^>]+>", references_str)]

        # Parse addresses
        from_ = EmailAddress.parse(from_str)
        to = [
            EmailAddress.parse(addr.strip())
            for addr in to_str.split(",")
            if addr.strip()
        ]
        cc = [
            EmailAddress.parse(addr.strip())
            for addr in cc_str.split(",")
            if addr.strip()
        ]
        bcc = [
            EmailAddress.parse(addr.strip())
            for addr in bcc_str.split(",")
            if addr.strip()
        ]

        # Parse date
        date = None
        if date_str:
            try:
                date = email.utils.parsedate_to_datetime(date_str)
            except (ValueError, TypeError):
                pass

        # Build headers dictionary
        headers = {}
        for name, value in message.items():
            headers[name] = decode_mime_header(value)

        # Parse content and attachments
        content = EmailContent()
        attachments: List[EmailAttachment] = []

        # Process the email body
        if message.is_multipart():
            # Create a recursive function to handle nested multipart messages
            def process_part(
                part: Message,
                content: EmailContent,
                attachments: List[EmailAttachment],
            ) -> None:
                if part.is_multipart():
                    # Recursively process each subpart
                    for subpart in part.get_payload():
                        if isinstance(subpart, Message):
                            process_part(subpart, content, attachments)
                else:
                    content_type = part.get_content_type()
                    content_disposition = part.get("Content-Disposition", "")

                    # Handle attachments (both explicit and inline)
                    if (
                        "attachment" in content_disposition
                        or "inline" in content_disposition
                        or content_type.startswith("image/")
                        or content_type.startswith("application/")
                        or "name=" in part.get("Content-Type", "")
                    ):

                        attachments.append(EmailAttachment.from_part(part))
                    # Handle text content
                    elif content_type == "text/plain":
                        # Only replace existing text if it's empty
                        if not content.text:
                            try:
                                charset = part.get_content_charset() or "utf-8"
                                payload = part.get_payload(decode=True)
                                if isinstance(payload, bytes):
                                    content.text = payload.decode(
                                        charset, errors="replace"
                                    )
                            except Exception as e:
                                content.text = (
                                    f"[Error decoding plain text content: {e}]"
                                )
                    # Handle HTML content
                    elif content_type == "text/html":
                        # Only replace existing HTML if it's empty
                        if not content.html:
                            try:
                                charset = part.get_content_charset() or "utf-8"
                                payload = part.get_payload(decode=True)
                                if isinstance(payload, bytes):
                                    content.html = payload.decode(
                                        charset, errors="replace"
                                    )
                            except Exception as e:
                                content.html = f"[Error decoding HTML content: {e}]"

            # Start processing parts
            process_part(message, content, attachments)
        else:
            # Single part message
            content_type = message.get_content_type()

            if content_type == "text/plain":
                try:
                    charset = message.get_content_charset() or "utf-8"
                    payload = message.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        content.text = payload.decode(charset, errors="replace")
                except Exception as e:
                    content.text = f"[Error decoding plain text content: {e}]"
            elif content_type == "text/html":
                try:
                    charset = message.get_content_charset() or "utf-8"
                    payload = message.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        content.html = payload.decode(charset, errors="replace")
                except Exception as e:
                    content.html = f"[Error decoding HTML content: {e}]"
            else:
                # If not plain text or HTML, treat as attachment
                attachments.append(EmailAttachment.from_part(message))

        return cls(
            message_id=message_id,
            subject=subject,
            from_=from_,
            to=to,
            cc=cc,
            bcc=bcc,
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
                f"Reply to {self.in_reply_to}"
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

        cid_map = {}
        for att in self.attachments:
            if att.content_id and att.content:
                cid = att.content_id.strip("<>")
                cid_map[cid] = att

        if not cid_map:
            return html_str

        def replace_cid(match: re.Match[str]) -> str:
            quote = match.group(1)
            cid = match.group(2)
            if cid in cid_map:
                att = cid_map[cid]
                assert att.content is not None  # guarded by cid_map construction
                b64 = base64.b64encode(att.content).decode("ascii")
                data_uri = f"data:{att.content_type};base64,{b64}"
                return f"src={quote}{data_uri}{quote}"
            logger.warning(f"Inline image CID not found: {cid}")
            return str(match.group(0))

        return re.sub(r'src=(["\'])cid:([^"\']+)\1', replace_cid, html_str)

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
