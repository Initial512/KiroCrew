"""Quick asks in the gateway: the ``ask`` op, its parked poll, and the person's answer.

Three legs, three trust classes:

* :func:`run_ask` -- the ``browser`` op ``ask``, called from ``/api/browser/op``
  (internal secret) under the op lock and the session's fence, after the tab is
  bound and its stream is running. It binds the ask to the page
  (``browser_harness.quick_ask.bind_ask``) and puts it on the card.
* ``POST /api/browser/ask/poll`` (internal secret, strict path) -- the parked
  tool's poll. Answers ``{done, outcome}``: an outcome WORD, never a value.
* ``POST /api/browser/ask/answer`` (dashboard OWNER only, the internal secret
  refused, like the takeover routes) -- the card's answer. Body capped at
  ``ANSWER_BODY_MAX``, each value at ``VALUE_MAX``. A fill re-checks the binding
  and writes in the gateway; the values go nowhere else: not to a log, the SEL,
  the replay, the card's state or the agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.browser_harness.budgets import ASK_WAIT_S
from kiro_crew.browser_harness.live_hub import LiveHub, get_live_hub
from kiro_crew.browser_harness.takeover import USER
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)

logger = logging.getLogger(__name__)


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def _audit(operation: str, outcome: str, detail: str = "") -> None:
    """SEL row: the operation and an outcome WORD. Never a value or page text."""
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome=outcome,
            source="browser",
            resources="/api/browser/ask",
            error=detail or None,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the request
        logger.debug("quick ask audit failed")


def _record(hub: LiveHub, key: str, ask: qa.QuickAsk, action: str, outcome: str) -> None:
    """The replay's ``person`` event: kind, action, outcome, origin. Nothing typed."""
    rec = hub.recorder
    if rec is None:
        return
    from kiro_crew.browser_harness.recording import now_ms

    try:
        rec.event(
            key,
            {
                "ts": now_ms(),
                "op": "other",
                "label": f"{ask.spec.kind}: {action} -> {outcome}",
                "args": {},
                "point": None,
                "path": None,
                "url": ask.origin,
                "status": "ok",
                "actor": "person",
                "ask": {
                    "kind": ask.spec.kind,
                    "action": action,
                    "outcome": outcome,
                    "origin": ask.origin,
                },
            },
        )
    except Exception:  # noqa: BLE001 - a replay event never fails the ask
        logger.debug("quick ask replay event not recorded")


def _refused(code: str) -> dict[str, Any]:
    return {"ok": True, "outcome": qa.REFUSED, "code": code}


async def run_ask(hub: LiveHub, key: str, args: dict[str, Any]) -> dict[str, Any]:
    """The ``ask`` op's result: ``{ok, ask_id, expires_at}``, or ``{ok, outcome: refused, code}``."""
    try:
        spec = qa.parse_ask(args)
    except qa.AskError as exc:
        return _refused(exc.code)
    sess = hub.get(key)
    if sess is not None and sess.ask is not None and not sess.ask.outcome:
        return _refused("busy")
    stream = sess.stream if sess is not None else None
    if stream is None or not stream.session_id or stream.target_id != hub.target_of(key):
        # No live card to answer on: the ask would wait ten minutes for nothing.
        return _refused("no_live_view")
    probe = qa.PageProbe(stream.relay, stream.session_id)
    try:
        ask = await qa.bind_ask(probe, spec, float(getattr(stream, "scale", 1.0) or 1.0))
    except qa.AskError as exc:
        _audit("browser_ask", "refused", exc.code)
        return _refused(exc.code)
    ask.start_clock(asyncio.get_running_loop().time())
    assert sess is not None
    sess.ask = ask
    _record(hub, key, ask, "ask", "pending")
    _audit("browser_ask", "pending", spec.kind)
    await hub.ask_changed(key)
    return {"ok": True, "ask_id": ask.id, "expires_at": ask.expires_at}


