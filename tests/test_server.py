"""Tests for the server module."""

import argparse
import logging
from contextlib import AsyncExitStack
from unittest import mock

import pytest
from mcp.server.fastmcp import FastMCP

from courier.config import CourierConfig, ImapBlock
from courier.mcp_server import create_server, main, server_lifespan


def _block(**kwargs) -> ImapBlock:
    """Build a minimal ImapBlock with defaults overridable per-test."""
    defaults = {
        "host": "imap.example.com",
        "port": 993,
        "username": "test@example.com",
        "password": "password",
    }
    defaults.update(kwargs)
    return ImapBlock(**defaults)


class TestServer:
    """Tests for the server module."""

    def test_create_server(self):
        """Test server creation with default configuration."""
        mock_config = CourierConfig(
            imap_blocks={"test": _block(allowed_folders=["INBOX", "Sent"])},
        )

        with mock.patch("courier.mcp_server.load_config", return_value=mock_config):
            server = create_server()

            assert isinstance(server, FastMCP)
            assert server.name == "Courier"
            assert server._config == mock_config

            with mock.patch(
                "courier.mcp_server.register_resources"
            ) as mock_register_resources:
                with mock.patch(
                    "courier.mcp_server.register_tools"
                ) as mock_register_tools:
                    create_server()
                    assert mock_register_resources.called
                    assert mock_register_tools.called

    def test_create_server_with_debug(self):
        """Test server creation with debug mode enabled."""
        mock_config = CourierConfig(
            imap_blocks={
                "test": _block(host="localhost", username="test", password="pw")
            },
        )
        with mock.patch("courier.mcp_server.load_config", return_value=mock_config):
            with mock.patch("courier.mcp_server.logger") as mock_logger:
                create_server(debug=True)
                mock_logger.setLevel.assert_called_with(logging.DEBUG)

    def test_create_server_with_config_path(self):
        """Test server creation with a specific config path."""
        config_path = "test_config.toml"

        with mock.patch("courier.mcp_server.load_config") as mock_load_config:
            create_server(config_path=config_path)
            mock_load_config.assert_called_with(config_path)

    @pytest.mark.asyncio
    async def test_server_lifespan(self):
        """Test server lifespan context manager."""
        mock_server = mock.MagicMock()
        block = _block()
        mock_config = CourierConfig(imap_blocks={"test": block})
        mock_server._config = mock_config

        with mock.patch("courier.mcp_server.ImapClient") as MockImapClient:
            mock_client = MockImapClient.return_value

            async with AsyncExitStack() as stack:
                context = await stack.enter_async_context(server_lifespan(mock_server))

                MockImapClient.assert_called_once_with(
                    block, local_cache=None, world_as_of=None
                )
                mock_client.connect.assert_called_once()
                assert context["imap_clients"]["test"] == mock_client
                assert context["default_imap"] == "test"

            mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_server_lifespan_fallback_config(self):
        """Test server lifespan with fallback config loading."""
        mock_server = mock.MagicMock()
        mock_server._config = None

        mock_config = CourierConfig(imap_blocks={"test": _block()})

        with mock.patch(
            "courier.mcp_server.load_config", return_value=mock_config
        ) as mock_load_config:
            with mock.patch("courier.mcp_server.ImapClient"):
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(server_lifespan(mock_server))
                    mock_load_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_server_lifespan_invalid_config(self):
        """Test server lifespan with invalid config."""
        mock_server = mock.MagicMock()
        mock_server._config = "not a CourierConfig object"

        with pytest.raises(TypeError, match="Invalid server configuration"):
            async with server_lifespan(mock_server):
                pass

    def test_status_tool(self):
        """Test the status tool."""
        mock_config = CourierConfig(
            imap_blocks={"test": _block(allowed_folders=["INBOX", "Sent"])},
        )

        with mock.patch("courier.mcp_server.load_config", return_value=mock_config):
            server = create_server()
            assert server is not None

    def test_main_function(self):
        """Test the main function."""
        test_args = ["--config", "test_config.toml", "--debug", "--dev"]

        with mock.patch("sys.argv", ["server.py"] + test_args):
            with mock.patch("courier.mcp_server.create_server") as mock_create_server:
                with mock.patch(
                    "courier.mcp_server.argparse.ArgumentParser.parse_args"
                ) as mock_parse_args:
                    mock_args = argparse.Namespace(
                        config="test_config.toml",
                        debug=True,
                        dev=True,
                        version=False,
                    )
                    mock_parse_args.return_value = mock_args

                    mock_server = mock.MagicMock()
                    mock_create_server.return_value = mock_server

                    with mock.patch("courier.mcp_server.logger") as mock_logger:
                        main()

                        mock_create_server.assert_called_once_with(
                            "test_config.toml", True
                        )
                        mock_server.run.assert_called_once()
                        mock_logger.setLevel.assert_called_with(logging.DEBUG)

    def test_main_refuses_an_invalid_bound(self, monkeypatch, capsys):
        """A bad WORLD_AS_OF stops main() with one line on stderr and exit 1,
        before any server is built."""
        monkeypatch.setenv("WORLD_AS_OF", "garbage")

        with (
            mock.patch("sys.argv", ["server.py"]),
            mock.patch("courier.mcp_server.create_server") as mock_create_server,
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code == 1
        mock_create_server.assert_not_called()
        err = capsys.readouterr().err
        assert err.startswith("Error: WORLD_AS_OF='garbage'")
        assert len(err.splitlines()) == 1

    def test_main_env_config(self, monkeypatch):
        """Test main function with config from environment variable."""
        monkeypatch.setenv("COURIER_CONFIG", "env_config.toml")

        with mock.patch("sys.argv", ["server.py"]):
            with mock.patch("courier.mcp_server.create_server") as mock_create_server:
                with mock.patch(
                    "courier.mcp_server.argparse.ArgumentParser.parse_args"
                ) as mock_parse_args:
                    mock_args = argparse.Namespace(
                        config="env_config.toml",
                        debug=False,
                        dev=False,
                        version=False,
                    )
                    mock_parse_args.return_value = mock_args

                    main()
                    mock_create_server.assert_called_once_with("env_config.toml", False)


class TestStartupConnectFailure:
    """A block that cannot connect at startup ends the server in one line."""

    @pytest.mark.asyncio
    async def test_lifespan_names_the_block(self):
        mock_server = mock.MagicMock()
        mock_server._config = CourierConfig(imap_blocks={"work": _block()})
        with mock.patch("courier.mcp_server.ImapClient") as MockImapClient:
            MockImapClient.return_value.connect.side_effect = ConnectionError(
                "Failed to connect to IMAP server: token revoked"
            )
            with pytest.raises(ConnectionError, match=r"^\[imap\.work\] Failed"):
                async with server_lifespan(mock_server):
                    pass

    def test_wrapped_connect_failure_is_one_line(self, capsys):
        from courier.mcp_server import run_server

        server = mock.MagicMock()
        server.run.side_effect = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ConnectionError("[imap.work] Failed to connect: token revoked")],
        )
        with pytest.raises(SystemExit) as exc:
            run_server(server)
        assert exc.value.code == 1
        assert capsys.readouterr().err == (
            "Error: [imap.work] Failed to connect: token revoked\n"
        )

    def test_other_errors_propagate(self):
        from courier.mcp_server import run_server

        server = mock.MagicMock()
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ConnectionError("down"), RuntimeError("bug")],
        )
        server.run.side_effect = group
        with pytest.raises(ExceptionGroup) as exc:
            run_server(server)
        assert exc.value is group
