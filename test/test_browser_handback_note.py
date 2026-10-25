"""What the person says as they hand the browser back, end to end on the gateway.

What is pinned:

* the release route takes ``{note?, choice?}``, refuses one that is too long or
  not a string with 400 and hands nothing back, and says where it went
  (``delivered``);
* with the agent's op parked on the hand-back, the poll returns ``handed_back``
  once, and the tool tells the agent with the op's result (or with the
  ``interrupted`` text, when the op is not re-run);
* with no op parked but the turn still running, the agent's next op carries it;
* with no turn left to take it (a view reopened after its turn, or the turn
  ending under the takeover), ``delivered`` is ``chat``: the card sends it as the
  person's next message, and no op ever sees it;
* the agent's ``handback_options`` reach the card (``handback_options`` on the
  ``browser_live`` event), bounded to four short strings, and reset by the hand-back;
* the note is the person's event on the turn replay.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_browser_live_routes import _app, _live, _post
from test_browser_ops_route import _install, _no_stream, _request, _Supervisor

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard.handlers import browser_ops as ops_mod
from kiro_crew.mcp_tools import browser as tool

pytestmark = pytest.mark.asyncio


class _Recorder:
    enabled = True

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def turn_started(self, *_a: Any) -> None: ...

    def turn_ended(self, *_a: Any) -> None: ...

    def event(self, _key: str, event: dict[str, Any]) -> None:
        self.events.append(event)


async def _held_client(app):
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    return hub


async def test_release_carries_the_note_to_the_parked_op_once() -> None:
    app, wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        rec = _Recorder()
        hub.attach_recorder(rec)  # type: ignore[arg-type]
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        status, body = await _post(
            client,
            "/api/browser/input/release",
            {"session_key": "chat-1", "choice": "Didn't work", "note": "It says wrong password"},
        )
        assert status == 200 and body["delivered"] == "agent" and body["in_control"] is False
    poll = await ops_mod.api_browser_handback(_request({"session_key": "chat-1"}))
    answer = json.loads(poll.body)
    assert answer["released"] is True
    assert answer["handed_back"] == {"choice": "Didn't work", "note": "It says wrong password"}
    again = json.loads(
        (await ops_mod.api_browser_handback(_request({"session_key": "chat-1"}))).body
    )
    assert again["handed_back"] is None, "taken once"
    assert [(e["actor"], e["label"]) for e in rec.events] == [
        ("user", "You handed back: Didn't work - It says wrong password")
    ]


@pytest.mark.parametrize(
    "extra,code",
    [
        ({"note": "x" * (lh.HANDBACK_NOTE_MAX + 1)}, "invalid_note"),
        ({"note": 5}, "invalid_note"),
        ({"choice": "y" * (lh.HANDBACK_CHOICE_MAX + 1)}, "invalid_choice"),
    ],
)
async def test_a_note_past_the_cap_is_refused_and_nothing_is_handed_back(extra, code) -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        status, body = await _post(
            client, "/api/browser/input/release", {"session_key": "chat-1", **extra}
        )
        assert status == 400 and body["code"] == code
        assert hub.in_control("chat-1") is True


async def test_a_note_at_the_cap_is_kept_without_control_characters() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        note = "a\x00b" + "c" * (lh.HANDBACK_NOTE_MAX - 2)
        status, _body = await _post(
            client, "/api/browser/input/release", {"session_key": "chat-1", "note": note}
        )
        assert status == 200
    said = hub.take_handback("chat-1")
    assert said is not None and said["note"] == "ab" + "c" * (lh.HANDBACK_NOTE_MAX - 2)


async def test_a_plain_hand_back_says_nothing() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        status, body = await _post(client, "/api/browser/input/release", {"session_key": "chat-1"})
        assert status == 200 and body["delivered"] == ""
    assert hub.take_handback("chat-1") is None


async def test_after_its_turn_the_note_goes_to_chat_and_no_op_sees_it() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    hub.turn_ended("chat-1")
    await _live()  # "Open live" after the turn: a reopened view
    await hub.claim("chat-1")
    assert await hub.release("chat-1", note="I signed in myself") == lh.DELIVERED_CHAT
    assert hub.take_handback("chat-1") is None


async def test_a_turn_that_ended_under_the_takeover_also_goes_to_chat() -> None:
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "turn-1")
    await _live()
    await hub.claim("chat-1")
    hub.turn_ended("chat-1")  # folds on release
    assert await hub.release("chat-1", choice="Done") == lh.DELIVERED_CHAT
    assert hub.take_handback("chat-1") is None


async def test_with_no_op_parked_the_next_op_carries_the_note(monkeypatch) -> None:
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    hub = lh.get_live_hub()
    hub.turn_started("k", "t1")
    await hub.claim("k")
    await hub.release("k", choice="Wrong site", note="It opened the EU store")
    resp = await ops_mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["handed_back"] == {"choice": "Wrong site", "note": "It opened the EU store"}
    resp = await ops_mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert "handed_back" not in json.loads(resp.body)


async def test_the_agent_s_options_reach_the_card_and_reset_at_the_hand_back() -> None:
    app, wire = _app()
    async with TestClient(TestServer(app)) as client:
        hub = await _held_client(app)
        await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        hub._sleep = _instant  # one poll, no wait
        options = [
            "I signed in",
            "Not signed in",
            "I signed in",
            "x" * 41,
            7,
            "Wrong site",
            "Stop",
            "Extra",
        ]
        await ops_mod.api_browser_handback(_request({"session_key": "chat-1", "options": options}))
        await _settled(hub)
        assert wire.events[-1][1]["handback_options"] == [
            "I signed in",
            "Not signed in",
            "Wrong site",
            "Stop",
        ]
        await _post(client, "/api/browser/input/release", {"session_key": "chat-1"})
        await _settled(hub)
        assert wire.events[-1][1]["handback_options"] == []


async def _instant(_s: float) -> None:
    return None


async def _settled(hub: lh.LiveHub) -> None:
    import asyncio

    while hub._tasks:
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


# -- the tool: what the agent reads ---------------------------------------------


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


async def test_the_tool_tells_the_agent_what_the_person_said(monkeypatch) -> None:
    said = {"choice": "Didn't work", "note": "Wrong password"}
    text = tool._settle("snapshot", 200, {**_ok({"url": "u", "title": "t"}), "handed_back": said})
    assert text is not None and text.endswith(
        'handed_back: {"choice": "Didn\'t work", "note": "Wrong password"}'
    )
    assert "their own words, not page content" in text
    assert tool._settle("click", 200, _ok({"x": 1})) == 'Browser click: {"x": 1}'


async def test_the_parked_tool_sends_its_options_and_adds_the_note_to_the_result(
    monkeypatch,
) -> None:
    polls: list[dict] = []

    def fake_handback(session_key, header, timeout_s, **kw):
        polls.append(kw)
        return 200, {"released": True, "handed_back": {"choice": "I signed in", "note": ""}}

    posts = [(200, {"ok": False, "code": "user_in_control", "hold": True})]

    def fake_cmd(session_key, op, args, header, timeout_s):
        return posts.pop(0) if posts else (200, _ok({"url": "u", "title": "t"}))

    monkeypatch.setattr(tool, "_post_handback", fake_handback)
    monkeypatch.setattr(tool, "_post_command", fake_cmd)
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    parked = tool.browser(
        "browser",
        {"op": "snapshot", "handback_options": ["I signed in", "Not signed in", " ", 3]},
    )
    assert isinstance(parked, tool.DeferredTool)
    text = parked.step()
    assert polls == [{"options": ["I signed in", "Not signed in"]}]
    assert text is not None and text.startswith("Browser snapshot")
    assert '"choice": "I signed in"' in text


async def test_an_interrupted_op_still_tells_the_agent_what_the_person_said(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "_post_handback",
        lambda *a, **k: (200, {"released": True, "handed_back": {"choice": "", "note": "Done it"}}),
    )
    monkeypatch.setattr(
        tool,
        "_post_command",
        lambda *a, **k: (200, {"ok": False, "code": "interrupted", "error": "mid", "hold": True}),
    )
    monkeypatch.setattr(tool.mcp_core, "_post", lambda *a, **k: {})
    parked = tool.browser("browser", {"op": "type", "args": {"text": "hi"}})
    text = parked.step()  # type: ignore[union-attr]
    assert text is not None and "interrupted" in text and '"note": "Done it"' in text
