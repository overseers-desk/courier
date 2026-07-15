"""Lockstep guards: the operator registry and the parser cannot drift.

Extends the old _known_prefixes guard pattern: the pinned inventory
proves the registry neither loses nor invents an operator, and the
parse-through guard proves every advertised prefix spelling actually
parses to a term carrying its row's canonical op and value type. The
docs guard carries over from the old translator's test file: the
quick-reference table may only cite real operators.
"""

import re
from datetime import date, timedelta
from pathlib import Path

from courier.query import parse
from courier.query.ast import Flag, Term
from courier.query.registry import (
    IS_KEYWORDS,
    OPERATORS,
    STANDALONE_KEYWORDS,
    ValueKind,
    known_prefixes,
    operator_for_prefix,
    render_operator_help,
    suggest_prefixes,
)

# One parseable sample value per value kind. A new kind added to the
# registry without a row here fails the parse-through guard loudly.
KIND_SAMPLES = {
    ValueKind.TEXT: "x",
    ValueKind.DATE: "2025-03-01",
    ValueKind.DELTA: "3d",
    ValueKind.SIZE: "1M",
    ValueKind.MSGID: "abc@host",
    ValueKind.FLAG: "unread",
    ValueKind.RAW: "UNSEEN",
}

# The value type each kind must produce on the parsed term.
KIND_TYPES = {
    ValueKind.TEXT: str,
    ValueKind.DATE: date,
    ValueKind.DELTA: timedelta,
    ValueKind.SIZE: int,
    ValueKind.MSGID: str,
    ValueKind.FLAG: Flag,
    ValueKind.RAW: str,
}

# The full prefix inventory, pinned. A row added to or removed from the
# registry must be a deliberate edit here too.
EXPECTED_PREFIXES = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "is",
        "after",
        "before",
        "on",
        "newer",
        "newer_than",
        "older",
        "older_than",
        "msgid",
        "rfc822msgid",
        "larger",
        "smaller",
        "has",
        "filename",
        "list",
        "deliveredto",
        "label",
        "category",
        "in",
        "imap",
    }
)


class TestPrefixInventory:
    """The registry's prefix set is exactly the pinned inventory."""

    def test_known_prefixes_match_pinned_inventory(self):
        assert known_prefixes() == EXPECTED_PREFIXES

    def test_prefix_spellings_are_claimed_once(self):
        spellings = [p for row in OPERATORS for p in row.prefixes]
        assert len(spellings) == len(set(spellings))

    def test_canonical_op_is_one_of_its_spellings(self):
        for row in OPERATORS:
            if row.prefixes:
                assert row.op in row.prefixes, row.op

    def test_lookup_is_case_insensitive(self):
        assert operator_for_prefix("FROM") is operator_for_prefix("from")

    def test_unknown_prefix_returns_none(self):
        assert operator_for_prefix("bogus") is None


class TestParserRegistryLockstep:
    """Every advertised prefix parses to its canonical op and type."""

    def test_every_prefix_parses_to_its_canonical_op(self):
        for row in OPERATORS:
            for prefix in row.prefixes:
                node = parse(f"{prefix}:{KIND_SAMPLES[row.kind]}").ast
                assert isinstance(node, Term), prefix
                assert node.op == row.op, prefix

    def test_every_prefix_produces_the_kind_typed_value(self):
        for row in OPERATORS:
            for prefix in row.prefixes:
                node = parse(f"{prefix}:{KIND_SAMPLES[row.kind]}").ast
                assert isinstance(node, Term), prefix
                assert isinstance(node.value, KIND_TYPES[row.kind]), prefix

    def test_no_prefix_row_uses_the_none_kind(self):
        for row in OPERATORS:
            if row.prefixes:
                assert row.kind is not ValueKind.NONE, row.op

    def test_non_prefix_rows_use_the_none_kind(self):
        for row in OPERATORS:
            if not row.prefixes:
                assert row.kind is ValueKind.NONE, row.op


class TestIsKeywordInventory:
    """The is: spellings are pinned and all map to flag states."""

    def test_spellings_pinned(self):
        assert set(IS_KEYWORDS) == {
            "unread",
            "read",
            "flagged",
            "starred",
            "unflagged",
            "unstarred",
            "answered",
            "unanswered",
        }

    def test_values_are_flags(self):
        for flag in IS_KEYWORDS.values():
            assert isinstance(flag, Flag)

    def test_synonym_pairs_share_a_state(self):
        assert IS_KEYWORDS["starred"] is IS_KEYWORDS["flagged"]
        assert IS_KEYWORDS["unstarred"] is IS_KEYWORDS["unflagged"]


class TestStandaloneKeywordInventory:
    """The whole-query keywords are pinned."""

    def test_keywords_pinned(self):
        assert tuple(STANDALONE_KEYWORDS) == (
            "all",
            "today",
            "yesterday",
            "week",
            "month",
        )


class TestSuggestions:
    """Near-miss suggestions come from the registry vocabulary."""

    def test_transposition_is_one_edit(self):
        assert "from" in suggest_prefixes("form")

    def test_substitution_is_one_edit(self):
        assert "subject" in suggest_prefixes("sublect")

    def test_deletion_is_one_edit(self):
        assert "body" in suggest_prefixes("bod")

    def test_insertion_is_one_edit(self):
        assert "cc" in suggest_prefixes("ccc")

    def test_distant_tokens_suggest_nothing(self):
        assert suggest_prefixes("https") == ()

    def test_suggestions_are_sorted(self):
        hits = suggest_prefixes("im")
        assert list(hits) == sorted(hits)


class TestDocsOperatorTable:
    """The docs quick-reference table must cite only real prefixes.

    One-directional until the stage-4 doc rewrite re-pins the full
    lockstep: the table may lag the registry's new rows between the
    two stages, but it must never cite an operator the parser does not
    accept.
    """

    def test_documented_table_prefixes_are_known(self):
        doc = Path(__file__).parents[1] / "docs" / "COMPLEX_SEARCH_IMPLEMENTATION.md"
        lines = doc.read_text(encoding="utf-8").splitlines()
        start = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("| Syntax")
        )
        tokens = []
        # Skip the header and its separator row, then read data rows.
        for ln in lines[start + 2 :]:
            if not ln.lstrip().startswith("|"):
                break
            first_col = ln.split("|")[1]
            tokens.extend(re.findall(r"([a-z_]+):", first_col))
        assert tokens, "no prefix tokens found in the docs operator table"
        known = known_prefixes()
        for tok in tokens:
            assert tok in known, f"docs cite unknown prefix {tok!r}"


class TestRenderedHelp:
    """The help surface renders from the registry, bracket-free."""

    def test_no_square_brackets(self):
        """Rich markup in the Typer help eats square brackets, so none
        may appear in the rendered inventory."""
        rendered = render_operator_help()
        assert "[" not in rendered
        assert "]" not in rendered

    def test_header_line(self):
        assert render_operator_help().splitlines()[0] == (
            "Gmail-style search operators:"
        )

    def test_every_row_syntax_appears(self):
        rendered = render_operator_help()
        for row in OPERATORS:
            assert row.syntax in rendered, row.syntax

    def test_every_row_has_help_text(self):
        for row in OPERATORS:
            assert row.syntax and row.meaning and row.example, row.op
