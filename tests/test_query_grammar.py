"""Grammar corpus: query strings against their expected ASTs.

Each row pins one shape of the grammar so a change in precedence,
negation, grouping, or value typing fails loudly here before it can
reach an emitter. The precedence counterexample and the required error
rows come verbatim from the translator plan.
"""

from datetime import date, timedelta

import pytest

from courier.query import QuerySyntaxError, parse
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
)


def ast_of(query: str):
    """Parse a query and return only the AST root."""
    return parse(query).ast


def w(text: str) -> Term:
    """Shorthand for a bare-word term."""
    return Term(OP_WORD, text)


class TestBareWordsAndPhrases:
    """Bare words stay words, one term each; quoting forces literal text."""

    def test_single_word(self):
        assert ast_of("meeting") == w("meeting")

    def test_multiple_words_are_one_term_each(self):
        assert ast_of("meeting notes") == And((w("meeting"), w("notes")))

    def test_extra_whitespace_is_insignificant(self):
        assert ast_of("  meeting   notes  ") == And((w("meeting"), w("notes")))

    def test_quoted_words_are_a_phrase(self):
        assert ast_of('"meeting notes"') == Term(OP_PHRASE, "meeting notes")

    def test_quoting_forces_operator_text_literal(self):
        assert ast_of('"label:work"') == Term(OP_PHRASE, "label:work")

    def test_infix_hyphen_stays_inside_one_word(self):
        assert ast_of("meeting-notes") == w("meeting-notes")

    def test_standalone_dash_is_a_word(self):
        assert ast_of("-") == w("-")

    def test_numeric_query_is_a_word(self):
        assert ast_of("69172700") == w("69172700")

    def test_and_is_a_plain_word_not_an_operator(self):
        """The grammar has no AND token; juxtaposition is the only AND."""
        assert ast_of("alpha AND beta") == And((w("alpha"), w("AND"), w("beta")))

    def test_empty_query_means_match_all(self):
        assert ast_of("") == Term(OP_KEYWORD, "all")

    def test_whitespace_only_query_means_match_all(self):
        assert ast_of("   ") == Term(OP_KEYWORD, "all")


class TestPrefixTerms:
    """prefix:value tokens become typed terms keyed by canonical op."""

    def test_from(self):
        assert ast_of("from:alice") == Term("from", "alice")

    def test_to(self):
        assert ast_of("to:bob") == Term("to", "bob")

    def test_cc(self):
        assert ast_of("cc:team") == Term("cc", "team")

    def test_bcc(self):
        assert ast_of("bcc:carol@x") == Term("bcc", "carol@x")

    def test_subject(self):
        assert ast_of("subject:invoice") == Term("subject", "invoice")

    def test_body(self):
        assert ast_of("body:hello") == Term("body", "hello")

    def test_prefix_is_case_insensitive(self):
        assert ast_of("FROM:alice") == Term("from", "alice")
        assert ast_of("Subject:invoice") == Term("subject", "invoice")

    def test_value_keeps_address_untouched(self):
        assert ast_of("from:alice@example.com") == Term("from", "alice@example.com")

    def test_new_operators_parse(self):
        assert ast_of("filename:report.pdf") == Term("filename", "report.pdf")
        assert ast_of("list:announce.example.com") == Term(
            "list", "announce.example.com"
        )
        assert ast_of("deliveredto:me@example.com") == Term(
            "deliveredto", "me@example.com"
        )
        assert ast_of("label:work") == Term("label", "work")
        assert ast_of("category:promotions") == Term("category", "promotions")
        assert ast_of("in:sent") == Term("in", "sent")

    def test_has_accepts_any_value_at_parse_time(self):
        """Backend fitness of has: values is judged at emit, not parse."""
        assert ast_of("has:attachment") == Term("has", "attachment")
        assert ast_of("has:drive") == Term("has", "drive")


