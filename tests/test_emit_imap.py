"""Goldens and wire-bytes tests for the generic IMAP emitter.

One golden per registry row and one per refusal message, plus
wire-bytes tests (mocked socket) proving the criteria survive
imapclient 3.1.0's normalisation: charset only when needed, literals
for non-ASCII values, and no closing paren glued onto a trailing
8-bit literal.
"""

from datetime import date, datetime
from unittest import mock

import pytest
from imapclient import IMAPClient

from courier.query import UntranslatableForBackend, parse
from courier.query.emit_imap import emit

NOW = datetime(2026, 7, 15, 12, 0, 0)


def criteria_of(query: str, **kwargs):
    """Emit a query and return only the criteria list."""
    return emit(parse(query), now=NOW, **kwargs).criteria


def emission_of(query: str, **kwargs):
    """Emit a query with the shared reference instant."""
    return emit(parse(query), now=NOW, **kwargs)


class TestDirectKeys:
    """Header operators map onto their native RFC 3501 keys."""

    def test_from(self):
        assert criteria_of("from:alice") == [b"FROM", b"alice"]

    def test_to(self):
        assert criteria_of("to:bob") == [b"TO", b"bob"]

    def test_cc(self):
        assert criteria_of("cc:team") == [b"CC", b"team"]

    def test_bcc(self):
        emission = emission_of("bcc:carol@x")
        assert emission.criteria == [b"BCC", b"carol@x"]
        assert any("sent copies" in note for note in emission.report.approximations)

    def test_subject(self):
        assert criteria_of("subject:invoice") == [b"SUBJECT", b"invoice"]

    def test_body(self):
        assert criteria_of("body:hello") == [b"BODY", b"hello"]

    def test_quoted_value_stays_one_value(self):
        assert criteria_of('from:"Alice Smith"') == [b"FROM", b"Alice Smith"]

    def test_single_quoted_value_stays_one_value(self):
        assert criteria_of("from:'Alice Smith'") == [b"FROM", b"Alice Smith"]


class TestWordsAndPhrases:
    """Bare words emit one TEXT key each; phrases stay one TEXT key."""

    def test_single_word(self):
        emission = emission_of("meeting")
        assert emission.criteria == [b"TEXT", b"meeting"]
        assert any("substring" in note for note in emission.report.approximations)

    def test_words_emit_one_text_key_each(self):
        assert criteria_of("meeting notes") == [
            b"TEXT",
            b"meeting",
            b"TEXT",
            b"notes",
        ]

    def test_phrase_is_one_text_key(self):
        emission = emission_of('"meeting notes"')
        assert emission.criteria == [b"TEXT", b"meeting notes"]
        assert any("contiguous" in note for note in emission.report.approximations)

    def test_treated_as_text_token_reaches_report(self):
        emission = emission_of("see https://example.com/x today-notes")
        assert emission.report.treated_as_text == ["https://example.com/x"]


class TestFlags:
    """Every is: state maps onto its native flag key."""

    @pytest.mark.parametrize(
        "keyword,expected",
        [
            ("read", b"SEEN"),
            ("unread", b"UNSEEN"),
            ("flagged", b"FLAGGED"),
            ("starred", b"FLAGGED"),
            ("unflagged", b"UNFLAGGED"),
            ("unstarred", b"UNFLAGGED"),
            ("answered", b"ANSWERED"),
            ("unanswered", b"UNANSWERED"),
        ],
    )
    def test_flag(self, keyword, expected):
        assert criteria_of(f"is:{keyword}") == [expected]


class TestDates:
    """Absolute dates map to SINCE/BEFORE/ON with typed date values."""

    def test_after(self):
        emission = emission_of("after:2026-07-13")
        assert emission.criteria == [b"SINCE", date(2026, 7, 13)]
        assert any("INTERNALDATE" in note for note in emission.report.approximations)

    def test_before(self):
        assert criteria_of("before:2026-04-01") == [b"BEFORE", date(2026, 4, 1)]

    def test_on(self):
        assert criteria_of("on:2026-03-15") == [b"ON", date(2026, 3, 15)]


