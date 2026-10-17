"""Importing courier has no side effect.

A library caller hands courier its configuration; the ``.env`` file and the
logging setup belong to the entry points (``courier`` and the MCP server).
Each import runs in a fresh interpreter whose cwd and HOME hold a planted
``.env`` and ``~/.config/courier/config.toml``. ``-c`` is deliberate: with
no ``__main__.__file__``, python-dotenv searches from the cwd, so an
import-time ``load_dotenv()`` would find the planted file.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest

from courier import __main__ as cli

_PLANTED_ENV = {
    "COURIER_PLANTED": "1",
    "IMAP_PASSWORD": "planted",
    "WORLD_AS_OF": "garbage",
}

_PROBE = """
import importlib, json, logging, os, sys

before = dict(os.environ)
importlib.import_module(sys.argv[1])
after = dict(os.environ)
print(json.dumps({
    "env_changed": sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    ),
    "root_handlers": [type(h).__name__ for h in logging.getLogger().handlers],
    "dotenv_loaded": "dotenv" in sys.modules,
}))
"""

_needs_mcp = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None, reason="needs the mcp extra"
)


def _plant(home: Path) -> None:
    (home / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in _PLANTED_ENV.items())
    )
    config_dir = home / ".config" / "courier"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[imap.planted]\n"
        'host = "imap.planted.invalid"\n'
        'username = "planted@planted.invalid"\n'
        'password = "planted"\n'
    )


def _import_in_fresh_interpreter(module: str, home: Path) -> Dict[str, Any]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _PLANTED_ENV and key not in ("COURIER_CONFIG", "XDG_CONFIG_HOME")
    }
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report: Dict[str, Any] = json.loads(result.stdout.splitlines()[-1])
    return report


@pytest.mark.parametrize(
    "module", ["courier", "courier.auth_setup", "courier.gmail_auth"]
)
def test_import_has_no_side_effect(module: str, tmp_path: Path) -> None:
    _plant(tmp_path)
    report = _import_in_fresh_interpreter(module, tmp_path)
    assert report["env_changed"] == []
    assert report["root_handlers"] == []
    assert report["dotenv_loaded"] is False


@_needs_mcp
def test_import_mcp_server_has_no_side_effect(tmp_path: Path) -> None:
    # dotenv is not checked here: the mcp SDK imports it through
    # pydantic-settings.
    _plant(tmp_path)
    report = _import_in_fresh_interpreter("courier.mcp_server", tmp_path)
    assert report["env_changed"] == []
    assert report["root_handlers"] == []


def test_cli_main_loads_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dotenv = mock.Mock()
    monkeypatch.setattr(cli, "load_dotenv", load_dotenv)
    monkeypatch.setattr(sys, "argv", ["courier", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    load_dotenv.assert_called_once_with()


@_needs_mcp
def test_mcp_server_main_loads_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    from courier import mcp_server

    load_dotenv = mock.Mock()
    monkeypatch.setattr(mcp_server, "load_dotenv", load_dotenv)
    monkeypatch.setattr(sys, "argv", ["courier-mcp", "--version"])
    mcp_server.main()
    load_dotenv.assert_called_once_with()
