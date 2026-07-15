"""Search query translation package: grammar, AST, registry, emitters.

:func:`parse` turns a Gmail-style query string into the AST defined in
:mod:`courier.query.ast`. :mod:`courier.query.registry` is the single
inventory from which the parser's prefixes, the typed value grammar,
the rendered operator help, and the near-miss suggestion vocabulary
all derive. The per-backend emitters live in
:mod:`courier.query.emit_imap`, :mod:`courier.query.emit_gmail`, and
:mod:`courier.query.emit_mu`; each exposes ``emit(parsed, *, now,
...)`` returning its emission plus a
:class:`~courier.query.ast.TranslationReport`, and refuses
inexpressible terms with
:class:`~courier.query.ast.UntranslatableForBackend`.
"""

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
from courier.query.grammar import ParseResult, QuerySyntaxError, parse
from courier.query.registry import (
    IS_KEYWORDS,
    OPERATORS,
    STANDALONE_KEYWORDS,
    Operator,
    ValueKind,
    known_prefixes,
    operator_for_prefix,
    render_operator_help,
    suggest_prefixes,
)

__all__ = [
    # ast
    "And",
    "Or",
    "Not",
    "Term",
    "Node",
    "Flag",
    "OP_WORD",
    "OP_PHRASE",
    "OP_KEYWORD",
    "OP_IMAP",
    "TranslationReport",
    "UntranslatableForBackend",
    # grammar
    "parse",
    "ParseResult",
    "QuerySyntaxError",
    # registry
    "Operator",
    "OPERATORS",
    "ValueKind",
    "IS_KEYWORDS",
    "STANDALONE_KEYWORDS",
    "known_prefixes",
    "operator_for_prefix",
    "render_operator_help",
    "suggest_prefixes",
]
