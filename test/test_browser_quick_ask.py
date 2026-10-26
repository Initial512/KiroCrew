"""Quick asks: the ``ask`` op, its binding to the page, the person's answer, and the leak bar.

The page is a fake CDP peer (:class:`FakePage`) that answers the handful of
methods ``browser_harness.quick_ask`` sends, keyed by ``backendNodeId``, so a test
can navigate it, re-render a field, cover it or move it into another document
between the ask and the answer. The hub and the routes are the real ones.

The invariant under test: a value the person types reaches only the bound element
on the bound origin (``Input.insertText`` right after ``DOM.focus`` of that node),
and nothing the agent can read -- tool text, the poll, logs, SEL, the replay,
the card's state -- ever contains it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_browser_live_hub import FakeStream
from test_browser_live_routes import _app, _post

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.dashboard.handlers import browser_live as live_mod
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.mcp_tools import browser as tool

pytestmark = pytest.mark.asyncio

KEY = "chat-1"
CANARY = "hunter2-CANARY-7c1f"


# ── the fake page ───────────────────────────────────────────────────────────


def _node(**over: Any) -> dict[str, Any]:
    info = {
        "connected": True,
        "sameDoc": True,
        "tag": "input",
        "type": "text",
        "role": "",
        "href": False,
        "disabled": False,
        "readOnly": False,
        "visible": True,
        "covered": False,
        "rect": [10, 20, 200, 30],
        "vw": 1280,
        "vh": 800,
        "text": "",
    }
    info.update(over)
    return info


class FakePage:
    """A CDP peer for one tab: a top frame, nodes by backendNodeId, refs onto them."""

    def __init__(self) -> None:
        self.frame = {"id": "F1", "loaderId": "L1", "url": "https://github.com/login"}
        self.nodes: dict[int, dict[str, Any]] = {}
        self.refs: dict[str, int] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.focused: int | None = None
        self.passed = False
        self.closed = False
        self._seq = 100

    def add(self, ref: str, **info: Any) -> int:
        self._seq += 1
        self.nodes[self._seq] = _node(**info)
        self.refs[ref] = self._seq
        return self._seq

    def rerender(self, ref: str) -> None:
        """The framework replaced the element: same ref name, a new node; the old one detached."""
        old = self.refs[ref]
        self.nodes[old]["connected"] = False
        self.add(ref, **{k: v for k, v in self.nodes[old].items() if k != "connected"})

    def written(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "Input.insertText"]

    def clicks(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "Input.dispatchMouseEvent"]

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": dict(self.frame)}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            ref = re.search(r"get\('(e\d+)'\)", params["expression"])
            node = self.refs.get(ref.group(1)) if ref else None
            if node is None:
                return {"result": {"type": "object", "subtype": "null"}}
            return {"result": {"objectId": f"m:{node}"}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": int(params["objectId"].split(":")[1])}}
        if method == "DOM.resolveNode":
            node = params["backendNodeId"]
            if node not in self.nodes:
                raise RuntimeError("No node with given id found")
            return {"object": {"objectId": f"i:{node}"}}
        if method == "Runtime.callFunctionOn":
            node = int(params["objectId"].split(":")[1])
            fn = params["functionDeclaration"]
            if fn == qa._INSPECT_JS:
                value: Any = dict(self.nodes[node])
            elif fn == qa._FOCUSED_JS:
                value = self.focused == node
            elif fn == qa._PREPARE_JS:
                self.nodes[node]["asked"] = True
                value = True
            elif fn == qa._PASSED_JS:
                value = self.passed
            else:
                raise AssertionError(f"unexpected function {fn[:40]}")
            return {"result": {"value": value}}
        if method == "DOM.focus":
            self.focused = params["backendNodeId"]
        return {}

    def agent_targets(self) -> list[str]:
        return ["tgt"]


class Recorder:
    """The replay recorder's seam: keeps every event, ignores the rest."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, key: str, event: dict[str, Any]) -> None:
        self.events.append((key, event))

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(live_mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None))

    async def _keep_streams(_app: web.Application) -> None:
        return None

    # Each answer runs its own test server; its cleanup must not stop the hub's stream.
    monkeypatch.setattr(route_slice, "_close_live_hub", _keep_streams)
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