class TestRelativeDates:
    """newer:/older: use WITHIN when eligible, else whole-day bounds."""

    def test_newer_computed_by_default(self):
        assert criteria_of("newer:3d") == [b"SINCE", date(2026, 7, 12)]

    def test_older_computed_by_default(self):
        assert criteria_of("older:2w") == [b"BEFORE", date(2026, 7, 1)]

    def test_newer_than_synonym(self):
        assert criteria_of("newer_than:3d") == [b"SINCE", date(2026, 7, 12)]

    def test_older_than_synonym(self):
        assert criteria_of("older_than:2w") == [b"BEFORE", date(2026, 7, 1)]

    def test_month_unit_is_thirty_days(self):
        assert criteria_of("newer:1m") == [b"SINCE", date(2026, 6, 15)]

    def test_newer_uses_younger_with_within(self):
        assert criteria_of("newer:3d", supports_within=True) == [
            b"YOUNGER",
            259200,
        ]

    def test_older_uses_older_with_within(self):
        assert criteria_of("older:2w", supports_within=True) == [
            b"OLDER",
            1209600,
        ]

    def test_bounded_client_skips_within(self):
        """A WORLD_AS_OF bound cannot use the server's own clock."""
        emission = emission_of("newer:3d", supports_within=True, bounded=True)
        assert emission.criteria == [b"SINCE", date(2026, 7, 12)]
        assert any("WORLD_AS_OF" in note for note in emission.report.approximations)


class TestSizes:
    """larger:/smaller: emit native keys with computed byte counts."""

    def test_larger(self):
        assert criteria_of("larger:1M") == [b"LARGER", 1048576]

    def test_smaller(self):
        assert criteria_of("smaller:500k") == [b"SMALLER", 512000]

    def test_bare_byte_count(self):
        assert criteria_of("larger:12345") == [b"LARGER", 12345]


class TestHeaderOperators:
    """msgid:, list:, and deliveredto: map to HEADER searches."""

    def test_msgid(self):
        assert criteria_of("msgid:<abc@host>") == [
            b"HEADER",
            b"Message-ID",
            b"abc@host",
        ]

    def test_rfc822msgid_synonym(self):
        assert criteria_of("rfc822msgid:abc@host") == [
            b"HEADER",
            b"Message-ID",
            b"abc@host",
        ]

    def test_list(self):
        assert criteria_of("list:announce.example.com") == [
            b"HEADER",
            b"List-Id",
            b"announce.example.com",
        ]

    def test_deliveredto(self):
        assert criteria_of("deliveredto:me@example.com") == [
            b"HEADER",
            b"Delivered-To",
            b"me@example.com",
        ]


class TestKeywords:
    """Standalone keywords resolve against the reference instant."""

    def test_all(self):
        assert criteria_of("all") == [b"ALL"]

    def test_empty_query_is_all(self):
        assert criteria_of("") == [b"ALL"]

    def test_today(self):
        assert criteria_of("today") == [b"SINCE", date(2026, 7, 15)]

    def test_yesterday(self):
        assert criteria_of("yesterday") == [
            b"SINCE",
            date(2026, 7, 14),
            b"BEFORE",
            date(2026, 7, 15),
        ]

    def test_week(self):
        assert criteria_of("week") == [b"SINCE", date(2026, 7, 8)]

    def test_month(self):
        assert criteria_of("month") == [b"SINCE", date(2026, 6, 15)]


class TestRawPassthrough:
    """imap: ships the expression through with shlex token splitting."""

    def test_raw_tokens(self):
        assert criteria_of("imap:OR TEXT foo SUBJECT bar") == [
            b"OR",
            b"TEXT",
            b"foo",
            b"SUBJECT",
            b"bar",
        ]

    def test_raw_respects_quoting(self):
        assert criteria_of('imap:TEXT "hello world"') == [
            b"TEXT",
            b"hello world",
        ]

    def test_raw_non_ascii_sets_charset(self):
        emission = emission_of("imap:FROM josé")
        assert emission.criteria == [b"FROM", "josé".encode("utf-8")]
        assert emission.charset == "UTF-8"


