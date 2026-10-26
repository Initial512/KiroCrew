"""Quick asks: the ``browser`` op ``ask``, answered by the person on the live card.

The agent asks the person for one thing it must never handle itself -- a
sign-in, one short text field, a one-time code, a "not a robot" check, or the
click that buys something -- and gets back ONE non-secret outcome word.

The invariant everything here serves: a value the person types reaches only the
element the ask was bound to, on the origin it was bound to, and nothing the
agent can read ever contains it. So:

* The ask is BOUND when it is made: the page's committed top frame (id, loader,
  origin from the frame URL) and each target's ``backendNodeId``. A target in
  another document, hidden, covered or of the wrong type is refused right away.
* Every DOM read runs in an isolated world (``Page.createIsolatedWorld``), so a
  page script cannot patch ``getBoundingClientRect`` or ``elementFromPoint`` to
  fake a check. The ref -> node lookup is the only main-world read, and what it
  returns is checked in the isolated world like any other node.
* The answer is RE-CHECKED before anything is written: same frame, same
  document, same origin, every bound node still attached, visible, uncovered and
  of its type. Anything else is ``origin_changed`` and nothing is written.
* A fill is ``DOM.focus`` on the bound node then ``Input.insertText``, in the
  gateway. Values are never logged, stored, published or put in an exception.

Pure validation (:func:`parse_ask`) and the CDP half (:class:`PageProbe`,
:func:`bind_ask`, :func:`verify`, :func:`fill`, :func:`click_once`,
:func:`human_check_passed`) live together so the contract reads in one place.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.browser_harness.budgets import ASK_TIMEOUT_S, HUMAN_CHECK_TIMEOUT_S

KINDS = ("credentials", "text", "otp", "human_check", "confirm")
#: The target roles each kind takes, and which of them are required.
ROLES: dict[str, tuple[tuple[str, bool], ...]] = {
    "credentials": (("username", False), ("password", True)),
    "text": (("text", True),),
    "otp": (("otp", True),),
    "human_check": (("widget", True),),
    "confirm": (("action", True),),
}
TEXT_LABELS = frozenset({"name", "email", "username", "phone", "other"})
#: The answer actions each kind accepts ("cancel" and "decline" fit every kind).
ACTIONS: dict[str, frozenset[str]] = {
    "credentials": frozenset({"fill", "decline", "cancel"}),
    "text": frozenset({"fill", "decline", "cancel"}),
    "otp": frozenset({"fill", "decline", "cancel"}),
    "human_check": frozenset({"take_over", "done", "decline", "cancel"}),
    "confirm": frozenset({"confirm", "decline", "cancel"}),
}
ALL_ACTIONS = frozenset().union(*ACTIONS.values())

FILLED = "filled"
DECLINED = "declined"
PASSED = "passed"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"
ORIGIN_CHANGED = "origin_changed"
TIMED_OUT = "timed_out"
REFUSED = "refused"

REASON_MAX = 80
SUMMARY_MAX = 120
BUTTON_TEXT_MAX = 60
#: The largest human_check widget, in CSS pixels: a challenge pane (reCAPTCHA's is
#: about 400 x 580), never a whole page.
WIDGET_MAX_W = 520
WIDGET_MAX_H = 720
OTP_DIGITS_MIN = 4
OTP_DIGITS_MAX = 10
#: The answer route's caps.
ANSWER_BODY_MAX = 8 * 1024
VALUE_MAX = 1024

PHASE_WAITING = "waiting"
PHASE_TAKEN_OVER = "taken_over"

_REF_RE = re.compile(r"^e[1-9][0-9]{0,6}$")
#: C0/C1 controls, and the zero-width and bidi-override characters that let a
#: reason read differently from what it says.
_UNSAFE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACE_RE = re.compile(r"\s+")
#: Editable text input types for ``text``/``otp``/``username`` targets.
_TEXT_TYPES = frozenset({"", "text", "email", "tel", "number", "search", "url"})


class AskError(Exception):
    """An ask refused, or an answer that could not land. ``code`` is the whole message:
    no page text and no value ever rides on it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_text(raw: Any, limit: int) -> str:
    """Plain one-line text: unsafe characters dropped, whitespace folded, cut to *limit*."""
    if not isinstance(raw, str):
        return ""
    # Whitespace (newlines and tabs included) folds to a space first, so dropping
    # the unsafe characters after it never glues two words together.
    text = _SPACE_RE.sub(" ", _UNSAFE_RE.sub("", _SPACE_RE.sub(" ", raw))).strip()
    return text[:limit].rstrip()


