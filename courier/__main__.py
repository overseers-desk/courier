"""Courier — email toolkit for AI assistants and command-line scripting.

All CLI commands are subcommands of `courier`. The `mcp` subcommand starts
the MCP server; every other subcommand operates directly via IMAP without
importing the mcp package.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import typer

from courier import __version__
from courier.config import (
    CourierConfig,
    ImapBlock,
    SmtpConfig,
    load_config,
    load_config_with_warnings,
)
from courier.errors import CourierError, FolderNotFound
from courier.imap_client import ImapClient
from courier.logging_setup import setup_logging
from courier.models import extract_links_batch

if TYPE_CHECKING:
    from courier.local_cache import MuBackend


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"courier {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="courier",
    help=(
        "Email toolkit for AI assistants and command-line scripting.\n\n"
        "Examples:\n\n"
        "  courier -A search 'sergio' search 'panedas' read -f INBOX -u 42\n"
        "  courier search 'from:alice@x' || courier search 'alice'\n\n"
        "Exit codes for data-returning commands (search, attachments, links): "
        "0 on success with results, 1 on success with zero results."
    ),
    no_args_is_help=True,
)

# Module-level state set by the --config callback.
_config_path: Optional[str] = None
_imap_names: List[str] = []
_all_imap: bool = False

# Process-wide MuBackend instance, lazily built when the configured
# [imap.*] blocks opt into the local cache.  Shared across ImapClient
# instances so the muhome discovery (mu info store) runs at most once.
_mu_backend_singleton: Optional["MuBackend"] = None

logger = logging.getLogger(__name__)


def _get_mu_backend(cfg: CourierConfig) -> Optional["MuBackend"]:
    """Return the shared MuBackend, building it on first use.

    Args:
        cfg: The loaded courier configuration.

    Returns:
        A ``MuBackend`` instance when ``cfg.local_cache`` is present;
        ``None`` when the local cache is not configured.
    """
    global _mu_backend_singleton
    if cfg.local_cache is None:
        return None
    if _mu_backend_singleton is None:
        from courier.local_cache import MuBackend

        _mu_backend_singleton = MuBackend(cfg.local_cache)
    return _mu_backend_singleton


@app.callback()
def _global_options(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to TOML configuration file.",
        envvar="COURIER_CONFIG",
    ),
    imap_names: List[str] = typer.Option(
        [],
        "--imap",
        help=(
            "[imap.NAME] block to use. Uses default_imap if omitted. "
            "Repeat for several blocks."
        ),
    ),
    all_imap: bool = typer.Option(
        False,
        "--all-imap",
        "-A",
        help="Use every configured [imap.NAME] block.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose logging."
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    global _config_path, _imap_names, _all_imap
    _config_path = config
    _imap_names = list(imap_names)
    _all_imap = all_imap
    level = logging.DEBUG if verbose else logging.WARNING
    setup_logging(level)
    if ctx.invoked_subcommand not in ("install-claude-command", "config-sample"):
        nudge = _claude_registration_status()
        if nudge:
            print(nudge, file=sys.stderr)


def _resolve_imap_names() -> List[str]:
    """Resolve module-level [imap.*] flags to a deduplicated list of names.

    Returns the [imap.NAME] block names to search, validated against the
    config. Errors hard if a name is unknown or if ``--all-imap`` is set
    with no [imap.*] blocks configured. Emits a stderr note when
    ``--all-imap`` overrides explicit ``-i`` flags.
    """
    try:
        cfg = load_config(_config_path)
    except (ValueError, FileNotFoundError, Exception) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    if _all_imap:
        if not cfg.imap_blocks:
            typer.echo("Error: no [imap.*] blocks configured", err=True)
            raise typer.Exit(1)
        if _imap_names:
            typer.echo("note: --all-imap overrides --imap values", err=True)
        return list(cfg.imap_blocks.keys())
    if not _imap_names:
        return [cfg.default_imap]
    seen: set = set()
    resolved: List[str] = []
    for name in _imap_names:
        if name not in cfg.imap_blocks:
            available = list(cfg.imap_blocks.keys())
            typer.echo(
                f"Error: unknown [imap.{name}] block. Available: {available}",
                err=True,
            )
            raise typer.Exit(1)
        if name not in seen:
            resolved.append(name)
            seen.add(name)
    return resolved


def _make_client(imap_override: Optional[str] = None) -> ImapClient:
    """Create and connect an ImapClient.

    Args:
        imap_override: If given, use this [imap.NAME] block instead of
            the global ``--imap`` flag.

    Raises:
        typer.Exit: On config error, unknown [imap.NAME] block,
            multi-block misuse, or IMAP connect failure.
    """
    try:
        cfg = load_config(_config_path)
    except (ValueError, FileNotFoundError, Exception) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    if imap_override is None:
        if _all_imap or len(_imap_names) > 1:
            typer.echo(
                "Error: this command does not support multi-block flags "
                "(--all-imap or repeated --imap)",
                err=True,
            )
            raise typer.Exit(1)
        single = _imap_names[0] if _imap_names else None
        name = single or cfg.default_imap
    else:
        name = imap_override
    if name not in cfg.imap_blocks:
        available = list(cfg.imap_blocks.keys())
        typer.echo(
            f"Error: unknown [imap.{name}] block. Available: {available}",
            err=True,
        )
        raise typer.Exit(1)
    if not _imap_names and imap_override is None:
        typer.echo(f"Using [imap.{name}]", err=True)
    block = cfg.imap_blocks[name]
    client = ImapClient(block, local_cache=_get_mu_backend(cfg))
    try:
        client.connect()
    except Exception as exc:
        typer.echo(f"Error: failed to connect to IMAP server: {exc}", err=True)
        raise typer.Exit(1)
    return client


def _make_client_soft(name: str) -> Optional[ImapClient]:
    """Connect for one [imap.NAME] block, returning None on failure.

    Used by the chain executor so one unreachable block does not abort
    the whole chain. Emits a stderr warning on failure.
    """
    try:
        cfg = load_config(_config_path)
    except (ValueError, FileNotFoundError, Exception) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    if name not in cfg.imap_blocks:
        available = list(cfg.imap_blocks.keys())
        typer.echo(
            f"Error: unknown [imap.{name}] block. Available: {available}",
            err=True,
        )
        raise typer.Exit(1)
    block = cfg.imap_blocks[name]
    client = ImapClient(block, local_cache=_get_mu_backend(cfg))
    try:
        client.connect()
    except Exception as exc:
        typer.echo(f"warning: connect failed for [imap.{name}]: {exc}", err=True)
        return None
    return client


def _resolve_single_imap_name() -> str:
    """Return the single [imap.NAME] block for non-multi commands.

    Raises:
        typer.Exit: On config error, unknown block name, or multi-block
            flags.
    """
    try:
        cfg = load_config(_config_path)
    except (ValueError, FileNotFoundError, Exception) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    if _all_imap or len(_imap_names) > 1:
        typer.echo(
            "Error: this command does not support multi-block flags "
            "(--all-imap or repeated --imap)",
            err=True,
        )
        raise typer.Exit(1)
    name = _imap_names[0] if _imap_names else cfg.default_imap
    if name not in cfg.imap_blocks:
        typer.echo(f"Error: unknown [imap.{name}] block.", err=True)
        raise typer.Exit(1)
    return name


def _empty_result_for_subcmd(subcmd: str) -> Dict[str, Any]:
    """Return a placeholder result for a failed [imap.NAME] block connection."""
    if subcmd == "search":
        return _empty_search_result()
    return {"error": "connection failed"}


def _build_op_key(subcmd: str, **kwargs: Any) -> str:
    """Build a canonical operation key string from a subcommand and its arguments.

    Non-default arguments are included; defaults are omitted so the key
    remains compact. The key for a single-command run is used as the outer
    JSON key, matching the chain output shape.
    """
    parts = [subcmd]
    if subcmd == "search":
        folder = kwargs.get("folder")
        limit = kwargs.get("limit", 50)
        query = kwargs.get("query", "")
        if folder:
            parts += ["-f", folder]
        if limit != 50:
            parts += ["--limit", str(limit)]
        if query:
            parts.append(query)
    elif subcmd == "read":
        folder = kwargs.get("folder", "")
        uid = kwargs.get("uid", 0)
        parts += ["-f", folder, "--uid", str(uid)]
    return " ".join(parts)


def _fetch_email_result(
    client: ImapClient, folder: str, uid: int, no_cache: bool = False
) -> Dict[str, Any]:
    """Fetch one email and return its JSON representation, or ``{"error": ...}``.

    Extracted so both the standalone ``read`` command and the chain executor
    share identical output structure.
    """
    email_obj = client.fetch_email(uid, folder, no_cache=no_cache)
    if not email_obj:
        return {"error": f"Email UID {uid} not found in {folder}"}
    result: Dict[str, Any] = {
        "uid": uid,
        "folder": folder,
        "from": str(email_obj.from_),
        "to": [str(to) for to in email_obj.to],
        "subject": email_obj.subject,
        "date": (email_obj.date.astimezone().isoformat() if email_obj.date else None),
        "flags": email_obj.flags,
        "message_id": email_obj.message_id,
        "content_type": ("text/html" if email_obj.content.html else "text/plain"),
        "body": (
            str(email_obj.content.html)
            if email_obj.content.html
            else str(email_obj.content.text) if email_obj.content.text else None
        ),
    }
    if email_obj.in_reply_to:
        result["in_reply_to"] = email_obj.in_reply_to
    if email_obj.references:
        result["references"] = list(email_obj.references)
    if email_obj.cc:
        result["cc"] = [str(cc) for cc in email_obj.cc]
    if email_obj.attachments:
        result["attachments"] = [
            {
                "index": i,
                "filename": att.filename,
                "size": att.size,
                "content_type": att.content_type,
            }
            for i, att in enumerate(email_obj.attachments)
        ]
    return result


def _run_op(client: ImapClient, subcmd: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch one operation against an already-connected IMAP client.

    Args:
        client: Connected ImapClient for the current [imap.NAME] block.
        subcmd: Subcommand name (``"search"`` or ``"read"``).
        kwargs: Parsed arguments for the subcommand.

    Returns:
        Per-block result dict suitable for inclusion in the chain output.
    """
    if subcmd == "search":
        return client.search_emails(
            kwargs["query"],
            folder=kwargs.get("folder"),
            limit=kwargs.get("limit", 50),
            no_cache=kwargs.get("no_cache", False),
        )
    if subcmd == "read":
        return _fetch_email_result(
            client,
            kwargs["folder"],
            kwargs["uid"],
            no_cache=kwargs.get("no_cache", False),
        )
    return {"error": f"unknown subcommand '{subcmd}'"}