class TestBooleans:
    """AND is juxtaposition, OR right-folds to binary, NOT prefixes."""

    def test_and_is_flat_juxtaposition(self):
        assert criteria_of("from:alice subject:invoice") == [
            b"FROM",
            b"alice",
            b"SUBJECT",
            b"invoice",
        ]

    def test_binary_or(self):
        assert criteria_of("from:alice or from:bob") == [
            b"OR",
            b"FROM",
            b"alice",
            b"FROM",
            b"bob",
        ]

    def test_nary_or_right_folds(self):
        """The #35 regression: OR is binary, so n-ary must fold."""
        assert criteria_of("ticket or booking or e-ticket") == [
            b"OR",
            b"TEXT",
            b"ticket",
            b"OR",
            b"TEXT",
            b"booking",
            b"TEXT",
            b"e-ticket",
        ]

    def test_issue_58_query(self):
        """The #58 regression: parens group instead of passing through
        as literal words."""
        assert criteria_of("after:2026-07-13 (ticket OR booking OR e-ticket)") == [
            b"SINCE",
            date(2026, 7, 13),
            b"OR",
            b"TEXT",
            b"ticket",
            b"OR",
            b"TEXT",
            b"booking",
            b"TEXT",
            b"e-ticket",
        ]

    def test_not_single_key_stays_flat(self):
        assert criteria_of("-from:alice") == [b"NOT", b"FROM", b"alice"]

    def test_not_word(self):
        """The T7 regression: -draft negates instead of searching for
        the literal token."""
        assert criteria_of("invoice -draft") == [
            b"TEXT",
            b"invoice",
            b"NOT",
            b"TEXT",
            b"draft",
        ]

    def test_not_group_nests(self):
        assert criteria_of("-(from:alice subject:x)") == [
            b"NOT",
            [b"FROM", b"alice", b"SUBJECT", b"x"],
        ]

    def test_or_operand_with_two_keys_nests(self):
        assert criteria_of("(from:alice subject:x) or from:bob") == [
            b"OR",
            [b"FROM", b"alice", b"SUBJECT", b"x"],
            b"FROM",
            b"bob",
        ]

    def test_precedence_counterexample(self):
        """The T5 row: or binds adjacent terms only."""
        assert criteria_of("from:alice subject:invoice or from:bob") == [
            b"FROM",
            b"alice",
            b"OR",
            b"SUBJECT",
            b"invoice",
            b"FROM",
            b"bob",
        ]

    def test_brace_group_is_or(self):
        assert criteria_of("{from:a from:b}") == [
            b"OR",
            b"FROM",
            b"a",
            b"FROM",
            b"b",
        ]

    def test_value_group_or(self):
        assert criteria_of("subject:(a OR b)") == [
            b"OR",
            b"SUBJECT",
            b"a",
            b"SUBJECT",
            b"b",
        ]

    def test_value_group_and(self):
        assert criteria_of("subject:(a b)") == [
            b"SUBJECT",
            b"a",
            b"SUBJECT",
            b"b",
        ]


class TestCharset:
    """UTF-8 pre-encoding with charset only when a value needs it."""

    def test_ascii_query_has_no_charset(self):
        assert emission_of("from:alice").charset is None

    def test_non_ascii_value_sets_charset(self):
        emission = emission_of("from:josé")
        assert emission.criteria == [b"FROM", "josé".encode("utf-8")]
        assert emission.charset == "UTF-8"

    def test_non_ascii_inside_group_sets_charset(self):
        emission = emission_of("(from:josé subject:x) or from:bob")
        assert emission.charset == "UTF-8"

    def test_trailing_non_ascii_in_group_gets_all_guard(self):
        """imapclient glues the closing paren onto the group's last
        element, so a trailing 8-bit literal needs the neutral ALL."""
        assert criteria_of("-(from:josé subject:café)") == [
            b"NOT",
            [
                b"FROM",
                "josé".encode("utf-8"),
                b"SUBJECT",
                "café".encode("utf-8"),
                b"ALL",
            ],
        ]

    def test_ascii_trailing_group_needs_no_guard(self):
        assert criteria_of("-(from:josé subject:x)") == [
            b"NOT",
            [b"FROM", "josé".encode("utf-8"), b"SUBJECT", b"x"],
        ]