class TestTypedValues:
    """Dates, offsets, sizes, and message ids are typed while parsing."""

    def test_after_iso_date(self):
        assert ast_of("after:2025-03-01") == Term("after", date(2025, 3, 1))

    def test_after_slash_date(self):
        assert ast_of("after:2025/03/01") == Term("after", date(2025, 3, 1))

    def test_before(self):
        assert ast_of("before:2025-04-01") == Term("before", date(2025, 4, 1))

    def test_on(self):
        assert ast_of("on:2025-03-15") == Term("on", date(2025, 3, 15))

    def test_invalid_date_raises(self):
        with pytest.raises(QuerySyntaxError, match="Invalid date"):
            parse("after:not-a-date")

    def test_newer_days_is_a_timedelta(self):
        assert ast_of("newer:3d") == Term("newer", timedelta(days=3))

    def test_older_weeks_is_a_timedelta(self):
        assert ast_of("older:2w") == Term("older", timedelta(weeks=2))

    def test_relative_month_is_thirty_days(self):
        assert ast_of("newer:1m") == Term("newer", timedelta(days=30))

    def test_newer_than_synonym_canonicalises(self):
        assert ast_of("newer_than:3d") == Term("newer", timedelta(days=3))

    def test_older_than_synonym_canonicalises(self):
        assert ast_of("older_than:7d") == Term("older", timedelta(days=7))

    def test_invalid_relative_date_raises(self):
        with pytest.raises(QuerySyntaxError, match="Invalid relative date"):
            parse("newer:abc")

    def test_larger_megabyte(self):
        assert ast_of("larger:1M") == Term("larger", 1048576)

    def test_larger_bare_bytes(self):
        assert ast_of("larger:1048576") == Term("larger", 1048576)

    def test_smaller_kilobytes(self):
        assert ast_of("smaller:500k") == Term("smaller", 512000)

    def test_invalid_size_raises(self):
        with pytest.raises(QuerySyntaxError, match="Invalid size"):
            parse("larger:huge")

    def test_msgid_bare(self):
        assert ast_of("msgid:abc@host") == Term("msgid", "abc@host")

    def test_msgid_angle_brackets_stripped(self):
        assert ast_of("msgid:<abc@host>") == Term("msgid", "abc@host")

    def test_rfc822msgid_synonym_canonicalises(self):
        assert ast_of("rfc822msgid:<abc@host>") == Term("msgid", "abc@host")

    def test_quoted_msgid_canonicalises_from_the_value(self):
        """The quotes never reach the value, so the id normalises clean."""
        assert ast_of('msgid:"<abc@host>"') == Term("msgid", "abc@host")

    def test_quoted_date_value_is_still_typed(self):
        assert ast_of('after:"2025-03-01"') == Term("after", date(2025, 3, 1))


class TestIsFlags:
    """Every is: spelling maps onto its flag state."""

    @pytest.mark.parametrize(
        "keyword,flag",
        [
            ("unread", Flag.UNREAD),
            ("read", Flag.READ),
            ("flagged", Flag.FLAGGED),
            ("starred", Flag.FLAGGED),
            ("unflagged", Flag.UNFLAGGED),
            ("unstarred", Flag.UNFLAGGED),
            ("answered", Flag.ANSWERED),
            ("unanswered", Flag.UNANSWERED),
        ],
    )
    def test_keyword_maps_to_flag(self, keyword, flag):
        assert ast_of(f"is:{keyword}") == Term("is", flag)

    def test_keyword_is_case_insensitive(self):
        assert ast_of("is:UNREAD") == Term("is", Flag.UNREAD)

    def test_unknown_is_keyword_raises(self):
        with pytest.raises(QuerySyntaxError, match="Unknown is: keyword"):
            parse("is:bogus")


class TestStandaloneKeywords:
    """Preset keywords apply only when they are the whole query."""

    @pytest.mark.parametrize("keyword", ["all", "today", "yesterday", "week", "month"])
    def test_whole_query_keyword(self, keyword):
        assert ast_of(keyword) == Term(OP_KEYWORD, keyword)

    def test_keyword_is_case_insensitive(self):
        assert ast_of("Today") == Term(OP_KEYWORD, "today")
        assert ast_of("ALL") == Term(OP_KEYWORD, "all")

    def test_keyword_inside_a_larger_query_is_a_word(self):
        assert ast_of("today from:alice") == And((w("today"), Term("from", "alice")))

    def test_quoted_keyword_is_a_phrase(self):
        assert ast_of('"today"') == Term(OP_PHRASE, "today")