def _execute_chain(
    operations: List[Tuple[str, str, Dict[str, Any]]],
    names: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Execute multiple ops block-first over one IMAP connection per block.

    Opens one connection per [imap.NAME] block, runs every operation for
    that block, then closes before moving to the next. Blocks are
    processed sequentially to avoid server-side throttling.

    Args:
        operations: List of ``(op_key, subcmd, kwargs)`` tuples. ``op_key`` is
            echoed back as the outer JSON key.
        names: [imap.NAME] block names to query, processed in order.

    Returns:
        ``{op_key: {imap_name: result_dict}}`` -- the chain output shape.

    Raises:
        ValueError: Re-raised from ``_run_op`` so the caller can map it to
            exit code 2 (invalid query syntax).
    """
    result: Dict[str, Dict[str, Any]] = {key: {} for key, _, _ in operations}
    for name in names:
        client = _make_client_soft(name)
        if client is None:
            for key, subcmd, _ in operations:
                result[key][name] = _empty_result_for_subcmd(subcmd)
            continue
        try:
            for key, subcmd, kwargs in operations:
                try:
                    result[key][name] = _run_op(client, subcmd, kwargs)
                except ValueError:
                    raise
                except Exception as exc:
                    result[key][name] = {"error": str(exc)}
        finally:
            client.disconnect()
    return result


def _parse_search_args(
    tokens: List[str],
    default_folder: Optional[str] = None,
    default_limit: int = 50,
) -> Dict[str, Any]:
    """Parse tokenised search arguments into a kwargs dict.

    ``default_folder`` and ``default_limit`` let the chain dispatcher pass
    chain-level ``-f``/``-n`` values through; per-verb tokens override them.
    """
    folder: Optional[str] = default_folder
    limit = default_limit
    query_parts: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-f", "--folder") and i + 1 < len(tokens):
            folder = tokens[i + 1]
            i += 2
        elif tok.startswith("--folder="):
            folder = tok.split("=", 1)[1]
            i += 1
        elif tok in ("-n", "--limit") and i + 1 < len(tokens):
            limit = int(tokens[i + 1])
            i += 2
        elif tok.startswith("--limit="):
            limit = int(tok.split("=", 1)[1])
            i += 1
        elif not tok.startswith("-"):
            query_parts.append(tok)
            i += 1
        else:
            i += 1
    return {"query": " ".join(query_parts), "folder": folder, "limit": limit}


def _parse_read_args(
    tokens: List[str],
    default_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse tokenised read arguments into a kwargs dict.

    ``default_folder`` lets the chain dispatcher pass a chain-level ``-f``
    value through; a per-verb folder token overrides it.
    """
    folder: Optional[str] = default_folder
    uid: Optional[int] = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-f", "--folder") and i + 1 < len(tokens):
            folder = tokens[i + 1]
            i += 2
        elif tok.startswith("--folder="):
            folder = tok.split("=", 1)[1]
            i += 1
        elif tok in ("-u", "--uid") and i + 1 < len(tokens):
            uid = int(tokens[i + 1])
            i += 2
        elif tok.startswith("--uid="):
            uid = int(tok.split("=", 1)[1])
            i += 1
        else:
            i += 1
    if folder is None or uid is None:
        raise ValueError("read requires --folder (-f) and --uid (-u)")
    return {"folder": folder, "uid": uid}


# ---------------------------------------------------------------------------
# Top-level multi-op dispatch
# ---------------------------------------------------------------------------
#
# Repeating a read-only verb (search, read) at the top level runs each as
# a separate operation in one invocation. argv is pre-scanned before typer
# runs; with one verb present, the scanner returns and typer dispatches
# normally, leaving single-op invocations and ``--help`` unchanged.

_CHAINABLE_VERBS = {"search", "read"}

_GLOBAL_FLAGS_VALUE = {"-c", "--config", "--imap"}
_GLOBAL_FLAGS_BOOL = {"-A", "--all-imap", "-v", "--verbose", "--version"}

_VERB_FLAGS_VALUE: Dict[str, set] = {
    "search": {"-f", "--folder", "-n", "--limit", "-q", "--query"},
    "read": {"-f", "--folder", "-u", "--uid"},
}


def _peel_chain_tail_flags(
    rest: List[str],
) -> Tuple[List[str], Dict[str, Any]]:
    """Peel chain-level ``-n/--limit`` and ``-f/--folder`` off the tail.

    A trailing ``-n N`` or ``-f F`` (or their long / equals forms) at the
    end of ``rest`` becomes a chain-level default applied to every chained
    verb that does not set its own value. Stops at the first non-tail-flag
    token, so a flag interior to a verb stays with that verb.
    """
    chain: Dict[str, Any] = {}
    rest = list(rest)
    while rest:
        last = rest[-1]
        if last.startswith("--limit=") and "limit" not in chain:
            try:
                chain["limit"] = int(last.split("=", 1)[1])
            except ValueError:
                break
            rest.pop()
            continue
        if last.startswith("--folder=") and "folder" not in chain:
            chain["folder"] = last.split("=", 1)[1]
            rest.pop()
            continue
        if len(rest) >= 2:
            flag = rest[-2]
            value = rest[-1]
            if flag in ("-n", "--limit") and "limit" not in chain:
                try:
                    chain["limit"] = int(value)
                except ValueError:
                    break
                rest.pop()
                rest.pop()
                continue
            if flag in ("-f", "--folder") and "folder" not in chain:
                chain["folder"] = value
                rest.pop()
                rest.pop()
                continue
        break
    return rest, chain


def _split_chain_argv(
    argv: List[str],
) -> Optional[Tuple[List[str], List[Tuple[str, List[str]]], str, Dict[str, Any]]]:
    """Detect a verb-chain invocation and split argv into its parts.

    Returns ``(global_argv, [(verb, verb_tokens), ...], output_format,
    chain_defaults)`` when two or more chainable verbs are found at command
    position, or when a single chainable verb is preceded by a top-level
    ``--format/-F`` (typer's top-level callback has no ``--format``, so that
    lone case must run through the chain executor instead of falling back);
    ``None`` otherwise so the caller can fall back to typer dispatch. A
    ``--format`` that trails a single verb stays with typer, whose per-command
    option handles it.

    Walks argv left to right in two passes. The first pass strips global
    flags and the chain-level ``--format/-F`` option. Trailing chain-level
    ``-n/--limit`` and ``-f/--folder`` flags are peeled off next so they
    apply as defaults to every chained verb. The second pass walks the
    remainder, splitting on chainable-verb tokens while respecting each
    verb's value-taking flags so a folder named ``search`` does not split
    the chain.
    """
    out_format = "json"
    format_seen_pre = False
    globals_: List[str] = []
    rest: List[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i + 1 :])
            break
        if tok in _GLOBAL_FLAGS_VALUE and i + 1 < len(argv):
            globals_.extend([tok, argv[i + 1]])
            i += 2
            continue
        if (
            tok.startswith("--")
            and "=" in tok
            and tok.split("=", 1)[0] in _GLOBAL_FLAGS_VALUE
        ):
            globals_.append(tok)
            i += 1
            continue
        if tok.startswith("--imap="):
            globals_.append(tok)
            i += 1
            continue
        if tok in _GLOBAL_FLAGS_BOOL:
            globals_.append(tok)
            i += 1
            continue
        if tok in ("--format", "-F") and i + 1 < len(argv):
            out_format = argv[i + 1]
            if not rest:
                format_seen_pre = True
            i += 2
            continue
        if tok.startswith("--format=") or tok.startswith("-F="):
            out_format = tok.split("=", 1)[1]
            if not rest:
                format_seen_pre = True
            i += 1
            continue
        rest.append(tok)
        i += 1

    if not rest or rest[0] not in _CHAINABLE_VERBS:
        return None

    rest, chain_defaults = _peel_chain_tail_flags(rest)

    verbs: List[Tuple[str, List[str]]] = []
    cur_verb: Optional[str] = None
    cur_args: List[str] = []
    skip_next = False
    j = 0
    while j < len(rest):
        tok = rest[j]
        if skip_next:
            cur_args.append(tok)
            skip_next = False
            j += 1
            continue
        if tok in ("--format", "-F") and j + 1 < len(rest):
            out_format = rest[j + 1]
            j += 2
            continue
        if tok.startswith("--format=") or tok.startswith("-F="):
            out_format = tok.split("=", 1)[1]
            j += 1
            continue
        if cur_verb is not None and tok in _VERB_FLAGS_VALUE.get(cur_verb, set()):
            cur_args.append(tok)
            skip_next = True
            j += 1
            continue
        if tok in _CHAINABLE_VERBS:
            if cur_verb is not None:
                verbs.append((cur_verb, cur_args))
            cur_verb = tok
            cur_args = []
            j += 1
            continue
        cur_args.append(tok)
        j += 1
    if cur_verb is not None:
        verbs.append((cur_verb, cur_args))

    if len(verbs) < 2 and not format_seen_pre:
        return None
    return (globals_, verbs, out_format, chain_defaults)


def _apply_global_flags(global_argv: List[str]) -> None:
    """Apply pre-parsed global flags to module-level state.

    Mirrors the typer top-level callback (``_global_options``) without
    typer dispatch, so the chain branch shares state with the rest of the
    codebase. Honours ``--version`` by printing and exiting before any
    chain work begins.
    """
    global _config_path, _imap_names, _all_imap
    cfg_path: Optional[str] = None
    imap_names: List[str] = []
    all_imap = False
    verbose = False
    i = 0
    while i < len(global_argv):
        tok = global_argv[i]
        if tok in ("-c", "--config") and i + 1 < len(global_argv):
            cfg_path = global_argv[i + 1]
            i += 2
            continue
        if tok.startswith("--config=") or tok.startswith("-c="):
            cfg_path = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--imap" and i + 1 < len(global_argv):
            imap_names.append(global_argv[i + 1])
            i += 2
            continue
        if tok.startswith("--imap="):
            imap_names.append(tok.split("=", 1)[1])
            i += 1
            continue
        if tok in ("-A", "--all-imap"):
            all_imap = True
        elif tok in ("-v", "--verbose"):
            verbose = True
        elif tok == "--version":
            typer.echo(f"courier {__version__}")
            raise SystemExit(0)
        i += 1
    _config_path = cfg_path
    _imap_names = imap_names
    _all_imap = all_imap
    level = logging.DEBUG if verbose else logging.WARNING
    setup_logging(level)


def _run_chain(
    verbs: List[Tuple[str, List[str]]],
    output_format: str,
    chain_defaults: Optional[Dict[str, Any]] = None,
) -> int:
    """Execute a parsed verb-chain. Returns the process exit code.

    Builds the operations list from each verb's parsed kwargs, resolves the
    [imap.NAME] blocks under the global flags already applied to module
    state, and dispatches to ``_execute_chain``. ``chain_defaults`` carries
    the chain-level ``-n/--limit`` and ``-f/--folder`` values peeled off
    the chain tail; per-verb tokens override them.
    """
    chain_defaults = chain_defaults or {}
    cd_folder = chain_defaults.get("folder")
    cd_limit = chain_defaults.get("limit")
    operations: List[Tuple[str, str, Dict[str, Any]]] = []
    for verb, tokens in verbs:
        if verb == "search":
            kwargs = _parse_search_args(
                tokens,
                default_folder=cd_folder,
                default_limit=cd_limit if cd_limit is not None else 50,
            )
        elif verb == "read":
            try:
                kwargs = _parse_read_args(tokens, default_folder=cd_folder)
            except ValueError as exc:
                typer.echo(f"Error: {exc}", err=True)
                return 2
        else:  # pragma: no cover - guarded by _split_chain_argv
            typer.echo(f"Error: unsupported chain verb {verb!r}", err=True)
            return 2
        op_key = _build_op_key(verb, **kwargs)
        operations.append((op_key, verb, kwargs))
    try:
        names = _resolve_imap_names()
    except typer.Exit as exc:
        return int(exc.exit_code or 1)
    try:
        result = _execute_chain(operations, names)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return 2
    if output_format == "json":
        _out(result)
    elif output_format == "text":
        typer.echo(_format_chain_text(result))
    elif output_format == "oneline":
        typer.echo(_format_chain_oneline(result))
    else:
        typer.echo(
            f"Error: unknown --format '{output_format}'. Use json, text, or oneline.",
            err=True,
        )
        return 2
    has_results = any(
        v.get("results")
        for per_block in result.values()
        for v in per_block.values()
        if isinstance(v, dict)
    )
    return 0 if has_results else 1


