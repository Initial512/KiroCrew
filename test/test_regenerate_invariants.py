"""The Regenerate contract, tested as invariants over adversarial inputs.

I1  A reply carries at most ``_MAX_VARIANTS`` versions, the new one included.
I2  The version on screen when Regenerate was pressed (its text AND its
    redaction records) survives as exactly one of the earlier versions; a
    version that only shares its text is a different version and is never
    dropped as a duplicate; the earlier versions keep their order.
I3  At the window cap, no row a swap retires is credited to the frozen prefix,
    and every row reaches disk once.
I4  A Regenerate pressed while a swap is in flight is refused as busy, and one
    pressed after it works on the swapped reply.
I5  A Regenerate that ends without a reply (refused, cancelled, failed write,
    gateway gone mid-swap) leaves the previous reply and its versions as they were.

Inputs: duplicate texts, equal text with different blocked-link records, empty text, the cap boundary +/-1, a concurrent and a
cancelled Regenerate, a slash command with a fixed answer, reload mid-swap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from hypothesis import given, settings
from hypothesis import strategies as st
from test_regenerate_swap import (
    _arm,
    _disk,
    _disk_rows,
    _regenerate_through_the_runner,
    _run_chat_state,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _save_slot_to_history
from kiro_crew.dashboard.chat_regenerate import (
    _MAX_VARIANTS,
    cap_variants_keeping,
    same_version,
)
from kiro_crew.dashboard.chat_turn.segment_flush import _adopt_pending_variants
from kiro_crew.security.exfil import redact_exfiltration_urls_with_records
from kiro_crew.testing.wait import await_until

# Duplicates, the empty string and a command's fixed answer, so every list the
# strategy draws can collide with the shown text.
_TEXTS = st.sampled_from(["", "A", "B", "C", "No prompts found."])


def _records(path: str) -> list[dict]:
    """Real blocked-link records for a long-query link on one host. Two paths
    redact to the same text, so only these records tell the answers apart."""
    out = redact_exfiltration_urls_with_records(
        f"see https://evil.example.com/{path}?d={'a' * 300}"
    )
    return out[-1]


_LINK_RECORDS = {"one": _records("one"), "two": _records("two")}

#: Blocked-link records: absent, or one of two sets that redact to the same
#: text, so two versions can share their text and still be different answers.
_LINKS = st.sampled_from([None, "one", "two"])


def _version(content: str, link: str | None, pos: int | None = None) -> dict:
    entry: dict = {"content": content}
    if link is not None:
        entry["blocked_links"] = _LINK_RECORDS[link]
    if pos is not None:
        entry["pos"] = pos
    return entry


def _is_same(a: dict, b: dict) -> bool:
    """The test's own oracle for "the same answer": text and blocked-link
    records, compared directly, so the property does not grade
    ``same_version`` with itself."""
    return a.get("content") == b.get("content") and a.get("blocked_links") == b.get("blocked_links")


def _stash(variants: list[dict], shown: dict, shown_idx: int | None) -> list[dict]:
    """The endpoint's stash: the shown version added when missing, then capped."""
    stash = list(variants)
    if not any(same_version(v, shown) for v in stash):
        stash.append(shown)
    return cap_variants_keeping(stash, shown, shown_idx)


