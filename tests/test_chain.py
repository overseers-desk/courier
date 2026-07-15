"""Unit and CLI tests for the verb-chain dispatcher and the chain output shape.

Courier's read-only verbs (``search``, ``read``) chain at the top level:

    courier search foo search bar read -f INBOX -u 42

The dispatcher pre-scans argv before typer runs. With one chainable verb the
dispatcher returns and typer dispatches normally, so single-op invocations and
``--help`` keep working unchanged. With two or more chainable verbs the
dispatcher parses the chain itself and routes to ``_execute_chain``.

All tests run without a network connection. IMAP clients are mocked.
"""

import json
from typing import List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from courier.__main__ import (
    _apply_global_flags,
    _build_op_key,
    _empty_result_for_subcmd,
    _execute_chain,
    _parse_read_args,
    _parse_search_args,
    _peel_chain_tail_flags,
    _rewrite_argv,
    _run_chain,
    _split_chain_argv,
    app,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# _build_op_key
# ---------------------------------------------------------------------------


class TestBuildOpKey:
    def test_search_query_only(self):
        assert _build_op_key("search", query="from:foo") == "search from:foo"

    def test_search_with_folder(self):
        assert _build_op_key("search", query="x", folder="INBOX") == "search -f INBOX x"

    def test_search_default_limit_omitted(self):
        key = _build_op_key("search", query="x", limit=50)
        assert "--limit" not in key

    def test_search_non_default_limit_included(self):
        key = _build_op_key("search", query="x", limit=20)
        assert "--limit 20" in key

    def test_search_empty_query(self):
        key = _build_op_key("search", query="", folder="INBOX")
        assert key == "search -f INBOX"

    def test_read_key(self):
        assert _build_op_key("read", folder="INBOX", uid=42) == "read -f INBOX --uid 42"

    def test_unknown_subcmd_returns_bare_name(self):
        assert _build_op_key("folders") == "folders"


# ---------------------------------------------------------------------------
# _empty_result_for_subcmd
# ---------------------------------------------------------------------------


class TestEmptyResultForSubcmd:
    def test_search_returns_error_dict(self):
        """E1: a dead connection is an error, never an empty mailbox."""
        r = _empty_result_for_subcmd("search")
        assert r == {"error": "connection failed"}
        assert "results" not in r

    def test_read_returns_error_dict(self):
        r = _empty_result_for_subcmd("read")
        assert r == {"error": "connection failed"}


# ---------------------------------------------------------------------------
# _parse_search_args / _parse_read_args
# ---------------------------------------------------------------------------


class TestParseSearchArgs:
    def test_bare_query(self):
        r = _parse_search_args(["from:foo"])
        assert r["query"] == "from:foo"
        assert r["folder"] is None
        assert r["limit"] == 50

    def test_folder_short(self):
        r = _parse_search_args(["-f", "INBOX", "from:foo"])
        assert r["folder"] == "INBOX"

    def test_folder_long(self):
        r = _parse_search_args(["--folder=Sent", "x"])
        assert r["folder"] == "Sent"

    def test_limit_short(self):
        r = _parse_search_args(["-n", "5", "x"])
        assert r["limit"] == 5

    def test_limit_long_equals(self):
        r = _parse_search_args(["--limit=20", "x"])
        assert r["limit"] == 20

    def test_multi_word_query(self):
        r = _parse_search_args(["hello", "world"])
        assert r["query"] == "hello world"

    def test_unknown_flags_error_loudly(self):
        """E4: a dropped flag silently changes the query; refuse it."""
        with pytest.raises(ValueError, match="unknown search flag"):
            _parse_search_args(["--unknown", "from:foo"])

    def test_query_equals_form(self):
        """E4: --query=X used to be dropped, searching match-all."""
        r = _parse_search_args(["--query=from:foo"])
        assert r["query"] == "from:foo"

    def test_query_space_form(self):
        r = _parse_search_args(["-q", "from:foo"])
        assert r["query"] == "from:foo"

    def test_query_option_overrides_positional(self):
        r = _parse_search_args(["bar", "--query=from:foo"])
        assert r["query"] == "from:foo"

    def test_negation_tokens_stay_query_text(self):
        r = _parse_search_args(["invoice", "-draft"])
        assert r["query"] == "invoice -draft"

    def test_negated_operator_token_stays_query_text(self):
        r = _parse_search_args(["-from:alice"])
        assert r["query"] == "-from:alice"

    def test_default_folder_used_when_unset(self):
        r = _parse_search_args(["from:foo"], default_folder="Sent")
        assert r["folder"] == "Sent"

    def test_per_verb_folder_overrides_default(self):
        r = _parse_search_args(["-f", "Drafts", "from:foo"], default_folder="Sent")
        assert r["folder"] == "Drafts"

    def test_default_limit_used_when_unset(self):
        r = _parse_search_args(["from:foo"], default_limit=50)
        assert r["limit"] == 50

    def test_per_verb_limit_overrides_default(self):
        r = _parse_search_args(["-n", "5", "from:foo"], default_limit=50)
        assert r["limit"] == 5


class TestParseReadArgs:
    def test_basic(self):
        r = _parse_read_args(["-f", "INBOX", "--uid", "42"])
        assert r["folder"] == "INBOX"
        assert r["uid"] == 42

    def test_equals_forms(self):
        r = _parse_read_args(["--folder=Sent", "--uid=7"])
        assert r["folder"] == "Sent"
        assert r["uid"] == 7

    def test_missing_folder_raises(self):
        with pytest.raises(ValueError, match="--folder"):
            _parse_read_args(["--uid", "1"])

    def test_missing_uid_raises(self):
        with pytest.raises(ValueError, match="--uid"):
            _parse_read_args(["-f", "INBOX"])


# ---------------------------------------------------------------------------
# _split_chain_argv
# ---------------------------------------------------------------------------


def _verb_args(
    split: Optional[Tuple[List[str], List[Tuple[str, List[str]]], str]],
    index: int,
) -> List[str]:
    assert split is not None
    return split[1][index][1]


class TestSplitChainArgv:
    def test_single_search_returns_none(self):
        assert _split_chain_argv(["search", "foo"]) is None

    def test_single_read_returns_none(self):
        assert _split_chain_argv(["read", "-f", "INBOX", "-u", "1"]) is None

    def test_search_help_returns_none(self):
        assert _split_chain_argv(["search", "--help"]) is None

    def test_two_searches_split(self):
        s = _split_chain_argv(["search", "foo", "search", "bar"])
        assert s is not None
        assert s[1] == [("search", ["foo"]), ("search", ["bar"])]

    def test_search_then_read(self):
        s = _split_chain_argv(["search", "foo", "read", "-f", "INBOX", "-u", "1"])
        assert s is not None
        assert s[1] == [("search", ["foo"]), ("read", ["-f", "INBOX", "-u", "1"])]

    def test_folder_named_search_does_not_split(self):
        # `-f search` consumes 'search' as the folder value; the second 'search'
        # is the chain split point.
        s = _split_chain_argv(["search", "-f", "search", "foo", "search", "bar"])
        assert s is not None
        assert s[1] == [
            ("search", ["-f", "search", "foo"]),
            ("search", ["bar"]),
        ]

    def test_quoted_query_equal_to_verb_name(self):
        # The shell already unquotes "search me" into one token before argv,
        # so ``_split_chain_argv(["search", "search me"])`` is one op.
        assert _split_chain_argv(["search", "search me"]) is None

    def test_global_flags_extracted(self):
        s = _split_chain_argv(
            ["-A", "-c", "/tmp/x.toml", "search", "foo", "search", "bar"]
        )
        assert s is not None
        global_argv, verbs, _, _ = s
        assert "-A" in global_argv
        assert "-c" in global_argv
        assert "/tmp/x.toml" in global_argv
        assert verbs == [("search", ["foo"]), ("search", ["bar"])]

    def test_imap_equals_form(self):
        s = _split_chain_argv(["--imap=acct1", "search", "foo", "search", "bar"])
        assert s is not None
        assert "--imap=acct1" in s[0]

    def test_repeated_imap(self):
        s = _split_chain_argv(
            ["--imap", "acct1", "--imap", "acct2", "search", "foo", "search", "bar"]
        )
        assert s is not None
        assert s[0].count("--imap") == 2

    def test_format_flag_extracted(self):
        s = _split_chain_argv(["search", "foo", "search", "bar", "--format", "oneline"])
        assert s is not None
        assert s[2] == "oneline"

    def test_format_equals_form(self):
        s = _split_chain_argv(["search", "foo", "search", "bar", "--format=text"])
        assert s is not None
        assert s[2] == "text"

    def test_only_one_verb_returns_none_even_with_format(self):
        # A --format that *trails* a single verb stays with typer, whose
        # per-command --format option handles it.
        assert _split_chain_argv(["search", "foo", "--format", "json"]) is None

    def test_pre_verb_format_routes_single_search_through_chain(self):
        # A --format *ahead* of a single verb must route through the chain
        # executor: typer's top-level callback has no --format, so falling
        # back would error "No such option: --format".
        s = _split_chain_argv(["--format", "json", "search", "foo"])
        assert s is not None
        assert s[1] == [("search", ["foo"])]
        assert s[2] == "json"

    def test_pre_verb_format_with_all_imap_single_search(self):
        s = _split_chain_argv(["-A", "--format", "oneline", "search", "foo"])
        assert s is not None
        assert "-A" in s[0]
        assert s[1] == [("search", ["foo"])]
        assert s[2] == "oneline"

    def test_pre_verb_format_equals_form_single_search(self):
        s = _split_chain_argv(["--format=text", "search", "foo"])
        assert s is not None
        assert s[1] == [("search", ["foo"])]
        assert s[2] == "text"

    def test_chain_defaults_limit_at_tail(self):
        s = _split_chain_argv(["search", "a", "search", "b", "-n", "50"])
        assert s is not None
        assert s[3] == {"limit": 50}
        assert s[1] == [("search", ["a"]), ("search", ["b"])]

    def test_chain_defaults_folder_at_tail(self):
        s = _split_chain_argv(["search", "a", "search", "b", "-f", "Sent"])
        assert s is not None
        assert s[3] == {"folder": "Sent"}

    def test_chain_defaults_both_flags_at_tail(self):
        s = _split_chain_argv(["search", "a", "search", "b", "-n", "50", "-f", "Sent"])
        assert s is not None
        assert s[3] == {"limit": 50, "folder": "Sent"}

    def test_per_verb_flag_not_promoted_to_chain(self):
        # ``-n 5`` interior to the first verb stays per-verb; the second
        # verb has no -n so its limit is the parser default.
        s = _split_chain_argv(["search", "-n", "5", "a", "search", "b"])
        assert s is not None
        assert s[3] == {}
        assert s[1] == [("search", ["-n", "5", "a"]), ("search", ["b"])]


# ---------------------------------------------------------------------------
# _peel_chain_tail_flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rest,expected_rest,expected_chain",
    [
        ([], [], {}),
        (["search", "x"], ["search", "x"], {}),
        (
            ["search", "a", "search", "b", "-n", "50"],
            ["search", "a", "search", "b"],
            {"limit": 50},
        ),
        (
            ["search", "a", "search", "b", "--limit", "20"],
            ["search", "a", "search", "b"],
            {"limit": 20},
        ),
        (
            ["search", "a", "search", "b", "--limit=15"],
            ["search", "a", "search", "b"],
            {"limit": 15},
        ),
        (
            ["search", "a", "search", "b", "-f", "Sent"],
            ["search", "a", "search", "b"],
            {"folder": "Sent"},
        ),
        (
            ["search", "a", "search", "b", "--folder=Drafts"],
            ["search", "a", "search", "b"],
            {"folder": "Drafts"},
        ),
        (
            ["search", "a", "search", "b", "-n", "50", "-f", "Sent"],
            ["search", "a", "search", "b"],
            {"limit": 50, "folder": "Sent"},
        ),
        (
            ["search", "a", "search", "b", "-f", "Sent", "-n", "50"],
            ["search", "a", "search", "b"],
            {"limit": 50, "folder": "Sent"},
        ),
        # per-verb tokens preserved when no chain-tail flag follows
        (
            ["search", "-n", "5", "a", "search", "b"],
            ["search", "-n", "5", "a", "search", "b"],
            {},
        ),
        # non-integer limit halts peeling at that flag
        (
            ["search", "a", "search", "b", "-n", "notanint"],
            ["search", "a", "search", "b", "-n", "notanint"],
            {},
        ),
    ],
)
def test_peel_chain_tail_flags(rest, expected_rest, expected_chain):
    out_rest, out_chain = _peel_chain_tail_flags(rest)
    assert out_rest == expected_rest
    assert out_chain == expected_chain


