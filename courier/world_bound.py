"""WORLD_AS_OF: the office-wide as-of bound, parsed once, enforced downstream.

``WORLD_AS_OF`` is an environment variable carrying an ISO-8601 timestamp
with a timezone offset (e.g. ``2026-07-12T17:07:00+10:00``). When set,
nothing dated after that instant may leave the tool, so a session replayed
against the mailbox later yields the same answers. The three semantics:

1. Unset: unbounded, normal operation, zero cost for existing callers.
2. Set: searches are prefiltered server-side and post-filtered exactly;
   reads of messages dated after the bound are refused; ``watch`` refuses.
3. Set but unparseable, or naive (no timezone offset): hard failure at
   startup via :class:`~courier.errors.WorldAsOfInvalid`, never a silent
   fallback: a silently ignored bound produces a contaminated replay that
   looks valid.

This module is the single source of truth for parsing the variable and
for the comparison predicates; enforcement lives in ``ImapClient``,
``local_cache``, and ``watch``. Deliberately a function plus small
predicates, not a class: the state is one ``Optional[datetime]`` and the
behaviours are pure functions over it.
"""

import os
from datetime import datetime
from typing import Optional

from courier.errors import WorldAsOfInvalid

ENV_VAR = "WORLD_AS_OF"


def world_as_of() -> Optional[datetime]:
    """Parse the ``WORLD_AS_OF`` environment variable.

    Returns:
        ``None`` when the variable is unset (normal, unbounded
        operation); otherwise the bound as a timezone-aware
        ``datetime``.

    Raises:
        WorldAsOfInvalid: When the variable is set but empty,
            unparseable, or naive (no timezone offset). Naive
            timestamps are rejected because accepting one would
            silently bind against an assumed zone, a cousin of the
            silent fallback the contract forbids.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    try:
        bound = datetime.fromisoformat(raw)
    except ValueError as e:
        raise WorldAsOfInvalid(
            f"{ENV_VAR}={raw!r} is not a valid ISO-8601 timestamp "
            f"(expected e.g. 2026-07-12T17:07:00+10:00): {e}"
        ) from e
    if bound.tzinfo is None or bound.tzinfo.utcoffset(bound) is None:
        raise WorldAsOfInvalid(
            f"{ENV_VAR}={raw!r} has no timezone offset; a naive timestamp "
            "would silently bind against an assumed zone. Append an offset, "
            "e.g. 2026-07-12T17:07:00+10:00."
        )
    return bound


def _as_aware(dt: datetime) -> datetime:
    """Return *dt* as an aware datetime, taking naive values as local time.

    imapclient normalises INTERNALDATE to a naive datetime in the local
    zone, and Date headers occasionally parse naive; both are compared
    against the (always aware) bound in local time.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.astimezone()
    return dt


def after_bound(dt: datetime, bound: datetime) -> bool:
    """Whether *dt* falls strictly after the bound instant.

    Args:
        dt: A message's date (INTERNALDATE or indexed date). Naive
            values are taken as local time.
        bound: The parsed ``WORLD_AS_OF`` instant (aware).

    Returns:
        ``True`` when the message is dated after the bound and must not
        leave the tool.
    """
    return _as_aware(dt) > bound


def refusal_message(dt: datetime, bound: datetime) -> str:
    """The standard refusal text for a direct read of a post-bound message.

    Args:
        dt: The message's date. Naive values are taken as local time.
        bound: The parsed ``WORLD_AS_OF`` instant (aware).

    Returns:
        A message naming both instants and ``WORLD_AS_OF`` as the
        reason, e.g. ``message dated 2026-07-13T09:12:00+10:00 is after
        WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused``.
    """
    return (
        f"message dated {_as_aware(dt).isoformat()} is after "
        f"{ENV_VAR} {bound.isoformat()}; refused"
    )
