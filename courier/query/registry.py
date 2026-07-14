"""Operator registry: the single inventory of search query operators.

Every surface derives from this table: the parser's prefix vocabulary
and per-operator value typing, the rendered operator help shared by the
CLI and MCP surfaces, and the near-miss suggestion vocabulary for
misspelled operators. Guard tests hold the table and the parser in
lockstep so the documented inventory can neither omit a real operator
nor invent one the parser does not accept.

Constraint carried over from the old parser: no square brackets
anywhere in the help strings, because the Typer app renders help
through rich markup, which eats square brackets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from courier.query.ast import OP_KEYWORD, OP_PHRASE, OP_WORD, Flag


class ValueKind(Enum):
    """How the raw text after ``prefix:`` types into a term value."""

    NONE = "none"
    TEXT = "text"
    DATE = "date"
    DELTA = "delta"
    SIZE = "size"
    MSGID = "msgid"
    FLAG = "flag"
    RAW = "raw"


# is:keyword spelling to flag state. Synonym spellings map onto one
# state here, once, so no emitter ever re-normalises them.
IS_KEYWORDS: dict[str, Flag] = {
    "unread": Flag.UNREAD,
    "read": Flag.READ,
    "flagged": Flag.FLAGGED,
    "starred": Flag.FLAGGED,
    "unflagged": Flag.UNFLAGGED,
    "unstarred": Flag.UNFLAGGED,
    "answered": Flag.ANSWERED,
    "unanswered": Flag.UNANSWERED,
}

# Single-word queries with preset meaning. They apply only when the
# keyword is the entire query; anywhere else they are bare words.
STANDALONE_KEYWORDS: tuple[str, ...] = ("all", "today", "yesterday", "week", "month")


@dataclass(frozen=True)
class Operator:
    """One operator family: parser vocabulary plus help columns.

    Attributes:
        op: Canonical ``Term.op`` value for the family. Synonym prefixes
            (``newer_than``, ``rfc822msgid``) canonicalise onto it.
        prefixes: The prefix spellings the parser accepts for this row,
            empty for rows that document non-prefix syntax.
        kind: How the raw value text types into the term value.
        syntax: Help column showing the operator's written form.
        meaning: Help column explaining what the operator matches.
        example: Help column with one working example query fragment.
    """

    op: str
    prefixes: tuple[str, ...]
    kind: ValueKind
    syntax: str
    meaning: str
    example: str


OPERATORS: tuple[Operator, ...] = (
    Operator(
        op="from",
        prefixes=("from",),
        kind=ValueKind.TEXT,
        syntax="from:ADDR",
        meaning="Sender contains ADDR",
        example="from:alice",
    ),
    Operator(
        op="to",
        prefixes=("to",),
        kind=ValueKind.TEXT,
        syntax="to:ADDR",
        meaning="Recipient contains ADDR",
        example="to:bob",
    ),
    Operator(
        op="cc",
        prefixes=("cc",),
        kind=ValueKind.TEXT,
        syntax="cc:ADDR",
        meaning="Cc contains ADDR",
        example="cc:team",
    ),
    Operator(
        op="bcc",
        prefixes=("bcc",),
        kind=ValueKind.TEXT,
        syntax="bcc:ADDR",
        meaning="Bcc contains ADDR; only stored sent copies carry Bcc",
        example="bcc:carol",
    ),
    Operator(
        op="subject",
        prefixes=("subject",),
        kind=ValueKind.TEXT,
        syntax="subject:TEXT",
        meaning="Subject contains TEXT",
        example="subject:invoice",
    ),
    Operator(
        op="body",
        prefixes=("body",),
        kind=ValueKind.TEXT,
        syntax="body:TEXT",
        meaning="Body contains TEXT",
        example="body:hello",
    ),
    Operator(
        op="is",
        prefixes=("is",),
        kind=ValueKind.FLAG,
        syntax="is:KEYWORD",
        meaning="Match a flag; KEYWORD is one of: " + ", ".join(sorted(IS_KEYWORDS)),
        example="is:unread",
    ),
    Operator(
        op="after",
        prefixes=("after",),
        kind=ValueKind.DATE,
        syntax="after:DATE",
        meaning="Sent on or after DATE (YYYY-MM-DD or YYYY/MM/DD)",
        example="after:2025-03-01",
    ),
    Operator(
        op="before",
        prefixes=("before",),
        kind=ValueKind.DATE,
        syntax="before:DATE",
        meaning="Sent before DATE, exclusive",
        example="before:2025-04-01",
    ),
    Operator(
        op="on",
        prefixes=("on",),
        kind=ValueKind.DATE,
        syntax="on:DATE",
        meaning="Sent on DATE",
        example="on:2025-03-15",
    ),
    Operator(
        op="newer",
        prefixes=("newer", "newer_than"),
        kind=ValueKind.DELTA,
        syntax="newer:Nd|Nw|Nm",
        meaning="Within the last N days, weeks, or months; newer_than is a synonym",
        example="newer:3d",
    ),
    Operator(
        op="older",
        prefixes=("older", "older_than"),
        kind=ValueKind.DELTA,
        syntax="older:Nd|Nw|Nm",
        meaning="Beyond the last N days, weeks, or months; older_than is a synonym",
        example="older:2w",
    ),
    Operator(
        op="msgid",
        prefixes=("msgid", "rfc822msgid"),
        kind=ValueKind.MSGID,
        syntax="msgid:ID",
        meaning="Match by RFC 5322 Message-ID; rfc822msgid is a synonym",
        example="msgid:abc@host",
    ),
    Operator(
        op="larger",
        prefixes=("larger",),
        kind=ValueKind.SIZE,
        syntax="larger:SIZE",
        meaning=(
            "Messages larger than SIZE; SIZE may carry a k, m, or g unit "
            "(1024-based) or be a bare byte count"
        ),
        example="larger:1M",
    ),
    Operator(
        op="smaller",
        prefixes=("smaller",),
        kind=ValueKind.SIZE,
        syntax="smaller:SIZE",
        meaning="Messages smaller than SIZE; same size grammar as larger:",
        example="smaller:500k",
    ),
    Operator(
        op="has",
        prefixes=("has",),
        kind=ValueKind.TEXT,
        syntax="has:VALUE",
        meaning=(
            "has:attachment matches messages with attachments; other values "
            "(drive, youtube, ...) are Gmail-only"
        ),
        example="has:attachment",
    ),
    Operator(
        op="filename",
        prefixes=("filename",),
        kind=ValueKind.TEXT,
        syntax="filename:NAME",
        meaning="Attachment file name contains NAME",
        example="filename:report.pdf",
    ),
    Operator(
        op="list",
        prefixes=("list",),
        kind=ValueKind.TEXT,
        syntax="list:ID",
        meaning="Mailing list id from the List-Id header",
        example="list:announce.example.com",
    ),
    Operator(
        op="deliveredto",
        prefixes=("deliveredto",),
        kind=ValueKind.TEXT,
        syntax="deliveredto:ADDR",
        meaning="Delivered-To header contains ADDR",
        example="deliveredto:me@example.com",
    ),
    Operator(
        op="label",
        prefixes=("label",),
        kind=ValueKind.TEXT,
        syntax="label:NAME",
        meaning="Gmail label; on other backends scope with in:FOLDER instead",
        example="label:work",
    ),
    Operator(
        op="category",
        prefixes=("category",),
        kind=ValueKind.TEXT,
        syntax="category:NAME",
        meaning="Gmail inbox category tab",
        example="category:promotions",
    ),
    Operator(
        op="in",
        prefixes=("in",),
        kind=ValueKind.TEXT,
        syntax="in:PLACE",
        meaning=(
            "Scope the search: inbox, sent, spam, trash, anywhere, " "or a folder name"
        ),
        example="in:sent",
    ),
    Operator(
        op="imap",
        prefixes=("imap",),
        kind=ValueKind.RAW,
        syntax="imap:EXPR",
        meaning=(
            "Send EXPR straight through as a raw IMAP SEARCH expression; "
            "must lead the query"
        ),
        example="imap:OR TEXT foo SUBJECT bar",
    ),
    Operator(
        op=OP_WORD,
        prefixes=(),
        kind=ValueKind.NONE,
        syntax="WORDS",
        meaning="Tokens with no prefix search the full message text, one term each",
        example="meeting notes",
    ),
    Operator(
        op=OP_PHRASE,
        prefixes=(),
        kind=ValueKind.NONE,
        syntax='"SOME WORDS"',
        meaning=(
            "Quoted text searches as an exact phrase; quoting also forces "
            "operator-looking text to be literal"
        ),
        example='"label:work"',
    ),
    Operator(
        op=OP_KEYWORD,
        prefixes=(),
        kind=ValueKind.NONE,
        syntax=" ".join(STANDALONE_KEYWORDS),
        meaning="A one-word query mapping to a preset date range or match-all",
        example="today",
    ),
    Operator(
        op="boolean",
        prefixes=(),
        kind=ValueKind.NONE,
        syntax="or / not / -",
        meaning=(
            "Combine or negate terms; adjacent terms are AND-ed and 'or' "
            "binds only the terms beside it"
        ),
        example="from:alice or not is:read",
    ),
    Operator(
        op="grouping",
        prefixes=(),
        kind=ValueKind.NONE,
        syntax="( ) { } prefix:(...)",
        meaning=(
            "Group terms; braces OR their contents; prefix:(a b) applies the "
            "prefix to each value and prefix:(a OR b) matches any value"
        ),
        example="subject:(invoice OR receipt)",
    ),
)


_PREFIX_TO_OPERATOR: dict[str, Operator] = {
    prefix: row for row in OPERATORS for prefix in row.prefixes
}


def known_prefixes() -> frozenset[str]:
    """Return every prefix spelling the parser recognises.

    This is the reference set the guard tests use to prove the
    documented inventory neither omits a real prefix nor invents one
    the parser does not accept.

    Returns:
        The frozenset of lowercase prefix spellings, without the
        trailing colon.
    """
    return frozenset(_PREFIX_TO_OPERATOR)


def operator_for_prefix(prefix: str) -> Optional[Operator]:
    """Look up the registry row for one prefix spelling.

    Args:
        prefix: The prefix as written in the query, without the colon;
            case does not matter.

    Returns:
        The matching row, or ``None`` when the spelling is unknown.
    """
    return _PREFIX_TO_OPERATOR.get(prefix.lower())


def _within_one_edit(a: str, b: str) -> bool:
    """Report whether two strings are one Damerau-Levenshtein edit apart.

    One insertion, deletion, substitution, or adjacent transposition.
    The transposition case is what catches ``form`` for ``from``.

    Args:
        a: First string.
        b: Second string.

    Returns:
        True when the strings differ by at most one such edit.
    """
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2:
            i, j = diffs
            return j == i + 1 and a[i] == b[j] and a[j] == b[i]
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1 :]


def suggest_prefixes(prefix: str) -> tuple[str, ...]:
    """Return the known prefixes within one edit of a misspelled one.

    Drives the parser's near-miss refusals: a token like ``form:alice``
    refuses with the correction instead of degrading to literal text.

    Args:
        prefix: The unrecognised prefix as written, without the colon.

    Returns:
        The matching prefix spellings, sorted; empty when nothing in
        the registry is close enough.
    """
    lowered = prefix.lower()
    return tuple(sorted(p for p in _PREFIX_TO_OPERATOR if _within_one_edit(lowered, p)))


def render_operator_help() -> str:
    """Render the operator inventory as aligned help text.

    Walks :data:`OPERATORS` once, padding the syntax column to a common
    width so the meanings line up. This is the single rendering shared
    by the CLI ``--help`` and the MCP search tool, so both track the
    registry automatically.

    Returns:
        A multi-line string: a header line followed by one line per
        operator family, each as ``syntax  meaning  (e.g. example)``.
    """
    lines = ["Gmail-style search operators:"]
    width = max(len(row.syntax) for row in OPERATORS)
    for row in OPERATORS:
        lines.append(
            f"  {row.syntax.ljust(width)}  {row.meaning}  (e.g. {row.example})"
        )
    return "\n".join(lines)
