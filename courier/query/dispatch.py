"""Dispatch decisions for the search path: scope, capability, phrasing.

The dispatcher's pure decisions live here so the I/O layer
(``ImapClient``) only executes them:

- **Scope extraction**: top-level ``in:`` conjuncts (positive and
  negated) leave the parsed tree and become a folder :class:`Scope`.
  Both the generic-IMAP and mu emitters refuse ``in:`` unconditionally,
  so the stripped tree is what they receive; the Gmail emitter speaks
  ``in:`` natively and receives the original tree. An ``in:`` anywhere
  other than a top-level conjunct refuses for every backend — scope is
  a property of the whole search, not of a branch.
- **Capability gates**: the remote sub-dispatch picks the Gmail emitter
  on ``X-GM-EXT-1`` and offers RFC 5032 ``WITHIN`` to the generic
  emitter, from the server's CAPABILITY response, never from hostname
  substrings.
- **Fallback phrasing**: when every backend refuses, the terminal error
  names each decline in the ``fell_back_reason`` vocabulary, phrased
  for the caller's next action (a stale cache says "refresh it", never
  "enable a local mail cache").
"""

from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from courier.query.ast import (
    OP_KEYWORD,
    And,
    Node,
    Not,
    Or,
    Term,
    UntranslatableForBackend,
)
from courier.query.grammar import ParseResult, QuerySyntaxError


class Scope(NamedTuple):
    """The folder scope extracted from a query's top-level in: conjuncts.

    Attributes:
        include: ``in:`` values in query order; well-known names stay
            as written (matched case-insensitively later), literal
            folder names keep their case.
        exclude: ``-in:`` values, subtracted from the searched set.
        anywhere: Whether ``in:anywhere`` lifted every folder
            restriction.
    """

    include: Tuple[str, ...]
    exclude: Tuple[str, ...]
    anywhere: bool


# SPECIAL-USE roles for the well-known in: values that need server
# resolution. "inbox" is not here: INBOX is a literal RFC 3501 name.
SPECIAL_USE_FOR_IN: Dict[str, bytes] = {
    "sent": b"\\Sent",
    "spam": b"\\Junk",
    "trash": b"\\Trash",
}

# fell_back_reason tag -> phrase for the terminal refusal message. Each
# phrase names the caller's next action; the vocabulary is the closed
# set used by provenance.fell_back_reason.
_FALLBACK_PHRASES: Dict[str, str] = {
    "no_cache": "the local cache was bypassed for this call (no_cache)",
    "mu_missing": "a local cache is configured but the mu binary is not on PATH",
    "db_missing": (
        "a local cache is configured but its index database is missing; "
        "build it with mu index"
    ),
    "stale": (
        "your local cache exists but its index is stale; refresh the "
        "maildir sync and re-run mu index"
    ),
    "untranslatable": "the local cache could not express this query",
    "mu_no_matches": "the local cache answered with mu's ambiguous no-matches exit",
    "maildir_not_indexed": (
        "the block's maildir lies outside the mu store root, so the "
        "local cache does not index it"
    ),
    "folder_not_synced": (
        "the requested folder is not present in the locally synced "
        "maildir, so only the server can answer for it"
    ),
    "folder_not_allowed": (
        "the requested folder is outside this block's allowed_folders " "whitelist"
    ),
    "exception": "the local cache failed at runtime",
}

_IN_PLACEMENT_MESSAGE = (
    "in: scopes which folders are searched, so it must stand on its own "
    "at the top level of the query; it cannot sit under or, under a "
    "doubled negation, or inside groups."
)


def _refuse_nested_in(node: Node) -> None:
    """Refuse any in: term below the top-level conjunct positions.

    Args:
        node: A top-level conjunct that is not itself an ``in:`` term
            or its direct negation.

    Raises:
        QuerySyntaxError: When some descendant is an ``in:`` term.
    """
    if isinstance(node, Term):
        if node.op == "in":
            raise QuerySyntaxError(_IN_PLACEMENT_MESSAGE)
        return
    if isinstance(node, Not):
        _refuse_nested_in(node.child)
        return
    if isinstance(node, (And, Or)):
        for child in node.children:
            _refuse_nested_in(child)


