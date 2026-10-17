"""Typed error taxonomy for courier.

Two retry classes cover every IMAP failure mode:

- :class:`TransientError` — connection-layer trouble (aborted connection,
  socket errors, timeouts). Reconnect-and-retry semantics.
- :class:`PermanentError` — the server answered NO/BAD; retrying the
  identical command is useless.
"""

from typing import List, Optional, Tuple

from imapclient.exceptions import (  # type: ignore[import-untyped]
    IMAPClientAbortError,
    IMAPClientError,
)


class CourierError(Exception):
    """Base class for all courier errors."""


class TransientError(CourierError):
    """Connection-layer failure; reconnecting and retrying may succeed."""


class PermanentError(CourierError):
    """The server said NO/BAD; retrying the identical command is useless."""


class MessageNotFound(PermanentError):
    """The referenced UID does not exist in the folder."""


class FolderNotFound(PermanentError):
    """The referenced folder does not exist (or cannot be resolved)."""


class CapabilityMissing(PermanentError):
    """The server lacks a capability the operation requires."""


class WorldAsOfInvalid(CourierError):
    """A WORLD_AS_OF bound is unparseable or lacks a timezone offset.

    Raised at process start for a bad environment variable, so no partial
    output escapes under a bound the tool cannot honour, and by
    ``ImapClient`` for a naive ``world_as_of``.
    """


class WorldBoundRefused(PermanentError):
    """The operation would surface data dated after the WORLD_AS_OF bound.

    Raised on a direct read of a message dated after the bound, and by
    ``watch`` (a live tail of the future is meaningless under a bound).
    """


class AmbiguousMessageId(CourierError):
    """A Message-ID is held in more than one folder, so no one copy is the answer.

    Neither a server failure (:class:`PermanentError`) nor retryable
    (:class:`TransientError`), it subclasses :class:`CourierError`
    directly: the caller resolves it by narrowing the lookup to one
    folder. ``matches`` lists every ``(folder, uid)`` found; ``folders``
    names each folder once.
    """

    def __init__(self, message_id: str, matches: List[Tuple[str, int]]):
        self.message_id = message_id
        self.matches = matches
        super().__init__(
            f"Message-ID <{message_id}> is in several folders "
            f"({', '.join(self.folders)})"
        )

    @property
    def folders(self) -> List[str]:
        """The folders holding the Message-ID, sorted, each once."""
        return sorted({folder for folder, _uid in self.matches})


class FccUnresolved(PermanentError):
    """No usable Sent folder could be resolved for the FCC step."""

    def __init__(self, configured: Optional[str], tried: Tuple[str, ...]):
        self.configured = configured
        self.tried = tried
        if configured is not None:
            msg = (
                f"configured sent folder '{configured}' does not exist "
                f"on the IMAP server"
            )
        else:
            msg = "no Sent folder found on the IMAP server; tried: " + ", ".join(tried)
        super().__init__(msg)


def as_courier_error(exc: BaseException) -> CourierError:
    """Map a low-level exception into the courier taxonomy.

    IMAPClientAbortError / socket errors / timeouts map to
    :class:`TransientError`; IMAPClientError (server NO/BAD) maps to
    :class:`PermanentError`; anything already in the taxonomy passes
    through. Callers chain the original: ``raise as_courier_error(e) from e``.
    """
    if isinstance(exc, CourierError):
        return exc
    # Abort subclasses IMAPClientError in imapclient, so check it first.
    # socket.error is an alias of OSError, and TimeoutError subclasses it.
    if isinstance(exc, (IMAPClientAbortError, OSError)):
        return TransientError(str(exc) or repr(exc))
    if isinstance(exc, IMAPClientError):
        return PermanentError(str(exc) or repr(exc))
    return CourierError(str(exc) or repr(exc))
