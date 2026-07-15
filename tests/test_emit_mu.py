"""Goldens for the mu emitter, plus live tests against a real index.

The golden half pins one emission per registry row and every refusal
message. The live half builds a small maildir, indexes it with the
real ``mu`` binary when one is installed, and proves the two defects
the emitter exists to fix stay fixed against the actual instrument:
unparenthesized precedence (mu's NOT > AND > OR would regroup the
query) and the inclusive ``date:..X`` upper bound (which runs to the
end of day X while BEFORE semantics exclude it).
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from courier.query import UntranslatableForBackend, parse
from courier.query.emit_mu import emit

NOW = datetime(2026, 7, 15, 12, 0, 0)


def mu_of(query: str) -> str:
    """Emit a query and return only the mu query string."""
    return emit(parse(query), now=NOW).query


def emission_of(query: str):
    """Emit a query with the shared reference instant."""
    return emit(parse(query), now=NOW)


class TestFields:
    """Directly mapped operators emit mu field terms."""

    def test_from(self):
        assert mu_of("from:alice") == "from:alice"

    def test_to(self):
        assert mu_of("to:bob") == "to:bob"

    def test_cc(self):
        assert mu_of("cc:team") == "cc:team"

    def test_bcc(self):
        assert mu_of("bcc:carol@x") == "bcc:carol@x"

    def test_subject(self):
        assert mu_of("subject:invoice") == "subject:invoice"

    def test_body(self):
        assert mu_of("body:hello") == "body:hello"

    def test_filename_maps_to_file(self):
        assert mu_of("filename:report.pdf") == "file:report.pdf"

    def test_list(self):
        assert mu_of("list:announce.example.com") == "list:announce.example.com"

    def test_msgid(self):
        assert mu_of("msgid:<abc@host>") == "msgid:abc@host"

    def test_rfc822msgid_synonym(self):
        assert mu_of("rfc822msgid:abc@host") == "msgid:abc@host"

    def test_multiword_value_is_quoted(self):
        assert mu_of("from:'Alice Smith'") == 'from:"Alice Smith"'


class TestWordsAndPhrases:
    """Words emit one term each; phrases stay quoted."""

    def test_words_join_with_explicit_and(self):
        assert mu_of("meeting notes") == "meeting AND notes"

    def test_phrase(self):
        assert mu_of('"meeting notes"') == '"meeting notes"'


class TestQuotingRules:
    """Xapian keywords and special characters force quotes."""

    @pytest.mark.parametrize("word", ["and", "or", "not", "xor", "AND", "Not"])
    def test_keyword_words_are_quoted(self, word):
        assert mu_of(f'"{word}"') == f'"{word}"'

    def test_colon_word_is_quoted(self):
        emission = emission_of("https://example.com/x")
        assert emission.query == '"https://example.com/x"'
        assert emission.report.treated_as_text == ["https://example.com/x"]

    def test_star_is_quoted(self):
        assert mu_of('"wor*"') == '"wor*"'

    def test_slash_value_is_quoted(self):
        assert mu_of("subject:'a/b'") == 'subject:"a/b"'

    def test_paren_value_is_quoted(self):
        assert mu_of("subject:'(urgent)'") == 'subject:"(urgent)"'

    def test_embedded_quote_becomes_a_space(self):
        """T8: Xapian has no quote escape; quotes are token boundaries
        (live-verified), so a space reproduces the index exactly."""
        emission = emission_of('"club \\"42\\""')
        assert emission.query == '"club 42"'
        assert any(
            "token boundaries" in note for note in emission.report.approximations
        )

    def test_quote_only_value_refuses(self):
        with pytest.raises(UntranslatableForBackend, match="matches nothing"):
            emission_of('subject:"\\""')


class TestFlags:
    """is: states map to mu flags; negative states negate the flag."""

    def test_read(self):
        assert mu_of("is:read") == "flag:seen"

    def test_unread(self):
        assert mu_of("is:unread") == "flag:unread"

    def test_flagged(self):
        assert mu_of("is:flagged") == "flag:flagged"

    def test_starred(self):
        assert mu_of("is:starred") == "flag:flagged"

    def test_unflagged(self):
        assert mu_of("is:unflagged") == "(NOT flag:flagged)"

    def test_unstarred(self):
        assert mu_of("is:unstarred") == "(NOT flag:flagged)"

    def test_answered(self):
        assert mu_of("is:answered") == "flag:replied"

    def test_unanswered(self):
        assert mu_of("is:unanswered") == "(NOT flag:replied)"


class TestDates:
    """T10: upper bounds emit the prior day because mu ranges are
    inclusive while BEFORE semantics are exclusive."""

    def test_after(self):
        assert mu_of("after:2026-07-13") == "date:20260713.."

    def test_before_emits_prior_day(self):
        assert mu_of("before:2026-04-01") == "date:..20260331"

    def test_before_crosses_month_boundary(self):
        assert mu_of("before:2026-03-01") == "date:..20260228"

    def test_on(self):
        assert mu_of("on:2026-03-15") == "date:20260315..20260315"

    def test_newer(self):
        assert mu_of("newer:3d") == "date:20260712.."

    def test_older_emits_prior_day(self):
        assert mu_of("older:2w") == "date:..20260630"

    def test_local_time_note(self):
        emission = emission_of("after:2026-07-13")
        assert any("local time" in note for note in emission.report.approximations)


class TestSizes:
    """Bare byte counts, shifted one byte because mu ranges are
    inclusive while LARGER/SMALLER are strict."""

    def test_larger(self):
        assert mu_of("larger:1M") == "size:1048577.."

    def test_smaller(self):
        assert mu_of("smaller:500k") == "size:..511999"

    def test_smaller_never_goes_negative(self):
        assert mu_of("smaller:0") == "size:..0"


class TestHasAttachment:
    """has:attachment maps to mu's attach flag."""

    def test_flag_attach(self):
        assert mu_of("has:attachment") == "flag:attach"


