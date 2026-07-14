"""Tests for the query AST node types, report, and refusal exception."""

import dataclasses

import pytest

from courier.query.ast import (
    OP_IMAP,
    OP_KEYWORD,
    OP_PHRASE,
    OP_WORD,
    And,
    Flag,
    Not,
    Or,
    Term,
    TranslationReport,
    UntranslatableForBackend,
)


class TestNodeConstruction:
    """The four node types build and normalise as specified."""

    def test_term_holds_op_and_value(self):
        term = Term("from", "alice")
        assert term.op == "from"
        assert term.value == "alice"

    def test_and_coerces_children_to_tuple(self):
        a, b = Term(OP_WORD, "a"), Term(OP_WORD, "b")
        node = And([a, b])
        assert node.children == (a, b)
        assert isinstance(node.children, tuple)

    def test_or_coerces_children_to_tuple(self):
        a, b = Term(OP_WORD, "a"), Term(OP_WORD, "b")
        node = Or([a, b])
        assert node.children == (a, b)
        assert isinstance(node.children, tuple)

    def test_and_requires_at_least_two_children(self):
        with pytest.raises(ValueError):
            And((Term(OP_WORD, "a"),))
        with pytest.raises(ValueError):
            And(())

    def test_or_requires_at_least_two_children(self):
        with pytest.raises(ValueError):
            Or((Term(OP_WORD, "a"),))
        with pytest.raises(ValueError):
            Or(())

    def test_not_holds_single_child(self):
        child = Term("is", Flag.READ)
        assert Not(child).child == child

    def test_op_constants_are_distinct(self):
        assert len({OP_WORD, OP_PHRASE, OP_KEYWORD, OP_IMAP}) == 4


class TestNodeImmutability:
    """Nodes are frozen and hashable so trees can be shared safely."""

    def test_term_is_frozen(self):
        term = Term("from", "alice")
        with pytest.raises(dataclasses.FrozenInstanceError):
            term.value = "bob"

    def test_and_is_frozen(self):
        node = And((Term(OP_WORD, "a"), Term(OP_WORD, "b")))
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.children = ()

    def test_not_is_frozen(self):
        node = Not(Term(OP_WORD, "a"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.child = Term(OP_WORD, "b")

    def test_trees_are_hashable(self):
        tree = And((Term("from", "alice"), Not(Term("is", Flag.READ))))
        assert isinstance(hash(tree), int)


class TestNodeEquality:
    """Structural equality lets corpus tests compare whole trees."""

    def test_equal_trees_compare_equal(self):
        left = And(
            (Term("from", "alice"), Or((Term(OP_WORD, "a"), Term(OP_WORD, "b"))))
        )
        right = And(
            (Term("from", "alice"), Or((Term(OP_WORD, "a"), Term(OP_WORD, "b"))))
        )
        assert left == right

    def test_different_ops_compare_unequal(self):
        assert Term("from", "alice") != Term("to", "alice")

    def test_and_or_are_not_interchangeable(self):
        children = (Term(OP_WORD, "a"), Term(OP_WORD, "b"))
        assert And(children) != Or(children)


class TestFlagEnum:
    """The is: keyword spellings collapse onto six flag states."""

    def test_exactly_six_states(self):
        assert {f.name for f in Flag} == {
            "READ",
            "UNREAD",
            "FLAGGED",
            "UNFLAGGED",
            "ANSWERED",
            "UNANSWERED",
        }


class TestTranslationReport:
    """The report shape matches the provenance.query envelope contract."""

    def test_defaults_are_empty(self):
        report = TranslationReport(dialect="imap")
        assert report.approximations == []
        assert report.fallbacks == []
        assert report.treated_as_text == []

    def test_as_dict_shape(self):
        report = TranslationReport(dialect="gmail")
        report.approximations.append("body: approximated as a bare word")
        report.treated_as_text.append("https://example.com")
        assert report.as_dict() == {
            "dialect": "gmail",
            "approximations": ["body: approximated as a bare word"],
            "fallbacks": [],
            "treated_as_text": ["https://example.com"],
        }


class TestUntranslatableForBackend:
    """The refusal names the backend, the operator, and an alternative."""

    def test_attributes(self):
        exc = UntranslatableForBackend(
            backend="imap",
            operator="label:",
            message="plain IMAP servers have no labels",
            suggestion="Use in:FOLDER to scope to a folder instead.",
        )
        assert exc.backend == "imap"
        assert exc.operator == "label:"
        assert exc.suggestion == "Use in:FOLDER to scope to a folder instead."

    def test_reason_matches_fell_back_vocabulary(self):
        exc = UntranslatableForBackend("mu", "deliveredto:", "not indexed")
        assert exc.reason == "untranslatable"

    def test_message_carries_the_pieces(self):
        exc = UntranslatableForBackend(
            backend="mu",
            operator="category:",
            message="only Gmail has category tabs",
            suggestion="Search the Gmail backend for category: terms.",
        )
        text = str(exc)
        assert "category:" in text
        assert "mu" in text
        assert "only Gmail has category tabs" in text
        assert "Search the Gmail backend" in text
