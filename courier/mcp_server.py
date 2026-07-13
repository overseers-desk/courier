"""Courier MCP server — exposes email operations as MCP tools."""

import argparse
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

from mcp.server.fastmcp import FastMCP

from courier import __version__
from courier.config import CourierConfig, load_config
from courier.errors import WorldAsOfInvalid
from courier.imap_client import ImapClient
from courier.local_cache import MuBackend
from courier.logging_setup import setup_logging
from courier.mcp_protocol import extend_server
from courier.resources import register_resources
from courier.tools import register_tools
from courier.world_bound import world_as_of

# Set up logging: prefer the local syslog socket so MCP-server warnings
# survive process restarts, with a timestamped stderr fallback when no
# syslog daemon is reachable.
setup_logging(
    logging.INFO,
    stderr_format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("courier")


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict]:
    """Server lifespan manager to handle IMAP client lifecycle.

    Creates one ``ImapClient`` per configured [imap.NAME] block and
    yields them as a dict keyed by block name.

    Args:
        server: MCP server instance

    Yields:
        Context dictionary with ``imap_clients`` dict and ``default_imap``
    """
    # WORLD_AS_OF hard-fails at startup: the server must refuse to start
    # rather than silently serve an unbounded world under a bad bound.
    try:
        bound = world_as_of()
    except WorldAsOfInvalid as exc:
        logger.error(f"Refusing to start: {exc}")
        raise

    config_attr = getattr(server, "_config", None)
    config: CourierConfig
    if config_attr is None:
        config = load_config()
    elif isinstance(config_attr, CourierConfig):
        config = config_attr
    else:
        raise TypeError("Invalid server configuration")

    mu_backend = MuBackend(config.local_cache) if config.local_cache else None

    clients: Dict[str, ImapClient] = {}
    try:
        for name, block in config.imap_blocks.items():
            logger.info(f"Connecting to IMAP server for [imap.{name}]...")
            client = ImapClient(block, local_cache=mu_backend, world_as_of=bound)
            client.connect()
            clients[name] = client

        yield {
            "imap_clients": clients,
            "default_imap": config.default_imap,
        }
    finally:
        for name, client in clients.items():
            logger.info(f"Disconnecting from IMAP server for [imap.{name}]...")
            client.disconnect()


def create_server(config_path: Optional[str] = None, debug: bool = False) -> FastMCP:
    """Create and configure the MCP server.

    Args:
        config_path: Path to configuration file
        debug: Enable debug mode

    Returns:
        Configured MCP server instance
    """
    if debug:
        logger.setLevel(logging.DEBUG)

    config = load_config(config_path)

    server = FastMCP(
        "Courier",
        instructions="Email toolkit for AI assistants",
        lifespan=server_lifespan,
    )

    # Store config for access in the lifespan
    server._config = config  # type: ignore[attr-defined]

    # Create a throwaway client for tool/resource registration (not used at runtime)
    first_block = config.imap_blocks[config.default_imap]
    mu_backend = MuBackend(config.local_cache) if config.local_cache else None
    imap_client = ImapClient(first_block, local_cache=mu_backend)

    register_resources(server, imap_client)
    register_tools(server, imap_client)

    @server.tool(name="status")
    def status() -> str:
        """Get server status and configuration info."""
        lines = [
            "server: Courier",
            f"version: {__version__}",
            f"default_imap: {config.default_imap}",
            f"imap blocks: {', '.join(config.imap_blocks.keys())}",
        ]
        for name, block in config.imap_blocks.items():
            lines.append(f"  [{name}] {block.username}@{block.host}:{block.port}")
            if block.allowed_folders:
                lines.append(f"    allowed_folders: {block.allowed_folders}")
        return "\n".join(lines)

    server = extend_server(server)

    return server


def main() -> None:
    """Run the Courier MCP server."""
    parser = argparse.ArgumentParser(description="Courier MCP Server")
    parser.add_argument(
        "--config",
        help="Path to configuration file",
        default=os.environ.get("COURIER_CONFIG"),
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Enable development mode",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version information and exit",
    )
    args = parser.parse_args()

    if args.version:
        print(f"Courier MCP server version {__version__}")
        return

    if args.debug:
        logger.setLevel(logging.DEBUG)

    server = create_server(args.config, args.debug)

    # Start the server
    logger.info(
        "Starting server{}...".format(" in development mode" if args.dev else "")
    )
    server.run()


if __name__ == "__main__":
    main()