class TestRefusals:
    """Per-backend refusals name the operator and the alternative."""

    def test_has_attachment(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("has:attachment")
        assert str(exc.value) == (
            "has:attachment cannot be expressed on the imap backend: "
            "has:attachment has no server-side search on this IMAP backend. "
            "The operator works on Gmail accounts and on accounts with a "
            "local mail cache."
        )

    def test_has_other(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("has:drive")
        assert str(exc.value) == (
            "has:drive cannot be expressed on the imap backend: only Gmail "
            "understands has:drive. Run this query on a Gmail account."
        )

    def test_filename(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("filename:report.pdf")
        assert str(exc.value) == (
            "filename: cannot be expressed on the imap backend: generic "
            "IMAP has no attachment file name search. Use a Gmail account "
            "or the local mail cache."
        )

    def test_label(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("label:work")
        assert str(exc.value) == (
            "label: cannot be expressed on the imap backend: labels are "
            "Gmail-only on this backend. Scope the search with in:FOLDER "
            "instead."
        )

    def test_category(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("category:promotions")
        assert str(exc.value) == (
            "category: cannot be expressed on the imap backend: inbox "
            "categories exist only on Gmail. Run this query on a Gmail "
            "account."
        )

    def test_in_refuses_at_the_emitter(self):
        """Scope extraction happens at dispatch; an in: reaching the
        emitter is nested where scope cannot apply."""
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("in:sent")
        assert str(exc.value) == (
            "in: cannot be expressed on the imap backend: in: scopes which "
            "folders are searched, so it must stand as a top-level "
            "condition, not under or/not. Move in: to the top level of the "
            "query."
        )

    def test_control_characters_refuse(self):
        with pytest.raises(UntranslatableForBackend, match="control characters"):
            emission_of('subject:"evil\r\ntext"')

    def test_unquotable_specials_refuse(self):
        """imapclient sends a space-less paren value unquoted, which
        would corrupt the command grammar on the wire."""
        with pytest.raises(UntranslatableForBackend, match="unquoted"):
            emission_of('subject:"(urgent)"')

    def test_specials_with_a_space_are_quoted_and_fine(self):
        assert criteria_of('subject:"(urgent) now"') == [
            b"SUBJECT",
            b"(urgent) now",
        ]

    def test_refusal_reason_tag(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("label:work")
        assert exc.value.reason == "untranslatable"
        assert exc.value.backend == "imap"


class TestReport:
    """The emission report carries dialect and approximations."""

    def test_dialect(self):
        assert emission_of("from:alice").report.dialect == "imap"

    def test_exact_translation_has_no_notes(self):
        assert emission_of("from:alice is:unread").report.approximations == []

    def test_as_dict_shape(self):
        report = emission_of("from:alice").report
        assert report.as_dict() == {
            "dialect": "imap",
            "approximations": [],
            "fallbacks": [],
            "treated_as_text": [],
        }


def wire_of(criteria, charset):
    """Drive IMAPClient.search over a mocked socket, returning the
    concatenated bytes it would send (LITERAL+ enabled so literals
    are inline)."""
    client = IMAPClient.__new__(IMAPClient)
    client.use_uid = True
    sent = []
    fake = mock.Mock()
    fake._new_tag.return_value = b"wire1"
    fake.send.side_effect = sent.append
    fake._command_complete.return_value = ("OK", [b"SEARCH completed"])
    fake._untagged_response.return_value = ("OK", [b"1"])
    client._imap = fake
    with mock.patch.object(IMAPClient, "has_capability", return_value=True):
        client.search(criteria, charset=charset)
    return b"".join(sent)


class TestWireBytes:
    """The emitted criteria survive imapclient's own normalisation."""

    def test_ascii_query_sends_no_charset(self):
        emission = emission_of("from:alice subject:x")
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == b"wire1 UID SEARCH FROM alice SUBJECT x\r\n"

    def test_negated_non_ascii_value(self):
        emission = emission_of("-from:josé")
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == (
            b"wire1 UID SEARCH CHARSET UTF-8 NOT FROM" b" {5+}\r\njos\xc3\xa9\r\n"
        )

    def test_or_with_non_ascii_operand(self):
        emission = emission_of("(from:josé or subject:x)")
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == (
            b"wire1 UID SEARCH CHARSET UTF-8 OR FROM"
            b" {5+}\r\njos\xc3\xa9 SUBJECT x\r\n"
        )

    def test_trailing_literal_in_group_is_not_glued_to_paren(self):
        """The ALL guard keeps the closing paren off the trailing
        literal, whose bytes must arrive exactly as counted."""
        emission = emission_of("-(from:josé subject:café)")
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == (
            b"wire1 UID SEARCH CHARSET UTF-8 NOT (FROM"
            b" {5+}\r\njos\xc3\xa9 SUBJECT"
            b" {5+}\r\ncaf\xc3\xa9 ALL)\r\n"
        )

    def test_quoted_value_is_quoted_on_the_wire(self):
        emission = emission_of('from:"Alice Smith"')
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == b'wire1 UID SEARCH FROM "Alice Smith"\r\n'

    def test_dates_format_as_imap_dates(self):
        emission = emission_of("after:2026-07-13")
        wire = wire_of(emission.criteria, emission.charset)
        assert wire == b"wire1 UID SEARCH SINCE 13-Jul-2026\r\n"
