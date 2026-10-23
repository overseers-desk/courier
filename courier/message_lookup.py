"""Find the one folder holding a Message-ID: the local cache first, then IMAP."""

import os
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional

from courier.errors import (
    AmbiguousMessageId,
    CourierError,
    PermanentError,
    TransientError,
)
from courier.models import message_ids
from courier.query.ast import UntranslatableForBackend

if TYPE_CHECKING:
    from courier.imap_client import ImapClient

_CANNOT_LOOK_UP = "cannot look up Message-ID on this account"


class MessageLocation(NamedTuple):
    """Where a Message-ID was found.

    ``path`` is set only when ``uid`` is ``None`` and the body is on
    disk at ``path``.
    """

    folder: str
    uid: Optional[int]
    path: Optional[str]


def bare_message_id(message_id: Any) -> str:
    """Return a Message-ID without whitespace, angle brackets or comments.

    The first ``<...>`` identifier is taken when there is one, so a
    trailing comment (``<a@b.c> (note)``) is ignored; a value without
    angle brackets is taken whole.

    Args:
        message_id: The Message-ID as given or as a hit carries it.

    Returns:
        The identifier without its angle brackets.
    """
    text = str(message_id)
    found = message_ids(text)
    return found[0][1:-1] if found else text.strip().strip("<>")


def _cached_location(
    client: "ImapClient",
    bare: str,
    scopes: List[Optional[str]],
    need_uid: bool,
) -> Optional[MessageLocation]:
    """Answer from the local cache alone, with no network.

    The mu index gives the message's maildir path and folder, and its
    UID when the file name is mbsync-style. The cache answers only when
    every hit is that Message-ID with a folder and a path; anything
    else (no cache, an index miss, a redacted or odd hit) is left to
    the live search, as is a hit whose UID is unknown while the caller
    needs one or whose file is not on disk.

    Args:
        client: Client for the account to search.
        bare: The Message-ID without angle brackets.
        scopes: Folders to look in, in order; ``[None]`` looks in all.
        need_uid: When ``True`` the caller cannot use a bare file path.

    Returns:
        The location, or ``None`` when the live search must decide.

    Raises:
        AmbiguousMessageId: When copies sit in several folders and every
            one carries a UID; copies without one leave it to the live
            search, which knows them.
    """
    hits: List[Dict[str, Any]] = []
    for folder in scopes:
        try:
            hits += client.local_message_id_hits(bare, folder) or []
        except (CourierError, UntranslatableForBackend, ValueError):
            return None
    if not hits:
        return None
    for hit in hits:
        if (
            bare_message_id(hit.get("message_id", "")) != bare
            or not hit.get("folder")
            or not hit.get("path")
        ):
            return None
    if len({h["folder"] for h in hits}) > 1:
        if any(h.get("uid") is None for h in hits):
            return None
        raise AmbiguousMessageId(
            bare, sorted((h["folder"], int(h["uid"])) for h in hits)
        )
    first = hits[0]
    if first.get("uid") is not None:
        return MessageLocation(first["folder"], int(first["uid"]), None)
    if not need_uid and os.path.isfile(first["path"]):
        return MessageLocation(first["folder"], None, first["path"])
    return None


def _live_location(
    client: "ImapClient", bare: str, scopes: List[Optional[str]]
) -> Optional[MessageLocation]:
    """Answer from a live ``msgid:`` search of each folder asked.

    Args:
        client: Client for the account to search.
        bare: The Message-ID without angle brackets.
        scopes: Folders to search, in order; ``[None]`` searches all.

    Returns:
        The location, or ``None`` when every folder was searched and
        none holds the Message-ID.

    Raises:
        AmbiguousMessageId: When copies sit in several folders.
        TransientError: When the search failed in some folder or lost
            its connection.
        PermanentError: When the search cannot run on this account, or
            the server returned messages with other Message-IDs.
        ValueError: When a folder is not allowed or the query is
            malformed.
        ConnectionError: When the client cannot connect.
    """
    hits: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for folder in scopes:
        try:
            envelope = client.search_emails(
                f"msgid:<{bare}>", folder=folder, limit=20, no_cache=True
            )
        except TransientError as exc:
            raise TransientError(f"{_CANNOT_LOOK_UP}: {exc}") from exc
        except (CourierError, UntranslatableForBackend) as exc:
            raise PermanentError(f"{_CANNOT_LOOK_UP}: {exc}") from exc
        hits += envelope.get("results") or []
        failed += envelope.get("folders_failed") or []
    if failed:
        names = ", ".join(f.get("folder", "?") for f in failed)
        raise TransientError(f"{_CANNOT_LOOK_UP}: search failed in {names}")
    if any(bare_message_id(h.get("message_id", "")) != bare for h in hits):
        raise PermanentError(
            f"{_CANNOT_LOOK_UP}: the server returned messages with other "
            "Message-IDs, so it does not honour the search"
        )
    if not hits:
        return None
    if len({h["folder"] for h in hits}) > 1:
        raise AmbiguousMessageId(
            bare, sorted((h["folder"], int(h["uid"])) for h in hits)
        )
    return MessageLocation(hits[0]["folder"], int(hits[0]["uid"]), None)


def locate_message_id(
    client: "ImapClient",
    message_id: str,
    folders: Optional[List[str]] = None,
    *,
    need_uid: bool = True,
    use_cache: bool = True,
) -> Optional[MessageLocation]:
    """Find the folder, UID and (for a cache hit) file of a Message-ID.

    The local cache is asked first and answers without a connection.
    The live ``msgid:`` search runs only when the cache cannot answer,
    or answers with a file whose UID is unknown while the caller needs
    one, or whose file is not on disk. A live hit whose Message-ID
    differs raises: IMAP ``HEADER`` is a substring match, and some
    servers ignore it.

    Args:
        client: Client for the account to search; only the live search
            needs it connected.
        message_id: The Message-ID, with or without ``<>``.
        folders: Folders to search, in order; ``None`` searches all.
        need_uid: When ``True`` the caller cannot use a bare file path.
        use_cache: When ``False`` the cache is skipped and the live
            search answers.

    Returns:
        The location, or ``None`` when every folder asked was searched
        and none holds the Message-ID.

    Raises:
        AmbiguousMessageId: When copies sit in several folders.
        TransientError: When the search failed in some folder or lost
            its connection.
        PermanentError: When the search cannot run on this account, or
            the server returned messages with other Message-IDs.
        TypeError: When ``folders`` is one name rather than a list.
        ValueError: When a folder is not allowed or the query is
            malformed.
        ConnectionError: When the live search cannot connect.
    """
    if isinstance(folders, str):
        raise TypeError("folders takes a list of folder names, not one name")
    bare = bare_message_id(message_id)
    scopes: List[Optional[str]] = [None] if folders is None else list(folders)
    if use_cache:
        cached = _cached_location(client, bare, scopes, need_uid)
        if cached is not None:
            return cached
    return _live_location(client, bare, scopes)
