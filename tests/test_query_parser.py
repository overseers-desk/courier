"""Tests for the Gmail-style query parser."""

from datetime import date, datetime, timedelta

import pytest

from courier.query_parser import (
    UntranslatableQuery,
    parse_query,
    parse_query_to_mu,
)


class TestBareWords:
    """Bare words (no prefix) become TEXT searches."""

    def test_single_word(self):
        assert parse_query("meeting") == ["TEXT", "meeting"]

    def test_multiple_words_merge(self):
        assert parse_query("meeting notes") == ["TEXT", "meeting notes"]

    def test_multiple_words_with_extra_spaces(self):
        assert parse_query("  meeting   notes  ") == ["TEXT", "meeting notes"]


class TestPrefixValue:
    """prefix:value terms map to IMAP search keys."""

    def test_from(self):
        assert parse_query("from:alice") == ["FROM", "alice"]

    def test_to(self):
        assert parse_query("to:bob") == ["TO", "bob"]

    def test_cc(self):
        assert parse_query("cc:team") == ["CC", "team"]

    def test_subject(self):
        assert parse_query("subject:invoice") == ["SUBJECT", "invoice"]

    def test_body(self):
        assert parse_query("body:hello") == ["BODY", "hello"]

    def test_prefix_is_case_insensitive(self):
        assert parse_query("FROM:alice") == ["FROM", "alice"]
        assert parse_query("Subject:invoice") == ["SUBJECT", "invoice"]


class TestQuotedValues:
    """Quoted values group multi-word strings."""

    def test_double_quoted_subject(self):
        assert parse_query('subject:"hotel booking"') == ["SUBJECT", "hotel booking"]

    def test_single_quoted_from(self):
        assert parse_query("from:'Alice Smith'") == ["FROM", "Alice Smith"]

    def test_quoted_bare_words(self):
        assert parse_query('"meeting notes"') == ["TEXT", "meeting notes"]


class TestIsKeywords:
    """is:keyword maps to IMAP flags."""

    def test_is_unread(self):
        assert parse_query("is:unread") == "UNSEEN"

    def test_is_read(self):
        assert parse_query("is:read") == "SEEN"

    def test_is_flagged(self):
        assert parse_query("is:flagged") == "FLAGGED"

    def test_is_starred(self):
        assert parse_query("is:starred") == "FLAGGED"

    def test_is_unflagged(self):
        assert parse_query("is:unflagged") == "UNFLAGGED"

    def test_is_unstarred(self):
        assert parse_query("is:unstarred") == "UNFLAGGED"

    def test_is_answered(self):
        assert parse_query("is:answered") == "ANSWERED"

    def test_is_unanswered(self):
        assert parse_query("is:unanswered") == "UNANSWERED"

    def test_invalid_is_keyword(self):
        with pytest.raises(ValueError, match="Unknown is: keyword"):
            parse_query("is:bogus")


class TestDateOperators:
    """after:/before:/on: map to SINCE/BEFORE/ON with date objects."""

    def test_after_iso(self):
        result = parse_query("after:2025-03-01")
        assert result == ["SINCE", date(2025, 3, 1)]

    def test_after_slash(self):
        result = parse_query("after:2025/03/01")
        assert result == ["SINCE", date(2025, 3, 1)]

    def test_before(self):
        result = parse_query("before:2025-04-01")
        assert result == ["BEFORE", date(2025, 4, 1)]

    def test_on(self):
        result = parse_query("on:2025-03-15")
        assert result == ["ON", date(2025, 3, 15)]

    def test_invalid_date(self):
        with pytest.raises(ValueError, match="Invalid date"):
            parse_query("after:not-a-date")


