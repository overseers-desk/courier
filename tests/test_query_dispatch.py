"""Dispatch decisions: scope extraction, capability gates, fallback phrasing.

The dispatch module owns the pure decisions of the search path: pulling
top-level ``in:`` conjuncts out of the parsed query into a folder scope
(both emitters refuse ``in:`` unconditionally, so it must never reach
them), selecting the remote emitter from server capabilities instead of
hostnames, and phrasing backend declines for the terminal refusal
message.
"""

import pytest

from courier.query import QuerySyntaxError, parse
from courier.query.ast import And, Not, Term, UntranslatableForBackend
from courier.query.dispatch import (
    Scope,
    cache_folder_for_scope,
    compose_backend_error,
    describe_fallbacks,
    ensure_no_folder_conflict,
    extract_scope,
    gmail_capable,
    supports_within,
)


def scope_of(query: str) -> Scope:
    """Extract just the scope for one query."""
    _, scope = extract_scope(parse(query))
    return scope


class TestExtractScope:
    """Top-level in: conjuncts leave the tree and become folder scope."""

    def test_no_scope_terms_pass_through_unchanged(self):
        parsed = parse("from:alice subject:x")
        remaining, scope = extract_scope(parsed)
        assert remaining == parsed
        assert scope == Scope((), (), False)

    def test_positive_in_becomes_include(self):
        remaining, scope = extract_scope(parse("in:sent from:alice"))
        assert scope.include == ("sent",)
        assert remaining.ast == Term("from", "alice")

    def test_negated_in_becomes_exclude(self):
        remaining, scope = extract_scope(parse("-in:trash from:alice"))
        assert scope.exclude == ("trash",)
        assert remaining.ast == Term("from", "alice")

    def test_in_alone_leaves_match_all(self):
        remaining, scope = extract_scope(parse("in:inbox"))
        assert scope.include == ("inbox",)
        assert remaining.ast == Term("keyword", "all")

    def test_anywhere_sets_the_flag(self):
        scope = scope_of("in:anywhere from:alice")
        assert scope.anywhere is True
        assert scope.include == ()

    def test_literal_folder_value_keeps_case(self):
        assert scope_of("in:Archive/2025 x").include == ("Archive/2025",)

    def test_multiple_includes_collect_in_order(self):
        assert scope_of("in:inbox in:sent x").include == ("inbox", "sent")

    def test_remaining_conjunction_rebuilds(self):
        remaining, _ = extract_scope(parse("in:sent from:alice subject:x"))
        assert remaining.ast == And((Term("from", "alice"), Term("subject", "x")))

    def test_treated_as_text_survives_extraction(self):
        remaining, _ = extract_scope(parse("in:sent https://example.com/x"))
        assert remaining.treated_as_text == ("https://example.com/x",)

    def test_parenthesized_top_level_in_still_extracts(self):
        # _make_and flattens the paren group, so the conjunct is
        # genuinely top-level.
        assert scope_of("(in:sent from:a) subject:b").include == ("sent",)


class TestScopePlacementRefusals:
    """in: under or, nested negation, or groups refuses for every backend."""

    def test_in_under_or_refuses(self):
        with pytest.raises(QuerySyntaxError, match="top level"):
            extract_scope(parse("from:a or in:sent"))

    def test_in_under_negated_group_refuses(self):
        with pytest.raises(QuerySyntaxError, match="top level"):
            extract_scope(parse("-(in:sent from:a)"))

    def test_in_inside_brace_group_refuses(self):
        with pytest.raises(QuerySyntaxError, match="top level"):
            extract_scope(parse("{in:sent from:a}"))

    def test_double_negated_in_refuses(self):
        with pytest.raises(QuerySyntaxError, match="top level"):
            extract_scope(parse("--in:sent from:a"))

    def test_negated_anywhere_refuses(self):
        with pytest.raises(QuerySyntaxError, match="anywhere"):
            extract_scope(parse("-in:anywhere from:a"))

    def test_anywhere_with_include_refuses(self):
        with pytest.raises(QuerySyntaxError, match="cannot be combined"):
            extract_scope(parse("in:anywhere in:inbox x"))

    def test_anywhere_with_exclude_refuses(self):
        with pytest.raises(QuerySyntaxError, match="cannot be combined"):
            extract_scope(parse("in:anywhere -in:trash x"))