def _out(data: object) -> None:
    """Print data as JSON to stdout."""
    if isinstance(data, str):
        # Many tools return a JSON string; pass it through as-is.
        print(data)
    else:
        print(json.dumps(data, indent=2, default=str))


def _email_only(s: str) -> str:
    """Strip display name from ``Display Name <addr@host>``."""
    if "<" in s and ">" in s:
        return s.split("<", 1)[1].rsplit(">", 1)[0]
    return s


def _load_cfg_or_exit() -> CourierConfig:
    """Load config or exit 1 with the usual error formatting."""
    try:
        return load_config(_config_path)
    except (ValueError, FileNotFoundError, Exception) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


def _resolve_smtp_or_exit(
    identity: Any,
    imap_block: Optional[ImapBlock],
    imap_name: str,
    smtp_blocks: Dict[str, SmtpConfig],
    smtp_override: Optional[SmtpConfig],
) -> SmtpConfig:
    """Pick the SmtpConfig for this send, exiting non-zero on failure.

    Hoisted out of ``_perform_send`` so callers can resolve SMTP early
    enough to decide whether an IMAP connection is needed at all (a
    Gmail-host SMTP with ``save_sent='auto'`` resolves to ``False``, so
    no FCC and no IMAP).
    """
    if smtp_override is not None:
        return smtp_override
    from courier.identity import SmtpUnresolved, resolve_smtp_for_identity

    try:
        return resolve_smtp_for_identity(identity, imap_block, imap_name, smtp_blocks)
    except SmtpUnresolved as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


def _will_fcc(
    smtp: SmtpConfig,
    save_sent_override: Optional[bool],
    identity_fcc: Union[bool, str, None],
    fcc_imap: Optional[str],
) -> bool:
    """Decide whether the FCC step will actually file a Sent copy.

    The FCC step files a copy by IMAP APPEND, so it can only run when
    there is a target ``[imap.NAME]`` block to file into. A free-form
    ``--smtp`` send without ``--fcc`` (and a send-only identity with no
    ``imap``) has no target, so no copy is filed whatever the host
    convention would otherwise prefer; this returns ``False`` there. The
    earlier design read the SMTP block's ``save_sent`` preference even
    with no target, reporting an FCC that never ran and so defeating the
    no-copy guard downstream.

    Once a target exists, precedence is: an explicit
    ``--save-sent``/``--no-save-sent`` wins; then the identity's own
    ``fcc`` (``False`` off, a folder string on); then the SMTP block's
    host convention. BCC plays no part here: FCC and BCC are independent
    axes, so turning FCC off is done explicitly via ``fcc = false``,
    never as a side effect of setting ``bcc``.
    """
    if not fcc_imap:
        return False
    if save_sent_override is not None:
        return save_sent_override
    if identity_fcc is False:
        return False
    if isinstance(identity_fcc, str):
        return True
    return smtp.resolve_save_sent()


def _refuse_if_no_copy(
    will_fcc: bool,
    bcc: Optional[List[str]],
    sender_addr: str,
    allow_no_copy: bool,
) -> None:
    """Refuse the send if no copy of the message will be retained.

    A copy is retained iff the FCC step will run, or the BCC list
    includes the sender's own address. A BCC addressed only to third
    parties (e.g. an auditor) does not count as a self-copy: the user
    still has no record. Pass ``--allow-no-copy`` to override (e.g.
    throwaway sends through a relay that archives independently).
    """
    if will_fcc or allow_no_copy:
        return
    if bcc:
        from courier.models import EmailAddress

        sender_lower = sender_addr.lower()
        for entry in bcc:
            try:
                if EmailAddress.parse(entry).address.lower() == sender_lower:
                    return
            except Exception:
                continue
    typer.echo(
        "Error: refusing to send: no FCC, and BCC does not include the "
        "sender's own address, so no copy of this message will be "
        "retained. Pass --allow-no-copy to override.",
        err=True,
    )
    raise typer.Exit(1)


def _perform_send(
    client: Optional[ImapClient],
    smtp: SmtpConfig,
    mime_message: Any,
    identity: Any,
    sent_folder_override: Optional[str],
) -> Dict[str, Any]:
    """Verify FCC folder, transmit via SMTP, and APPEND. Used by send paths.

    Whether FCC runs is decided by the caller, which signals the choice
    via ``client``: a non-None ``client`` means FCC is required and must
    be verified before SMTP opens; ``None`` skips FCC entirely. The same
    connection used for verification is reused for the post-SMTP APPEND,
    closing the small race that an earlier two-connection design left
    open.

    Args:
        client: Connected ImapClient used for verification + APPEND, or
            ``None`` to skip FCC.
        smtp: Pre-resolved SmtpConfig (see ``_resolve_smtp_or_exit``).
        mime_message: Built MIME message ready to serialise.
        identity: Resolved ``Identity`` carrying address (for the result
            envelope) and ``fcc`` (for FCC folder resolution; only the
            folder-name string form selects a folder here).
        sent_folder_override: When set, overrides the identity's ``fcc``
            folder for the FCC step. ``None`` means use the identity's
            value or auto-discover.

    Returns:
        The standard send-result JSON shape (status, identity, message_ids,
        smtp_response, accepted_recipients, fcc_folder, fcc_uid).
    """
    from courier.imap_client import SENT_FOLDER_CANDIDATES
    from courier.smtp_transport import send as smtp_send

    fcc_target: Optional[str] = None
    if client is not None:
        identity_fcc_folder = identity.fcc if isinstance(identity.fcc, str) else None
        configured = sent_folder_override or identity_fcc_folder
        fcc_target = client.resolve_sent_folder(configured=configured)
        if fcc_target is None:
            if configured is not None:
                typer.echo(
                    f"Error: configured sent folder '{configured}' does not "
                    f"exist on the IMAP server. Set it to a folder that "
                    f"exists, drop the override to auto-discover, or pass "
                    f"--no-save-sent.",
                    err=True,
                )
            else:
                typer.echo(
                    "Error: no Sent folder found on the IMAP server. Tried, "
                    "in order: " + ", ".join(SENT_FOLDER_CANDIDATES) + ". "
                    "Configure [identity.NAME].fcc, pass "
                    "--sent-folder, or pass --no-save-sent.",
                    err=True,
                )
            raise typer.Exit(1)

    try:
        fcc_bytes, send_result = smtp_send(mime_message, smtp)
    except Exception as exc:
        typer.echo(f"Error: SMTP send failed: {exc}", err=True)
        raise typer.Exit(1)

    fcc_uid: Optional[int] = None
    if client is not None and fcc_target is not None:
        try:
            fcc_uid = client.append_raw(fcc_target, fcc_bytes, flags=(r"\Seen",)).uid
        except Exception as exc:
            typer.echo(f"warning: FCC to {fcc_target} failed: {exc}", err=True)

    return {
        "status": "success",
        "identity": identity.address,
        **send_result,
        "fcc_folder": fcc_target,
        "fcc_uid": fcc_uid,
    }


def _resolve_send_route(
    cfg: CourierConfig,
    identity_name: Optional[str],
    smtp_name: Optional[str],
    from_email: Optional[str],
    display_name: Optional[str],
    fcc: Optional[str],
) -> Optional[Tuple[Any, Optional[SmtpConfig], Optional[str]]]:
    """Resolve a ``--send`` route from the two-mode flag set.

    Mode A (``--identity NAME``) returns the configured Identity. Mode B
    (``--smtp NAME --from EMAIL [--name N] [--fcc IMAP:FOLDER]``) returns
    a synthetic Identity plus the explicit SMTP block; no
    ``[identity.NAME]`` is consulted.

    Returns:
        ``None`` when neither flag is present (caller decides what to do
        with it). Otherwise a 3-tuple ``(identity, smtp_override, fcc_imap)``:
        - identity: Identity to send as. ``identity.fcc`` is the chosen
          FCC folder (mode A: from the block; mode B: the user's
          ``--fcc`` folder or None).
        - smtp_override: Set in mode B; ``None`` in mode A so
          ``_perform_send`` runs the identity-driven resolution chain.
        - fcc_imap: Name of the ``[imap.NAME]`` block to FCC into, or
          ``None`` to skip FCC. Mode A uses ``identity.imap``; mode B
          uses the imap part of ``--fcc IMAP:FOLDER`` when given.

    Exits via ``typer.Exit(1)`` on a validation failure with a message
    naming the offending input and how to correct it.
    """
    from courier.config import Identity as _Identity
    from courier.config import smtp_has_own_creds, validate_display_name

    if identity_name and smtp_name:
        typer.echo(
            "Error: --identity and --smtp are mutually exclusive. "
            "--identity NAME selects a configured [identity.NAME]; "
            "--smtp NAME --from EMAIL sends through a named SMTP block "
            "without a declared identity.",
            err=True,
        )
        raise typer.Exit(1)

    if identity_name:
        if identity_name not in cfg.identities:
            typer.echo(
                f"Error: --identity '{identity_name}' does not name any "
                f"[identity.NAME] block. Configured identities: "
                f"{sorted(cfg.identities)}",
                err=True,
            )
            raise typer.Exit(1)
        identity = cfg.identities[identity_name]
        if from_email and from_email.strip().lower() != identity.address.lower():
            typer.echo(
                f"Error: --identity '{identity_name}' has address "
                f"'{identity.address}', but --from is '{from_email}'. "
                f"Drop --from, or pass the matching address.",
                err=True,
            )
            raise typer.Exit(1)
        if display_name is not None:
            typer.echo(
                "Error: --name is only valid with --smtp; --identity uses "
                "the [identity.NAME] block's configured name.",
                err=True,
            )
            raise typer.Exit(1)
        if fcc is not None:
            typer.echo(
                "Error: --fcc is only valid with --smtp; --identity uses "
                "the [identity.NAME] block's fcc folder.",
                err=True,
            )
            raise typer.Exit(1)
        return identity, None, identity.imap

    if smtp_name:
        if not from_email:
            typer.echo(
                f"Error: --smtp '{smtp_name}' requires --from EMAIL "
                f"(no [identity.NAME] is in scope to supply the address).",
                err=True,
            )
            raise typer.Exit(1)
        if smtp_name not in cfg.smtp_blocks:
            typer.echo(
                f"Error: --smtp '{smtp_name}' does not name any [smtp.NAME] "
                f"block. Configured SMTP blocks: {sorted(cfg.smtp_blocks)}",
                err=True,
            )
            raise typer.Exit(1)
        smtp = cfg.smtp_blocks[smtp_name]
        if not smtp_has_own_creds(smtp):
            typer.echo(
                f"Error: [smtp.{smtp_name}] has no username/password; "
                f"--smtp mode has no [imap.NAME] block in scope to inherit "
                f"credentials from. Add username/password to the SMTP block.",
                err=True,
            )
            raise typer.Exit(1)
        if display_name:
            try:
                validate_display_name(display_name, "--name")
            except ValueError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1)
        fcc_imap: Optional[str] = None
        fcc_folder: Optional[str] = None
        if fcc:
            if ":" not in fcc:
                typer.echo(
                    "Error: --fcc must be IMAP_NAME:FOLDER (e.g. " "--fcc work:Sent).",
                    err=True,
                )
                raise typer.Exit(1)
            fcc_imap, fcc_folder = fcc.split(":", 1)
            if fcc_imap not in cfg.imap_blocks:
                typer.echo(
                    f"Error: --fcc references unknown [imap.{fcc_imap}]. "
                    f"Available: {sorted(cfg.imap_blocks)}",
                    err=True,
                )
                raise typer.Exit(1)
            if not fcc_folder:
                typer.echo(
                    "Error: --fcc IMAP:FOLDER requires a folder after the "
                    "colon (e.g. work:Sent).",
                    err=True,
                )
                raise typer.Exit(1)
        synth = _Identity(
            imap=fcc_imap or "",
            address=from_email,
            name=display_name or "",
            smtp=smtp_name,
            fcc=fcc_folder,
        )
        return synth, smtp, fcc_imap

    return None


