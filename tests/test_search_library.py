"""Tests for ``courier.search``, the library's search across [imap.NAME] blocks.

The clients are real; ``ImapClient.search_emails`` and
``ImapClient.disconnect`` are replaced on the class, so each call
records the client it ran on, and the block loop, the per-block
outcome and the CLI's use of it run as written.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import courier
from courier import QuerySyntaxError
from courier.__main__ import _chain_exit_code, app
from courier.config import CourierConfig, ImapBlock, LocalCacheConfig
from courier.imap_client import ImapClient
from courier.local_cache import MuBackend
from courier.searching import block_status, search

BOUND = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _block(name: str) -> ImapBlock:
    return ImapBlock(host=f"{name}.example.com", port=993, username="u", password="p")


def _config(
    *names: str,
    default: Optional[str] = None,
    local_cache: Optional[LocalCacheConfig] = None,
) -> CourierConfig:
    return CourierConfig(
        imap_blocks={name: _block(name) for name in names},
        _default_imap=default,
        local_cache=local_cache,
    )


def _name(client: ImapClient) -> str:
    return client.block.host.split(".")[0]


def _envelope(hits: int = 1, **extra: Any) -> Dict[str, Any]:
    env: Dict[str, Any] = {
        "results": [{"uid": n, "folder": "INBOX"} for n in range(hits)],
        "provenance": {"source": "remote"},
        "total_count": hits,
        "truncated": False,
    }
    env.update(extra)
    return env


@pytest.fixture
def server() -> Iterator[SimpleNamespace]:
    """Each block's search answers from ``outcomes``; every call is recorded."""
    outcomes: Dict[str, Any] = {}

    def answer(client: ImapClient, *args: Any, **kwargs: Any) -> Any:
        outcome = outcomes.get(_name(client), _envelope())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        patch.object(
            ImapClient, "search_emails", autospec=True, side_effect=answer
        ) as search_emails,
        patch.object(ImapClient, "disconnect", autospec=True) as disconnect,
    ):
        yield SimpleNamespace(
            outcomes=outcomes, search_emails=search_emails, disconnect=disconnect
        )


def _clients(server: SimpleNamespace) -> List[ImapClient]:
    """The client each search ran on, in call order."""
    return [c.args[0] for c in server.search_emails.call_args_list]


def _searched(server: SimpleNamespace) -> List[str]:
    return [_name(c) for c in _clients(server)]


class TestStatus:

    @pytest.mark.parametrize(
        "result, status",
        [
            (_envelope(), "complete"),
            (_envelope(0), "complete"),
            (_envelope(folders_failed=[]), "complete"),
            (_envelope(truncated=True), "complete"),
            (_envelope(0, folders_failed=[{"folder": "Spam"}]), "partial"),
            (_envelope(truncated=True, folders_failed=[{"folder": "S"}]), "partial"),
            ({"error": "boom"}, "failed"),
            ({"error": "boom", "folders_failed": [{"folder": "S"}]}, "failed"),
        ],
    )
    def test_block_status(self, result: Dict[str, Any], status: str):
        assert block_status(result) == status

    def test_a_truncated_search_is_complete(self, server: SimpleNamespace):
        server.outcomes["a"] = _envelope(10, truncated=True, total_count=40)
        assert search(_config("a"), "q")["a"]["status"] == "complete"

    def test_a_failed_folder_makes_the_block_partial(self, server: SimpleNamespace):
        failed = [{"folder": "Spam", "error": "NO"}]
        server.outcomes["a"] = _envelope(folders_failed=failed)
        out = search(_config("a"), "q")["a"]
        assert out["status"] == "partial"
        assert out["folders_failed"] == failed

    def test_a_connection_failure_names_its_reason(self, server: SimpleNamespace):
        server.outcomes["a"] = ConnectionError("network is down")
        assert search(_config("a"), "q")["a"] == {
            "error": "connection failed: network is down",
            "status": "failed",
        }

    def test_any_other_failure_carries_its_text(self, server: SimpleNamespace):
        server.outcomes["a"] = RuntimeError("timeout")
        assert search(_config("a"), "q")["a"] == {
            "error": "timeout",
            "status": "failed",
        }

    def test_a_search_dict_keeps_every_key(self, server: SimpleNamespace):
        server.outcomes["a"] = _envelope()
        assert search(_config("a"), "q")["a"] == {**_envelope(), "status": "complete"}


class TestBlocks:

    def test_none_searches_every_block_in_config_order(self, server: SimpleNamespace):
        out = search(_config("c", "a", "b", default="b"), "q")
        assert list(out) == ["c", "a", "b"]
        assert _searched(server) == ["c", "a", "b"]

    def test_the_blocks_named_are_searched_in_that_order(self, server: SimpleNamespace):
        out = search(_config("a", "b", "c"), "q", blocks=["c", "a"])
        assert list(out) == ["c", "a"]
        assert _searched(server) == ["c", "a"]

    def test_a_name_given_twice_is_searched_once(self, server: SimpleNamespace):
        out = search(_config("a", "b"), "q", blocks=["b", "a", "b"])
        assert list(out) == ["b", "a"]
        assert _searched(server) == ["b", "a"]

    def test_an_unknown_name_is_refused_before_any_client(
        self, server: SimpleNamespace
    ):
        with patch("courier.searching.ImapClient", wraps=ImapClient) as built:
            with pytest.raises(ValueError, match=r"\[imap\.nope\]"):
                search(_config("a"), "q", blocks=["a", "nope"])
        built.assert_not_called()
        server.search_emails.assert_not_called()

    def test_one_name_in_place_of_a_list_is_refused(self, server: SimpleNamespace):
        with patch("courier.searching.ImapClient", wraps=ImapClient) as built:
            with pytest.raises(TypeError):
                search(_config("a"), "q", blocks="a")  # type: ignore[arg-type]
        built.assert_not_called()

    def test_a_malformed_query_is_refused_before_any_client(
        self, server: SimpleNamespace
    ):
        with patch("courier.searching.ImapClient", wraps=ImapClient) as built:
            with pytest.raises(QuerySyntaxError):
                search(_config("a", "b"), "after:not-a-date")
        built.assert_not_called()
        server.search_emails.assert_not_called()


