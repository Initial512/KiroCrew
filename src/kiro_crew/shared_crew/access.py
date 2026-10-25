"""Which Shared Crew setup paths this Crew has, and what each reports.

The People surface guides a person who has no shared-access path configured to
one of the setup paths in :data:`SETUP_PATHS`. Each path reports the
availability a source registered for it, and a path with no source reports
``not_in_this_build``. :func:`note_tailnet` is the Tailscale source: the gateway
calls it with the tailnet state it resolved at start.

Every answer here is computed from state already in memory, so it is safe on
the request path.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from kiro_crew.shared_crew.contracts import CapabilityState

logger = logging.getLogger(__name__)


# ── shared-access setup paths ──

SETUP_TAILSCALE = "tailscale"
SETUP_KIRO_LOGIN = "kiro_login"
SETUP_SYSTEMS_MANAGER = "systems_manager"
SETUP_PATHS: tuple[str, ...] = (SETUP_TAILSCALE, SETUP_KIRO_LOGIN, SETUP_SYSTEMS_MANAGER)

REASON_NOT_IN_THIS_BUILD = "not_in_this_build"
REASON_NOT_CONFIGURED = "not_configured"
REASON_TAILNET_UNREACHABLE = "tailnet_unreachable"
REASON_SOURCE_FAILED = "source_failed"
#: Why a person reaching the Crew through ``tailscale serve`` cannot be named:
#: the ``tailnet_proxy_proof.proof_support`` answers, mirrored here so this
#: package does not import the dashboard. ``daemon_not_found`` is the one gap
#: that can close while the gateway runs.
REASON_PLATFORM_UNPROVABLE = "platform_unprovable"
REASON_DAEMON_SAME_USER = "daemon_same_user"
REASON_DAEMON_NOT_FOUND = "daemon_not_found"
FORWARDED_ATTRIBUTION_REASONS: tuple[str, ...] = (
    REASON_PLATFORM_UNPROVABLE,
    REASON_DAEMON_SAME_USER,
    REASON_DAEMON_NOT_FOUND,
)
#: The forwarded-attribution reason until the post-start probe has answered.
REASON_NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class SetupPathState:
    state: CapabilityState
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state.value, "reason": self.reason}


SetupPathSource = Callable[[], SetupPathState]

_SOURCES: dict[str, SetupPathSource] = {}
_LOCK = threading.Lock()


def register_setup_path_source(path: str, source: SetupPathSource) -> None:
    """Make *source* the answer for one setup path; a path registers once."""
    if path not in SETUP_PATHS:
        raise ValueError(f"unknown setup path {path!r}")
    with _LOCK:
        if path in _SOURCES:
            raise ValueError(f"setup path {path!r} already has a source")
        _SOURCES[path] = source


def unregister_setup_path_source(path: str) -> None:
    with _LOCK:
        _SOURCES.pop(path, None)


def _reset_setup_paths_for_tests() -> None:
    global _forwarded_attribution, _forwarded_inputs, _identity_config_unreadable, _direct_tailnet
    with _LOCK:
        _SOURCES.clear()
        _forwarded_attribution = ForwardedAttribution()
        _forwarded_inputs = (False, False)
        _identity_config_unreadable = False
        _direct_tailnet = DirectTailnet()


def _tailnet_state(enabled: bool, host: str, identity_trust: bool) -> SetupPathState:
    if not enabled:
        if identity_trust:
            # Identity trust with an allowlist only exists to admit tailnet
            # people, so sharing is set up even where ``tailscale serve`` was
            # configured by hand rather than through ``dashboard.tailscale.enabled``.
            return SetupPathState(CapabilityState.AVAILABLE)
        return SetupPathState(CapabilityState.UNAVAILABLE, REASON_NOT_CONFIGURED)
    if not host:
        return SetupPathState(CapabilityState.DEGRADED, REASON_TAILNET_UNREACHABLE)
    return SetupPathState(CapabilityState.AVAILABLE)


@dataclass(frozen=True)
class ForwardedAttribution:
    """Whether a person reaching this Crew through ``tailscale serve`` can be named."""

    state: CapabilityState = CapabilityState.AVAILABLE
    reason: str = ""


_forwarded_attribution = ForwardedAttribution()
#: ``(identity_trust, loopback_only)`` as :func:`note_tailnet` last recorded them.
_forwarded_inputs: tuple[bool, bool] = (False, False)
_identity_config_unreadable = False


def _forwarded_state(
    identity_trust: bool, loopback_only: bool, proof_gap: str | None
) -> ForwardedAttribution:
    if not (identity_trust and loopback_only):
        return ForwardedAttribution()
    if proof_gap is None:
        return ForwardedAttribution(CapabilityState.DEGRADED, REASON_NOT_CHECKED)
    if not proof_gap:
        return ForwardedAttribution()
    if proof_gap not in FORWARDED_ATTRIBUTION_REASONS:
        return ForwardedAttribution(CapabilityState.UNAVAILABLE, REASON_PLATFORM_UNPROVABLE)
    if proof_gap == REASON_DAEMON_NOT_FOUND:
        return ForwardedAttribution(CapabilityState.DEGRADED, proof_gap)
    return ForwardedAttribution(CapabilityState.UNAVAILABLE, proof_gap)


def note_tailnet(
    *,
    enabled: bool,
    host: str,
    identity_trust: bool = False,
    loopback_only: bool = False,
    proof_gap: str | None = "",
    identity_config_unreadable: bool = False,
) -> None:
    """Record the tailnet path and forwarded attribution the gateway resolved at start.

    The path is ``available`` once Tailscale access is on and this machine's
    tailnet name resolved, or once identity trust with an allowlist is on;
    ``degraded`` when access is on but no name resolved.

    Forwarded attribution is weakened only when identity trust is on, the
    gateway listens on loopback alone (so ``tailscale serve`` is the only way
    in), and *proof_gap* says why the daemon proof cannot succeed here
    (``tailnet_proxy_proof.proof_support``): ``unavailable`` for a platform or
    a same-user daemon, ``degraded`` while no daemon is running. ``None`` means
    the probe has not answered yet: the row reads ``degraded`` with
    ``not_checked`` until :func:`note_proof_gap` records the answer.

    *identity_config_unreadable* says the tailnet login allowlist could not be
    read (``config.resolution.tailnet_identity_unknown``); ``crew_mode`` refuses
    team mode while it holds.
    """
    global _forwarded_attribution, _forwarded_inputs, _identity_config_unreadable
    answer = _tailnet_state(bool(enabled), str(host or ""), bool(identity_trust))
    gap = None if proof_gap is None else str(proof_gap)
    forwarded = _forwarded_state(bool(identity_trust), bool(loopback_only), gap)
    with _LOCK:
        _SOURCES[SETUP_TAILSCALE] = lambda: answer
        _forwarded_attribution = forwarded
        _forwarded_inputs = (bool(identity_trust), bool(loopback_only))
        _identity_config_unreadable = bool(identity_config_unreadable)


def note_proof_gap(proof_gap: str) -> None:
    """Record the proxy-proof answer the post-start probe found (``""`` when provable)."""
    global _forwarded_attribution
    with _LOCK:
        identity_trust, loopback_only = _forwarded_inputs
        _forwarded_attribution = _forwarded_state(identity_trust, loopback_only, str(proof_gap))


@dataclass(frozen=True)
class DirectTailnet:
    """The direct tailnet listener as the gateway started it.

    ``state`` is ``active``, ``off`` or ``unavailable``; ``reason`` says why it
    is not active; ``method`` is how the daemon was reached (a
    ``tailnet_localapi`` method name); ``urls`` are the plain share links.
    """

    state: str = "off"
    reason: str = "not_started"
    method: str = "none"
    urls: tuple[str, ...] = ()
    owner_login: str = ""
    addresses: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.state == "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "method": self.method,
            "urls": list(self.urls),
            "owner_login": self.owner_login,
            "addresses": list(self.addresses),
        }


_direct_tailnet = DirectTailnet()


def note_tailnet_direct(answer: DirectTailnet) -> None:
    """Record what the direct tailnet listener resolved to at gateway start."""
    global _direct_tailnet
    with _LOCK:
        _direct_tailnet = answer


def tailnet_direct() -> DirectTailnet:
    """The direct tailnet listener :func:`note_tailnet_direct` last recorded."""
    with _LOCK:
        return _direct_tailnet


def tailnet_identity_config_unreadable() -> bool:
    """Whether :func:`note_tailnet` last recorded an unreadable tailnet allowlist."""
    with _LOCK:
        return _identity_config_unreadable


def forwarded_attribution() -> ForwardedAttribution:
    """The forwarded attribution :func:`note_tailnet` last recorded."""
    with _LOCK:
        return _forwarded_attribution


def setup_paths_dict() -> dict[str, dict[str, str]]:
    """Each setup path's availability, ``not_in_this_build`` when no source speaks."""
    with _LOCK:
        sources = dict(_SOURCES)
    out: dict[str, dict[str, str]] = {}
    for path in SETUP_PATHS:
        source = sources.get(path)
        if source is None:
            reason = REASON_NOT_CONFIGURED if path == SETUP_TAILSCALE else REASON_NOT_IN_THIS_BUILD
            out[path] = SetupPathState(CapabilityState.UNAVAILABLE, reason).to_dict()
            continue
        try:
            answer = source()
            if not isinstance(answer, SetupPathState):
                raise TypeError("setup path source returned a non-state")
        except Exception:  # noqa: BLE001 -- a failed source claims nothing
            logger.warning("setup path source %s failed", path, exc_info=True)
            answer = SetupPathState(CapabilityState.UNAVAILABLE, REASON_SOURCE_FAILED)
        out[path] = answer.to_dict()
    if tailnet_direct().active:
        # The direct tailnet listener is itself a Tailscale sharing path.
        out[SETUP_TAILSCALE] = SetupPathState(CapabilityState.AVAILABLE).to_dict()
    return out


__all__ = [
    "FORWARDED_ATTRIBUTION_REASONS",
    "SETUP_PATHS",
    "DirectTailnet",
    "ForwardedAttribution",
    "SetupPathState",
    "forwarded_attribution",
    "note_proof_gap",
    "note_tailnet",
    "note_tailnet_direct",
    "register_setup_path_source",
    "setup_paths_dict",
    "tailnet_direct",
    "tailnet_identity_config_unreadable",
    "unregister_setup_path_source",
]
