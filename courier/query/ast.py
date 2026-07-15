"""AST node types for the Gmail-style search query grammar.

The recursive-descent parser in :mod:`courier.query.grammar` produces
trees built from the four node types defined here, and the per-backend
emitters walk the same trees. Query semantics (precedence, negation,
what a bare word means) are decided once, at this level; an emitter
translates a tree or refuses a node, but never reinterprets one.

:class:`TranslationReport` is the declared-approximations record every
emission returns, and :class:`UntranslatableForBackend` is the refusal
an emitter raises for a term its backend cannot express.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Union

# Term.op values for the non-prefix term forms. Prefix operators carry
# the canonical op name of their registry row ("from", "after", ...).
OP_WORD = "word"
OP_PHRASE = "phrase"
OP_KEYWORD = "keyword"
OP_IMAP = "imap"


class Flag(Enum):
    """Message-state values carried by ``is:`` terms.

    The eight ``is:`` keyword spellings collapse onto these six states:
    ``starred`` is the same state as ``flagged``, and ``unstarred`` the
    same as ``unflagged``.
    """

    READ = "read"
    UNREAD = "unread"
    FLAGGED = "flagged"
    UNFLAGGED = "unflagged"
    ANSWERED = "answered"
    UNANSWERED = "unanswered"


@dataclass(frozen=True)
class Term:
    """A single leaf condition.

    Attributes:
        op: Canonical operator name from the registry row, or one of the
            non-prefix forms ``OP_WORD``, ``OP_PHRASE``, ``OP_KEYWORD``,
            ``OP_IMAP``.
        value: The typed operand: text for word, phrase, and text-valued
            operators; a ``date`` for absolute date operators; a
            ``timedelta`` for relative ones; a byte count for size
            operators; a :class:`Flag` for ``is:`` terms.
    """

    op: str
    value: Union[str, date, timedelta, int, Flag]


@dataclass(frozen=True)
class Not:
    """Negation of exactly one child node.

    The child is a required constructor argument, so a negation without
    an operand is impossible to build (the old parser could emit a bare
    ``NOT`` criteria fragment; this shape cannot).

    Attributes:
        child: The negated node.
    """

    child: "Node"


@dataclass(frozen=True)
class And:
    """Conjunction of two or more children (query juxtaposition).

    Attributes:
        children: The conjoined nodes, at least two. A sequence passed
            to the constructor is coerced to a tuple.
    """

    children: tuple["Node", ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        if len(self.children) < 2:
            raise ValueError("And requires at least two children.")


@dataclass(frozen=True)
class Or:
    """Disjunction of two or more children.

    ``or`` binds only the terms beside it, so the parser builds one
    n-ary node per run of ``or``-joined operands.

    Attributes:
        children: The alternative nodes, at least two. A sequence passed
            to the constructor is coerced to a tuple.
    """

    children: tuple["Node", ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        if len(self.children) < 2:
            raise ValueError("Or requires at least two children.")


Node = Union[Term, Not, And, Or]


@dataclass
class TranslationReport:
    """What one emission did to the query, declared for the caller.

    Surfaced on result envelopes as ``provenance.query`` so an AI caller
    can tell an exact translation from an approximated one instead of
    reading a wrong-but-empty result as evidence of absence.

    Attributes:
        dialect: The emitted dialect (``"imap"``, ``"gmail"``, ``"mu"``).
        approximations: Human-readable notes on terms whose backend
            meaning only approximates the query's meaning.
        fallbacks: Backends that declined before this one ran, each as
            ``{"backend": ..., "reason": ...}`` with the reason drawn
            from the ``fell_back_reason`` vocabulary. The dispatcher
            fills this; emitters leave it empty.
        treated_as_text: Tokens that looked prefix-like but matched no
            operator and were kept as literal words (URLs and the like).
    """

    dialect: str
    approximations: list[str] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    treated_as_text: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return the report as the ``provenance.query`` envelope shape.

        Returns:
            A dict with exactly the keys ``dialect``, ``approximations``,
            ``fallbacks``, and ``treated_as_text``.
        """
        return {
            "dialect": self.dialect,
            "approximations": list(self.approximations),
            "fallbacks": list(self.fallbacks),
            "treated_as_text": list(self.treated_as_text),
        }


class UntranslatableForBackend(Exception):
    """Raised when an emitter cannot express a term on its backend.

    The dispatcher catches this to try the next backend; when every
    backend refuses, the terminal error names the operator, each
    backend's reason, and the nearest alternative.

    Attributes:
        backend: The refusing backend (``"imap"``, ``"gmail"``, ``"mu"``).
        operator: The operator that cannot be expressed, colon included.
        suggestion: The nearest alternative the caller can act on, or an
            empty string when there is none.
        reason: Short tag matching the ``fell_back_reason`` vocabulary
            used by the local-cache backend; always ``"untranslatable"``.
    """

    def __init__(
        self,
        backend: str,
        operator: str,
        message: str,
        suggestion: str = "",
    ) -> None:
        self.backend = backend
        self.operator = operator
        self.suggestion = suggestion
        self.reason = "untranslatable"
        text = f"{operator} cannot be expressed on the {backend} backend: {message}."
        if suggestion:
            text = f"{text} {suggestion}"
        super().__init__(text)