@dataclass(frozen=True)
class AskSpec:
    kind: str
    targets: dict[str, str]
    reason: str
    label: str = ""
    digits: int = 0
    summary: str = ""


def _targets(raw: Any, kind: str) -> dict[str, str]:
    """``targets``: a list of ``"role=ref"`` strings (the tool's args are flat), or an object."""
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                raise AskError("bad_targets")
            role, _, ref = item.partition("=")
            pairs.append((role.strip(), ref.strip()))
    else:
        raise AskError("bad_targets")
    allowed = dict(ROLES[kind])
    out: dict[str, str] = {}
    for role, ref in pairs:
        if role not in allowed or role in out:
            raise AskError("bad_targets")
        if not isinstance(ref, str) or not _REF_RE.match(ref):
            raise AskError("bad_ref")
        out[role] = ref
    for role, required in ROLES[kind]:
        if required and role not in out:
            raise AskError("bad_targets")
    if len(set(out.values())) != len(out):
        raise AskError("bad_targets")
    return out


def parse_ask(args: Any) -> AskSpec:
    """Validate the agent's ``ask`` args. Raises :class:`AskError` (``bad_*``)."""
    if not isinstance(args, dict):
        raise AskError("bad_args")
    kind = args.get("kind")
    if kind not in ROLES:
        raise AskError("bad_kind")
    targets = _targets(args.get("targets"), kind)
    reason = clean_text(args.get("reason"), REASON_MAX)
    if not reason:
        raise AskError("bad_reason")
    label = ""
    digits = 0
    summary = ""
    if kind == "text":
        raw_label = args.get("label")
        label = raw_label if isinstance(raw_label, str) and raw_label in TEXT_LABELS else "other"
    if kind == "otp":
        raw_digits = args.get("digits")
        if isinstance(raw_digits, bool) or not isinstance(raw_digits, int):
            raise AskError("bad_digits")
        if not OTP_DIGITS_MIN <= raw_digits <= OTP_DIGITS_MAX:
            raise AskError("bad_digits")
        digits = raw_digits
    if kind == "confirm":
        summary = clean_text(args.get("summary"), SUMMARY_MAX)
        if not summary:
            raise AskError("bad_summary")
    return AskSpec(kind, targets, reason, label=label, digits=digits, summary=summary)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an http(s) URL, else ``""``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return ""
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


@dataclass
class Binding:
    role: str
    node: int
    #: The target's rect in viewport CSS pixels at ask time.
    rect: tuple[float, float, float, float]
    input: str = ""


