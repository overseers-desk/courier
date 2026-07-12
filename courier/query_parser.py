"""Gmail-style query parser for IMAP search and mu CLI emission.

Parses queries like ``from:alice subject:invoice is:unread after:2025-03-01``
into imapclient-compatible search criteria, or into a mu CLI query string
for the optional local-cache search backend.

Example shapes:

    from:alice subject:invoice      prefix:value terms, implicitly AND-ed
    is:unread after:2025-03-01      flag and date operators
    imap:OR TEXT foo SUBJECT bar    raw IMAP passthrough

The full operator inventory is defined once in ``_OPERATOR_TABLE`` and
rendered by :func:`render_operator_help`; that table is the authoritative
list from which the CLI and MCP help surfaces derive, so they cannot drift
from what the parser actually accepts.
"""

import logging
import re
import shlex
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Union

logger = logging.getLogger(__name__)


class UntranslatableQuery(Exception):
    """Raised when a query cannot be translated to a backend's syntax.

    The ``reason`` attribute carries a short tag matching the
    ``fell_back_reason`` vocabulary used by the local-cache backend.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or f"Query is not translatable: {reason}")


# Prefixes that map directly to IMAP search keys (prefix → IMAP key).
_PREFIX_MAP = {
    "from": "FROM",
    "to": "TO",
    "cc": "CC",
    "subject": "SUBJECT",
    "body": "BODY",
}

# Absolute-date prefixes (prefix → IMAP date key). The IMAP key also picks
# the mu bound side: SINCE → lower bound, BEFORE → upper bound, ON → both.
_DATE_PREFIX_MAP = {
    "after": "SINCE",
    "before": "BEFORE",
    "on": "ON",
}

# Relative-date prefixes (prefix → IMAP date key), resolved against "now".
_RELATIVE_PREFIX_MAP = {
    "newer": "SINCE",
    "newer_than": "SINCE",
    "older": "BEFORE",
    "older_than": "BEFORE",
}

# Prefixes with dedicated branch logic rather than a plain key mapping.
_SPECIAL_PREFIXES = frozenset({"is", "imap", "msgid", "rfc822msgid"})

# is:keyword → IMAP flag string.
_IS_MAP = {
    "unread": "UNSEEN",
    "read": "SEEN",
    "flagged": "FLAGGED",
    "starred": "FLAGGED",
    "unflagged": "UNFLAGGED",
    "unstarred": "UNFLAGGED",
    "answered": "ANSWERED",
    "unanswered": "UNANSWERED",
}

# Single-word queries that have special meaning.
_STANDALONE_KEYWORDS = {
    "all": lambda: "ALL",
    "today": lambda: ["SINCE", date.today()],
    "yesterday": lambda: [
        "SINCE",
        (datetime.now() - timedelta(days=1)).date(),
        "BEFORE",
        date.today(),
    ],
    "week": lambda: ["SINCE", (datetime.now() - timedelta(days=7)).date()],
    "month": lambda: ["SINCE", (datetime.now() - timedelta(days=30)).date()],
}

_RELATIVE_DATE_RE = re.compile(r"^(\d+)([dwm])$")


def _known_prefixes() -> Set[str]:
    """Return every prefix token the parser recognises.

    The union of the direct IMAP prefix map, the absolute and relative
    date prefix maps, and the special-cased prefixes (``is``, ``imap``,
    and the two message-id spellings). This is the reference set the guard
    tests use to prove the documented inventory neither omits a real prefix
    nor invents one the parser does not accept.

    Returns:
        The set of lowercase prefix tokens, without the trailing colon.
    """
    return (
        set(_PREFIX_MAP)
        | set(_DATE_PREFIX_MAP)
        | set(_RELATIVE_PREFIX_MAP)
        | set(_SPECIAL_PREFIXES)
    )


# The single authoritative inventory of query operators. Each row documents
# one operator family; ``prefixes`` names the parser prefix keys the row
# covers (empty for non-prefix forms) so the guard tests can prove the table
# and the parser stay in lockstep. The ``is:`` keyword list and the
# standalone-keyword syntax derive from the parser's own maps so even those
# cannot drift. Constraint: no ``[`` or ``]`` anywhere in these strings,
# because the Typer app renders help through rich markup, which eats
# square brackets.
_OPERATOR_TABLE: List[Dict[str, Any]] = [
    {
        "syntax": "from:ADDR",
        "meaning": "Sender contains ADDR",
        "example": "from:alice",
        "prefixes": ("from",),
    },
    {
        "syntax": "to:ADDR",
        "meaning": "Recipient contains ADDR",
        "example": "to:bob",
        "prefixes": ("to",),
    },
    {
        "syntax": "cc:ADDR",
        "meaning": "Cc contains ADDR",
        "example": "cc:team",
        "prefixes": ("cc",),
    },
    {
        "syntax": "subject:TEXT",
        "meaning": "Subject contains TEXT",
        "example": "subject:invoice",
        "prefixes": ("subject",),
    },
    {
        "syntax": "body:TEXT",
        "meaning": "Body contains TEXT",
        "example": "body:hello",
        "prefixes": ("body",),
    },
    {
        "syntax": "is:KEYWORD",
        "meaning": "Match a flag; KEYWORD is one of: " + ", ".join(sorted(_IS_MAP)),
        "example": "is:unread",
        "prefixes": ("is",),
    },
    {
        "syntax": "after:DATE before:DATE on:DATE",
        "meaning": "Sent since, before, or on a date (YYYY-MM-DD or YYYY/MM/DD)",
        "example": "after:2025-03-01 before:2025-04-01",
        "prefixes": ("after", "before", "on"),
    },
    {
        "syntax": "newer:Nd|Nw|Nm older:Nd|Nw|Nm",
        "meaning": (
            "Within or beyond the last N days, weeks, or months; "
            "newer_than / older_than are synonyms"
        ),
        "example": "newer:3d older:2w",
        "prefixes": ("newer", "newer_than", "older", "older_than"),
    },
    {
        "syntax": "msgid:ID",
        "meaning": "Match by RFC 5322 Message-ID; rfc822msgid is a synonym",
        "example": "msgid:abc@host",
        "prefixes": ("msgid", "rfc822msgid"),
    },
    {
        "syntax": "imap:EXPR",
        "meaning": "Send EXPR straight through as a raw IMAP SEARCH expression",
        "example": "imap:OR TEXT foo SUBJECT bar",
        "prefixes": ("imap",),
    },
    {
        "syntax": "WORDS",
        "meaning": "Tokens with no prefix search the full message text",
        "example": "meeting notes",
        "prefixes": (),
    },
    {
        "syntax": " ".join(_STANDALONE_KEYWORDS),
        "meaning": "A one-word query mapping to a preset date range or match-all",
        "example": "today",
        "prefixes": (),
    },
    {
        "syntax": "or / not / -",
        "meaning": "Combine or negate terms; adjacent terms are AND-ed",
        "example": "from:alice or not is:read",
        "prefixes": (),
    },
]


def render_operator_help() -> str:
    """Render the operator inventory as aligned help text.

    Walks :data:`_OPERATOR_TABLE` once, padding the syntax column to a
    common width so the meanings line up. This is the single rendering
    shared by the CLI ``--help`` and the MCP search tool, so both track the
    parser automatically.

    Returns:
        A multi-line string: a header line followed by one line per
        operator family, each as ``syntax  meaning  (e.g. example)``.
    """
    lines = ["Gmail-style search operators:"]
    width = max(len(str(row["syntax"])) for row in _OPERATOR_TABLE)
    for row in _OPERATOR_TABLE:
        syntax = str(row["syntax"]).ljust(width)
        lines.append(f"  {syntax}  {row['meaning']}  (e.g. {row['example']})")
    return "\n".join(lines)


def parse_query(query: str) -> Union[str, List]:
    """Parse a Gmail-style query string into imapclient-compatible criteria.

    Args:
        query: The search query. Examples:
            ``"from:alice subject:invoice"``
            ``"is:unread after:2025-03-01"``
            ``"meeting notes"`` (bare words → TEXT search)
            ``"imap:OR TEXT foo SUBJECT bar"`` (raw IMAP passthrough)

    Returns:
        A string (e.g. ``"ALL"``, ``"UNSEEN"``) or a list
        (e.g. ``["FROM", "alice", "SUBJECT", "invoice"]``) suitable for
        ``imapclient.IMAPClient.search()``.

    Raises:
        ValueError: On malformed queries (dangling ``or``/``not``, bad dates,
            unknown ``is:`` keywords).
    """
    stripped = query.strip()
    if not stripped:
        return "ALL"

    # imap: escape hatch — pass through raw IMAP expression.
    if stripped.lower().startswith("imap:"):
        return _parse_raw_imap(stripped[5:])

    # Standalone keyword (entire query is one word).
    if stripped.lower() in _STANDALONE_KEYWORDS:
        result = _STANDALONE_KEYWORDS[stripped.lower()]()
        if isinstance(result, str):
            return result
        return list(result)

    tokens = _tokenize(stripped)
    return _build_criteria(tokens)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _parse_raw_imap(raw: str) -> Union[str, List]:
    """Tokenize a raw IMAP search expression."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        logger.warning("shlex failed on raw IMAP query, falling back to split")
        tokens = raw.split()
    if len(tokens) == 1:
        return tokens[0]
    return tokens