class TestRelativeDates:
    """newer:/older: with relative offsets like 3d, 1w, 2m."""

    def test_newer_days(self):
        result = parse_query("newer:3d")
        expected_date = (datetime.now() - timedelta(days=3)).date()
        assert result == ["SINCE", expected_date]

    def test_older_days(self):
        result = parse_query("older:7d")
        expected_date = (datetime.now() - timedelta(days=7)).date()
        assert result == ["BEFORE", expected_date]

    def test_newer_weeks(self):
        result = parse_query("newer:2w")
        expected_date = (datetime.now() - timedelta(weeks=2)).date()
        assert result == ["SINCE", expected_date]

    def test_older_months(self):
        result = parse_query("older:1m")
        expected_date = (datetime.now() - timedelta(days=30)).date()
        assert result == ["BEFORE", expected_date]

    def test_newer_than_synonym(self):
        result = parse_query("newer_than:3d")
        expected_date = (datetime.now() - timedelta(days=3)).date()
        assert result == ["SINCE", expected_date]

    def test_older_than_synonym(self):
        result = parse_query("older_than:7d")
        expected_date = (datetime.now() - timedelta(days=7)).date()
        assert result == ["BEFORE", expected_date]

    def test_invalid_relative_date(self):
        with pytest.raises(ValueError, match="Invalid relative date"):
            parse_query("newer:abc")


class TestStandaloneKeywords:
    """Single-word shortcuts for common searches."""

    def test_all(self):
        assert parse_query("all") == "ALL"

    def test_today(self):
        result = parse_query("today")
        assert result == ["SINCE", date.today()]

    def test_yesterday(self):
        result = parse_query("yesterday")
        yesterday = (datetime.now() - timedelta(days=1)).date()
        assert result == ["SINCE", yesterday, "BEFORE", date.today()]

    def test_week(self):
        result = parse_query("week")
        week_ago = (datetime.now() - timedelta(days=7)).date()
        assert result == ["SINCE", week_ago]

    def test_month(self):
        result = parse_query("month")
        month_ago = (datetime.now() - timedelta(days=30)).date()
        assert result == ["SINCE", month_ago]

    def test_standalone_case_insensitive(self):
        assert parse_query("ALL") == "ALL"
        assert parse_query("Today") == ["SINCE", date.today()]


class TestEmptyQuery:
    """Empty or whitespace-only query returns ALL."""

    def test_empty_string(self):
        assert parse_query("") == "ALL"

    def test_whitespace_only(self):
        assert parse_query("   ") == "ALL"


class TestOrOperator:
    """or keyword produces IMAP OR (Polish notation)."""

    def test_simple_or(self):
        result = parse_query("from:alice or from:bob")
        assert result == ["OR", "FROM", "alice", "FROM", "bob"]

    def test_chained_or(self):
        result = parse_query("from:a or from:b or from:c")
        assert result == ["OR", "FROM", "a", "OR", "FROM", "b", "FROM", "c"]

    def test_or_with_is_keywords(self):
        result = parse_query("is:read or is:flagged")
        assert result == ["OR", "SEEN", "FLAGGED"]

    def test_or_case_insensitive(self):
        result = parse_query("from:alice OR from:bob")
        assert result == ["OR", "FROM", "alice", "FROM", "bob"]


class TestNotOperator:
    """Negation with - prefix or not keyword."""

    def test_dash_prefix(self):
        result = parse_query("-from:alice")
        assert result == ["NOT", "FROM", "alice"]

    def test_not_keyword(self):
        result = parse_query("not is:read")
        assert result == ["NOT", "SEEN"]

    def test_not_keyword_case_insensitive(self):
        result = parse_query("NOT from:alice")
        assert result == ["NOT", "FROM", "alice"]


class TestCombinedQueries:
    """Multiple terms combined with implicit AND."""

    def test_from_and_subject(self):
        result = parse_query("from:alice subject:invoice")
        assert result == ["FROM", "alice", "SUBJECT", "invoice"]

    def test_from_and_is(self):
        result = parse_query("from:alice is:unread")
        assert result == ["FROM", "alice", "UNSEEN"]

    def test_prefix_and_bare_words(self):
        result = parse_query("from:alice meeting notes")
        assert result == ["FROM", "alice", "TEXT", "meeting notes"]

    def test_date_and_flag(self):
        result = parse_query("after:2025-03-01 is:unread")
        assert result == ["SINCE", date(2025, 3, 1), "UNSEEN"]

    def test_multiple_prefixes_and_date(self):
        result = parse_query("from:alice subject:invoice after:2025-01-01 is:unread")
        assert result == [
            "FROM",
            "alice",
            "SUBJECT",
            "invoice",
            "SINCE",
            date(2025, 1, 1),
            "UNSEEN",
        ]

    def test_or_mixed_with_and(self):
        """OR between two terms, AND with a third."""
        result = parse_query("from:alice or from:bob subject:invoice")
        assert result == ["OR", "FROM", "alice", "FROM", "bob", "SUBJECT", "invoice"]

    def test_not_in_combined(self):
        result = parse_query("from:alice -is:read")
        assert result == ["FROM", "alice", "NOT", "SEEN"]

    def test_bare_words_before_prefix(self):
        result = parse_query("meeting from:alice")
        assert result == ["TEXT", "meeting", "FROM", "alice"]