@dataclass
class QuickAsk:
    """One pending ask: what it was bound to, what the card shows, and how it ended."""

    spec: AskSpec
    origin: str
    frame_id: str
    loader_id: str
    bindings: list[Binding]
    scale: float
    frame_w: int
    frame_h: int
    button: str = ""
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    expires_at: int = 0
    deadline: float = 0.0
    phase: str = PHASE_WAITING
    outcome: str = ""
    #: Set once the ask has an outcome; the agent's poll waits on it.
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: The scoped takeover this ask claimed (human_check): input outside this
    #: frame-pixel rect is dropped while it is held.
    claimed: bool = False
    #: The tab (CDP target) the card streamed when the ask was bound.
    target_id: str = ""
    #: One answer at a time: a double-clicked Fill runs once.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def start_clock(self, now_mono: float) -> None:
        span = HUMAN_CHECK_TIMEOUT_S if self.spec.kind == "human_check" else ASK_TIMEOUT_S
        self.deadline = now_mono + span
        self.expires_at = int((time.time() + span) * 1000)

    def settle(self, outcome: str) -> bool:
        if self.outcome:
            return False
        self.outcome = outcome
        self.settled.set()
        return True

    def wire(self) -> dict[str, Any]:
        """The card's ``ask`` field. Built from bound state only, never page text but the button."""
        fields = []
        for b in self.bindings:
            x, y, w, h = _frame_rect(b.rect, self.scale)
            entry: dict[str, Any] = {"role": b.role, "rect": {"x": x, "y": y, "w": w, "h": h}}
            if b.input:
                entry["input"] = b.input
            if b.role == "text":
                entry["label"] = self.spec.label
            fields.append(entry)
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.spec.kind,
            "origin": self.origin,
            "reason": self.spec.reason,
            "fields": fields,
            "frame": {"w": self.frame_w, "h": self.frame_h},
            "expires_at": self.expires_at,
            "phase": self.phase,
        }
        if self.spec.kind == "otp":
            out["digits"] = self.spec.digits
        if self.spec.kind == "confirm":
            out["summary"] = self.spec.summary
            out["button"] = self.button
        return out


def _frame_rect(rect: tuple[float, float, float, float], scale: float) -> tuple[int, int, int, int]:
    s = scale if scale > 0 else 1.0
    x, y, w, h = rect
    return int(round(x * s)), int(round(y * s)), int(round(w * s)), int(round(h * s))


# -- the CDP half -------------------------------------------------------------

#: Runs ON the target element in the isolated world. Reads only; the page's own
#: prototypes are not reachable from here, so it cannot be lied to.
_INSPECT_JS = """function () {
  var el = this, d = document;
  var root = el.getRootNode ? el.getRootNode() : d;
  var r = el.getBoundingClientRect();
  var cs = getComputedStyle(el);
  var visible = r.width >= 2 && r.height >= 2 && cs.display !== 'none'
    && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0.05;
  for (var p = el.parentElement; visible && p; p = p.parentElement) {
    var ps = getComputedStyle(p);
    if (ps.display === 'none' || ps.visibility === 'hidden'
        || parseFloat(ps.opacity || '1') <= 0.05) visible = false;
  }
  var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  var inView = cx >= 0 && cy >= 0 && cx < innerWidth && cy < innerHeight;
  var hitRoot = root && root.elementFromPoint ? root : d;
  var hit = inView ? hitRoot.elementFromPoint(cx, cy) : null;
  var tag = (el.tagName || '').toLowerCase();
  var text = '';
  if (tag === 'input') text = el.value || ''; else text = el.innerText || el.textContent || '';
  return {
    connected: el.isConnected === true,
    sameDoc: el.ownerDocument === d,
    tag: tag,
    type: tag === 'input' ? String(el.type || '').toLowerCase() : '',
    role: String(el.getAttribute('role') || '').toLowerCase(),
    href: tag === 'a' && el.hasAttribute('href'),
    disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    readOnly: el.readOnly === true,
    visible: visible,
    covered: !(hit && (hit === el || el.contains(hit))),
    rect: [r.left, r.top, r.width, r.height],
    vw: innerWidth, vh: innerHeight,
    text: text.replace(/\\s+/g, ' ').trim().slice(0, 120)
  };
}"""

#: Is the bound element the focused one (through open shadow roots)?
_FOCUSED_JS = """function () {
  var root = this.getRootNode ? this.getRootNode() : document;
  return root.activeElement === this;
}"""

#: Mark the field as the person's (the snapshot walker, the frame mask and the
#: screenshot guard then withhold its value) and put the caret over its whole
#: value so the insert replaces it.
_PREPARE_JS = """function () {
  this.setAttribute('data-kc-asked', '');
  try { if (typeof this.select === 'function') this.select(); } catch (e) {}
  return true;
}"""