def _tokenize(query: str) -> List[str]:
    """Split query respecting quotes, preserving prefix:value as one token."""
    try:
        return shlex.split(query)
    except ValueError:
        logger.warning("shlex failed, falling back to simple split")
        return query.split()


def _parse_date(value: str) -> date:
    """Parse YYYY-MM-DD or YYYY/MM/DD into a date object."""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {value!r}. Use YYYY-MM-DD or YYYY/MM/DD.")


def _parse_relative_date(value: str) -> date:
    """Parse relative offset like 3d, 2w, 1m into a date."""
    m = _RELATIVE_DATE_RE.match(value)
    if not m:
        raise ValueError(
            f"Invalid relative date: {value!r}. Use <number><d|w|m> (e.g. 3d, 2w, 1m)."
        )
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        delta = timedelta(days=n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    else:  # "m"
        delta = timedelta(days=n * 30)
    return (datetime.now() - delta).date()


def _normalize_msgid(value: str) -> str:
    """Normalize a Message-ID value to its bare form.

    Strips surrounding whitespace and one enclosing ``<...>`` pair.  The
    single bare form serves all three query dialects: IMAP
    ``HEADER Message-ID`` (a substring match), mu (which indexes bare
    ids), and Gmail's ``rfc822msgid:`` (which also takes bare ids).

    Args:
        value: The raw Message-ID as written in the query, e.g.
            ``"<abc@host>"`` or ``"abc@host"``.

    Returns:
        The Message-ID with whitespace and a single surrounding angle-bracket
        pair removed, e.g. ``"abc@host"``.
    """
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return value


def _expand_term(token: str) -> List:
    """Expand a single token into its IMAP criteria components.

    Returns a list of IMAP criteria items.  For flag-only results
    (e.g. ``is:unread`` → ``UNSEEN``) returns a single-element list.
    """
    # Negation with dash prefix: -from:alice
    if token.startswith("-") and ":" in token[1:]:
        inner = _expand_term(token[1:])
        return ["NOT"] + inner

    if ":" not in token:
        # Bare word — will be collected by the caller.
        return []

    prefix, value = token.split(":", 1)
    prefix_lower = prefix.lower()

    # Direct prefix mapping (from, to, cc, subject, body).
    if prefix_lower in _PREFIX_MAP:
        return [_PREFIX_MAP[prefix_lower], value]

    # is: flag keywords.
    if prefix_lower == "is":
        val_lower = value.lower()
        if val_lower not in _IS_MAP:
            raise ValueError(
                f"Unknown is: keyword: {value!r}. "
                f"Valid: {', '.join(sorted(_IS_MAP))}"
            )
        return [_IS_MAP[val_lower]]

    # Date operators (absolute), membership-driven off _DATE_PREFIX_MAP.
    if prefix_lower in _DATE_PREFIX_MAP:
        return [_DATE_PREFIX_MAP[prefix_lower], _parse_date(value)]

    # Relative date operators, keyed off _RELATIVE_PREFIX_MAP.
    if prefix_lower in _RELATIVE_PREFIX_MAP:
        return [_RELATIVE_PREFIX_MAP[prefix_lower], _parse_relative_date(value)]

    # Message-ID lookup (both spellings) → IMAP HEADER substring match.
    if prefix_lower in ("msgid", "rfc822msgid"):
        return ["HEADER", "Message-ID", _normalize_msgid(value)]

    # Unknown prefix — treat the whole token as a bare word.
    return []


def _build_criteria(tokens: List[str]) -> Union[str, List]:
    """Walk tokens and assemble a flat IMAP criteria list.

    Handles ``or``, ``not``, prefix:value terms, and bare words.
    """
    # Phase 1: classify each token into a "clause" (an IMAP criteria fragment).
    clauses: List[Union[str, List]] = []  # each entry is "OR" or a list of IMAP items
    bare_words: List[str] = []
    i = 0

    def _flush_bare_words() -> None:
        if bare_words:
            clauses.append(["TEXT", " ".join(bare_words)])
            bare_words.clear()

    while i < len(tokens):
        tok = tokens[i]
        tok_lower = tok.lower()

        if tok_lower == "or":
            _flush_bare_words()
            clauses.append("OR")
            i += 1
            continue

        if tok_lower == "not":
            _flush_bare_words()
            # Next token must exist.
            if i + 1 >= len(tokens):
                raise ValueError("'not' at end of query with nothing to negate.")
            next_tok = tokens[i + 1]
            expanded = _expand_term(next_tok)
            if not expanded:
                # Bare word after not.
                clauses.append(["NOT", "TEXT", next_tok])
            else:
                clauses.append(["NOT"] + expanded)
            i += 2
            continue

        # Regular token.
        expanded = _expand_term(tok)
        if expanded:
            _flush_bare_words()
            clauses.append(expanded)
        else:
            # Bare word (or unknown prefix treated as bare word).
            bare_words.append(tok)
        i += 1

    _flush_bare_words()

    if not clauses:
        return "ALL"

    # Phase 2: resolve OR operators.
    # OR binds two adjacent clauses in Polish notation: OR <left> <right>.
    # Chained ORs right-associate: a or b or c → OR a OR b c.
    result = _resolve_or(clauses)

    # Flatten single-element results.
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], str):
        return result[0]
    return result


