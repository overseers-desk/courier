"""IMAP IDLE watch: a generator of mailbox change events.

The seam between the IMAP layer and daemon-side folder watching.
:func:`watch` owns a dedicated connection (IDLE monopolizes one and
serves exactly one selected mailbox) and yields :class:`WatchEvent`
items until the ``stop`` event is set or the consumer closes the
generator.

Usage notes:

- Generators are not thread-safe; run one per thread. Multi-folder
  fan-out is N generators in N threads, composed by the caller.
- Gmail budgets ~15 simultaneous IMAP connections per account; each
  watcher holds one for its whole lifetime.
- The mailbox is SELECTed read-only, so the watcher can neither mutate
  the folder nor mark messages seen.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from courier.config import ImapBlock
from courier.errors import (
    CapabilityMissing,
    TransientError,
    WorldBoundRefused,
    as_courier_error,
)
from courier.imap_client import ImapClient
from courier.world_bound import world_as_of

logger = logging.getLogger(__name__)

# Untagged response tokens worth surfacing, mapped to event kinds.
_KIND_BY_TOKEN = {b"EXISTS": "exists", b"EXPUNGE": "expunge", b"FETCH": "flags"}

_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0


@dataclass
class WatchEvent:
    """One mailbox change observed by :func:`watch`.

    Kinds and what ``count`` means for each:

    - ``started``: first successful SELECT. ``uidvalidity`` is set and
      ``count`` is the mailbox's total message count at that moment.
    - ``exists``: the mailbox size changed; ``count`` is the new total
      number of messages (RFC 3501 EXISTS semantics — not a delta).
    - ``expunge``: a message was removed; ``count`` is its message
      sequence number at removal time.
    - ``flags``: a message's flags changed; ``count`` is its message
      sequence number.
    - ``reconnected``: the connection was rebuilt after a transient
      failure and the folder re-SELECTed; fields as for ``started``.
      A ``uidvalidity`` differing from the previous started/reconnected
      event means the mailbox was reset and cached UIDs are void.
    """

    kind: str
    folder: str
    uidvalidity: Optional[int] = None
    count: Optional[int] = None
    raw: str = ""


def _to_event(resp: Tuple, folder: str) -> Optional[WatchEvent]:
    """Map one idle_check untagged response to a WatchEvent, or None.

    imapclient yields tuples like ``(1, b'EXISTS')``, ``(5, b'EXPUNGE')``,
    ``(1, b'FETCH', (b'FLAGS', ...))`` plus status noise like
    ``(b'OK', b'Still here')``; only the first three become events.
    """
    if len(resp) < 2 or not isinstance(resp[0], int):
        return None
    token = resp[1] if isinstance(resp[1], bytes) else str(resp[1]).encode("ascii")
    kind = _KIND_BY_TOKEN.get(token.upper())
    if kind is None:
        return None
    return WatchEvent(kind=kind, folder=folder, count=resp[0], raw=repr(resp))


def watch(
    block: ImapBlock,
    folder: str = "INBOX",
    *,
    reissue_after: int = 15 * 60,
    poll_interval: float = 30.0,
    stop: Optional[threading.Event] = None,
) -> Iterator[WatchEvent]:
    """Yield mailbox change events for *folder* via IMAP IDLE.

    Owns a dedicated connection built through :class:`ImapClient`'s
    connect path, so OAuth2/XOAUTH2 and token refresh apply on every
    reconnect. IDLE monopolizes a connection and serves one selected
    mailbox: run one generator per folder, one per thread (generators
    are not thread-safe), and mind Gmail's ~15-connection-per-account
    budget. The folder is SELECTed read-only so the watcher can neither
    mutate the mailbox nor mark messages seen.

    IDLE is reissued every *reissue_after* seconds (default 15 min,
    under RFC 2177's 29-minute cap; NAT gear drops idle TCP earlier).
    *poll_interval* bounds both the idle_check granularity and the
    latency of noticing *stop*; when *stop* is set the generator
    terminates IDLE and returns.

    On transient failure/abort the connection is rebuilt with capped
    exponential backoff (1 s doubling to 60 s) and a ``reconnected``
    event carries the fresh UIDVALIDITY.

    Refused outright — eagerly, at call time — when ``WORLD_AS_OF`` is
    set: a watch is a live tail of events after the bound, and every
    one of them would be dated in the bound's future.

    Args:
        block: [imap.NAME] block to connect with.
        folder: Folder to watch.
        reissue_after: Seconds between IDLE reissues.
        poll_interval: idle_check timeout in seconds.
        stop: Optional event; setting it ends the generator (observed
            at poll granularity).

    Raises:
        WorldBoundRefused: WORLD_AS_OF is set.
        WorldAsOfInvalid: WORLD_AS_OF is set but unparseable or naive.
        CapabilityMissing: The server does not advertise IDLE.
        PermanentError: The server answered NO/BAD.
    """
    bound = world_as_of()
    if bound is not None:
        raise WorldBoundRefused(
            "watch refused: a live tail of the mailbox is meaningless "
            f"under WORLD_AS_OF {bound.isoformat()}"
        )
    return _watch_events(
        block,
        folder,
        reissue_after=reissue_after,
        poll_interval=poll_interval,
        stop=stop,
    )


def _watch_events(
    block: ImapBlock,
    folder: str,
    *,
    reissue_after: int,
    poll_interval: float,
    stop: Optional[threading.Event],
) -> Iterator[WatchEvent]:
    """The IDLE loop behind :func:`watch` (see its docstring)."""
    client = ImapClient(block)
    kind_next = "started"
    backoff = _BACKOFF_START
    try:
        while stop is None or not stop.is_set():
            try:
                client.connect()
                if not client.has_capability("IDLE"):
                    raise CapabilityMissing(
                        f"server {block.host} does not advertise IDLE"
                    )
                # ponytail: a folder the server NOs is retried like a
                # transient failure (ImapClient.select_folder wraps NO
                # in ConnectionError); a daemon watcher prefers waiting
                # for the folder over dying.
                info = client.select_folder(folder, readonly=True)
                uidvalidity = info.get(b"UIDVALIDITY")
                yield WatchEvent(
                    kind=kind_next,
                    folder=folder,
                    uidvalidity=uidvalidity,
                    count=info.get(b"EXISTS"),
                    raw=f"SELECT {folder} UIDVALIDITY {uidvalidity}",
                )
                kind_next = "reconnected"
                backoff = _BACKOFF_START
                raw_client = client._client_or_raise()
                raw_client.idle()
                idle_since = time.monotonic()
                while stop is None or not stop.is_set():
                    for resp in raw_client.idle_check(poll_interval):
                        event = _to_event(resp, folder)
                        if event is not None:
                            yield event
                    if time.monotonic() - idle_since >= reissue_after:
                        raw_client.idle_done()
                        raw_client.idle()
                        idle_since = time.monotonic()
                raw_client.idle_done()
                return
            except Exception as e:
                err = as_courier_error(e)
                if not isinstance(err, TransientError):
                    if err is e:
                        raise
                    raise err from e
                logger.warning(
                    "watch(%s): transient failure (%s); reconnecting in %.0fs",
                    folder,
                    e,
                    backoff,
                )
                client.disconnect()
                if stop is not None:
                    stop.wait(backoff)
                else:
                    time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)
    finally:
        client.disconnect()