#: Has the human_check widget reported success? A checkbox-style widget sets
#: aria-checked; the token widgets write a non-empty response field into the page.
_PASSED_JS = """function () {
  var el = this;
  if (el.getAttribute('aria-checked') === 'true' || el.checked === true) return true;
  if (el.querySelector && el.querySelector('[aria-checked="true"]')) return true;
  var names = ['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response'];
  for (var i = 0; i < names.length; i++) {
    var f = document.querySelectorAll('[name="' + names[i] + '"]');
    for (var j = 0; j < f.length; j++) if (f[j].value) return true;
  }
  return false;
}"""

_WORLD = "kirocrew_quick_ask"


class PageProbe:
    """The CDP calls an ask makes, on the live stream's own session for the tab."""

    def __init__(self, relay: Any, session_id: str) -> None:
        self.relay = relay
        self.session_id = session_id

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = await self.relay.send(method, params, self.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The method name only: a CDP error never carries a value through here.
            raise AskError("cdp_failed") from None
        return reply if isinstance(reply, dict) else {}

    async def top_frame(self) -> tuple[str, str, str]:
        """``(frame_id, loader_id, origin)`` of the committed top frame."""
        tree = await self._send("Page.getFrameTree", {})
        frame = (tree.get("frameTree") or {}).get("frame") or {}
        return (
            str(frame.get("id") or ""),
            str(frame.get("loaderId") or ""),
            origin_of(str(frame.get("url") or "")),
        )

    async def world(self, frame_id: str) -> int:
        made = await self._send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": _WORLD, "grantUniveralAccess": False},
        )
        ctx = made.get("executionContextId")
        if not isinstance(ctx, int):
            raise AskError("cdp_failed")
        return ctx

    async def ref_node(self, ref: str) -> int:
        """The ``backendNodeId`` a snapshot ref names, or :class:`AskError` ``ref_stale``."""
        expr = (
            "(window.__kcRefDocObj === document && window.__kcRefs instanceof Map)"
            f" ? (window.__kcRefs.get({ref!r}) || null) : null"
        )
        found = await self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        obj = (found.get("result") or {}).get("objectId")
        if not obj:
            raise AskError("ref_stale")
        try:
            desc = await self._send("DOM.describeNode", {"objectId": obj})
        finally:
            try:
                await self._send("Runtime.releaseObject", {"objectId": obj})
            except AskError:
                pass
        node = (desc.get("node") or {}).get("backendNodeId")
        if not isinstance(node, int):
            raise AskError("ref_stale")
        return node

    async def call(self, node: int, ctx: int, js: str) -> Any:
        """Run *js* on *node* in the isolated world *ctx*; its by-value result."""
        resolved = await self._send(
            "DOM.resolveNode", {"backendNodeId": node, "executionContextId": ctx}
        )
        obj = (resolved.get("object") or {}).get("objectId")
        if not obj:
            raise AskError("gone")
        try:
            out = await self._send(
                "Runtime.callFunctionOn",
                {"objectId": obj, "functionDeclaration": js, "returnByValue": True},
            )
        finally:
            try:
                await self._send("Runtime.releaseObject", {"objectId": obj})
            except AskError:
                pass
        if out.get("exceptionDetails"):
            raise AskError("cdp_failed")
        return (out.get("result") or {}).get("value")

    async def inspect(self, node: int, ctx: int) -> dict[str, Any]:
        info = await self.call(node, ctx, _INSPECT_JS)
        if not isinstance(info, dict):
            raise AskError("cdp_failed")
        return info


