"""What one warm advance of a SLOT fold costs, measured on a real store.

A slot fold is read over every unit its slot names, and an eager advance runs once per
append batch. So the price of one advance has to follow what ARRIVED -- the units that
grew and their new entries -- and never the number of units or the length of any of
them. Four costs would break that: opening every unit's log (a tail read each) to build
its mark; hashing a unit with no cut counter whole to prove its folded records intact;
walking such a unit from record 1 for want of a resume point; and folding every unit
cold when a conductor's board grows in the MIDDLE of its list (its own unit beside each
bound worker's), which it does on almost every append.

Each test here builds a slot on a temp data home, warms it, appends, and counts what
the next advance did: log opens, records decoded, whole-prefix digests, bytes read. It
also compares every warm answer with the fold from empty over the same bytes, because
an advance that got cheaper by being wrong is the one outcome worse than slow.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Any

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    GATEWAY,
    _isolated_home,
    _slot_log,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store

BOARD = "dashboard:board"
FILLER_PER_UNIT = 400
NOON = 1_767_441_600_000

#: Records one changed unit may decode per advance, whatever its length: the open's own
#: header and tail reads, the segment provenance checks, the record at the resume point,
#: and the new entries. Measured at about 11; a walk of one unit here is over 400.
PER_UNIT_DECODES = 16


@pytest.fixture(autouse=True)
def _ticking_clock():
    """Every append gets its own millisecond.

    A time-ordered fold refuses a continuation whose first new entry does not sort above
    what the cell folded, so two appends in one real millisecond would turn a measured
    warm advance into a cold fold and the bounds below into a race with the clock.
    """
    ticks = itertools.count(NOON)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(crew_log_store, "now_ms", lambda: next(ticks))
        yield


# --------------------------------------------------------------------------- #
# a slot on disk
# --------------------------------------------------------------------------- #


def _work(handle: CrewLog, item_id: str, *, actor: str = "conductor", **extra: Any) -> None:
    data: dict[str, Any] = {"slot": BOARD, "actor": actor, "by": BOARD, "item_id": item_id}
    if actor == "conductor":
        data.update({"action": "create", "title": f"item {item_id}"})
    else:
        data.update({"action": "report", "status": "progress", "summary": f"on {item_id}"})
    data.update(extra)
    handle.append("work/recorded", data, src=GATEWAY)


def _filler(handle: CrewLog, count: int) -> None:
    """Entries the work fold never reads, so a unit is long without moving the board."""
    handle.append_many(
        [
            {"type": "turn/started", "data": {"turn": n + 1, "actor": "user", "depth": 0}}
            for n in range(count)
        ],
        src=GATEWAY,
    )


def _board(units: int, *, counterless: bool = False) -> "list[str]":
    """A conductor unit followed by *units* - 1 worker units, each with its own slot.

    The order a ``work`` fold reads them in: the conductor's units, then each bound
    worker's. Every unit carries filler, so a whole read is visibly a whole read.
    """
    ids = [f"u-cond-{units}"] + [f"u-work-{units}-{n}" for n in range(1, units)]
    for index, unit in enumerate(ids):
        slot = BOARD if index == 0 else f"worker:{units}-{index}"
        handle = _slot_log(unit, slot)
        _filler(handle, FILLER_PER_UNIT)
        if index == 0:
            for n in range(units):
                _work(handle, f"it-{n}")
        else:
            _work(handle, f"it-{index}", actor="worker")
        if counterless:
            (handle.path.parent / crew_log_store._CUTS_FILE).unlink()
    return ids


def _handle(unit: str) -> CrewLog:
    return CrewLog.open(lg.KIND_SESSION, unit)


def _warm(name: str, units: "list[str]") -> "dict[str, Any]":
    return crew_log.fold_slot_warm(name, tuple(units), slot=BOARD).state


def _cold(name: str, units: "list[str]") -> "dict[str, Any]":
    return crew_log.fold_slot_checkpoint(name, tuple(units), slot=BOARD).state


def _rendered(name: str, state: "dict[str, Any]") -> "dict[str, Any]":
    return crew_log._FOLDS[name].render(state)


def _same_as_cold(name: str, units: "list[str]") -> None:
    warm = _rendered(name, _warm(name, units))
    assert warm == _rendered(name, _cold(name, units)), "the warm cell drifted from a cold fold"


# --------------------------------------------------------------------------- #
# what an advance did
# --------------------------------------------------------------------------- #


def _rchar() -> "int | None":
    try:
        text = Path("/proc/self/io").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("rchar:"):
            return int(line.split()[1])
    return None


class _Cost:
    """What the block inside it did: log opens, records decoded, prefix digests, bytes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opens = 0
        self.decoded = 0
        self.digests = 0
        self.bytes: "int | None" = None
        self._patch = monkeypatch
        real_open = CrewLog.open.__func__  # type: ignore[attr-defined]
        real_parse = crew_log_store._parses_to_object
        real_digest = CrewLog.raw_prefix_digest

        def _open(cls, *args, **kwargs):
            self.opens += 1
            return real_open(cls, *args, **kwargs)

        def _parse(blob):
            self.decoded += 1
            return real_parse(blob)

        def _digest(handle, records):
            self.digests += 1
            return real_digest(handle, records)

        monkeypatch.setattr(CrewLog, "open", classmethod(_open))
        monkeypatch.setattr(crew_log_store, "_parses_to_object", _parse)
        monkeypatch.setattr(CrewLog, "raw_prefix_digest", _digest)

    def __enter__(self) -> "_Cost":
        self.opens = self.decoded = self.digests = 0
        self._start = _rchar()
        return self

    def __exit__(self, *_exc: object) -> None:
        end = _rchar()
        self.bytes = None if self._start is None or end is None else end - self._start