def _resolve_or(clauses: List) -> Union[str, List]:
    """Resolve OR markers in the clause list into IMAP Polish notation."""
    # Validate: OR must not be first, last, or consecutive.
    if not clauses:
        return "ALL"

    if clauses[0] == "OR":
        raise ValueError("Query cannot start with 'or'.")
    if clauses[-1] == "OR":
        raise ValueError("'or' at end of query with no right operand.")
    for j in range(len(clauses) - 1):
        if clauses[j] == "OR" and clauses[j + 1] == "OR":
            raise ValueError("Consecutive 'or' operators.")

    # Split into groups separated by OR.
    groups: List[List] = [[]]
    for c in clauses:
        if c == "OR":
            groups.append([])
        else:
            groups[-1].append(c)

    if len(groups) == 1:
        # No OR — just flatten all clauses.
        return _flatten(groups[0])

    # Right-associate: OR g[0] OR g[1] ... g[n]
    # Start from the right.
    right = _flatten(groups[-1])
    for g in reversed(groups[:-1]):
        left = _flatten(g)
        right = ["OR"] + left + right
    return right


def _flatten(clause_list: List[List]) -> List:
    """Flatten a list of clause lists into a single IMAP criteria list."""
    result = []
    for clause in clause_list:
        if isinstance(clause, list):
            result.extend(clause)
        else:
            result.append(clause)
    return result


