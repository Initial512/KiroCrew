"""The slot index answers a repeated read from one ``stat`` of the unit root.

A data home holds a few thousand session units, and every fold that asks which units a
slot owns reaches ``session_units_by_slot``, so the listing must not be paid per read.
These tests pin the bound and the correctness it must keep: a repeated call on an unchanged root lists nothing; a unit
created or removed is seen on the next call, by this process or another one; and two
creates landing inside one filesystem timestamp tick are both seen, because a listing
whose root changed inside the racy window is never trusted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.crew_log import store, store_units
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

SLOT = "chat-cache"
#: A window short enough for a test to wait out, long enough to cover a coarse kernel tick.
_SHORT_WINDOW_NS = 20_000_000


def _root() -> Path:
    root = store.crew_log_root(KIND_SESSION)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _create(unit_id: str, slot: str = SLOT) -> None:
    CrewLog.create(KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _wait_out_window() -> None:
    time.sleep(_SHORT_WINDOW_NS * 3 / 1e9)


@pytest.fixture
def listings(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every listing of the session unit root, by either spelling, while the test runs."""
    root = _root()
    seen: list[str] = []
    real_listdir = os.listdir
    real_iterdir = Path.iterdir

    def counting_listdir(path: "os.PathLike[str] | str" = ".") -> list[str]:
        if Path(path) == root:
            seen.append("listdir")
        return real_listdir(path)

    def counting_iterdir(self: Path):  # type: ignore[no-untyped-def]
        if self == root:
            seen.append("iterdir")
        return real_iterdir(self)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    monkeypatch.setattr(store, "_slot_index", None)
    monkeypatch.setattr(store_units, "_RACY_WINDOW_NS", _SHORT_WINDOW_NS)
    store_units._SLOT_INDEX_TRUST[0] = None
    yield seen
    store_units._SLOT_INDEX_TRUST[0] = None


def test_repeated_reads_of_an_unchanged_root_list_nothing_after_the_first(
    listings: list[str],
) -> None:
    for index in range(5):
        _create(f"acp-cache-{index}")
    _wait_out_window()

    first = store.session_units_by_slot()
    assert len(first[SLOT]) == 5
    assert len(listings) == 1, "the first read lists the root once"

    for _ in range(50):
        assert store.session_units_by_slot() == first
        assert store.session_units_for_slot(SLOT) == first[SLOT]
    assert len(listings) == 1, "a read on an unchanged, settled root must not list it again"


