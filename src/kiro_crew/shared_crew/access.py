"""Who may use which Shared Crew access mode, and what each mode needs.

Three access modes exist and each has its own bar:

* **shared multi-user access** lets several people work in one Crew. Which
  people are eligible is decided outside this build by an access-decision
  authority; no identity class is eligible or refused here, and a sign-in
  alone grants nothing;
* **personal remote control** lets one person reach their own machine. Any
  human identity class is eligible, and it stays off until the operator turns
  it on;
* **agent workload identity** lets the Crew act as itself. It needs a cloud
  account setup and a supported hosted container platform.

Shared access and workload identity need a cloud account setup; personal remote
control needs only an individual login. :func:`access_modes_dict` reports the
modes separately, so a client never reads one requirement as the other.

The People surface guides a person who has no shared-access path configured to
one of the setup paths in :data:`SETUP_PATHS`. Each path reports the
availability a source registered for it, and a path with no source reports
``not_in_this_build``. :func:`note_tailnet` is the Tailscale source: the gateway
calls it with the tailnet state it resolved at start.

Every answer here is computed from state already in memory or from the process
environment, so it is safe on the request path.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from kiro_crew.shared_crew.contracts import CapabilityState, Principal, PrincipalKind, Provenance

logger = logging.getLogger(__name__)


# ── identity class ──


class IdentityClass(str, enum.Enum):
    """How a human credential was established.

    Data, not policy: the class is recorded and shown, and an external access
    decision may read it, but no rule in this build allows or refuses shared
    access because of it.
    """

    #: An organization-managed identity backed by a directory.
    ORGANIZATION_DIRECTORY = "organization_directory"
    #: An individually authenticated consumer account.
    INDIVIDUAL = "individual"
    #: A tailnet user resolved by the Tailscale daemon.
    TAILNET = "tailnet"


#: The key a verified enterprise principal carries its class under, in ``extra``.
IDENTITY_CLASS_KEY = "identity_class"

_TAILNET_PROVENANCE = frozenset(
    {Provenance.TAILSCALE_SERVE, Provenance.TAILSCALE_DIRECT, Provenance.TAILSCALE_LISTENER}
)


def parse_identity_class(raw: object) -> IdentityClass:
    """*raw* as a class; anything unrecognised reads as ``individual``.

    The fallback is the least-privileged class, so a credential that states no
    class, or one this build does not know, is never treated as an
    organization identity.
    """
    try:
        return IdentityClass(raw)
    except ValueError:
        return IdentityClass.INDIVIDUAL


def identity_class_of(principal: Principal) -> IdentityClass:
    """The identity class a principal was established with.

    A tailnet principal is ``tailnet``. An enterprise principal carries the class
    its verifier read from the credential. Every other principal (the local
    operator, a principal with no stated class) reads as ``individual``.
    """
    if principal.provenance in _TAILNET_PROVENANCE:
        return IdentityClass.TAILNET
    if principal.provenance is Provenance.ENTERPRISE:
        return parse_identity_class(principal.extra.get(IDENTITY_CLASS_KEY))
    return IdentityClass.INDIVIDUAL


class EligibilityReason(str, enum.Enum):
    ELIGIBLE = "eligible"
    #: Shared access for a person is decided by an external access-decision
    #: authority; this build holds no eligibility rule of its own.
    EXTERNAL_DECISION_REQUIRED = "external_decision_required"
    #: The principal is not a person.
    NOT_A_PERSON = "not_a_person"


@dataclass(frozen=True)
class Eligibility:
    reason: EligibilityReason
    identity_class: IdentityClass

    @property
    def eligible(self) -> bool:
        return self.reason is EligibilityReason.ELIGIBLE


REMOTE_CONTROL_CLASSES = frozenset(IdentityClass)

#: Who decides shared multi-user eligibility. Reported in ``access_modes`` so a
#: client never reads an identity class as a verdict.
SHARED_ELIGIBILITY_DECIDED_BY = "external_authority"


def shared_access_eligibility(
    principal: Principal, identity_class: Optional[IdentityClass] = None
) -> Eligibility:
    """What this build can say about *principal* and shared multi-user access.

    Nothing beyond "a person needs an external decision": a person reads
    ``external_decision_required`` whatever their identity class, and anything
    else reads ``not_a_person``. The decision itself is
    ``enterprise.GrantGate.decide`` against a composed authority.
    """
    cls = identity_class if identity_class is not None else identity_class_of(principal)
    if principal.kind is not PrincipalKind.HUMAN:
        return Eligibility(EligibilityReason.NOT_A_PERSON, cls)
    return Eligibility(EligibilityReason.EXTERNAL_DECISION_REQUIRED, cls)


def remote_control_eligibility(
    principal: Principal, identity_class: Optional[IdentityClass] = None
) -> Eligibility:
    """Whether *principal* may remotely control their own machine.

    Every human identity class qualifies, an individual account included.
    This is the personal same-account path, separate from shared access.
    """
    cls = identity_class if identity_class is not None else identity_class_of(principal)
    if principal.kind is not PrincipalKind.HUMAN:
        return Eligibility(EligibilityReason.NOT_A_PERSON, cls)
    return Eligibility(EligibilityReason.ELIGIBLE, cls)


# ── access modes and their requirements ──

MODE_SHARED_MULTI_USER = "shared_multi_user"
MODE_PERSONAL_REMOTE_CONTROL = "personal_remote_control"
MODE_AGENT_WORKLOAD_IDENTITY = "agent_workload_identity"

REQUIRES_CLOUD_ACCOUNT = "cloud_account"
REQUIRES_INDIVIDUAL_LOGIN = "individual_login"

#: Personal remote control is opt-in: off until the operator turns it on.
REMOTE_CONTROL_DEFAULT_ENABLED = False


REASON_ELIGIBILITY_NOT_CONFIGURED = "not_configured"


def access_modes_dict(
    *,
    shared_state: CapabilityState,
    workload_state: CapabilityState,
    eligibility_state: CapabilityState = CapabilityState.UNAVAILABLE,
    eligibility_reason: str = REASON_ELIGIBILITY_NOT_CONFIGURED,
) -> dict[str, dict[str, Any]]:
    """The three access modes, each with its own requirement.

    ``shared_multi_user.state`` is the network setup path (Tailscale and the
    other setup paths). ``shared_multi_user.eligibility`` is the external
    access decision for people: ``unavailable`` with no authority composed,
    which means no person is granted shared access through it.
    """
    eligibility: dict[str, Any] = {
        "decided_by": SHARED_ELIGIBILITY_DECIDED_BY,
        "state": eligibility_state.value,
        "reason": "" if eligibility_state is CapabilityState.AVAILABLE else eligibility_reason,
    }
    return {
        MODE_SHARED_MULTI_USER: {
            "state": shared_state.value,
            "requires": REQUIRES_CLOUD_ACCOUNT,
            "eligibility": eligibility,
        },
        MODE_PERSONAL_REMOTE_CONTROL: {
            "state": CapabilityState.DISABLED.value,
            "requires": REQUIRES_INDIVIDUAL_LOGIN,
            "identity_classes": sorted(c.value for c in REMOTE_CONTROL_CLASSES),
            "default_enabled": REMOTE_CONTROL_DEFAULT_ENABLED,
        },
        MODE_AGENT_WORKLOAD_IDENTITY: {
            "state": workload_state.value,
            "requires": REQUIRES_CLOUD_ACCOUNT,
        },
    }


# ── workload identity host ──

HOST_EKS = "eks"
HOST_KUBERNETES = "kubernetes"
HOST_ECS = "ecs"
HOST_LOCAL = "local"

#: The hosted container platforms workload identity runs on. No other host, and
#: no static-key fallback, is supported.
SUPPORTED_WORKLOAD_HOSTS: tuple[str, ...] = (HOST_EKS,)

REASON_HOST_NOT_SUPPORTED = "host_not_supported"


def detect_workload_host(env: Optional[Mapping[str, str]] = None) -> str:
    """The container platform this process runs on, from its environment.

    EKS is a Kubernetes pod that was handed a projected workload identity (a web
    identity token file with a role, or the pod identity agent endpoint). A
    Kubernetes pod without one, an ECS task and a plain host are named so a
    caller can say which unsupported host it is on.
    """
    e = os.environ if env is None else env
    if e.get("KUBERNETES_SERVICE_HOST"):
        web_identity = e.get("AWS_WEB_IDENTITY_TOKEN_FILE") and e.get("AWS_ROLE_ARN")
        pod_identity = e.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        return HOST_EKS if web_identity or pod_identity else HOST_KUBERNETES
    if e.get("ECS_CONTAINER_METADATA_URI_V4") or e.get("ECS_CONTAINER_METADATA_URI"):
        return HOST_ECS
    return HOST_LOCAL


def workload_host_supported(host: str) -> bool:
    return host in SUPPORTED_WORKLOAD_HOSTS


#: The pod boundary workload identity actually has. Any process in an attested
#: pod can ask the workload API for the Crew's identity, so there is no
#: per-process isolation to claim; credential stripping and the sandbox are
#: what keep a tool from it, and they are unchanged.
POD_BOUNDARY: dict[str, Any] = {
    "scope": "any_process_in_pod",
    "per_process_isolation": False,
}


def workload_host_dict(host: str) -> dict[str, Any]:
    supported = workload_host_supported(host)
    return {
        "supported_hosts": list(SUPPORTED_WORKLOAD_HOSTS),
        "host": host,
        "host_supported": supported,
        "reason": "" if supported else REASON_HOST_NOT_SUPPORTED,
        "pod_boundary": dict(POD_BOUNDARY),
    }


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
    "IDENTITY_CLASS_KEY",
    "POD_BOUNDARY",
    "REMOTE_CONTROL_DEFAULT_ENABLED",
    "SHARED_ELIGIBILITY_DECIDED_BY",
    "SETUP_PATHS",
    "SUPPORTED_WORKLOAD_HOSTS",
    "Eligibility",
    "EligibilityReason",
    "DirectTailnet",
    "ForwardedAttribution",
    "IdentityClass",
    "SetupPathState",
    "access_modes_dict",
    "detect_workload_host",
    "forwarded_attribution",
    "note_proof_gap",
    "identity_class_of",
    "note_tailnet",
    "note_tailnet_direct",
    "parse_identity_class",
    "register_setup_path_source",
    "remote_control_eligibility",
    "setup_paths_dict",
    "shared_access_eligibility",
    "tailnet_direct",
    "tailnet_identity_config_unreadable",
    "unregister_setup_path_source",
    "workload_host_dict",
    "workload_host_supported",
]
