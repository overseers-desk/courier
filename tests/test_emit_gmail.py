"""Goldens for the Gmail emitter: one per registry row and refusal.

The Gmail emitter renders X-GM-RAW in Gmail's own dialect (dialect
normalisation happens here, never on the raw query string) and emits
the is:answered family as standard keys beside it. Braces never
appear in the output.
"""

from datetime import datetime

import pytest

from courier.query import UntranslatableForBackend, parse
from courier.query.emit_gmail import emit

NOW = datetime(2026, 7, 15, 12, 0, 0)


def raw_of(query: str, **kwargs) -> str:
    """Emit a query and return the decoded X-GM-RAW string."""
    criteria = emit(parse(query), now=NOW, **kwargs).criteria
    assert criteria[0] == b"X-GM-RAW", criteria
    return criteria[1].decode("utf-8")


def emission_of(query: str, **kwargs):
    """Emit a query with the shared reference instant."""
    return emit(parse(query), now=NOW, **kwargs)


class TestNativeOperators:
    """Rows Gmail speaks natively pass through in its dialect."""

    def test_from(self):
        assert raw_of("from:alice") == "from:alice"

    def test_to(self):
        assert raw_of("to:bob") == "to:bob"

    def test_cc(self):
        assert raw_of("cc:team") == "cc:team"

    def test_bcc(self):
        assert raw_of("bcc:carol@x") == "bcc:carol@x"

    def test_subject(self):
        assert raw_of("subject:invoice") == "subject:invoice"

    def test_quoted_value(self):
        assert raw_of('subject:"hotel booking"') == 'subject:"hotel booking"'

    def test_single_quoted_value(self):
        assert raw_of("from:'Alice Smith'") == 'from:"Alice Smith"'

    def test_has_attachment(self):
        assert raw_of("has:attachment") == "has:attachment"

    def test_has_other_values_pass_verbatim(self):
        assert raw_of("has:drive") == "has:drive"

    def test_filename(self):
        assert raw_of("filename:report.pdf") == "filename:report.pdf"

    def test_list(self):
        assert raw_of("list:announce.example.com") == "list:announce.example.com"

    def test_deliveredto(self):
        assert raw_of("deliveredto:me@example.com") == "deliveredto:me@example.com"

    def test_label(self):
        assert raw_of("label:work") == "label:work"

    def test_category(self):
        assert raw_of("category:promotions") == "category:promotions"

    def test_in(self):
        assert raw_of("in:anywhere") == "in:anywhere"

    def test_negated_in_passes_natively(self):
        assert raw_of("-in:trash from:alice") == "-in:trash from:alice"


class TestWordsAndPhrases:
    """Words stay verbatim; phrases quote; URL-words quote to stay
    literal."""

    def test_words(self):
        assert raw_of("meeting notes") == "meeting notes"

    def test_phrase(self):
        assert raw_of('"meeting notes"') == '"meeting notes"'

    def test_colon_word_is_quoted_literal(self):
        emission = emission_of("https://example.com/x")
        assert emission.criteria == [b"X-GM-RAW", b'"https://example.com/x"']
        assert emission.report.treated_as_text == ["https://example.com/x"]


class TestBodyNormalisation:
    """Gmail has no body:; the value demotes to plain text with a
    report note."""

    def test_body_word(self):
        emission = emission_of("body:hello")
        assert emission.criteria == [b"X-GM-RAW", b"hello"]
        assert any("body:" in note for note in emission.report.approximations)

    def test_body_phrase(self):
        assert raw_of('body:"notes ready"') == '"notes ready"'


class TestFlags:
    """Native is: spellings; flagged family maps onto starred."""

    def test_read(self):
        assert raw_of("is:read") == "is:read"

    def test_unread(self):
        assert raw_of("is:unread") == "is:unread"

    def test_flagged_is_starred(self):
        assert raw_of("is:flagged") == "is:starred"

    def test_starred(self):
        assert raw_of("is:starred") == "is:starred"

    def test_unflagged_is_negated_starred(self):
        assert raw_of("is:unflagged") == "-is:starred"

    def test_unstarred(self):
        assert raw_of("is:unstarred") == "-is:starred"

    def test_negating_unflagged_wraps_the_dash(self):
        assert raw_of("-is:unflagged") == "-(-is:starred)"


