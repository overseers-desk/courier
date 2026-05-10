"""Per-block redaction policies expressed as Sieve scripts.

A ``[imap.NAME]`` block can carry ``redact = "rules.sieve"``.  The script
uses a ``mailroom-policy`` extension that adds one custom action,
``redact;``: any message whose tests evaluate true is replaced with a
placeholder Email before reaching the agent or the model provider.
Date, UID, folder and threading headers survive; subject, body, and
every party address are blanked.

Sieve was chosen for the rule format so that per-rule actions land
naturally when the prompt-injection follow-up arrives.  The current
subset is constrained to what evaluates safely against a parsed
``Email`` object: ``address``/``header`` tests, ``anyof``/``allof``/``not``
combinators, and the ``:is``/``:contains``/``:matches`` match types.
``body``, ``envelope`` and ``:regex`` are deliberately deferred; each
carries its own privacy or correctness considerations and earns its
slot only when a concrete case calls for it.

Failure mode is closed: any I/O error, sievelib parse failure, or
out-of-subset construct raises ``ValueError`` at config-load
time.  The IMAP client offers no "policy unavailable, falling back to
unfiltered" branch.
"""

from __future__ import annotations

import ast
import logging
from typing import Any, Callable, List

from sievelib.commands import (  # type: ignore[import-untyped]
    ActionCommand,
    add_commands,
)
from sievelib.parser import Parser  # type: ignore[import-untyped]

from mailroom.models import Email

logger = logging.getLogger(__name__)


# Register the custom action exactly once.  sievelib stores commands
# in module-level globals keyed by class name; re-registering is a
# no-op but cheap.
class RedactCommand(ActionCommand):  # noqa: D401  (sievelib API)
    """Custom Sieve action: hide this message from the agent."""

    args_definition: List[Any] = []


add_commands(RedactCommand)


_SUPPORTED_MATCH_TYPES = {":is", ":contains", ":matches"}
_ADDRESS_HEADERS = {"from", "to", "cc", "bcc", "sender", "reply-to"}