def _log_bytes(units: "list[str]") -> int:
    return sum(os.path.getsize(_handle(unit).path) for unit in units)


# --------------------------------------------------------------------------- #
# appends in the middle of the list: the conductor's board
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", [8, 32])
def test_appends_to_one_middle_unit_open_only_that_unit(monkeypatch, width):
    """K appends to one worker unit in the middle: each advance opens O(1) logs in U.

    A grown middle unit must not mean a cold fold of every unit, and no advance may open
    every unit of the slot to build its marks.
    """
    units = _board(width)
    _same_as_cold("work", units)
    middle = _handle(units[width // 2])
    cost = _Cost(monkeypatch)
    for step in range(5):
        _work(middle, f"it-{width // 2}", actor="worker", summary=f"step {step}")
        with cost:
            _warm("work", units)
        assert cost.opens <= 3, f"one grown unit of {width} opened {cost.opens} logs"
        assert cost.decoded <= PER_UNIT_DECODES, f"one new entry decoded {cost.decoded} records"
        assert cost.digests == 0
    _same_as_cold("work", units)


def test_appends_to_several_units_and_a_new_unit_cost_what_changed(monkeypatch):
    """The conductor's own unit, two workers' and a NEW worker unit grow in one batch."""
    units = _board(24)
    _same_as_cold("work", units)
    cost = _Cost(monkeypatch)

    _work(_handle(units[0]), "it-new")  # the conductor: first in the list
    _work(_handle(units[5]), "it-5", actor="worker", summary="five again")
    _work(_handle(units[17]), "it-17", actor="worker", summary="seventeen again")
    fresh = _slot_log("u-work-24-fresh", "worker:24-fresh")
    _work(fresh, "it-new", actor="worker", summary="on the new item")
    grown = units + ["u-work-24-fresh"]
    with cost:
        _warm("work", grown)
    # Four units moved: each costs an open for its mark and one for its tail.
    assert cost.opens <= 4 * 3, f"four changed units cost {cost.opens} opens"
    assert cost.decoded <= 4 * PER_UNIT_DECODES, f"four new entries decoded {cost.decoded}"
    assert cost.digests == 0
    _same_as_cold("work", grown)
    state = _warm("work", grown)
    assert state["items"]["it-new"]["summary"] == "on the new item"


def test_a_new_unit_inserted_mid_list_is_picked_up(monkeypatch):
    """A conductor reset adds a conductor unit BEFORE the workers' units in the list."""
    units = _board(10)
    _same_as_cold("work", units)
    reset = _slot_log("u-cond-10-b", BOARD)
    _work(reset, "it-after-reset")
    grown = units[:1] + ["u-cond-10-b"] + units[1:]
    cost = _Cost(monkeypatch)
    with cost:
        _warm("work", grown)
    assert cost.decoded <= PER_UNIT_DECODES, f"one new unit decoded {cost.decoded} records"
    _same_as_cold("work", grown)
    assert "it-after-reset" in _warm("work", grown)["items"]


def test_a_unit_ordered_fold_still_folds_cold_on_middle_growth():
    """The ``ledger`` fold concatenates units; a middle append must not land at the end."""
    units = [f"u-ledger-{n}" for n in range(4)]
    for unit in units:
        handle = _slot_log(unit, BOARD)
        handle.append(
            "ledger/recorded",
            {"slot": BOARD, "goal": f"goal of {unit}", "event": unit, "event_kind": "progress"},
            src=GATEWAY,
        )
    _same_as_cold("ledger", units)
    _handle(units[1]).append(
        "ledger/recorded",
        {"slot": BOARD, "goal": "a late goal", "event": "late", "event_kind": "progress"},
        src=GATEWAY,
    )
    _same_as_cold("ledger", units)


# --------------------------------------------------------------------------- #
# a log with no cut counter
# --------------------------------------------------------------------------- #


def test_a_counterless_unit_is_never_hashed_whole_and_not_walked_from_the_top(monkeypatch):
    """Logs created before the counter existed: bounded window, resumed walk."""
    units = _board(12, counterless=True)
    _same_as_cold("work", units)
    middle = _handle(units[6])
    cost = _Cost(monkeypatch)
    for step in range(5):
        _work(middle, "it-6", actor="worker", summary=f"step {step}")
        with cost:
            _warm("work", units)
        assert cost.digests == 0, "a counterless unit was hashed whole"
        assert cost.decoded <= PER_UNIT_DECODES, f"the walk restarted: {cost.decoded} decoded"
        assert cost.opens <= 4
    _same_as_cold("work", units)


def test_bytes_read_per_advance_follow_new_entries_not_the_logs(monkeypatch):
    """The read side measured in bytes: a constant per changed unit, not the slot."""
    if _rchar() is None:
        pytest.skip("this platform does not report bytes read per process")
    units = _board(32, counterless=True)
    _same_as_cold("work", units)
    total = _log_bytes(units)
    middle = _handle(units[16])
    cost = _Cost(monkeypatch)
    worst = 0
    for step in range(5):
        _work(middle, "it-16", actor="worker", summary=f"step {step}")
        with cost:
            _warm("work", units)
        assert cost.bytes is not None
        worst = max(worst, cost.bytes)
    # One changed unit: its tail read on open (<= 72 KiB) plus the sealed window and the
    # new record. A whole read of the slot is many times this.
    assert total > 1_000_000, f"the slot is too small to tell a whole read apart: {total}"
    assert worst < 400_000, f"one advance read {worst} bytes of a {total}-byte slot"


# --------------------------------------------------------------------------- #
# what a continuation must still refuse
# --------------------------------------------------------------------------- #


def _last_line_rewritten(unit: str, old: str, new: str) -> None:
    path = _handle(unit).path
    data = path.read_bytes()
    head, _sep, last = data.rstrip(b"\n").rpartition(b"\n")
    assert old.encode() in last
    path.write_bytes(head + b"\n" + last.replace(old.encode(), new.encode()) + b"\n")


@pytest.mark.parametrize("counterless", [False, True])
def test_a_rewritten_folded_record_in_a_middle_unit_is_refolded(counterless):
    units = _board(6, counterless=counterless)
    middle = units[3]
    _work(_handle(middle), "it-3", actor="worker", summary="ORIGINAL")
    _same_as_cold("work", units)
    _last_line_rewritten(middle, "ORIGINAL", "REWRITTEN-LONGER")
    if not counterless:
        crew_log_store._open_cut(_handle(middle).path.parent)
        crew_log_store._close_cut(_handle(middle).path.parent)
    _work(_handle(middle), "it-3", actor="worker", summary="after")
    _same_as_cold("work", units)


@pytest.mark.parametrize("counterless", [False, True])
def test_a_truncated_and_regrown_middle_unit_is_refolded(counterless):
    """Cut back past folded records, then grown above the old height again."""
    units = _board(6, counterless=counterless)
    middle = units[2]
    _work(_handle(middle), "it-2", actor="worker", summary="KEPT")
    _work(_handle(middle), "it-2", actor="worker", summary="CUT-AWAY")
    _same_as_cold("work", units)
    path = _handle(middle).path
    data = path.read_bytes().rstrip(b"\n")
    path.write_bytes(data.rpartition(b"\n")[0] + b"\n")
    if not counterless:
        crew_log_store._open_cut(path.parent)
        crew_log_store._close_cut(path.parent)
    handle = _handle(middle)
    _work(handle, "it-2", actor="worker", summary="REGROWN-ONE")
    _work(handle, "it-2", actor="worker", summary="REGROWN-TWO")
    _same_as_cold("work", units)
    assert _warm("work", units)["items"]["it-2"]["summary"] == "REGROWN-TWO"


def test_a_replaced_middle_unit_is_refolded():
    """Removed and recreated under the same id: a new origin, so nothing carries over."""
    units = _board(6)
    _same_as_cold("work", units)
    middle = units[4]
    directory = _handle(middle).path.parent
    for child in sorted(directory.iterdir()):
        child.unlink()
    directory.rmdir()
    handle = _slot_log(middle, "worker:6-4")
    _filler(handle, FILLER_PER_UNIT + 5)
    _work(handle, "it-4", actor="worker", summary="a new life")
    _same_as_cold("work", units)
    assert _warm("work", units)["items"]["it-4"]["summary"] == "a new life"


def test_a_unit_that_left_the_list_is_refolded():
    units = _board(6)
    _same_as_cold("work", units)
    _same_as_cold("work", units[:2] + units[3:])


def test_a_middle_entry_stamped_before_what_the_cell_folded_folds_cold(monkeypatch):
    """A clock that stepped back makes a new entry sort below the cell: refold, not append."""
    units = _board(6)
    _same_as_cold("work", units)
    monkeypatch.setattr(crew_log_store, "now_ms", lambda: 1_000)
    _work(_handle(units[3]), "it-3", actor="worker", summary="from the past")
    _same_as_cold("work", units)


def test_a_cell_resumed_from_a_savepoint_continues_from_its_tail_point(monkeypatch):
    """A cell that stood on a savepoint carries the point its tail walk sealed.

    Without it the first advance after a resume walks the newest unit from record 1, and
    on a unit with no counter it has no evidence at all, folds cold, finds the same
    savepoint again and resumes -- so every advance pays the savepoint's prefix check.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    units = ["u-resume-a", "u-resume-b"]
    for unit in units:
        _slot_log(unit, BOARD)
    newest = _handle(units[-1])
    _filler(newest, FILLER_PER_UNIT)
    for step in range(savepoints.MIN_ADVANCE_ENTRIES + 4):
        newest.append(
            "ledger/recorded",
            {"slot": BOARD, "event": f"e{step}", "event_kind": "progress"},
            src=GATEWAY,
        )
    _same_as_cold("ledger", units)
    assert savepoints.slot_checkpoint_path(lg.KIND_SESSION, units[-1], "ledger").exists()
    crew_log.forget_slot_folds()
    newest.append(
        "ledger/recorded", {"slot": BOARD, "event": "tail", "event_kind": "progress"}, src=GATEWAY
    )
    _warm("ledger", units)  # resumes from the savepoint, folding the one-entry tail

    cost = _Cost(monkeypatch)
    for step in range(3):
        newest.append(
            "ledger/recorded",
            {"slot": BOARD, "event": f"after {step}", "event_kind": "progress"},
            src=GATEWAY,
        )
        with cost:
            _warm("ledger", units)
        assert cost.decoded <= PER_UNIT_DECODES, f"advance {step} decoded {cost.decoded}"
    _same_as_cold("ledger", units)


def test_an_append_landing_mid_pass_does_not_fold_the_next_pass_cold(monkeypatch):
    """The eager thread races the writer: an append lands after the sample, before the walk.

    That pass folds the extra entry, so its mark is above the sampled one while its
    fingerprint is the sampled one. Read as "files moved at the same height", the next
    pass would take it for an in-place rewrite and fold every unit cold, on every busy
    log.
    """
    units = _board(8)
    _same_as_cold("work", units)
    middle = _handle(units[4])
    real_marks = crew_log._unit_marks
    landed: "list[bool]" = []

    def _sample_then_append(unit_ids):
        sampled = real_marks(unit_ids)
        if not landed:
            landed.append(True)
            _work(middle, "it-4", actor="worker", summary="landed mid-pass")
        return sampled

    _work(middle, "it-4", actor="worker", summary="before the pass")
    monkeypatch.setattr(crew_log, "_unit_marks", _sample_then_append)
    _warm("work", units)
    monkeypatch.setattr(crew_log, "_unit_marks", real_marks)

    cost = _Cost(monkeypatch)
    with cost:
        state = _warm("work", units)
    assert state["items"]["it-4"]["summary"] == "landed mid-pass"
    assert cost.decoded <= PER_UNIT_DECODES, f"the next pass folded cold: {cost.decoded}"
    _same_as_cold("work", units)


def test_a_skipped_tail_in_a_middle_unit_is_not_read_as_growth(monkeypatch):
    """A unit whose last records are an ignorable type this reader does not know.

    The walk yields nothing for them, so the folded height sits below the file's newest
    seq. Taken as growth, that unit would be re-read on every pass, and in a unit-ordered
    fold a middle unit that "grew" would send every idle pass back to a cold fold.
    """
    from kiro_crew.crew_log import projection_slot_memo

    units = [f"u-skip-{n}" for n in range(4)]
    for unit in units:
        handle = _slot_log(unit, BOARD)
        _filler(handle, FILLER_PER_UNIT)
        handle.append(
            "ledger/recorded",
            {"slot": BOARD, "event": unit, "event_kind": "progress"},
            src=GATEWAY,
        )
    _handle(units[1]).append_many(
        [
            {
                "type": "work/recorded",
                "data": {
                    "slot": BOARD,
                    "actor": "conductor",
                    "by": BOARD,
                    "action": "create",
                    "item_id": "x",
                    "title": "unknown here",
                },
                "ignorable": True,
            }
        ],
        src=GATEWAY,
    )
    known = frozenset(projection_slot_memo.KNOWN_TYPES - {"work/recorded"})
    monkeypatch.setattr(projection_slot_memo, "KNOWN_TYPES", known)
    _warm("ledger", units)
    cost = _Cost(monkeypatch)
    for _ in range(3):
        with cost:
            _warm("ledger", units)
        assert cost.decoded <= PER_UNIT_DECODES, f"an idle pass decoded {cost.decoded}"


# --------------------------------------------------------------------------- #
# what the time merge must keep: whose spend, which board
# --------------------------------------------------------------------------- #


def _opened(handle: CrewLog, slot: str) -> None:
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )


def _closed(handle: CrewLog, turn: int, credits: float) -> None:
    handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append(
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
        src=GATEWAY,
    )


def test_interleaved_units_keep_each_charge_with_its_own_session():
    """``workstreams`` charges a closer to the session its unit opened, across lanes.

    The lanes interleave by time: the conductor opens, the worker opens, the worker's
    turn closes, then the conductor's. A single cursor would credit the conductor's
    charge to the worker, whose opener was merged last.
    """
    name = "workstreams"
    worker_slot = "worker:spend"
    conductor = _slot_log("u-spend-cond", BOARD)
    worker = _slot_log("u-spend-work", worker_slot)
    _opened(conductor, BOARD)
    _opened(worker, worker_slot)
    _closed(worker, 1, 2.0)
    _closed(conductor, 1, 5.0)
    units = ["u-spend-cond", "u-spend-work"]

    for state in (_cold(name, units), _warm(name, units)):
        assert state["spend"][BOARD]["credits"] == 5.0
        assert state["spend"][worker_slot]["credits"] == 2.0

    _closed(worker, 2, 3.0)
    _closed(conductor, 2, 7.0)
    warm = _warm(name, units)
    assert warm["spend"][BOARD]["credits"] == 12.0
    assert warm["spend"][worker_slot]["credits"] == 5.0
    _same_as_cold(name, units)


def test_a_report_on_the_next_board_stamped_before_its_create_is_kept(monkeypatch):
    """A clock step puts a worker's report on board B before the conductor starts B.

    The report is held until the conductor entry that starts its generation arrives,
    then applied; a straggler naming an ENDED board is still dropped.
    """
    conductor = _slot_log("u-gen-cond", BOARD)
    worker = _slot_log("u-gen-work", "worker:gen")
    clock = {"now": 1_000}
    monkeypatch.setattr(crew_log_store, "now_ms", lambda: clock["now"])

    def at(stamp: int) -> None:
        clock["now"] = stamp

    at(1_000)
    _work(conductor, "it-a", generation="gen-a")
    at(3_000)
    _work(conductor, "it-b", generation="gen-b")
    at(2_000)  # the clock stepped back
    _work(worker, "it-b", actor="worker", generation="gen-b", status="done", summary="did b")
    at(4_000)
    _work(worker, "it-a", actor="worker", generation="gen-a", summary="late, board a ended")
    units = ["u-gen-cond", "u-gen-work"]

    for state in (_cold("work", units), _warm("work", units)):
        assert state["generation"] == "gen-b"
        assert state["items"]["it-b"]["status"] == "done"
        assert state["items"]["it-b"]["summary"] == "did b"
        assert "it-a" not in state["items"]
    _same_as_cold("work", units)


@pytest.mark.parametrize("counterless", [False, True])
def test_a_long_lane_is_merged_in_bounded_batches(monkeypatch, counterless):
    """The merge holds at most a batch per lane, resuming each batch where the last stopped.

    With a batch of three, a worker lane of forty reports is read in many batches, and
    the value is still the fold from empty over the same bytes, warm and cold.
    """
    from kiro_crew.crew_log import projection_slot_memo

    monkeypatch.setattr(projection_slot_memo, "MERGE_BATCH", 3)
    units = _board(4, counterless=counterless)
    worker = _handle(units[2])
    for step in range(40):
        _work(worker, "it-2", actor="worker", summary=f"report {step}")
    cost = _Cost(monkeypatch)
    with cost:
        _same_as_cold("work", units)
    # Each batch reopens the unit, so a lane of 41 kept entries opened its log repeatedly.
    assert cost.opens > 10, f"the long lane was read in one go ({cost.opens} opens)"
    assert cost.digests == 0
    assert _warm("work", units)["items"]["it-2"]["summary"] == "report 39"
