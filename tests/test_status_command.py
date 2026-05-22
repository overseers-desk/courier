"""Tests for the `mailroom status` connection-probe table.

The CLI ``status`` command runs an IMAP login per [imap.NAME] and an
EHLO + optional auth per [smtp.NAME] sequentially, then prints a
short table. Probes are mocked here so the tests do not require live
servers.
"""

from unittest.mock import MagicMock, patch

from mailroom.__main__ import (
    _format_age,
    _print_status_table,
    _probe_all,
    _probe_cache,
    _probe_imap,
    _probe_smtp,
)
from mailroom.config import ImapBlock, LocalCacheConfig, MailroomConfig, SmtpConfig
from mailroom.local_cache import EligibilityResult


def _imap_block(name: str = "acc") -> ImapBlock:
    return ImapBlock(
        host=f"imap.{name}.example.com",
        port=993,
        username=f"user@{name}.example.com",
        password="p",
        use_ssl=True,
    )


def _smtp_block(
    name: str = "out",
    *,
    username: str = "",
    password: str = "",
    port: int = 587,
) -> SmtpConfig:
    return SmtpConfig(
        host=f"smtp.{name}.example.com",
        port=port,
        username=username or None,
        password=password or None,
    )


class TestProbeImap:
    """`_probe_imap` returns 'ok' on success and 'FAIL: ...' on connect error."""

    def test_ok_when_connect_succeeds(self):
        with patch("mailroom.__main__.ImapClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            assert _probe_imap(_imap_block()) == "ok"
            instance.connect.assert_called_once()
            instance.disconnect.assert_called_once()

    def test_fail_message_carries_exception_text(self):
        with patch("mailroom.__main__.ImapClient") as mock_cls:
            instance = MagicMock()
            instance.connect.side_effect = ConnectionError("login refused")
            mock_cls.return_value = instance
            assert _probe_imap(_imap_block()) == "FAIL: login refused"


class TestProbeSmtp:
    """`_probe_smtp` covers the three blocks the table must distinguish.

    A block with credentials authenticates ('ok'); a credential-less
    template stops at EHLO+STARTTLS ('ok (template, no auth)'); a
    block whose server is unreachable surfaces 'FAIL: ...'.
    """

    def test_creds_block_authenticates(self):
        with patch("smtplib.SMTP") as mock_cls:
            conn = MagicMock()
            mock_cls.return_value = conn
            assert _probe_smtp(_smtp_block(username="u", password="p")) == "ok"
            conn.starttls.assert_called_once()
            conn.login.assert_called_once_with("u", "p")
            conn.quit.assert_called_once()

    def test_template_block_stops_at_starttls(self):
        with patch("smtplib.SMTP") as mock_cls:
            conn = MagicMock()
            mock_cls.return_value = conn
            assert _probe_smtp(_smtp_block()) == "ok (template, no auth)"
            conn.starttls.assert_called_once()
            conn.login.assert_not_called()

    def test_smtps_uses_ssl_factory(self):
        """Port 465 picks SMTP_SSL; STARTTLS is not issued."""
        with patch("smtplib.SMTP_SSL") as mock_cls:
            conn = MagicMock()
            mock_cls.return_value = conn
            assert _probe_smtp(_smtp_block(port=465)) == "ok (template, no auth)"
            conn.starttls.assert_not_called()

    def test_connect_failure_surfaces(self):
        with patch("smtplib.SMTP", side_effect=OSError("network unreachable")):
            assert "FAIL" in _probe_smtp(_smtp_block())


class TestProbeAll:
    """`_probe_all` runs probes sequentially in config order, IMAP then SMTP."""

    def test_orders_imap_then_smtp(self):
        cfg = MailroomConfig(
            imap_blocks={"a": _imap_block("a"), "b": _imap_block("b")},
            smtp_blocks={"out": _smtp_block("out")},
        )
        with (
            patch("mailroom.__main__._probe_imap", return_value="ok"),
            patch("mailroom.__main__._probe_smtp", return_value="ok"),
        ):
            rows = _probe_all(cfg)
        kinds = [r[1] for r in rows]
        assert kinds == ["imap", "imap", "smtp"]
        names = [r[0] for r in rows]
        assert names == ["a", "b", "out"]

    def test_calls_probes_in_sequence(self):
        """Each probe runs to completion before the next one starts.

        Verifies serial dispatch: if probe 1 sets a flag at exit and
        probe 2 reads that flag at entry, probe 2 must see it set.
        Under parallel dispatch this property is not guaranteed.
        """
        cfg = MailroomConfig(
            imap_blocks={"a": _imap_block("a"), "b": _imap_block("b")},
            smtp_blocks={},
        )
        order: list = []

        def probe(block):
            order.append(block.host)
            return "ok"

        with patch("mailroom.__main__._probe_imap", side_effect=probe):
            _probe_all(cfg)

        # Both probes ran; the sequence is the config order.
        assert order == ["imap.a.example.com", "imap.b.example.com"]


class TestPrintStatusTable:
    """Output is a header line plus aligned rows; empty config has its own line."""

    def test_renders_aligned_columns(self, capsys):
        rows = [
            ("acc1", "imap", "imap.example.com:993", "ok", "ok (12m old)"),
            ("acc-with-long-name", "imap", "imap.example.com:993", "FAIL: x", "-"),
        ]
        _print_status_table(rows)
        out = capsys.readouterr().out.splitlines()
        # First line is the version stamp; second is the header; rest are rows.
        assert out[0].startswith("mailroom ")
        assert "NAME" in out[1] and "STATUS" in out[1]
        # Each row carries every field, and every row has the same total
        # width as the header (the fixed-width fmt string pads the last
        # column out, so column boundaries line up).
        header_width = len(out[1])
        for row, expected in zip(out[2:], rows):
            assert len(row) == header_width
            for field in expected:
                assert field in row

    def test_empty_config_prints_marker(self, capsys):
        _print_status_table([])
        out = capsys.readouterr().out
        assert "no [imap.*] or [smtp.*] blocks configured" in out


class TestFormatAge:
    """`_format_age` picks the largest whole unit and clamps negatives."""

    def test_units(self):
        assert _format_age(45) == "45s"
        assert _format_age(90) == "1m"
        assert _format_age(750) == "12m"
        assert _format_age(7200) == "2h"
        assert _format_age(200000) == "2d"

    def test_negative_clamps_to_zero(self):
        assert _format_age(-5) == "0s"


def _opted_in_block() -> ImapBlock:
    """An [imap.NAME] block carrying a maildir, i.e. opted into the cache."""
    return ImapBlock(
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="p",
        use_ssl=True,
        maildir="/tmp/maildir",
    )


def _cache_cfg(block: ImapBlock, max_staleness_seconds: int = 4000) -> MailroomConfig:
    """A config whose [local_cache] table opts the given block in."""
    return MailroomConfig(
        imap_blocks={"a": block},
        smtp_blocks={},
        local_cache=LocalCacheConfig(max_staleness_seconds=max_staleness_seconds),
    )


class TestProbeCache:
    """`_probe_cache` renders the CACHE cell from backend eligibility.

    A block not opted into the cache renders ``"-"``; an opted-in block
    renders freshness ('ok'), staleness ('STALE' with age and budget),
    or the backend-unavailable reasons ('mu not found' / 'no index').
    """

    def test_dash_when_no_local_cache_configured(self):
        cfg = MailroomConfig(imap_blocks={"a": _opted_in_block()}, smtp_blocks={})
        assert _probe_cache(cfg, cfg.imap_blocks["a"]) == "-"

    def test_dash_when_block_has_no_maildir(self):
        cfg = _cache_cfg(_imap_block())  # _imap_block() carries no maildir
        assert _probe_cache(cfg, cfg.imap_blocks["a"]) == "-"

    def test_ok_when_index_fresh(self):
        from datetime import datetime, timezone

        block = _opted_in_block()
        cfg = _cache_cfg(block)
        backend = MagicMock()
        backend.is_eligible.return_value = EligibilityResult(True)
        backend.index_mtime_iso.return_value = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        with patch("mailroom.__main__._get_mu_backend", return_value=backend):
            cell = _probe_cache(cfg, block)
        assert cell.startswith("ok (") and cell.endswith("old)")

    def test_stale_reports_age_and_budget(self):
        from datetime import datetime, timedelta, timezone

        block = _opted_in_block()
        cfg = _cache_cfg(block, max_staleness_seconds=3600)
        backend = MagicMock()
        backend.is_eligible.return_value = EligibilityResult(False, "stale")
        old = datetime.now(timezone.utc) - timedelta(days=2)
        backend.index_mtime_iso.return_value = old.isoformat(timespec="seconds")
        with patch("mailroom.__main__._get_mu_backend", return_value=backend):
            cell = _probe_cache(cfg, block)
        assert cell.startswith("STALE (") and "max 1h" in cell

    def test_mu_missing_reason(self):
        block = _opted_in_block()
        cfg = _cache_cfg(block)
        backend = MagicMock()
        backend.is_eligible.return_value = EligibilityResult(False, "mu_missing")
        with patch("mailroom.__main__._get_mu_backend", return_value=backend):
            assert _probe_cache(cfg, block) == "mu not found"

    def test_db_missing_reason(self):
        block = _opted_in_block()
        cfg = _cache_cfg(block)
        backend = MagicMock()
        backend.is_eligible.return_value = EligibilityResult(False, "db_missing")
        with patch("mailroom.__main__._get_mu_backend", return_value=backend):
            assert _probe_cache(cfg, block) == "no index"