@pytest.fixture
def sel_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        browser_ask, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw))
    )
    return rows


async def _live(page: FakePage) -> tuple[lh.LiveHub, Recorder]:
    hub = lh.get_live_hub()
    rec = Recorder()
    hub.attach_recorder(rec)  # type: ignore[arg-type]
    hub.turn_started(KEY, "t1")
    await hub.start_stream(
        KEY, page, "tgt", browser="edge", headless=False, stream_factory=FakeStream  # type: ignore[arg-type]
    )
    hub.bind_target(KEY, "tgt")
    return hub, rec


def _login(page: FakePage) -> None:
    page.add("e3", type="text", rect=[10, 20, 200, 30])
    page.add("e4", type="password", rect=[10, 60, 200, 30])


CREDS = {"kind": "credentials", "targets": ["username=e3", "password=e4"], "reason": "Sign in"}


# ── pure validation ─────────────────────────────────────────────────────────


def test_every_kind_parses_with_its_own_roles() -> None:
    assert qa.parse_ask(CREDS).targets == {"username": "e3", "password": "e4"}
    assert qa.parse_ask({**CREDS, "targets": ["password=e4"]}).targets == {"password": "e4"}
    text = qa.parse_ask({"kind": "text", "targets": ["text=e1"], "reason": "r", "label": "email"})
    assert text.label == "email"
    assert qa.parse_ask({"kind": "text", "targets": ["text=e1"], "reason": "r"}).label == "other"
    assert (
        qa.parse_ask({"kind": "otp", "targets": ["otp=e1"], "reason": "r", "digits": 6}).digits == 6
    )
    assert qa.parse_ask({"kind": "human_check", "targets": {"widget": "e9"}, "reason": "r"})
    c = qa.parse_ask(
        {"kind": "confirm", "targets": ["action=e2"], "reason": "r", "summary": "Pay $35"}
    )
    assert c.summary == "Pay $35"


@pytest.mark.parametrize(
    "args, code",
    [
        ({**CREDS, "kind": "card_number"}, "bad_kind"),
        ({**CREDS, "targets": ["username=e3"]}, "bad_targets"),
        ({**CREDS, "targets": ["password=e4", "text=e5"]}, "bad_targets"),
        ({**CREDS, "targets": ["password=e4", "password=e5"]}, "bad_targets"),
        ({**CREDS, "targets": ["username=e4", "password=e4"]}, "bad_targets"),
        ({**CREDS, "targets": ["password=#login"]}, "bad_ref"),
        ({**CREDS, "targets": "password=e4"}, "bad_targets"),
        ({**CREDS, "reason": ""}, "bad_reason"),
        ({**CREDS, "reason": "\u202e\u200b"}, "bad_reason"),
        ({"kind": "otp", "targets": ["otp=e1"], "reason": "r"}, "bad_digits"),
        ({"kind": "otp", "targets": ["otp=e1"], "reason": "r", "digits": 3}, "bad_digits"),
        ({"kind": "otp", "targets": ["otp=e1"], "reason": "r", "digits": True}, "bad_digits"),
        ({"kind": "confirm", "targets": ["action=e1"], "reason": "r"}, "bad_summary"),
        ("nope", "bad_args"),
    ],
)
def test_bad_args_are_refused_with_a_code(args: Any, code: str) -> None:
    with pytest.raises(qa.AskError) as exc:
        qa.parse_ask(args)
    assert exc.value.code == code


def test_the_reason_is_plain_one_line_and_cut_to_80() -> None:
    raw = "Sign\u202e in\n\tnow\x00 " + "x" * 200
    reason = qa.parse_ask({**CREDS, "reason": raw}).reason
    assert reason.startswith("Sign in now x") and len(reason) <= qa.REASON_MAX
    assert not re.search("[\x00-\x1f\u202a-\u202e\u200b]", reason)


@pytest.mark.parametrize(
    "url, origin",
    [
        ("https://github.com/login?x=1", "https://github.com"),
        ("https://shop.example.com:8443/a", "https://shop.example.com:8443"),
        ("http://Example.com:80/", "http://example.com"),
        ("about:blank", ""),
        ("javascript:alert(1)", ""),
        ("https://[::1]:9/", "https://[::1]:9"),
    ],
)
def test_the_origin_comes_from_the_committed_url(url: str, origin: str) -> None:
    assert qa.origin_of(url) == origin