# ---------------------------------------------------------------------------
# _execute_chain
# ---------------------------------------------------------------------------


def _fake_search_result(query: str = "q") -> dict:
    return {
        "results": [{"subject": f"result for {query}", "from": "x@y.com"}],
        "provenance": {
            "source": "remote",
            "indexed_at": None,
            "fell_back_reason": None,
        },
    }


class TestExecuteChain:
    def _make_client(self):
        client = MagicMock()
        client.search_emails.return_value = _fake_search_result("q")
        return client

    @patch("courier.__main__._make_client_soft")
    def test_single_search_wraps_in_op_key(self, mock_soft):
        mock_soft.return_value = self._make_client()
        ops = [
            (
                "search from:foo",
                "search",
                {"query": "from:foo", "folder": None, "limit": 10},
            )
        ]
        result = _execute_chain(ops, ["acct1"])
        assert "search from:foo" in result
        assert "acct1" in result["search from:foo"]
        assert "results" in result["search from:foo"]["acct1"]

    @patch("courier.__main__._make_client_soft")
    def test_two_ops_produce_two_outer_keys(self, mock_soft):
        mock_soft.return_value = self._make_client()
        ops = [
            (
                "search from:a",
                "search",
                {"query": "from:a", "folder": None, "limit": 10},
            ),
            (
                "search from:b",
                "search",
                {"query": "from:b", "folder": None, "limit": 10},
            ),
        ]
        result = _execute_chain(ops, ["acct1"])
        assert set(result.keys()) == {"search from:a", "search from:b"}

    @patch("courier.__main__._make_client_soft")
    def test_one_connection_per_account(self, mock_soft):
        client = self._make_client()
        mock_soft.return_value = client
        ops = [
            (
                "search from:a",
                "search",
                {"query": "from:a", "folder": None, "limit": 10},
            ),
            (
                "search from:b",
                "search",
                {"query": "from:b", "folder": None, "limit": 10},
            ),
        ]
        _execute_chain(ops, ["acct1"])
        assert mock_soft.call_count == 1
        assert client.disconnect.call_count == 1

    @patch("courier.__main__._make_client_soft")
    def test_sequential_accounts_two_connections(self, mock_soft):
        mock_soft.return_value = self._make_client()
        ops = [("search q", "search", {"query": "q", "folder": None, "limit": 10})]
        _execute_chain(ops, ["acct1", "acct2"])
        assert mock_soft.call_count == 2

    @patch("courier.__main__._make_client_soft")
    def test_failed_connection_produces_error_envelope(self, mock_soft):
        """E1: the old shape fabricated a clean-empty search result for
        a server that was never reached."""
        mock_soft.return_value = None
        ops = [("search q", "search", {"query": "q", "folder": None, "limit": 10})]
        result = _execute_chain(ops, ["acct1"])
        assert result["search q"]["acct1"] == {"error": "connection failed"}

    @patch("courier.__main__._make_client_soft")
    def test_runtime_error_produces_error_dict(self, mock_soft):
        client = MagicMock()
        client.search_emails.side_effect = RuntimeError("timeout")
        mock_soft.return_value = client
        ops = [("search q", "search", {"query": "q", "folder": None, "limit": 10})]
        result = _execute_chain(ops, ["acct1"])
        assert "error" in result["search q"]["acct1"]
        assert "timeout" in result["search q"]["acct1"]["error"]

    @patch("courier.__main__._make_client_soft")
    def test_valueerror_propagates(self, mock_soft):
        client = MagicMock()
        client.search_emails.side_effect = ValueError("bad query")
        mock_soft.return_value = client
        ops = [("search q", "search", {"query": "q", "folder": None, "limit": 10})]
        with pytest.raises(ValueError, match="bad query"):
            _execute_chain(ops, ["acct1"])

    @patch("courier.__main__._make_client_soft")
    def test_multi_account_results_keyed_by_account(self, mock_soft):
        def make(name):
            c = MagicMock()
            c.search_emails.return_value = _fake_search_result(name)
            return c

        mock_soft.side_effect = make
        ops = [("search q", "search", {"query": "q", "folder": None, "limit": 10})]
        result = _execute_chain(ops, ["acct1", "acct2"])
        assert "acct1" in result["search q"]
        assert "acct2" in result["search q"]