def target_problem(role: str, info: dict[str, Any]) -> str:
    """Why this element cannot take this role, or ``""``. The refusal codes."""
    if not info.get("connected"):
        return "gone"
    if not info.get("sameDoc"):
        return "cross_frame"
    if not info.get("visible"):
        return "hidden"
    if info.get("covered"):
        return "covered"
    tag, typ = info.get("tag"), info.get("type")
    if role == "password":
        if tag != "input" or typ != "password":
            return "wrong_type"
    elif role in ("username", "text", "otp"):
        if tag != "input" or typ not in _TEXT_TYPES:
            return "wrong_type"
    elif role == "widget":
        # A CAPTCHA frame or a checkbox, no bigger than a challenge pane: never a
        # button or a page-sized box the person's "tick" would land on.
        boxy = (
            tag == "iframe"
            or (tag == "input" and typ == "checkbox")
            or info.get("role") == "checkbox"
        )
        if not boxy:
            return "wrong_type"
        rect = info.get("rect") or [0, 0, 0, 0]
        if float(rect[2]) > WIDGET_MAX_W or float(rect[3]) > WIDGET_MAX_H:
            return "too_large"
    elif role == "action":
        clickable = (
            tag == "button"
            or (tag == "input" and typ in ("submit", "button"))
            or (tag == "a" and info.get("href"))
            or info.get("role") == "button"
        )
        if not clickable:
            return "wrong_type"
    if role != "widget" and (info.get("disabled") or info.get("readOnly")):
        return "disabled"
    return ""


_INPUT_KIND = {"username": "text", "password": "password", "otp": "otp"}


async def bind_ask(probe: PageProbe, spec: AskSpec, scale: float) -> QuickAsk:
    """Bind *spec* to the page as it is now. Raises :class:`AskError` with the refusal code."""
    frame_id, loader_id, origin = await probe.top_frame()
    if not frame_id or not origin:
        raise AskError("not_web_page")
    ctx = await probe.world(frame_id)
    bindings: list[Binding] = []
    button = ""
    vw = vh = 0.0
    order = [role for role, _ in ROLES[spec.kind] if role in spec.targets]
    for role in order:
        node = await probe.ref_node(spec.targets[role])
        info = await probe.inspect(node, ctx)
        problem = target_problem(role, info)
        if problem:
            raise AskError(problem)
        x, y, w, h = (float(v) for v in info["rect"])
        typ = str(info.get("type") or "text")
        kind = _INPUT_KIND.get(role, "")
        if role == "text":
            kind = typ if typ in ("email", "tel") else "text"
        bindings.append(Binding(role=role, node=node, rect=(x, y, w, h), input=kind))
        if role == "action":
            button = clean_text(info.get("text"), BUTTON_TEXT_MAX)
            if not button:
                raise AskError("no_button_text")
        vw, vh = float(info.get("vw") or 0), float(info.get("vh") or 0)
    s = scale if scale > 0 else 1.0
    return QuickAsk(
        spec=spec,
        origin=origin,
        frame_id=frame_id,
        loader_id=loader_id,
        bindings=bindings,
        scale=s,
        frame_w=int(round(vw * s)),
        frame_h=int(round(vh * s)),
        button=button,
    )


async def verify(probe: PageProbe, ask: QuickAsk) -> int:
    """Re-check the binding; the isolated world's context id, or ``origin_changed``."""
    try:
        frame_id, loader_id, origin = await probe.top_frame()
        if (frame_id, loader_id, origin) != (ask.frame_id, ask.loader_id, ask.origin):
            raise AskError(ORIGIN_CHANGED)
        ctx = await probe.world(frame_id)
        for b in ask.bindings:
            info = await probe.inspect(b.node, ctx)
            if target_problem(b.role, info):
                raise AskError(ORIGIN_CHANGED)
    except AskError as exc:
        raise AskError(ORIGIN_CHANGED) from exc.__cause__
    return ctx