@pytest.mark.parametrize(
    "role, over, problem",
    [
        ("password", {"type": "password"}, ""),
        ("password", {"type": "text"}, "wrong_type"),
        ("otp", {"type": "password"}, "wrong_type"),
        ("text", {"tag": "div"}, "wrong_type"),
        ("text", {"type": "checkbox"}, "wrong_type"),
        ("username", {"visible": False}, "hidden"),
        ("username", {"covered": True}, "covered"),
        ("username", {"sameDoc": False}, "cross_frame"),
        ("username", {"connected": False}, "gone"),
        ("username", {"readOnly": True}, "disabled"),
        ("action", {"tag": "button"}, ""),
        ("action", {"tag": "div", "role": "button"}, ""),
        ("action", {"tag": "div"}, "wrong_type"),
        ("widget", {"tag": "iframe"}, ""),
        ("widget", {"tag": "input", "type": "checkbox"}, ""),
        ("widget", {"tag": "span", "role": "checkbox"}, ""),
        ("widget", {"tag": "iframe", "covered": True}, "covered"),
        ("widget", {"tag": "button"}, "wrong_type"),
        ("widget", {"tag": "div"}, "wrong_type"),
        ("widget", {"tag": "iframe", "rect": [0, 0, 1280, 800]}, "too_large"),
    ],
)
def test_target_problems(role: str, over: dict[str, Any], problem: str) -> None:
    assert qa.target_problem(role, _node(**over)) == problem


# ── binding ─────────────────────────────────────────────────────────────────


async def test_an_ask_binds_origin_frame_and_nodes_and_goes_on_the_card(
    sel_rows: list[dict[str, Any]],
) -> None:
    page = FakePage()
    _login(page)
    hub, rec = await _live(page)
    hub.get(KEY).stream.scale = 0.5  # type: ignore[union-attr]
    out = await browser_ask.run_ask(hub, KEY, dict(CREDS))
    assert out["ok"] is True and isinstance(out["ask_id"], str)
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (ask.origin, ask.frame_id, ask.loader_id) == ("https://github.com", "F1", "L1")
    assert [b.node for b in ask.bindings] == [page.refs["e3"], page.refs["e4"]]
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["kind"] == "credentials" and wire["origin"] == "https://github.com"
    assert wire["phase"] == "waiting" and wire["frame"] == {"w": 640, "h": 400}
    assert wire["fields"] == [
        {"role": "username", "input": "text", "rect": {"x": 5, "y": 10, "w": 100, "h": 15}},
        {"role": "password", "input": "password", "rect": {"x": 5, "y": 30, "w": 100, "h": 15}},
    ]
    assert wire["expires_at"] == ask.expires_at
    assert rec.events[-1][1]["actor"] == "person"
    assert rec.events[-1][1]["ask"] == {
        "kind": "credentials",
        "action": "ask",
        "outcome": "pending",
        "origin": "https://github.com",
    }


@pytest.mark.parametrize(
    "over, code",
    [
        ({"sameDoc": False}, "cross_frame"),
        ({"visible": False}, "hidden"),
        ({"rect": [0, 0, 0, 0], "visible": False}, "hidden"),
        ({"covered": True}, "covered"),
        ({"type": "text"}, "wrong_type"),
    ],
)
async def test_a_bad_password_target_is_refused_at_ask_time(
    over: dict[str, Any], code: str
) -> None:
    page = FakePage()
    page.add("e4", **{"type": "password", **over})
    hub, _rec = await _live(page)
    out = await browser_ask.run_ask(hub, KEY, {**CREDS, "targets": ["password=e4"]})
    assert out == {"ok": True, "outcome": "refused", "code": code}
    assert hub.get(KEY).ask is None  # type: ignore[union-attr]


async def test_a_stale_ref_and_a_non_web_page_are_refused() -> None:
    page = FakePage()
    hub, _rec = await _live(page)
    assert (await browser_ask.run_ask(hub, KEY, dict(CREDS)))["code"] == "ref_stale"
    page.frame["url"] = "about:blank"
    _login(page)
    assert (await browser_ask.run_ask(hub, KEY, dict(CREDS)))["code"] == "not_web_page"