class TestPrecedence:
    """Tightest first: dash/not, then adjacent or, then juxtaposition."""

    def test_plan_counterexample_verbatim(self):
        """from:alice subject:invoice or from:bob groups the or tightly."""
        assert ast_of("from:alice subject:invoice or from:bob") == And(
            (
                Term("from", "alice"),
                Or((Term("subject", "invoice"), Term("from", "bob"))),
            )
        )

    def test_simple_or(self):
        assert ast_of("from:alice or from:bob") == Or(
            (Term("from", "alice"), Term("from", "bob"))
        )

    def test_chained_or_is_one_nary_node(self):
        assert ast_of("a or b or c") == Or((w("a"), w("b"), w("c")))

    def test_or_is_case_insensitive(self):
        assert ast_of("from:alice OR from:bob") == Or(
            (Term("from", "alice"), Term("from", "bob"))
        )

    def test_or_group_then_and(self):
        assert ast_of("from:alice or from:bob subject:invoice") == And(
            (
                Or((Term("from", "alice"), Term("from", "bob"))),
                Term("subject", "invoice"),
            )
        )

    def test_negation_binds_tighter_than_or(self):
        assert ast_of("-is:read or from:bob") == Or(
            (Not(Term("is", Flag.READ)), Term("from", "bob"))
        )

    def test_parenthesised_or_flattens_by_associativity(self):
        assert ast_of("(a or b) or c") == Or((w("a"), w("b"), w("c")))

    def test_grouped_and_flattens_by_associativity(self):
        assert ast_of("(a b) c") == And((w("a"), w("b"), w("c")))


class TestNegation:
    """Dash and not negate the next unary; groups negate whole."""

    def test_dash_bare_word(self):
        assert ast_of("-spam") == Not(w("spam"))

    def test_dash_prefix_term(self):
        assert ast_of("-from:alice") == Not(Term("from", "alice"))

    def test_dash_in_scope(self):
        assert ast_of("-in:trash") == Not(Term("in", "trash"))

    def test_dash_scope_beside_a_term(self):
        assert ast_of("-in:trash from:alice") == And(
            (Not(Term("in", "trash")), Term("from", "alice"))
        )

    def test_not_keyword(self):
        assert ast_of("not is:read") == Not(Term("is", Flag.READ))

    def test_not_keyword_case_insensitive(self):
        assert ast_of("NOT from:alice") == Not(Term("from", "alice"))

    def test_dash_negates_paren_group(self):
        assert ast_of("-(a b)") == Not(And((w("a"), w("b"))))

    def test_dash_negates_brace_group(self):
        assert ast_of("-{a b}") == Not(Or((w("a"), w("b"))))

    def test_dash_negates_phrase(self):
        assert ast_of('-"exact phrase"') == Not(Term(OP_PHRASE, "exact phrase"))

    def test_negation_nests_inside_a_negated_group(self):
        assert ast_of("-(-a b)") == Not(And((Not(w("a")), w("b"))))

    def test_negated_label_keeps_its_operand(self):
        """T11 regression shape: negation always has a structural operand."""
        assert ast_of("-label:promo") == Not(Term("label", "promo"))

    def test_word_then_negated_word(self):
        assert ast_of("invoice -draft") == And((w("invoice"), Not(w("draft"))))

    def test_dash_followed_by_space_is_a_word(self):
        assert ast_of("- spam") == And((w("-"), w("spam")))

    def test_double_dash_is_double_negation(self):
        assert ast_of("--spam") == Not(Not(w("spam")))

    def test_dangling_not_raises(self):
        with pytest.raises(QuerySyntaxError, match="'not'"):
            parse("not")

    def test_not_at_end_raises(self):
        with pytest.raises(QuerySyntaxError, match="'not'"):
            parse("from:alice not")


class TestParenGroups:
    """Parentheses group subqueries; the issue #58 shape parses."""

    def test_or_group_of_words(self):
        assert ast_of("(ticket OR booking)") == Or((w("ticket"), w("booking")))

    def test_issue_58_shape(self):
        assert ast_of("after:2026-07-13 (ticket OR booking OR flight)") == And(
            (
                Term("after", date(2026, 7, 13)),
                Or((w("ticket"), w("booking"), w("flight"))),
            )
        )

    def test_nested_singleton_parens_unwrap(self):
        assert ast_of("((a))") == w("a")

    def test_empty_parens_raise(self):
        with pytest.raises(QuerySyntaxError, match="[Ee]mpty"):
            parse("()")

    def test_missing_close_paren_raises(self):
        with pytest.raises(QuerySyntaxError, match=r"\)"):
            parse("(ticket OR booking")

    def test_stray_close_paren_raises(self):
        with pytest.raises(QuerySyntaxError, match=r"\)"):
            parse("ticket)")

    def test_mismatched_closers_raise(self):
        with pytest.raises(QuerySyntaxError):
            parse("(a}")