def _no_route_error(cfg: CourierConfig) -> None:
    """Emit the canonical 'no --identity / --smtp on --send' error and exit.

    Centralised so compose, reply, and send-draft show the same wording.
    """
    typer.echo(
        "Error: --send requires either --identity NAME, or "
        "--smtp NAME --from EMAIL. Configured identities: "
        f"{sorted(cfg.identities)}; configured SMTP blocks: "
        f"{sorted(cfg.smtp_blocks)}.",
        err=True,
    )
    raise typer.Exit(1)


def _print_eager_warnings_if_relevant() -> None:
    """Print config warnings to stderr when user is 'checking in' on courier.

    Detects no-args, ``--help``, or ``-h`` in argv (after stripping known
    global options like ``--config``/``--imap``). Surfaces warnings before
    typer takes over so the user sees them above the help text.

    Safe-fails: any error in load (missing config, bad TOML) is swallowed
    here; the user will see the actual error from typer's normal flow if
    they then run a real command.
    """
    args = sys.argv[1:]
    globals_with_value = {"--config", "-c", "--imap"}

    config_path: Optional[str] = None
    first_real: Optional[str] = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break
        if tok in globals_with_value and i + 1 < len(args):
            if tok in ("--config", "-c"):
                config_path = args[i + 1]
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in globals_with_value:
            if tok.startswith("--config="):
                config_path = tok.split("=", 1)[1]
            i += 1
            continue
        first_real = tok
        break

    is_help_or_noargs = first_real is None or first_real in ("--help", "-h")
    if not is_help_or_noargs:
        return

    try:
        _, warnings = load_config_with_warnings(config_path)
    except Exception:
        return
    for w in warnings:
        print(f"warn: {w}", file=sys.stderr)


def _empty_search_result() -> Dict[str, Any]:
    """Return the wrapped result shape for a block that returned nothing.

    Used when client construction or connection fails, so the per-block
    output stays uniform with successful calls.
    """
    return {
        "results": [],
        "provenance": {
            "source": "remote",
            "indexed_at": None,
            "fell_back_reason": None,
        },
    }


def _format_provenance_line(provenance: Dict[str, Any]) -> str:
    """Format a one-line provenance summary for the text output mode."""
    source = provenance.get("source", "remote")
    indexed_at = provenance.get("indexed_at") or "-"
    reason = provenance.get("fell_back_reason")
    parts = [f"source={source}", f"indexed_at={indexed_at}"]
    if reason:
        parts.append(f"fell_back={reason}")
    return "# " + " ".join(parts)


def _format_chain_text(result: Dict[str, Dict[str, Any]]) -> str:
    """Render a chain result as multi-line, prompt-friendly text."""
    sections: List[str] = []
    for op_key, blocks in result.items():
        lines: List[str] = [f"=== {op_key} ==="]
        for imap_name, value in blocks.items():
            lines.append(f"== {imap_name} ==")
            if "results" in value:
                hits: List[Dict[str, Any]] = value.get("results") or []
                lines.append(_format_provenance_line(value.get("provenance") or {}))
            elif "error" in value:
                lines.append(f"(error: {value['error']})")
                continue
            else:
                # read: the per-block value is a single message object, not a
                # {results, provenance} wrapper, so render it as one record.
                hits = [value]
            if not hits:
                lines.append("(no results)")
            else:
                for r in hits:
                    date = str(r.get("date", ""))[:10]
                    subject = r.get("subject", "")
                    from_ = r.get("from", "")
                    to_list = r.get("to") or [""]
                    to = to_list[0]
                    folder = r.get("folder", "")
                    message_id = r.get("message_id", "")
                    lines.append(f"{date}  {subject}")
                    lines.append(f"            from: {from_}")
                    lines.append(f"            to:   {to}")
                    lines.append(f"            folder: {folder}")
                    if message_id:
                        lines.append(f"            id:     {message_id}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _format_chain_oneline(result: Dict[str, Dict[str, Any]]) -> str:
    """Render a chain result as one tab-separated line per result.

    Columns: op_key, imap_name, date, subject, from -> to, message_id.
    """
    lines: List[str] = []
    for op_key, blocks in result.items():
        for imap_name, value in blocks.items():
            if "results" in value:
                hits: List[Dict[str, Any]] = value.get("results") or []
            elif "error" in value:
                lines.append(f"{op_key}\t{imap_name}\t(error: {value['error']})")
                continue
            else:
                # read: the per-block value is the message object itself.
                hits = [value]
            if not hits:
                lines.append(f"{op_key}\t{imap_name}\t(no results)")
                continue
            for r in hits:
                date = str(r.get("date", ""))[:10]
                subject = r.get("subject", "")
                from_addr = _email_only(r.get("from", ""))
                to_list = r.get("to") or [""]
                to_addr = _email_only(to_list[0]) if to_list[0] else ""
                message_id = r.get("message_id", "")
                lines.append(
                    f"{op_key}\t{imap_name}\t{date}\t{subject}"
                    f"\t{from_addr} -> {to_addr}\t{message_id}"
                )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# config-check, list, status
# ---------------------------------------------------------------------------


@app.command("config-check")
def config_check() -> None:
    """Validate the configuration file without invoking IMAP or SMTP.

    Reports hard errors on invalid TOML or bad cross-references (typo'd
    default_smtp, identity smtp referencing an undefined block, duplicate
    addresses within one [imap.NAME] block, etc.) and lists non-fatal
    warnings (send-disabled blocks, shared credential-less SMTP on
    non-Gmail hosts, no smtp blocks).

    Exit codes:
        0  config is valid (warnings may still be present on stderr)
        1  config is invalid (errors on stderr)
    """
    try:
        _, warnings = load_config_with_warnings(_config_path)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    for w in warnings:
        print(f"warn: {w}", file=sys.stderr)
    path = _config_path or "~/.config/courier/config.toml"
    print(f"config-check: OK ({path})")
    if warnings:
        print(f"  ({len(warnings)} warning(s) above)")


def _build_inventory(cfg: CourierConfig) -> Dict[str, Any]:
    """Build the unified JSON inventory of [imap.*]/[smtp.*]/[identity.*]."""
    by_imap: Dict[str, List[str]] = {}
    for ident_name, ident in cfg.identities.items():
        by_imap.setdefault(ident.imap, []).append(ident_name)
    imap_out: Dict[str, Any] = {}
    for name, block in cfg.imap_blocks.items():
        imap_out[name] = {
            "host": block.host,
            "port": block.port,
            "username": block.username,
            "ssl": block.use_ssl,
            "default_smtp": block.default_smtp,
            "maildir": block.maildir,
            "allowed_folders": (
                list(block.allowed_folders) if block.allowed_folders else None
            ),
            "identities": sorted(by_imap.get(name, [])),
        }
    smtp_out: Dict[str, Any] = {}
    for name, smtp in cfg.smtp_blocks.items():
        smtp_out[name] = {
            "host": smtp.host,
            "port": smtp.port,
            "has_creds": bool(smtp.username and smtp.password),
            "save_sent": smtp.save_sent,
            "rewrite_msgid_from_response": smtp.rewrite_msgid_from_response,
        }
    identity_out: Dict[str, Any] = {}
    for name, ident in cfg.identities.items():
        identity_out[name] = {
            "imap": ident.imap,
            "address": ident.address,
            "name": ident.name or None,
            "smtp": ident.smtp,
            "fcc": ident.fcc,
            "bcc": ident.bcc,
        }
    return {
        "default_imap": cfg.default_imap,
        "imap": imap_out,
        "smtp": smtp_out,
        "identity": identity_out,
    }


@app.command("list")
def list_cmd() -> None:
    """List configured [imap.*], [smtp.*], and [identity.*] blocks as JSON."""
    cfg = _load_cfg_or_exit()
    _out(_build_inventory(cfg))
    for w in cfg.warnings:
        print(f"warn: {w}", file=sys.stderr)


def _installed_command_version() -> Optional[str]:
    """Return the version stamped in ~/.claude/commands/courier.md, or None.

    Reads the ``version:`` frontmatter field written by ``install-claude-command``.
    Returns None when the file is absent or carries no version field (pre-versioning
    installs).
    """
    command_file = Path.home() / ".claude" / "commands" / "courier.md"
    if not command_file.exists():
        return None
    for line in command_file.read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _claude_registration_status() -> Optional[str]:
    """Return a nudge string if ~/.claude exists but courier is not registered or is stale.

    Returns None when ~/.claude is absent (user has no Claude Code install)
    or when courier is already registered at the current version.
    """
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return None
    command_file = claude_dir / "commands" / "courier.md"
    skill_dir = claude_dir / "skills" / "courier"
    if skill_dir.exists():
        return None
    if not command_file.exists():
        return (
            "courier command not installed in Claude Code. "
            "Run `courier install-claude-command` to install it, "
            "then continue with the user's request."
        )
    installed = _installed_command_version()
    if installed != __version__:
        installed_label = installed if installed else "unknown"
        return (
            f"courier command file is at version {installed_label}, "
            f"package is {__version__}. "
            "Run `courier install-claude-command --yes` to update it, "
            "then continue with the user's request."
        )
    return None


@app.command("status")
def status() -> None:
    """Show one row per configured server with the result of a connection probe.

    Each [imap.NAME] block gets a full IMAP login attempt; each
    [smtp.NAME] block gets an EHLO + STARTTLS handshake plus an
    authenticated login when the block carries credentials. Probes
    run sequentially in config order: a self-hosted server behind a
    fail2ban-style lockout would see N near-simultaneous auth events
    from one IP under parallel probing, which can trip per-IP rate
    limits and lock the operator out of the very server they are
    diagnosing. The wall-time cost is real (sum of per-server timeouts
    rather than the slowest), but a status check is interactive and
    paid once.

    Each [imap.NAME] block opted into the local cache (a [local_cache]
    table plus a per-block ``maildir``) also gets a CACHE cell reporting
    index freshness and whether it has fallen past the staleness budget,
    so a silently stale index is visible here rather than only as a
    fallback reason in search provenance.

    Use ``list`` for the JSON inventory; ``status`` is a short
    operational view answering "which servers are reachable right
    now".
    """
    cfg = _load_cfg_or_exit()
    rows = _probe_all(cfg)
    _print_status_table(rows)
    for w in cfg.warnings:
        print(f"warn: {w}", file=sys.stderr)


@app.command("install-claude-command")
def install_claude_command(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Replace an existing older version without prompting "
        "(for non-interactive callers such as Claude Code sessions).",
    ),
) -> None:
    """Copy the Claude Code command file into ~/.claude/commands/courier.md.

    After running this command, Claude Code will recognise the ``courier``
    skill and route email-related requests through the courier CLI.
    If a previous version is already installed, you will be asked to confirm
    before it is replaced, unless ``--yes`` is given.
    """
    from courier._claude_command import render

    dest_dir = Path.home() / ".claude" / "commands"
    dest = dest_dir / "courier.md"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        installed = _installed_command_version()
        installed_label = installed if installed else "unknown version"
        if installed == __version__:
            print(f"Already at {__version__}: {dest}")
            return
        if not yes:
            confirmed = typer.confirm(
                f"courier command already installed ({installed_label}). "
                f"Replace with {__version__}?"
            )
            if not confirmed:
                print("Aborted.")
                raise typer.Exit(1)

    dest.write_text(render(__version__))
    print(f"Installed {__version__}: {dest}")


