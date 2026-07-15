"""Tokenizer and recursive-descent parser for the search query grammar.

Grammar (juxtaposition is AND; ``or`` binds only the terms beside it;
``-`` and ``not`` bind tightest)::

    query        := and_seq
    and_seq      := or_group+
    or_group     := unary ( OR unary )*
    unary        := ('-' | NOT) unary | atom
    atom         := '(' and_seq ')'
                  | '{' or_group+ '}'
                  | prefix ':' value
                  | prefix ':' '(' value_seq ')'
                  | PHRASE
                  | WORD
    value_seq    := value+ | value (OR value)+
    value        := WORD | PHRASE

A leading ``imap:`` bypasses the grammar entirely: the rest of the
query is carried verbatim as the raw expression of a single ``imap``
term, and an ``imap:`` token anywhere else is a parse error. The
standalone keywords (``all``, ``today``, ``yesterday``, ``week``,
``month``) apply only when the keyword is the entire query.

Every syntax problem raises :class:`QuerySyntaxError` before any I/O
happens. No operator-looking token ever degrades to literal search
text: registry-known prefixes parse, near-misses refuse with the
correction, and only genuinely word-shaped tokens (URLs, clock times)
stay words, recorded in :attr:`ParseResult.treated_as_text`. An
unbalanced quote is a parse error too; the old parser's shlex fallback
kept the quote as literal text and searched for it.

Single quotes are accepted like double quotes, per the documented CLI
behaviour: ``'phrase words'`` is a phrase and ``from:'Alice Smith'`` a
quoted operator value. Unlike double quotes, a single quote is only
structural at a token start or directly after ``prefix:``, so
apostrophes inside words (``don't``, ``students'``) stay literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum, auto
from typing import Optional

from courier.query import registry
from courier.query.ast import (
    OP_IMAP,
    OP_KEYWORD,
    OP_PHRASE,
    OP_WORD,
    And,
    Node,
    Not,
    Or,
    Term,
)
from courier.query.registry import Operator, ValueKind

# Relative-offset grammar for newer:/older:, e.g. 3d, 2w, 1m.
_RELATIVE_RE = re.compile(r"^(\d+)([dwm])$")

# Size grammar for larger:/smaller:: an integer with an optional k/m/g
# unit and an optional trailing b, case-insensitive.
_SIZE_RE = re.compile(r"^(\d+)([kmg]?)b?$", re.IGNORECASE)

# Binary (1024-based) unit factors for the size grammar.
_SIZE_UNIT_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

# Characters that end a bare run and stand as structural tokens.
_STRUCTURAL = '(){}"'


class QuerySyntaxError(ValueError):
    """Raised when a query cannot be parsed.

    Subclasses :class:`ValueError` so callers that guarded the old
    parser's errors keep working.

    Attributes:
        suggestions: Near-miss operator corrections, without colons
            (``("from",)`` for the token ``form:alice``); empty for
            errors that carry no correction.
    """

    def __init__(self, message: str, suggestions: tuple[str, ...] = ()) -> None:
        self.suggestions = suggestions
        super().__init__(message)


@dataclass(frozen=True)
class ParseResult:
    """The outcome of parsing one query string.

    Attributes:
        ast: Root node of the parsed query.
        treated_as_text: Tokens that contain a colon but matched no
            operator and no near-miss, kept as literal words (URLs and
            the like), in query order. The dispatcher copies these into
            the :class:`~courier.query.ast.TranslationReport`.
    """

    ast: Node
    treated_as_text: tuple[str, ...] = ()


class _TokenKind(Enum):
    """Token categories the tokenizer emits."""

    LPAREN = auto()
    RPAREN = auto()
    LBRACE = auto()
    RBRACE = auto()
    OR = auto()
    NOT = auto()
    NEG = auto()
    PHRASE = auto()
    RUN = auto()
    PRE_QUOTED = auto()
    PRE_GROUP = auto()


@dataclass(frozen=True)
class _Token:
    """One token: its kind plus the text fields the kind uses.

    Attributes:
        kind: The token category.
        text: Run text, phrase content, or a quoted prefix value.
        prefix: The prefix spelling for PRE_QUOTED and PRE_GROUP tokens.
    """

    kind: _TokenKind
    text: str = ""
    prefix: str = ""


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read a quoted string starting at ``text[start]``.

    The character at ``start`` (a double or single quote) is the
    delimiter; the other quote character is literal inside it.
    Backslash escapes the delimiter and the backslash itself; any
    other backslash pair is kept literally.

    Args:
        text: The whole query string.
        start: Index of the opening quote.

    Returns:
        A tuple of the unquoted value and the index one past the
        closing quote.

    Raises:
        QuerySyntaxError: When the closing quote is missing.
    """
    quote = text[start]
    out: list[str] = []
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in ("\\", quote):
            out.append(text[i + 1])
            i += 2
            continue
        if c == quote:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    kind = "double" if quote == '"' else "single"
    raise QuerySyntaxError(
        f"Unbalanced {kind} quote in query: {text[start:]!r} has no closing "
        "quote. Close it, or drop the quote character."
    )


