"""Crew Mode owner decisions 4, 5 and 6: one switch for every root, workers out of
History, and the server-side one-way lock.

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from kiro_crew import crew_mode


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    yield


def _slot(key: str, agent: str = "kirocrew", **over: Any) -> SimpleNamespace:
    base = dict(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        running=False,
        turn_running=False,
        # A chat the person has written in: its Crew Mode is locked.
        messages=[{"role": "user", "content": "go"}],
        _disk_older_count=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── decision 6: the lock ─────────────────────────────────────────────────────


class _Sel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture
def _sel(monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    rec = _Sel()
    monkeypatch.setattr(chat_handlers, "sel", lambda: rec)
    return rec


def _refusal(slot: Any, agent: str, kind: str = "template", migration: bool = False):
    from kiro_crew.dashboard import chat_handlers

    caller = (
        chat_handlers.SlotAgentSwitchCaller.for_crew_mode_migration()
        if migration
        else chat_handlers.SlotAgentSwitchCaller(app="", remote="t", owner=True)
    )
    return chat_handlers._crew_mode_lock_refusal(slot, agent, kind, caller)


def test_a_crew_mode_root_refuses_another_agent_with_crew_mode_locked(_sel):
    resp = _refusal(_slot("r", "kirocrew-team-lead"), "kirocrew")
    assert isinstance(resp, web.Response) and resp.status == 409
    assert json.loads(resp.text)["code"] == "crew_mode_locked"
    assert _sel.calls and _sel.calls[0]["outcome"] == "denied"


def test_crew_mode_turns_off_before_the_first_user_message_and_locks_after(_sel):
    # No user message yet (the switch notice is not one): back to a plain agent passes.
    fresh = _slot(
        "r", "kirocrew-conductor", messages=[{"role": "assistant", "content": "Switched"}]
    )
    assert _refusal(fresh, "kirocrew") is None
    # Not onto another conductor, and not onto a crewmate.
    assert _refusal(fresh, "kirocrew-team-lead").status == 409
    assert _refusal(fresh, "kirocrew", kind="member").status == 409
    # From the first user message on, it is locked, in the window or trimmed to disk.
    sent = _slot("r", "kirocrew-conductor")
    assert _refusal(sent, "kirocrew").status == 409
    trimmed = _slot("r", "kirocrew-conductor", messages=[], _disk_older_count=3)
    assert _refusal(trimmed, "kirocrew").status == 409


def test_the_lock_refuses_the_configured_conductor_too_unless_it_is_the_migration(_sel):
    root = _slot("r", "kirocrew-team-lead")
    assert _refusal(root, "kirocrew-conductor").status == 409
    assert _refusal(root, "kirocrew-conductor", migration=True) is None
    # The migration moves only onto the configured conductor.
    assert _refusal(root, "kirocrew", migration=True).status == 409


def test_a_re_pick_of_the_same_agent_passes_and_a_plain_chat_is_never_locked(_sel):
    assert _refusal(_slot("r", "kirocrew-conductor"), "kirocrew-conductor") is None
    assert _refusal(_slot("p"), "kirocrew-conductor") is None
    # A worker (something dispatched it) is not a Crew Mode root, so it is not locked.
    assert _refusal(_slot("w", "kirocrew-conductor", _created_by="r"), "kirocrew") is None
    assert _sel.calls == []


def test_the_provider_side_switch_is_vetoed_on_a_crew_mode_root():
    from kiro_crew.dashboard import chat_runner

    assert chat_runner.crew_mode_is_root(_slot("r", "kirocrew-conductor"))
    assert not chat_runner.crew_mode_is_root(_slot("p"))
    source = open(chat_runner.__file__, encoding="utf-8").read()
    assert 'source="crew_mode_locked"' in source


# ── decision 4: one switch for every root ────────────────────────────────────


class _State:
    def __init__(self, *slots: Any) -> None:
        self._slots = {s.key: s for s in slots}
        self.pushes = 0
        self.conversation_log = None

    def push_slots_update(self) -> None:
        self.pushes += 1


@pytest.fixture
def _switch(monkeypatch, tmp_path):
    """The migration's switch, recorded; ``busy`` keys answer turn_in_flight."""
    from kiro_crew.config import paths
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import crew_mode_migration as mig

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(mig, "_pending", set())
    monkeypatch.setattr(mig, "_failed", {})
    monkeypatch.setattr(mig, "_lock", None)
    monkeypatch.setattr(mig, "_usable", lambda name: "")
    monkeypatch.setattr(crew_mode, "_unusable", {})
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(mig, "_audit", lambda *a, **k: audits.append({"args": a}))
    calls: list[tuple[str, str, Any]] = []

    async def fake_switch(state, slot, name, agent, kind, caller):
        calls.append((name, agent, caller.crew_mode_migration))
        slot.agent = agent
        slot.crew_mode_agent = agent
        return web.json_response({"ok": True, "agent": agent})

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake_switch)
    return SimpleNamespace(calls=calls, audits=audits, mig=mig, home=tmp_path)