async def test_no_live_card_means_no_ask() -> None:
    hub = lh.get_live_hub()
    hub.turn_started(KEY, "t1")
    out = await browser_ask.run_ask(hub, KEY, dict(CREDS))
    assert out["code"] == "no_live_view"


async def test_one_ask_at_a_time() -> None:
    page = FakePage()
    _login(page)
    hub, _rec = await _live(page)
    assert (await browser_ask.run_ask(hub, KEY, dict(CREDS)))["ok"] is True
    assert (await browser_ask.run_ask(hub, KEY, dict(CREDS)))["code"] == "busy"


# ── answers over the owner route ────────────────────────────────────────────


async def _asked(page: FakePage, args: dict[str, Any]) -> tuple[lh.LiveHub, Recorder, Any]:
    hub, rec = await _live(page)
    out = await browser_ask.run_ask(hub, KEY, args)
    assert out["ok"] is True and "ask_id" in out, out
    return hub, rec, hub.get(KEY).ask  # type: ignore[union-attr]


def _poll_app() -> web.Application:
    @web.middleware
    async def internal(request: web.Request, handler: Any) -> Any:
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app.router.add_post("/api/browser/ask/poll", browser_ask.api_browser_ask_poll)
    return app


async def _poll(ask_id: str) -> dict[str, Any]:
    async with TestClient(TestServer(_poll_app())) as client:
        _status, body = await _post(
            client, "/api/browser/ask/poll", {"session_key": KEY, "ask_id": ask_id}
        )
    return body


async def _answer(body: dict[str, Any]) -> tuple[int, dict[str, Any], Any]:
    hub = lh.get_live_hub()
    rec = hub.recorder
    app, wire = _app()
    hub.attach_recorder(rec)  # registering the slice attached the real recorder
    async with TestClient(TestServer(app)) as client:
        status, out = await _post(client, "/api/browser/ask/answer", {"session_key": KEY, **body})
    return status, out, wire


async def test_fill_writes_only_into_the_bound_nodes_and_leaks_nowhere(
    sel_rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    page = FakePage()
    _login(page)
    hub, rec, ask = await _asked(page, dict(CREDS))
    status, out, wire = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"username": "octocat", "password": CANARY}}
    )
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})
    assert page.written() == ["octocat", CANARY]
    # Each insert follows the focus of ITS bound node.
    seq = [
        (m, p.get("backendNodeId")) for m, p in page.calls if m in ("DOM.focus", "Input.insertText")
    ]
    assert seq == [
        ("DOM.focus", page.refs["e3"]),
        ("Input.insertText", None),
        ("DOM.focus", page.refs["e4"]),
        ("Input.insertText", None),
    ]
    polled = await _poll(ask.id)
    assert polled == {"done": True, "outcome": "filled"}
    text = tool._ask_text(polled["outcome"])
    leak_surfaces = [
        json.dumps(polled),
        text,
        json.dumps(out),
        caplog.text,
        json.dumps(sel_rows, default=str),
        json.dumps([e for _k, e in rec.events]),
        json.dumps(wire.events, default=str),
        json.dumps(lh.live_event(hub.get(KEY))),  # type: ignore[arg-type]
    ]
    for surface in leak_surfaces:
        assert CANARY not in surface
        assert "octocat" not in surface
    outcomes = [e["ask"]["outcome"] for _k, e in rec.events if e.get("actor") == "person"]
    assert outcomes == ["pending", "filled"]
    # Both fields are marked as the person's before the write, so the walker, the
    # frame mask and the screenshot guard withhold them afterwards.
    assert page.nodes[page.refs["e3"]].get("asked") and page.nodes[page.refs["e4"]].get("asked")


async def test_an_answer_that_waited_on_the_fence_past_the_turn_lands_nothing() -> None:
    page = FakePage()
    page.add("e2", tag="button", text="Buy")
    hub, _rec, ask = await _asked(
        page, {"kind": "confirm", "targets": ["action=e2"], "reason": "Buy", "summary": "Pay"}
    )
    fence = hub.op_fence(KEY)
    await fence.acquire()  # an agent op is running
    task = asyncio.ensure_future(_answer({"ask_id": ask.id, "action": "confirm"}))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if ask.lock.locked():
            break
    hub.turn_ended(KEY)  # the turn folds the ask while the answer waits
    fence.release()
    status, out, _ = await task
    assert (status, out["error"]) == (409, "stale")
    assert page.clicks() == []


