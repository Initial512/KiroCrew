"""Which Slack channels Crew may use: ``channels.posture.slack.allowed_channel_ids``.

A policy names the channel ids in the ``slack`` member's posture (RFC
``rfc-crew-enterprise-edition``, appendix row "Slack channels"), e.g.
``{"channels": {"members": {"mode": "allow", "allow": ["slack"]}, "posture":
{"slack": {"allowed_channel_ids": {"mode": "allow", "allow": ["C0123", "D*"]}}}}}``.
The identifier matcher takes globs, so ``D*`` admits every direct message.

:func:`channel_refusal` is asked where a message comes in (``slack.events``, before
the turn is dispatched; a pure ``!stop`` stays exempt) and where Crew writes to a
channel (the real client's ``post_message``, ``post_blocks``, ``post_ephemeral``,
``upload_file``, ``update_message`` and the stream start, append and stop), so a
channel withdrawn mid-turn gets no further content. A refused write is audited. It reads the installed ceiling only, with no file I/O, so it is
safe on the event loop. The posture is policy-only, so no profile is asked.

With no policy, or a policy without the leaf, an ``oss`` build admits every channel
as before. An enterprise build admits none until the policy names the leaf (the
appendix minimal set is empty), and refuses on an evaluation error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The ``slack`` posture leaf that lists the channel ids.
LEAF = "allowed_channel_ids"


class SlackChannelRefused(PermissionError):
    """Raised by a Slack client write to a channel the policy does not admit."""


def _leaf_named(ceiling: object) -> bool:
    """Whether the policy level names :data:`LEAF` for the ``slack`` member."""
    from kiro_crew.platform.governance import ScopedMap, policy_level_control

    control = policy_level_control(ceiling, "channels")  # type: ignore[arg-type]
    if not isinstance(control, ScopedMap):
        return False
    return LEAF in control.posture.get("slack", {})


def channel_refusal(channel: str) -> str:
    """Why the policy refuses Slack *channel*; ``""`` when it admits it.

    In an ``oss`` build a ``PlatformCompositionError`` propagates, as at every
    governance chokepoint; an enterprise build refuses instead.
    """
    from kiro_crew.edition_locks import UNNAMED_REASON, locked_off
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    enterprise = locked_off()
    try:
        from kiro_crew.platform.governance import resolve

        ceiling = getattr(current_context(), "governance", None)
        if ceiling is None:
            return f"slack channels {UNNAMED_REASON}" if enterprise else ""
        decision = resolve(ceiling, None, "channels", f"slack/{LEAF}:{channel}")
        if not decision.permitted:
            return f"slack channel {channel!r} refused by the security policy"
        if enterprise and not _leaf_named(ceiling):
            return f"slack channels {UNNAMED_REASON}"
        return ""
    except PlatformCompositionError:
        if enterprise:
            return f"slack channels {UNNAMED_REASON}"
        raise
    except Exception:
        logger.debug("slack channel governance unavailable", exc_info=True)
        return "slack channel governance unavailable (fail-closed)" if enterprise else ""


def require_channel(channel: str) -> None:
    """Raise :class:`SlackChannelRefused` when the policy refuses *channel*."""
    reason = channel_refusal(channel)
    if reason:
        logger.warning("Slack write refused: %s", reason)
        try:
            # Looked up at call time: the suite patches ``kiro_crew.sel.sel``.
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller="slack_client",
                operation="slack.write",
                outcome="denied",
                source="slack",
                resources=f"channel={channel}",
                error="channel_not_allowed_by_policy",
            )
        except Exception:
            logger.debug("slack write refusal audit failed", exc_info=True)
        raise SlackChannelRefused(reason)
