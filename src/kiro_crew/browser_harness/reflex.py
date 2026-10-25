"""The ``reflex`` op: a fast observe -> judge -> act loop inside ONE browser op.

The agent names a goal and a CLOSED set of actions (keys to press, refs to click,
ref pairs to drag, values to type into a ref). The gateway then loops with no LLM
turn in between: each step reads the page (a grid of cells the agent named by CSS
selector, or the outline, plus a masked frame when ``see`` is on), asks the
decision seam ONE request -- a Choice over the actions and the done / stuck /
off-path Nouls, plus a risk Noul for each action not yet judged -- and runs the
picked action through the ordinary op handlers. The model only judges; the stop
rules, the no-op withdrawal, the budget, the terminal checks and every safety gate
are code here.

Off is free: without an armed ``browser.step`` point (no provider, no consent,
governance denied), or when the first decision fails, the op answers
``not_available`` before it has touched the page, and the agent carries on with
single ops. No other op's path changes.

Fences are the ones every op has: the route's gateway lock, the per-session tab
and the claim fence (a Take over cancels the op task); the loop also checks
``in_control`` before each action. The landed URL after every action goes through
the navigate gate. A text value must appear verbatim in the goal, a fact or the
page, so the model never writes text. An action judged hard to undo stops the loop
and hands it to the person. Every action is one step label on the live card.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.browser_harness import decide as _decide
from kiro_crew.browser_harness import ops as _ops

logger = logging.getLogger(__name__)

#: Caps on what one call may ask for. ``max_seconds`` stays inside the route's slow
#: op budget, so a loop always ends with its own summary, never a route timeout.
MAX_ACTIONS = 40
MAX_STEPS = 300
DEFAULT_STEPS = 60
MAX_SECONDS = 45.0
DEFAULT_SECONDS = 20.0
DEFAULT_SETTLE_MS = 120
MAX_SETTLE_MS = 1000
#: Risk questions asked per request; the rest are asked on later steps.
RISK_PER_REQUEST = 8
#: A risk Noul at or above this stops the loop before the action runs.
RISK_STOP = 0.5
#: The outline and the grid sent as state are cut to these sizes.
STATE_OUTLINE_CAP = 6000
GRID_CELL_CAP = 64
#: Moves kept in the result.
RECENT_MOVES = 20
_FACT_CAP = 500
_SELECTOR_CAP = 200

STOP_DONE = "done"
STOP_STUCK = "stuck"
STOP_OFF_PATH = "off_path"
STOP_RISK = "risk"
STOP_UNTIL = "until"
STOP_BUDGET = "budget"
STOP_USER = "user_in_control"
STOP_NO_ACTIONS = "no_actions_left"
STOP_DECISION = "decision_unavailable"
STOP_REFUSED = "redirect_refused"

#: Keys that only move focus, scroll or steer and never submit anything: judged
#: safe by code, so no risk question is spent on them (Enter, Space and
#: characters are still asked about, since they can press or submit).
SAFE_KEYS = frozenset(
    {
        "ArrowLeft",
        "ArrowUp",
        "ArrowRight",
        "ArrowDown",
        "Tab",
        "Escape",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

_SAFE_ID = re.compile(r"[^A-Za-z0-9_]+")


def not_available(why: str) -> dict[str, Any]:
    return _ops.refusal(
        "not_available",
        f"reflex is not available ({why}); drive the page with single ops instead",
    )


@dataclass
class Action:
    """One closed-set action: its option name, its label, and the op that runs it."""

    id: str
    op: str
    args: dict[str, Any]
    label: str
    risky: float | None = None


@dataclass
class Plan:
    goal: str
    facts: list[str]
    actions: list[Action]
    grid: str | None
    grid_items: str | None
    see: bool
    until_text: str | None
    until_url: str | None
    max_steps: int
    max_seconds: float
    settle_s: float


def _text(value: Any, cap: int) -> str | None:
    return value.strip()[:cap] if isinstance(value, str) and value.strip() else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _int(value: Any, default: int, low: int, high: int) -> int:
    number = _ops._number(value)
    return default if number is None else max(low, min(high, int(number)))


def _option_id(raw: str, taken: set[str]) -> str:
    base = _SAFE_ID.sub("_", raw).strip("_")[:48] or "action"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def parse(args: dict[str, Any]) -> Plan | dict[str, Any]:
    """The call's plan, or a refusal. Checks shapes only; values are checked later."""
    goal = _text(args.get("goal"), 500)
    if goal is None:
        return _ops.refusal("bad_goal", "reflex needs a `goal` string")
    taken: set[str] = set()
    actions: list[Action] = []
    for key in _strings(args.get("keys")):
        if key == "Space":
            key = " "
        if key not in _ops._NAMED_KEYS and len(key) != 1:
            return _ops.refusal("bad_key", f"unknown key in `keys`: {key!r}")
        name = "Space" if key == " " else key
        actions.append(
            Action(
                _option_id(f"press_{name}", taken),
                "press_key",
                {"key": key},
                f"Press {name}",
                risky=0.0 if key in SAFE_KEYS else None,
            )
        )
    for ref in _strings(args.get("click")):
        if not _ops._is_ref(ref):
            return _ops.refusal("bad_ref", f"not a valid ref in `click`: {ref!r}")
        actions.append(
            Action(_option_id(f"click_{ref}", taken), "click", {"ref": ref}, f"Click {ref}")
        )
    for pair in _strings(args.get("drag")):
        src, _, dst = pair.partition(">")
        src, dst = src.strip(), dst.strip()
        if not (_ops._is_ref(src) and _ops._is_ref(dst)):
            return _ops.refusal("bad_ref", f"`drag` entries are 'eA>eB', got {pair!r}")
        actions.append(
            Action(
                _option_id(f"drag_{src}_to_{dst}", taken),
                "drag",
                {"ref": src, "to_ref": dst},
                f"Drag {src} to {dst}",
            )
        )
    for entry in _strings(args.get("type")):
        ref, sep, value = entry.partition("=")
        ref = ref.strip()
        if not sep or not _ops._is_ref(ref) or not value:
            return _ops.refusal("bad_value", f"`type` entries are 'eN=value', got {entry[:60]!r}")
        actions.append(
            Action(
                _option_id(f"type_{ref}", taken),
                "type",
                {"ref": ref, "text": value},
                f"Type into {ref}",
            )
        )
    if not actions:
        return _ops.refusal("no_actions", "reflex needs at least one of: keys, click, drag, type")
    if len(actions) > MAX_ACTIONS:
        return _ops.refusal("too_many_actions", f"reflex takes at most {MAX_ACTIONS} actions")
    seconds = _ops._number(args.get("max_seconds"))
    return Plan(
        goal=goal,
        facts=[f[:_FACT_CAP] for f in _strings(args.get("facts"))][:20],
        actions=actions,
        grid=_text(args.get("grid"), _SELECTOR_CAP),
        grid_items=_text(args.get("grid_items"), _SELECTOR_CAP),
        see=args.get("see") is True,
        until_text=_text(args.get("until_text"), 200),
        until_url=_text(args.get("until_url"), 500),
        max_steps=_int(args.get("max_steps"), DEFAULT_STEPS, 1, MAX_STEPS),
        max_seconds=DEFAULT_SECONDS if seconds is None else max(1.0, min(MAX_SECONDS, seconds)),
        settle_s=_int(args.get("settle_ms"), DEFAULT_SETTLE_MS, 0, MAX_SETTLE_MS) / 1000.0,
    )


