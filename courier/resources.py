"""MCP resources implementation for email access."""

import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

from courier.imap_client import ImapClient

logger = logging.getLogger(__name__)

# Result caps for the listing/search resources. Both surfaces mark the
# cut with total_count/truncated instead of truncating silently.
_LIST_CAP = 50
_SEARCH_LIMIT = 50


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
            JSON-formatted list of folders. Under WORLD_AS_OF the list
            is wrapped and flagged as current-state data.
        """
        return json.dumps(imap_client.folders_result(), indent=2)

    # List email summaries in a folder
    @mcp.resource("email://{folder}/list")
    async def list_emails(folder: str) -> str:
        """List the newest emails in a folder, summary-level.

        Capped at 50 messages; the cut is marked, never silent, and
        only headers/flags/structure are fetched (no message bodies).

        Args:
            folder: Folder name

        Returns:
            JSON ``{"results": [...], "total_count": N, "truncated":
            bool}`` where ``total_count`` is the folder's full match
            count; ``{"error": ...}`` on failure.
        """
        try:
            uids = imap_client.search("ALL", folder=folder)
            total = len(uids)
            page = sorted(uids, reverse=True)[:_LIST_CAP]
            summaries = imap_client.fetch_summaries(page, folder=folder)
            payload = {
                "results": summaries,
                "total_count": total,
                "truncated": total > _LIST_CAP,
            }
            return json.dumps(payload, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error listing emails: {e}")
            return json.dumps({"error": str(e)})

    # Search emails across folders
    @mcp.resource("email://search/{query}")
    async def search_emails(query: str) -> str:
        """Search for emails across folders using Gmail-style query syntax.

        Runs through the shared search path, so the envelope matches
        the search tool: dispatch (local cache, then remote with the
        capability-gated emitter), ``provenance`` with the query
        translation report, ``folders_failed`` when some folder's
        search failed, and ``total_count``/``truncated`` marking the
        50-result cap.

        Args:
            query: Gmail-style search query (e.g. ``from:alice``,
                   ``is:unread``, ``meeting notes``).

        Returns:
            The JSON search envelope, or ``{"error": ...}`` when the
            search could not run at all (connection failure, a query no
            backend could express, a refused charset).
        """
        try:
            result = imap_client.search_emails(
                query, folder=None, limit=_SEARCH_LIMIT
            )
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            logger.error(
                f"{imap_client.block.label} Error searching for {query!r}: {e}"
            )
            return json.dumps({"error": str(e)})

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
