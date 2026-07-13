"""Tests for courier.world_bound: WORLD_AS_OF parsing, predicates, wiring.

The three semantics under test:

1. Unset: ``world_as_of()`` returns ``None``; nothing changes.
2. Set: an aware ``datetime`` is returned for enforcement downstream.
3. Set but unparseable or naive: ``WorldAsOfInvalid`` at parse time,
   surfaced as a hard failure at CLI and MCP-server startup.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from courier.config import CourierConfig, ImapBlock
from courier.errors import CourierError, WorldAsOfInvalid
from courier.world_bound import after_bound, refusal_message, world_as_of

runner = CliRunner()

BOUND_STR = "2026-07-12T17:07:00+10:00"
BOUND = datetime.fromisoformat(BOUND_STR)


class TestWorldAsOfParsing:
    """Semantics of the environment variable itself."""

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("WORLD_AS_OF", raising=False)
        assert world_as_of() is None

    def test_valid_iso_with_offset(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", BOUND_STR)
        bound = world_as_of()
        assert bound is not None
        assert bound.tzinfo is not None
        assert bound.utcoffset() == timedelta(hours=10)
        assert bound.isoformat() == BOUND_STR

    def test_valid_utc_z_suffix(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", "2026-07-12T07:07:00Z")
        bound = world_as_of()
        assert bound is not None
        assert bound.utcoffset() == timedelta(0)

    def test_garbage_raises(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", "not-a-date")
        with pytest.raises(WorldAsOfInvalid, match="WORLD_AS_OF"):
            world_as_of()

    def test_naive_timestamp_raises(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", "2026-07-12T17:07:00")
        with pytest.raises(WorldAsOfInvalid, match="timezone offset"):
            world_as_of()

    def test_empty_value_raises(self, monkeypatch):
        # Set-but-empty is not unset: treating it as unbounded would be
        # the silent fallback the contract forbids.
        monkeypatch.setenv("WORLD_AS_OF", "")
        with pytest.raises(WorldAsOfInvalid):
            world_as_of()

    def test_invalid_is_a_courier_error(self):
        assert issubclass(WorldAsOfInvalid, CourierError)


class TestPredicates:
    """after_bound and refusal_message over aware and naive datetimes."""

    def test_aware_after_bound(self):
        after = datetime.fromisoformat("2026-07-13T09:12:00+10:00")
        assert after_bound(after, BOUND) is True

    def test_aware_before_bound(self):
        before = datetime.fromisoformat("2026-07-12T17:06:59+10:00")
        assert after_bound(before, BOUND) is False

    def test_exactly_at_bound_is_not_after(self):
        assert after_bound(datetime.fromisoformat(BOUND_STR), BOUND) is False

    def test_offset_normalisation(self):
        # The same instant expressed in UTC is not after the bound.
        same_instant_utc = datetime.fromisoformat("2026-07-12T07:07:00+00:00")
        assert after_bound(same_instant_utc, BOUND) is False

    def test_naive_taken_as_local_time(self):
        # A naive datetime (imapclient's INTERNALDATE shape) is compared
        # as local time: one hour past the bound in local terms is after.
        local_bound = BOUND.astimezone()
        naive_after = (local_bound + timedelta(hours=1)).replace(tzinfo=None)
        naive_before = (local_bound - timedelta(hours=1)).replace(tzinfo=None)
        assert after_bound(naive_after, BOUND) is True
        assert after_bound(naive_before, BOUND) is False

    def test_refusal_message_names_both_instants(self):
        after = datetime.fromisoformat("2026-07-13T09:12:00+10:00")
        msg = refusal_message(after, BOUND)
        assert msg == (
            "message dated 2026-07-13T09:12:00+10:00 is after "
            "WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused"
        )

    def test_refusal_message_normalises_naive_dates(self):
        naive = datetime(2026, 7, 13, 9, 12, 0)
        msg = refusal_message(naive, BOUND)
        assert "WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused" in msg
        # The naive date is rendered aware (offset present).
        assert naive.astimezone().isoformat() in msg


class TestUtcComparisonSanity:
    """The bound compares instants, not wall-clock strings."""

    def test_cross_zone_comparison(self):
        bound_utc = datetime(2026, 7, 12, 7, 7, 0, tzinfo=timezone.utc)
        just_after = datetime.fromisoformat("2026-07-12T17:07:01+10:00")
        assert after_bound(just_after, bound_utc) is True


class TestCliStartupWiring:
    """A bad WORLD_AS_OF is a hard failure before any verb runs."""

    def test_bad_bound_exits_one_with_message(self):
        from courier.__main__ import app

        result = runner.invoke(app, ["folders"], env={"WORLD_AS_OF": "garbage"})
        assert result.exit_code == 1
        assert "WORLD_AS_OF" in result.output

    def test_naive_bound_exits_one(self):
        from courier.__main__ import app

        result = runner.invoke(
            app, ["folders"], env={"WORLD_AS_OF": "2026-07-12T17:07:00"}
        )
        assert result.exit_code == 1
        assert "timezone offset" in result.output

    def test_good_bound_proceeds(self):
        from courier.__main__ import app

        client = MagicMock()
        client.folders_result.return_value = {
            "folders": ["INBOX"],
            "world_as_of": {
                "bound": BOUND_STR,
                "current_state_fields": ["folders"],
            },
        }
        with patch("courier.__main__._make_client", return_value=client):
            result = runner.invoke(app, ["folders"], env={"WORLD_AS_OF": BOUND_STR})
        assert result.exit_code == 0
        assert "INBOX" in result.output

    def test_unset_proceeds(self, monkeypatch):
        from courier.__main__ import app

        monkeypatch.delenv("WORLD_AS_OF", raising=False)
        client = MagicMock()
        client.folders_result.return_value = ["INBOX"]
        with patch("courier.__main__._make_client", return_value=client):
            result = runner.invoke(app, ["folders"])
        assert result.exit_code == 0

    def test_chain_globals_fail_hard(self, monkeypatch):
        from courier.__main__ import _apply_global_flags

        monkeypatch.setenv("WORLD_AS_OF", "garbage")
        with pytest.raises(SystemExit) as excinfo:
            _apply_global_flags([])
        assert excinfo.value.code == 1


class TestMcpStartupWiring:
    """The MCP server refuses to start under an invalid bound."""

    @pytest.mark.asyncio
    async def test_lifespan_refuses_bad_bound(self, monkeypatch):
        from courier.mcp_server import server_lifespan

        monkeypatch.setenv("WORLD_AS_OF", "garbage")
        server = MagicMock()
        server._config = CourierConfig(
            imap_blocks={
                "default": ImapBlock(
                    host="imap.example.com",
                    port=993,
                    username="u@example.com",
                    password="x",
                )
            },
            _default_imap="default",
        )
        with pytest.raises(WorldAsOfInvalid):
            async with server_lifespan(server):
                pass  # pragma: no cover - startup must refuse first