async def _finish(hub: LiveHub, key: str, ask: qa.QuickAsk, action: str, outcome: str) -> None:
    """Settle *ask* once: hand back its scoped takeover, record, audit, publish."""
    if not ask.settle(outcome):
        return
    sess = hub.get(key)
    if ask.claimed and sess is not None and sess.lock.held() and sess.lock.owner == USER:
        await hub.release(key)
    _record(hub, key, ask, action, outcome)
    _audit("browser_ask_answer", outcome, f"{ask.spec.kind}:{action}")
    await hub.ask_changed(key)


async def _tick(hub: LiveHub, key: str, ask: qa.QuickAsk) -> None:
    """Lazy upkeep on every poll: the deadline, and a human_check's takeover."""
    if ask.outcome:
        return
    if asyncio.get_running_loop().time() >= ask.deadline:
        await _finish(hub, key, ask, "timeout", qa.TIMED_OUT)
        return
    if ask.spec.kind != "human_check" or ask.phase != qa.PHASE_TAKEN_OVER:
        return
    sess = hub.get(key)
    if sess is None or not sess.lock.held():
        # Handed back with Esc instead of Done: the person stopped.
        await _finish(hub, key, ask, "hand_back", qa.CANCELLED)
        return
    stream = sess.stream
    if stream is None or not stream.session_id:
        return
    try:
        passed = await qa.human_check_passed(qa.PageProbe(stream.relay, stream.session_id), ask)
    except qa.AskError:
        await _finish(hub, key, ask, "page_moved", qa.ORIGIN_CHANGED)
        return
    if passed:
        await _finish(hub, key, ask, "auto", qa.PASSED)


async def api_browser_ask_poll(request: web.Request) -> web.Response:
    """POST /api/browser/ask/poll -- ``{session_key, ask_id}`` -> ``{done, outcome?}``."""
    from kiro_crew.dashboard.handlers.browser_ops import live_session_key

    if request.get("internal_auth") is not True:
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("session_key"), str):
        return web.json_response({"error": "session_key required"}, status=400)
    hub = get_live_hub()
    key = live_session_key(body["session_key"])
    sess = hub.get(key)
    ask = sess.ask if sess is not None else None
    if ask is None or ask.id != body.get("ask_id"):
        # Ended with the turn (or replaced): nobody answered it.
        return web.json_response({"done": True, "outcome": qa.CANCELLED})
    await _tick(hub, key, ask)
    if not ask.outcome:
        try:
            await asyncio.wait_for(ask.settled.wait(), timeout=ASK_WAIT_S)
        except asyncio.TimeoutError:
            pass
        await _tick(hub, key, ask)
    if not ask.outcome:
        return web.json_response({"done": False})
    if sess is not None and sess.ask is ask:
        sess.ask = None
    return web.json_response({"done": True, "outcome": ask.outcome})


def _answer(ok: bool, status: int = 200, **fields: Any) -> web.Response:
    return web.json_response({"ok": ok, **fields}, status=status)


async def api_browser_ask_answer(request: web.Request) -> web.Response:
    """POST /api/browser/ask/answer -- the person's answer from the card."""
    from kiro_crew.dashboard.handlers.browser_live import _owned_session_key

    if request.get("internal_auth") is True:
        _audit("browser_ask_answer", "denied", "internal secret refused")
        return _answer(False, 403, error="not_owner", code="internal_secret_required")
    refusal = await require_owner_dashboard_request(request, "browser_ask_answer")
    if refusal is not None:
        return _answer(False, 403, error="not_owner", code="owner_only")
    body, err = await read_bounded_json(request, max_bytes=qa.ANSWER_BODY_MAX)
    if err is not None or body is None:
        if err is not None and err.status == 413:
            return _answer(False, 413, error="too_large")
        return _answer(False, 400, error="bad_action")
    key = _owned_session_key(request, body)
    hub = get_live_hub()
    sess = hub.get(key) if key else None
    ask = sess.ask if sess is not None else None
    if key is None or ask is None or ask.outcome or ask.id != body.get("ask_id"):
        return _answer(False, 409, error="stale")
    action = body.get("action")
    if action not in qa.ACTIONS[ask.spec.kind]:
        return _answer(False, 400, error="bad_action")
    values: dict[str, str] | None = None
    if action == "fill":
        raw = body.get("values")
        if isinstance(raw, dict) and any(
            isinstance(v, str) and len(v) > qa.VALUE_MAX for v in raw.values()
        ):
            return _answer(False, 413, error="too_large")
        values = qa.check_values(ask, raw)
        if values is None:
            return _answer(False, 400, error="bad_action")
    async with ask.lock:
        if not ask.outcome and asyncio.get_running_loop().time() >= ask.deadline:
            await _finish(hub, key, ask, "timeout", qa.TIMED_OUT)
        if ask.outcome:
            return _answer(False, 409, error="stale")
        return await _act(hub, key, ask, str(action), values)