class TestBraceGroups:
    """Braces are the Gmail any-of group: OR of their contents."""

    def test_brace_of_words(self):
        assert ast_of("{jetstar qantas}") == Or((w("jetstar"), w("qantas")))

    def test_brace_of_prefix_terms(self):
        assert ast_of("{from:a to:b}") == Or((Term("from", "a"), Term("to", "b")))

    def test_brace_with_inner_or_flattens(self):
        assert ast_of("{a or b c}") == Or((w("a"), w("b"), w("c")))

    def test_singleton_brace_unwraps(self):
        assert ast_of("{a}") == w("a")

    def test_empty_braces_raise(self):
        with pytest.raises(QuerySyntaxError, match="[Ee]mpty"):
            parse("{}")

    def test_missing_close_brace_raises(self):
        with pytest.raises(QuerySyntaxError, match=r"\}"):
            parse("{jetstar qantas")

    def test_stray_close_brace_raises(self):
        with pytest.raises(QuerySyntaxError, match=r"\}"):
            parse("qantas}")


class TestValueGroups:
    """prefix:(...) distributes the prefix over the grouped values."""

    def test_space_group_is_and(self):
        assert ast_of("subject:(a b)") == And(
            (Term("subject", "a"), Term("subject", "b"))
        )

    def test_or_group_is_or(self):
        assert ast_of("subject:(a OR b)") == Or(
            (Term("subject", "a"), Term("subject", "b"))
        )

    def test_singleton_group_unwraps(self):
        assert ast_of("subject:(a)") == Term("subject", "a")

    def test_three_value_or_group_is_one_nary_node(self):
        assert ast_of("subject:(a OR b OR c)") == Or(
            (Term("subject", "a"), Term("subject", "b"), Term("subject", "c"))
        )

    def test_quoted_phrase_value(self):
        assert ast_of('subject:("exact phrase" OR urgent)') == Or(
            (Term("subject", "exact phrase"), Term("subject", "urgent"))
        )

    def test_flag_values_distribute(self):
        assert ast_of("is:(unread OR flagged)") == Or(
            (Term("is", Flag.UNREAD), Term("is", Flag.FLAGGED))
        )

    def test_unprefixed_colon_word_is_a_plain_value(self):
        assert ast_of("subject:(re: hello)") == And(
            (Term("subject", "re:"), Term("subject", "hello"))
        )

    def test_mixed_and_or_raises(self):
        with pytest.raises(QuerySyntaxError, match="value group"):
            parse("subject:(a b OR c)")

    def test_nested_prefix_raises(self):
        with pytest.raises(QuerySyntaxError, match="value group"):
            parse("subject:(from:alice)")

    def test_negation_inside_group_raises(self):
        with pytest.raises(QuerySyntaxError, match="value group"):
            parse("subject:(a -b)")

    def test_empty_group_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Ee]mpty"):
            parse("subject:()")

    def test_missing_close_raises(self):
        with pytest.raises(QuerySyntaxError, match=r"\)"):
            parse("subject:(a b")


class TestQuoting:
    """Quoted values, escapes, and the unbalanced-quote hard error."""

    def test_quoted_prefix_value(self):
        assert ast_of('subject:"hotel booking"') == Term("subject", "hotel booking")

    def test_escaped_quote_inside_phrase(self):
        assert ast_of('subject:"a \\" b"') == Term("subject", 'a " b')

    def test_unbalanced_quote_in_value_raises(self):
        """The old shlex fallback kept the quote as literal text and
        produced a silent false-empty result; the grammar refuses instead."""
        with pytest.raises(QuerySyntaxError, match="[Qq]uote"):
            parse('subject:"unbalanced meeting')

    def test_unbalanced_bare_quote_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Qq]uote"):
            parse('"unclosed')

    def test_empty_phrase_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Ee]mpty"):
            parse('""')


class TestEmptyValues:
    """A prefix without a value is an error naming the operator."""

    def test_trailing_empty_value(self):
        with pytest.raises(QuerySyntaxError, match="from:"):
            parse("from:")

    def test_no_joining_across_whitespace(self):
        """from: alice is the same error, never from:alice."""
        with pytest.raises(QuerySyntaxError, match="from:"):
            parse("from: alice")

    def test_empty_quoted_value(self):
        with pytest.raises(QuerySyntaxError, match="subject:"):
            parse('subject:""')

    def test_empty_value_before_other_terms(self):
        with pytest.raises(QuerySyntaxError, match="subject:"):
            parse("subject: is:unread")