@app.command("config-sample")
def config_sample() -> None:
    """Print a sample configuration file to stdout.

    The template documents the ``[imap.NAME]``, ``[smtp.NAME]``, and
    ``[identity.NAME]`` tables with worked examples. It writes nothing to
    disk and needs no existing config, so pip, pipx, and Homebrew users
    (who have no repository checkout) can bootstrap one by redirecting it::

        courier config-sample > ~/.config/courier/config.toml

    Returns:
        None. The bundled sample is written verbatim to stdout.
    """
    from importlib.resources import files

    content = (
        files("courier").joinpath("config.sample.toml").read_text(encoding="utf-8")
    )
    sys.stdout.write(content)


def _probe_all(cfg: CourierConfig) -> List[Tuple[str, str, str, str, str]]:
    """Probe every configured IMAP and SMTP block sequentially.

    Returns rows as (name, kind, endpoint, status, cache). Order: IMAP
    blocks in config order, then SMTP blocks in config order. Connection
    failures surface in the status column; local-cache health surfaces in
    the cache column (``"-"`` for SMTP blocks and for IMAP blocks not
    opted into the cache); the function never raises. Probes are serial
    so a self-hosted server behind a per-IP rate-limit (fail2ban etc.)
    does not see a burst of concurrent auth attempts from this command.
    """
    rows: List[Tuple[str, str, str, str, str]] = []
    for name, block in cfg.imap_blocks.items():
        rows.append(
            (
                name,
                "imap",
                f"{block.host}:{block.port}",
                _probe_imap(block),
                _probe_cache(cfg, block),
            )
        )
    for name, smtp in cfg.smtp_blocks.items():
        rows.append((name, "smtp", f"{smtp.host}:{smtp.port}", _probe_smtp(smtp), "-"))
    return rows


def _probe_imap(block: ImapBlock) -> str:
    """Connect + login to an IMAP block; return ``ok`` or ``FAIL: <reason>``."""
    client = ImapClient(block)
    try:
        client.connect()
    except Exception as exc:
        return f"FAIL: {exc}"
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    return "ok"


def _probe_smtp(smtp: SmtpConfig) -> str:
    """EHLO + STARTTLS + optional login on an SMTP block.

    A template SMTP block (no own creds) is probed only as far as
    EHLO+STARTTLS, since authentication only happens at send time
    against an inheriting [imap.NAME] block. The status row notes
    that case as ``ok (template, no auth)`` so the operator knows
    auth was not exercised.
    """
    import smtplib
    import socket

    factory = smtplib.SMTP_SSL if smtp.port == 465 else smtplib.SMTP
    try:
        conn = factory(smtp.host, smtp.port, timeout=10)
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        return f"FAIL: {exc}"
    try:
        conn.ehlo()
        if smtp.port in (587, 2587):
            conn.starttls()
            conn.ehlo()
        if smtp.username and smtp.password:
            conn.login(smtp.username, smtp.password)
            return "ok"
        return "ok (template, no auth)"
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        return f"FAIL: {exc}"
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def _probe_cache(cfg: CourierConfig, block: ImapBlock) -> str:
    """Report local-cache health for one [imap.NAME] block.

    Produces the status table's CACHE cell. A block is "opted in" when
    the global [local_cache] table is configured and the block carries a
    ``maildir``; otherwise the cell is ``"-"``. For an opted-in block the
    cell reflects the mu index: ``"ok (<age> old)"`` when fresh,
    ``"STALE (<age> old, max <budget>)"`` when past the staleness budget,
    or ``"mu not found"`` / ``"no index"`` when the backend cannot run.

    Args:
        cfg: The loaded courier configuration.
        block: The [imap.NAME] block to report on.

    Returns:
        A short cell string for the CACHE column.
    """
    if cfg.local_cache is None or not block.maildir:
        return "-"
    backend = _get_mu_backend(cfg)
    if backend is None:
        return "-"
    eligibility = backend.is_eligible(block)
    if eligibility.reason == "mu_missing":
        return "mu not found"
    if eligibility.reason == "db_missing":
        return "no index"
    mtime_iso = backend.index_mtime_iso()
    age = "?"
    if mtime_iso is not None:
        from datetime import datetime, timezone

        delta = datetime.now(timezone.utc) - datetime.fromisoformat(mtime_iso)
        age = _format_age(delta.total_seconds())
    if eligibility.reason == "stale":
        budget = _format_age(cfg.local_cache.max_staleness_seconds)
        return f"STALE ({age} old, max {budget})"
    if eligibility.eligible:
        return f"ok ({age} old)"
    return eligibility.reason or "unavailable"


def _format_age(seconds: float) -> str:
    """Render an elapsed time in seconds as a compact s/m/h/d cell.

    Args:
        seconds: Elapsed seconds; negative inputs clamp to zero.

    Returns:
        The largest whole unit that fits, e.g. ``"45s"``, ``"12m"``,
        ``"3h"``, ``"2d"``.
    """
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _print_status_table(rows: List[Tuple[str, str, str, str, str]]) -> None:
    """Print the status rows as an aligned plain-text table to stdout."""
    print(f"courier {__version__}")
    if not rows:
        print("(no [imap.*] or [smtp.*] blocks configured)")
        return
    headers = ("NAME", "KIND", "HOST:PORT", "STATUS", "CACHE")
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows)) for i in range(5)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


@app.command("folders")
def folders() -> None:
    """List available email folders."""
    client = _make_client()
    try:
        folder_list = client.list_folders()
        _out(folder_list)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@app.command("search")
def search(
    query: str = typer.Argument(
        "",
        help=(
            "Gmail-style search query. Examples: "
            "'from:alice subject:invoice', 'is:unread after:2025-03-01', "
            "'meeting notes' (bare words search text), "
            "'imap:OR TEXT foo SUBJECT bar' (raw IMAP)."
        ),
    ),
    query_opt: Optional[str] = typer.Option(
        None, "--query", "-q", help="Alias for the positional query (overrides if set)."
    ),
    folder: Optional[str] = typer.Option(
        None, "--folder", "-f", help="Folder to search (default: all)."
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum number of results."),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local cache and query live IMAP.",
    ),
    output_format: str = typer.Option(
        "json",
        "--format",
        "-F",
        help="Output format: json (default), text, or oneline.",
    ),
) -> None:
    """Search for emails.

    Repeat the verb to look up several keywords in one invocation:
    ``courier search foo search bar``.

    Output is a JSON object keyed first by operation string, then by
    [imap.NAME] block. Each per-block value is ``{"results": [...],
    "provenance": {...}}`` where ``provenance`` reports whether the
    result came from IMAP (``source: "remote"``) or from a local mu
    cache (``source: "local"``). ``--limit`` is applied per block.

    ``--format text`` renders multi-line, prompt-friendly output grouped
    by operation and block; ``--format oneline`` renders one
    tab-separated line per result (op_key, imap_name, date, subject,
    from -> to, message_id).

    Exit code: 0 on hits, 1 when every block returned zero results, so
    shell fallback chains work: ``courier search 'from:x' || courier
    search 'x'``.
    """
    effective = query_opt if query_opt is not None else query
    op_key = _build_op_key("search", query=effective, folder=folder, limit=limit)
    names = _resolve_imap_names()
    try:
        result = _execute_chain(
            [
                (
                    op_key,
                    "search",
                    {
                        "query": effective,
                        "folder": folder,
                        "limit": limit,
                        "no_cache": no_cache,
                    },
                )
            ],
            names,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    if output_format == "json":
        _out(result)
    elif output_format == "text":
        typer.echo(_format_chain_text(result))
    elif output_format == "oneline":
        typer.echo(_format_chain_oneline(result))
    else:
        typer.echo(
            f"Error: unknown --format '{output_format}'. Use json, text, or oneline.",
            err=True,
        )
        raise typer.Exit(2)
    has_results = any(
        v.get("results")
        for per_block in result.values()
        for v in per_block.values()
        if isinstance(v, dict)
    )
    if not has_results:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@app.command("read")
def read(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local cache and read from live IMAP.",
    ),
) -> None:
    """Read an email's content.

    Output is a JSON object keyed by operation string, then by [imap.NAME]
    block name.
    """
    name = _resolve_single_imap_name()
    client = _make_client()
    try:
        result = _fetch_email_result(client, folder, uid, no_cache=no_cache)
        if "error" in result:
            typer.echo(result["error"], err=True)
            raise typer.Exit(1)
        op_key = _build_op_key("read", folder=folder, uid=uid)
        _out({op_key: {name: result}})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