@pytest.mark.asyncio
async def test_a_first_start_records_the_conductor_and_moves_nothing(_switch):
    state = _State(_slot("r", "kirocrew-team-lead"))
    assert await _switch.mig.reconcile(state) is None
    assert _switch.mig.read_applied() == "kirocrew-conductor"
    assert _switch.calls == []
    assert state._slots["r"].agent == "kirocrew-team-lead"


@pytest.mark.asyncio
async def test_a_changed_conductor_moves_every_idle_root_and_audits_each(_switch, monkeypatch):
    _switch.mig.write_applied("kirocrew-conductor")
    monkeypatch.setattr(crew_mode, "_configured", "better-conductor")
    idle = _slot("r1", "kirocrew-conductor")
    old = _slot("r2", "kirocrew-team-lead")
    plain = _slot("p")
    worker = _slot("w", "kirocrew-conductor", _created_by="r1")
    state = _State(idle, old, plain, worker)
    out = await _switch.mig.reconcile(state)
    assert out == {"moved": ["r1", "r2"], "pending": []}
    assert idle.agent == old.agent == "better-conductor"
    assert plain.agent == "kirocrew" and worker.agent == "kirocrew-conductor"
    assert all(migration for _n, _a, migration in _switch.calls)
    assert len(_switch.audits) == 2
    assert _switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_a_busy_root_waits_for_its_next_idle_point(_switch, monkeypatch):
    _switch.mig.write_applied("kirocrew-conductor")
    monkeypatch.setattr(crew_mode, "_configured", "better-conductor")
    busy = _slot("r", "kirocrew-conductor", turn_running=True)
    state = _State(busy)
    out = await _switch.mig.reconcile(state)
    assert out == {"moved": [], "pending": ["r"]}
    assert busy.agent == "kirocrew-conductor" and _switch.calls == []
    # Not advanced while a root is owed the move: a restart migrates it then.
    assert _switch.mig.read_applied() == "kirocrew-conductor"
    busy.turn_running = False
    out = await _switch.mig.migrate(state)
    assert out == {"moved": ["r"], "pending": []}
    assert _switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_an_update_shipping_a_new_default_moves_the_roots(_switch):
    _switch.mig.write_applied("kirocrew-old-default")
    root = _slot("r", "kirocrew-old-default", crew_mode_agent="kirocrew-old-default")
    state = _State(root)
    await _switch.mig.reconcile(state)
    assert root.agent == "kirocrew-conductor"


@pytest.mark.asyncio
async def test_the_start_pass_stamps_old_conductor_sessions_and_learns_closed_roots(
    _switch,
):
    class _Log:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict]] = []

        def update_metadata(self, key: str, fields: dict) -> None:
            self.updates.append((key, fields))

        def list_sessions(self) -> list[dict]:
            return [
                {"key": "dashboard_old-root", "agent": "kirocrew-team-lead"},
                {"key": "dashboard_old-worker", "agent": "kirocrew-team-lead"},
                {"key": "dashboard_plain", "agent": "kirocrew"},
            ]

        def get_metadata(self, key: str) -> dict:
            if key == "dashboard_old-worker":
                return {"agent": "kirocrew-team-lead", "created_by": "old-root"}
            return {"agent": "kirocrew-team-lead"}

    lead = _slot("lead", "kirocrew-team-lead")
    state = _State(lead)
    state.conversation_log = _Log()
    await _switch.mig.on_start(state)
    assert lead.crew_mode_agent == "kirocrew-team-lead"
    assert state.conversation_log.updates == [
        ("dashboard:lead", {"crew_mode_agent": "kirocrew-team-lead"})
    ]
    assert "old-root" in crew_mode.known_roots()
    assert "old-worker" not in crew_mode.known_roots()


