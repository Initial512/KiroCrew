"""Production call sites for the enterprise grant and budget gates.

``enterprise.GrantGate`` and ``enterprise.BudgetGate`` decide; this module is
where the gateway asks them. :func:`enterprise_gate_middleware` runs inner to
token auth, after the request's actor is published, and checks two things:

* **shared access.** A request from a person reaching the Crew through a shared
  path (a tailnet peer or an enterprise identity) passes ``GrantGate.decide``
  once a grant authority is composed. A signed-in enterprise identity with no
  authority composed is refused ``access_decision_unavailable``: a login alone
  never grants Crew access. While a grant authority is composed, an Unknown
  caller (for example one who opened a channel-minted dashboard link through a
  proxy) is refused ``unattributed_actor``, because the authority cannot decide
  for no one; a channel user acts only through the authority's explicit
  mapping (:meth:`EnterpriseGates.check_channel_actor`). The local operator,
  Crew automation and apps are not shared access and are not asked.
* **revocation.** With a revocation feed composed, a removed person is refused
  at the cookie, WebSocket and approval-submission points
  (``enterprise_revocation``), and the same enforcer rechecks queued work
  before it starts.
* **inference.** A request that starts new model work (:data:`CHARGEABLE_ROUTES`)
  passes ``BudgetGate.check`` once a budget provider is composed. Model work
  with no request (cron schedules, channel turns) asks
  :func:`check_background_inference` at its entry point.

With the public default seam neither gate is composed, so both checks return
``not_configured`` and every request proceeds exactly as before: the disabled
default is a no-op, and the capabilities report shows each concern unavailable.

A refusal is a 403 JSON body naming a machine-readable ``reason``. The gates
are rebuilt only when the composed providers change, so a grant cached by one
request serves the next.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from aiohttp import web

from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Principal,
    PrincipalKind,
    Provenance,
)
from kiro_crew.shared_crew.enterprise import (
    BudgetGate,
    EnterpriseSeams,
    GrantGate,
    OperationKind,
    resolve_enterprise,
)
from kiro_crew.shared_crew.enterprise_revocation import (
    EnforcementPoint,
    RevocationEnforcer,
    UnavailableRevocationEnforcer,
    install_enforcer,
)

logger = logging.getLogger(__name__)

REASON_NOT_CONFIGURED = "not_configured"
REASON_ALLOWED = "allowed"
ERROR_SHARED_ACCESS_REFUSED = "shared_access_refused"
ERROR_INFERENCE_REFUSED = "inference_refused"
#: Mirrors ``crew_mode.ERROR_TEAM_CONFIG_INVALID``.
ERROR_TEAM_CONFIG_INVALID = "team_config_invalid"
REASON_ACCESS_DECISION_UNAVAILABLE = "access_decision_unavailable"
REASON_UNATTRIBUTED_ACTOR = "unattributed_actor"
REASON_CHANNEL_ACTOR_UNMAPPED = "channel_actor_unmapped"

_APPROVAL_PATHS = re.compile(
    r"\A/api/(?:approvals/[^/]+/[^/]+|chat/slots/[^/]+/approve|ask-question/[^/]+/answer)\Z"
)

#: Provenance that marks a person reaching the Crew through a shared path.
SHARED_PROVENANCE = frozenset(
    {
        Provenance.TAILSCALE_SERVE,
        Provenance.TAILSCALE_DIRECT,
        Provenance.TAILSCALE_LISTENER,
        Provenance.ENTERPRISE,
    }
)

#: ``(pattern, operation)`` for every POST that starts new model work over HTTP.
CHARGEABLE_ROUTES: tuple[tuple[re.Pattern[str], OperationKind], ...] = tuple(
    (re.compile(pattern), op)
    for pattern, op in (
        (r"\A/api/chat\Z", OperationKind.NEW_TURN),
        (r"\A/v1/chat/completions\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/continue\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/side/turn\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/threads/[^/]+/reply\Z", OperationKind.NEW_TURN),
        (r"\A/api/members/[^/]+/thread\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/regenerate\Z", OperationKind.RETRY),
        (r"\A/api/chat/slots/[^/]+/edit-resend\Z", OperationKind.RETRY),
        (r"\A/api/taskrunner/[^/]+/retry\Z", OperationKind.RETRY),
        (r"\A/api/spawn\Z", OperationKind.SPAWN),
        (r"\A/api/spawn/adaptive\Z", OperationKind.SPAWN),
        (r"\A/api/spawn/[^/]+/continue\Z", OperationKind.SPAWN),
        (r"\A/api/crons/[^/]+/run\Z", OperationKind.AUTOMATION_RUN),
        (r"\A/api/taskrunner/plan\Z", OperationKind.AUTOMATION_RUN),
        (r"\A/api/taskrunner/[^/]+/execute\Z", OperationKind.AUTOMATION_RUN),
    )
)


def chargeable_operation(method: str, path: str) -> Optional[OperationKind]:
    """The operation a request starts, or ``None`` when it starts no model work."""
    if method != "POST":
        return None
    for pattern, op in CHARGEABLE_ROUTES:
        if pattern.match(path):
            return op
    return None


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    reason: str
    #: False when no provider is composed, so nothing was enforced.
    enforced: bool = False


_NOT_CONFIGURED = GateVerdict(True, REASON_NOT_CONFIGURED)


def _crew_id_of(seams: EnterpriseSeams) -> str:
    binding = None
    if seams.crew_binding is not None:
        try:
            binding = seams.crew_binding.current_binding()
        except Exception:  # noqa: BLE001 -- an unreadable binding names no Crew
            logger.info("crew binding unreadable", exc_info=True)
    if binding is not None:
        return binding.crew.crew_id
    return seams.deployment.deployment_ref or "local"


class EnterpriseGates:
    """The grant, revocation and budget gates built from one set of checked providers."""

    def __init__(self, seams: EnterpriseSeams, *, now: Optional[float] = None) -> None:
        self.seams = seams
        self._grants: Optional[GrantGate] = None
        self._budget: Optional[BudgetGate] = None
        self._revocation: Optional[RevocationEnforcer] = None
        crew_id = _crew_id_of(seams)
        if seams.grant_authority is not None:
            self._grants = GrantGate(seams.grant_authority, crew_id=crew_id)
        if seams.inference_budget is not None:
            self._budget = BudgetGate(seams.inference_budget, seams.deployment)
        if seams.revocation_feed is not None:
            try:
                self._revocation = RevocationEnforcer(
                    seams.revocation_feed,
                    crew_id=crew_id,
                    now=time.time() if now is None else now,
                )
            except Exception:  # noqa: BLE001 -- a composed but unreadable feed refuses access
                logger.warning("revocation feed refused", exc_info=True)
                self._revocation = UnavailableRevocationEnforcer(
                    None, crew_id=crew_id, now=time.time() if now is None else now
                )
        self._sign_in = seams.credential_verifier is not None or seams.sign_in is not None

    @property
    def configured(self) -> bool:
        return (
            self._grants is not None
            or self._budget is not None
            or self._revocation is not None
            or self._sign_in
        )

    @property
    def revocation(self) -> Optional[RevocationEnforcer]:
        return self._revocation

    def check_shared_access(
        self,
        principal: Principal,
        now: float,
        point: EnforcementPoint = EnforcementPoint.COOKIE,
        *,
        authenticated: bool = True,
    ) -> GateVerdict:
        """Whether *principal* may use this Crew through a shared path.

        *authenticated* is whether dashboard auth admitted the request. An
        Unknown caller that auth did not admit (a public path) is left to auth.
        """
        if not self.configured:
            return _NOT_CONFIGURED
        if principal.kind is PrincipalKind.UNKNOWN:
            if self._grants is None or not authenticated:
                return _NOT_CONFIGURED
            return GateVerdict(False, REASON_UNATTRIBUTED_ACTOR, enforced=True)
        if not is_shared_access(principal):
            return _NOT_CONFIGURED
        if self._revocation is not None:
            removed = self._revocation.check(point, principal.id, now)
            if not removed.allowed:
                return GateVerdict(False, removed.reason.value, enforced=True)
        if self._grants is None:
            if principal.provenance is Provenance.ENTERPRISE:
                return GateVerdict(False, REASON_ACCESS_DECISION_UNAVAILABLE, enforced=True)
            return _NOT_CONFIGURED
        decision = self._grants.decide(principal, now)
        if decision.allowed and self._revocation is not None:
            self._revocation.note_member(principal.id)
        return GateVerdict(decision.allowed, decision.reason.value, enforced=True)

    def check_channel_actor(
        self, channel: str, channel_user_id: str, now: float
    ) -> tuple[GateVerdict, Optional[Principal]]:
        """The person a channel user acts as, and whether they may.

        With no grant authority composed the channel's own allowlist governs
        alone, as before. Otherwise the user acts only through the
        authority's explicit mapping, and the mapped person passes
        :meth:`check_shared_access` like anyone else.
        """
        if self._grants is None:
            return _NOT_CONFIGURED, None
        mapping = self.seams.channel_actors
        person: Optional[Principal] = None
        if mapping is not None:
            try:
                person = mapping.principal_for(channel, channel_user_id)
            except Exception:  # noqa: BLE001 -- an unreadable mapping maps no one
                logger.info("channel actor mapping failed", exc_info=True)
        if not isinstance(person, Principal) or person.kind is not PrincipalKind.HUMAN:
            return GateVerdict(False, REASON_CHANNEL_ACTOR_UNMAPPED, enforced=True), None
        decision = self._grants.decide(person, now)
        verdict = GateVerdict(decision.allowed, decision.reason.value, enforced=True)
        if decision.allowed and self._revocation is not None:
            removed = self._revocation.check(EnforcementPoint.COOKIE, person.id, now)
            if not removed.allowed:
                verdict = GateVerdict(False, removed.reason.value, enforced=True)
        return verdict, person if verdict.allowed else None

    def check_inference(self, operation: OperationKind) -> GateVerdict:
        """Whether *operation* may start new model work under the budget."""
        if self._budget is None:
            return _NOT_CONFIGURED
        decision = self._budget.check(operation)
        return GateVerdict(decision.allowed, decision.reason.value, enforced=True)


def check_background_inference(
    operation: OperationKind,
    gates: Callable[[], EnterpriseGates] | None = None,
) -> GateVerdict:
    """``BudgetGate.check`` for model work that does not arrive over HTTP.

    Cron schedules and channel turns start model work with no request the
    middleware could see, so their entry points ask here. With no budget
    provider composed this is ``not_configured`` and the work proceeds; an
    unreadable seam composes nothing, exactly as in the middleware.
    """
    try:
        current = (gates or current_gates)()
    except Exception:  # noqa: BLE001 -- an unreadable seam composes nothing
        logger.warning("shared crew enterprise gates unavailable", exc_info=True)
        return _NOT_CONFIGURED
    return current.check_inference(operation)


async def check_background_inference_off_loop(operation: OperationKind) -> GateVerdict:
    """:func:`check_background_inference` with the seam read (config, files) off the loop."""
    return await asyncio.to_thread(check_background_inference, operation)


def is_shared_access(principal: Principal) -> bool:
    """A person reaching the Crew through a tailnet or enterprise identity."""
    return principal.kind is PrincipalKind.HUMAN and principal.provenance in SHARED_PROVENANCE


_LOCK = threading.Lock()
_CACHED: Optional[tuple[tuple[int, ...], EnterpriseGates]] = None


def current_gates(resolver: Callable[[], EnterpriseSeams] = resolve_enterprise) -> EnterpriseGates:
    """The gates for the composed providers, reused while they stay the same."""
    global _CACHED
    seams = resolver()
    key = (
        id(seams.grant_authority),
        id(seams.inference_budget),
        id(seams.crew_binding),
        id(seams.revocation_feed),
        id(seams.credential_verifier),
        id(seams.sign_in),
        id(seams.channel_actors),
    )
    with _LOCK:
        if _CACHED is not None and _CACHED[0] == key:
            return _CACHED[1]
        gates = EnterpriseGates(seams)
        _CACHED = (
            None if isinstance(gates.revocation, UnavailableRevocationEnforcer) else (key, gates)
        )
        install_enforcer(gates.revocation)
        return gates


def _reset_for_tests() -> None:
    global _CACHED
    with _LOCK:
        _CACHED = None
    install_enforcer(None)


def _point_of(request: web.Request) -> EnforcementPoint:
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        return EnforcementPoint.WEBSOCKET
    if request.method == "POST" and _APPROVAL_PATHS.match(request.path):
        return EnforcementPoint.APPROVAL_SUBMISSION
    return EnforcementPoint.COOKIE


def _refusal(error: str, reason: str) -> web.Response:
    return web.json_response({"error": error, "code": error, "reason": reason}, status=403)


def _actor_principal(request: web.Request) -> Optional[Principal]:
    actor = request.get(ACTOR_REQUEST_KEY)
    return actor.principal if isinstance(actor, ActorContext) else None


def team_config_refusal(seams: EnterpriseSeams) -> Optional[str]:
    """The reason team mode is refused for *seams*, or ``None`` (``crew_mode``)."""
    from kiro_crew.shared_crew.access import setup_paths_dict
    from kiro_crew.shared_crew.crew_mode import team_config_problem

    problem = team_config_problem(seams, setup_paths_dict())
    return None if problem is None else problem.reason


def make_enterprise_gate_middleware(
    gates: Callable[[], EnterpriseGates] = current_gates,
    clock: Callable[[], float] = time.time,
    team_check: Callable[[EnterpriseSeams], Optional[str]] = team_config_refusal,
) -> Any:
    """The middleware that asks both gates; *gates*, *clock* and *team_check* are seams for tests.

    While the team configuration is invalid, a person reaching the Crew through
    a shared path is refused with ``team_config_invalid``: an unusable team
    setup never falls back to admitting them as if the Crew were personal. The
    local operator is not a shared-access principal and keeps working.
    """

    @web.middleware
    async def enterprise_gate_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            current = gates()
        except Exception:  # noqa: BLE001 -- an unreadable seam composes nothing
            logger.warning("shared crew enterprise gates unavailable", exc_info=True)
            return await handler(request)
        principal = _actor_principal(request)
        if principal is not None and is_shared_access(principal):
            try:
                refused = team_check(current.seams)
            except Exception:  # noqa: BLE001 -- an unreadable check refuses
                logger.warning("shared crew team configuration check failed", exc_info=True)
                refused = "team_config_check_failed"
            if refused:
                return _refusal(ERROR_TEAM_CONFIG_INVALID, refused)
        if not current.configured:
            return await handler(request)
        now = clock()
        enforcer = current.revocation
        if enforcer is not None and enforcer.sync_due(now):
            await asyncio.to_thread(enforcer.sync, now)
        if principal is not None:
            verdict = current.check_shared_access(
                principal, now, _point_of(request), authenticated=bool(request.get("user"))
            )
            if not verdict.allowed:
                return _refusal(ERROR_SHARED_ACCESS_REFUSED, verdict.reason)
        operation = chargeable_operation(request.method, request.path)
        if operation is not None:
            verdict = current.check_inference(operation)
            if not verdict.allowed:
                return _refusal(ERROR_INFERENCE_REFUSED, verdict.reason)
        return await handler(request)

    return enterprise_gate_middleware


SHUTDOWN_CLEANUP_TIMEOUT_SECONDS = 10


def install_lifecycle(app: web.Application) -> Callable[[], None]:
    """Wire the safety probe before traffic; return the post-bind maintenance starter.

    Reconciliation and orphan cleanup scale with durable state, so neither runs
    in aiohttp's pre-bind startup hooks. Admission and capacity reads still await
    ``ensure_ready`` themselves, sharing its reconciliation lock with maintenance.
    """
    maintenance: asyncio.Task[None] | None = None
    probes: asyncio.Task[None] | None = None

    async def _shared_crew_startup(app_: web.Application) -> None:
        # Defer admission imports until startup; registering hooks performs no I/O.
        from kiro_crew.shared_crew import wiring
        from kiro_crew.shared_crew.admission_gate import startup_check

        wiring.install(app_["state"])
        await startup_check()

    def start_maintenance() -> None:
        nonlocal maintenance, probes
        from kiro_crew.shared_crew import host_power
        from kiro_crew.shared_crew.admission_gate import installed_service

        if probes is None:
            # Informational host probes, after the bind and off the boot path.
            probes = asyncio.create_task(host_power.probe_after_start())
        service = installed_service()
        if maintenance is not None or service is None or not service.enabled:
            return

        async def maintain() -> None:
            try:
                await service.ensure_ready()
                released = await service.release_stale_slices()
                if released:
                    logger.info("shared crew: released %d idle principal slice(s)", released)
            except Exception:  # noqa: BLE001 -- admission keeps its own readiness fence
                logger.warning("shared crew: startup maintenance failed", exc_info=True)

        maintenance = asyncio.create_task(maintain())

    async def _shared_crew_cleanup(_app: web.Application) -> None:
        # Cleanup is optional until startup has built an admission service.
        from kiro_crew.shared_crew.admission_gate import shutdown_cleanup

        for task in (maintenance, probes):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            await asyncio.wait_for(shutdown_cleanup(), timeout=SHUTDOWN_CLEANUP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 -- cleanup never blocks the stop
            logger.debug("shared crew: slice cleanup at stop failed", exc_info=True)

    app.on_startup.append(_shared_crew_startup)
    app.on_cleanup.append(_shared_crew_cleanup)
    return start_maintenance


def install(app: web.Application) -> None:
    """Append the gate middleware, inner to token auth and the slot checkpoint."""
    app.middlewares.append(make_enterprise_gate_middleware())


__all__ = [
    "CHARGEABLE_ROUTES",
    "check_background_inference",
    "check_background_inference_off_loop",
    "EnterpriseGates",
    "GateVerdict",
    "chargeable_operation",
    "current_gates",
    "install",
    "is_shared_access",
    "make_enterprise_gate_middleware",
]
