"""Pins the signatures and names a library caller relies on across releases.

Programs that import courier call these by keyword and catch these
errors by class, so a renamed parameter, a changed default or a moved
base class breaks them without breaking anything in this repo. A test
failing here marks a change to what those callers were promised, not a
test to update alongside the code.
"""

import inspect
from typing import Any, List, Tuple

import courier
from courier import (
    AmbiguousMessageId,
    CourierError,
    ImapClient,
    PermanentError,
    TransientError,
)

POSITIONAL = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD = inspect.Parameter.KEYWORD_ONLY
REQUIRED = inspect.Parameter.empty


def _shape(func: Any) -> List[Tuple[str, Any, Any]]:
    return [
        (p.name, p.kind, p.default) for p in inspect.signature(func).parameters.values()
    ]


def test_search_signature():
    assert _shape(courier.search) == [
        ("config", POSITIONAL, REQUIRED),
        ("query", POSITIONAL, REQUIRED),
        ("blocks", KEYWORD, None),
        ("folder", KEYWORD, None),
        ("limit", KEYWORD, 10),
        ("no_cache", KEYWORD, False),
        ("world_as_of", KEYWORD, None),
    ]


def test_imap_client_takes_the_bound_unbounded_by_default():
    assert _shape(ImapClient.__init__) == [
        ("self", POSITIONAL, REQUIRED),
        ("block", POSITIONAL, REQUIRED),
        ("local_cache", POSITIONAL, None),
        ("world_as_of", POSITIONAL, None),
    ]


def test_fetch_by_message_id_signature():
    assert _shape(ImapClient.fetch_by_message_id) == [
        ("self", POSITIONAL, REQUIRED),
        ("message_id", POSITIONAL, REQUIRED),
        ("folders", POSITIONAL, None),
        ("no_cache", KEYWORD, False),
    ]


def test_ambiguous_message_id_is_its_own_courier_error():
    assert issubclass(AmbiguousMessageId, CourierError)
    assert not issubclass(AmbiguousMessageId, (PermanentError, TransientError))
    exc = AmbiguousMessageId("a@b", [("INBOX", 1), ("Archive", 7)])
    assert exc.matches == [("INBOX", 1), ("Archive", 7)]


def test_search_and_the_lookup_and_bound_errors_are_in_all():
    assert {
        "search",
        "AmbiguousMessageId",
        "WorldAsOfInvalid",
        "WorldBoundRefused",
    } <= set(courier.__all__)