class TestKeywords:
    """Standalone keywords resolve against the reference instant."""

    def test_all_is_empty_match_all(self):
        assert mu_of("all") == ""

    def test_empty_query_is_match_all(self):
        assert mu_of("") == ""

    def test_today(self):
        assert mu_of("today") == "date:20260715.."

    def test_yesterday(self):
        assert mu_of("yesterday") == "date:20260714..20260714"

    def test_week(self):
        assert mu_of("week") == "date:20260708.."

    def test_month(self):
        assert mu_of("month") == "date:20260615.."


class TestBooleans:
    """Fully parenthesized emission: mu's own precedence never runs."""

    def test_precedence_counterexample(self):
        """The live-confirmed divergence: unparenthesized, mu returns
        the from-bob message the adjacent-binding contract excludes."""
        assert mu_of("from:alice subject:invoice or from:bob") == (
            "from:alice AND (subject:invoice OR from:bob)"
        )

    def test_issue_58_query(self):
        """Bare e-ticket stays bare: the oracle run proved Xapian
        matches infix hyphens without quoting."""
        assert mu_of("after:2026-07-13 (ticket OR booking OR e-ticket)") == (
            "date:20260713.. AND (ticket OR booking OR e-ticket)"
        )

    def test_negated_word(self):
        assert mu_of("invoice -draft") == "invoice AND (NOT draft)"

    def test_negated_group(self):
        assert mu_of("-(from:a subject:b)") == "NOT (from:a AND subject:b)"

    def test_or_operand_group_parenthesizes(self):
        assert mu_of("(from:a subject:b) or from:c") == (
            "(from:a AND subject:b) OR from:c"
        )

    def test_brace_group_is_or(self):
        assert mu_of("{from:a subject:b}") == "from:a OR subject:b"

    def test_compound_flag_stays_grouped_under_not(self):
        assert mu_of("not is:unflagged") == "NOT (NOT flag:flagged)"

    def test_value_group_or(self):
        assert mu_of("subject:(a OR b)") == "subject:a OR subject:b"