@app.command("move")
def move(
    folder: str = typer.Option(..., "--folder", "-f", help="Source folder."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    target: str = typer.Option(..., "--target", "-t", help="Destination folder."),
) -> None:
    """Move an email to another folder."""
    client = _make_client()
    try:
        # ponytail: CourierError shim keeps today's success=false JSON and
        # exit 0; stage C2 replaces it with typed exit codes.
        try:
            client.move_email(uid, folder, target)
        except CourierError:
            _out({"success": False, "message": "Failed to move email"})
        else:
            _out({"success": True, "message": f"Moved from {folder} to {target}"})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


@app.command("copy")
def copy_cmd(
    from_imap: str = typer.Option(..., "--from-imap", help="Source [imap.NAME] block."),
    from_folder: str = typer.Option(..., "--from-folder", help="Source folder."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID in the source folder."),
    to_folder: str = typer.Option(
        "INBOX", "--to-folder", "-t", help="Destination folder."
    ),
    move_flag: bool = typer.Option(
        False, "--move", help="Delete from source after copy."
    ),
    preserve_flags: bool = typer.Option(
        False, "--preserve-flags", help="Copy original flags to destination."
    ),
) -> None:
    """Copy an email from one [imap.NAME] block into another.

    The global --imap selects the destination block.
    Fetches the raw RFC 822 message from the source and APPENDs it to the
    destination, preserving the message byte-for-byte and its original date.
    """
    from courier.imap_client import copy_email_between_imap_blocks

    source = _make_client(imap_override=from_imap)
    dest = _make_client()
    try:
        result = copy_email_between_imap_blocks(
            source,
            dest,
            uid,
            from_folder,
            to_folder=to_folder,
            move=move_flag,
            preserve_flags=preserve_flags,
        )
        if not result["success"]:
            typer.echo(f"Error: {result['error']}", err=True)
            raise typer.Exit(1)
        _out(
            {
                "success": True,
                "subject": result["subject"],
                "source": f"{from_imap}/{from_folder}/{uid}",
                "destination": to_folder,
                "new_uid": result["new_uid"],
                "moved": result["moved"],
            }
        )
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        source.disconnect()
        dest.disconnect()


# ---------------------------------------------------------------------------
# mark-read / mark-unread
# ---------------------------------------------------------------------------


@app.command("mark-read")
def mark_read(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
) -> None:
    """Mark an email as read."""
    client = _make_client()
    try:
        try:  # ponytail: CourierError shim; C2 replaces with typed exit codes
            client.mark_email(uid, folder, r"\Seen", True)
            _out({"success": True})
        except CourierError:
            _out({"success": False})
    finally:
        client.disconnect()


@app.command("mark-unread")
def mark_unread(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
) -> None:
    """Mark an email as unread."""
    client = _make_client()
    try:
        try:  # ponytail: CourierError shim; C2 replaces with typed exit codes
            client.mark_email(uid, folder, r"\Seen", False)
            _out({"success": True})
        except CourierError:
            _out({"success": False})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# flag
# ---------------------------------------------------------------------------


@app.command("flag")
def flag(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    unflag: bool = typer.Option(
        False, "--unflag", help="Remove the flag instead of setting it."
    ),
) -> None:
    """Flag (star) an email, or unflag it with --unflag."""
    client = _make_client()
    try:
        try:  # ponytail: CourierError shim; C2 replaces with typed exit codes
            client.mark_email(uid, folder, r"\Flagged", not unflag)
            _out({"success": True, "flagged": not unflag})
        except CourierError:
            _out({"success": False, "flagged": not unflag})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@app.command("trash")
def trash(
    folder: str = typer.Option(
        ..., "--folder", "-f", help="Folder containing the email."
    ),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
) -> None:
    """Move an email to the server's Trash/Bin (recoverable removal).

    The normal way to remove a message. Resolves the Trash folder via the
    \\Trash SPECIAL-USE role, falling back to [Gmail]/Bin, [Gmail]/Trash, or
    Trash. On Gmail this is what actually removes a message; ``delete`` only
    un-labels it.
    """
    client = _make_client()
    try:
        try:  # ponytail: CourierError shim; C2 replaces with typed exit codes
            client.trash_email(uid, folder)
            _out({"success": True})
        except FolderNotFound as exc:
            # No resolvable Trash/Bin: same message + exit 1 as the old
            # ValueError path.
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        except CourierError:
            _out({"success": False})
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        client.disconnect()


@app.command("delete")
def delete(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
) -> None:
    """Expunge an email in place, irrecoverable. (Normally use trash.)

    Sets \\Deleted and EXPUNGEs in the given folder. On standard IMAP this is
    a permanent removal that bypasses the Trash; on Gmail it only removes this
    folder's label, leaving the message in All Mail. For normal removal use
    ``trash``, which moves the message to the Bin.
    """
    client = _make_client()
    try:
        try:  # ponytail: CourierError shim; C2 replaces with typed exit codes
            client.delete_email(uid, folder)
            _out({"success": True})
        except CourierError:
            _out({"success": False})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------


@app.command("triage")
def triage(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    action: str = typer.Argument(
        ..., help="Action: move, read, unread, flag, unflag, trash, delete."
    ),
    target_folder: Optional[str] = typer.Option(
        None, "--target-folder", "-t", help="Target folder (for move)."
    ),
    notes: Optional[str] = typer.Option(None, "--notes", help="Optional notes."),
) -> None:
    """Triage an email with a given action."""
    client = _make_client()
    try:
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            typer.echo(f"Email UID {uid} not found in {folder}", err=True)
            raise typer.Exit(1)
        try:
            message = client.process_email_action(
                uid, folder, action, target_folder=target_folder
            )
        except (ValueError, CourierError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        _out({"message": message})
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


@app.command("attachments")
def attachments(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
) -> None:
    """List attachments for an email.

    Exit code: 0 if at least one attachment is found, 1 if the email has
    none. Shell idiom: ``courier attachments -f INBOX -u 1 || echo none``.
    """
    client = _make_client()
    empty = False
    try:
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            _out({"error": f"Email UID {uid} not found in {folder}"})
            raise typer.Exit(1)
        result = []
        for i, att in enumerate(email_obj.attachments):
            entry = {
                "index": i,
                "filename": att.filename,
                "size": att.size,
                "content_type": att.content_type,
            }
            if att.content_id:
                entry["content_id"] = att.content_id
            result.append(entry)
        _out(result)
        empty = not result
    finally:
        client.disconnect()
    if empty:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


@app.command("save")
def save(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    attachment: str = typer.Option(
        ..., "--attachment", help="Attachment filename or numeric index."
    ),
    save_path: str = typer.Option(
        ..., "--save-path", "-o", help="Path to save the attachment."
    ),
) -> None:
    """Download an attachment from an email."""
    client = _make_client()
    try:
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            typer.echo(f"Email UID {uid} not found in {folder}", err=True)
            raise typer.Exit(1)
        result = email_obj.save_attachment(attachment, save_path)
        _out(result)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _export_raw(client: ImapClient, folder: str, uid: int, save_path: str) -> None:
    """Export raw RFC 822 bytes to *save_path* (or stdout if ``-``)."""
    fetched = client.fetch_raw(uid, folder)
    if not fetched:
        typer.echo(f"Email UID {uid} not found in {folder}", err=True)
        raise typer.Exit(1)

    raw_bytes = fetched["raw"]
    if save_path == "-":
        sys.stdout.buffer.write(raw_bytes)
        return

    dir_part = os.path.dirname(save_path)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)
    with open(save_path, "wb") as fh:
        fh.write(raw_bytes)
    _out(
        {
            "success": True,
            "save_path": save_path,
            "size": len(raw_bytes),
            "subject": fetched.get("subject"),
        }
    )


def _export_html(client: ImapClient, folder: str, uid: int, save_path: str) -> None:
    """Export HTML with embedded images to *save_path*."""
    email_obj = client.fetch_email(uid, folder)
    if not email_obj:
        typer.echo(f"Email UID {uid} not found in {folder}", err=True)
        raise typer.Exit(1)
    _out(email_obj.export_html_to_file(save_path))


@app.command("export")
def export(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    save_path: str = typer.Option(
        ...,
        "--save-path",
        "-o",
        help="Path to save to. Use '-' with --raw to stream raw RFC 822 to stdout.",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Export the raw RFC 822 message bytes instead of HTML.",
    ),
) -> None:
    """Export email content to a standalone file.

    Default: HTML with embedded images.
    With --raw: the raw RFC 822 message as stored on the IMAP server.
    """
    client = _make_client()
    try:
        if raw:
            _export_raw(client, folder, uid, save_path)
        else:
            _export_html(client, folder, uid, save_path)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


@app.command("links")
def links(
    folder: str = typer.Option(..., "--folder", "-f", help="Folder name."),
    uids: List[int] = typer.Option(..., "--uid", "-u", help="One or more email UIDs."),
) -> None:
    """Extract all links from email HTML content."""
    client = _make_client()
    try:
        results = extract_links_batch(client.fetch_email, folder, uids)
        _out(results)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def _write_raw_output(mime_message: Any, output: str) -> None:
    """Serialise *mime_message* and write to *output* path (``-`` is stdout)."""
    if hasattr(mime_message, "as_bytes"):
        raw = mime_message.as_bytes()
    else:
        raw = mime_message.as_string().encode("utf-8")
    if output == "-":
        sys.stdout.buffer.write(raw)
        return
    dir_part = os.path.dirname(output)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)
    with open(output, "wb") as fh:
        fh.write(raw)
    typer.echo(f"Wrote {len(raw)} bytes to {output}", err=True)


@app.command("compose")
def compose(
    to: Optional[List[str]] = typer.Option(
        None,
        "--to",
        help=(
            "Recipient email address. Repeatable. Optional: a message with "
            "only --bcc (and no --to/--cc) is allowed, e.g. a send to a "
            "distribution list with no visible recipient."
        ),
    ),
    body: str = typer.Option(..., "--body", "-b", help="Plain-text body."),
    subject: str = typer.Option("", "--subject", "-s", help="Subject line."),
    cc: Optional[List[str]] = typer.Option(None, "--cc", help="CC recipients."),
    bcc: Optional[List[str]] = typer.Option(
        None,
        "--bcc",
        help="BCC recipients (added to raw message; stripped by sending agents).",
    ),
    body_html: Optional[str] = typer.Option(
        None,
        "--body-html",
        help=(
            "HTML version of body. Omit (default) to auto-render an HTML "
            "alternative when --body contains a markdown table or heading. "
            "Pass an empty string to force text/plain only. Any other value "
            "is used verbatim."
        ),
    ),
    attach: Optional[List[str]] = typer.Option(
        None, "--attach", help="Path to a file to attach. Repeatable."
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output path for raw RFC 822 message. Use '-' for stdout (suitable for piping).",
    ),
    send_flag: bool = typer.Option(
        False,
        "--send",
        help="Transmit via SMTP instead of saving as a draft. Mutually exclusive with --output.",
    ),
    identity_name: Optional[str] = typer.Option(
        None,
        "--identity",
        "-i",
        help=(
            "Send as the named [identity.NAME] block (resolves From, "
            "display name, IMAP block, SMTP, fcc). Required on "
            "--send unless --smtp/--from is given. (Note: --identity "
            "selects an [identity.NAME] block; --imap selects an "
            "[imap.NAME] block, which is a different thing.)"
        ),
    ),
    smtp_name: Optional[str] = typer.Option(
        None,
        "--smtp",
        help=(
            "Send through the named [smtp.NAME] block with a free-form "
            "--from address. Requires --from. The SMTP block must carry "
            "its own username/password (no inheritance). Use this for "
            "relays like SES that are authorised to carry many addresses."
        ),
    ),
    from_email: Optional[str] = typer.Option(
        None,
        "--from",
        help=(
            "From address. With --identity, must match the identity's "
            "address. With --smtp, the address to send as (mode B). "
            "In drafting mode (no --send), selects an identity by address."
        ),
    ),
    display_name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Display name for the From header. Mode B (--smtp) only.",
    ),
    fcc: Optional[str] = typer.Option(
        None,
        "--fcc",
        help=(
            "FCC the sent message into IMAP_NAME:FOLDER. Mode B only "
            "(mode A uses the identity's fcc folder). Without --fcc "
            "in mode B, no copy is saved."
        ),
    ),
    save_sent: Optional[bool] = typer.Option(
        None,
        "--save-sent/--no-save-sent",
        help="Override the SMTP block's save_sent default for this send.",
    ),
    sent_folder: Optional[str] = typer.Option(
        None,
        "--sent-folder",
        help="Override the identity's fcc folder for the FCC step (mode A).",
    ),
    allow_no_copy: bool = typer.Option(
        False,
        "--allow-no-copy",
        help=(
            "Permit sending when neither FCC nor BCC will retain a copy. "
            "Default is to refuse, since the user would have no record."
        ),
    ),
) -> None:
    """Compose a new email.

    Default: saves to the IMAP drafts folder.
    --output: writes raw RFC 822 to a file or stdout.
    --send: transmits via SMTP. Requires --identity NAME, or
    --smtp NAME --from EMAIL (mode B).
    """
    import email.utils

    from courier.identity import (
        IdentityNotFound,
        SendDisabled,
        resolve_identity_for_send,
    )
    from courier.models import EmailAddress
    from courier.smtp_client import create_mime

    if not (to or cc or bcc):
        typer.echo(
            "Error: at least one recipient is required. Pass --to (and/or "
            "--cc); --bcc alone is allowed for a no-visible-recipient send.",
            err=True,
        )
        raise typer.Exit(2)

    if send_flag and output is not None:
        typer.echo("Error: --send and --output are mutually exclusive", err=True)
        raise typer.Exit(2)

    cfg = _load_cfg_or_exit()

    if not send_flag and (identity_name or smtp_name or display_name or fcc):
        typer.echo(
            "Error: --identity / --smtp / --name / --fcc are only valid "
            "with --send. Drafting picks the identity from --imap "
            "(--from selects by address).",
            err=True,
        )
        raise typer.Exit(1)

    if send_flag:
        route = _resolve_send_route(
            cfg, identity_name, smtp_name, from_email, display_name, fcc
        )
        if route is None:
            _no_route_error(cfg)
        identity, smtp_override, fcc_imap = route  # type: ignore[misc]
        block = cfg.imap_blocks[fcc_imap] if fcc_imap else None
        smtp_resolved = _resolve_smtp_or_exit(
            identity, block, fcc_imap or "", cfg.smtp_blocks, smtp_override
        )
        effective_bcc: List[str] = list(bcc or [])
        if getattr(identity, "bcc", None):
            effective_bcc.extend(identity.bcc)
        will_fcc = _will_fcc(
            smtp_resolved, save_sent, identity_fcc=identity.fcc, fcc_imap=fcc_imap
        )
        _refuse_if_no_copy(
            will_fcc,
            bcc=effective_bcc,
            sender_addr=identity.address,
            allow_no_copy=allow_no_copy,
        )
        client = (
            _make_client(imap_override=fcc_imap) if (will_fcc and fcc_imap) else None
        )
        try:
            from_addr = EmailAddress(name=identity.name, address=identity.address)
            to_addrs = [EmailAddress.parse(a) for a in to] if to else None
            cc_addrs = [EmailAddress.parse(a) for a in cc] if cc else None
            bcc_addrs = (
                [EmailAddress.parse(a) for a in effective_bcc]
                if effective_bcc
                else None
            )
            mime_message = create_mime(
                from_addr=from_addr,
                body=body,
                to=to_addrs,
                subject=subject,
                cc=cc_addrs,
                bcc=bcc_addrs,
                html_body=body_html,
                attachments=attach,
            )
            if not mime_message.get("Message-ID"):
                mime_message["Message-ID"] = email.utils.make_msgid()
            _out(
                _perform_send(
                    client,
                    smtp_resolved,
                    mime_message,
                    identity,
                    sent_folder,
                )
            )
        finally:
            if client is not None:
                client.disconnect()
        return

    name = _resolve_single_imap_name()
    try:
        identity = resolve_identity_for_send(cfg, name, from_addr=from_email)
    except (IdentityNotFound, SendDisabled) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    client = _make_client()
    try:
        from_addr = EmailAddress(name=identity.name, address=identity.address)
        to_addrs = [EmailAddress.parse(a) for a in to] if to else None
        cc_addrs = [EmailAddress.parse(a) for a in cc] if cc else None
        bcc_addrs = [EmailAddress.parse(a) for a in bcc] if bcc else None

        mime_message = create_mime(
            from_addr=from_addr,
            body=body,
            to=to_addrs,
            subject=subject,
            cc=cc_addrs,
            bcc=bcc_addrs,
            html_body=body_html,
            attachments=attach,
        )
        if not mime_message.get("Message-ID"):
            mime_message["Message-ID"] = email.utils.make_msgid()

        if output is not None:
            _write_raw_output(mime_message, output)
        else:
            # ponytail: CourierError shim keeps today's failure output;
            # C2 replaces it with typed exit codes.
            try:
                draft_uid = client.save_draft_mime(mime_message).uid
            except CourierError:
                draft_uid = None
            if draft_uid is None:
                typer.echo("Failed to save draft", err=True)
                raise typer.Exit(1)
            _out(
                {
                    "status": "success",
                    "message": "Draft saved",
                    "identity": identity.address,
                    "draft_uid": draft_uid,
                    "draft_folder": client.resolve_drafts_folder(),
                }
            )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------