# ---------------------------------------------------------------------------
# CLI: chain dispatch through main(). Tests use _run_chain directly to avoid
# rewriting sys.argv globally; argv parsing is covered by TestSplitChainArgv.
# ---------------------------------------------------------------------------


def _patch_config(imap_name: str = "default"):
    from courier.config import CourierConfig, ImapBlock

    block = ImapBlock(
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        use_ssl=True,
    )
    cfg = CourierConfig(imap_blocks={imap_name: block}, _default_imap=imap_name)
    return patch("courier.__main__.load_config", return_value=cfg)


def _patch_search_client(result=None):
    if result is None:
        result = _fake_search_result()

    def factory(name):
        client = MagicMock()
        client.search_emails.return_value = result
        return client

    return patch("courier.__main__._make_client_soft", side_effect=factory)


class TestRunChain:
    def test_two_searches_produce_two_keys(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client():
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "json")
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert len(data) == 2
        assert any("foo" in k for k in data)
        assert any("bar" in k for k in data)

    def test_mixed_search_and_read(self, capsys):
        _apply_global_flags([])

        def factory(name):
            client = MagicMock()
            client.search_emails.return_value = _fake_search_result()
            email = MagicMock()
            email.from_ = MagicMock(__str__=lambda s: "alice@example.com")
            email.to = []
            email.subject = "Hello"
            email.date = None
            email.flags = []
            email.message_id = "<hello@example.com>"
            email.content.html = None
            email.content.text = "body"
            email.in_reply_to = None
            email.references = None
            email.cc = []
            email.attachments = []
            client.fetch_email.return_value = email
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            code = _run_chain(
                [("search", ["foo"]), ("read", ["-f", "INBOX", "-u", "1"])], "json"
            )
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        search_key = next(k for k in data if k.startswith("search"))
        read_key = next(k for k in data if k.startswith("read"))
        assert "results" in data[search_key]["default"]
        assert "subject" in data[read_key]["default"]

    def test_no_results_returns_exit_1(self, capsys):
        _apply_global_flags([])
        empty = {
            "results": [],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": None,
            },
        }
        with _patch_config(), _patch_search_client(empty):
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "json")
        assert code == 1

    def test_invalid_query_returns_exit_2(self, capsys):
        _apply_global_flags([])
        client = MagicMock()
        client.search_emails.side_effect = ValueError("bad query")
        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", return_value=client),
        ):
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "json")
        assert code == 2

    def test_read_missing_uid_returns_exit_2(self, capsys):
        _apply_global_flags([])
        with _patch_config():
            code = _run_chain([("search", ["foo"]), ("read", ["-f", "INBOX"])], "json")
        assert code == 2

    def test_unknown_format_returns_exit_2(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client():
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "yaml")
        assert code == 2

    def test_text_format_renders_op_key_headers(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client():
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "text")
        assert code == 0
        out = capsys.readouterr().out
        assert "=== search foo ===" in out
        assert "=== search bar ===" in out

    def test_oneline_format_first_column_op_key(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client():
            code = _run_chain([("search", ["foo"]), ("search", ["bar"])], "oneline")
        assert code == 0
        first = capsys.readouterr().out.strip().splitlines()[0]
        assert first.split("\t")[0].startswith("search ")

    def test_chain_default_limit_applied_to_all_verbs(self, capsys):
        _apply_global_flags([])
        seen_limits: List[int] = []

        def factory(name):
            client = MagicMock()

            def fake_search(query, folder=None, limit=10, no_cache=False):
                seen_limits.append(limit)
                return _fake_search_result(query)

            client.search_emails.side_effect = fake_search
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            code = _run_chain(
                [("search", ["a"]), ("search", ["b"]), ("search", ["c"])],
                "json",
                {"limit": 50},
            )
        assert code == 0
        assert seen_limits == [50, 50, 50]

    def test_chain_default_limit_overridden_per_verb(self, capsys):
        _apply_global_flags([])
        seen_limits: List[int] = []

        def factory(name):
            client = MagicMock()

            def fake_search(query, folder=None, limit=10, no_cache=False):
                seen_limits.append(limit)
                return _fake_search_result(query)

            client.search_emails.side_effect = fake_search
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            code = _run_chain(
                [("search", ["-n", "5", "a"]), ("search", ["b"])],
                "json",
                {"limit": 50},
            )
        assert code == 0
        assert seen_limits == [5, 50]

    def test_chain_default_folder_applied_to_all_verbs(self, capsys):
        _apply_global_flags([])
        seen_folders: List[Optional[str]] = []

        def factory(name):
            client = MagicMock()

            def fake_search(query, folder=None, limit=10, no_cache=False):
                seen_folders.append(folder)
                return _fake_search_result(query)

            client.search_emails.side_effect = fake_search
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            code = _run_chain(
                [("search", ["a"]), ("search", ["b"])],
                "json",
                {"folder": "Sent"},
            )
        assert code == 0
        assert seen_folders == ["Sent", "Sent"]

    def test_chain_default_folder_used_for_read_when_unset(self, capsys):
        _apply_global_flags([])

        def factory(name):
            client = MagicMock()
            client.search_emails.return_value = _fake_search_result()
            email = MagicMock()
            email.from_ = MagicMock(__str__=lambda s: "alice@example.com")
            email.to = []
            email.subject = "Hello"
            email.date = None
            email.flags = []
            email.message_id = "<hello@example.com>"
            email.content.html = None
            email.content.text = "body"
            email.in_reply_to = None
            email.references = None
            email.cc = []
            email.attachments = []
            client.fetch_email.return_value = email
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            # read has only -u; chain default supplies folder
            code = _run_chain(
                [("search", ["foo"]), ("read", ["-u", "1"])],
                "json",
                {"folder": "INBOX"},
            )
        assert code == 0


# ---------------------------------------------------------------------------
# Single-op typer dispatch is unchanged (smoke test the chain didn't break it)
# ---------------------------------------------------------------------------


class TestSingleOpUnchanged:
    def test_search_single_keyword_keeps_op_key_outer(self):
        with _patch_config(), _patch_search_client():
            result = runner.invoke(app, ["search", "from:alice"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        key = next(iter(data))
        assert key.startswith("search ")
        assert "default" in data[key]
        assert "results" in data[key]["default"]

    def test_search_no_results_exit_1(self):
        empty = {
            "results": [],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": None,
            },
        }
        with _patch_config(), _patch_search_client(empty):
            result = runner.invoke(app, ["search", "from:nobody"])
        assert result.exit_code == 1

    def test_search_no_cache_forwarded_to_client(self):
        captured = {}

        def factory(name):
            client = MagicMock()
            client.search_emails.return_value = _fake_search_result()
            captured["client"] = client
            return client

        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", side_effect=factory),
        ):
            result = runner.invoke(app, ["search", "from:alice", "--no-cache"])

        assert result.exit_code == 0
        captured["client"].search_emails.assert_called_once_with(
            "from:alice", folder=None, limit=50, no_cache=True
        )


# ---------------------------------------------------------------------------
# Apply global flags
# ---------------------------------------------------------------------------


class TestApplyGlobalFlags:
    def test_imap_repeats(self):
        _apply_global_flags(["--imap", "a", "--imap", "b"])
        from courier.__main__ import _imap_names

        assert _imap_names == ["a", "b"]
        # reset for other tests
        _apply_global_flags([])

    def test_imap_equals_form(self):
        _apply_global_flags(["--imap=a", "--imap=b"])
        from courier.__main__ import _imap_names

        assert _imap_names == ["a", "b"]
        _apply_global_flags([])

    def test_all_imap_flag(self):
        _apply_global_flags(["-A"])
        from courier.__main__ import _all_imap

        assert _all_imap is True
        _apply_global_flags([])

    def test_config_flag(self):
        _apply_global_flags(["-c", "/tmp/x.toml"])
        from courier.__main__ import _config_path

        assert _config_path == "/tmp/x.toml"
        _apply_global_flags([])

    def test_version_short_circuits_with_systemexit(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            _apply_global_flags(["--version"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# _rewrite_argv
# ---------------------------------------------------------------------------


class TestRewriteArgv:
    def test_compose_dash_i_not_hoisted_as_imap(self):
        # Regression: issue #36 (updated for v1.1.9 flag layout).
        # `-i` after a subcommand is the subcommand's `--identity`,
        # never the global `--imap`; the hoist preprocessor must
        # leave it alone.
        out = _rewrite_argv(
            [
                "--imap",
                "acct",
                "compose",
                "--to",
                "alice@example.com",
                "--subject",
                "hi",
                "--body",
                "x",
                "-i",
                "partnerships",
                "--send",
            ]
        )
        compose_idx = out.index("compose")
        assert out[compose_idx + 1 :] == [
            "--to",
            "alice@example.com",
            "--subject",
            "hi",
            "--body",
            "x",
            "-i",
            "partnerships",
            "--send",
        ]

    def test_long_imap_after_subcommand_still_hoisted(self):
        # `--imap` after the subcommand is hoisted to before it so the
        # group callback sees it (the original purpose of the preprocessor).
        out = _rewrite_argv(["search", "foo", "--imap", "acct"])
        assert out.index("--imap") < out.index("search")
        assert out.index("acct") == out.index("--imap") + 1


class TestExitCodeTrichotomy:
    """0 hits; 1 clean zero; 2 zero-with-failures or total failure."""

    EMPTY = {
        "results": [],
        "provenance": {
            "source": "remote",
            "indexed_at": None,
            "fell_back_reason": None,
        },
    }

    def test_hits_exit_0(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client(_fake_search_result()):
            assert _run_chain([("search", ["foo"])], "json") == 0

    def test_clean_zero_exits_1(self, capsys):
        _apply_global_flags([])
        with _patch_config(), _patch_search_client(self.EMPTY):
            assert _run_chain([("search", ["foo"])], "json") == 1

    def test_all_blocks_connect_failed_exits_2(self, capsys):
        _apply_global_flags([])
        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", return_value=None),
        ):
            assert _run_chain([("search", ["foo"])], "json") == 2

    def test_zero_with_folder_failures_exits_2(self, capsys):
        _apply_global_flags([])
        partial = dict(self.EMPTY)
        partial["folders_failed"] = [{"folder": "INBOX", "error": "timeout"}]
        with _patch_config(), _patch_search_client(partial):
            assert _run_chain([("search", ["foo"])], "json") == 2

    def test_hits_with_failures_still_exit_0(self, capsys):
        _apply_global_flags([])
        hit = _fake_search_result()
        hit["folders_failed"] = [{"folder": "Archive", "error": "timeout"}]
        with _patch_config(), _patch_search_client(hit):
            assert _run_chain([("search", ["foo"])], "json") == 0

    def test_untranslatable_block_error_exits_2(self, capsys):
        _apply_global_flags([])
        client = MagicMock()
        client.search_emails.side_effect = RuntimeError(
            "label: cannot be expressed on the imap backend"
        )
        with (
            _patch_config(),
            patch("courier.__main__._make_client_soft", return_value=client),
        ):
            assert _run_chain([("search", ["label:x"])], "json") == 2

    def test_search_help_documents_the_trichotomy(self):
        from courier.__main__ import _SEARCH_CLI_HELP

        assert "0 on hits" in _SEARCH_CLI_HELP
        assert "clean zero" in _SEARCH_CLI_HELP
        assert "folders_failed" in _SEARCH_CLI_HELP


class TestChainTextFormatFailures:
    """Text mode surfaces folders_failed, not just JSON."""

    def test_folder_failures_render_in_text_output(self):
        from courier.__main__ import _format_chain_text

        result = {
            "search q": {
                "acct1": {
                    "results": [],
                    "provenance": {"source": "remote", "indexed_at": None},
                    "folders_failed": [{"folder": "INBOX", "error": "read timeout"}],
                }
            }
        }
        text = _format_chain_text(result)
        assert "folder failed" in text
        assert "read timeout" in text