class TestImapEscapeHatch:
    """imap: prefix passes raw IMAP expressions through."""

    def test_simple_raw(self):
        result = parse_query("imap:UNSEEN")
        assert result == "UNSEEN"

    def test_complex_raw(self):
        result = parse_query('imap:OR TEXT "Edinburgh" TEXT "Berlin"')
        assert result == ["OR", "TEXT", "Edinburgh", "TEXT", "Berlin"]

    def test_raw_combined(self):
        result = parse_query('imap:UNSEEN FROM "john@example.com"')
        assert result == ["UNSEEN", "FROM", "john@example.com"]


class TestEdgeCases:
    """Edge cases and unusual inputs."""

    def test_colon_in_value(self):
        """Email addresses contain colons in edge cases; main case is the prefix split."""
        result = parse_query("from:alice@example.com")
        assert result == ["FROM", "alice@example.com"]

    def test_unknown_prefix_treated_as_bare_word(self):
        """Unknown prefixes are not expanded — treated as bare text."""
        result = parse_query("label:work")
        assert result == ["TEXT", "label:work"]

    def test_only_or_raises(self):
        with pytest.raises(ValueError, match="[Oo]r"):
            parse_query("or")

    def test_dangling_or_raises(self):
        with pytest.raises(ValueError, match="[Oo]r"):
            parse_query("from:alice or")

    def test_dangling_not_raises(self):
        with pytest.raises(ValueError, match="[Nn]ot"):
            parse_query("not")

    def test_numeric_query(self):
        """Numeric strings are TEXT searches."""
        assert parse_query("69172700") == ["TEXT", "69172700"]


class TestMsgidOperator:
    """msgid: / rfc822msgid: expand to an IMAP HEADER Message-ID search."""

    def test_bare_id(self):
        result = parse_query("msgid:abc@host")
        assert result == ["HEADER", "Message-ID", "abc@host"]

    def test_angle_brackets_stripped(self):
        result = parse_query("msgid:<abc@host>")
        assert result == ["HEADER", "Message-ID", "abc@host"]

    def test_rfc822msgid_alias(self):
        result = parse_query("rfc822msgid:<abc@host>")
        assert result == ["HEADER", "Message-ID", "abc@host"]

    def test_negated(self):
        result = parse_query("-msgid:<abc@host>")
        assert result == ["NOT", "HEADER", "Message-ID", "abc@host"]

    def test_or_with_from(self):
        result = parse_query("msgid:<a@h> or from:alice")
        assert result == ["OR", "HEADER", "Message-ID", "a@h", "FROM", "alice"]

    def test_combined_with_flag(self):
        result = parse_query("msgid:<a@h> is:unread")
        assert result == ["HEADER", "Message-ID", "a@h", "UNSEEN"]