def test_a_root_changed_inside_the_racy_window_is_listed_again(
    listings: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A window no slow runner can outlast between the create and the reads, so "the
    # root changed just now" holds for both calls and neither listing may be trusted.
    monkeypatch.setattr(store_units, "_RACY_WINDOW_NS", 600 * 1_000_000_000)
    _create("acp-racy-a")
    store.session_units_by_slot()
    store.session_units_by_slot()
    assert len(listings) == 2


def test_a_created_unit_is_seen_on_the_next_read(listings: list[str]) -> None:
    _create("acp-before")
    _wait_out_window()
    assert store.session_units_for_slot(SLOT) == ("acp-before",)
    assert store.session_units_for_slot(SLOT) == ("acp-before",)

    _create("acp-after")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-before", "acp-after"}


def test_a_removed_unit_is_gone_on_the_next_read(listings: list[str]) -> None:
    _create("acp-keep")
    _create("acp-drop")
    _wait_out_window()
    assert set(store.session_units_for_slot(SLOT)) == {"acp-keep", "acp-drop"}
    assert set(store.session_units_for_slot(SLOT)) == {"acp-keep", "acp-drop"}

    status = store.remove_unit(KIND_SESSION, "acp-drop", guard=lambda _path: True)
    assert status == store.REMOVE_REMOVED
    assert store.session_units_for_slot(SLOT) == ("acp-keep",)


def test_two_creates_in_one_tick_are_both_seen(listings: list[str]) -> None:
    _create("acp-tick-a")
    assert store.session_units_for_slot(SLOT) == ("acp-tick-a",)
    _create("acp-tick-b")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-tick-a", "acp-tick-b"}


def test_two_creates_on_a_filesystem_whose_stamps_did_not_move_are_both_seen(
    listings: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coarse-timestamp filesystem: the second create leaves every stat field equal.

    The stat is frozen at a stamp inside the racy window, which is what such a
    filesystem reports right after the first create. The read must list again rather
    than trust a stat that cannot tell the two states apart.
    """
    root = _root()
    stamp = time.time_ns()
    frozen = (str(root), 1, 2, stamp, stamp, 3)
    monkeypatch.setattr(store_units, "_root_stat_key", lambda _root: frozen)

    _create("acp-coarse-a")
    assert store.session_units_for_slot(SLOT) == ("acp-coarse-a",)
    _create("acp-coarse-b")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-coarse-a", "acp-coarse-b"}


def test_the_trust_rule_needs_the_change_to_lie_outside_the_window() -> None:
    now = time.time_ns()
    window = store_units._RACY_WINDOW_NS
    recent = ("r", 1, 2, now - window // 2, now - window // 2, 3)
    settled = ("r", 1, 2, now - 2 * window, now - 2 * window, 3)
    ctime_recent = ("r", 1, 2, now - 2 * window, now - window // 2, 3)
    assert not store_units._trusted_root_stat(recent, now)
    assert store_units._trusted_root_stat(settled, now)
    assert not store_units._trusted_root_stat(ctime_recent, now)
    assert not store_units._trusted_root_stat(None, now)


def test_a_planted_index_is_not_served_on_a_stat_it_was_not_built_under(
    listings: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _create("acp-real")
    _wait_out_window()
    assert store.session_units_for_slot(SLOT) == ("acp-real",)
    assert store.session_units_for_slot(SLOT) == ("acp-real",)
    count = len(listings)

    root = _root()
    fingerprint = store._slot_root_fingerprint(root, sorted(os.listdir(root)))
    monkeypatch.setattr(store, "_slot_index", (fingerprint, {SLOT: ("planted",)}, ()))
    assert store.session_units_for_slot(SLOT) == ("planted",)
    assert len(listings) > count + 1, "the planted triple went through the listing path"


def _child(code: str) -> None:
    """Run *code* in a real child interpreter on this test's data home."""
    env = dict(os.environ)
    subprocess.run([sys.executable, "-c", code], check=True, env=env, timeout=60)


def test_a_unit_another_process_creates_or_removes_is_seen_on_the_next_read(
    listings: list[str],
) -> None:
    _create("acp-parent")
    _wait_out_window()
    assert store.session_units_for_slot(SLOT) == ("acp-parent",)
    assert store.session_units_for_slot(SLOT) == ("acp-parent",)
    settled = len(listings)

    _child(
        "from kiro_crew.crew_log.store import CrewLog\n"
        "from kiro_crew.crew_log.schema import KIND_SESSION\n"
        f"CrewLog.create(KIND_SESSION, 'acp-child', owner='owner', agent='kirocrew', slot={SLOT!r})\n"
    )
    assert set(store.session_units_for_slot(SLOT)) == {"acp-parent", "acp-child"}
    assert len(listings) == settled + 1

    _wait_out_window()
    assert set(store.session_units_for_slot(SLOT)) == {"acp-parent", "acp-child"}
    _child(
        "from kiro_crew.crew_log import store\n"
        "from kiro_crew.crew_log.schema import KIND_SESSION\n"
        "assert store.remove_unit(KIND_SESSION, 'acp-child', guard=lambda _p: True)"
        " == store.REMOVE_REMOVED\n"
    )
    assert store.session_units_for_slot(SLOT) == ("acp-parent",)


def test_a_header_published_after_the_listing_is_still_seen_on_a_cache_hit(
    listings: list[str],
) -> None:
    """``create`` makes the directory first and publishes the header second; a stat-only
    hit must keep re-checking the children the listing could not prove."""
    root = _root()
    pending = root / store._store_name("acp-pending")
    pending.mkdir()
    _wait_out_window()
    assert store.session_units_for_slot(SLOT) == ()
    assert store.session_units_for_slot(SLOT) == ()
    _create("acp-pending")
    assert store.session_units_for_slot(SLOT) == ("acp-pending",)


# --------------------------------------------------------------------------- #
# The eager folder's pass after each append: no listing, no read from seq 1.
# --------------------------------------------------------------------------- #

BOARD = "member-cache-board"
BOARD_UNIT = "acp-cache-board"


def _board_work(**fields: Any) -> None:
    """One ``work/recorded`` on BOARD's board, appended and acknowledged."""
    from kiro_crew.crew_log import emit as crew_log_emit

    payload: dict[str, Any] = {"slot": BOARD, "actor": "conductor", "by": BOARD}
    payload.update(fields)
    assert crew_log_emit.on_work_recorded(BOARD_UNIT, payload, timeout=5.0) is True
    assert crew_log_emit.flush(), "the board append never settled"


@pytest.fixture
def eager_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A board with a bound worker, in its own data home, with the eager thread silenced
    so the test drives each pass itself and counts only that pass's work."""
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import projection as crew_log_projection
    from kiro_crew.crew_log import projection_work

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log_projection.forget_slot_folds()
    projection_work.forget_work_marks()
    CrewLog.create(KIND_SESSION, BOARD_UNIT, owner="owner", agent="crew", slot=BOARD)
    _board_work(action="goal", goal="keep reads cheap", round=1)
    _board_work(
        action="create",
        item_id="it_00000001",
        title="item",
        acceptance={"kind": "human_approval"},
        round=1,
    )
    _board_work(action="bind", item_id="it_00000001", worker_session_key="chat-cache-worker")
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log_projection.forget_slot_folds()
    projection_work.forget_work_marks()


#: Entries a warm pass may read beyond the ones appended since the last pass: the
#: fold's own stream and the bind walk's each resume at a remembered point.
_PASS_ENTRY_SLACK = 4


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> "tuple[list[int], list[int]]":
    """The start seq of every entry stream, and a one-cell count of entries yielded."""
    starts: list[int] = []
    yielded = [0]
    real_iter_from = CrewLog.iter_from

    def counted(self: CrewLog, start: int = 1, **kwargs: Any):  # type: ignore[no-untyped-def]
        starts.append(start)
        for entry in real_iter_from(self, start, **kwargs):
            yielded[0] += 1
            yield entry

    monkeypatch.setattr(CrewLog, "iter_from", counted)
    return starts, yielded


def _append_item(index: int) -> None:
    _board_work(
        action="create",
        item_id=f"it_1000000{index}",
        title=f"item {index}",
        acceptance={"kind": "human_approval"},
        round=1,
    )


@pytest.mark.parametrize("fold", ["work", "workstreams"])
def test_eager_passes_after_appends_list_nothing_and_read_only_the_tail(
    eager_board: None, listings: list[str], monkeypatch: pytest.MonkeyPatch, fold: str
) -> None:
    """K appends on an unchanged unit set, each followed by the eager pass that folds
    *fold*: after the first pass no pass lists the unit root, no stream of any unit
    restarts at seq 1, and each pass reads the appended entry plus a constant."""
    from kiro_crew.crew_log import eager as crew_log_eager

    starts, yielded = _count_reads(monkeypatch)
    _wait_out_window()
    crew_log_eager._advance(BOARD, fold)
    warm_listings = len(listings)
    starts.clear()

    passes = 8
    for index in range(passes):
        before = yielded[0]
        _append_item(index)
        crew_log_eager._advance(BOARD, fold)
        assert yielded[0] - before <= 1 + _PASS_ENTRY_SLACK, (
            f"pass {index} read {yielded[0] - before} entries for one append"
        )

    assert len(listings) == warm_listings, "an eager pass listed the unit root again"
    assert starts, "the passes read nothing, so they never saw the appends"
    assert 1 not in starts, f"an eager pass re-read a unit from seq 1: {starts}"


@pytest.mark.parametrize("fold", ["work", "workstreams"])
def test_a_warm_read_reads_only_what_was_appended_since(
    eager_board: None, listings: list[str], monkeypatch: pytest.MonkeyPatch, fold: str
) -> None:
    """The read path the dashboard subscribes through, warm: an unchanged log reads
    nothing beyond a constant, and one append reads that append plus a constant."""
    from kiro_crew.crew_log import projection as crew_log_projection

    starts, yielded = _count_reads(monkeypatch)
    _wait_out_window()
    first = crew_log_projection.read_slot_projection(BOARD, fold).value
    assert first, "the board read came back empty, so it measured nothing"
    warm_listings = len(listings)

    before = yielded[0]
    crew_log_projection.read_slot_projection(BOARD, fold)
    assert yielded[0] - before <= _PASS_ENTRY_SLACK

    starts.clear()
    before = yielded[0]
    _append_item(0)
    after = crew_log_projection.read_slot_projection(BOARD, fold).value
    assert after != first, "the warm read did not see the append"
    assert yielded[0] - before <= 1 + _PASS_ENTRY_SLACK
    assert 1 not in starts, f"a warm read re-read a unit from seq 1: {starts}"
    assert len(listings) == warm_listings, "a warm read listed the unit root again"


def test_the_bind_walk_sees_a_bind_appended_after_it_was_remembered(eager_board: None) -> None:
    from kiro_crew.crew_log import projection_work

    assert [m[3] for m in projection_work._unit_work_marks(BOARD_UNIT) if m[1] == "bind"] == [
        "chat-cache-worker"
    ]
    _board_work(action="bind", item_id="it_00000001", worker_session_key="chat-cache-second")
    assert [m[3] for m in projection_work._unit_work_marks(BOARD_UNIT) if m[1] == "bind"] == [
        "chat-cache-worker",
        "chat-cache-second",
    ]


def test_a_log_that_shrank_is_read_whole_not_resumed(
    eager_board: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An append only adds bytes, so a smaller segment set is a rewrite: the bind walk
    must read it from seq 1 rather than resume past what it remembered."""
    from kiro_crew.crew_log import projection_work

    real_identity = projection_work._log_identity
    projection_work._unit_work_marks(BOARD_UNIT)
    handle = projection_work.open_session_log(BOARD_UNIT)
    assert handle is not None
    origin, size, mtime = real_identity(handle)
    assert size is not None and mtime is not None
    monkeypatch.setattr(
        projection_work, "_log_identity", lambda _handle: (origin, size - 1, mtime + 1)
    )
    starts, _yielded = _count_reads(monkeypatch)
    projection_work._unit_work_marks(BOARD_UNIT)
    assert starts == [1], f"a shrunk log was resumed instead of read whole: {starts}"


def test_marks_keep_one_copy_of_each_binding_and_only_what_the_walks_read(
    eager_board: None,
) -> None:
    """A goal, a create and a repeated bind of the same worker leave one mark: the
    remembered set grows with distinct bindings, not with the entries a board logs."""
    from kiro_crew.crew_log import projection_work

    _board_work(action="bind", item_id="it_00000001", worker_session_key="chat-cache-worker")
    _append_item(0)
    assert projection_work._unit_work_marks(BOARD_UNIT) == (
        (BOARD, "bind", False, "chat-cache-worker"),
    )


def test_a_unit_past_the_mark_budget_is_answered_whole_and_not_remembered(
    eager_board: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config.paths import data_home
    from kiro_crew.crew_log import projection_work

    monkeypatch.setattr(projection_work, "_WORK_MARKS_PER_UNIT", 1)
    _board_work(action="bind", item_id="it_00000001", worker_session_key="chat-cache-second")
    starts, _yielded = _count_reads(monkeypatch)
    expected = (
        (BOARD, "bind", False, "chat-cache-worker"),
        (BOARD, "bind", False, "chat-cache-second"),
    )
    assert projection_work._unit_work_marks(BOARD_UNIT) == expected
    assert (str(data_home()), BOARD_UNIT) not in projection_work._work_marks_memo
    assert projection_work._unit_work_marks(BOARD_UNIT) == expected
    assert starts == [1, 1], f"an over-budget unit was resumed from a memo: {starts}"