async def _act(
    hub: LiveHub, key: str, ask: qa.QuickAsk, action: str, values: dict[str, str] | None
) -> web.Response:
    if action == "decline":
        await _finish(hub, key, ask, action, qa.DECLINED)
        return _answer(True, outcome=qa.DECLINED)
    if action == "cancel":
        await _finish(hub, key, ask, action, qa.CANCELLED)
        return _answer(True, outcome=qa.CANCELLED)
    sess = hub.get(key)
    stream = sess.stream if sess is not None else None
    if sess is None or stream is None or not stream.session_id:
        await _finish(hub, key, ask, action, qa.ORIGIN_CHANGED)
        return _answer(False, 409, error=qa.ORIGIN_CHANGED)
    probe = qa.PageProbe(stream.relay, stream.session_id)
    if action == "done":
        if ask.phase != qa.PHASE_TAKEN_OVER:
            return _answer(False, 400, error="bad_action")
        # The person's word: the widget may live in a frame we cannot read.
        await _finish(hub, key, ask, action, qa.PASSED)
        return _answer(True, outcome=qa.PASSED)
    if action == "take_over":
        return await _take_over(hub, key, ask, probe)
    try:
        async with hub.op_fence(key):
            if action == "fill":
                assert values is not None
                await qa.fill(probe, ask, values)
                outcome = qa.FILLED
            else:  # confirm: exactly one click, then the ask is spent
                await qa.click_once(probe, ask)
                outcome = qa.CONFIRMED
    except qa.AskError:
        # Nothing about why rides on the answer: the page moved, or would not take it.
        await _finish(hub, key, ask, action, qa.ORIGIN_CHANGED)
        return _answer(False, 409, error=qa.ORIGIN_CHANGED)
    await _finish(hub, key, ask, action, outcome)
    return _answer(True, outcome=outcome)


async def _take_over(hub: LiveHub, key: str, ask: qa.QuickAsk, probe: qa.PageProbe) -> web.Response:
    """human_check: the person gets the widget's box, and nothing else, for its timeout."""
    if ask.phase == qa.PHASE_TAKEN_OVER:
        return _answer(True)
    try:
        async with hub.op_fence(key):
            await qa.verify(probe, ask)
    except qa.AskError:
        await _finish(hub, key, ask, "take_over", qa.ORIGIN_CHANGED)
        return _answer(False, 409, error=qa.ORIGIN_CHANGED)
    sess = hub.get(key)
    if sess is None:
        return _answer(False, 409, error="stale")
    held_already = sess.lock.held()
    if not held_already:
        # Scoped BEFORE the claim publishes in_control, so no card event can land
        # outside the box. Only a takeover this ask makes is scoped; one the person
        # already had stays theirs.
        sess.input_rect = ask.widget_rect()
    try:
        claimed = await hub.claim(key)
    except LookupError:
        claimed = False
    if not claimed:
        if not held_already:
            sess.input_rect = None
        return _answer(False, 409, error="stale")
    ask.claimed = not held_already
    ask.phase = qa.PHASE_TAKEN_OVER
    _record(hub, key, ask, "take_over", "taken_over")
    await hub.ask_changed(key)
    return _answer(True)


__all__ = ["api_browser_ask_answer", "api_browser_ask_poll", "run_ask"]
