"""The MCP probe and listing routes read ``~/.kiro/settings/mcp.json`` off the loop.

``_run_mcp_probe``, ``GET /api/mcp``, ``GET /api/mcp/active`` and
``POST /api/mcp/probe`` each read the Kiro-global ``mcp.json`` for its
disabled / ``disabledTools`` state. Read on the event loop, a slow home
directory (a network mount) stalled every session for the length of the read.
These tests swap the file for one that records the thread it was read on; no
real server is probed or spawned.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

import kiro_crew.mcp_discovery as disc
from kiro_crew.dashboard.handlers import mcp as mcp_mod


class _ThreadRecordingFile:
    """A stand-in for ``_GLOBAL_MCP_JSON`` that notes which thread read it."""

    def __init__(self, real: Path) -> None:
        self._real = real
        self.reader_threads: list[int] = []

    def read_text(self, encoding: str | None = None) -> str:
        self.reader_threads.append(threading.get_ident())
        return self._real.read_text(encoding=encoding)

    def __fspath__(self) -> str:
        return str(self._real)


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=False)


def _request(state: _State) -> Any:
    """The owner-claim request stand-in ``test_mcp_probe_treadmill`` uses, plus a query."""

    class _App(dict):
        pass

    class _Req(dict):
        def __init__(self, app: _App) -> None:
            super().__init__(user="local-app", app="")
            self.app = app
            self.query: dict[str, str] = {}
            self._claims = {"user": "local-app", "app": ""}

        def get(self, key: str, default: Any = None) -> Any:
            return self._claims.get(key, default)

        def __contains__(self, key: object) -> bool:
            return key in self._claims

        def __getitem__(self, key: str) -> Any:
            return self._claims[key]

    app = _App()
    app["state"] = state
    return _Req(app)


@pytest.fixture
def global_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ThreadRecordingFile:
    real = tmp_path / "mcp.json"
    real.write_text(json.dumps({"mcpServers": {"x": {"command": "true"}}}), encoding="utf-8")
    recorder = _ThreadRecordingFile(real)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", recorder)
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: [])
    monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_mod, "_evaluate_shareability", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    return recorder


def _run_probe() -> Awaitable[Any]:
    return mcp_mod._run_mcp_probe(None)


def _list_servers() -> Awaitable[Any]:
    return mcp_mod.api_mcp_servers(_request(_State()))


def _list_active() -> Awaitable[Any]:
    return mcp_mod.api_mcp_active(_request(_State()))


def _probe_route() -> Awaitable[Any]:
    return mcp_mod.api_mcp_probe(_request(_State()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [_run_probe, _list_servers, _list_active, _probe_route],
    ids=["_run_mcp_probe", "api_mcp_servers", "api_mcp_active", "api_mcp_probe"],
)
async def test_global_mcp_json_is_read_off_the_loop_thread(
    global_file: _ThreadRecordingFile, call: Callable[[], Awaitable[Any]]
) -> None:
    loop_thread = threading.get_ident()

    resp = await call()

    if resp is not None:
        assert resp.status == 200
    assert global_file.reader_threads, "the global mcp.json was not read at all"
    assert loop_thread not in global_file.reader_threads, "mcp.json was read on the event loop"