def _tokenize(text: str) -> list[_Token]:
    """Split a query string into grammar tokens.

    Parens, braces, and double quotes are structural everywhere outside
    a quoted string; a value containing them must be quoted. A single
    quote is structural only at a token start or directly after
    ``prefix:``, so apostrophes inside words stay literal. A dash is
    the negation prefix only at a token start directly against the next
    token; standalone it is a word, and inside a word it stays a
    hyphen. A run ending in a colon glues to a directly following
    quoted string or group, which is how ``from:"Alice Smith"`` and
    ``subject:(a b)`` tokenize; whitespace after the colon never joins.

    Args:
        text: The stripped query string.

    Returns:
        The token list, in query order.

    Raises:
        QuerySyntaxError: On an unbalanced double quote.
    """
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(_Token(_TokenKind.LPAREN))
            i += 1
            continue
        if c == ")":
            tokens.append(_Token(_TokenKind.RPAREN))
            i += 1
            continue
        if c == "{":
            tokens.append(_Token(_TokenKind.LBRACE))
            i += 1
            continue
        if c == "}":
            tokens.append(_Token(_TokenKind.RBRACE))
            i += 1
            continue
        if c in "\"'":
            value, i = _read_quoted(text, i)
            tokens.append(_Token(_TokenKind.PHRASE, text=value))
            continue
        if c == "-":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt and not nxt.isspace() and nxt not in ")}":
                tokens.append(_Token(_TokenKind.NEG))
            else:
                tokens.append(_Token(_TokenKind.RUN, text="-"))
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in _STRUCTURAL:
            # A single quote ends the run only right after prefix:, so
            # from:'Alice Smith' glues while don't stays one word.
            if text[j] == "'" and text[j - 1] == ":":
                break
            j += 1
        run = text[i:j]
        if j < n and run.endswith(":"):
            if text[j] in "\"'":
                value, j = _read_quoted(text, j)
                tokens.append(
                    _Token(_TokenKind.PRE_QUOTED, text=value, prefix=run[:-1])
                )
                i = j
                continue
            if text[j] == "(":
                tokens.append(_Token(_TokenKind.PRE_GROUP, prefix=run[:-1]))
                i = j + 1
                continue
        lowered = run.lower()
        if lowered == "or":
            tokens.append(_Token(_TokenKind.OR))
        elif lowered == "not":
            tokens.append(_Token(_TokenKind.NOT))
        else:
            tokens.append(_Token(_TokenKind.RUN, text=run))
        i = j
    return tokens


def _make_and(children: list[Node]) -> Node:
    """Build a conjunction, flattening nested Ands and unwrapping one.

    Flattening is safe because AND is associative; the emitters see one
    n-ary node per juxtaposition run.

    Args:
        children: The nodes to conjoin, at least one.

    Returns:
        The single child, or an :class:`And` over the flattened list.
    """
    flat: list[Node] = []
    for child in children:
        if isinstance(child, And):
            flat.extend(child.children)
        else:
            flat.append(child)
    if len(flat) == 1:
        return flat[0]
    return And(tuple(flat))


def _make_or(children: list[Node]) -> Node:
    """Build a disjunction, flattening nested Ors and unwrapping one.

    Args:
        children: The alternative nodes, at least one.

    Returns:
        The single child, or an :class:`Or` over the flattened list.
    """
    flat: list[Node] = []
    for child in children:
        if isinstance(child, Or):
            flat.extend(child.children)
        else:
            flat.append(child)
    if len(flat) == 1:
        return flat[0]
    return Or(tuple(flat))