class TestFolderConflict:
    """A folder argument and an in: scope cannot both steer the search."""

    def test_folder_with_include_refuses(self):
        with pytest.raises(QuerySyntaxError, match="not both"):
            ensure_no_folder_conflict("INBOX", scope_of("in:sent x"))

    def test_folder_with_exclude_refuses(self):
        with pytest.raises(QuerySyntaxError, match="not both"):
            ensure_no_folder_conflict("INBOX", scope_of("-in:trash x"))

    def test_folder_with_anywhere_refuses(self):
        with pytest.raises(QuerySyntaxError, match="not both"):
            ensure_no_folder_conflict("INBOX", scope_of("in:anywhere x"))

    def test_folder_without_scope_passes(self):
        ensure_no_folder_conflict("INBOX", scope_of("from:alice"))

    def test_scope_without_folder_passes(self):
        ensure_no_folder_conflict(None, scope_of("in:sent x"))


class TestCacheFolderForScope:
    """What the mu cache can serve: one exact folder or the whole block."""

    def test_no_scope_passes_folder_through(self):
        assert cache_folder_for_scope(scope_of("x"), "INBOX") == (True, "INBOX")

    def test_no_scope_no_folder_serves_whole_block(self):
        assert cache_folder_for_scope(scope_of("x"), None) == (True, None)

    def test_inbox_include_maps_to_inbox(self):
        assert cache_folder_for_scope(scope_of("in:inbox x"), None) == (True, "INBOX")

    def test_literal_include_passes_through(self):
        scope = scope_of("in:Work/Clients x")
        assert cache_folder_for_scope(scope, None) == (True, "Work/Clients")

    def test_anywhere_serves_whole_block(self):
        assert cache_folder_for_scope(scope_of("in:anywhere x"), None) == (True, None)

    @pytest.mark.parametrize("value", ["sent", "spam", "trash"])
    def test_special_use_values_decline(self, value):
        # The cache cannot resolve the server's SPECIAL-USE folders.
        scope = scope_of(f"in:{value} x")
        assert cache_folder_for_scope(scope, None) == (False, None)

    def test_multiple_includes_decline(self):
        scope = scope_of("in:inbox in:Work x")
        assert cache_folder_for_scope(scope, None) == (False, None)

    def test_excludes_decline(self):
        scope = scope_of("-in:trash x")
        assert cache_folder_for_scope(scope, None) == (False, None)


class TestCapabilityGates:
    """Remote emitter selection is capability-gated, never hostname-gated."""

    def test_gmail_ext_present(self):
        assert gmail_capable(["IMAP4REV1", "X-GM-EXT-1"]) is True

    def test_gmail_ext_absent(self):
        assert gmail_capable(["IMAP4REV1", "IDLE"]) is False

    def test_gmail_ext_case_insensitive(self):
        assert gmail_capable(["x-gm-ext-1"]) is True

    def test_within_present(self):
        assert supports_within(["IMAP4REV1", "WITHIN"]) is True

    def test_within_absent(self):
        assert supports_within(["IMAP4REV1"]) is False


class TestFallbackPhrasing:
    """Terminal refusals name every decline in cache-user vocabulary."""

    def test_stale_names_the_existing_cache(self):
        text = describe_fallbacks([{"backend": "cache", "reason": "stale"}])
        assert "your local cache exists but its index is stale" in text
        assert "enable" not in text

    def test_unknown_reason_passes_through(self):
        text = describe_fallbacks([{"backend": "cache", "reason": "novel_tag"}])
        assert "novel_tag" in text

    def test_compose_appends_declines_to_the_refusal(self):
        exc = UntranslatableForBackend(
            "imap",
            "label:",
            "labels are Gmail-only on this backend",
            "Scope the search with in:FOLDER instead.",
        )
        text = compose_backend_error(exc, [{"backend": "cache", "reason": "stale"}])
        assert str(exc) in text
        assert "stale" in text

    def test_compose_without_declines_is_the_refusal_alone(self):
        exc = UntranslatableForBackend("imap", "label:", "no mapping")
        assert compose_backend_error(exc, []) == str(exc)


class TestScopeAgainstNot:
    """Not(...) around non-in terms is untouched by extraction."""

    def test_negated_from_stays(self):
        remaining, scope = extract_scope(parse("-from:alice in:sent"))
        assert scope.include == ("sent",)
        assert remaining.ast == Not(Term("from", "alice"))
