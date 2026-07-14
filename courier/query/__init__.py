"""Search query translation package: grammar, AST, and operator registry.

:func:`parse` turns a Gmail-style query string into the AST defined in
:mod:`courier.query.ast`. :mod:`courier.query.registry` is the single
inventory from which the parser's prefixes, the typed value grammar,
the rendered operator help, and the near-miss suggestion vocabulary
all derive.
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