async def fill(probe: PageProbe, ask: QuickAsk, values: dict[str, str]) -> None:
    """Write each value into its own bound node. Raises ``origin_changed`` or ``fill_failed``.

    Re-verified first; then, per field, focus the bound node, prove it holds focus,
    select its contents and insert the text. The values never leave this function
    except as the ``Input.insertText`` parameter.
    """
    ctx = await verify(probe, ask)
    for b in ask.bindings:
        value = values.get(b.role)
        if value is None:
            continue
        await probe._send("DOM.focus", {"backendNodeId": b.node})
        await probe.call(b.node, ctx, _PREPARE_JS)
        # The focus proof is the LAST read before the insert, so the window in which
        # a page script could move focus is one round trip.
        if await probe.call(b.node, ctx, _FOCUSED_JS) is not True:
            raise AskError(ORIGIN_CHANGED)
        try:
            await probe.relay.send("Input.insertText", {"text": value}, probe.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AskError("fill_failed") from None
    frame_id, loader_id, origin = await probe.top_frame()
    if (frame_id, loader_id, origin) != (ask.frame_id, ask.loader_id, ask.origin):
        # Written already; the page moved during the insert. Say so, not "filled".
        raise AskError(ORIGIN_CHANGED)


async def click_once(probe: PageProbe, ask: QuickAsk) -> None:
    """The confirm ask's ONE click: re-verified, at the bound button's centre right now."""
    ctx = await verify(probe, ask)
    b = ask.bindings[0]
    info = await probe.inspect(b.node, ctx)
    if target_problem(b.role, info):
        raise AskError(ORIGIN_CHANGED)
    x, y, w, h = (float(v) for v in info["rect"])
    point = {"x": round(x + w / 2, 2), "y": round(y + h / 2, 2), "button": "left", "clickCount": 1}
    await probe._send("Input.dispatchMouseEvent", {"type": "mousePressed", **point})
    await probe._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **point})


async def widget_rect_now(probe: PageProbe, ask: QuickAsk) -> tuple[float, float, float, float]:
    """The human_check widget's rect in viewport CSS pixels, measured NOW (re-verified):
    a scroll or layout shift since the ask must not move the person's box off it."""
    ctx = await verify(probe, ask)
    info = await probe.inspect(ask.bindings[0].node, ctx)
    x, y, w, h = (float(v) for v in info["rect"])
    return x, y, w, h


async def human_check_passed(probe: PageProbe, ask: QuickAsk) -> bool:
    """Whether the widget reports success. Raises ``origin_changed`` when the page moved."""
    frame_id, loader_id, origin = await probe.top_frame()
    if (frame_id, loader_id, origin) != (ask.frame_id, ask.loader_id, ask.origin):
        raise AskError(ORIGIN_CHANGED)
    ctx = await probe.world(frame_id)
    try:
        return await probe.call(ask.bindings[0].node, ctx, _PASSED_JS) is True
    except AskError:
        raise AskError(ORIGIN_CHANGED) from None


def check_values(ask: QuickAsk, values: Any) -> dict[str, str] | None:
    """The fill's values, shaped and bounded for this ask, or ``None`` when they do not fit."""
    if not isinstance(values, dict):
        return None
    out: dict[str, str] = {}
    for b in ask.bindings:
        name = b.role
        raw = values.get(name)
        if not isinstance(raw, str) or not raw or len(raw) > VALUE_MAX:
            return None
        if name == "otp" and (
            len(raw) != ask.spec.digits or not raw.isdigit() or not raw.isascii()
        ):
            return None
        if "\n" in raw or "\r" in raw:
            return None
        out[name] = raw
    extra = set(values) - {b.role for b in ask.bindings}
    if extra:
        return None
    return out


def in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


__all__ = [
    "ACTIONS",
    "ALL_ACTIONS",
    "ANSWER_BODY_MAX",
    "AskError",
    "AskSpec",
    "Binding",
    "KINDS",
    "PageProbe",
    "QuickAsk",
    "VALUE_MAX",
    "bind_ask",
    "check_values",
    "clean_text",
    "click_once",
    "fill",
    "human_check_passed",
    "in_rect",
    "origin_of",
    "parse_ask",
    "target_problem",
    "verify",
]
