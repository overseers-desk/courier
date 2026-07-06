"""Typed error taxonomy for courier.

Two retry classes cover every IMAP failure mode:

- :class:`TransientError` — connection-layer trouble (aborted connection,
  socket errors, timeouts). Reconnect-and-retry semantics.
- :class:`PermanentError` — the server answered NO/BAD; retrying the
  identical command is useless.
"""

from typing import Optional, Tuple

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