@pytest.mark.parametrize(
    "change", ["navigate", "same_origin_nav", "rerender", "overlay", "iframe_swap", "tab_switch"]
)
async def test_a_page_that_moved_gets_origin_changed_and_nothing_written(change: str) -> None:
    page = FakePage()
    _login(page)
    hub, _rec, ask = await _asked(page, dict(CREDS))
    if change == "navigate":
        page.frame.update(url="https://evil.example/login", loaderId="L2")
    elif change == "same_origin_nav":
        page.frame.update(url="https://github.com/other", loaderId="L2")
    elif change == "rerender":
        page.rerender("e4")
    elif change == "overlay":
        page.nodes[page.refs["e4"]]["covered"] = True
    elif change == "tab_switch":
        # The agent moved to another tab: the card streams it now (same fake page,
        # so only the target check can tell).
        await hub.start_stream(
            KEY, page, "tgt2", browser="edge", headless=False, stream_factory=FakeStream  # type: ignore[arg-type]
        )
    else:
        page.nodes[page.refs["e4"]]["sameDoc"] = False
    status, out, _wire = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"username": "u", "password": CANARY}}
    )
    assert (status, out) == (409, {"ok": False, "error": "origin_changed"})
    assert page.written() == []
    assert (await _poll(ask.id))["outcome"] == "origin_changed"


async def test_a_focus_the_page_steals_writes_nothing() -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    original = page.send

    async def steal(method: str, params: Any = None, session_id: Any = None) -> Any:
        out = await original(method, params, session_id)
        if method == "DOM.focus":
            page.focused = 999
        return out

    page.send = steal  # type: ignore[method-assign]
    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"username": "u", "password": CANARY}}
    )
    assert status == 409 and out["error"] == "origin_changed" and page.written() == []


@pytest.mark.parametrize(
    "values",
    [
        {"username": "u"},  # the password is the one field credentials needs
        {"username": "u", "password": CANARY, "otp": "1"},
        {"username": "u", "password": "a\nb"},
        {"username": "", "password": CANARY},
        "nope",
    ],
)
async def test_values_that_do_not_fit_the_ask_are_bad_action(values: Any) -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": values})
    assert (status, out) == (400, {"ok": False, "error": "bad_action"})
    assert page.written() == [] and not ask.outcome


async def test_size_caps_stale_ids_and_wrong_actions() -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    long = "x" * (qa.VALUE_MAX + 1)
    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"username": "u", "password": long}}
    )
    assert (status, out["error"]) == (413, "too_large")
    status, out, _ = await _answer({"ask_id": ask.id, "action": "decline", "pad": "x" * 9000})
    assert (status, out["error"]) == (413, "too_large")
    status, out, _ = await _answer({"ask_id": "nope", "action": "decline"})
    assert (status, out["error"]) == (409, "stale")
    status, out, _ = await _answer({"ask_id": ask.id, "action": "confirm"})
    assert (status, out["error"]) == (400, "bad_action")
    assert page.written() == [] and not ask.outcome


async def test_decline_then_the_ask_is_spent() -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    assert (await _answer({"ask_id": ask.id, "action": "decline"}))[1] == {
        "ok": True,
        "outcome": "declined",
    }
    assert (await _answer({"ask_id": ask.id, "action": "decline"}))[1]["error"] == "stale"


async def test_otp_takes_exactly_its_digits() -> None:
    page = FakePage()
    page.add("e7", type="text")
    _hub, _rec, ask = await _asked(
        page, {"kind": "otp", "targets": ["otp=e7"], "reason": "r", "digits": 6}
    )
    assert (await _answer({"ask_id": ask.id, "action": "fill", "values": {"otp": "12345"}}))[
        0
    ] == 400
    assert (await _answer({"ask_id": ask.id, "action": "fill", "values": {"otp": "١٢٣٤٥٦"}}))[
        0
    ] == 400
    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"otp": "123456"}}
    )
    assert (status, out.get("outcome")) == (200, "filled"), (status, out, page.calls[-6:])
    assert page.written() == ["123456"]