class TestHybridStandardKeys:
    """The answered family emits as standard keys beside X-GM-RAW."""

    def test_answered_alone_is_standard_only(self):
        assert emission_of("is:answered").criteria == [b"ANSWERED"]

    def test_unanswered_alone(self):
        assert emission_of("is:unanswered").criteria == [b"UNANSWERED"]

    def test_negated_answered(self):
        assert emission_of("-is:answered").criteria == [b"NOT", b"ANSWERED"]

    def test_hybrid_beside_raw(self):
        emission = emission_of("from:alice is:answered")
        assert emission.criteria == [b"X-GM-RAW", b"from:alice", b"ANSWERED"]

    def test_hybrid_with_negation(self):
        emission = emission_of("from:alice -is:unanswered")
        assert emission.criteria == [
            b"X-GM-RAW",
            b"from:alice",
            b"NOT",
            b"UNANSWERED",
        ]


class TestDates:
    """Gmail dates in YYYY/MM/DD; on: becomes an after/before pair."""

    def test_after(self):
        assert raw_of("after:2026-07-13") == "after:2026/07/13"

    def test_before(self):
        assert raw_of("before:2026-04-01") == "before:2026/04/01"

    def test_on_becomes_a_pair(self):
        assert raw_of("on:2026-07-01") == "(after:2026/07/01 before:2026/07/02)"

    def test_on_pair_stays_grouped_under_negation(self):
        assert raw_of("-on:2026-07-01") == "-(after:2026/07/01 before:2026/07/02)"

    def test_newer_resolves_to_absolute(self):
        emission = emission_of("newer:2w")
        assert emission.criteria == [b"X-GM-RAW", b"after:2026/07/01"]
        assert any("absolute" in note for note in emission.report.approximations)

    def test_older_resolves_to_absolute(self):
        assert raw_of("older:3d") == "before:2026/07/12"

    def test_timezone_note_recorded(self):
        emission = emission_of("after:2026-07-13")
        assert any("timezone" in note for note in emission.report.approximations)


class TestSizes:
    """Computed byte counts, never unit suffixes."""

    def test_larger(self):
        assert raw_of("larger:1M") == "larger:1048576"

    def test_smaller(self):
        assert raw_of("smaller:500k") == "smaller:512000"


class TestMsgid:
    """rfc822msgid: is built from the AST value (the T12 fix)."""

    def test_msgid_strips_angle_brackets(self):
        assert raw_of("msgid:<abc@host>") == "rfc822msgid:abc@host"

    def test_rfc822msgid_synonym(self):
        assert raw_of("rfc822msgid:abc@host") == "rfc822msgid:abc@host"

    def test_quoted_msgid_canonicalises_from_the_value(self):
        """The old rewrite ran on the raw pre-shlex string and kept
        the quotes; the AST value carries none."""
        assert raw_of('msgid:"<abc@host>"') == "rfc822msgid:abc@host"

    def test_negated_msgid(self):
        assert raw_of("-msgid:<abc@host>") == "-rfc822msgid:abc@host"


class TestKeywords:
    """Standalone keywords resolve against the reference instant."""

    def test_all_is_standard_all(self):
        assert emission_of("all").criteria == [b"ALL"]

    def test_empty_query_is_all(self):
        assert emission_of("").criteria == [b"ALL"]

    def test_today(self):
        assert raw_of("today") == "after:2026/07/15"

    def test_yesterday(self):
        assert raw_of("yesterday") == "(after:2026/07/14 before:2026/07/15)"

    def test_week(self):
        assert raw_of("week") == "after:2026/07/08"

    def test_month(self):
        assert raw_of("month") == "after:2026/06/15"


class TestBooleans:
    """Or canonicalises to parenthesized OR groups; braces never
    appear in the output."""

    def test_or_group(self):
        assert raw_of("from:alice or from:bob") == "(from:alice OR from:bob)"

    def test_brace_group_becomes_or_group(self):
        assert raw_of("{from:a subject:b}") == "(from:a OR subject:b)"

    def test_not_under_or_from_braces(self):
        """The grammar permits Not inside braces; Gmail gets an OR
        group, never braces."""
        assert raw_of("{from:a -subject:b}") == "(from:a OR -subject:b)"

    def test_and_operand_inside_or_parenthesizes(self):
        assert raw_of("(from:a subject:b) or from:c") == (
            "((from:a subject:b) OR from:c)"
        )

    def test_negated_group(self):
        assert raw_of("-(from:a subject:b)") == "-(from:a subject:b)"

    def test_precedence_counterexample(self):
        assert raw_of("from:alice subject:invoice or from:bob") == (
            "from:alice (subject:invoice OR from:bob)"
        )

    def test_no_braces_ever_emitted(self):
        for query in ("{a b}", "-{from:a subject:b}", "{a {b c}}"):
            assert "{" not in raw_of(query)


