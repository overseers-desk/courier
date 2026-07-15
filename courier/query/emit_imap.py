"""Generic IMAP SEARCH emitter: AST to nested imapclient criteria.

The emitter walks the AST from :mod:`courier.query.grammar` and builds
criteria for ``imapclient.IMAPClient.search``. Structure is expressed
with real grouping: an n-ary ``Or`` right-folds into binary RFC 3501
``OR`` pairs, and a multi-key operand becomes a nested list, which
imapclient renders as a parenthesized group.

Charset handling works around imapclient 3.1.0 behaviour verified
against its source: ``_normalise_search_criteria`` does not forward the
charset into nested lists, and it concatenates the closing paren onto
the last element of a nested list, which corrupts a trailing 8-bit
literal. The emitter therefore pre-encodes every text value to UTF-8
bytes itself, reports ``charset="UTF-8"`` only when some value is
non-ASCII (some servers reject an unnecessary CHARSET), and guarantees
the first and last element of every nested list is ASCII: a key always
leads, and when a non-ASCII value would trail a group the neutral
``ALL`` key is appended inside it (``ALL`` is the identity for AND).

Values that imapclient would put on the wire unquoted and unprotected
are refused rather than sent wrong: control characters always (a CR/LF
inside an inline value would splice into the command), and the
IMAP atom-specials ``( ) { } % *`` when nothing in the value triggers
imapclient's quoting (it quotes only for backslash, double quote,
space, or empty). Such values only arise from quoted phrases.
"""

from __future__ import annotations

import shlex
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

# One criteria atom as imapclient accepts it: a key or pre-encoded
# value (bytes), a size (int), a calendar date, or a nested group.
Atom = Union[bytes, int, date, List["Atom"]]

# A search key is one RFC 3501 key with its arguments, e.g.
# [b"FROM", b"alice"] or [b"SEEN"] or [b"OR", ...].
_Key = List[Atom]

# prefix Term.op -> plain IMAP key for the directly mapped operators.
_DIRECT_KEYS = {
    "from": b"FROM",
    "to": b"TO",
    "cc": b"CC",
    "bcc": b"BCC",
    "subject": b"SUBJECT",
    "body": b"BODY",
}

# Flag state -> IMAP flag key.
_FLAG_KEYS = {
    Flag.READ: b"SEEN",
    Flag.UNREAD: b"UNSEEN",
    Flag.FLAGGED: b"FLAGGED",
    Flag.UNFLAGGED: b"UNFLAGGED",
    Flag.ANSWERED: b"ANSWERED",
    Flag.UNANSWERED: b"UNANSWERED",
}

# Term.op -> header name for the HEADER-mapped operators.
_HEADER_FIELDS = {
    "msgid": b"Message-ID",
    "list": b"List-Id",
    "deliveredto": b"Delivered-To",
}

# Atom-specials imapclient leaves unquoted unless something else in the
# value already forces quoting; unprotected they would splice into the
# command grammar on the wire.
_UNPROTECTED_SPECIALS = set("(){}%*")

# Characters that DO make imapclient quote a value.
_QUOTE_TRIGGERS = set(' "\\')

_NOTE_TEXT_SUBSTRING = (
    "IMAP TEXT matches substrings anywhere in the message, not whole words"
)
_NOTE_PHRASE_SUBSTRING = "a quoted phrase matches as one contiguous substring"
_NOTE_DATES = (
    "IMAP dates compare the server's INTERNALDATE at day granularity "
    "in the server's local time"
)
_NOTE_RELATIVE_DAYS = (
    "newer:/older: resolved to whole-day SINCE/BEFORE bounds against "
    "the reference instant"
)
_NOTE_WITHIN_SKIPPED = (
    "server-side WITHIN is skipped under a WORLD_AS_OF bound because the "
    "server would evaluate it against its own clock; resolved to whole-day "
    "bounds instead"
)
_NOTE_BCC = "Bcc survives only on stored sent copies, so bcc: can only match those"