async def test_confirm_clicks_the_bound_button_exactly_once() -> None:
    page = FakePage()
    page.add("e2", tag="button", text="  Place   order ", rect=[100, 200, 80, 40])
    hub, _rec, ask = await _asked(
        page, {"kind": "confirm", "targets": ["action=e2"], "reason": "Buy", "summary": "Pay $35"}
    )
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["button"] == "Place order" and wire["summary"] == "Pay $35"
    status, out, _ = await _answer({"ask_id": ask.id, "action": "confirm"})
    assert (status, out) == (200, {"ok": True, "outcome": "confirmed"})
    assert [c["type"] for c in page.clicks()] == ["mousePressed", "mouseReleased"]
    assert (page.clicks()[0]["x"], page.clicks()[0]["y"]) == (140, 220)
    assert (await _answer({"ask_id": ask.id, "action": "confirm"}))[1]["error"] == "stale"
    assert len(page.clicks()) == 2


async def test_confirm_on_a_covered_button_clicks_nothing() -> None:
    page = FakePage()
    page.add("e2", tag="button", text="Buy")
    _hub, _rec, ask = await _asked(
        page, {"kind": "confirm", "targets": ["action=e2"], "reason": "Buy", "summary": "Pay"}
    )
    page.nodes[page.refs["e2"]]["covered"] = True
    assert (await _answer({"ask_id": ask.id, "action": "confirm"}))[1]["error"] == "origin_changed"
    assert page.clicks() == []


# ── human_check: the scoped takeover ────────────────────────────────────────


HUMAN = {"kind": "human_check", "targets": ["widget=e9"], "reason": "Prove you are human"}


async def test_human_check_takeover_is_scoped_to_the_widget_and_hands_back_on_success() -> None:
    page = FakePage()
    page.add("e9", tag="iframe", rect=[100, 100, 300, 80])
    hub, _rec, ask = await _asked(page, dict(HUMAN))
    # The page scrolled between the ask and Take over: the box is measured again.
    page.nodes[page.refs["e9"]]["rect"] = [100, 300, 300, 80]
    status, out, _ = await _answer({"ask_id": ask.id, "action": "take_over"})
    assert (status, out) == (200, {"ok": True})
    assert hub.in_control(KEY) and ask.phase == "taken_over"
    assert lh.live_event(hub.get(KEY))["ask"]["phase"] == "taken_over"  # type: ignore[index]
    before = len(page.clicks())
    old_spot = {"type": "mouse", "action": "click", "x": 150, "y": 120, "button": "left"}
    inside = {"type": "mouse", "action": "click", "x": 150, "y": 320, "button": "left"}
    assert await hub.input_event(KEY, old_spot) == 0
    assert await hub.input_event(KEY, {"type": "wheel", "x": 150, "y": 320, "dy": 300}) == 0
    for key in ("Tab", " ", "Enter", "PageDown", "a"):
        assert await hub.input_event(KEY, {"type": "key", "key": key}) == 0
    assert await hub.input_event(KEY, inside) == 2
    assert len(page.clicks()) == before + 2
    # A rung drop halves the frame: the same CSS box is now at half the frame pixels.
    hub.get(KEY).stream.scale = 0.5  # type: ignore[union-attr]
    assert await hub.input_event(KEY, inside) == 0
    assert await hub.input_event(KEY, {**inside, "x": 75, "y": 160}) == 2
    assert (await _poll(ask.id))["done"] is False
    page.passed = True
    assert await _poll(ask.id) == {"done": True, "outcome": "passed"}
    assert not hub.in_control(KEY) and hub.get(KEY).input_rect is None  # type: ignore[union-attr]


async def test_human_check_done_esc_and_timeout() -> None:
    page = FakePage()
    page.add("e9", tag="iframe", rect=[0, 0, 50, 50])
    hub, _rec, ask = await _asked(page, dict(HUMAN))
    assert (await _answer({"ask_id": ask.id, "action": "done"}))[0] == 400  # not taken over yet
    await _answer({"ask_id": ask.id, "action": "take_over"})
    assert (await _answer({"ask_id": ask.id, "action": "done"}))[1] == {
        "ok": True,
        "outcome": "passed",
    }
    assert not hub.in_control(KEY)
    await _poll(ask.id)

    # Esc hand-back instead of Done: cancelled.
    _, _, ask2 = await _asked_again(hub, page)
    await _answer({"ask_id": ask2.id, "action": "take_over"})
    await hub.release(KEY)
    assert (await _poll(ask2.id))["outcome"] == "cancelled"

    # Past its deadline: timed_out, and the scoped takeover is handed back.
    _, _, ask3 = await _asked_again(hub, page)
    await _answer({"ask_id": ask3.id, "action": "take_over"})
    ask3.deadline = 0.0
    assert (await _poll(ask3.id))["outcome"] == "timed_out"
    assert not hub.in_control(KEY)