class TestWorldAsOfBound:
    """The second-precision epoch clause reproduces inside X-GM-RAW."""

    def test_bound_appends_epoch_before(self):
        bound = datetime(2026, 7, 14, 9, 30, 0)
        emission = emission_of("from:alice", world_as_of=bound)
        expected = f"from:alice before:{int(bound.timestamp())}"
        assert emission.criteria == [b"X-GM-RAW", expected.encode("utf-8")]
        assert any("WORLD_AS_OF" in note for note in emission.report.approximations)

    def test_bound_alone_still_emits_raw(self):
        bound = datetime(2026, 7, 14, 9, 30, 0)
        emission = emission_of("all", world_as_of=bound)
        expected = f"before:{int(bound.timestamp())}"
        assert emission.criteria == [b"X-GM-RAW", expected.encode("utf-8")]

    def test_bound_beside_standard_keys(self):
        bound = datetime(2026, 7, 14, 9, 30, 0)
        emission = emission_of("is:answered", world_as_of=bound)
        expected = f"before:{int(bound.timestamp())}"
        assert emission.criteria == [
            b"X-GM-RAW",
            expected.encode("utf-8"),
            b"ANSWERED",
        ]

    def test_bound_lands_outside_or_groups(self):
        bound = datetime(2026, 7, 14, 9, 30, 0)
        raw = raw_of("from:a or from:b", world_as_of=bound)
        assert raw == f"(from:a OR from:b) before:{int(bound.timestamp())}"


class TestCharset:
    """UTF-8 charset only when the raw string needs it."""

    def test_ascii_raw_has_no_charset(self):
        assert emission_of("from:alice").charset is None

    def test_non_ascii_raw_sets_charset(self):
        emission = emission_of("from:josé")
        assert emission.criteria == [b"X-GM-RAW", "from:josé".encode("utf-8")]
        assert emission.charset == "UTF-8"


class TestRefusals:
    """Refusal goldens: exact messages with the nearest alternative."""

    def test_or_spanning_families_refuses_with_split_suggestion(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("is:answered or from:alice")
        assert str(exc.value) == (
            "is:answered cannot be expressed on the gmail backend: "
            "is:answered/is:unanswered has no Gmail spelling, so it cannot "
            "sit under or/not groups beside Gmail-dialect terms. Run "
            "is:answered / is:unanswered as its own search and combine the "
            "results yourself; Gmail expresses that family only as a "
            "standard IMAP key, which can only be AND-ed beside the rest "
            "of the query."
        )

    def test_answered_under_negated_group_refuses(self):
        with pytest.raises(UntranslatableForBackend, match="split|combine"):
            emission_of("-(is:answered from:alice)")

    def test_embedded_double_quote_refuses(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of('subject:"say \\" hi"')
        assert str(exc.value) == (
            "subject: cannot be expressed on the gmail backend: Gmail "
            "defines no escape for a double quote inside a quoted value. "
            "Drop the quote character from the search text, or search "
            "another backend."
        )

    def test_embedded_quote_in_phrase_refuses(self):
        with pytest.raises(UntranslatableForBackend, match="no escape"):
            emission_of('"club \\"42\\""')

    def test_control_characters_refuse(self):
        with pytest.raises(UntranslatableForBackend, match="control characters"):
            emission_of('subject:"evil\r\ntext"')

    def test_imap_raw_refuses(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("imap:OR TEXT foo SUBJECT bar")
        assert str(exc.value) == (
            "imap: cannot be expressed on the gmail backend: raw IMAP "
            "expressions never travel inside X-GM-RAW. The raw query runs "
            "on the standard IMAP search path instead."
        )

    def test_refusal_reason_tag(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("imap:UNSEEN")
        assert exc.value.reason == "untranslatable"
        assert exc.value.backend == "gmail"


class TestReport:
    """The emission report carries dialect and approximations."""

    def test_dialect(self):
        assert emission_of("from:alice").report.dialect == "gmail"

    def test_exact_translation_has_no_notes(self):
        assert emission_of("from:alice is:read").report.approximations == []

    def test_regression_t1_query_normalises(self):
        """The T1 row: on:, body:, newer:, is:answered are not Gmail
        grammar; each must normalise instead of shipping verbatim."""
        emission = emission_of("on:2026-07-01 body:hello newer:2w is:answered")
        assert emission.criteria == [
            b"X-GM-RAW",
            b"(after:2026/07/01 before:2026/07/02) hello after:2026/07/01",
            b"ANSWERED",
        ]


class TestDateExtremes:
    """Degenerate dates refuse instead of escaping as OverflowError."""

    def test_on_date_max_refuses(self):
        with pytest.raises(UntranslatableForBackend):
            emit(parse("on:9999-12-31"), now=NOW)

    def test_year_under_1000_zero_pads(self):
        emission = emit(parse("before:0999-01-02"), now=NOW)
        assert b"before:0999/01/02" in emission.criteria[1]