def compile_policy(path: str) -> Callable[[Email], bool]:
    """Load a Sieve script and return the evaluator it describes.

    Args:
        path: Filesystem path to the ``.sieve`` file.

    Returns:
        A callable that takes an ``Email`` and returns ``True`` when at
        least one ``if`` block's test evaluates true (so the message
        should be redacted), ``False`` otherwise.

    Raises:
        ValueError: On any I/O failure, sievelib parse failure,
            or use of a Sieve construct outside the supported subset.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as e:
        raise ValueError(f"cannot read sieve file {path!r}: {e}") from e

    parser = Parser()
    if not parser.parse(source):
        raise ValueError(f"sieve parse failed in {path!r}: {parser.error}")

    tests: List[Callable[[Email], bool]] = []
    for node in parser.result:
        cmd = node.name
        if cmd == "require":
            continue  # capabilities are advisory; we accept anything declared
        if cmd != "if":
            raise ValueError(
                f"{path!r}: only `require` and `if` are supported at top level, "
                f"got `{cmd}`"
            )
        if len(node.children) != 1 or node.children[0].name != "redact":
            raise ValueError(
                f"{path!r}: every `if` body must be a single `redact;` action"
            )
        tests.append(_compile_test(node.arguments["test"], path))

    if not tests:
        raise ValueError(
            f"{path!r}: no `if ... {{ redact; }}` rules found. Given sieve is for fending of leakage to AI agents, cowardly refuse to continue."
        )

    def evaluate(email: Email) -> bool:
        return any(t(email) for t in tests)

    return evaluate


def _compile_test(node: Any, path: str) -> Callable[[Email], bool]:
    """Recursively compile a sievelib test node into an Email predicate."""
    name = node.name
    args = node.arguments

    if name == "anyof":
        subs = [_compile_test(s, path) for s in args["tests"]]
        return lambda e: any(s(e) for s in subs)
    if name == "allof":
        subs = [_compile_test(s, path) for s in args["tests"]]
        return lambda e: all(s(e) for s in subs)
    if name == "not":
        sub = _compile_test(args["test"], path)
        return lambda e: not sub(e)

    match_type = args.get("match-type", ":is")
    if match_type not in _SUPPORTED_MATCH_TYPES:
        raise ValueError(
            f"{path!r}: match-type {match_type!r} is not in the supported "
            f"subset {sorted(_SUPPORTED_MATCH_TYPES)}"
        )

    if name == "address":
        headers = _as_list(args["header-list"])
        for h in headers:
            if h.lower() not in _ADDRESS_HEADERS:
                raise ValueError(
                    f"{path!r}: address test on header {h!r} is outside the "
                    f"supported subset {sorted(_ADDRESS_HEADERS)}"
                )
        keys = _as_list(args["key-list"])
        match = _matcher(match_type, keys, path)
        return lambda e: any(
            match(addr) for h in headers for addr in _email_addresses(e, h)
        )
    if name == "header":
        header_names = _as_list(args["header-names"])
        keys = _as_list(args["key-list"])
        match = _matcher(match_type, keys, path)
        return lambda e: any(
            match(value) for h in header_names for value in _email_headers(e, h)
        )

    raise ValueError(
        f"{path!r}: test {name!r} is outside the supported subset "
        f"(address, header, anyof, allof, not)"
    )


def _as_list(token: Any) -> List[str]:
    """Sievelib stores Sieve string and string-list args as source text.

    A single-string ``"foo"`` arrives as the literal ``'"foo"'``; a
    string-list ``["a","b"]`` arrives as a Python list whose elements
    are themselves still-quoted literals (``['"a"', '"b"']``).  Both
    shapes need to round-trip through ``ast.literal_eval`` per element
    to produce plain strings.
    """
    if isinstance(token, list):
        return [str(ast.literal_eval(x)) for x in token]
    parsed = ast.literal_eval(token)
    if isinstance(parsed, str):
        return [parsed]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    raise ValueError(f"unexpected token shape: {token!r}")


def _matcher(match_type: str, keys: List[str], path: str) -> Callable[[str], bool]:
    """Build a matcher callable for one (match-type, key-list) pair."""
    keys_lower = [k.lower() for k in keys]
    if match_type == ":is":
        return lambda v: v.lower() in keys_lower
    if match_type == ":contains":
        return lambda v: any(k in v.lower() for k in keys_lower)
    # ``:matches`` is the Sieve glob match type with ``*`` (any) and
    # ``?`` (one).  Limited here to leading/trailing-glob shapes; an
    # interior glob would translate to a regex search whose semantics
    # need a deliberate decision, so the deferred-features note in the
    # module docstring covers it.
    for k in keys:
        if "*" in k[1:-1] or "?" in k:
            raise ValueError(
                f"{path!r}: :matches pattern {k!r} uses an interior "
                f"glob; only leading/trailing ``*`` is supported"
            )

    def predicate(v: str) -> bool:
        vl = v.lower()
        for k in keys_lower:
            if k.startswith("*") and k.endswith("*"):
                if k.strip("*") in vl:
                    return True
            elif k.startswith("*"):
                if vl.endswith(k[1:]):
                    return True
            elif k.endswith("*"):
                if vl.startswith(k[:-1]):
                    return True
            elif k == vl:
                return True
        return False

    return predicate


def _email_addresses(email: Email, header: str) -> List[str]:
    """Return the address strings on ``email`` for a Sieve address header."""
    h = header.lower()
    if h == "from":
        return [email.from_.address] if email.from_ else []
    if h == "to":
        return [a.address for a in email.to]
    if h == "cc":
        return [a.address for a in email.cc]
    if h == "bcc":
        return [a.address for a in email.bcc]
    # sender / reply-to live in the raw headers map; addresses arrive as
    # full RFC 5322 header values, so a substring/exact match against the
    # bare address is the closest the subset allows.
    raw = email.headers.get(header) or email.headers.get(header.title()) or ""
    return [raw] if raw else []


def _email_headers(email: Email, header: str) -> List[str]:
    """Return header values on ``email`` for a Sieve header test."""
    h = header.lower()
    if h == "subject":
        return [email.subject] if email.subject else []
    # The address-bearing headers are mirrored in the raw ``headers`` dict
    # only for some flows; rebuild them from the structured fields so a
    # ``header`` test covers them uniformly.
    if h == "from" and email.from_:
        return [str(email.from_)]
    if h == "to":
        return [str(a) for a in email.to]
    if h == "cc":
        return [str(a) for a in email.cc]
    raw = email.headers.get(header) or email.headers.get(header.title()) or ""
    return [raw] if raw else []
