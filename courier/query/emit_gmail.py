"""Gmail emitter: AST to an X-GM-RAW string plus standard keys.

The emitter renders the query in Gmail's own web dialect and returns
imapclient criteria of the shape ``[b"X-GM-RAW", <raw>, *standard]``.
Dialect normalisation happens here, never in the raw query string:
``on:`` becomes an ``after:/before:`` pair, relative dates become
computed absolutes (keeping WORLD_AS_OF replay semantics), ``body:``
becomes a bare word with a report note (Gmail has no ``body:``),
``is:flagged`` becomes ``is:starred``, ``rfc822msgid:`` is built
from the AST value, never rewritten from raw text, and sizes emit in
the K/M unit forms Gmail actually filters on (bare byte counts and G
silently match nothing; probed live).

The ``is:answered``/``is:unanswered`` family has no Gmail spelling, so
those terms emit as the standard ANSWERED/UNANSWERED search keys
AND-ed beside the X-GM-RAW key (X-GM-RAW is one search key among
others). An ``or`` spanning the two families is refused with a
split-the-query suggestion, and so is any nesting that would need
client-side set algebra.

Braces are never emitted: brace nesting is undocumented Gmail, so
``Or`` nodes (including ``Not`` under ``Or``, which the grammar
permits inside braces) canonicalise to explicit, parenthesized ``OR``
groups. Embedded double quotes in values are refused because Gmail
defines no escape for them.

When the caller passes a WORLD_AS_OF bound, the second-precision
``before:<epoch seconds>`` clause is reproduced inside the X-GM-RAW
string (an undocumented Gmail form the pre-translator code already
depended on); coarse Layer-2 post-filtering remains the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Union

from courier.query.ast import (
    OP_IMAP,
    OP_KEYWORD,
    OP_PHRASE,
    OP_WORD,
    And,
    Flag,
    Node,
    Not,
    Or,
    Term,
    TranslationReport,
    UntranslatableForBackend,
)
from courier.query.grammar import ParseResult

# Flag states with a native Gmail spelling.
_FLAG_FRAGMENTS = {
    Flag.READ: "is:read",
    Flag.UNREAD: "is:unread",
    Flag.FLAGGED: "is:starred",
    Flag.UNFLAGGED: "-is:starred",
}

# Flag states that only exist as standard IMAP keys (the hybrid side).
_STANDARD_FLAG_KEYS = {
    Flag.ANSWERED: b"ANSWERED",
    Flag.UNANSWERED: b"UNANSWERED",
}

# Operators whose Gmail spelling is prefix:value verbatim.
_NATIVE_PREFIXES = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "has",
        "filename",
        "list",
        "deliveredto",
        "label",
        "category",
        "in",
    }
)

# Characters that force a value into Gmail quotes.
_VALUE_QUOTE_TRIGGERS = set("(){}")

# Size unit factors Gmail actually understands, largest first. Gmail
# silently matches nothing for bare byte counts and for the G suffix,
# while K and M filter correctly, so emission never goes above M and
# never below K.
_SIZE_UNIT_FACTORS = ((1024**2, "M"), (1024, "K"))

_NOTE_BODY = (
    "Gmail has no body: operator; " "the body: value is searched as ordinary query text"
)
_NOTE_DATES = "Gmail evaluates dates in the account profile's timezone"
_NOTE_RELATIVE = (
    "newer:/older: resolved to absolute after:/before: dates against "
    "the reference instant"
)
_NOTE_BOUND = (
    "results bounded with a second-precision before:<epoch seconds> "
    "clause per WORLD_AS_OF"
)

_SPLIT_SUGGESTION = (
    "Run is:answered / is:unanswered as its own search and combine the "
    "results yourself; Gmail expresses that family only as a standard "
    "IMAP key, which can only be AND-ed beside the rest of the query."
)


@dataclass(frozen=True)
class GmailEmission:
    """One Gmail emission: criteria, charset, and the report.

    Attributes:
        criteria: imapclient criteria: ``[b"X-GM-RAW", <UTF-8 raw>]``
            plus any standard keys AND-ed beside it; pure standard
            queries omit the X-GM-RAW pair.
        charset: ``"UTF-8"`` when the raw string is non-ASCII, else
            ``None``.
        report: The translation report for ``provenance.query``.
    """

    criteria: List[Union[bytes, int]]
    charset: Optional[str]
    report: TranslationReport


def _refuse(
    operator: str, message: str, suggestion: str = ""
) -> UntranslatableForBackend:
    """Build this backend's refusal for one operator.

    Args:
        operator: The operator as written, colon included.
        message: Why the Gmail backend cannot express it.
        suggestion: The nearest alternative, or empty.

    Returns:
        The exception for the caller to raise.
    """
    return UntranslatableForBackend("gmail", operator, message, suggestion)


def _gmail_date(d: date) -> str:
    """Format a date in Gmail's YYYY/MM/DD dialect.

    Args:
        d: The calendar date.

    Returns:
        The date as Gmail's query language writes it.
    """
    return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"


def _check_value(operator: str, value: str) -> None:
    """Refuse values the Gmail dialect cannot carry.

    Args:
        operator: The operator the value belongs to, for the message.
        value: The value text as parsed.

    Raises:
        UntranslatableForBackend: On an embedded double quote (Gmail
            defines no escape for quotes) or control characters.
    """
    if '"' in value:
        raise _refuse(
            operator,
            "Gmail defines no escape " "for a double quote inside a quoted value",
            "Drop the quote character from the search text, or search "
            "another backend.",
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise _refuse(
            operator,
            "the value contains control characters, which cannot be "
            "carried in a Gmail query",
            "Remove the control characters from the search text.",
        )


def _quote_value(operator: str, value: str) -> str:
    """Quote a prefix value for the Gmail dialect when needed.

    Args:
        operator: The operator the value belongs to, for refusals.
        value: The value text as parsed.

    Returns:
        The value, double-quoted when it contains whitespace or
        grouping characters.

    Raises:
        UntranslatableForBackend: When the value cannot be carried.
    """
    _check_value(operator, value)
    if any(c.isspace() for c in value) or any(
        c in _VALUE_QUOTE_TRIGGERS for c in value
    ):
        return f'"{value}"'
    return value


def _contains_standard_flag(node: Node) -> bool:
    """Report whether a subtree contains an answered-family term.

    Args:
        node: The subtree root.

    Returns:
        True when some descendant is ``is:answered``/``is:unanswered``.
    """
    if isinstance(node, Term):
        return node.op == "is" and node.value in _STANDARD_FLAG_KEYS
    if isinstance(node, Not):
        return _contains_standard_flag(node.child)
    if isinstance(node, (And, Or)):
        return any(_contains_standard_flag(child) for child in node.children)
    return False


class _Renderer:
    """One raw-string rendering walk: reference instant plus notes.

    Attributes:
        now: Reference instant for relative and keyword terms.
        notes: Approximation notes gathered on the walk.
    """

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.notes: list[str] = []
        self.neg_depth = 0

    def _note(self, note: str) -> None:
        """Record an approximation note once, in first-seen order.

        Args:
            note: The approximation note text.
        """
        if note not in self.notes:
            self.notes.append(note)

    def render(self, node: Node) -> str:
        """Render a node in the Gmail dialect.

        ``And`` joins with spaces, ``Or`` becomes an explicit
        parenthesized OR group (braces are never emitted), and ``Not``
        becomes a leading dash, parenthesizing compound operands.

        Args:
            node: The AST node.

        Returns:
            The Gmail query fragment.

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        if isinstance(node, And):
            return " ".join(self.render(child) for child in node.children)
        if isinstance(node, Or):
            parts = []
            for child in node.children:
                rendered = self.render(child)
                if isinstance(child, And):
                    rendered = f"({rendered})"
                parts.append(rendered)
            return "(" + " OR ".join(parts) + ")"
        if isinstance(node, Not):
            self.neg_depth += 1
            try:
                rendered = self.render(node.child)
            finally:
                self.neg_depth -= 1
            if isinstance(node.child, Term) and not rendered.startswith("-"):
                return f"-{rendered}"
            return f"-({rendered})"
        return self._term_fragment(node)

    def _term_fragment(self, term: Term) -> str:
        """Render one leaf term in the Gmail dialect.

        Args:
            term: The leaf term.

        Returns:
            The Gmail query fragment.

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        op = term.op
        if op == OP_WORD:
            assert isinstance(term.value, str)
            _check_value(op, term.value)
            if ":" in term.value:
                # Quoting forces literal text, so a URL-shaped word is
                # not read as an operator by Gmail either.
                return f'"{term.value}"'
            return term.value
        if op == OP_PHRASE:
            assert isinstance(term.value, str)
            _check_value(op, term.value)
            return f'"{term.value}"'
        if op == OP_KEYWORD:
            assert isinstance(term.value, str)
            return self._keyword_fragment(term.value)
        if op == "body":
            assert isinstance(term.value, str)
            self._note(_NOTE_BODY)
            _check_value("body:", term.value)
            if any(c.isspace() for c in term.value):
                return f'"{term.value}"'
            return term.value
        if op in _NATIVE_PREFIXES:
            assert isinstance(term.value, str)
            return f"{op}:{_quote_value(f'{op}:', term.value)}"
        if op == "is":
            assert isinstance(term.value, Flag)
            return _FLAG_FRAGMENTS[term.value]
        if op == "after":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return f"after:{_gmail_date(term.value)}"
        if op == "before":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return f"before:{_gmail_date(term.value)}"
        if op == "on":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            try:
                next_day = term.value + timedelta(days=1)
            except OverflowError:
                raise _refuse(
                    "on:",
                    "the day after this date is outside the representable range",
                    "Bound the search with after: instead.",
                ) from None
            return (
                f"(after:{_gmail_date(term.value)} " f"before:{_gmail_date(next_day)})"
            )
        if op in ("newer", "older"):
            assert isinstance(term.value, timedelta)
            self._note(_NOTE_RELATIVE)
            self._note(_NOTE_DATES)
            boundary = (self.now - term.value).date()
            key = "after" if op == "newer" else "before"
            return f"{key}:{_gmail_date(boundary)}"
        if op in ("larger", "smaller"):
            assert isinstance(term.value, int)
            return self._size_fragment(op, term.value)
        if op == "msgid":
            assert isinstance(term.value, str)
            return f"rfc822msgid:{_quote_value('msgid:', term.value)}"
        if op == OP_IMAP:
            raise _refuse(
                "imap:",
                "raw IMAP expressions never travel inside X-GM-RAW",
                "The raw query runs " "on the standard IMAP search path instead.",
            )
        raise _refuse(f"{op}:", "the operator has no Gmail mapping")

    def _size_fragment(self, op: str, nbytes: int) -> str:
        """Render larger:/smaller: in a unit form Gmail understands.

        Gmail silently matches nothing for bare byte counts and for
        the G suffix, while K and M filter correctly. A byte count
        divisible by a working unit emits exactly at the largest such
        unit; any
        other count rounds at K toward the over-matching side — down
        for ``larger:``, up for ``smaller:``, and the direction flips
        under an odd number of negations so the negated whole still
        over-matches — with the shift declared in the report.

        Args:
            op: ``"larger"`` or ``"smaller"``.
            nbytes: The threshold in bytes, from the parsed term.

        Returns:
            The Gmail query fragment, e.g. ``larger:1M``.

        Raises:
            UntranslatableForBackend: When over-match rounding would
                need a threshold under 1K, which Gmail cannot express.
        """
        if nbytes > 0:
            for factor, suffix in _SIZE_UNIT_FACTORS:
                if nbytes % factor == 0:
                    return f"{op}:{nbytes // factor}{suffix}"
        negated = self.neg_depth % 2 == 1
        round_down = (op == "larger") != negated
        units = nbytes // 1024 if round_down else -(-nbytes // 1024)
        if units <= 0:
            raise _refuse(
                f"{op}:",
                "Gmail cannot express a size threshold under 1K (bare "
                "byte counts silently match nothing)",
                "Use a threshold of at least 1K, or search another backend.",
            )
        self._note(
            f"{op}: threshold rounded from {nbytes} to {units * 1024} bytes "
            f"({units}K): Gmail sizes take K/M unit forms only, and the "
            "rounding direction widens the match"
        )
        return f"{op}:{units}K"

    def _keyword_fragment(self, keyword: str) -> str:
        """Render a standalone keyword against the reference instant.

        ``all`` never reaches here: the emitter handles it before
        rendering because it contributes no raw fragment.

        Args:
            keyword: The keyword, already lowercased by the parser.

        Returns:
            The Gmail query fragment.
        """
        today = self.now.date()
        self._note(_NOTE_DATES)
        if keyword == "today":
            return f"after:{_gmail_date(today)}"
        if keyword == "yesterday":
            yesterday = today - timedelta(days=1)
            return f"(after:{_gmail_date(yesterday)} before:{_gmail_date(today)})"
        if keyword == "week":
            return f"after:{_gmail_date(today - timedelta(days=7))}"
        return f"after:{_gmail_date(today - timedelta(days=30))}"


def _standard_key(conjunct: Node) -> Optional[List[bytes]]:
    """Emit a top-level conjunct as standard keys when it is pure.

    Args:
        conjunct: One top-level AND conjunct.

    Returns:
        The standard key (``[b"ANSWERED"]``, ``[b"NOT", b"ANSWERED"]``,
        ...) when the conjunct is an answered-family term or its
        direct negation; ``None`` when the conjunct belongs to the
        Gmail dialect side.

    Raises:
        UntranslatableForBackend: When an answered-family term sits
            under ``or`` or deeper nesting, which would need
            client-side set algebra.
    """
    if isinstance(conjunct, Term) and conjunct.op == "is":
        key = _STANDARD_FLAG_KEYS.get(conjunct.value)  # type: ignore[arg-type]
        if key is not None:
            return [key]
        return None
    if isinstance(conjunct, Not) and isinstance(conjunct.child, Term):
        child = conjunct.child
        if child.op == "is":
            key = _STANDARD_FLAG_KEYS.get(child.value)  # type: ignore[arg-type]
            if key is not None:
                return [b"NOT", key]
        return None
    if _contains_standard_flag(conjunct):
        raise _refuse(
            "is:answered",
            "is:answered/is:unanswered has no Gmail spelling, so it cannot "
            "sit under or/not groups beside Gmail-dialect terms",
            _SPLIT_SUGGESTION,
        )
    return None


def emit(
    parsed: ParseResult,
    *,
    now: datetime,
    world_as_of: Optional[datetime] = None,
) -> GmailEmission:
    """Emit a parsed query as Gmail X-GM-RAW criteria.

    Args:
        parsed: The parse result; its ``treated_as_text`` tokens are
            copied into the report.
        now: Reference instant for relative and keyword terms; a
            bounded caller passes its WORLD_AS_OF instant so replays
            do not drift.
        world_as_of: When set, the second-precision
            ``before:<epoch seconds>`` clause is appended inside the
            X-GM-RAW string.

    Returns:
        The emission: criteria, charset (``"UTF-8"`` only when the
        raw string is non-ASCII), and the translation report.

    Raises:
        UntranslatableForBackend: When some operator cannot be
            expressed on Gmail; the message names the nearest
            alternative.
    """
    report = TranslationReport(
        dialect="gmail", treated_as_text=list(parsed.treated_as_text)
    )
    root = parsed.ast
    if isinstance(root, Term) and root.op == OP_IMAP:
        raise _refuse(
            "imap:",
            "raw IMAP expressions never travel inside X-GM-RAW",
            "The raw query runs on the standard IMAP search path instead.",
        )

    renderer = _Renderer(now)
    conjuncts = list(root.children) if isinstance(root, And) else [root]
    standard: List[bytes] = []
    fragments: List[str] = []
    for conjunct in conjuncts:
        key = _standard_key(conjunct)
        if key is not None:
            standard.extend(key)
            continue
        if isinstance(conjunct, Term) and conjunct.op == OP_KEYWORD:
            if conjunct.value == "all":
                continue
        fragments.append(renderer.render(conjunct))

    if world_as_of is not None:
        fragments.append(f"before:{int(world_as_of.timestamp())}")
        renderer._note(_NOTE_BOUND)

    raw = " ".join(fragments)
    criteria: List[Union[bytes, int]] = []
    if raw:
        criteria.extend([b"X-GM-RAW", raw.encode("utf-8")])
    criteria.extend(standard)
    if not criteria:
        criteria.append(b"ALL")

    report.approximations = list(renderer.notes)
    charset = "UTF-8" if not raw.isascii() else None
    return GmailEmission(criteria, charset, report)