class TestNearMissPrefixes:
    """One edit away from a real operator refuses with the correction."""

    def test_transposed_prefix_suggests(self):
        with pytest.raises(QuerySyntaxError, match="from") as excinfo:
            parse("form:alice")
        assert "from" in excinfo.value.suggestions

    def test_substituted_prefix_suggests(self):
        with pytest.raises(QuerySyntaxError, match="subject") as excinfo:
            parse("sublect:hello")
        assert "subject" in excinfo.value.suggestions

    def test_url_is_not_a_near_miss(self):
        assert ast_of("https://example.com") == w("https://example.com")

    def test_unknown_prefix_with_quoted_value_refuses(self):
        """An attached quote makes the token operator-shaped, so it
        refuses instead of degrading to text."""
        with pytest.raises(QuerySyntaxError, match="[Uu]nknown"):
            parse('xyz:"foo bar"')

    def test_unknown_prefix_with_value_group_refuses(self):
        with pytest.raises(QuerySyntaxError, match="[Uu]nknown"):
            parse("xyz:(a b)")


class TestTreatedAsText:
    """Colon words that are not operators stay words and are recorded."""

    def test_url_is_recorded(self):
        result = parse("https://example.com")
        assert result.ast == w("https://example.com")
        assert result.treated_as_text == ("https://example.com",)

    def test_clock_time_is_recorded(self):
        result = parse("meeting 10:30")
        assert result.ast == And((w("meeting"), w("10:30")))
        assert result.treated_as_text == ("10:30",)

    def test_plain_words_are_not_recorded(self):
        assert parse("meeting notes").treated_as_text == ()

    def test_known_operator_is_not_recorded(self):
        assert parse("label:work").treated_as_text == ()

    def test_order_is_preserved(self):
        result = parse("https://a.example b:c")
        assert result.treated_as_text == ("https://a.example", "b:c")


class TestImapPlacement:
    """Leading imap: bypasses the grammar; anywhere else it refuses."""

    def test_leading_imap_is_raw(self):
        assert ast_of("imap:UNSEEN") == Term(OP_IMAP, "UNSEEN")

    def test_raw_expression_kept_verbatim(self):
        assert ast_of('imap:OR TEXT "Edinburgh" TEXT "Berlin"') == Term(
            OP_IMAP, 'OR TEXT "Edinburgh" TEXT "Berlin"'
        )

    def test_leading_imap_case_insensitive(self):
        assert ast_of("IMAP:UNSEEN") == Term(OP_IMAP, "UNSEEN")

    def test_space_after_colon_is_tolerated_when_leading(self):
        assert ast_of("imap: UNSEEN") == Term(OP_IMAP, "UNSEEN")

    def test_empty_raw_expression_raises(self):
        with pytest.raises(QuerySyntaxError, match="imap:"):
            parse("imap:")

    def test_imap_after_a_word_raises(self):
        with pytest.raises(QuerySyntaxError, match="imap:"):
            parse("foo imap:RAW")

    def test_imap_inside_parens_raises(self):
        with pytest.raises(QuerySyntaxError, match="imap:"):
            parse("(imap:UNSEEN)")

    def test_negated_imap_raises(self):
        with pytest.raises(QuerySyntaxError, match="imap:"):
            parse("-imap:UNSEEN")

    def test_not_imap_raises(self):
        with pytest.raises(QuerySyntaxError, match="imap:"):
            parse("not imap:UNSEEN")


class TestOrErrors:
    """or needs a term on each side; edges and doubles refuse."""

    def test_only_or_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Oo]r"):
            parse("or")

    def test_leading_or_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Oo]r"):
            parse("or from:alice")

    def test_dangling_or_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Oo]r"):
            parse("from:alice or")

    def test_consecutive_or_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Oo]r"):
            parse("a or or b")

    def test_or_against_group_close_raises(self):
        with pytest.raises(QuerySyntaxError, match="[Oo]r"):
            parse("(a or)")

    def test_query_syntax_error_is_a_value_error(self):
        """Callers that guarded the old parser's ValueError keep working."""
        with pytest.raises(ValueError):
            parse("or")