class TestRefusals:
    """Refusal goldens: exact messages with the nearest alternative."""

    def test_has_other(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("has:drive")
        assert str(exc.value) == (
            "has:drive cannot be expressed on the mu backend: only Gmail "
            "understands has:drive. Run this query on a Gmail account."
        )

    def test_deliveredto(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("deliveredto:me@example.com")
        assert str(exc.value) == (
            "deliveredto: cannot be expressed on the mu backend: the local "
            "cache does not index the Delivered-To header. Search the "
            "remote server instead."
        )

    def test_label_names_the_x_label_difference(self):
        """The message must not claim the field is absent: mu has its
        own label: field, but it means X-Label tags."""
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("label:work")
        assert str(exc.value) == (
            "label: cannot be expressed on the mu backend: the cache's own "
            "label: field searches X-Label tags, which are not Gmail "
            "labels. Scope the search with in:FOLDER instead."
        )

    def test_category(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("category:promotions")
        assert str(exc.value) == (
            "category: cannot be expressed on the mu backend: inbox "
            "categories exist only on Gmail. Run this query on a Gmail "
            "account."
        )

    def test_in_refuses_at_the_emitter(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("in:sent")
        assert str(exc.value) == (
            "in: cannot be expressed on the mu backend: in: scopes which "
            "folders are searched, so it must stand as a top-level "
            "condition, not under or/not. Move in: to the top level of "
            "the query."
        )

    def test_imap_raw_refuses(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("imap:OR TEXT foo SUBJECT bar")
        assert str(exc.value) == (
            "imap: cannot be expressed on the mu backend: raw IMAP "
            "expressions cannot run against the local cache. The raw query "
            "runs on the remote IMAP search path instead."
        )

    def test_refusal_reason_tag(self):
        with pytest.raises(UntranslatableForBackend) as exc:
            emission_of("imap:UNSEEN")
        assert exc.value.reason == "untranslatable"
        assert exc.value.backend == "mu"


class TestReport:
    """The emission report carries dialect and approximations."""

    def test_dialect(self):
        assert emission_of("from:alice").report.dialect == "mu"

    def test_exact_translation_has_no_notes(self):
        assert emission_of("from:alice is:unread").report.approximations == []


# ---------------------------------------------------------------------------
# Live tests against a real mu index.
# ---------------------------------------------------------------------------

_MESSAGES = {
    "INBOX/cur/1720900001.m1.test:2,S": (
        "From: alice@example.com\n"
        "To: user@example.com\n"
        "Subject: Invoice #42\n"
        "Date: Mon, 30 Mar 2026 10:00:00 +1000\n"
        "Message-ID: <inv42@example.com>\n"
        "\n"
        "Please find the invoice attached in spirit.\n"
    ),
    "INBOX/cur/1720900002.m2.test:2,S": (
        "From: bob@example.com\n"
        "To: user@example.com\n"
        "Subject: weekend plans\n"
        "Date: Wed, 01 Apr 2026 10:00:00 +1000\n"
        "Message-ID: <plans@example.com>\n"
        "\n"
        "notes from the meeting are ready\n"
    ),
    "INBOX/cur/1720900003.m3.test:2,S": (
        "From: carol@example.com\n"
        "To: user@example.com\n"
        'Subject: say "hi" now\n'
        "Date: Thu, 02 Apr 2026 10:00:00 +1000\n"
        "Message-ID: <quote1@example.com>\n"
        "\n"
        'The word club "42" appears here.\n'
    ),
    "Travel/cur/1720900004.m4.test:2,S": (
        "From: Xiamen Airlines <noreply@xiamenair.com>\n"
        "To: user@example.com\n"
        "Subject: Xiamen Airlines - E-ticket Issued Successfully\n"
        "Date: Tue, 14 Jul 2026 09:00:00 +1000\n"
        "Message-ID: <eticket-1@xiamenair.com>\n"
        "\n"
        "Your e-ticket has been issued. Booking reference inside.\n"
    ),
}

_needs_mu = pytest.mark.skipif(
    shutil.which("mu") is None, reason="mu binary not installed"
)


@pytest.fixture(scope="module")
def mu_index(tmp_path_factory):
    """Build a maildir with the four corpus messages and index it.

    Returns:
        The muhome path holding the ready index.
    """
    root = tmp_path_factory.mktemp("mu-live")
    maildir = root / "maildir"
    muhome = root / "muhome"
    for relative, content in _MESSAGES.items():
        target = maildir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.parent.parent / "new").mkdir(exist_ok=True)
        (target.parent.parent / "tmp").mkdir(exist_ok=True)
        target.write_text(content)
    subprocess.run(
        ["mu", "init", f"--muhome={muhome}", f"--maildir={maildir}"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["mu", "index", f"--muhome={muhome}"], check=True, capture_output=True
    )
    return muhome


def _find_subjects(muhome: Path, query: str) -> set:
    """Run mu find and return the matching subjects.

    Args:
        muhome: The indexed muhome from the fixture.
        query: The emitted mu query string.

    Returns:
        The set of subject lines mu returned; empty on no match.
    """
    result = subprocess.run(
        ["mu", "find", f"--muhome={muhome}", "--fields", "s", query],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "no matches" in (result.stderr + result.stdout):
            return set()
        raise AssertionError(
            f"mu find failed for {query!r}: {result.stderr or result.stdout}"
        )
    return {line for line in result.stdout.splitlines() if line}


@_needs_mu
class TestLiveMuIndex:
    """The emitted queries return the contract's result sets against
    the real instrument."""

    def test_precedence_defect_fixed(self, mu_index):
        """Unparenthesized, mu also returned 'weekend plans'."""
        query = mu_of("from:alice subject:invoice or from:bob")
        assert _find_subjects(mu_index, query) == {"Invoice #42"}

    def test_upper_date_bound_defect_fixed(self, mu_index):
        """date:..20260401 also returned the Apr 1 message."""
        query = mu_of("before:2026-04-01")
        assert _find_subjects(mu_index, query) == {"Invoice #42"}

    def test_issue_58_query_finds_the_eticket(self, mu_index):
        query = mu_of("after:2026-07-13 (ticket OR booking OR e-ticket)")
        assert _find_subjects(mu_index, query) == {
            "Xiamen Airlines - E-ticket Issued Successfully"
        }

    def test_bare_words_match(self, mu_index):
        query = mu_of("meeting notes")
        assert _find_subjects(mu_index, query) == {"weekend plans"}

    def test_negation(self, mu_index):
        query = mu_of("invoice -draft")
        assert _find_subjects(mu_index, query) == {"Invoice #42"}

    def test_embedded_quote_phrase_matches(self, mu_index):
        """T8 end to end: the quoted-value phrase reaches the message
        whose body carries the quote characters."""
        query = mu_of('"club \\"42\\""')
        assert _find_subjects(mu_index, query) == {'say "hi" now'}

    def test_subject_phrase_with_quotes(self, mu_index):
        query = mu_of("subject:'say \"hi\" now'")
        assert _find_subjects(mu_index, query) == {'say "hi" now'}

    def test_emitted_query_parses_cleanly(self, mu_index):
        """A structurally rich emission must not be a Xapian parse
        error (exit codes other than the no-match one fail loudly in
        the helper)."""
        query = mu_of("not (from:alice or subject:'weekend plans') 'e-ticket'")
        assert _find_subjects(mu_index, query) == {
            "Xiamen Airlines - E-ticket Issued Successfully"
        }


class TestDateExtremes:
    """Degenerate dates refuse instead of escaping as OverflowError."""

    def test_before_date_min_refuses(self):
        with pytest.raises(UntranslatableForBackend):
            emit(parse("before:0001-01-01"), now=NOW)