# ------------------------------------------------------------------
# Mu CLI emitter
# ------------------------------------------------------------------

# Prefixes that map directly to mu CLI search keys (prefix → mu key).
# The keys happen to be identical to mu's own vocabulary; kept as a map
# for symmetry with _PREFIX_MAP and to make divergence (if any) explicit.
_MU_PREFIX_MAP = {
    "from": "from",
    "to": "to",
    "cc": "cc",
    "subject": "subject",
    "body": "body",
}

# is:keyword → mu CLI fragment.  Negative forms (unflagged/unstarred/
# unanswered) become explicit ``NOT flag:X`` rather than a dedicated
# negative flag, since mu does not expose negative flag terms directly.
_MU_IS_MAP = {
    "unread": "flag:unread",
    "read": "flag:seen",
    "flagged": "flag:flagged",
    "starred": "flag:flagged",
    "unflagged": "NOT flag:flagged",
    "unstarred": "NOT flag:flagged",
    "answered": "flag:replied",
    "unanswered": "NOT flag:replied",
}


def parse_query_to_mu(query: str) -> str:
    """Translate a Gmail-style query into a mu CLI query string.

    Args:
        query: Same syntax accepted by :func:`parse_query`.

    Returns:
        A mu CLI query string suitable for ``mu find``.  The empty
        string represents "match all" (used together with ``--maxnum``
        and ``--sort-field=date`` to fetch the most recent N messages).

    Raises:
        UntranslatableQuery: When the query uses constructs (e.g. the
            ``imap:`` raw escape, or any token with the ``imap:``
            prefix) that mu's CLI cannot express.
        ValueError: On malformed queries (dangling ``or``/``not``,
            bad dates, unknown ``is:`` keywords).
    """
    stripped = query.strip()
    if not stripped:
        return ""

    if stripped.lower().startswith("imap:"):
        raise UntranslatableQuery("untranslatable")

    if stripped.lower() in _STANDALONE_KEYWORDS:
        return _standalone_keyword_to_mu(stripped.lower())

    tokens = _tokenize(stripped)
    return _build_mu_query(tokens)


