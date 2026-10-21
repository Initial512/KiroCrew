"""A pi member's saved spec is confirmed against the spec its session consumed.

pi has no mirror, so the consumed spec is the ``session_mcp_projection`` parse that
``AcpClient._mirrorless_member_mounts`` also builds the member mounts and the zero-tool
answer from. These drive the REAL resolver over a real spec on disk and the REAL
confirmation over what it recorded. The live round trip is
``test/fixtures/acp_frames/pi/saved-spec-projection-live.jsonl``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import agent_state
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_capabilities import _digest, member_backend_gap
from kiro_crew.platform.governance_profiles import governance_answer_generation
from kiro_crew.session_capabilities import LoadedCapabilities, runtime_view

FIXTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "pi" / "saved-spec-projection-live.jsonl"
)

agents_dir = deepseek_mount.agents_dir
MEMBER_KEY = deepseek_mount.MEMBER_KEY

SAVED = {"name": "kirocrew", "description": "a saved member", "tools": ["*"]}


def _write(agents_dir: Path, spec: dict) -> dict:
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    return spec


def _pi_member(tmp_path: Path) -> AcpClient:
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    client.member_context = True
    client._session_id = "pi-session-1"
    client._confirmed_capability_session = None
    client._loaded_capability_template = ""
    client._capability_projection_gaps = ()
    return client


def _saved_as(monkeypatch, spec: dict) -> None:
    """Stamp the saved intent the way a save does: the digest of the spec as saved."""
    intent = {"materialized": _digest(spec)}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _template: intent)


def test_pi_clears_the_start_gate_but_not_the_tool_limits_one() -> None:
    assert ACP_BACKEND_PI in ACP_BACKENDS_MEMBER_CAPABILITIES
    assert member_backend_gap(ACP_BACKEND_PI, ()) is None
    # disabledTools is still kiro-cli's alone: such a spec is refused before spawn.
    assert ACP_BACKEND_PI not in ACP_BACKENDS_MEMBER_TOOL_LIMITS
    assert member_backend_gap(ACP_BACKEND_PI, ("docs",)) == "tool_limits_unsupported"


def test_the_consumed_spec_is_the_parse_the_mounts_came_from(
    agents_dir, tmp_path, monkeypatch
) -> None:
    """One parse: the recorded spec IS the projection's, and no second read happens."""
    _write(agents_dir, SAVED)
    real = session_mcp.session_mcp_projection
    seen = []

    def counting(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(session_mcp, "session_mcp_projection", counting)
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    assert len(seen) == 1
    assert client._session_agent_spec is seen[0].agent_spec
    assert client._spec_zero_tools is seen[0].zero_tools


def test_a_saved_spec_is_confirmed_on_pi(agents_dir, tmp_path, monkeypatch) -> None:
    spec = _write(agents_dir, SAVED)
    _saved_as(monkeypatch, spec)
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    asyncio.run(client.confirm_member_projection())
    assert client._loaded_capability_template == "kirocrew"
    assert client.capability_projection_gaps == ()
    assert client._confirmed_capability_session == "pi-session-1"


def test_a_spec_edited_after_its_save_is_refused_on_pi(agents_dir, tmp_path, monkeypatch) -> None:
    _saved_as(monkeypatch, _write(agents_dir, SAVED))
    _write(agents_dir, dict(SAVED, description="edited after it was saved"))
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        asyncio.run(client.confirm_member_projection())
    assert client._loaded_capability_template == ""


def test_an_unreadable_spec_on_a_re_resolve_drops_the_earlier_parse(
    agents_dir, tmp_path, monkeypatch
) -> None:
    """A failed re-read must not leave the previous generation standing as consumed."""
    _saved_as(monkeypatch, _write(agents_dir, SAVED))
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    assert client._session_agent_spec is not None

    def unreadable(*_a, **_k):
        raise OSError("spec unreadable")

    monkeypatch.setattr(session_mcp, "session_mcp_projection", unreadable)
    assert client._resolve_session_mcp_servers() == []
    assert client._session_agent_spec is None
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        asyncio.run(client.confirm_member_projection())


def test_kiro_records_no_consumed_spec(agents_dir, tmp_path) -> None:
    """kiro-cli reads the saved file itself through ``--agent``; this branch is not its."""
    _write(agents_dir, SAVED)
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_KIRO)
    marker = {"untouched": True}
    client._session_agent_spec = marker
    assert client._mirrorless_member_mounts(()) == []
    assert client._session_agent_spec is marker


def _view(provider, mcp_servers: tuple[str, ...]) -> dict:
    stamp = LoadedCapabilities(
        "A",
        "kirocrew",
        "rev-1",
        "proc-1",
        "pi-session-1",
        governance_answer_generation(),
        mcp_servers,
    )
    session = SimpleNamespace(
        capability_member="A",
        loaded_capabilities=stamp,
        provider=provider,
        semaphore=asyncio.Lock(),
    )
    state = SimpleNamespace(sessions={"dashboard:A": session}, capability_failures={})
    return runtime_view(state, "A", "rev-1")


def _confirmed_pi_provider() -> SimpleNamespace:
    # What a confirmed pi session reports: pi sends no MCP registration frames, so its
    # report is the empty one an AcpClient starts with.
    return SimpleNamespace(
        is_process_alive=lambda: True,
        process_instance="proc-1",
        session_id="pi-session-1",
        loaded_capability_template="kirocrew",
        capability_projection_gaps=(),
        mcp_session_report=McpSessionReport,
    )


def test_a_declared_server_that_did_not_reach_pi_keeps_the_pane_unverified() -> None:
    """The ``BROKER_ONLY`` gap: confirming the spec does not claim its servers run."""
    view = _view(_confirmed_pi_provider(), ("spec-probe",))
    assert view["status"] == "unverified"
    assert view["sessions"][0]["error_code"] == "capability_mcp_unreported"


def test_a_confirmed_spec_with_no_servers_reads_applied_on_pi() -> None:
    assert _view(_confirmed_pi_provider(), ())["status"] == "applied"


def _frames() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def test_the_live_capture_confirms_a_saved_spec_and_runs_its_server() -> None:
    header, *frames = _frames()
    meta = header["_meta"]
    assert meta["recorded"] == "live" and meta["backend"] == ACP_BACKEND_PI
    assert meta["agent_version"] == "0.0.34 (pi 1.3.0)"
    for fact in ("consumed_spec_matches was true", "capability_runtime_unverified"):
        assert fact in meta["note"]
    updates = [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update" and isinstance(f["params"].get("update"), dict)
    ]
    call = next(u for u in updates if u.get("title") == "mcp__probe_server__echo")
    asks = [f for f in frames if f.get("method") == "session/request_permission"]
    assert len(asks) == 1
    envelope = json.loads(asks[0]["params"]["toolCall"]["rawInput"]["message"])
    assert envelope["nonce"] == meta["gate_envelope_nonce"]
    assert envelope["toolCallId"] == call["toolCallId"]
    done = [
        u
        for u in updates
        if u.get("toolCallId") == call["toolCallId"] and u.get("status") == "completed"
    ]
    assert done and done[-1]["rawOutput"]["details"]["server"] == "probe-server"
    assert "ECHO:ping" in json.dumps(done[-1]["content"])