def _parse_date(value: str) -> date:
    """Parse YYYY-MM-DD or YYYY/MM/DD into a date object.

    Args:
        value: The date as written after the prefix.

    Returns:
        The parsed calendar date.

    Raises:
        QuerySyntaxError: When ``value`` matches neither format.
    """
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise QuerySyntaxError(
        f"Invalid date format: {value!r}. Use YYYY-MM-DD or YYYY/MM/DD."
    )


def _parse_delta(value: str) -> timedelta:
    """Parse a relative offset like 3d, 2w, 1m into a timedelta.

    The offset stays a duration on the term; emitters resolve it
    against the caller's reference instant, so a backend that can
    express the duration natively (RFC 5032 WITHIN) still can. A month
    is 30 days, matching the old parser.

    Args:
        value: The offset as written after the prefix, e.g. ``"3d"``.

    Returns:
        The parsed duration.

    Raises:
        QuerySyntaxError: When ``value`` does not match the offset
            grammar.
    """
    m = _RELATIVE_RE.match(value)
    if not m:
        raise QuerySyntaxError(
            f"Invalid relative date: {value!r}. Use <number><d|w|m> "
            "(e.g. 3d, 2w, 1m)."
        )
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    return timedelta(days=n * 30)


def _parse_size(value: str) -> int:
    """Parse a size like 1M, 500k, or 1048576 into a byte count.

    The grammar is an integer with an optional k/m/g unit and an
    optional trailing b, case-insensitive. Units are binary
    (1024-based); a bare number is bytes. All backends filter on the
    one computed byte count.

    Args:
        value: The size token as written after the prefix.

    Returns:
        The size in bytes.

    Raises:
        QuerySyntaxError: When ``value`` does not match the size
            grammar.
    """
    m = _SIZE_RE.match(value.strip())
    if not m:
        raise QuerySyntaxError(
            f"Invalid size: {value!r}. Use a number with an optional "
            "k/m/g unit (1024-based), e.g. 1M, 500k, 1048576."
        )
    return int(m.group(1)) * _SIZE_UNIT_FACTORS[m.group(2).lower()]


def _normalize_msgid(value: str) -> str:
    """Normalize a Message-ID value to its bare form.

    Strips surrounding whitespace and one enclosing ``<...>`` pair. The
    single bare form serves all three query dialects; each emitter adds
    its own decoration from this AST value, never from the raw query
    string.

    Args:
        value: The raw Message-ID as written in the query, e.g.
            ``"<abc@host>"`` or ``"abc@host"``.

    Returns:
        The Message-ID with whitespace and a single surrounding
        angle-bracket pair removed, e.g. ``"abc@host"``.
    """
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return value


def _imap_placement_error() -> QuerySyntaxError:
    """Build the error for an imap: token that does not lead the query."""
    return QuerySyntaxError(
        "imap: must lead the query; the raw passthrough cannot appear "
        "inside a larger query."
    )