async def _asked_again(hub: lh.LiveHub, page: FakePage) -> tuple[None, None, Any]:
    out = await browser_ask.run_ask(hub, KEY, dict(HUMAN))
    assert out["ok"] is True and "ask_id" in out, out
    return None, None, hub.get(KEY).ask  # type: ignore[union-attr]


async def test_human_check_page_moved_during_takeover_is_origin_changed() -> None:
    page = FakePage()
    page.add("e9", tag="iframe")
    hub, _rec, ask = await _asked(page, dict(HUMAN))
    await _answer({"ask_id": ask.id, "action": "take_over"})
    page.frame.update(url="https://other.example/", loaderId="L9")
    assert (await _poll(ask.id))["outcome"] == "origin_changed"
    assert not hub.in_control(KEY)


async def test_the_turn_ending_cancels_the_ask() -> None:
    page = FakePage()
    _login(page)
    hub, _rec, ask = await _asked(page, dict(CREDS))
    hub.turn_ended(KEY)
    assert ask.outcome == "cancelled"
    assert await _poll(ask.id) == {"done": True, "outcome": "cancelled"}


# ── what the agent can still read after a fill ──────────────────────────────


@pytest.mark.parametrize("asked, refused", [(1, True), (0, False)])
async def test_a_screenshot_is_refused_while_an_asked_field_holds_its_value(
    asked: int, refused: bool, tmp_path: Path
) -> None:
    from kiro_crew.browser_harness import ops

    calls: list[str] = []

    async def helper(name: str, **kw: Any) -> Any:
        calls.append(name)
        if name == "js":
            return asked if ".askedValues(" in kw["expression"] else None
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    out = await ops.run_op("screenshot", {}, ops.OpContext(helper=helper, screenshot_dir=tmp_path))
    assert (out.get("code") == "asked_value_on_page") is refused
    assert ("screenshot" in calls) is not refused


def test_the_walker_withholds_an_asked_field_and_the_mask_covers_it() -> None:
    from kiro_crew.browser_harness import ops, screencast

    assert 'hasAttribute("data-kc-asked")' in ops.WALKER_SOURCE
    assert "askedValues" in ops.WALKER_SOURCE
    assert "input[data-kc-asked]" in screencast._MASK_JS
    assert "setAttribute('data-kc-asked'" in qa._PREPARE_JS


# ── the shim ────────────────────────────────────────────────────────────────


def test_the_tool_parks_on_an_ask_and_returns_only_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool.mcp_core, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(tool.mcp_core, "_vet_browse_governance", lambda _s: None)
    monkeypatch.setattr(
        tool,
        "_post_command",
        lambda *a, **k: (200, {"ok": True, "result": {"ok": True, "ask_id": "a1"}}),
    )
    polls = iter([(200, {"done": False}), (200, {"done": True, "outcome": "filled"})])
    monkeypatch.setattr(tool, "_post_ask_poll", lambda *a, **k: next(polls))
    wait = tool.browser("browser", {"op": "ask", "args": {"kind": "credentials"}})
    assert isinstance(wait, tool._AskWait)
    assert wait.step() is None
    text = wait.step()
    assert text is not None and text.startswith("Browser ask: filled")


def test_a_refusal_and_an_unknown_outcome_read_as_words() -> None:
    assert tool._result_text(
        "ask", {"ok": True, "outcome": "refused", "code": "covered"}
    ).startswith("Browser ask: refused(covered)")
    assert "refused(invalid)" in tool._ask_text("refused", "<script>")


def test_the_tool_description_routes_secrets_to_ask() -> None:
    desc = tool.schemas()[0]["description"]
    assert "ask {kind" in desc and "never solve" in desc and "untrusted" in desc
    assert "ask" in tool.BROWSER_OPS