def _mu_date(d: date) -> str:
    """Format a date as YYYYMMDD for mu's date: predicate."""
    return d.strftime("%Y%m%d")


def _mu_date_bound(imap_key: str, d: date) -> str:
    """Emit a mu ``date:`` predicate for an IMAP date key.

    The same key that the absolute and relative date maps assign to a
    prefix chooses which side of the mu range is bound: ``SINCE`` opens a
    lower bound (``date:X..``), ``BEFORE`` opens an upper bound
    (``date:..X``), and ``ON`` closes a single-day range (``date:X..X``).

    Args:
        imap_key: The IMAP date key (``SINCE``, ``BEFORE``, or ``ON``).
        d: The resolved calendar date.

    Returns:
        A mu ``date:`` query fragment.

    Raises:
        ValueError: If ``imap_key`` is not a recognised date key.
    """
    ds = _mu_date(d)
    if imap_key == "SINCE":
        return f"date:{ds}.."
    if imap_key == "BEFORE":
        return f"date:..{ds}"
    if imap_key == "ON":
        return f"date:{ds}..{ds}"
    raise ValueError(f"Unhandled IMAP date key: {imap_key!r}")


def _standalone_keyword_to_mu(keyword: str) -> str:
    """Translate a standalone keyword (today/yesterday/...) to mu syntax."""
    today = date.today()
    if keyword == "all":
        return ""
    if keyword == "today":
        return f"date:{_mu_date(today)}.."
    if keyword == "yesterday":
        y = today - timedelta(days=1)
        return f"date:{_mu_date(y)}..{_mu_date(y)}"
    if keyword == "week":
        return f"date:{_mu_date(today - timedelta(days=7))}.."
    if keyword == "month":
        return f"date:{_mu_date(today - timedelta(days=30))}.."
    raise ValueError(f"Unhandled standalone keyword: {keyword!r}")


def _quote_mu(value: str) -> str:
    """Quote a mu query value when it contains whitespace.

    mu's CLI uses Xapian's query parser, which treats double-quoted
    strings as phrase searches.  Values without whitespace are emitted
    bare so the resulting fragment matches mu's own conventions.
    """
    if any(c.isspace() for c in value):
        return f'"{value}"'
    return value