def values_sourced(plan: Plan, page_text: str) -> str | None:
    """The first ``type`` value found in neither the goal, a fact nor the page, or ``None``."""
    sources = [plan.goal, *plan.facts, page_text]
    for action in plan.actions:
        if action.op == "type":
            value = str(action.args["text"])
            if not any(value in source for source in sources):
                return value
    return None


def arrange(slots: list[dict[str, Any]], items: list[dict[str, Any]] | None) -> list[list[str]]:
    """Slots (cells) as rows by their centres; with *items*, each slot takes the
    highest-numbered (else last) item whose centre is nearest to it."""
    if not slots:
        return []
    values = [str(s.get("t") or "") for s in slots]
    if items is not None:
        values = [""] * len(slots)
        for item in items:
            try:
                ix, iy = float(item["x"]), float(item["y"])
            except (KeyError, TypeError, ValueError):
                continue
            best = min(
                range(len(slots)),
                key=lambda i: (float(slots[i]["x"]) - ix) ** 2 + (float(slots[i]["y"]) - iy) ** 2,
            )
            text = str(item.get("t") or "")
            values[best] = _higher(values[best], text)
    rows: list[list[tuple[float, str]]] = []
    tops: list[float] = []
    order = sorted(range(len(slots)), key=lambda i: (float(slots[i]["y"]), float(slots[i]["x"])))
    for i in order:
        y = float(slots[i]["y"])
        if tops and abs(y - tops[-1]) < 4.0:
            rows[-1].append((float(slots[i]["x"]), values[i]))
        else:
            tops.append(y)
            rows.append([(float(slots[i]["x"]), values[i])])
    return [[v[:16] for _x, v in sorted(row)][:GRID_CELL_CAP] for row in rows][:GRID_CELL_CAP]


