"""Search [imap.NAME] blocks, for library callers and the CLI alike.

Each block is searched over its own client, in order, and that client
is closed before the next block. A block that fails is reported in its
own dict, never raised, so one unreachable server leaves the other
blocks' results standing. The client connects only when the search
needs the server, so a block the local cache answers makes no
connection.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from courier.config import CourierConfig
from courier.imap_client import ImapClient
from courier.local_cache import MuBackend
from courier.query import parse


def block_status(result: Dict[str, Any]) -> str:
    """Name how far one block's search got.

    Args:
        result: One block's search dict.

    Returns:
        ``"failed"`` when the dict carries ``error``; ``"partial"`` when
        some folder failed (``folders_failed`` is non-empty);
        ``"complete"`` otherwise. A ``limit`` cut (``truncated``) leaves
        a search complete: every folder was searched.
    """
    if "error" in result:
        return "failed"
    if result.get("folders_failed"):
        return "partial"
    return "complete"


def search_block(
    client: ImapClient,
    query: str,
    *,
    folder: Optional[str],
    limit: int,
    no_cache: bool,
) -> Dict[str, Any]:
    """Search one block, reporting a failure in the dict rather than raising.

    Args:
        client: Client for the block.
        query: Gmail-style search query.
        folder: Folder to search; ``None`` searches all.
        limit: Maximum number of results.
        no_cache: When ``True``, bypass the local cache and query live
            IMAP.

    Returns:
        :meth:`ImapClient.search_emails`'s dict, or ``{"error": ...}``
        when the search failed (``"connection failed: <reason>"`` when
        the server could not be reached), plus ``status`` (see
        :func:`block_status`).

    Raises:
        ValueError: When the search rejects the query (malformed, or an
            ``in:`` scope that conflicts with ``folder``), before any I/O.
    """
    try:
        result = client.search_emails(
            query, folder=folder, limit=limit, no_cache=no_cache
        )
    except ValueError:
        raise
    except ConnectionError as exc:
        result = {"error": f"connection failed: {exc}"}
    except Exception as exc:
        result = {"error": str(exc)}
    return {**result, "status": block_status(result)}


def search(
    config: CourierConfig,
    query: str,
    *,
    blocks: Optional[Sequence[str]] = None,
    folder: Optional[str] = None,
    limit: int = 10,
    no_cache: bool = False,
    world_as_of: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Search [imap.NAME] blocks, one client each, in order.

    Args:
        config: The courier configuration. When it has a
            ``[local_cache]`` table, one local-cache backend serves every
            block of this call.
        query: Gmail-style search query (see :func:`courier.parse`).
        blocks: [imap.NAME] block names to search; a name given twice is
            searched once. ``None`` searches every block in config
            order.
        folder: Folder to search in each block; ``None`` searches all.
        limit: Maximum number of results per block.
        no_cache: When ``True``, bypass the local cache and query live
            IMAP.
        world_as_of: The WORLD_AS_OF bound every block's client
            enforces, as an aware ``datetime``; ``None`` leaves the
            search unbounded. No environment variable is read.

    Returns:
        ``{name: result}`` in search order, each ``result`` as
        :func:`search_block` returns it.

    Raises:
        QuerySyntaxError: On a malformed query, before any client is
            built.
        TypeError: When ``blocks`` is one name rather than a list.
        ValueError: When ``blocks`` names a block the config lacks,
            before any client is built; or as :func:`search_block`
            raises it.
        WorldAsOfInvalid: When ``world_as_of`` has no timezone offset.
    """
    parse(query)
    if isinstance(blocks, str):
        raise TypeError("blocks takes a list of block names, not one name")
    names = list(config.imap_blocks) if blocks is None else list(dict.fromkeys(blocks))
    for name in names:
        if name not in config.imap_blocks:
            raise ValueError(
                f"unknown [imap.{name}] block. "
                f"Available: {list(config.imap_blocks)}"
            )
    backend = MuBackend(config.local_cache) if config.local_cache is not None else None
    results: Dict[str, Dict[str, Any]] = {}
    for name in names:
        client = ImapClient(
            config.imap_blocks[name], local_cache=backend, world_as_of=world_as_of
        )
        try:
            results[name] = search_block(
                client, query, folder=folder, limit=limit, no_cache=no_cache
            )
        finally:
            client.disconnect()
    return results