def extract_scope(parsed: ParseResult) -> Tuple[ParseResult, Scope]:
    """Split a parsed query into its folder scope and the rest.

    Top-level ``in:`` conjuncts (and their direct negations) leave the
    tree; what remains is the query the imap/mu emitters receive. When
    every conjunct was scope, the remainder is the match-all keyword.

    Args:
        parsed: The parse result for the whole query.

    Returns:
        ``(remaining, scope)``: the query without its scope conjuncts,
        and the extracted :class:`Scope`.

    Raises:
        QuerySyntaxError: When an ``in:`` term sits under ``or``,
            nested negation, or a group, or when ``in:anywhere`` is
            negated.
    """
    root = parsed.ast
    conjuncts = list(root.children) if isinstance(root, And) else [root]
    include: List[str] = []
    exclude: List[str] = []
    anywhere = False
    remaining: List[Node] = []
    for conjunct in conjuncts:
        if isinstance(conjunct, Term) and conjunct.op == "in":
            value = str(conjunct.value)
            if value.lower() == "anywhere":
                anywhere = True
            else:
                include.append(value)
            continue
        if (
            isinstance(conjunct, Not)
            and isinstance(conjunct.child, Term)
            and conjunct.child.op == "in"
        ):
            value = str(conjunct.child.value)
            if value.lower() == "anywhere":
                raise QuerySyntaxError(
                    "in:anywhere cannot be negated; name the folders to "
                    "exclude with -in:FOLDER instead."
                )
            exclude.append(value)
            continue
        _refuse_nested_in(conjunct)
        remaining.append(conjunct)
    scope = Scope(tuple(include), tuple(exclude), anywhere)
    rest: Node
    if not remaining:
        rest = Term(OP_KEYWORD, "all")
    elif len(remaining) == 1:
        rest = remaining[0]
    else:
        rest = And(tuple(remaining))
    return ParseResult(rest, parsed.treated_as_text), scope


def ensure_no_folder_conflict(folder: Optional[str], scope: Scope) -> None:
    """Refuse a search steered by both a folder argument and in: scope.

    Choosing silently between the two would make one of them a no-op;
    the loud error names the choice instead.

    Args:
        folder: The caller's folder argument, or ``None``.
        scope: The scope extracted from the query.

    Raises:
        QuerySyntaxError: When both are present.
    """
    if folder is not None and (scope.include or scope.exclude or scope.anywhere):
        raise QuerySyntaxError(
            "The search names folders twice: give the scope either as in: "
            "in the query or as the folder argument, not both."
        )


def cache_folder_for_scope(
    scope: Scope, folder: Optional[str]
) -> Tuple[bool, Optional[str]]:
    """Decide whether the mu cache can serve a scope, and as what folder.

    The cache's scope predicate takes one exact folder or the whole
    block. ``in:sent``/``in:spam``/``in:trash`` need the server's
    SPECIAL-USE resolution, which the cache cannot do, so they decline
    to the remote backend; so do multiple includes and any exclude.

    Args:
        scope: The extracted scope.
        folder: The caller's folder argument (only set when the scope
            is empty; :func:`ensure_no_folder_conflict` rejects the
            combination).

    Returns:
        ``(serveable, folder)``: whether the cache can express the
        scope, and the folder to pass it (``None`` for the whole
        block).
    """
    if scope.exclude:
        return False, None
    if scope.anywhere:
        return True, None
    if not scope.include:
        return True, folder
    if len(scope.include) > 1:
        return False, None
    value = scope.include[0]
    lowered = value.lower()
    if lowered == "inbox":
        return True, "INBOX"
    if lowered in SPECIAL_USE_FOR_IN:
        return False, None
    return True, value


def gmail_capable(capabilities: Iterable[str]) -> bool:
    """Report whether the server advertises Gmail's X-GM-EXT-1.

    Args:
        capabilities: The CAPABILITY strings, any case.

    Returns:
        True when the Gmail extension is advertised, which routes the
        remote search through the Gmail emitter.
    """
    return any(str(cap).upper() == "X-GM-EXT-1" for cap in capabilities)


def supports_within(capabilities: Iterable[str]) -> bool:
    """Report whether the server advertises RFC 5032 WITHIN.

    Args:
        capabilities: The CAPABILITY strings, any case.

    Returns:
        True when server-side YOUNGER/OLDER is available.
    """
    return any(str(cap).upper() == "WITHIN" for cap in capabilities)


def describe_fallbacks(fallbacks: List[Dict[str, str]]) -> str:
    """Phrase backend declines for the terminal refusal message.

    Args:
        fallbacks: ``{"backend": ..., "reason": ...}`` records in the
            order the backends declined.

    Returns:
        One semicolon-joined clause per decline, each naming the
        backend and the caller's next action.
    """
    parts = []
    for record in fallbacks:
        reason = record.get("reason", "")
        phrase = _FALLBACK_PHRASES.get(reason, reason)
        parts.append(f"{record.get('backend', 'backend')}: {phrase}")
    return "; ".join(parts)


def compose_backend_error(
    exc: UntranslatableForBackend, fallbacks: List[Dict[str, str]]
) -> str:
    """Build the terminal error text when the last backend refuses.

    Args:
        exc: The refusal from the last backend tried.
        fallbacks: The earlier declines, eligibility included.

    Returns:
        The refusal message, with every earlier decline appended so
        the caller sees why no backend served the query.
    """
    text = str(exc)
    if fallbacks:
        text = f"{text} Backends declined earlier — {describe_fallbacks(fallbacks)}."
    return text