class TestClients:

    def test_one_client_per_block_each_disconnected_once(self, server: SimpleNamespace):
        search(_config("a", "b", "c"), "q")
        clients = _clients(server)
        assert len({id(c) for c in clients}) == 3
        assert [c.args[0] for c in server.disconnect.call_args_list] == clients

    def test_a_failed_block_does_not_stop_the_later_ones(self, server: SimpleNamespace):
        server.outcomes["a"] = ConnectionError("down")
        server.outcomes["b"] = RuntimeError("boom")
        out = search(_config("a", "b", "c"), "q")
        assert [out[n]["status"] for n in "abc"] == ["failed", "failed", "complete"]
        assert server.disconnect.call_count == 3

    def test_a_rejected_query_still_disconnects(self, server: SimpleNamespace):
        server.outcomes["a"] = ValueError("in: conflicts with folder")
        with pytest.raises(ValueError, match="conflicts"):
            search(_config("a", "b"), "in:sent q", folder="INBOX")
        assert _searched(server) == ["a"]
        assert server.disconnect.call_count == 1

    def test_limit_defaults_to_10(self, server: SimpleNamespace):
        search(_config("a"), "q")
        assert server.search_emails.call_args.kwargs["limit"] == 10

    def test_query_folder_limit_and_no_cache_are_forwarded(
        self, server: SimpleNamespace
    ):
        search(_config("a", "b"), "from:x", folder="Archive", limit=3, no_cache=True)
        for call in server.search_emails.call_args_list:
            assert call.args[1:] == ("from:x",)
            assert call.kwargs == {"folder": "Archive", "limit": 3, "no_cache": True}

    def test_world_as_of_reaches_every_client(self, server: SimpleNamespace):
        search(_config("a", "b"), "q", world_as_of=BOUND)
        assert [c.world_as_of for c in _clients(server)] == [BOUND, BOUND]

    def test_the_environment_bound_is_not_read(
        self, server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("WORLD_AS_OF", "2026-01-01T00:00:00+00:00")
        search(_config("a", "b"), "q")
        assert [c.world_as_of for c in _clients(server)] == [None, None]

    def test_one_backend_serves_every_block(
        self, server: SimpleNamespace, tmp_path: Path
    ):
        local_cache = LocalCacheConfig(mu_index=str(tmp_path))
        search(_config("a", "b", local_cache=local_cache), "q")
        first, second = (c.local_cache for c in _clients(server))
        assert isinstance(first, MuBackend)
        assert first is second
        assert first.cfg is local_cache

    def test_no_backend_without_a_local_cache_table(self, server: SimpleNamespace):
        search(_config("a"), "q")
        assert _clients(server)[0].local_cache is None

    def test_nothing_is_printed(
        self, server: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ):
        server.outcomes["a"] = ConnectionError("down")
        search(_config("a", "b"), "q")
        assert capsys.readouterr() == ("", "")


class TestCommand:

    def _invoke(self, cfg: CourierConfig, *args: str) -> Any:
        with patch("courier.__main__.load_config", return_value=cfg):
            return CliRunner().invoke(app, ["--all-imap", *args])

    def test_the_command_asks_for_50(self, server: SimpleNamespace):
        result = self._invoke(_config("a"), "search", "q")
        assert result.exit_code == 0, result.output
        assert server.search_emails.call_args.kwargs["limit"] == 50
        assert json.loads(result.stdout)["search q"]["a"]["status"] == "complete"

    def test_a_failed_search_prints_no_warning(self, server: SimpleNamespace):
        server.outcomes["a"] = ConnectionError("down")
        result = self._invoke(_config("a"), "search", "q")
        assert result.exit_code == 2
        assert result.stderr == ""
        assert json.loads(result.stdout)["search q"]["a"] == {
            "error": "connection failed: down",
            "status": "failed",
        }

    @pytest.mark.parametrize(
        "outcome, status, code",
        [
            (_envelope(), "complete", 0),
            (_envelope(0), "complete", 1),
            (_envelope(0, folders_failed=[{"folder": "Spam"}]), "partial", 2),
            (ConnectionError("down"), "failed", 2),
        ],
    )
    def test_the_exit_code_agrees_with_status(
        self, server: SimpleNamespace, outcome: Any, status: str, code: int
    ):
        server.outcomes["a"] = outcome
        out = search(_config("a"), "q")
        assert out["a"]["status"] == status
        assert _chain_exit_code({"search q": out}) == code


def test_search_is_exported():
    assert courier.search is search
    assert "search" in courier.__all__
