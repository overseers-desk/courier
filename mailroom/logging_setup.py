"""Logging destination setup for the mailroom CLI and MCP server.

Records are routed to the local syslog socket when one is reachable,
so that intermittent warnings (search-folder timeouts, connection
verification failures, IMAP logout errors) survive past the terminal
session and can be queried with e.g. ``journalctl -t mailroom``.  A
stderr fallback covers hosts that do not have a writable syslog socket
(Windows, sandboxes, macOS without a running syslog daemon, etc.), so
existing behaviour is preserved where syslog is absent.

See ``docs/INSTALLATION.md`` for the query path on each platform.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

_SYSLOG_SOCKETS = ("/dev/log", "/var/run/syslog")
_DEFAULT_FORMAT = "%(levelname)s %(name)s: %(message)s"


def setup_logging(level: int, stderr_format: Optional[str] = None) -> None:
    """Configure the root logger for mailroom.

    Replaces any existing handlers on the root logger with a single
    handler: a ``SysLogHandler`` if a local syslog socket is reachable,
    otherwise a ``StreamHandler`` on ``sys.stderr``.

    Args:
        level: Logging level applied to the root logger.
        stderr_format: Format string used by the stderr fallback only.
            Ignored when the syslog handler is selected, since syslog
            attaches its own timestamp and the ident-tag prefix.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass
    root.setLevel(level)
    handler = _make_syslog_handler() or _make_stderr_handler(stderr_format)
    root.addHandler(handler)


def _make_syslog_handler() -> Optional[logging.Handler]:
    """Return a ``SysLogHandler`` bound to the first reachable socket.

    The ident is set to ``mailroom`` so that systemd-journald records
    the SYSLOG_IDENTIFIER and ``journalctl -t mailroom`` works as a
    query path.

    Returns:
        A configured ``SysLogHandler``, or ``None`` if no syslog socket
        is reachable.
    """
    for address in _SYSLOG_SOCKETS:
        if not os.path.exists(address):
            continue
        try:
            handler = logging.handlers.SysLogHandler(address=address)
        except OSError:
            continue
        handler.ident = "mailroom: "
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        return handler
    return None


def _make_stderr_handler(fmt: Optional[str]) -> logging.Handler:
    """Return a ``StreamHandler`` writing to ``sys.stderr``.

    Args:
        fmt: Format string. Falls back to a level/name/message format
            when ``None``.

    Returns:
        A configured ``StreamHandler``.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt or _DEFAULT_FORMAT))
    return handler