@dataclass(frozen=True)
class ImapEmission:
    """One generic-IMAP emission: criteria, charset, and the report.

    Attributes:
        criteria: The nested criteria list for
            ``imapclient.IMAPClient.search``. Text values are
            pre-encoded UTF-8 bytes; dates and sizes stay typed.
        charset: ``"UTF-8"`` when any value is non-ASCII, else ``None``
            so no CHARSET argument is sent.
        report: The translation report for ``provenance.query``.
    """

    criteria: List[Atom]
    charset: Optional[str]
    report: TranslationReport


def _refuse(
    operator: str, message: str, suggestion: str = ""
) -> UntranslatableForBackend:
    """Build this backend's refusal for one operator.

    Args:
        operator: The operator as written, colon included.
        message: Why the generic IMAP backend cannot express it.
        suggestion: The nearest alternative, or empty.

    Returns:
        The exception for the caller to raise.
    """
    return UntranslatableForBackend("imap", operator, message, suggestion)


def _check_value(operator: str, value: str) -> None:
    """Refuse values imapclient cannot put on the wire safely.

    Control characters are refused always: quoted strings cannot carry
    them and an inline CR/LF would splice into the command. The
    atom-specials ``( ) { } % *`` are refused only when the value is
    ASCII and contains none of the characters that make imapclient
    quote it (non-ASCII values travel as literals, which protect any
    content).

    Args:
        operator: The operator the value belongs to, for the message.
        value: The value text as parsed.

    Raises:
        UntranslatableForBackend: When the value cannot be transmitted.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise _refuse(
            operator,
            "the value contains control characters, which cannot be "
            "carried in an IMAP search string",
            "Remove the control characters from the search text.",
        )
    if (
        value.isascii()
        and any(c in _UNPROTECTED_SPECIALS for c in value)
        and not any(c in _QUOTE_TRIGGERS for c in value)
    ):
        raise _refuse(
            operator,
            "the IMAP client library would send this value unquoted, and "
            "its special characters would corrupt the command",
            "Add a space to the quoted text, drop the special characters, "
            "or search the local cache or a Gmail account.",
        )


def _encode(operator: str, value: str) -> bytes:
    """Pre-encode one text value to UTF-8 bytes after safety checks.

    Args:
        operator: The operator the value belongs to, for refusals.
        value: The value text as parsed.

    Returns:
        The UTF-8 encoded value.

    Raises:
        UntranslatableForBackend: When the value cannot be transmitted.
    """
    _check_value(operator, value)
    return value.encode("utf-8")


def _group(keys: List[_Key]) -> List[Atom]:
    """Fold search keys into one nested (parenthesized) criteria list.

    imapclient appends the closing paren onto the group's last element,
    which corrupts a trailing 8-bit literal, so when the last atom is a
    non-ASCII bytes value the neutral ``ALL`` key is appended inside
    the group. The first element is always an ASCII key already.

    Args:
        keys: The search keys to group, in order.

    Returns:
        The flat atom list to embed as one nested criteria element.
    """
    atoms: List[Atom] = []
    for key in keys:
        atoms.extend(key)
    last = atoms[-1]
    if isinstance(last, bytes) and not last.isascii():
        atoms.append(b"ALL")
    return atoms


class _Emitter:
    """One emission walk: holds the reference instant and the notes.

    Attributes:
        now: Reference instant for relative and keyword terms.
        supports_within: Whether the server advertises RFC 5032 WITHIN.
        bounded: Whether the caller runs under a WORLD_AS_OF bound
            (server-side WITHIN is ineligible then, because the server
            evaluates it against its own clock).
        notes: The approximation notes gathered on the walk.
    """

    def __init__(self, now: datetime, supports_within: bool, bounded: bool) -> None:
        self.now = now
        self.supports_within = supports_within
        self.bounded = bounded
        self.notes: list[str] = []

    def _note(self, note: str) -> None:
        """Record an approximation note once, in first-seen order.

        Args:
            note: The approximation note text.
        """
        if note not in self.notes:
            self.notes.append(note)

    def keys(self, node: Node) -> List[_Key]:
        """Emit a node as a list of AND-joined search keys.

        Args:
            node: The AST node.

        Returns:
            One or more search keys whose juxtaposition means AND.

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        if isinstance(node, And):
            keys: List[_Key] = []
            for child in node.children:
                keys.extend(self.keys(child))
            return keys
        if isinstance(node, Or):
            return [self._or_key(list(node.children))]
        if isinstance(node, Not):
            inner = self.keys(node.child)
            if len(inner) == 1:
                return [[b"NOT", *inner[0]]]
            return [[b"NOT", _group(inner)]]
        return self._term_keys(node)

    def _unit(self, node: Node) -> _Key:
        """Emit a node as exactly one search key, grouping if needed.

        Args:
            node: The AST node standing as an OR operand.

        Returns:
            A single search key; multi-key emissions become one nested
            group.
        """
        keys = self.keys(node)
        if len(keys) == 1:
            return keys[0]
        return [_group(keys)]

    def _or_key(self, children: List[Node]) -> _Key:
        """Right-fold an n-ary Or into binary RFC 3501 OR pairs.

        Args:
            children: The Or node's operands, at least two.

        Returns:
            One search key of the shape OR a (OR b c) in prefix form.
        """
        head = self._unit(children[0])
        if len(children) == 2:
            tail = self._unit(children[1])
        else:
            tail = self._or_key(children[1:])
        return [b"OR", *head, *tail]

    def _term_keys(self, term: Term) -> List[_Key]:
        """Emit one leaf term as its search keys.

        Args:
            term: The leaf term.

        Returns:
            The search keys for the term (keyword terms may need two).

        Raises:
            UntranslatableForBackend: For operators this backend
                refuses.
        """
        op = term.op
        if op == OP_WORD:
            assert isinstance(term.value, str)
            self._note(_NOTE_TEXT_SUBSTRING)
            return [[b"TEXT", _encode(op, term.value)]]
        if op == OP_PHRASE:
            assert isinstance(term.value, str)
            self._note(_NOTE_PHRASE_SUBSTRING)
            return [[b"TEXT", _encode(op, term.value)]]
        if op == OP_KEYWORD:
            assert isinstance(term.value, str)
            return self._keyword_keys(term.value)
        if op in _DIRECT_KEYS:
            assert isinstance(term.value, str)
            if op == "bcc":
                self._note(_NOTE_BCC)
            return [[_DIRECT_KEYS[op], _encode(f"{op}:", term.value)]]
        if op == "is":
            assert isinstance(term.value, Flag)
            return [[_FLAG_KEYS[term.value]]]
        if op == "after":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return [[b"SINCE", term.value]]
        if op == "before":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return [[b"BEFORE", term.value]]
        if op == "on":
            assert isinstance(term.value, date)
            self._note(_NOTE_DATES)
            return [[b"ON", term.value]]
        if op in ("newer", "older"):
            assert isinstance(term.value, timedelta)
            return [self._relative_key(op, term.value)]
        if op == "larger":
            assert isinstance(term.value, int)
            return [[b"LARGER", term.value]]
        if op == "smaller":
            assert isinstance(term.value, int)
            return [[b"SMALLER", term.value]]
        if op in _HEADER_FIELDS:
            assert isinstance(term.value, str)
            return [[b"HEADER", _HEADER_FIELDS[op], _encode(f"{op}:", term.value)]]
        if op == "has":
            assert isinstance(term.value, str)
            if term.value.lower() == "attachment":
                raise _refuse(
                    "has:attachment",
                    "has:attachment has no server-side search on this IMAP " "backend",
                    "The operator works on Gmail accounts and on accounts "
                    "with a local mail cache.",
                )
            raise _refuse(
                f"has:{term.value}",
                f"only Gmail understands has:{term.value}",
                "Run this query on a Gmail account.",
            )
        if op == "filename":
            raise _refuse(
                "filename:",
                "generic IMAP has no attachment file name search",
                "Use a Gmail account or the local mail cache.",
            )
        if op == "label":
            raise _refuse(
                "label:",
                "labels are Gmail-only on this backend",
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
        raise _refuse(f"{op}:", "the operator has no generic IMAP mapping")

    def _keyword_keys(self, keyword: str) -> List[_Key]:
        """Emit a standalone keyword (all/today/yesterday/week/month).

        Args:
            keyword: The keyword, already lowercased by the parser.

        Returns:
            The search keys resolved against the reference instant.
        """
        today = self.now.date()
        if keyword == "all":
            return [[b"ALL"]]
        if keyword == "today":
            self._note(_NOTE_DATES)
            return [[b"SINCE", today]]
        if keyword == "yesterday":
            self._note(_NOTE_DATES)
            yesterday = today - timedelta(days=1)
            return [[b"SINCE", yesterday], [b"BEFORE", today]]
        if keyword == "week":
            self._note(_NOTE_DATES)
            return [[b"SINCE", today - timedelta(days=7)]]
        self._note(_NOTE_DATES)
        return [[b"SINCE", today - timedelta(days=30)]]

    def _relative_key(self, op: str, delta: timedelta) -> _Key:
        """Emit newer:/older: via WITHIN when eligible, else by date.

        RFC 5032 YOUNGER/OLDER is used only when the server advertises
        WITHIN and the client is unbounded, because the server
        evaluates the interval against its own now, which a
        WORLD_AS_OF replay cannot use.

        Args:
            op: ``"newer"`` or ``"older"``.
            delta: The parsed offset.

        Returns:
            The single search key.
        """
        if self.supports_within and not self.bounded:
            seconds = int(delta.total_seconds())
            key = b"YOUNGER" if op == "newer" else b"OLDER"
            return [key, seconds]
        if self.supports_within and self.bounded:
            self._note(_NOTE_WITHIN_SKIPPED)
        else:
            self._note(_NOTE_RELATIVE_DAYS)
        self._note(_NOTE_DATES)
        boundary = (self.now - delta).date()
        key = b"SINCE" if op == "newer" else b"BEFORE"
        return [key, boundary]


def _raw_criteria(raw: str) -> List[Atom]:
    """Tokenize an imap: raw passthrough into criteria atoms.

    Args:
        raw: The raw IMAP SEARCH expression after ``imap:``.

    Returns:
        The tokens as UTF-8 bytes atoms, quoting respected via shlex
        (matching the old passthrough behaviour).
    """
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    return [token.encode("utf-8") for token in tokens]


def _needs_charset(criteria: List[Atom]) -> bool:
    """Report whether any criteria value is non-ASCII.

    Args:
        criteria: The assembled criteria.

    Returns:
        True when some bytes atom carries a byte above 0x7F.
    """
    for item in criteria:
        if isinstance(item, bytes) and not item.isascii():
            return True
        if isinstance(item, list) and _needs_charset(item):
            return True
    return False


def emit(
    parsed: ParseResult,
    *,
    now: datetime,
    supports_within: bool = False,
    bounded: bool = False,
) -> ImapEmission:
    """Emit a parsed query as generic IMAP SEARCH criteria.

    Args:
        parsed: The parse result; its ``treated_as_text`` tokens are
            copied into the report.
        now: Reference instant for relative and keyword terms; a
            bounded caller passes its WORLD_AS_OF instant so replays
            do not drift.
        supports_within: Whether the server advertises RFC 5032
            WITHIN, making server-side YOUNGER/OLDER eligible.
        bounded: Whether the caller runs under a WORLD_AS_OF bound,
            which disables server-side WITHIN.

    Returns:
        The emission: criteria, charset (``"UTF-8"`` only when some
        value is non-ASCII), and the translation report.

    Raises:
        UntranslatableForBackend: When some operator cannot be
            expressed on generic IMAP; the message names the nearest
            alternative.
    """
    report = TranslationReport(
        dialect="imap", treated_as_text=list(parsed.treated_as_text)
    )
    root = parsed.ast
    if isinstance(root, Term) and root.op == OP_IMAP:
        assert isinstance(root.value, str)
        criteria = _raw_criteria(root.value)
        charset = "UTF-8" if _needs_charset(criteria) else None
        return ImapEmission(criteria, charset, report)
    emitter = _Emitter(now, supports_within, bounded)
    keys = emitter.keys(root)
    criteria: List[Atom] = []
    for key in keys:
        criteria.extend(key)
    report.approximations = list(emitter.notes)
    charset = "UTF-8" if _needs_charset(criteria) else None
    return ImapEmission(criteria, charset, report)
