"""mu/Xapian emitter: AST to a fully parenthesized mu query string.

mu's native precedence is conventional (NOT binds tighter than AND,
AND tighter than OR), the opposite of the grammar's adjacent-binding
``or``, so the emitter writes fully parenthesized boolean structure
with explicit ``AND``/``OR``/``NOT`` keywords and never bare
juxtaposition of mixed operators: ``from:alice AND (subject:invoice
OR from:bob)``. Verified live against a real mu 1.12.14 index: the
unparenthesized form returns the from-bob message the adjacent-binding
contract excludes.

Date upper bounds: mu's ``date:..X`` runs to the end of day X, while
IMAP ``BEFORE`` and Gmail ``before:`` exclude day X, so ``before:``
and ``older:`` emit the prior day as the upper bound (verified live).
Size bounds get the same inclusive-to-exclusive shift in bytes:
``larger:N`` emits ``size:N+1..`` and ``smaller:N`` emits
``size:..N-1`` because mu ranges are inclusive while LARGER/SMALLER
are strict.

Quoting: any bare word or value that case-insensitively equals
``and``/``or``/``not``/``xor`` or contains one of ``: * / ( ) "`` or
whitespace is double-quoted. An embedded double quote becomes a
space inside the quoted value, and whitespace runs then collapse to
single spaces: probed live, Xapian has no quote escape (a backslash
produces empty-phrase artifacts that silently match nothing), a run
of spaces inside a phrase also matches nothing, and quote characters
are never indexed anyway; they act as token boundaries, which a
single space reproduces exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

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

# Term.op -> mu field name for the directly mapped operators.
_FIELDS = {
    "from": "from",
    "to": "to",
    "cc": "cc",
    "bcc": "bcc",
    "subject": "subject",
    "body": "body",
    "msgid": "msgid",
    "filename": "file",
    "list": "list",
}

# Flag state -> mu fragment. The negative states have no dedicated mu
# flag, so they emit a pre-parenthesized NOT.
_FLAG_FRAGMENTS = {
    Flag.READ: "flag:seen",
    Flag.UNREAD: "flag:unread",
    Flag.FLAGGED: "flag:flagged",
    Flag.UNFLAGGED: "(NOT flag:flagged)",
    Flag.ANSWERED: "flag:replied",
    Flag.UNANSWERED: "(NOT flag:replied)",
}

# Words equal to these (case-insensitively) must be quoted or Xapian
# reads them as operators.
_XAPIAN_KEYWORDS = frozenset({"and", "or", "not", "xor"})

# Characters that force quoting so the value is not read as a field
# reference, wildcard, or group.
_QUOTE_TRIGGERS = set(':*/()"')

_NOTE_DATES = "the local cache evaluates dates in local time"
_NOTE_QUOTE = (
    "the local index never contains quote characters (they are token "
    "boundaries), so an embedded double quote matches as a space"
)


@dataclass(frozen=True)
class MuEmission:
    """One mu emission: the query string and the report.

    Attributes:
        query: The mu query string for ``mu find``. Empty means match
            all (paired with ``--maxnum`` and date sorting by the
            caller, as before).
        report: The translation report for ``provenance.query``.
    """

    query: str
    report: TranslationReport


def _refuse(
    operator: str, message: str, suggestion: str = ""
) -> UntranslatableForBackend:
    """Build this backend's refusal for one operator.

    Args:
        operator: The operator as written, colon included.
        message: Why the mu backend cannot express it.
        suggestion: The nearest alternative, or empty.

    Returns:
        The exception for the caller to raise.
    """
    return UntranslatableForBackend("mu", operator, message, suggestion)


def _mu_date(d: date) -> str:
    """Format a date as YYYYMMDD for mu's date: predicate.

    Args:
        d: The calendar date.

    Returns:
        The eight-digit date string.
    """
    return d.strftime("%Y%m%d")


class _Renderer:
    """One rendering walk: reference instant plus gathered notes.

    Attributes:
        now: Reference instant for relative and keyword terms.
        notes: Approximation notes gathered on the walk.
    """

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.notes: list[str] = []

    def _note(self, note: str) -> None:
        """Record an approximation note once, in first-seen order.

        Args:
            note: The approximation note text.
        """
        if note not in self.notes:
            self.notes.append(note)

    def _clean_quotes(self, value: str) -> str:
        """Replace embedded double quotes and renormalise whitespace.

        Probed live: a run of spaces inside a mu phrase silently
        matches nothing, so after quote replacement whitespace runs
        collapse to single spaces. The original tokens sit at adjacent
        index positions (quote characters are never indexed), so the
        collapsed phrase matches them exactly.

        Args:
            value: The text as parsed.

        Returns:
            The value with quotes replaced and whitespace collapsed.

        Raises:
            UntranslatableForBackend: When nothing searchable remains
                (the value was quote characters and whitespace only).
        """
        if '"' in value:
            self._note(_NOTE_QUOTE)
            value = " ".join(value.replace('"', " ").split())
            if not value:
                raise _refuse(
                    '"',
                    "quote characters are token boundaries in the local "
                    "index, so a value of only quotes matches nothing",
                    "Search for the text around the quote instead.",
                )
        return value

    def _quote(self, value: str) -> str:
        """Quote a word or field value for mu when it needs it.

        Args:
            value: The text as parsed.

        Returns:
            The value, double-quoted when it is a Xapian keyword or
            contains whitespace or special characters; embedded double
            quotes become spaces (see the module docstring).
        """
        value = self._clean_quotes(value)
        if (
            value.lower() in _XAPIAN_KEYWORDS
            or any(c.isspace() for c in value)
            or any(c in _QUOTE_TRIGGERS for c in value)
        ):
            return f'"{value}"'
        return value

    def render(self, node: Node) -> str:
        """Render a node as a mu query fragment without outer parens.

        Operands of ``AND``/``OR``/``NOT`` are parenthesized whenever
        they are compound, so the emitted string never relies on mu's
        own precedence.

        Args:
            node: The AST node.

        Returns:
            The mu query fragment.

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        if isinstance(node, And):
            return " AND ".join(self._operand(child) for child in node.children)
        if isinstance(node, Or):
            return " OR ".join(self._operand(child) for child in node.children)
        if isinstance(node, Not):
            return f"NOT {self._operand(node.child)}"
        return self._term_fragment(node)

    def _operand(self, node: Node) -> str:
        """Render a node as one operand, parenthesizing compounds.

        Args:
            node: The AST node standing as a boolean operand.

        Returns:
            The fragment, wrapped in parens unless it is a leaf term
            (compound leaf expansions arrive pre-parenthesized).
        """
        if isinstance(node, Term):
            return self._term_fragment(node)
        return f"({self.render(node)})"

    def _term_fragment(self, term: Term) -> str:
        """Render one leaf term as a mu query fragment.

        Args:
            term: The leaf term.

        Returns:
            The mu query fragment.

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        op = term.op
        if op in (OP_WORD, OP_PHRASE):
            assert isinstance(term.value, str)
            if op == OP_PHRASE:
                return f'"{self._clean_quotes(term.value)}"'
            return self._quote(term.value)
        if op == OP_KEYWORD:
            assert isinstance(term.value, str)
            return self._keyword_fragment(term.value)
        if op in _FIELDS:
            assert isinstance(term.value, str)
            return f"{_FIELDS[op]}:{self._quote(term.value)}"
        if op == "is":
            assert isinstance(term.value, Flag)
            return _FLAG_FRAGMENTS[term.value]
        if op == "after":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return f"date:{_mu_date(term.value)}.."
        if op == "before":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            try:
                prior = term.value - timedelta(days=1)
            except OverflowError:
                raise _refuse(
                    "before:",
                    "the day before this date is outside the representable range",
                    "Bound the search with on: or after: instead.",
                ) from None
            return f"date:..{_mu_date(prior)}"
        if op == "on":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            day = _mu_date(term.value)
            return f"date:{day}..{day}"
        if op in ("newer", "older"):
            assert isinstance(term.value, timedelta)
            self._note(_NOTE_DATES)
            boundary = (self.now - term.value).date()
            if op == "newer":
                return f"date:{_mu_date(boundary)}.."
            try:
                upper = boundary - timedelta(days=1)
            except OverflowError:
                raise _refuse(
                    "older:",
                    "the resolved date is outside the representable range",
                    "",
                ) from None
            return f"date:..{_mu_date(upper)}"
        if op == "larger":
            assert isinstance(term.value, int)
            return f"size:{term.value + 1}.."
        if op == "smaller":
            assert isinstance(term.value, int)
            return f"size:..{max(term.value - 1, 0)}"
        if op == "has":
            assert isinstance(term.value, str)
            if term.value.lower() == "attachment":
                return "flag:attach"
            raise _refuse(
                f"has:{term.value}",
                f"only Gmail understands has:{term.value}",
                "Run this query on a Gmail account.",
            )
        if op == "deliveredto":
            raise _refuse(
                "deliveredto:",
                "the local cache does not index the Delivered-To header",
                "Search the remote server instead.",
            )
        if op == "label":
            raise _refuse(
                "label:",
                "the cache's own label: field searches X-Label tags, which "
                "are not Gmail labels",
                "Scope the search with in:FOLDER instead.",
            )
        if op == "category":
            raise _refuse(
                "category:",
                "inbox categories exist only on Gmail",
                "Run this query on a Gmail account.",
            )
        if op == "in":
            raise _refuse(
                "in:",
                "in: scopes which folders are searched, so it must stand as "
                "a top-level condition, not under or/not",
                "Move in: to the top level of the query.",
            )
        if op == OP_IMAP:
            raise _refuse(
                "imap:",
                "raw IMAP expressions cannot run against the local cache",
                "The raw query runs on the remote IMAP search path instead.",
            )
        raise _refuse(f"{op}:", "the operator has no local-cache mapping")

    def _keyword_fragment(self, keyword: str) -> str:
        """Render a standalone keyword against the reference instant.

        ``all`` never reaches here: the emitter maps it to the empty
        match-all query before rendering.

        Args:
            keyword: The keyword, already lowercased by the parser.

        Returns:
            The mu date fragment.
        """
        today = self.now.date()
        self._note(_NOTE_DATES)
        if keyword == "today":
            return f"date:{_mu_date(today)}.."
        if keyword == "yesterday":
            yesterday = _mu_date(today - timedelta(days=1))
            return f"date:{yesterday}..{yesterday}"
        if keyword == "week":
            return f"date:{_mu_date(today - timedelta(days=7))}.."
        return f"date:{_mu_date(today - timedelta(days=30))}.."


def emit(parsed: ParseResult, *, now: datetime) -> MuEmission:
    """Emit a parsed query as a mu query string.

    Args:
        parsed: The parse result; its ``treated_as_text`` tokens are
            copied into the report.
        now: Reference instant for relative and keyword terms; a
            bounded caller passes its WORLD_AS_OF instant so replays
            do not drift.

    Returns:
        The emission: the mu query string (empty means match all) and
        the translation report.

    Raises:
        UntranslatableForBackend: When some operator cannot be
            expressed on the local cache; the message names the
            nearest alternative.
    """
    report = TranslationReport(
        dialect="mu", treated_as_text=list(parsed.treated_as_text)
    )
    root = parsed.ast
    if isinstance(root, Term) and root.op == OP_KEYWORD and root.value == "all":
        return MuEmission("", report)
    renderer = _Renderer(now)
    query = renderer.render(root)
    report.approximations = list(renderer.notes)
    return MuEmission(query, report)
