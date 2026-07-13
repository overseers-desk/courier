"""MCP tool registrations — thin wrappers only.

This module must contain **no domain logic**.  Each function's job is:
1. Obtain an ``ImapClient`` from the MCP context.
2. Delegate to a domain function (``imap_client``, ``models``, ``smtp_client``,
   or ``workflows``).
3. Return the result in an MCP-friendly format.

Any logic that could also be useful from the CLI or in tests belongs in one
of the domain modules listed above, not here.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import Context, FastMCP

from courier.errors import CourierError, FolderNotFound
from courier.imap_client import ImapClient
from courier.models import extract_links_batch
from courier.query_parser import render_operator_help
from courier.resources import get_client_from_context

logger = logging.getLogger(__name__)

# Explicit description for the search tool. FastMCP uses an explicit
# ``description`` over the function docstring, so the operator inventory
# derives from the parser (via render_operator_help) and cannot drift.
_SEARCH_TOOL_DESCRIPTION = (
    "Search for emails using Gmail-style query syntax.\n\n"
    + render_operator_help()
    + '\n\nReturns a JSON dict {"results": [...], "provenance": {...}} where '
    'provenance.source is "local" (served from a local mu cache) or "remote" '
    "(went to IMAP), provenance.indexed_at carries the local index mtime when "
    "applicable, and provenance.fell_back_reason names the condition that forced "
    "an IMAP fallback or is null."
)


def register_tools(mcp: FastMCP, imap_client: ImapClient) -> None:
    """Register MCP tools.

    Args:
        mcp: MCP server
        imap_client: IMAP client
    """

    @mcp.tool(name="compose")
    async def compose(
        to: List[str],
        body: str,
        ctx: Context,
        subject: str = "",
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        body_html: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        imap: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Composes a new email and saves it as a draft.

        Unlike ``reply``, this produces a fresh email with no threading
        headers and no quoted original. The From address is taken from
        the [imap.NAME] block's first identity.

        Args:
            to: List of recipient email addresses
            body: Plain-text body
            ctx: MCP context
            subject: Subject line (default: empty)
            cc: Optional CC recipients
            bcc: Optional BCC recipients
            body_html: Optional HTML version of the body. ``None``
                (default) auto-renders an HTML alternative when *body*
                contains a markdown table or heading; ``""`` forces
                text/plain only; any other string is used verbatim.
            attachments: Optional list of filesystem paths to attach. Paths
                are read by the MCP server process.
            imap: [imap.NAME] block name (None for default)

        Returns:
            Dictionary with status and the UID of the created draft
        """
        from courier.smtp_client import compose_and_save_draft

        client = get_client_from_context(ctx, imap)
        return compose_and_save_draft(
            client,
            to,
            subject,
            body,
            cc=cc,
            bcc=bcc,
            body_html=body_html,
            attachments=attachments,
        )

    @mcp.tool(name="reply")
    async def reply(
        folder: str,
        uid: int,
        reply_body: str,
        ctx: Context,
        reply_all: bool = True,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        body_html: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        imap: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates a draft reply to an email and saves it to the drafts folder.

        Args:
            folder: Email folder name
            uid: Email UID
            reply_body: Reply text content
            ctx: MCP context
            reply_all: Whether to reply to all recipients
            cc: Optional CC recipients
            bcc: Optional BCC recipients
            body_html: Optional HTML version of the reply. ``None``
                (default) auto-renders an HTML alternative when
                *reply_body* contains a markdown table or heading;
                ``""`` forces text/plain only; any other string is
                used verbatim.
            attachments: Optional list of filesystem paths to attach to the
                draft. Paths are read by the MCP server process.
            imap: [imap.NAME] block name (None for default)

        Returns:
            Dictionary with status and the UID of the created draft
        """
        from courier.smtp_client import compose_and_save_reply_draft

        client = get_client_from_context(ctx, imap)
        return compose_and_save_reply_draft(
            client,
            folder,
            uid,
            reply_body,
            reply_all=reply_all,
            cc=cc,
            bcc=bcc,
            body_html=body_html,
            attachments=attachments,
        )

    @mcp.tool(name="move")
    async def move(
        folder: str,
        uid: int,
        target_folder: str,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Move email to another folder.

        Args:
            folder: Source folder
            uid: Email UID
            target_folder: Target folder
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            client.move_email(uid, folder, target_folder)
            return f"Email moved from {folder} to {target_folder}"
        except CourierError:
            # MCP returns error strings, not exit codes; the text is the contract
            return "Failed to move email"
        except Exception as e:
            logger.error(f"Error moving email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="mark-read")
    async def mark_read(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Mark email as read.

        Args:
            folder: Folder name
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            client.mark_email(uid, folder, r"\Seen", True)
            return "Email marked as read"
        except CourierError:
            return "Failed to mark email as read"
        except Exception as e:
            logger.error(f"Error marking email as read: {e}")
            return f"Error: {e}"

    @mcp.tool(name="mark-unread")
    async def mark_unread(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Mark email as unread.

        Args:
            folder: Folder name
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            client.mark_email(uid, folder, r"\Seen", False)
            return "Email marked as unread"
        except CourierError:
            return "Failed to mark email as unread"
        except Exception as e:
            logger.error(f"Error marking email as unread: {e}")
            return f"Error: {e}"

    @mcp.tool(name="flag")
    async def flag(
        folder: str,
        uid: int,
        ctx: Context,
        flag: bool = True,
        imap: Optional[str] = None,
    ) -> str:
        """Flag or unflag email.

        Args:
            folder: Folder name
            uid: Email UID
            flag: True to flag, False to unflag
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        action = "flagged" if flag else "unflagged"
        try:
            client.mark_email(uid, folder, r"\Flagged", flag)
            return f"Email {action}"
        except CourierError:
            return f"Failed to {action.replace('ged','g').replace('ed','')} email"
        except Exception as e:
            logger.error(f"Error flagging email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="trash")
    async def trash(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Move an email to the server's Trash/Bin (recoverable removal).

        The normal way to remove a message. On Gmail a plain delete only
        removes the folder label, leaving the message in All Mail; trashing
        moves it to the Bin, which is what actually removes it.

        Args:
            folder: Folder containing the email
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            client.trash_email(uid, folder)
            return "Email trashed"
        except FolderNotFound as e:
            # No resolvable Trash/Bin: same "Error: ..." text as the old
            # ValueError path.
            logger.error(f"Error trashing email: {e}")
            return f"Error: {e}"
        except CourierError:
            return "Failed to trash email"
        except Exception as e:
            logger.error(f"Error trashing email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="delete")
    async def delete(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Expunge an email in place, irrecoverable. Normally use trash.

        Sets \\Deleted and EXPUNGEs in the given folder. On standard IMAP a
        permanent removal that bypasses the Trash; on Gmail it only removes
        this folder's label, leaving the message in All Mail. For normal
        removal use the trash tool.

        Args:
            folder: Folder name
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            client.delete_email(uid, folder)
            return "Email deleted"
        except CourierError:
            return "Failed to delete email"
        except Exception as e:
            logger.error(f"Error deleting email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="search", description=_SEARCH_TOOL_DESCRIPTION)
    async def search(
        query: Union[str, int] = "",
        ctx: Optional[Context] = None,
        folder: Optional[str] = None,
        limit: int = 50,
        imap: Optional[str] = None,
        no_cache: bool = False,
    ) -> str:
        """Search for emails using Gmail-style query syntax.

        The full operator inventory is rendered into the tool description
        (see ``_SEARCH_TOOL_DESCRIPTION``, derived from the query parser).

        Args:
            query: Gmail-style search query (numeric IDs are converted to strings).
            folder: Folder to search in (None for all folders)
            limit: Maximum number of results
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)
            no_cache: Bypass the local cache and query live IMAP.

        Returns:
            JSON-formatted dict ``{"results": [...], "provenance": {...}}``.
            ``provenance.source`` is ``"local"`` when the call was served
            from a local mu cache and ``"remote"`` when it went to IMAP;
            ``provenance.indexed_at`` carries the local index mtime when
            applicable; ``provenance.fell_back_reason`` names the
            condition that forced an IMAP fallback (``"no_cache"``,
            ``"mu_missing"``, ``"db_missing"``, ``"stale"``,
            ``"untranslatable"``, ``"exception"``) or is ``null``.
        """
        if ctx is None:
            return json.dumps({"error": "No MCP context available"})
        client = get_client_from_context(ctx, imap)
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    client.search_emails,
                    str(query),
                    folder=folder,
                    limit=limit,
                    no_cache=no_cache,
                ),
                timeout=30.0,
            )
            return json.dumps(results, indent=2, default=str)
        except asyncio.TimeoutError:
            error_msg = f"Email search timed out after 30 seconds (query={query}, folder={folder})"
            logger.error(error_msg)
            return json.dumps({"error": error_msg, "results": []})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool(name="triage")
    async def triage(
        folder: str,
        uid: int,
        action: str,
        ctx: Context,
        notes: Optional[str] = None,
        target_folder: Optional[str] = None,
        imap: Optional[str] = None,
    ) -> str:
        """Process an email with specified action.

        This is a higher-level tool that combines multiple actions and records
        the decision for learning purposes.

        Args:
            folder: Folder name
            uid: Email UID
            action: Action to take (move, read, unread, flag, unflag, trash, delete)
            notes: Optional notes about the decision
            target_folder: Target folder for move action
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message or error message
        """
        client = get_client_from_context(ctx, imap)
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            return f"Email with UID {uid} not found in folder {folder}"
        try:
            return client.process_email_action(
                uid, folder, action, target_folder=target_folder
            )
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error(f"Error processing email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="accept-invite")
    async def accept_invite(
        folder: str,
        uid: int,
        ctx: Context,
        availability_mode: str = "random",
        imap: Optional[str] = None,
    ) -> dict:
        """Process a meeting invite email and create a draft reply.

        This tool orchestrates the full workflow:
        1. Identifies if the email is a meeting invite
        2. Checks calendar availability for the meeting time
        3. Generates an appropriate reply (accept/decline)
        4. Creates a MIME message for the reply
        5. Saves the reply as a draft

        Args:
            folder: Folder containing the invite email
            uid: UID of the invite email
            ctx: MCP context
            availability_mode: Mode for availability check (random, always_available,
                              always_busy, business_hours, weekdays)

        Returns:
            Dictionary with the processing result
        """
        from courier.workflows.meeting_reply import process_meeting_invite_workflow

        client = get_client_from_context(ctx, imap)
        return process_meeting_invite_workflow(client, folder, uid, availability_mode)

    @mcp.tool(name="attachments")
    async def attachments(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """List attachments for a specific email.

        Args:
            folder: Folder name
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            JSON-formatted list of attachments with metadata
        """
        client = get_client_from_context(ctx, imap)
        try:
            email_obj = client.fetch_email(uid, folder)
            if not email_obj:
                return json.dumps(
                    {"error": f"Email with UID {uid} not found in folder {folder}"}
                )
            return json.dumps(email_obj.attachment_summaries(), indent=2)
        except Exception as e:
            logger.error(f"Error listing attachments: {e}")
            return json.dumps({"error": str(e)})

    @mcp.tool(name="save")
    async def save(
        folder: str,
        uid: int,
        attachment: str,
        save_path: str,
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Download an attachment by filename or index.

        Args:
            folder: Folder name
            uid: Email UID
            attachment: Attachment filename or index (as string)
            save_path: Path where to save the attachment
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message with filename and size, or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            email_obj = client.fetch_email(uid, folder)
            if not email_obj:
                return f"Error: Email with UID {uid} not found in folder {folder}"
            result = email_obj.save_attachment(attachment, save_path)
            logger.info(
                f"Saved attachment '{result['filename']}' ({result['size']} bytes) to {result['saved']}"
            )
            return f"Success: Saved '{result['filename']}' ({result['size']} bytes) to {result['saved']}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Error downloading attachment: {e}")
            return f"Error: {e}"

    @mcp.tool(name="export")
    async def export(
        folder: str,
        uid: int,
        save_path: str,
        ctx: Context,
        raw: bool = False,
        imap: Optional[str] = None,
    ) -> str:
        """Export an email to a standalone file.

        Default exports HTML with embedded images. With ``raw=True`` exports
        the raw RFC 822 message bytes as stored on the IMAP server, suitable
        for bounces, archival, or feeding into another MIME-processing tool.

        Args:
            folder: Folder name
            uid: Email UID
            save_path: Path where to save the exported file
            ctx: MCP context
            raw: If True, save the raw RFC 822 bytes instead of HTML
            imap: [imap.NAME] block name (None for default)

        Returns:
            Success message with file path and size, or error message
        """
        client = get_client_from_context(ctx, imap)
        try:
            if raw:
                fetched = client.fetch_raw(uid, folder)
                if not fetched:
                    return f"Error: Email with UID {uid} not found in folder {folder}"
                raw_bytes = fetched["raw"]
                dir_part = os.path.dirname(save_path)
                if dir_part:
                    os.makedirs(dir_part, exist_ok=True)
                with open(save_path, "wb") as fh:
                    fh.write(raw_bytes)
                size = len(raw_bytes)
                logger.info(f"Exported raw message ({size} bytes) to {save_path}")
                return f"Success: Exported raw message ({size} bytes) to {save_path}"

            email_obj = client.fetch_email(uid, folder)
            if not email_obj:
                return f"Error: Email with UID {uid} not found in folder {folder}"
            result = email_obj.export_html_to_file(save_path)
            logger.info(
                f"Exported HTML content ({result['size']} bytes) to {result['saved']}"
            )
            return f"Success: Exported HTML content ({result['size']} bytes) to {result['saved']}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"Error exporting: {e}")
            return f"Error: {e}"

    @mcp.tool(name="copy")
    async def copy(
        from_imap: str,
        from_folder: str,
        uid: int,
        ctx: Context,
        to_folder: str = "INBOX",
        move: bool = False,
        preserve_flags: bool = False,
        imap: Optional[str] = None,
    ) -> str:
        """Import an email from one [imap.NAME] block into another's folder.

        Fetches the raw RFC 822 message from the source block and APPENDs
        it to the destination, preserving the message byte-for-byte. The
        original internal date is always preserved so the email appears
        at its original chronological position.

        Args:
            from_imap: Source [imap.NAME] block.
            from_folder: Folder in the source block containing the email.
            uid: UID of the email in the source folder.
            ctx: MCP context.
            to_folder: Destination folder (default: INBOX).
            move: If True, delete the email from the source after import.
            preserve_flags: If True, copy original flags to the destination.
                If False, email arrives with no flags (unread, unflagged).
            imap: Destination [imap.NAME] block (None for default).

        Returns:
            Status message with the UID assigned by the destination server.
        """
        from courier.imap_client import copy_email_between_imap_blocks

        source_client = get_client_from_context(ctx, from_imap)
        dest_client = get_client_from_context(ctx, imap)

        try:
            result = copy_email_between_imap_blocks(
                source_client,
                dest_client,
                uid,
                from_folder,
                to_folder=to_folder,
                move=move,
                preserve_flags=preserve_flags,
            )
            if not result["success"]:
                return f"Error: {result['error']}"

            subject = result["subject"]
            uid_info = f" as UID {result['new_uid']}" if result["new_uid"] else ""

            if result["moved"]:
                return (
                    f'Imported and removed from source: "{subject}" '
                    f"(UID {uid} from {from_imap}/{from_folder}) -> "
                    f"{to_folder}{uid_info}"
                )

            return (
                f'Imported "{subject}" '
                f"(UID {uid} from {from_imap}/{from_folder}) "
                f"into {to_folder}{uid_info}"
            )
        except Exception as e:
            logger.error(f"Error importing email: {e}")
            return f"Error: {e}"

    @mcp.tool(name="links")
    async def links(
        folder: str,
        uids: List[int],
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """Extract all links from email HTML content for multiple emails.

        This tool is useful for fraud detection and security analysis, allowing
        you to examine all URLs in multiple emails without downloading the full HTML content.
        Links are deduplicated per email (only first occurrence of each URL is kept per email).

        Args:
            folder: Folder name
            uids: List of email UIDs
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            JSON-formatted list of results, one per UID
        """
        client = get_client_from_context(ctx, imap)
        results = extract_links_batch(client.fetch_email, folder, uids)
        return json.dumps(results, indent=2)

    @mcp.tool(name="read")
    async def read(
        folder: str,
        uid: int,
        ctx: Context,
        imap: Optional[str] = None,
        no_cache: bool = False,
    ) -> str:
        """Read an email's content by folder and UID.

        Args:
            folder: Folder name
            uid: Email UID
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)
            no_cache: Bypass the local cache and read from live IMAP.

        Returns:
            JSON-formatted email with headers, content, and attachment list
        """
        client = get_client_from_context(ctx, imap)
        try:
            email_obj = client.fetch_email(uid, folder, no_cache=no_cache)
            if not email_obj:
                return json.dumps(
                    {"error": f"Email with UID {uid} not found in folder {folder}"}
                )
            result: Dict[str, Any] = {
                "uid": uid,
                "folder": folder,
                "from": str(email_obj.from_),
                "to": [str(to) for to in email_obj.to],
                "subject": email_obj.subject,
                "date": (
                    email_obj.date.astimezone().isoformat() if email_obj.date else None
                ),
                "flags": email_obj.flags,
                "content_type": (
                    "text/html" if email_obj.content.html else "text/plain"
                ),
                "body": (
                    str(email_obj.content.html)
                    if email_obj.content.html
                    else str(email_obj.content.text) if email_obj.content.text else None
                ),
            }
            if email_obj.cc:
                result["cc"] = [str(cc) for cc in email_obj.cc]
            if email_obj.attachments:
                result["attachments"] = email_obj.attachment_summaries()
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error reading email: {e}")
            return json.dumps({"error": str(e)})

    @mcp.tool(name="folders")
    async def folders(
        ctx: Context,
        imap: Optional[str] = None,
    ) -> str:
        """List available email folders.

        Args:
            ctx: MCP context
            imap: [imap.NAME] block name (None for default)

        Returns:
            JSON-formatted list of folder names. Under WORLD_AS_OF the
            list is wrapped and flagged as current-state data:
            ``{"folders": [...], "world_as_of": {...}}``.
        """
        client = get_client_from_context(ctx, imap)
        try:
            return json.dumps(client.folders_result(), indent=2)
        except Exception as e:
            logger.error(f"Error listing folders: {e}")
            return json.dumps({"error": str(e)})