@app.command("reply")
def reply(
    folder: str = typer.Option(
        ..., "--folder", "-f", help="Folder containing the email."
    ),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID to reply to."),
    body: str = typer.Option(..., "--body", "-b", help="Reply body text."),
    no_thread: bool = typer.Option(
        False,
        "--no-thread",
        help="Reply to sender only, without carrying original thread recipients.",
    ),
    cc: Optional[List[str]] = typer.Option(None, "--cc", help="CC recipients."),
    bcc: Optional[List[str]] = typer.Option(
        None,
        "--bcc",
        help="BCC recipients (added to raw message; stripped by sending agents).",
    ),
    body_html: Optional[str] = typer.Option(
        None,
        "--body-html",
        help=(
            "HTML version of reply body. Omit (default) to auto-render an "
            "HTML alternative when --body contains a markdown table or "
            "heading. Pass an empty string to force text/plain only. Any "
            "other value is used verbatim."
        ),
    ),
    attach: Optional[List[str]] = typer.Option(
        None,
        "--attach",
        help="Path to a file to attach. Repeatable.",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output path for raw RFC 822 message. Use '-' for stdout (suitable for piping).",
    ),
    send_flag: bool = typer.Option(
        False,
        "--send",
        help="Transmit via SMTP instead of saving as a draft. Mutually exclusive with --output.",
    ),
    identity_name: Optional[str] = typer.Option(
        None,
        "--identity",
        "-i",
        help=(
            "Send as the named [identity.NAME] block. On --send, either "
            "this or --smtp/--from is required unless the parent's "
            "recipients match a configured identity."
        ),
    ),
    smtp_name: Optional[str] = typer.Option(
        None,
        "--smtp",
        help=(
            "Send through the named [smtp.NAME] block with --from EMAIL. "
            "Mode B; see compose --help."
        ),
    ),
    from_email: Optional[str] = typer.Option(
        None,
        "--from",
        help=(
            "Reply From address. Drafting: selects identity by address "
            "(falls back to recipient match). --send: required with --smtp."
        ),
    ),
    display_name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Display name for the From header. Mode B (--smtp) only.",
    ),
    fcc: Optional[str] = typer.Option(
        None,
        "--fcc",
        help=("FCC the sent message into IMAP_NAME:FOLDER. Mode B only."),
    ),
    save_sent: Optional[bool] = typer.Option(
        None,
        "--save-sent/--no-save-sent",
        help="Override the SMTP block's save_sent default for this send.",
    ),
    sent_folder: Optional[str] = typer.Option(
        None,
        "--sent-folder",
        help="Override the identity's fcc folder for the FCC step (mode A).",
    ),
    allow_no_copy: bool = typer.Option(
        False,
        "--allow-no-copy",
        help=(
            "Permit sending when neither FCC nor BCC will retain a copy. "
            "Default is to refuse, since the user would have no record."
        ),
    ),
) -> None:
    """Draft or send a reply to an email.

    Default: saves to drafts.
    --output: writes raw RFC 822.
    --send: transmits via SMTP and FCCs to Sent. Requires --identity, or
    --smtp NAME --from EMAIL (mode B), or a parent recipient that
    matches a configured identity on the selected [imap.NAME] block.
    """
    import email.utils

    from courier.identity import (
        IdentityNotFound,
        SendDisabled,
        identities_for_imap,
        resolve_identity_for_reply,
        resolve_identity_for_send,
    )
    from courier.models import EmailAddress
    from courier.smtp_client import create_mime

    if send_flag and output is not None:
        typer.echo("Error: --send and --output are mutually exclusive", err=True)
        raise typer.Exit(2)

    cfg = _load_cfg_or_exit()

    if not send_flag and (identity_name or smtp_name or display_name or fcc):
        typer.echo(
            "Error: --identity / --smtp / --name / --fcc are only valid "
            "with --send.",
            err=True,
        )
        raise typer.Exit(1)

    name = _resolve_single_imap_name()
    block = cfg.imap_blocks[name]

    client = _make_client()
    try:
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            typer.echo(f"Email UID {uid} not found in {folder}", err=True)
            raise typer.Exit(1)

        identity: Any
        smtp_override: Optional[SmtpConfig] = None
        fcc_imap_for_send: Optional[str] = name
        send_block: Optional[ImapBlock] = block
        send_block_name: str = name
        send_client: Optional[ImapClient] = client
        owns_send_client = False

        if send_flag:
            route = _resolve_send_route(
                cfg, identity_name, smtp_name, from_email, display_name, fcc
            )
            if route is None:
                # No mode A/B given: try recipient match. The matched
                # identity is by definition on the global -i block, so
                # the fetch client doubles as the FCC client.
                idents = identities_for_imap(cfg, name)
                if not idents:
                    typer.echo(
                        f"Error: [imap.{name}] has no identities. "
                        "Specify --identity NAME, or "
                        "--smtp NAME --from EMAIL.",
                        err=True,
                    )
                    raise typer.Exit(1)
                addr_to_ident = {i.address.lower(): i for i in idents}
                matched = None
                for recipient in (email_obj.to or []) + (email_obj.cc or []):
                    addr = (getattr(recipient, "address", "") or "").lower()
                    if addr and addr in addr_to_ident:
                        matched = addr_to_ident[addr]
                        break
                if matched is None:
                    typer.echo(
                        "Error: no recipient on the parent email matches "
                        f"any identity on [imap.{name}]. Specify "
                        "--identity NAME, or --smtp NAME --from EMAIL. "
                        f"Configured identities on this block: "
                        f"{[i.address for i in idents]}",
                        err=True,
                    )
                    raise typer.Exit(1)
                identity = matched
            else:
                identity, smtp_override, fcc_imap_for_send = route
                if fcc_imap_for_send and fcc_imap_for_send != name:
                    send_block = cfg.imap_blocks[fcc_imap_for_send]
                    send_block_name = fcc_imap_for_send
                elif fcc_imap_for_send is None:
                    send_block = None
                    send_block_name = ""
            # Resolve SMTP and decide on FCC before opening any extra
            # connection: a Gmail-host SMTP, --no-save-sent, or a
            # user-supplied --bcc all skip FCC, so the second IMAP
            # connection can be skipped too.
            smtp_resolved = _resolve_smtp_or_exit(
                identity,
                send_block,
                send_block_name,
                cfg.smtp_blocks,
                smtp_override,
            )
            effective_bcc = list(bcc or [])
            if getattr(identity, "bcc", None):
                effective_bcc.extend(identity.bcc)
            will_fcc = _will_fcc(
                smtp_resolved,
                save_sent,
                identity_fcc=identity.fcc,
                fcc_imap=fcc_imap_for_send,
            )
            _refuse_if_no_copy(
                will_fcc,
                bcc=effective_bcc,
                sender_addr=identity.address,
                allow_no_copy=allow_no_copy,
            )
            if not will_fcc:
                send_client = None
            elif fcc_imap_for_send and fcc_imap_for_send != name:
                send_client = _make_client(imap_override=fcc_imap_for_send)
                owns_send_client = True
            elif fcc_imap_for_send is None:
                send_client = None
        else:
            try:
                if from_email is not None:
                    identity = resolve_identity_for_send(
                        cfg, name, from_addr=from_email
                    )
                else:
                    identity = resolve_identity_for_reply(cfg, name, email_obj)
            except (IdentityNotFound, SendDisabled) as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1)

        from_addr = EmailAddress(name=identity.name, address=identity.address)
        cc_addresses = [EmailAddress.parse(addr) for addr in cc] if cc else None
        # On --send, fold identity.bcc into the outgoing BCC. In drafting,
        # leave the draft's BCC alone (user may set it manually).
        outgoing_bcc = list(bcc or [])
        if send_flag and getattr(identity, "bcc", None):
            outgoing_bcc.extend(identity.bcc)
        bcc_addresses = (
            [EmailAddress.parse(addr) for addr in outgoing_bcc]
            if outgoing_bcc
            else None
        )

        mime_message = create_mime(
            original_email=email_obj,
            from_addr=from_addr,
            body=body,
            reply_all=not no_thread,
            cc=cc_addresses,
            bcc=bcc_addresses,
            html_body=body_html,
            attachments=attach,
        )
        if not mime_message.get("Message-ID"):
            mime_message["Message-ID"] = email.utils.make_msgid()

        try:
            if send_flag:
                _out(
                    _perform_send(
                        send_client,
                        smtp_resolved,
                        mime_message,
                        identity,
                        sent_folder,
                    )
                )
            elif output is not None:
                _write_raw_output(mime_message, output)
            else:
                # ponytail: CourierError shim keeps today's failure output;
                # C2 replaces it with typed exit codes.
                try:
                    draft_uid = client.save_draft_mime(mime_message).uid
                except CourierError:
                    draft_uid = None
                if draft_uid is None:
                    typer.echo("Failed to save reply draft", err=True)
                    raise typer.Exit(1)
                _out(
                    {
                        "status": "success",
                        "message": "Draft reply saved",
                        "identity": identity.address,
                        "draft_uid": draft_uid,
                        "draft_folder": client.resolve_drafts_folder(),
                    }
                )
        finally:
            if owns_send_client and send_client is not None:
                send_client.disconnect()
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# send-draft
# ---------------------------------------------------------------------------