class _RootLog:
    def list_sessions(self) -> list[dict]:
        return [{"key": "dashboard_closed-root", "agent": "kirocrew-conductor"}]

    def get_metadata(self, key: str) -> dict:
        return {"agent": "kirocrew-conductor"}

    def update_metadata(self, key: str, fields: dict) -> None:
        pass


@pytest.mark.asyncio
async def test_G5_a_live_worker_of_a_closed_root_is_hidden_from_the_first_frame(_switch):
    """Edge review R3 G5: the restore learns closed roots before its one broadcast,
    so the first slot frame already stamps the worker; the start pass pushes a
    frame when it learned a root that holds a live slot."""
    worker = _slot("w", "kirocrew-worker", _created_by="closed-root")
    state = _State(worker)
    state.conversation_log = _RootLog()
    assert "closed-root" not in crew_mode.known_roots()
    # The restore path: learned before the first frame, and it says a slot moved.
    assert await _switch.mig.learn_closed_roots(state) is True
    assert "closed-root" in crew_mode.known_roots()
    stamps = crew_mode.live_stamps(state._slots)
    assert stamps["w"][0] == "closed-root"
    # Already known: nothing new, no frame owed.
    assert await _switch.mig.learn_closed_roots(state) is False


@pytest.mark.asyncio
async def test_G5_the_start_pass_pushes_a_frame_when_it_learns_a_holding_root(_switch):
    worker = _slot("w", "kirocrew-worker", _created_by="closed-root")
    state = _State(worker)
    state.conversation_log = _RootLog()
    await _switch.mig.on_start(state)
    assert state.pushes >= 1


def test_G5_the_restore_learns_closed_roots_inside_its_coalesced_push():
    from kiro_crew.dashboard.server_runtime import session_restore

    source = open(session_restore.__file__, encoding="utf-8").read()
    block = source.split("with state.suspend_slots_push():", 1)[1].split("\n    if ", 1)[0]
    assert "crew_mode_migration.learn_closed_roots(state)" in block


def test_a_history_stem_is_its_slot_key():
    from kiro_crew.dashboard.crew_mode_migration import history_slot_key

    assert history_slot_key("dashboard_chat-3-1792") == "chat-3-1792"
    assert history_slot_key("dashboard_dashboard_chat-3-1792") == "chat-3-1792"
    assert history_slot_key("dashboard:chat-3") == "chat-3"
    assert history_slot_key("slack_123") == "slack_123"


def test_the_config_key_is_watched_live():
    from kiro_crew.dashboard.server_runtime import config_watch

    source = open(config_watch.__file__, encoding="utf-8").read()
    assert '"agent.crew_mode_agent"' in source
    assert "crew_mode_migration.run(state)" in source


# ── decision 5: History ──────────────────────────────────────────────────────


def test_history_leaves_out_every_session_a_crew_mode_root_dispatched():
    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (
        _without_crew_workers,
    )

    state = _State(_slot("live-root", "kirocrew-conductor"))
    rows = [
        {"key": "dashboard_closed-root", "agent": "kirocrew-team-lead"},
        {"key": "dashboard_w1", "agent": "kirocrew", "created_by": "closed-root"},
        {"key": "dashboard_w2", "agent": "kirocrew", "created_by": "w1"},
        {"key": "dashboard_w3", "agent": "kirocrew", "created_by": "live-root"},
        {"key": "dashboard_plain", "agent": "kirocrew"},
        {"key": "dashboard_child", "agent": "kirocrew", "created_by": "plain"},
    ]
    kept = [r["key"] for r in _without_crew_workers(state, rows)]  # type: ignore[arg-type]
    assert kept == ["dashboard_closed-root", "dashboard_plain", "dashboard_child"]


def test_history_follows_the_crew_log_tree_too(monkeypatch):
    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (
        _without_crew_workers,
    )

    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"w": "gone"})
    crew_mode.note_roots(["gone"])
    rows = [{"key": "dashboard_w", "agent": "kirocrew"}, {"key": "dashboard_x"}]
    kept = [r["key"] for r in _without_crew_workers(_State(), rows)]  # type: ignore[arg-type]
    assert kept == ["dashboard_x"]
