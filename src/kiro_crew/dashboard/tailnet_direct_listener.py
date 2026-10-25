"""Start the direct tailnet listener beside the loopback one, or record why not.

The gate is resolved before the middleware chain is built (the chain carries
it), and the sites are bound after the loopback listener is up, on the same
port. :func:`start` records the outcome for the Shared Crew capabilities and
adds the listener's own origins to the CSRF and ``Host`` allowlists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.config.resolution import tailnet_effective_allowed_logins, tailnet_identity_unknown
from kiro_crew.dashboard import tailnet, tailnet_direct
from kiro_crew.shared_crew import access

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Why the listener does not run when the gateway is not bound to loopback.
REASON_BIND_NOT_LOOPBACK = "bind_not_loopback"


def _install_access_set(cfg: KiroCrewConfig) -> None:
    """Publish Shared Crew's derived access set, seeding it once from the config list.

    An unreadable tailnet section seeds nothing (the seed is taken only from a
    list that was read), and a later start seeds from it then.
    """
    from kiro_crew.shared_crew import access_set

    unknown = tailnet_identity_unknown(cfg.degraded_sections)
    seed = None if unknown else tuple(cfg.dashboard.tailscale.allowed_logins)
    access_set.install(seed)


def _resolve_blocking(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    try:
        _install_access_set(cfg)
    except Exception:  # noqa: BLE001 - the set fails closed inside install
        logger.warning("shared crew access set could not be installed", exc_info=True)
    resolution = _resolve_gate(cfg, port, local_only)
    gate = resolution.gate
    if gate is not None:
        from kiro_crew.shared_crew import access_set

        access_set.set_owner(gate.owner.login, gate.owner.user_id)
    return resolution


def _resolve_gate(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    if not cfg.dashboard.shared_crew.tailnet_direct:
        return tailnet_direct.DirectResolution(
            tailnet_direct.STATE_OFF, tailnet_direct.REASON_DISABLED
        )
    if not local_only:
        # A widened bind already accepts tailnet traffic on its own socket;
        # the direct listener runs only beside the loopback-only default.
        return tailnet_direct.DirectResolution(
            tailnet_direct.STATE_UNAVAILABLE, REASON_BIND_NOT_LOOPBACK
        )
    ts = cfg.dashboard.tailscale
    return tailnet_direct.resolve(
        port=port,
        allowed_logins=tailnet_effective_allowed_logins(cfg.degraded_sections, ts.allowed_logins),
        identity_unknown=tailnet_identity_unknown(cfg.degraded_sections),
        governance_pinned=tailnet.is_governance_pinned_off(audit_tool="tailnet_direct_startup"),
    )


async def resolve(
    cfg: KiroCrewConfig, port: int, local_only: bool
) -> tailnet_direct.DirectResolution:
    """Decide, off the event loop, whether the direct listener runs. Never raises."""
    try:
        resolution = await asyncio.to_thread(_resolve_blocking, cfg, port, local_only)
    except Exception:  # noqa: BLE001 - the listener is an addition; its failure adds nothing
        logger.warning("tailnet direct listener could not be resolved", exc_info=True)
        resolution = tailnet_direct.DirectResolution(
            tailnet_direct.STATE_UNAVAILABLE, tailnet_direct.REASON_TAILSCALE_NOT_RUNNING
        )
    _note(resolution, ())
    if resolution.state != tailnet_direct.STATE_ACTIVE:
        logger.info(
            "tailnet direct listener %s (%s)", resolution.state, resolution.reason or "no reason"
        )
    return resolution


def _note(resolution: tailnet_direct.DirectResolution, urls: tuple[str, ...]) -> None:
    gate = resolution.gate
    access.note_tailnet_direct(
        access.DirectTailnet(
            state=resolution.state,
            reason=resolution.reason,
            method=resolution.method,
            urls=urls,
            owner_login=gate.owner.login if gate is not None else "",
            addresses=gate.owner.addresses if gate is not None else (),
        )
    )


async def start(
    app: web.Application,
    runner: web.AppRunner,
    bound_port: int,
    resolution: tailnet_direct.DirectResolution,
) -> list[web.BaseSite]:
    """Bind the direct sites for an active *resolution* and publish the result."""
    gate = resolution.gate
    if resolution.state != tailnet_direct.STATE_ACTIVE or gate is None:
        return []
    gate.port = bound_port
    sites = await tailnet_direct.start_sites(runner, bound_port, gate)
    if not sites:
        _note(
            tailnet_direct.DirectResolution(
                tailnet_direct.STATE_UNAVAILABLE,
                tailnet_direct.REASON_BIND_FAILED,
                gate=gate,
                method=resolution.method,
            ),
            (),
        )
        return []
    bound = {site.name for site in sites}
    origins = tailnet_direct.direct_origins(gate, bound_port)
    allowed = app.get("allowed_origins")
    if isinstance(allowed, set):
        allowed.update(origins)
    urls = tuple(tailnet_direct.direct_urls(gate, bound_port))
    _note(resolution, urls)
    logger.info("tailnet direct listener active on %s", ", ".join(sorted(bound)))
    return sites


__all__ = ["REASON_BIND_NOT_LOOPBACK", "resolve", "start"]
