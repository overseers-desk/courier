"""WORLD_AS_OF enforcement in ImapClient: the two layers, mocked IMAP.

Layer 1 is the coarse server-side prefilter (SEARCH BEFORE, day-granular;
X-GM-RAW before:<epoch> on Gmail). Layer 2 is the exact INTERNALDATE
post-filter: drops in result assembly, refusals on direct reads. The
straddle tests plant two same-day messages either side of the bound
instant to prove Layer 2 corrects Layer 1's day-granularity.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from courier.config import ImapBlock
from courier.errors import WorldBoundRefused
from courier.imap_client import ImapClient, _apply_search_bound

BOUND_STR = "2026-07-12T17:07:00+10:00"
BOUND = datetime.fromisoformat(BOUND_STR)

# Two messages on the bound's own day, straddling the bound instant.
# SEARCH BEFORE 13-Jul-2026 (day-granular) returns both; only the
# post-filter can tell them apart.
SAME_DAY_BEFORE = datetime.fromisoformat("2026-07-12T16:00:00+10:00")
SAME_DAY_AFTER = datetime.fromisoformat("2026-07-12T18:00:00+10:00")
NEXT_DAY = datetime.fromisoformat("2026-07-13T09:12:00+10:00")


def _block(**kwargs) -> ImapBlock:
    kwargs.setdefault("host", "imap.example.com")
    return ImapBlock(
        port=993,
        username="test@example.com",
        password="password",
        use_ssl=True,
        **kwargs,
    )


def _raw_message(uid: int, subject: str = "Test") -> bytes:
    return (
        f"From: alice@example.com\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Thu, 09 Jul 2026 12:00:00 +1000\r\n"
        f"Message-ID: <msg-{uid}@example.com>\r\n"
        f"\r\n"
        f"body {uid}\r\n"
    ).encode("utf-8")


def _connected_client(bound=BOUND, block=None, gmail=False) -> ImapClient:
    """A bounded ImapClient whose wire is a MagicMock."""
    client = ImapClient(block or _block(), world_as_of=bound)
    mock_server = MagicMock()
    caps = [b"IMAP4REV1"] + ([b"X-GM-EXT-1"] if gmail else [])
    mock_server.capabilities.return_value = caps
    with patch("imapclient.IMAPClient", return_value=mock_server):
        client.connect()
    return client


def _wire(client: ImapClient) -> MagicMock:
    return client.client


class TestConstructorBoundThreading:
    """The bound is computed once at construction, not per call."""

    def test_default_reads_environment(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", BOUND_STR)
        client = ImapClient(_block())
        assert client.world_as_of == BOUND

    def test_default_unset_is_unbounded(self, monkeypatch):
        monkeypatch.delenv("WORLD_AS_OF", raising=False)
        client = ImapClient(_block())
        assert client.world_as_of is None

    def test_explicit_bound_overrides_environment(self, monkeypatch):
        monkeypatch.delenv("WORLD_AS_OF", raising=False)
        client = ImapClient(_block(), world_as_of=BOUND)
        assert client.world_as_of == BOUND

    def test_explicit_none_is_unbounded(self, monkeypatch):
        monkeypatch.setenv("WORLD_AS_OF", BOUND_STR)
        client = ImapClient(_block(), world_as_of=None)
        assert client.world_as_of is None


class TestSearchLayerOne:
    """Every search the client issues gains the BEFORE prefilter."""

    def test_string_criteria_gain_before_clause(self):
        client = _connected_client()
        _wire(client).search.return_value = []
        client.search("ALL", folder="INBOX")
        _wire(client).search.assert_called_once_with(
            "ALL BEFORE 13-Jul-2026", charset=None
        )

    def test_list_criteria_gain_before_items(self):
        client = _connected_client()
        _wire(client).search.return_value = []
        client.search(["FROM", "alice"], folder="INBOX")
        _wire(client).search.assert_called_once_with(
            ["FROM", "alice", "BEFORE", date(2026, 7, 13)], charset=None
        )

    def test_unbounded_criteria_unchanged(self):
        client = _connected_client(bound=None)
        _wire(client).search.return_value = []
        client.search("ALL", folder="INBOX")
        _wire(client).search.assert_called_once_with("ALL", charset=None)

    def test_apply_search_bound_is_over_inclusive_by_a_day(self):
        # BEFORE is exclusive of the named day, so bound day + 1 keeps
        # the whole bound day in play; never under-inclusive.
        assert _apply_search_bound("ALL", BOUND) == "ALL BEFORE 13-Jul-2026"

    def test_gmail_raw_gains_epoch_before(self):
        client = _connected_client(gmail=True)
        wire = _wire(client)
        wire.search.return_value = []
        client.search_emails("from:alice", folder="INBOX")
        raw = f"from:alice before:{int(BOUND.timestamp())}".encode("utf-8")
        # The epoch clause rides inside X-GM-RAW; the day-granular
        # BEFORE (Layer 1) is still ANDed on beside it.
        wire.search.assert_called_once_with(
            [b"X-GM-RAW", raw, "BEFORE", date(2026, 7, 13)], charset=None
        )

    def test_gmail_raw_unbounded_unchanged(self):
        client = _connected_client(bound=None, gmail=True)
        wire = _wire(client)
        wire.search.return_value = []
        client.search_emails("from:alice", folder="INBOX")
        wire.search.assert_called_once_with([b"X-GM-RAW", b"from:alice"], charset=None)


class TestFetchRefusal:
    """A direct read of a post-bound message refuses with the bound named."""

    def _fetch_response(self, uid: int, internal_date: datetime) -> dict:
        return {
            uid: {
                b"BODY[]": _raw_message(uid),
                b"FLAGS": (b"\\Seen",),
                b"INTERNALDATE": internal_date,
            }
        }

    def test_fetch_email_after_bound_refuses(self):
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(42, NEXT_DAY)
        with pytest.raises(WorldBoundRefused) as excinfo:
            client.fetch_email(42, "INBOX")
        assert "WORLD_AS_OF 2026-07-12T17:07:00+10:00" in str(excinfo.value)
        assert "refused" in str(excinfo.value)

    def test_fetch_email_same_day_after_bound_refuses(self):
        # Same calendar day as the bound: only the exact post-filter
        # catches this; SEARCH BEFORE would have let it through.
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(43, SAME_DAY_AFTER)
        with pytest.raises(WorldBoundRefused):
            client.fetch_email(43, "INBOX")

    def test_fetch_email_before_bound_served(self):
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(44, SAME_DAY_BEFORE)
        email_obj = client.fetch_email(44, "INBOX")
        assert email_obj is not None
        assert email_obj.subject == "Test"

    def test_fetch_email_requests_internaldate_only_under_bound(self):
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(44, SAME_DAY_BEFORE)
        client.fetch_email(44, "INBOX")
        _wire(client).fetch.assert_called_once_with(
            [44], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]
        )

    def test_fetch_email_unbounded_wire_unchanged(self):
        client = _connected_client(bound=None)
        _wire(client).fetch.return_value = {
            44: {b"BODY[]": _raw_message(44), b"FLAGS": (b"\\Seen",)}
        }
        client.fetch_email(44, "INBOX")
        _wire(client).fetch.assert_called_once_with([44], ["BODY.PEEK[]", "FLAGS"])

    def test_fetch_raw_after_bound_refuses(self):
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(45, NEXT_DAY)
        with pytest.raises(WorldBoundRefused, match="WORLD_AS_OF"):
            client.fetch_raw(45, "INBOX")

    def test_fetch_raw_before_bound_served(self):
        client = _connected_client()
        _wire(client).fetch.return_value = self._fetch_response(46, SAME_DAY_BEFORE)
        raw = client.fetch_raw(46, "INBOX")
        assert raw is not None
        assert raw["subject"] == "Test"


class TestBatchFetchDrops:
    """Result-assembly paths drop post-bound messages instead of refusing."""

    def test_fetch_emails_drops_after_bound(self):
        client = _connected_client()
        _wire(client).fetch.return_value = {
            1: {
                b"BODY[]": _raw_message(1),
                b"FLAGS": (b"\\Seen",),
                b"INTERNALDATE": SAME_DAY_BEFORE,
            },
            2: {
                b"BODY[]": _raw_message(2),
                b"FLAGS": (b"\\Seen",),
                b"INTERNALDATE": SAME_DAY_AFTER,
            },
        }
        emails = client.fetch_emails([1, 2], "INBOX")
        assert set(emails.keys()) == {1}


class TestSearchAssembly:
    """search_emails: post-filter before the limit cut, provenance counting."""

    def _install_search(self, client, dates_by_uid):
        wire = _wire(client)
        wire.search.return_value = sorted(dates_by_uid, reverse=True)

        def fetch_side_effect(uids, items):
            if items == ["INTERNALDATE"]:
                return {u: {b"INTERNALDATE": dates_by_uid[u]} for u in uids}
            return {
                u: {
                    b"BODY[]": _raw_message(u),
                    b"FLAGS": (b"\\Seen",),
                    b"INTERNALDATE": dates_by_uid[u],
                }
                for u in uids
            }

        wire.fetch.side_effect = fetch_side_effect

    def test_same_day_straddle_corrected_by_post_filter(self):
        client = _connected_client()
        self._install_search(client, {1: SAME_DAY_BEFORE, 2: SAME_DAY_AFTER})
        result = client.search_emails("test", folder="INBOX", limit=10)
        uids = [r["uid"] for r in result["results"]]
        assert uids == [1]
        prov = result["provenance"]["world_as_of"]
        assert prov["bound"] == BOUND_STR
        assert prov["dropped_after_bound"] == 1
        assert prov["date_source"] == "internaldate"
        assert prov["current_state_fields"] == ["flags", "folder"]

    def test_bound_applies_before_limit_cut(self):
        # Three candidates, newest is post-bound. With limit=2 the two
        # surviving pre-bound messages must both return; a filter that
        # ran after the limit cut would return only one.
        client = _connected_client()
        self._install_search(
            client,
            {
                1: datetime.fromisoformat("2026-07-10T10:00:00+10:00"),
                2: SAME_DAY_BEFORE,
                3: SAME_DAY_AFTER,
            },
        )
        result = client.search_emails("test", folder="INBOX", limit=2)
        uids = {r["uid"] for r in result["results"]}
        assert uids == {1, 2}
        assert result["provenance"]["world_as_of"]["dropped_after_bound"] == 1

    def test_unbounded_provenance_has_no_world_as_of(self):
        client = _connected_client(bound=None)
        self._install_search(client, {1: SAME_DAY_BEFORE})
        result = client.search_emails("test", folder="INBOX", limit=10)
        assert "world_as_of" not in result["provenance"]


class TestThreadMembersDropped:
    """fetch_thread: post-bound members drop; a post-bound root refuses."""

    def _install_thread(self, client, dates_by_uid):
        wire = _wire(client)
        wire.search.return_value = list(dates_by_uid)

        def fetch_side_effect(uids, items):
            out = {}
            for u in uids:
                data = {
                    b"BODY[]": _raw_message(u, subject="Thread"),
                    b"FLAGS": (b"\\Seen",),
                }
                if "INTERNALDATE" in items:
                    data[b"INTERNALDATE"] = dates_by_uid[u]
                out[u] = data
            return out

        wire.fetch.side_effect = fetch_side_effect

    def test_future_members_are_dropped(self):
        client = _connected_client()
        self._install_thread(client, {1: SAME_DAY_BEFORE, 2: NEXT_DAY})
        thread = client.fetch_thread(1, "INBOX")
        assert [e.uid for e in thread] == [1]

    def test_future_root_refuses(self):
        client = _connected_client()
        self._install_thread(client, {1: NEXT_DAY, 2: SAME_DAY_BEFORE})
        with pytest.raises(WorldBoundRefused, match="WORLD_AS_OF"):
            client.fetch_thread(1, "INBOX")


class TestDiskCachePathBounded:
    """Maildir-served reads are bounded on the Date-header date."""

    def _client_with_disk(self, tmp_path, message_date: str, bound=BOUND):
        from courier.local_cache import EligibilityResult

        root = tmp_path / "maildir"
        (root / "INBOX" / "cur").mkdir(parents=True)
        (root / "INBOX" / "new").mkdir(parents=True)
        name = "1700000000_0.host,U=7,FMD5=abc:2,S"
        (root / "INBOX" / "cur" / name).write_bytes(
            (
                "From: alice@example.com\r\n"
                "To: bob@example.com\r\n"
                "Subject: Disk\r\n"
                f"Date: {message_date}\r\n"
                "Message-ID: <disk-7@example.com>\r\n"
                "\r\n"
                "disk body\r\n"
            ).encode("utf-8")
        )
        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        return ImapClient(_block(maildir=str(root)), local_cache=mu, world_as_of=bound)

    def test_disk_read_after_bound_refuses(self, tmp_path):
        client = self._client_with_disk(tmp_path, "Mon, 13 Jul 2026 09:12:00 +1000")
        with pytest.raises(WorldBoundRefused, match="WORLD_AS_OF"):
            client.fetch_email(7, "INBOX")

    def test_disk_read_before_bound_served(self, tmp_path):
        client = self._client_with_disk(tmp_path, "Sun, 12 Jul 2026 16:00:00 +1000")
        email_obj = client.fetch_email(7, "INBOX")
        assert email_obj is not None
        assert email_obj.subject == "Disk"

    def test_disk_batch_drops_after_bound(self, tmp_path):
        client = self._client_with_disk(tmp_path, "Mon, 13 Jul 2026 09:12:00 +1000")
        emails = client.fetch_emails([7], "INBOX")
        assert emails == {}


class TestCliReadSurfacing:
    """The CLI read path renders a refusal as an error, not a traceback."""

    def test_fetch_email_result_returns_error_dict(self):
        from courier.__main__ import _fetch_email_result

        client = MagicMock()
        client.fetch_email.side_effect = WorldBoundRefused(
            "message dated 2026-07-13T09:12:00+10:00 is after "
            "WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused"
        )
        result = _fetch_email_result(client, "INBOX", 42)
        assert "error" in result
        assert "WORLD_AS_OF" in result["error"]


class TestRelativeTermsAnchorToBound:
    """ImapClient threads its bound into relative-date resolution."""

    def test_search_emails_anchors_newer_to_bound(self):
        client = _connected_client()
        wire = _wire(client)
        wire.search.return_value = []
        client.search_emails("newer:7d", folder="INBOX")
        wire.search.assert_called_once_with(
            [b"SINCE", date(2026, 7, 5), "BEFORE", date(2026, 7, 13)],
            charset=None,
        )

    def test_search_emails_today_keyword_anchors_to_bound(self):
        client = _connected_client()
        wire = _wire(client)
        wire.search.return_value = []
        client.search_emails("today", folder="INBOX")
        wire.search.assert_called_once_with(
            [b"SINCE", date(2026, 7, 12), "BEFORE", date(2026, 7, 13)],
            charset=None,
        )

    @pytest.mark.asyncio
    async def test_search_resource_rides_the_shared_bounded_path(self):
        # resources.py no longer parses queries itself (the old bypass
        # path); it delegates to search_emails, whose bound anchoring
        # the client-level tests above pin.
        from courier.resources import register_resources

        registered = {}
        mcp = MagicMock()
        mcp.resource = lambda path: lambda func: registered.setdefault(path, func)

        imap_client = MagicMock()
        imap_client.search_emails.return_value = {"results": [], "provenance": {}}
        register_resources(mcp, imap_client)

        await registered["email://search/{query}"]("newer:7d")
        imap_client.search_emails.assert_called_once_with(
            "newer:7d", folder=None, limit=50
        )


class TestLocalCachePathBoundedLikeImap:
    """The mu-cache path is bounded the same as the IMAP path."""

    def _local_client(self, results):
        from courier.local_cache import EligibilityResult
        from courier.query.ast import TranslationReport

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.return_value = (results, TranslationReport(dialect="mu"), False)
        mu.index_mtime_iso.return_value = "2026-07-12T00:00:00+00:00"
        return (
            ImapClient(
                _block(maildir="/var/local/mail/work"),
                local_cache=mu,
                world_as_of=BOUND,
            ),
            mu,
        )

    def _result(self, message_id: str, iso_date: str) -> dict:
        return {
            "message_id": message_id,
            "path": f"/var/local/mail/work/cur/{message_id}",
            "folder": "INBOX",
            "from": "Alice <a@b.com>",
            "to": ["c@d.com"],
            "subject": "Hi",
            "date": iso_date,
            "flags": ["seen"],
            "has_attachments": False,
        }

    def test_bound_threaded_to_backend(self):
        from courier.query import parse

        client, mu = self._local_client([])
        client.search_emails("from:alice")
        mu.search.assert_called_once_with(
            client.block, parse("from:alice"), 10, None, world_as_of=BOUND
        )

    def test_post_filter_drops_and_counts(self):
        client, _ = self._local_client(
            [
                self._result("keep", "2026-07-12T16:00:00+10:00"),
                self._result("drop", "2026-07-12T18:00:00+10:00"),
            ]
        )
        result = client.search_emails("from:alice")
        assert [r["message_id"] for r in result["results"]] == ["keep"]
        prov = result["provenance"]["world_as_of"]
        assert prov["bound"] == BOUND_STR
        assert prov["dropped_after_bound"] == 1
        assert prov["date_source"] == "mu_index"

    def test_undated_results_are_kept(self):
        record = self._result("undated", "2026-07-12T16:00:00+10:00")
        record["date"] = None
        client, _ = self._local_client([record])
        result = client.search_emails("from:alice")
        assert len(result["results"]) == 1
        assert result["provenance"]["world_as_of"]["dropped_after_bound"] == 0


class TestFoldersHonestRule:
    """Folder list: mutable with no history, so served current-state and flagged."""

    def test_folders_result_flagged_under_bound(self):
        client = ImapClient(_block(), world_as_of=BOUND)
        with patch.object(client, "list_folders", return_value=["INBOX", "Sent"]):
            result = client.folders_result()
        assert result == {
            "folders": ["INBOX", "Sent"],
            "world_as_of": {
                "bound": BOUND_STR,
                "current_state_fields": ["folders"],
            },
        }

    def test_folders_result_plain_list_unbounded(self):
        client = ImapClient(_block(), world_as_of=None)
        with patch.object(client, "list_folders", return_value=["INBOX"]):
            assert client.folders_result() == ["INBOX"]


class TestResourceBypassSurfaces:
    """resources.py calls ImapClient directly; the bound must hold there."""

    def _register(self, imap_client):
        from courier.resources import register_resources

        registered = {}
        mcp = MagicMock()
        mcp.resource = lambda path: lambda func: registered.setdefault(path, func)
        register_resources(mcp, imap_client)
        return registered

    @pytest.mark.asyncio
    async def test_get_email_resource_surfaces_refusal(self):
        imap_client = MagicMock()
        imap_client.fetch_email.side_effect = WorldBoundRefused(
            "message dated 2026-07-13T09:12:00+10:00 is after "
            "WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused"
        )
        registered = self._register(imap_client)
        result = await registered["email://{folder}/{uid}"]("INBOX", "42")
        assert "WORLD_AS_OF" in result
        assert "refused" in result

    @pytest.mark.asyncio
    async def test_folders_resource_flagged_under_bound(self):
        import json

        client = ImapClient(_block(), world_as_of=BOUND)
        registered = self._register(client)
        with patch.object(client, "list_folders", return_value=["INBOX"]):
            result = await registered["email://folders"]()
        data = json.loads(result)
        assert data["folders"] == ["INBOX"]
        assert data["world_as_of"]["bound"] == BOUND_STR

    @pytest.mark.asyncio
    async def test_list_resource_drops_post_bound_messages(self):
        # email://{folder}/list is search("ALL") + fetch_summaries on
        # the real client; the summary fetch drops post-bound UIDs on
        # the fetched INTERNALDATE (Layer 2).
        import json

        client = _connected_client()
        wire = _wire(client)
        wire.search.return_value = [1, 2]

        def fetch_side_effect(uids, items):
            dates = {1: SAME_DAY_BEFORE, 2: SAME_DAY_AFTER}
            return {
                u: {
                    b"BODY[HEADER]": _raw_message(u),
                    b"FLAGS": (b"\\Seen",),
                    b"BODYSTRUCTURE": (b"text", b"plain"),
                    b"INTERNALDATE": dates[u],
                }
                for u in uids
            }

        wire.fetch.side_effect = fetch_side_effect
        registered = self._register(client)
        result = await registered["email://{folder}/list"]("INBOX")
        payload = json.loads(result)
        assert [entry["uid"] for entry in payload["results"]] == [1]
        # INTERNALDATE was requested for the Layer 2 judgement.
        assert "INTERNALDATE" in wire.fetch.call_args[0][1]