class _Parser:
    """Recursive-descent parser over the token list.

    Holds the cursor and the treated-as-text record so the grammar
    productions read as plain methods.

    Attributes:
        tokens: The token list from :func:`_tokenize`.
        pos: Index of the next unconsumed token.
        treated_as_text: Colon-bearing tokens kept as literal words,
            in query order.
    """

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.treated_as_text: list[str] = []

    def _peek(self) -> Optional[_Token]:
        """Return the next token without consuming it, or None at end."""
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _next(self) -> Optional[_Token]:
        """Consume and return the next token, or None at end."""
        token = self._peek()
        if token is not None:
            self.pos += 1
        return token

    def parse(self) -> Node:
        """Parse the whole token list as one query.

        Returns:
            The AST root.

        Raises:
            QuerySyntaxError: On any malformed query.
        """
        return self._parse_and_seq(closer=None)

    def _parse_and_seq(self, closer: Optional[_TokenKind]) -> Node:
        """Parse or_group+ up to the closer (or end of query).

        Args:
            closer: The token kind that ends the sequence: RPAREN
                inside a paren group, None at the top level.

        Returns:
            The single group, or an :class:`And` over the groups.

        Raises:
            QuerySyntaxError: On a missing closing paren or an empty
                group.
        """
        groups: list[Node] = []
        while True:
            token = self._peek()
            if token is None:
                if closer is not None:
                    raise QuerySyntaxError("Missing closing ')' in query.")
                break
            if closer is not None and token.kind is closer:
                break
            groups.append(self._parse_or_group())
        if not groups:
            raise QuerySyntaxError("Empty () group is not allowed.")
        return _make_and(groups)

    def _parse_or_group(self) -> Node:
        """Parse unary (OR unary)*: 'or' binds only adjacent terms.

        Returns:
            The single operand, or an n-ary :class:`Or`.

        Raises:
            QuerySyntaxError: On a dangling or doubled ``or``.
        """
        items = [self._parse_unary()]
        while (token := self._peek()) is not None and token.kind is _TokenKind.OR:
            self._next()
            nxt = self._peek()
            if nxt is None or nxt.kind in (_TokenKind.RPAREN, _TokenKind.RBRACE):
                raise QuerySyntaxError("'or' has no right operand.")
            if nxt.kind is _TokenKind.OR:
                raise QuerySyntaxError("Consecutive 'or' operators.")
            items.append(self._parse_unary())
        if len(items) == 1:
            return items[0]
        return _make_or(items)

    def _parse_unary(self) -> Node:
        """Parse ('-' | NOT) unary | atom.

        Returns:
            The parsed node, wrapped in :class:`Not` per negation.

        Raises:
            QuerySyntaxError: On a negation with nothing to negate, or
                an ``or`` with no left operand.
        """
        token = self._peek()
        if token is None:
            raise QuerySyntaxError("Expected a search term.")
        if token.kind is _TokenKind.OR:
            if self.pos == 0:
                raise QuerySyntaxError("Query cannot start with 'or'.")
            raise QuerySyntaxError("'or' has no term on its left.")
        if token.kind is _TokenKind.NOT:
            self._next()
            if self._peek() is None:
                raise QuerySyntaxError("'not' at end of query with nothing to negate.")
            return Not(self._parse_unary())
        if token.kind is _TokenKind.NEG:
            self._next()
            if self._peek() is None:
                raise QuerySyntaxError("'-' at end of query with nothing to negate.")
            return Not(self._parse_unary())
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        """Parse one atom: group, brace group, phrase, or term.

        Returns:
            The parsed node.

        Raises:
            QuerySyntaxError: On stray closers, empty phrases, and the
                per-form errors raised by the helpers.
        """
        token = self._next()
        if token is None:  # callers peek first; kept for type narrowing
            raise QuerySyntaxError("Expected a search term.")
        if token.kind is _TokenKind.LPAREN:
            node = self._parse_and_seq(closer=_TokenKind.RPAREN)
            self._next()
            return node
        if token.kind is _TokenKind.LBRACE:
            return self._parse_brace_group()
        if token.kind is _TokenKind.PHRASE:
            if not token.text:
                raise QuerySyntaxError("Empty quoted phrase in query.")
            return Term(OP_PHRASE, token.text)
        if token.kind is _TokenKind.RUN:
            return self._classify_run(token.text)
        if token.kind is _TokenKind.PRE_QUOTED:
            return self._classify_prefixed(token.prefix, token.text)
        if token.kind is _TokenKind.PRE_GROUP:
            return self._parse_value_group(token.prefix)
        if token.kind is _TokenKind.RPAREN:
            raise QuerySyntaxError("Unexpected ')' in query.")
        raise QuerySyntaxError("Unexpected '}' in query.")

    def _parse_brace_group(self) -> Node:
        """Parse a Gmail brace group: OR of its contents.

        Returns:
            The single member, or an :class:`Or` over the members.

        Raises:
            QuerySyntaxError: On an empty or unclosed brace group.
        """
        items: list[Node] = []
        while True:
            token = self._peek()
            if token is None:
                raise QuerySyntaxError("Missing closing '}' in query.")
            if token.kind is _TokenKind.RBRACE:
                self._next()
                break
            items.append(self._parse_or_group())
        if not items:
            raise QuerySyntaxError("Empty {} group is not allowed.")
        return _make_or(items)

    def _classify_run(self, text: str) -> Term:
        """Classify a bare run: word, operator term, or refusal.

        Args:
            text: The run text as written.

        Returns:
            A word term or a typed operator term.

        Raises:
            QuerySyntaxError: On a misplaced ``imap:``, an empty
                operator value, or a near-miss prefix.
        """
        if ":" not in text:
            return Term(OP_WORD, text)
        prefix, _, value = text.partition(":")
        lowered = prefix.lower()
        if lowered == "imap":
            raise _imap_placement_error()
        row = registry.operator_for_prefix(lowered)
        if row is not None:
            if not value:
                raise self._empty_value_error(lowered)
            return self._typed_term(row, value)
        self._refuse_near_miss(prefix)
        self.treated_as_text.append(text)
        return Term(OP_WORD, text)

    def _classify_prefixed(self, prefix: str, value: str) -> Term:
        """Classify a prefix with a directly attached quoted value.

        The attached quote makes the token operator-shaped, so an
        unknown prefix refuses here instead of degrading to text.

        Args:
            prefix: The prefix spelling before the colon.
            value: The unquoted value text.

        Returns:
            A typed operator term.

        Raises:
            QuerySyntaxError: On a misplaced ``imap:``, an unknown or
                near-miss prefix, or an empty value.
        """
        row = self._require_operator(prefix)
        if not value:
            raise self._empty_value_error(prefix.lower())
        return self._typed_term(row, value)

    def _require_operator(self, prefix: str) -> Operator:
        """Resolve a prefix that must be an operator, refusing loudly.

        Args:
            prefix: The prefix spelling before the colon.

        Returns:
            The registry row.

        Raises:
            QuerySyntaxError: On ``imap:``, a near-miss, or an unknown
                prefix.
        """
        lowered = prefix.lower()
        if lowered == "imap":
            raise _imap_placement_error()
        row = registry.operator_for_prefix(lowered)
        if row is None:
            self._refuse_near_miss(prefix)
            raise QuerySyntaxError(
                f"Unknown search operator '{prefix}:'. Quote the whole token "
                "to search it as literal text."
            )
        return row

    def _refuse_near_miss(self, prefix: str) -> None:
        """Raise the correction error when the prefix is one edit away.

        Args:
            prefix: The unrecognised prefix as written.

        Raises:
            QuerySyntaxError: When the registry holds a spelling within
                one edit; carries the suggestions.
        """
        suggestions = registry.suggest_prefixes(prefix)
        if suggestions:
            pretty = " or ".join(f"'{s}:'" for s in suggestions)
            raise QuerySyntaxError(
                f"Unknown search operator '{prefix}:'. Did you mean {pretty}?",
                suggestions=suggestions,
            )

    def _empty_value_error(self, prefix: str) -> QuerySyntaxError:
        """Build the empty-value error, naming the operator.

        Args:
            prefix: The lowercase prefix spelling.

        Returns:
            The error to raise; values never join across whitespace,
            so ``from: alice`` gets this same error.
        """
        return QuerySyntaxError(
            f"The {prefix}: operator needs a value; write {prefix}:VALUE "
            "with no space after the colon."
        )

    def _parse_value_group(self, prefix: str) -> Node:
        """Parse prefix:(value_seq): the prefix distributes over values.

        Spaces between values mean AND, ``or`` between every value
        means OR; the two must not mix in one group.

        Args:
            prefix: The prefix spelling before the colon.

        Returns:
            A single term, or the prefix distributed over an
            :class:`And` or :class:`Or` of terms.

        Raises:
            QuerySyntaxError: On unknown prefixes, nesting, negation,
                mixed joiners, empty groups, or a missing ')'.
        """
        row = self._require_operator(prefix)
        lowered = prefix.lower()
        # Values interleaved with None markers standing for 'or'.
        items: list[Optional[str]] = []
        while True:
            token = self._next()
            if token is None:
                raise QuerySyntaxError(
                    f"Missing closing ')' in the {lowered}:(...) value group."
                )
            if token.kind is _TokenKind.RPAREN:
                break
            if token.kind is _TokenKind.OR:
                items.append(None)
            elif token.kind is _TokenKind.PHRASE:
                items.append(token.text)
            elif token.kind is _TokenKind.RUN:
                self._check_group_value(lowered, token.text)
                items.append(token.text)
            elif token.kind in (_TokenKind.NEG, _TokenKind.NOT):
                raise QuerySyntaxError(
                    f"Negation is not allowed inside the {lowered}:(...) value "
                    "group; quote the word to search it literally."
                )
            elif token.kind in (_TokenKind.PRE_QUOTED, _TokenKind.PRE_GROUP):
                raise QuerySyntaxError(
                    f"A prefix operator cannot nest inside the {lowered}:(...) "
                    "value group."
                )
            else:
                raise QuerySyntaxError(
                    f"Only words, quoted phrases, and 'or' may appear inside "
                    f"the {lowered}:(...) value group."
                )
        values = [item for item in items if item is not None]
        if not values:
            raise QuerySyntaxError(f"Empty value group after {lowered}:.")
        joined_by_or = any(item is None for item in items)
        if joined_by_or:
            alternating = len(items) == 2 * len(values) - 1 and all(
                (item is None) == (index % 2 == 1) for index, item in enumerate(items)
            )
            if not alternating:
                raise QuerySyntaxError(
                    f"In the {lowered}:(...) value group use spaces between "
                    "all values or 'or' between all values, not a mix."
                )
        terms = [self._typed_term(row, value) for value in values]
        if len(terms) == 1:
            return terms[0]
        if joined_by_or:
            return _make_or(list(terms))
        return _make_and(list(terms))

    def _check_group_value(self, group_prefix: str, text: str) -> None:
        """Reject operator-shaped tokens inside a value group.

        Plain colon words (``re:``) stay values; registry-known and
        near-miss prefixes are the nesting the grammar forbids.

        Args:
            group_prefix: The lowercase prefix that opened the group.
            text: The candidate value text.

        Raises:
            QuerySyntaxError: When the value nests a prefix operator.
        """
        if ":" not in text:
            return
        inner, _, _ = text.partition(":")
        lowered = inner.lower()
        if lowered == "imap" or registry.operator_for_prefix(lowered) is not None:
            raise QuerySyntaxError(
                f"A prefix operator cannot nest inside the {group_prefix}:(...) "
                "value group."
            )
        self._refuse_near_miss(inner)

    def _typed_term(self, row: Operator, raw: str) -> Term:
        """Build a term, typing the raw value per the registry row.

        Args:
            row: The operator's registry row.
            raw: The value text as written (quotes already removed).

        Returns:
            The typed term carrying the row's canonical op.

        Raises:
            QuerySyntaxError: On a malformed date, offset, or size, or
                an unknown ``is:`` keyword.
        """
        if row.kind is ValueKind.TEXT:
            return Term(row.op, raw)
        if row.kind is ValueKind.DATE:
            return Term(row.op, _parse_date(raw))
        if row.kind is ValueKind.DELTA:
            return Term(row.op, _parse_delta(raw))
        if row.kind is ValueKind.SIZE:
            return Term(row.op, _parse_size(raw))
        if row.kind is ValueKind.MSGID:
            return Term(row.op, _normalize_msgid(raw))
        if row.kind is ValueKind.FLAG:
            flag = registry.IS_KEYWORDS.get(raw.lower())
            if flag is None:
                raise QuerySyntaxError(
                    f"Unknown is: keyword: {raw!r}. "
                    f"Valid: {', '.join(sorted(registry.IS_KEYWORDS))}"
                )
            return Term(row.op, flag)
        # ValueKind.RAW: leading imap: bypasses the grammar, so a RAW
        # row can only get here misplaced.
        raise _imap_placement_error()


