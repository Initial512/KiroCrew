"""A Crew Mode conductor's own session calls to ITS workers need no per-call approval.

Only the verified ``kirocrew-dashboard`` session_send / session_status /
session_read_message identity with a verified ``target`` that is the exact key of a
live worker stamped under the calling conductor qualifies. Everything else asks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_turn.tool_approval import _crew_root_own_worker_call


def _slot(key: str, *, agent: str = "default", created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by="",
        _app="",
        instance_id="",
        crew_mode_agent="",
    )


def _state() -> SimpleNamespace:
    root = _slot("root-1", agent="kirocrew-conductor")
    other_root = _slot("root-2", agent="kirocrew-conductor")
    return SimpleNamespace(
        _slots={
            "root-1": root,
            "w-1": _slot("w-1", agent="kirocrew-worker", created_by="root-1"),
            "root-2": other_root,
            "w-2": _slot("w-2", agent="kirocrew-worker", created_by="root-2"),
            "plain": _slot("plain"),
        }
    )


def _event(tool: str = "session_send", target: object = "w-1", **over: object) -> SimpleNamespace:
    fields = dict(
        sub_session_id="",
        mcp_identity_trusted=True,
        raw_params_trusted=True,
        mcp_server_name="kirocrew-dashboard",
        tool_name=tool,
        raw_tool_params={"target": target, "message": "go"},
    )
    fields.update(over)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("tool", ["session_send", "session_status", "session_read_message"])
def test_conductor_call_to_its_own_worker_is_granted(tool: str) -> None:
    state = _state()
    assert _crew_root_own_worker_call(state, state._slots["root-1"], _event(tool)) is True


@pytest.mark.parametrize(
    "caller, event",
    [
        ("root-1", _event(target="w-2")),  # another conductor's worker
        ("root-1", _event(target="plain")),  # a plain session
        ("root-1", _event(target="root-1")),  # itself
        ("root-1", _event(target="Worker title")),  # by title, not key
        ("root-1", _event(tool="session_stop")),  # a verb not on the list
        ("root-1", _event(mcp_server_name="kirocrew-core")),
        ("root-1", _event(mcp_identity_trusted=False)),
        ("root-1", _event(raw_params_trusted=False)),
        ("root-1", _event(sub_session_id="sub-1")),  # a subagent's call
        ("plain", _event(target="w-1")),  # caller is no conductor
    ],
)
def test_anything_else_still_asks(caller: str, event: SimpleNamespace) -> None:
    state = _state()
    assert _crew_root_own_worker_call(state, state._slots[caller], event) is False