def _higher(a: str, b: str) -> str:
    """Two tiles in one slot (a merge still animating): the larger number wins."""
    if not a:
        return b
    try:
        return a if float(a) >= float(b) else b
    except ValueError:
        return b or a


async def _cells(ctx: _ops.OpContext, selector: str) -> list[dict[str, Any]] | None:
    found = await _ops._page(ctx, "cells", selector)
    if (
        not isinstance(found, dict)
        or not found.get("ok")
        or not isinstance(found.get("cells"), list)
    ):
        return None
    return [c for c in found["cells"] if isinstance(c, dict)]


class _Loop:
    def __init__(self, ctx: _ops.OpContext, plan: Plan, session_key: str, image: bool) -> None:
        self.ctx = ctx
        self.plan = plan
        self.session_key = session_key
        self.image = image
        self.moves: list[str] = []
        self.counts: dict[str, int] = {}
        self.decisions = 0
        self.decision_ms = 0
        self.dropped: set[str] = set()
        self.step_no = 0
        self.started = time.monotonic()

    # -- observation ------------------------------------------------------------

    async def observe(self) -> tuple[dict[str, Any], str, bytes | None] | None:
        """``(state, fingerprint, frame)``: what the model reads, what no-op detection
        compares, and the masked frame when ``see`` is on and one may be sent now."""
        plan = self.plan
        state: dict[str, Any] = {"goal": plan.goal}
        if plan.facts:
            state["facts"] = plan.facts
        if plan.grid:
            slots = await _cells(self.ctx, plan.grid)
            items = await _cells(self.ctx, plan.grid_items) if plan.grid_items else None
            if slots is None or (plan.grid_items and items is None):
                return None
            grid = arrange(slots, items)
            state["grid"] = grid
            fingerprint = repr(grid)
        else:
            snap = await _decide._snapshot(self.ctx)
            if snap is None:
                return None
            outline, title, _nodes = snap
            state["page_title"] = title
            state["page"] = outline[:STATE_OUTLINE_CAP]
            fingerprint = outline
        frame = None
        if self.image:
            # The masked-frame rules of every screenshot the seam sends: none while a
            # password box is on the page or the person holds the browser. On a
            # canvas the outline never changes, so the frame is the fingerprint too.
            frame = await _decide._capture(self.ctx)
            if frame is not None:
                fingerprint += hashlib.sha256(frame).hexdigest()
        return state, fingerprint, frame

    # -- one decision -----------------------------------------------------------

    async def decide(
        self, state: dict[str, Any], live: list[Action], frame: bytes | None
    ) -> dict[str, Any] | None:
        unjudged = [a for a in live if a.risky is None][:RISK_PER_REQUEST]
        # Only an action with a verdict, or one judged in this very request, may be
        # picked: the rest wait for a later request and never run unjudged.
        offered = [a for a in live if a.risky is not None or a in unjudged]
        started = time.monotonic()
        verdict = await _decide.reflex_step(
            self.session_key,
            state,
            {a.id: a.label for a in offered},
            [a.label for a in unjudged],
            subject="grid" if self.plan.grid else "page",
            frame=frame,
            step=self.step_no,
        )
        self.decision_ms += int((time.monotonic() - started) * 1000)
        if verdict is None:
            return None
        self.decisions += 1
        for action, risk in zip(unjudged, verdict["risk"]):
            action.risky = risk
        return verdict

    # -- acting -----------------------------------------------------------------

    async def act(self, action: Action) -> dict[str, Any]:
        self.step_no += 1
        rate = self.rate()
        prefix = f"Reflex {self.step_no}: "
        suffix = f" ({rate:.1f}/s)" if rate else ""
        outer = self.ctx

        def _step(label: str) -> Any:
            return outer.step(f"{prefix}{label}{suffix}")

        inner = dataclasses.replace(outer, step=_step, decisions=None)
        handler = _ops._HANDLERS[action.op]
        mark = outer.mark()
        result = await handler(inner, dict(action.args))
        # Each action is its own step on the turn replay's timeline.
        outer.record(action.op, dict(action.args), result, mark)
        if result.get("ok"):
            self.moves.append(action.label)
            self.counts[action.label] = self.counts.get(action.label, 0) + 1
        self.report(None)
        return result

    def report(self, stop: str | None) -> None:
        """The live card's loop line: counters so far, and ``stop`` once it ended."""
        elapsed = time.monotonic() - self.started
        self.ctx.loop(
            {
                "moves": len(self.moves),
                "rate": round(len(self.moves) / elapsed, 2) if elapsed > 0 else 0.0,
                "steps": self.step_no,
                "max_steps": self.plan.max_steps,
                "seconds": round(elapsed, 2),
                "max_seconds": self.plan.max_seconds,
                "stop": stop,
            }
        )

    def rate(self) -> float:
        elapsed = time.monotonic() - self.started
        return len(self.moves) / elapsed if elapsed > 0 and self.moves else 0.0

    async def landed_refusal(self) -> dict[str, Any] | None:
        try:
            state = await _ops._page(self.ctx, "state")
        except Exception:  # noqa: BLE001 - mid-navigation; the next read judges it
            return None
        href = str(state.get("href", "")) if isinstance(state, dict) else ""
        if href and href != "about:blank":
            return await _ops._refuse_committed_url(self.ctx, href)
        return None

    async def until(self) -> bool:
        plan = self.plan
        if plan.until_text and await _ops._page(self.ctx, "textPresent", plan.until_text):
            return True
        if plan.until_url:
            state = await _ops._page(self.ctx, "state")
            if isinstance(state, dict) and plan.until_url in str(state.get("href", "")):
                return True
        return False

    def summary(self, stop: str, state: dict[str, Any] | None, **more: Any) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        out: dict[str, Any] = {
            "ok": True,
            "stop": stop,
            "steps": self.step_no,
            "moves": len(self.moves),
            "recent": self.moves[-RECENT_MOVES:],
            "actions": dict(sorted(self.counts.items())),
            "decisions": self.decisions,
            "decision_ms_avg": round(self.decision_ms / self.decisions) if self.decisions else 0,
            "seconds": round(elapsed, 2),
            "rate": round(len(self.moves) / elapsed, 2) if elapsed > 0 else 0.0,
            "see": self.image,
        }
        if state is not None:
            out["state"] = (
                state.get("grid") if "grid" in state else str(state.get("page", ""))[:1500]
            )
        out.update(more)
        return out

    async def finish(self, stop: str, state: dict[str, Any] | None, **more: Any) -> dict[str, Any]:
        result = self.summary(stop, state, **more)
        await _ops._step(
            self.ctx, f"Reflex {stop}: {result['moves']} moves, {result['rate']:.1f}/s"
        )
        self.report(stop)
        return result

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        plan = self.plan
        deadline = self.started + plan.max_seconds
        last_fp: str | None = None
        last_pick: str | None = None
        state: dict[str, Any] | None = None
        self.report(None)
        while True:
            if await _ops._in_control(self.ctx):
                return await self.finish(STOP_USER, state)
            # The URL gate runs before the page is read or sent anywhere, so a redirect
            # that landed after the last action (or a takeover navigation) is caught
            # before its content reaches the decision provider or a terminal verdict.
            refused = await self.landed_refusal()
            if refused is not None:
                return await self.finish(STOP_REFUSED, None, error=refused["error"])
            seen = await self.observe()
            if seen is None:
                if self.decisions == 0:
                    self.report(STOP_DECISION)
                    return not_available("the page state could not be read")
                return await self.finish(STOP_DECISION, state, why="page state unreadable")
            state, fp, frame = seen
            # No-op withdrawal: an action that left the state as it was is not
            # offered again until the state changes.
            if last_fp is not None:
                if fp == last_fp and last_pick is not None:
                    self.dropped.add(last_pick)
                elif fp != last_fp:
                    self.dropped.clear()
            last_fp = fp
            if (plan.until_text or plan.until_url) and await self.until():
                return await self.finish(STOP_UNTIL, state)
            if self.step_no >= plan.max_steps or time.monotonic() >= deadline:
                return await self.finish(STOP_BUDGET, state)
            live = [
                a for a in plan.actions if a.id not in self.dropped and (a.risky or 0.0) < RISK_STOP
            ]
            if not live:
                risky = [a.label for a in plan.actions if (a.risky or 0.0) >= RISK_STOP]
                if risky and len(risky) == len(plan.actions):
                    return await self.finish(STOP_RISK, state, handoff=_handoff(risky[0]))
                return await self.finish(STOP_NO_ACTIONS, state)
            verdict = await self.decide(state, live, frame)
            if verdict is None:
                if self.decisions == 0:
                    self.report(STOP_DECISION)
                    return not_available("no decision model answered")
                return await self.finish(STOP_DECISION, state)
            if verdict["done"] > _decide.YES:
                return await self.finish(STOP_DONE, state)
            if verdict["stuck"] > _decide.YES:
                return await self.finish(STOP_STUCK, state)
            if verdict["off_path"] > _decide.YES:
                return await self.finish(STOP_OFF_PATH, state)
            action = next(
                (a for a in live if a.id == verdict["pick"] and a.risky is not None), None
            )
            if action is None:
                return await self.finish(STOP_DECISION, state, why="no action picked")
            if (action.risky or 0.0) >= RISK_STOP:
                return await self.finish(STOP_RISK, state, handoff=_handoff(action.label))
            if await _ops._in_control(self.ctx):
                return await self.finish(STOP_USER, state)
            result = await self.act(action)
            last_pick = action.id
            if not result.get("ok"):
                if result.get("code") == "password_field":
                    return await self.finish(STOP_RISK, state, handoff=result["error"])
                # A stale ref or an unfocusable field: withdraw it and go on.
                self.dropped.add(action.id)
                last_pick = None
            if plan.settle_s:
                await self.ctx.sleep(plan.settle_s)


def _handoff(label: str) -> str:
    return (
        f"'{label}' looks hard to undo, so the loop stopped before it. Ask the user to "
        "take over the browser or to confirm, then do that step with a single op."
    )


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """The ``reflex`` op. See the module docstring."""
    plan = parse(args)
    if isinstance(plan, dict):
        return plan
    hooks = ctx.decisions
    session_key = getattr(hooks, "session_key", None)
    if not isinstance(session_key, str) or not session_key:
        return not_available("browser help is off")
    try:
        armed = await asyncio.wait_for(
            asyncio.to_thread(_decide._probe, session_key), timeout=_decide.PRE_OP_BUDGET_S
        )
    except asyncio.TimeoutError:
        return not_available("browser help did not answer")
    if not armed.text:
        return not_available("browser help is off")
    if any(a.op == "type" for a in plan.actions):
        snap = await _decide._snapshot(ctx)
        page_text = snap[0] if snap is not None else ""
        missing = values_sourced(plan, page_text)
        if missing is not None:
            return _ops.refusal(
                "bad_value",
                f"the value {missing[:60]!r} is not in the goal, the facts or the page; "
                "reflex only types values it can point to",
            )
    image = plan.see and armed.image
    return await _Loop(ctx, plan, session_key, image).run()