def parse(query: str) -> ParseResult:
    """Parse a Gmail-style query string into an AST.

    Args:
        query: The search query. Examples:
            ``"from:alice subject:invoice"``
            ``"after:2026-07-13 (ticket OR booking)"``
            ``"imap:OR TEXT foo SUBJECT bar"`` (raw IMAP passthrough)

    Returns:
        A :class:`ParseResult` holding the AST root and the tokens kept
        as literal words despite containing a colon. An empty query
        parses as the match-all keyword.

    Raises:
        QuerySyntaxError: On any malformed query: unknown or misspelled
            operators, empty operator values, unbalanced quotes or
            groups, and misplaced ``or``, ``not``, or ``imap:``.
    """
    stripped = query.strip()
    if not stripped:
        return ParseResult(Term(OP_KEYWORD, "all"))
    lowered = stripped.lower()
    if lowered.startswith("imap:"):
        raw = stripped[5:].strip()
        if not raw:
            raise QuerySyntaxError(
                "imap: needs a raw IMAP SEARCH expression after the colon."
            )
        return ParseResult(Term(OP_IMAP, raw))
    if lowered in registry.STANDALONE_KEYWORDS:
        return ParseResult(Term(OP_KEYWORD, lowered))
    parser = _Parser(_tokenize(stripped))
    node = parser.parse()
    return ParseResult(node, tuple(parser.treated_as_text))