@app.command("send-draft")
def send_draft(
    folder: str = typer.Option(
        ..., "--folder", "-f", help="Folder containing the draft."
    ),
    uid: int = typer.Option(..., "--uid", "-u", help="Draft UID to send."),
    keep_draft: bool = typer.Option(
        False,
        "--keep-draft",
        help="Leave the draft in place after sending. Default deletes it.",
    ),
    identity_name: Optional[str] = typer.Option(
        None,
        "--identity",
        "-i",
        help=(
            "Override the draft's From with the named [identity.NAME] "
            "block (which also picks the SMTP route)."
        ),
    ),
    smtp_name: Optional[str] = typer.Option(
        None,
        "--smtp",
        help=(
            "Send through the named [smtp.NAME] block with --from EMAIL "
            "(mode B). Overrides the draft's From and ignores any "
            "[identity.*] resolution."
        ),
    ),
    from_email: Optional[str] = typer.Option(
        None,
        "--from",
        help="Mode B From address. Required with --smtp.",
    ),
    display_name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Display name for the From header. Mode B (--smtp) only.",
    ),
    fcc: Optional[str] = typer.Option(
        None,
        "--fcc",
        help="FCC the sent message into IMAP_NAME:FOLDER. Mode B only.",
    ),
    save_sent: Optional[bool] = typer.Option(
        None,
        "--save-sent/--no-save-sent",
        help="Override the SMTP block's save_sent default for this send.",
    ),
    sent_folder: Optional[str] = typer.Option(
        None,
        "--sent-folder",
        help="Override the identity's fcc folder for the FCC step.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Connect to SMTP and authenticate, but stop before MAIL FROM. Useful for validating creds.",
    ),
    bcc: Optional[List[str]] = typer.Option(
        None,
        "--bcc",
        help="Add envelope-time BCC recipients without rewriting the draft body.",
    ),
    allow_no_copy: bool = typer.Option(
        False,
        "--allow-no-copy",
        help=(
            "Permit sending when neither FCC nor a sender-self BCC will "
            "retain a copy. Default is to refuse, since the user would "
            "have no record."
        ),
    ),
) -> None:
    """Send an existing draft as-is.

    Default route resolution: the draft's From header is parsed and
    matched against the selected [imap.NAME] block's identities (an
    unknown From is a hard error). --identity NAME overrides the draft's
    From with a configured identity; --smtp NAME --from EMAIL (mode B)
    sends through a named SMTP block without consulting any
    [identity.*].

    On success the draft is deleted from the source folder unless
    --keep-draft is set, and the message is FCC'd to Sent unless
    --no-save-sent or the SMTP block's save_sent resolution says otherwise.
    """
    from email.parser import BytesParser

    from courier.identity import (
        IdentityNotFound,
        SendDisabled,
        resolve_identity_for_send,
    )
    from courier.smtp_transport import _pick_default_transport

    cfg = _load_cfg_or_exit()
    name = _resolve_single_imap_name()
    block = cfg.imap_blocks[name]

    client = _make_client()
    owns_send_client = False
    send_client: Optional[ImapClient] = client
    send_block: Optional[ImapBlock] = block
    send_block_name: str = name
    smtp_override: Optional[SmtpConfig] = None
    fcc_imap_for_send: Optional[str] = name

    try:
        fetched = client.fetch_raw(uid, folder)
        if not fetched:
            typer.echo(f"Error: draft UID {uid} not found in {folder}", err=True)
            raise typer.Exit(1)

        msg = BytesParser().parsebytes(fetched["raw"])

        if identity_name or smtp_name:
            route = _resolve_send_route(
                cfg, identity_name, smtp_name, from_email, display_name, fcc
            )
            assert route is not None  # one of identity_name/smtp_name was set
            identity, smtp_override, fcc_imap_for_send = route
            if fcc_imap_for_send and fcc_imap_for_send != name:
                send_block = cfg.imap_blocks[fcc_imap_for_send]
                send_block_name = fcc_imap_for_send
            elif fcc_imap_for_send is None:
                send_block = None
                send_block_name = ""
            # Replace the draft's From with the resolved identity's address.
            if "From" in msg:
                del msg["From"]
            from email.utils import formataddr

            msg["From"] = formataddr((identity.name, identity.address))
        else:
            from_raw = str(msg.get("From", "") or "").strip()
            from_addr_only = from_raw
            if "<" in from_raw and ">" in from_raw:
                from_addr_only = from_raw.split("<", 1)[1].rsplit(">", 1)[0].strip()
            if not from_addr_only:
                typer.echo("Error: draft has no From header", err=True)
                raise typer.Exit(1)
            try:
                identity = resolve_identity_for_send(
                    cfg, name, from_addr=from_addr_only
                )
            except (IdentityNotFound, SendDisabled) as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1)

        envelope_bcc = list(bcc or [])
        if getattr(identity, "bcc", None):
            envelope_bcc.extend(identity.bcc)
        if envelope_bcc:
            existing = str(msg.get("Bcc", "") or "").strip()
            merged = (
                ", ".join([existing] + envelope_bcc)
                if existing
                else ", ".join(envelope_bcc)
            )
            if "Bcc" in msg:
                del msg["Bcc"]
            msg["Bcc"] = merged

        smtp_resolved = _resolve_smtp_or_exit(
            identity, send_block, send_block_name, cfg.smtp_blocks, smtp_override
        )

        if dry_run:
            factory = _pick_default_transport(smtp_resolved.port)
            try:
                conn = factory(smtp_resolved.host, smtp_resolved.port)
                conn.ehlo()
                if smtp_resolved.port in (587, 2587):
                    conn.starttls()
                    conn.ehlo()
                if smtp_resolved.username and smtp_resolved.password:
                    conn.login(smtp_resolved.username, smtp_resolved.password)
                conn.quit()
            except Exception as exc:
                typer.echo(f"Error: dry-run failed: {exc}", err=True)
                raise typer.Exit(1)
            _out(
                {
                    "dry_run": True,
                    "identity": identity.address,
                    "smtp": {"host": smtp_resolved.host, "port": smtp_resolved.port},
                }
            )
            return

        will_fcc = _will_fcc(
            smtp_resolved,
            save_sent,
            identity_fcc=identity.fcc,
            fcc_imap=fcc_imap_for_send,
        )
        _refuse_if_no_copy(
            will_fcc,
            bcc=envelope_bcc,
            sender_addr=identity.address,
            allow_no_copy=allow_no_copy,
        )
        if not will_fcc:
            send_client = None
        elif fcc_imap_for_send and fcc_imap_for_send != name:
            send_client = _make_client(imap_override=fcc_imap_for_send)
            owns_send_client = True
        elif fcc_imap_for_send is None:
            send_client = None

        try:
            result = _perform_send(
                send_client,
                smtp_resolved,
                msg,
                identity,
                sent_folder,
            )

            draft_removed = False
            if not keep_draft:
                try:
                    client.delete_email(uid, folder)
                    draft_removed = True
                except Exception as exc:
                    typer.echo(f"warning: draft delete failed: {exc}", err=True)

            result["draft_removed"] = draft_removed
            _out(result)
        finally:
            if owns_send_client and send_client is not None:
                send_client.disconnect()
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# accept-invite
# ---------------------------------------------------------------------------


@app.command("accept-invite")
def accept_invite(
    folder: str = typer.Option(
        ..., "--folder", "-f", help="Folder containing the invite email."
    ),
    uid: int = typer.Option(..., "--uid", "-u", help="Email UID."),
    availability_mode: str = typer.Option(
        "random",
        "--availability-mode",
        help="Availability mode: random, always_available, always_busy, business_hours, weekdays.",
    ),
) -> None:
    """Process a meeting invite and create a draft reply."""
    from courier.workflows.meeting_reply import process_meeting_invite_workflow

    client = _make_client()
    try:
        result = process_meeting_invite_workflow(client, folder, uid, availability_mode)
        _out(result)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# mcp (start MCP server)
# ---------------------------------------------------------------------------


@app.command("mcp")
def mcp_serve(
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to TOML configuration file.",
        envvar="COURIER_CONFIG",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    dev: bool = typer.Option(False, "--dev", help="Enable development mode."),
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    """Start the MCP server (Model Context Protocol)."""
    if version:
        print(f"Courier MCP server version {__version__}")
        raise typer.Exit()
    from courier.mcp_server import create_server

    server = create_server(config, debug)
    server.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_SEARCH_ALIASES = {"search-email", "search_email", "email-search", "email_search"}


def _rewrite_argv(argv: List[str]) -> List[str]:
    """Rewrite argv for AI-friendly invocation.

    Two transformations:
    1. If the subcommand is one of the known search aliases
       (``email_search``, ``email-search``, ``search_email``, ``search-email``),
       rewrite it to ``search`` and emit a note to stderr.
    2. If ``-i``/``--imap`` appears after the subcommand, hoist it to
       before the subcommand so Typer's global callback sees it.

    Skips when ``_COURIER_COMPLETE`` is set so shell-completion is undisturbed.
    """
    if os.environ.get("_COURIER_COMPLETE"):
        return argv
    out = list(argv)
    globals_with_value = {"--config", "-c", "--imap"}
    sub_idx: Optional[int] = None
    i = 0
    while i < len(out):
        tok = out[i]
        if tok == "--":
            break
        if tok in globals_with_value:
            i += 2
            continue
        if (
            tok.startswith("--")
            and "=" in tok
            and tok.split("=", 1)[0] in globals_with_value
        ):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub_idx = i
        break
    if sub_idx is None:
        return out
    sub = out[sub_idx]
    if sub in _SEARCH_ALIASES:
        sys.stderr.write(f"note: no such subcommand {sub!r}; running 'search'\n")
        out[sub_idx] = "search"
    imap_values: List[str] = []
    tail: List[str] = []
    j = sub_idx + 1
    while j < len(out):
        tok = out[j]
        if tok == "--imap" and j + 1 < len(out):
            imap_values.append(out[j + 1])
            j += 2
            continue
        if tok.startswith("--imap="):
            imap_values.append(tok.split("=", 1)[1])
            j += 1
            continue
        tail.append(tok)
        j += 1
    if imap_values:
        flat: List[str] = []
        for v in imap_values:
            flat.extend(["--imap", v])
        return out[:sub_idx] + flat + [out[sub_idx]] + tail
    return out


def main() -> None:
    sys.argv[1:] = _rewrite_argv(sys.argv[1:])
    chain = _split_chain_argv(sys.argv[1:])
    if chain is not None:
        global_argv, verbs, output_format, chain_defaults = chain
        _apply_global_flags(global_argv)
        sys.exit(_run_chain(verbs, output_format, chain_defaults))
    _print_eager_warnings_if_relevant()
    app()


if __name__ == "__main__":
    main()