@settings(max_examples=2000, deadline=None, database=None)
@given(
    # Lengths from empty to well past the cap, the boundary +/-1 included.
    entries=st.lists(st.tuples(_TEXTS, _LINKS), min_size=0, max_size=_MAX_VARIANTS + 6),
    shown=st.tuples(_TEXTS, _LINKS),
    shown_idx=st.one_of(st.none(), st.integers(min_value=-2, max_value=_MAX_VARIANTS + 8)),
    new=_TEXTS,
)
def test_i1_i2_the_version_list_is_bounded_and_keeps_the_shown_reply_once(
    entries: list[tuple[str, str | None]],
    shown: tuple[str, str | None],
    shown_idx: int | None,
    new: str,
) -> None:
    variants = [_version(c, link, pos) for pos, (c, link) in enumerate(entries)]
    on_screen = _version(*shown)
    stash = _stash(variants, on_screen, shown_idx)
    # The turn's end appends the new reply to the stash.
    settled = _adopt_pending_variants(SimpleNamespace(_pending_variants=stash), {"content": new})
    assert len(settled) <= _MAX_VARIANTS  # I1
    earlier = settled[:-1]
    # I2: exactly one earlier version IS the one on screen (text and records).
    assert sum(1 for v in earlier if _is_same(v, on_screen)) == 1
    order = [v["pos"] for v in earlier if "pos" in v]
    assert order == sorted(order)  # I2: order kept
    # A version that only shares the shown text is a different version: it is
    # dropped only as an oldest-first excess, never as a duplicate.
    others = [v for v in variants if not _is_same(v, on_screen)]
    kept_others = [v for v in earlier if not _is_same(v, on_screen)]
    assert kept_others == others[len(others) - len(kept_others) :]
    # Nothing is evicted that the bound did not require.
    assert len(earlier) == min(len(others) + 1, _MAX_VARIANTS - 1)
    # The version at the shown index is the copy the stash keeps, when it is
    # that version (checked on the stash: the settle step rebuilds entries).
    if (
        shown_idx is not None
        and 0 <= shown_idx < len(variants)
        and _is_same(variants[shown_idx], on_screen)
    ):
        assert any(v is variants[shown_idx] for v in stash)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_i1_at_the_cap_boundary(extra: int) -> None:
    """Exactly at, one under and one over the cap, distinct texts throughout."""
    n = _MAX_VARIANTS + extra
    variants = [{"content": f"v{i}"} for i in range(n)]
    stash = cap_variants_keeping(variants, {"content": "v0"})
    assert len(stash) == min(n, _MAX_VARIANTS - 1)
    assert stash[0]["content"] == "v0"


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_delta", [-1, 0, 1])
@pytest.mark.parametrize("appends_during_write", [0, 1, 2])
async def test_i3_no_retired_row_is_credited_at_the_window_cap(
    state, cap_delta: int, appends_during_write: int
) -> None:
    """The window sits one under, at, or one over the cap while rows land during
    the replace write: the old reply never comes back and no row is written twice."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    cap = len(slot.messages) + cap_delta
    real = chat_runner.save_slot_off_loop

    async def _append_during_the_write(*args, **kwargs):
        for i in range(appends_during_write):
            slot.append("assistant", f"late {i}")
        return await real(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", cap),
        patch.object(chat_runner, "save_slot_off_loop", new=_append_during_the_write),
    ):
        await chat_runner.settle_regenerate(state, slot)
        assert _save_slot_to_history(state, slot, force=True)
        for i in range(3):
            slot.append("assistant", f"after {i}")
            assert _save_slot_to_history(state, slot, force=True)
    disk = _disk(state, slot)
    assert "old" not in disk
    assert len(disk) == len(set(disk))
    expected = ["hi", "new", *[f"late {i}" for i in range(appends_during_write)]]
    assert disk == [*expected, *[f"after {i}" for i in range(3)]]


@pytest.mark.asyncio
async def test_i4_a_regenerate_during_the_swap_is_refused_and_after_it_works(
    tmp_path, monkeypatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from chat_regenerate_cov80_helpers import _make_regen_app
    from test_regenerate_swap import _client_streaming

    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    events = [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="fresh"),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state.sessions.get_or_create = AsyncMock(return_value=(_client_streaming(events), True, False))
    in_write = asyncio.Event()
    release = asyncio.Event()
    real = chat_runner.save_slot_off_loop

    async def _held(*args, **kwargs):
        if kwargs.get("retired_rows"):
            in_write.set()
            # Bounded: a test failing before it releases the barrier fails the
            # held write at a named deadline instead of suspending the turn.
            await asyncio.wait_for(release.wait(), 30)
        return await real(*args, **kwargs)

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
            new=AsyncMock(return_value=None),
        ),
        patch.object(chat_runner, "save_slot_off_loop", new=_held),
    ):
        async with TestClient(TestServer(_make_regen_app(state))) as client:
            assert (await client.post("/api/chat/slots/s1/regenerate")).status == 200
            try:
                await await_until(in_write, what="the swap's held write")
                assert slot._swap_in_flight
                during = await client.post("/api/chat/slots/s1/regenerate")
                assert during.status == 409
                # Either busy guard: the turn still owns the slot while it settles.
                assert (await during.json())["code"] in {"slot_running", "slot_busy"}
            finally:
                # Released and drained on every path, so a failed assertion
                # above cannot leave the turn suspended mid-swap.
                release.set()
                await asyncio.wait_for(slot.task, 30)
            # After the swap the reply is "fresh" with versions [old, fresh];
            # the next Regenerate stashes exactly those, built from the swapped row.
            seen: list[str] = []

            async def _capture(*a, **kw):
                seen.extend(v["content"] for v in slot._pending_variants)

            with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
                assert (await client.post("/api/chat/slots/s1/regenerate")).status == 200
                await asyncio.sleep(0)
    assert seen == ["old", "fresh"]


@pytest.mark.asyncio
async def test_i5_a_cancelled_regenerate_leaves_the_reply_and_its_versions(state) -> None:
    slot = _arm(state)
    slot.messages[-1]["variants"] = [{"content": "older"}, {"content": "old"}]
    before = [dict(m) for m in slot.messages]
    target = slot._regenerate_target
    release = chat_runner.release_regenerate_on_done(slot, target)
    cancelled = asyncio.get_running_loop().create_future()
    cancelled.cancel()
    release(cancelled)
    assert slot._regenerate_target is None and slot._pending_variants == []
    assert slot.messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False])
async def test_i5_reload_mid_swap_reads_one_whole_transcript(state, committed: bool) -> None:
    """A gateway gone mid-swap reloads from disk: before the replace write commits
    that is the old reply untouched, after it the new reply with its versions,
    never a mix."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    real = chat_runner.save_slot_off_loop
    on_disk: list = []

    async def _crash_around_the_write(*args, **kwargs):
        if not committed:
            on_disk.extend(_disk_rows(state, slot))
            raise OSError("gateway gone")
        result = await real(*args, **kwargs)
        on_disk.extend(_disk_rows(state, slot))
        return result

    with patch.object(chat_runner, "save_slot_off_loop", new=_crash_around_the_write):
        await chat_runner.settle_regenerate(state, slot)
    contents = [r["content"] for r in on_disk]
    if committed:
        assert contents == ["hi", "new"]
        assert [v["content"] for v in on_disk[-1]["variants"]] == ["old", "new"]
    else:
        assert contents == ["hi", "old", "new"]
        assert "variants" not in on_disk[-1]


@pytest.mark.asyncio
async def test_i1_i2_a_fixed_answer_regenerated_past_the_cap(tmp_path, monkeypatch) -> None:
    """``/prompts`` with none set up answers the same text every time; regenerating
    it past the cap through the real endpoint and runner keeps one earlier copy."""
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "/prompts")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    with patch.object(chat_runner, "_list_aim_prompts", return_value=[]):
        for _ in range(_MAX_VARIANTS + 2):
            shown = [m for m in slot.messages if m.get("role") == "assistant"][0]["content"]
            await _regenerate_through_the_runner(
                state, slot, [LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")]
            )
            reply = [m for m in slot.messages if m.get("role") == "assistant"]
            assert len(reply) == 1
            versions = [v["content"] for v in reply[0]["variants"]]
            assert len(versions) <= _MAX_VARIANTS
            # The reply that was on screen at the press is one earlier version.
            assert versions[:-1].count(shown) == 1
    # The answer that was there first is still one switch away.
    assert versions[0] == "old"