def _expand_term_mu(token: str) -> Optional[str]:
    """Translate a single token into a mu query fragment.

    Returns:
        A mu query fragment, or ``None`` if the token is a bare word
        that should be collected by the caller.

    Raises:
        UntranslatableQuery: On any token whose prefix is ``imap:``.
        ValueError: On a malformed date or unknown ``is:`` keyword.
    """
    # Negation with dash prefix: -from:alice
    if token.startswith("-") and ":" in token[1:]:
        inner = _expand_term_mu(token[1:])
        if inner is None:
            return None
        return f"NOT {inner}"

    if ":" not in token:
        return None

    prefix, value = token.split(":", 1)
    prefix_lower = prefix.lower()

    # imap:-prefixed tokens cannot be translated; surface to caller so
    # the local-cache backend can fall back to IMAP.
    if prefix_lower == "imap":
        raise UntranslatableQuery("untranslatable")

    if prefix_lower in _MU_PREFIX_MAP:
        return f"{_MU_PREFIX_MAP[prefix_lower]}:{_quote_mu(value)}"

    if prefix_lower == "is":
        val_lower = value.lower()
        if val_lower not in _MU_IS_MAP:
            raise ValueError(
                f"Unknown is: keyword: {value!r}. "
                f"Valid: {', '.join(sorted(_MU_IS_MAP))}"
            )
        return _MU_IS_MAP[val_lower]

    # Date operators (absolute). The same map that names the IMAP key picks
    # the mu bound side (SINCE → lower, BEFORE → upper, ON → both).
    if prefix_lower in _DATE_PREFIX_MAP:
        return _mu_date_bound(_DATE_PREFIX_MAP[prefix_lower], _parse_date(value))

    # Relative date operators, keyed off the same map as the IMAP side.
    if prefix_lower in _RELATIVE_PREFIX_MAP:
        return _mu_date_bound(
            _RELATIVE_PREFIX_MAP[prefix_lower], _parse_relative_date(value)
        )

    # Message-ID lookup (both spellings) → mu's exact-match msgid field.
    # msgids carry no whitespace, so the bare value needs no quoting.
    if prefix_lower in ("msgid", "rfc822msgid"):
        return f"msgid:{_normalize_msgid(value)}"

    # Unknown prefix — treat the whole token as a bare word, matching
    # _expand_term's permissive behaviour.
    return None


def _build_mu_query(tokens: List[str]) -> str:
    """Walk tokens and assemble a mu CLI query string.

    AND-binding between adjacent terms is implicit (Xapian's default
    operator).  ``or`` becomes ``OR``; ``not`` and ``-prefix:`` become
    ``NOT``.  Bare words are emitted as default-field search tokens.
    """
    fragments: List[str] = []  # each entry is a fragment string or "OR"
    bare_words: List[str] = []
    i = 0

    def _flush_bare_words() -> None:
        if bare_words:
            joined = " ".join(_quote_mu(w) for w in bare_words)
            fragments.append(joined)
            bare_words.clear()

    while i < len(tokens):
        tok = tokens[i]
        tok_lower = tok.lower()

        if tok_lower == "or":
            _flush_bare_words()
            fragments.append("OR")
            i += 1
            continue

        if tok_lower == "not":
            _flush_bare_words()
            if i + 1 >= len(tokens):
                raise ValueError("'not' at end of query with nothing to negate.")
            next_tok = tokens[i + 1]
            expanded = _expand_term_mu(next_tok)
            if expanded is None:
                fragments.append(f"NOT {_quote_mu(next_tok)}")
            else:
                fragments.append(f"NOT {expanded}")
            i += 2
            continue

        expanded = _expand_term_mu(tok)
        if expanded is not None:
            _flush_bare_words()
            fragments.append(expanded)
        else:
            bare_words.append(tok)
        i += 1

    _flush_bare_words()

    if not fragments:
        return ""

    if fragments[0] == "OR":
        raise ValueError("Query cannot start with 'or'.")
    if fragments[-1] == "OR":
        raise ValueError("'or' at end of query with no right operand.")
    for j in range(len(fragments) - 1):
        if fragments[j] == "OR" and fragments[j + 1] == "OR":
            raise ValueError("Consecutive 'or' operators.")

    return " ".join(fragments)