class TestMuEmit:
    """parse_query_to_mu translates courier queries into mu CLI strings."""

    # ------------------------------------------------------------------
    # prefix:value terms
    # ------------------------------------------------------------------

    def test_from(self):
        assert parse_query_to_mu("from:alice") == "from:alice"

    def test_to(self):
        assert parse_query_to_mu("to:bob@example.com") == "to:bob@example.com"

    def test_subject_unquoted_single_token(self):
        """`subject:meeting notes` — first token is `subject:meeting`,
        second `notes` is a bare word; concatenation reads naturally."""
        assert parse_query_to_mu("subject:meeting notes") == "subject:meeting notes"

    def test_subject_quoted_phrase(self):
        assert parse_query_to_mu('subject:"meeting notes"') == 'subject:"meeting notes"'

    # ------------------------------------------------------------------
    # is:keyword → flag:X
    # ------------------------------------------------------------------

    def test_is_unread(self):
        assert parse_query_to_mu("is:unread") == "flag:unread"

    def test_is_read(self):
        assert parse_query_to_mu("is:read") == "flag:seen"

    def test_is_flagged(self):
        assert parse_query_to_mu("is:flagged") == "flag:flagged"

    def test_is_starred(self):
        assert parse_query_to_mu("is:starred") == "flag:flagged"

    def test_is_answered(self):
        assert parse_query_to_mu("is:answered") == "flag:replied"

    def test_is_unflagged(self):
        assert parse_query_to_mu("is:unflagged") == "NOT flag:flagged"

    def test_is_unanswered(self):
        assert parse_query_to_mu("is:unanswered") == "NOT flag:replied"

    # ------------------------------------------------------------------
    # date operators
    # ------------------------------------------------------------------

    def test_after(self):
        assert parse_query_to_mu("after:2025-03-01") == "date:20250301.."

    def test_before(self):
        assert parse_query_to_mu("before:2025-03-01") == "date:..20250301"

    def test_on(self):
        assert parse_query_to_mu("on:2025-03-01") == "date:20250301..20250301"

    # ------------------------------------------------------------------
    # boolean operators
    # ------------------------------------------------------------------

    def test_or_lowercase_becomes_uppercase(self):
        assert parse_query_to_mu("from:alice OR to:bob") == "from:alice OR to:bob"

    def test_not_keyword(self):
        assert parse_query_to_mu("not is:unread") == "NOT flag:unread"

    def test_dash_negation(self):
        assert parse_query_to_mu("-from:alice") == "NOT from:alice"

    # ------------------------------------------------------------------
    # msgid: / rfc822msgid: → mu's exact-match msgid field
    # ------------------------------------------------------------------

    def test_msgid_angle_brackets_stripped(self):
        assert parse_query_to_mu("msgid:<abc@host>") == "msgid:abc@host"

    def test_msgid_rfc822msgid_alias(self):
        assert parse_query_to_mu("rfc822msgid:<abc@host>") == "msgid:abc@host"

    def test_msgid_negated(self):
        assert parse_query_to_mu("-msgid:<a@h>") == "NOT msgid:a@h"

    # ------------------------------------------------------------------
    # bare words and special inputs
    # ------------------------------------------------------------------

    def test_bare_words(self):
        assert parse_query_to_mu("meeting notes") == "meeting notes"

    def test_empty(self):
        assert parse_query_to_mu("") == ""

    def test_standalone_all(self):
        """`all` is mu's match-all (empty query)."""
        assert parse_query_to_mu("all") == ""

    def test_today_format(self):
        """`today` produces date:YYYYMMDD..; the date is variable so we
        only assert structure."""
        result = parse_query_to_mu("today")
        assert result.startswith("date:")
        assert result.endswith("..")

    # ------------------------------------------------------------------
    # untranslatable cases
    # ------------------------------------------------------------------

    def test_imap_escape_raises_untranslatable(self):
        with pytest.raises(UntranslatableQuery) as excinfo:
            parse_query_to_mu("imap:OR TEXT foo SUBJECT bar")
        assert excinfo.value.reason == "untranslatable"

    def test_imap_prefix_token_raises_untranslatable(self):
        """A non-leading token with imap: prefix also surfaces as
        untranslatable so the caller can fall back to IMAP."""
        with pytest.raises(UntranslatableQuery) as excinfo:
            parse_query_to_mu("foo imap:RAW")
        assert excinfo.value.reason == "untranslatable"

    # ------------------------------------------------------------------
    # malformed queries → ValueError
    # ------------------------------------------------------------------

    def test_unknown_is_keyword_raises(self):
        with pytest.raises(ValueError, match="Unknown is: keyword"):
            parse_query_to_mu("is:bogus")

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            parse_query_to_mu("after:not-a-date")

    def test_dangling_or_raises(self):
        with pytest.raises(ValueError, match="[Oo]r"):
            parse_query_to_mu("from:alice or")

    def test_dangling_not_raises(self):
        with pytest.raises(ValueError, match="[Nn]ot"):
            parse_query_to_mu("not")
