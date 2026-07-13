"""MCP resources implementation for email access."""

import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

from courier.imap_client import ImapClient
from courier.query_parser import parse_query

logger = logging.getLogger(__name__)


def get_client_from_context(
    ctx: Context, imap_name: Optional[str] = None
) -> ImapClient:
    """Get IMAP client from context, optionally for a specific block.

    Args:
        ctx: MCP context
        imap_name: [imap.NAME] block name. When *None*, the default
            [imap.NAME] block is used.

    Returns:
        IMAP client for the requested block.

    Raises:
        RuntimeError: If IMAP client is not available or the block name
            is unknown.
    """
    lc: Any = ctx.request_context.lifespan_context

    clients = lc.get("imap_clients")
    if clients is not None:
        default = lc.get("default_imap", "")
        key = imap_name or default
        if key not in clients:
            available = list(clients.keys())
            raise RuntimeError(f"Unknown [imap.{key}] block. Available: {available}")
        client: ImapClient = clients[key]
        return client

    legacy_client = lc.get("imap_client")
    if not legacy_client:
        raise RuntimeError("IMAP client not available")
    result: ImapClient = legacy_client
    return result


def get_smtp_client_from_context(ctx: Context) -> Any:
    """Get SMTP client from context.

    Args:
        ctx: MCP context

    Returns:
        SMTP client

    Raises:
        RuntimeError: If SMTP client is not available
    """
    lc: Any = ctx.request_context.lifespan_context
    client = lc.get("smtp_client")
    if not client:
        raise RuntimeError("SMTP client not available")
    return client


def register_resources(mcp: FastMCP, imap_client: ImapClient) -> None:
    """Register MCP resources.

    Args:
        mcp: MCP server
        imap_client: IMAP client
    """

    # List folders resource
    @mcp.resource("email://folders")
    async def get_folders() -> str:
        """List available email folders.

        Returns:
            JSON-formatted list of folders
        """
        folders = imap_client.list_folders()
        return json.dumps(folders, indent=2)

    # List email summaries in a folder
    @mcp.resource("email://{folder}/list")
    async def list_emails(folder: str) -> str:
        """List emails in a folder.

        Args:
            folder: Folder name

        Returns:
            JSON-formatted list of email summaries
        """
        # Search for all emails in the folder
        try:
            uids = imap_client.search("ALL", folder=folder)

            # Limit to the 50 most recent emails to avoid overwhelming
            # the LLM with too much context
            uids = sorted(uids, reverse=True)[:50]

            # Fetch emails
            emails = imap_client.fetch_emails(uids, folder=folder)

            # Create summaries
            summaries = []
            for uid, email_obj in emails.items():
                summaries.append(
                    {
                        "uid": uid,
                        "folder": folder,
                        "from": str(email_obj.from_),
                        "to": [str(to) for to in email_obj.to],
                        "subject": email_obj.subject,
                        "date": (
                            email_obj.date.astimezone().isoformat()
                            if email_obj.date
                            else None
                        ),
                        "flags": email_obj.flags,
                        "has_attachments": len(email_obj.attachments) > 0,
                    }
                )

            return json.dumps(summaries, indent=2)
        except Exception as e:
            logger.error(f"Error listing emails: {e}")
            return f"Error: {e}"

    # Search emails across folders
    @mcp.resource("email://search/{query}")
    async def search_emails(query: str) -> str:
        """Search for emails across folders using Gmail-style query syntax.

        Args:
            query: Gmail-style search query (e.g. ``from:alice``,
                   ``is:unread``, ``meeting notes``).

        Returns:
            JSON-formatted list of email summaries
        """
        # Relative terms anchor to the client's WORLD_AS_OF bound when
        # set (parse_query defaults to the wall clock on None).
        search_spec = parse_query(query, now=imap_client.world_as_of)
        folders = imap_client.list_folders()
        results = []

        for folder in folders:
            try:
                uids = imap_client.search(search_spec, folder=folder)
                uids = sorted(uids, reverse=True)[:10]

                if uids:
                    emails = imap_client.fetch_emails(uids, folder=folder)
                    for uid, email_obj in emails.items():
                        results.append(
                            {
                                "uid": uid,
                                "folder": folder,
                                "from": str(email_obj.from_),
                                "to": [str(to) for to in email_obj.to],
                                "subject": email_obj.subject,
                                "date": (
                                    email_obj.date.astimezone().isoformat()
                                    if email_obj.date
                                    else None
                                ),
                                "flags": email_obj.flags,
                                "has_attachments": len(email_obj.attachments) > 0,
                            }
                        )
            except Exception as e:
                logger.warning(
                    f"{imap_client.block.label} Error searching folder {folder}: {e}"
                )

        results.sort(key=lambda x: str(x.get("date") or "0"), reverse=True)
        return json.dumps(results, indent=2)

    # Get a specific email by UID
    @mcp.resource("email://{folder}/{uid}")
    async def get_email(folder: str, uid: str) -> str:
        """Get a specific email.

        Args:
            folder: Folder name
            uid: Email UID

        Returns:
            Email content in text format
        """
        try:
            # Fetch email
            email_obj = imap_client.fetch_email(int(uid), folder=folder)

            if not email_obj:
                return f"Email with UID {uid} not found in folder {folder}"

            # Format email as text
            parts = [
                f"From: {email_obj.from_}",
                f"To: {', '.join(str(to) for to in email_obj.to)}",
            ]

            if email_obj.cc:
                parts.append(f"Cc: {', '.join(str(cc) for cc in email_obj.cc)}")

            if email_obj.date:
                parts.append(f"Date: {email_obj.date.astimezone().isoformat()}")

            parts.append(f"Subject: {email_obj.subject}")
            parts.append(f"Flags: {', '.join(email_obj.flags)}")

            if email_obj.attachments:
                parts.append(f"Attachments: {len(email_obj.attachments)}")
                for i, attachment in enumerate(email_obj.attachments, 1):
                    parts.append(
                        f"  {i}. {attachment.filename} ({attachment.content_type}, {attachment.size} bytes)"
                    )

            parts.append("")  # Empty line before content

            # Add email content - prefer HTML if available for link extraction
            if email_obj.content.html:
                parts.append("Content-Type: text/html")
                parts.append("")
                parts.append(str(email_obj.content.html))
            elif email_obj.content.text:
                parts.append("Content-Type: text/plain")
                parts.append("")
                parts.append(str(email_obj.content.text))
            else:
                parts.append("(No content)")

            return "\n".join(parts)
        except Exception as e:
            logger.error(f"Error fetching email: {e}")
            return f"Error: {e}"
