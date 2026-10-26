"""A slot's folds on the projection kernel, kept warm: the stream concatenated over a
slot's units, the per-cell memo with its locks and byte ceiling, and the resume from and
write of each cell's savepoint.
"""

from __future__ import annotations

import heapq
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.projection_budget import (
    SLOT_FOLD_CACHE_BYTES_ENV,
    slot_fold_cache_bytes,
    slot_fold_cell_bytes,
)
from kiro_crew.crew_log.projection_catalogue import KNOWN_TYPES, _Fold, logger
from kiro_crew.crew_log.projection_kernel import Checkpoint, require_name
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.projection_session import _HeldCheckpoints
from kiro_crew.crew_log.projection_session_log import _log_identity, open_session_log
from kiro_crew.crew_log.schema import KIND_SESSION, Entry
from kiro_crew.crew_log.store import (
    _CUTS_FILE,
    CrewLog,
    ReadCursor,
    ResumePoint,
    log_exception_text,
    segment_paths,
    unit_header_slot,
)
from kiro_crew.projection import ProjectionRegistry

if TYPE_CHECKING:
    # Type-only: the savepoint module imports the fold surface, so a runtime
    # import here would close the cycle its function-local imports avoid.
    from kiro_crew.crew_log.checkpoint import CutWitness


class _Ordinal(NamedTuple):
    """One entry of a slot's concatenated stream, at a seq that grows across units.

    A crew log's ``seq`` restarts at 1 in every unit file, while the kernel holds ONE
    watermark per cell and drops an event at or below it. So the units' own seqs cannot
    be handed over as they are: the second unit's entries all sit at or below the
    first's and would be dropped as a re-fold -- the collision :func:`advance` names,
    arriving for a legitimate reason. ``ordinal`` is what the kernel orders by instead:
    the entry's own seq plus the heights of the units already streamed, which grows
    across the whole concatenation.

    Two properties come out of that, and both are needed. It never goes backwards, so
    no entry is wrongly dropped. And one entry always maps to the SAME ordinal, so
    re-driving a range costs the overlap and nothing else -- which a plain running
    counter would lose, because a re-read entry would take a new number and be counted
    a second time.

    ``entry`` is the crew log entry untouched, so the fold sees exactly the record the
    file holds.
    """

    ordinal: int
    entry: Entry
    #: The slot of the lane *entry* came from, for a time-ordered fold's ``from_lane``
    #: hook; empty for a unit-ordered stream and for a unit whose header names no slot.
    lane: str = ""


def _ordinal_seq(event: _Ordinal) -> int:
    """The kernel's ordering number for one wrapped entry."""
    return event.ordinal


@dataclass(frozen=True)
class _UnitMark:
    """One unit's log identity, height and byte fingerprint, as the file reports them.

    ``origin`` is :func:`log_origin`, the same value the session folds and the on-disk
    savepoints compare with; ``None`` is an unknown identity and never matches, so a
    unit whose log cannot be read folds cold. It is in the reuse test because a seq
    alone cannot tell a log that GREW from one REMOVED AND RECREATED under the same id
    whose seq has already climbed back to or past the remembered one.

    ``size`` and ``mtime_ns`` are the stat-only fingerprint :func:`_log_identity`
    already computes for the session fold, over exactly the segment set a walk would
    read. They are here because identity and height together still describe a log only
    by how FAR it goes, never by what it says: an already-folded entry rewritten in
    place keeps its seq, so a mark without them compares equal to a log whose bytes
    have changed underneath a cell, and the stale cell is then served to
    :func:`rebuild_from_projection`, which writes it back over the record. A changed
    fingerprint folds cold instead. Both come from the same call that yields
    ``origin``, so carrying them costs no extra stat.

    The fingerprint settles the units a continuation does not re-read. It cannot settle
    one that GREW: an append moves size and mtime by itself, so a stat has nothing left
    to compare there.

    ``cuts`` is what settles that one. It is :meth:`CrewLog.cuts`, the number of times
    the store has cut this unit's history back, and a cut is the only thing that can
    change a record a fold already consumed -- so the same count twice means those
    records are the same bytes, read off a file a few bytes long rather than off the
    log. ``None`` is NOT PROVEN rather than zero, and a mark carrying it falls back to
    the unit's sealed resume point (:func:`_folded_prefix_holds`), a bounded window
    digest, which is what a log created before the counter existed is settled by.
    """

    origin: str | None
    last_seq: int
    size: int | None = None
    mtime_ns: int | None = None
    cuts: int | None = None
    #: The slot the unit's HEADER names, written once at create. What a time-ordered
    #: fold groups units by (:func:`_lane_of`): one slot's units are successive sessions
    #: and stay in list order, and only different slots are merged by time.
    slot: str | None = None


#: Most unit marks :func:`_unit_mark` remembers at once, least recently stored first out.
#: A mark is a few hundred bytes, and a miss costs one open of that unit's log.
MARK_CACHE_LIMIT: Final[int] = 16384

#: The last mark read for each (data home, unit), beside the stat fingerprint it was
#: read under (:func:`_unit_fingerprint`). Guarded by :data:`_mark_cache_guard`.
_mark_cache: "OrderedDict[tuple[str, str], tuple[tuple[Any, ...], _UnitMark]]" = OrderedDict()
_mark_cache_guard = threading.Lock()


def _unit_fingerprint(unit_id: str) -> "tuple[Any, ...] | None":
    """*unit_id*'s files as stat calls see them, or ``None`` when one cannot be stat'ed.

    Every segment's name, device, inode, size and mtime, and the cut counter's inode,
    size and mtime (or its absence). That is every input a mark is computed from: the
    header's ``createdAt`` and the newest seq live in the segments, the cut count in the
    counter, and the counter is only ever replaced by a rename (a new inode) or emptied
    in place (a new size). So two equal fingerprints describe one mark, and checking one
    costs a directory listing and a stat per file -- never an open or a tail read.
    """
    try:
        paths = segment_paths(KIND_SESSION, unit_id)
        if not paths:
            return None
        parts: list[Any] = []
        for path in paths:
            stat = path.stat()
            parts.append((path.name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        try:
            counter = (paths[0].parent / _CUTS_FILE).stat()
            parts.append((counter.st_ino, counter.st_size, counter.st_mtime_ns))
        except FileNotFoundError:
            parts.append(None)
        return tuple(parts)
    except OSError:
        return None


def _unit_mark(unit_id: str) -> _UnitMark:
    """*unit_id*'s log identity, newest seq, fingerprint and cut count.

    ``origin`` is ``None`` for an unreadable or header-less log, and every caller
    treats that as unknown and folds cold -- which is also why the fingerprint needs no
    separate unknown test: :func:`_log_identity` returns an identity only when the same
    stat calls that produced the fingerprint succeeded.

    CACHED by the unit's stat fingerprint, because an eager advance asks for every unit
    of the slot and usually one of them moved. Reading a mark opens the log, which reads
    its tail; a slot of a hundred units would pay a hundred opens for one append. So a
    unit whose files stat the same as when its mark was read answers from the cache, and
    only a unit that moved is opened. A mark is stored only when the fingerprint taken
    after the read equals the one taken before it, so a file that moved during the read
    is never cached under a fingerprint that describes other bytes.
    """
    key = (str(data_home()), unit_id)
    seen = _unit_fingerprint(unit_id)
    if seen is not None:
        with _mark_cache_guard:
            held = _mark_cache.get(key)
        if held is not None and held[0] == seen:
            return held[1]
    mark = _read_unit_mark(unit_id)
    if seen is not None and mark.origin is not None and _unit_fingerprint(unit_id) == seen:
        with _mark_cache_guard:
            _mark_cache[key] = (seen, mark)
            _mark_cache.move_to_end(key)
            while len(_mark_cache) > MARK_CACHE_LIMIT:
                _mark_cache.popitem(last=False)
    return mark


def _read_unit_mark(unit_id: str) -> _UnitMark:
    """:func:`_unit_mark` read off the log itself: one open, its tail and its counter."""
    try:
        handle = open_session_log(unit_id)
        if handle is None:
            return _UnitMark(None, 0)
        origin, size, mtime_ns = _log_identity(handle)
        # ``last_seq`` on a freshly opened handle is read off the file's tail, which
        # is what makes it usable as a growth signal for a reader that never appends.
        return _UnitMark(
            origin,
            int(getattr(handle, "last_seq", 0) or 0),
            size,
            mtime_ns,
            handle.cuts(),
            unit_header_slot(KIND_SESSION, unit_id),
        )
    except Exception:
        return _UnitMark(None, 0)


def _unit_marks(unit_ids: Sequence[str]) -> "tuple[_UnitMark, ...]":
    """Every unit's mark, in the order they are folded."""
    return tuple(_unit_mark(unit_id) for unit_id in unit_ids)


#: What :func:`_cut_verdict` concluded.
#:
#: ``UNCUT`` and ``CUT`` are decided by the cut counter alone; ``UNPROVEN`` is the
#: counter declining to answer, and :func:`_folded_prefix_holds` decides instead from
#: the unit's sealed resume point -- a bounded window, never the folded prefix.
_UNCUT = "uncut"
_CUT = "cut"
_UNPROVEN = "unproven"


def _cut_verdict(now: "int | None", held: "int | None") -> str:
    """Whether the cut counter proves this unit's folded records are still intact.

    The store raises the counter ONCE BEFORE each cut and once after it, and
    :meth:`CrewLog.cuts` answers ``None`` for the odd value in between. So a reading
    that comes back at all was taken with the bytes settled, and two equal such readings
    bracket a window in which no cut COMPLETED -- which is the question, because a cut
    is the only thing in this store that can change a record already on disk. Equal
    therefore means the folded records are the same bytes, and that is the whole proof.

    One raise would not do it. It brackets "a cut STARTED since you last looked", and a
    reader that sampled the raised value, folded the records that cut was about to
    remove, and sampled the same value again afterwards would compare equal across the
    rewrite.

    Unequal means a cut landed, which is not a slow case to re-check but a decided one:
    the records a fold consumed may be gone. Either reading being ``None`` proves nothing
    in either direction -- an absent, unreadable or mid-cut counter is not a count.
    """
    if now is None or held is None:
        return _UNPROVEN
    return _UNCUT if now == held else _CUT


def _folded_prefix_holds(
    unit_id: str, held: _UnitMark, now: _UnitMark, point: "ResumePoint | None"
) -> bool:
    """Whether the records a cell folded from *unit_id* (as *held*) are still those bytes.

    The cheap proof first. A unit whose cut counter reads the same as it did when the
    cell was built has not been cut since, and nothing else in the store can disturb a
    committed record -- so the question is settled by a few bytes, and the warm read
    costs the same on a log of ten entries and a log of a hundred thousand.

    A counter on ONE side only is a cut, not a missing proof. The counter goes from
    absent to a number only when a cut raises it (:func:`_open_cut` starts an absent
    one at a fresh phase), and from a number to nothing only when a cut is in flight or
    its raise failed. Either way records may have moved, and the cell folds cold.

    Neither side counting -- a log created before the counter existed -- is settled by
    *point*, the resume point this cell's last walk of the unit sealed with a bounded
    window digest (:meth:`CrewLog.seal_point`). It names the last record folded, and
    :meth:`CrewLog.point_holds` re-hashes that window and checks the record is still
    there. That costs the window, never the prefix: a digest of the folded records would
    grow with the log on every eager advance. No point means no evidence, which folds
    cold rather than trusting a stat.

    A unit nothing was folded from has nothing to prove.
    """
    if held.last_seq <= 0:
        return True
    verdict = _cut_verdict(now.cuts, held.cuts)
    if verdict == _UNCUT:
        return True
    if verdict == _CUT or held.cuts is not None or now.cuts is not None:
        return False
    if point is None or point.window is None or point.seq > held.last_seq:
        return False
    try:
        handle = open_session_log(unit_id)
        return handle is not None and handle.point_holds(point)
    except Exception:
        return False


class _SlotFold:
    """One SLOT-keyed crew-log fold as a :mod:`kiro_crew.projection` unit.

    :class:`_SessionFold` with the two differences a concatenation of units brings.
    The event arriving is an :class:`_Ordinal`, so ``apply`` unwraps before the fold
    sees it -- the kernel orders by the wrapper's number and the fold reads the entry.
    And the starting state is BOUND to the slot for a fold that declares ``bind_slot``,
    so a slot-keyed fold knows which board it answers for before the first entry
    instead of guessing it from whichever entry comes first.

    The same-reference rule is kept the same way: an entry the fold cannot be moved by
    returns the state untouched, and any other entry is stepped onto a copy.
    """

    def __init__(self, fold: _Fold, slot: str) -> None:
        self._fold = fold
        self._slot = slot
        self.key = fold.name
        self.state_version = fold.state_version

    def init(self) -> dict[str, Any]:
        state = self._fold.start()
        if self._slot and self._fold.bind_slot is not None:
            self._fold.bind_slot(state, self._slot)
        return state

    def apply(self, state: dict[str, Any], event: _Ordinal) -> dict[str, Any]:
        entry = event.entry
        if not self._fold.touched_by(entry):
            return state
        grown = self._fold.copied(state)
        if event.lane and self._fold.from_lane is not None:
            self._fold.from_lane(grown, event.lane)
        self._fold.step(grown, entry)
        return grown

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._fold.render(state)


class _SlotStream:
    """A slot's units as one stream the kernel can order, oldest unit first.

    Each unit's base is the total height of the units ALREADY STREAMED, counted as
    this pass actually saw them rather than taken from a sampled height -- so an
    append landing in an earlier unit while this runs still cannot push one of its
    entries onto an ordinal a later unit has already used. A unit with no log
    contributes nothing and is skipped, the same as the cold fold: a slot whose
    oldest unit was collected by retention still folds the ones it has.

    Iterating is a GENERATOR, so the kernel folds one entry at a time and a cold
    fold of a long slot holds no more than that.

    Two numbers the stream cannot return and its caller needs afterwards.
    :attr:`heights` is the last seq FOLDED from each unit that had a log, which is
    what lets the memo record what this pass really consumed instead of what was
    sampled before it. :attr:`reached` is that height for the newest such unit, the
    only figure a later read of the same slot can compare against and the one
    :func:`fold_slot_checkpoint` has always answered with; it starts at *since*, so a
    warm tail that turned out to be empty reports the position it resumed from rather
    than dropping to zero.

    *resume* is a :class:`~kiro_crew.crew_log.store.ResumePoint` from an earlier pass,
    and it turns the walk of ONE unit's log into a read of its tail: without it,
    reaching ``since + 1`` means decoding every record below it, so a poll that finds
    one new entry decodes the whole log. It names a byte offset in one file, so it is
    applied only when this stream reads exactly one unit -- the warm tail's shape --
    and a stream over several units ignores it rather than aiming a position at the
    wrong log.

    :attr:`points` is the third thing the caller needs afterwards: per unit, the
    resume point the walk reached, for the next pass to hand back. A unit that
    yielded nothing leaves no entry in it at all, which is how the caller tells
    "nothing arrived, keep the point you have" from "this is the new point".
    """

    def __init__(
        self,
        unit_ids: Sequence[str],
        *,
        base: int = 0,
        since: int = 0,
        resume: "ResumePoint | None" = None,
    ) -> None:
        self._unit_ids = tuple(unit_ids)
        self._base = base
        self._since = since
        self._resume = resume if len(self._unit_ids) == 1 else None
        self.reached = since
        self.heights: dict[str, int] = {}
        self.points: "dict[str, ResumePoint]" = {}

    def __iter__(self) -> "Iterator[_Ordinal]":
        base = self._base
        for unit_id in self._unit_ids:
            handle = open_session_log(unit_id)
            if handle is None:
                continue
            top = base
            reached = self._since
            last: "Entry | None" = None
            # One cursor PER UNIT. A shared one would end the pass holding whichever
            # unit yielded last, and a later unit that yielded nothing would leave an
            # earlier unit's offset standing as this unit's position.
            cursor = ReadCursor()
            for entry in handle.iter_from(
                self._since + 1,
                known=KNOWN_TYPES,
                resume=self._resume,
                cursor=cursor,
            ):
                ordinal = base + entry.seq
                top = max(top, ordinal)
                reached = max(reached, entry.seq)
                last = entry
                yield _Ordinal(ordinal, entry)
            base = top
            self.heights[unit_id] = reached
            self.reached = reached
            if cursor.at is not None:
                # Sealed once per unit, after its walk: on a log with no cut counter the
                # window is the only evidence a later continuation of this unit has.
                self.points[unit_id] = handle.seal_point(cursor.at, expect=last)


@dataclass
class _SlotMemo:
    """One slot fold kept warm: the kernel cell holding it, and what it folded over.

    The registry is per (slot, fold), not one shared instance, and that is a
    correctness requirement rather than a preference: ``prime`` folds and RESETS every
    definition registered in it, so a shared registry would make a cold refold of one
    fold discard another's warm cell -- and the three slot folds are read over
    different unit lists, so they go stale independently.

    ``marks`` is the per-unit watermark vector the reuse test compares, and the
    ordinals are built from it: an entry's ordinal is the total folded height of every
    unit once that entry is counted (:func:`_drive_steps`). The total only rises, so a
    continuation always lands above the cell's watermark, whichever unit grew.

    A memo is never edited once stored. A read that carries one forward stores a NEW
    memo over the SAME registry, so two reads racing on one slot cannot leave a pair of
    fields half updated: the registry's cell is the single truth and holds its own lock,
    and re-driving a range it has already folded is dropped on its watermark rather
    than counted twice.
    """

    registry: ProjectionRegistry
    store: str
    units: "tuple[str, ...]"
    marks: "tuple[_UnitMark, ...]"
    reached: int
    #: The kernel ORDINAL this cell is persisted through on disk
    #: (:func:`kiro_crew.crew_log.checkpoint.save_slot`), or 0 when nothing is. What
    #: :func:`_persist_slot_fold` measures a write against, carried on the cell so a
    #: continuation decides without reading the file to find out.
    saved: int = 0
    #: Where in EACH unit's log this cell's fold stopped, as a byte offset the next read
    #: can open the file at, keyed by unit. Without one a continuation of that unit
    #: still decodes every record below the seq it resumes from, so a poll costs the
    #: whole log however little arrived. Per unit rather than the newest one alone,
    #: because a conductor's slot grows in the MIDDLE: its own unit and each bound
    #: worker's are all written to, and a continuation reads whichever of them moved.
    #: A unit absent here has no position vouched for and is walked from the top: a
    #: unit that never yielded, or a point the store declined to issue. On a log with no
    #: cut counter the point is also the evidence its folded prefix is intact
    #: (:func:`_folded_prefix_holds`).
    resumes: "Mapping[str, ResumePoint]" = field(default_factory=dict)
    #: The greatest merge key folded, for a fold whose units merge by time
    #: (:func:`_time_ordered`): ``(entry time, lane)``. A continuation is admitted
    #: only when every entry it adds sorts above it, because that is exactly when the
    #: cold merge would place them last too. ``None`` for a unit-ordered fold and for a
    #: time-ordered cell nothing touched yet.
    last_key: "tuple[int, str] | None" = None
    #: What ORDERS this cell against every other value of the same (slot, fold). Minted
    #: when the cell is built, so a cell CARRIED FORWARD unchanged keeps its number and a
    #: cell that folded anything new gets a higher one. See :func:`_next_slot_revision`.
    revision: int = 0
    #: What this cell is charged against the ceiling, counted ONCE when it is built
    #: (:func:`slot_fold_cell_bytes`). Stored rather than recomputed because the eviction
    #: loop sums every held cell, and re-counting there would make one store cost a walk
    #: of every warm cell's items.
    weight: int = 0


_slot_memos: "dict[tuple[str, str, str], _SlotMemo]" = {}
_slot_memo_guard = threading.Lock()

#: The last revision this process handed out, for any (slot, fold). Guarded by
#: :data:`_slot_memo_guard`.
#:
#: ONE COUNTER FOR EVERY KEY rather than one per key, which costs a shared number and
#: buys the property the push needs with nothing to evict: a per-key counter would have
#: to live somewhere, and the only table it could live in is the memo table -- which is
#: evicted, so an evicted key's next cell would restart at 1 and a client holding the
#: earlier number would discard every value after it. A process-wide counter is
#: strictly increasing for every key at once, so it is monotonic per key as a
#: consequence rather than as a thing to maintain.
#:
#: NOT comparable ACROSS PROCESSES, and nothing asks it to be: a frame carries the
#: revision of the process that folded it, and a client reconnecting to another gateway
#: re-reads rather than continues. In process, a restart folds cold and starts at 1
#: again, which a client that reconnected has already discarded its own state for.
_slot_revision = 0


def _next_slot_revision() -> int:
    """The next revision, strictly above every one already handed out.

    Caller holds :data:`_slot_memo_guard`. Never derived from a seq, a time or a file:
    every one of those can repeat or step backwards across a unit rollover, a unit
    recreated under the same id, or a rewritten prefix -- the three cases
    :func:`fold_slot_warm` names -- and the whole purpose of this number is to order two
    values that those cases leave looking identical.
    """
    global _slot_revision
    _slot_revision += 1
    return _slot_revision


@dataclass
class _FoldLock:
    """One key's fold lock, beside the number of passes holding or waiting for it.

    The count is what makes the table droppable: an entry at zero is one no pass can be
    inside, so replacing it with a fresh lock serializes exactly what it needs to.
    """

    lock: threading.Lock
    holders: int = 0


#: One lock per (data home, slot, fold), held across a whole pass of
#: :func:`fold_slot_warm`. Created under :data:`_slot_memo_guard`.
#:
#: WHY A PASS AND NOT A DICT ACCESS. Two warm passes on one key SHARE the cell: the
#: continuation drives ``memo.registry``, and :func:`_slot_checkpoint` then reads that
#: live cell and pairs it with the caller's OWN ``reached``. So a pass that read a
#: shorter tail could return the other pass's newer state labelled with its own older
#: seq -- and ``seq`` is what a reader truncates against, so the value is newer than the
#: number that describes it. The kernel's watermark repairs the CELL on a later read; it
#: cannot repair a value already returned.
#:
#: The pairing matters here because two readers on one slot is the NORMAL mode: the
#: append-driven wake folds the same board a dashboard poll is reading, which is what eager
#: folding is for. A lock is therefore the mechanism rather than a documented caveat.
#:
#: PER KEY, not one lock, because two slots have nothing to share. And held across the
#: pass rather than around the drive alone: the state and the seq are read at different
#: moments and it is their PAIRING that must be atomic. A caller that waits here waits
#: for a fold it would otherwise have duplicated, and finds the cell warm when it
#: arrives, so the lock costs no wall time it was not already paying.
#:
#: WHAT BOUNDS THIS TABLE. One entry per key with a pass holding or waiting for it, so
#: its size is the folds running right now rather than the slots this process has ever
#: folded. :func:`_release_fold_lock` drops an entry when its last holder leaves, which is
#: the only moment at which dropping one is safe: a lock handed out twice as two different
#: objects serializes nothing, so an entry with a waiter has to stay.
_slot_fold_locks: "dict[tuple[str, str, str], _FoldLock]" = {}


def _acquire_fold_lock(key: "tuple[str, str, str]") -> "_FoldLock":
    """Claim one (data home, slot, fold)'s lock entry, creating it on first use.

    Claiming is counting a holder, not taking the lock: the caller takes ``entry.lock``
    itself, outside :data:`_slot_memo_guard`, because a pass holds it while folding and
    the guard is taken inside that pass. Every claim owes a :func:`_release_fold_lock`.
    """
    with _slot_memo_guard:
        entry = _slot_fold_locks.get(key)
        if entry is None:
            entry = _FoldLock(lock=threading.Lock())
            _slot_fold_locks[key] = entry
        entry.holders += 1
        return entry


def _release_fold_lock(key: "tuple[str, str, str]", entry: "_FoldLock") -> None:
    """Give up a claim, and drop the entry once nobody holds or waits for it.

    The identity check keeps a release from deleting an entry some other pass created for
    the same key after this one was dropped.
    """
    with _slot_memo_guard:
        entry.holders -= 1
        if entry.holders <= 0 and _slot_fold_locks.get(key) is entry:
            del _slot_fold_locks[key]


def forget_slot_folds(slot: str = "", name: str = "") -> None:
    """Drop warm slot folds, so the next read folds cold.

    Everything when called with no argument; one slot's, or one fold of one slot's,
    when named. The memo is an optimization with no answer of its own, so dropping it
    costs time and never correctness -- which is what makes this safe as the read
    path's own response to doubt, and what a test isolating one slot's log from
    another's needs.
    """
    home = str(data_home())
    # The locks (``_slot_fold_locks``) are not touched here: an entry exists only while a
    # pass holds or waits for it, and dropping one out from under that pass would hand the
    # next caller a different lock object and reopen the race it exists to close.
    with _slot_memo_guard:
        if not slot and not name:
            _slot_memos.clear()
            _oversize_slot_cells.clear()
            with _mark_cache_guard:
                _mark_cache.clear()
            return
        for table in (_slot_memos, _oversize_slot_cells):
            for key in [
                held
                for held in table
                if held[0] == home
                and (not slot or held[1] == slot)
                and (not name or held[2] == name)
            ]:
                del table[key]


def _slot_memo_bytes() -> int:
    """What every warm cell held right now is charged, together. Caller holds the guard.

    Read off each memo's own stored charge rather than recomputed. Re-counting would walk
    every held cell's items on every store, which turns one store into O(all cells) work.
    """
    return sum(memo.weight for memo in _slot_memos.values())


def _remember_slot_fold(key: "tuple[str, str, str]", memo: _SlotMemo) -> None:
    """Keep *memo* under *key*, capped by BYTES; least recently STORED goes first.

    BY BYTES RATHER THAN BY COUNT, which is a reversal of an earlier decision and worth
    stating as one. The count ceiling's case was that each cell is a bounded record, so
    the thing needing a limit is how many are retained -- and that holds exactly as long
    as the cells are comparable. They are not. A ``panel`` cell is a few hundred KiB and a
    ``radar`` cell at its declared caps is 2.4 GiB, so one ceiling of 64 cells priced a
    resident total across four orders of magnitude with no number in the code moving.
    Eager folding is what makes the high end reachable rather than theoretical: the worker
    stores a cell for every board this process WRITES, so which 64 cells are held stopped
    being a function of what a reader asked for.

    So the ceiling is a byte figure (:func:`slot_fold_cache_bytes`) and each cell is
    charged what it RETAINS (:func:`slot_fold_cell_bytes`). Small cells keep many slots
    warm and a huge one keeps few, which is the behaviour a count could not express.

    The earlier decision's second objection stands and is accepted: below the worst case
    the eviction order does mean "whoever has the biggest board loses". That is now the
    POINT -- one 2 GiB cell should lose to four hundred small ones -- rather than a side
    effect, because what is being bounded is resident bytes and not cell count.

    A CELL THAT CANNOT FIT THE WHOLE CEILING IS NOT STORED. ``radar`` at its caps is
    charged more than any sane ceiling, and the two alternatives are both worse: parking
    it would hold 2.4 GiB resident until the next store, and evicting everything else
    first would empty the table to make room for one cell that still does not fit. So it
    is refused, the read still answers correctly, and that slot folds cold next time --
    which costs time and never an answer. The first refusal of a cell is logged at
    WARNING and later ones at DEBUG; the operator's answer is a higher ceiling.

    Eviction order is least recently stored or advanced, which is not the same as least
    recently read: a read that finds the cell already at the file's position returns it
    without storing anything, so it does not move the cell towards the back. A cell can
    therefore be read often while sitting at the front, be evicted, and fold cold on its
    next read. That is the cost of not taking the guard on a read that had nothing to
    record, and it is the cheaper side of the trade.

    Insertion order tracks stores because a cell is never edited in place -- a read or an
    eager fold that carries one forward stores a NEW memo, which moves it to the end.
    """
    ceiling = slot_fold_cache_bytes()
    if memo.weight > ceiling:
        with _slot_memo_guard:
            # Dropped rather than left standing: an older cell of this key describes
            # bytes this pass has already folded past, so serving it later would be a
            # stale answer, where no cell is a cold fold.
            _slot_memos.pop(key, None)
            first = key not in _oversize_slot_cells
            _oversize_slot_cells[key] = memo.weight
            _oversize_slot_cells.move_to_end(key)
            while len(_oversize_slot_cells) > OVERSIZE_SLOT_CELL_LIMIT:
                _oversize_slot_cells.popitem(last=False)
        # WARNING once per cell, where an operator looks: from here on every read of this
        # board folds cold, and the answer is a higher ceiling.
        logger.log(
            logging.WARNING if first else logging.DEBUG,
            "crew log slot fold %s/%s retains %d charged bytes, above the %d-byte "
            "ceiling (%s), so it is not kept warm; every read of it folds cold",
            key[1],
            key[2],
            memo.weight,
            ceiling,
            SLOT_FOLD_CACHE_BYTES_ENV,
        )
        return
    with _slot_memo_guard:
        _slot_memos.pop(key, None)
        _oversize_slot_cells.pop(key, None)
        _slot_memos[key] = memo
        held = _slot_memo_bytes()
        while held > ceiling and len(_slot_memos) > 1:
            oldest, evicted = next(iter(_slot_memos.items()))
            _slot_memos.pop(oldest)
            held -= evicted.weight


#: Cells the last pass REFUSED to keep because their charge was above the whole ceiling,
#: with that charge. The eager worker reads this (:func:`slot_fold_over_ceiling`) and does
#: not advance such a cell on a wake: a pass it cannot store is a cold fold of every unit
#: the slot ran under, paid for a value nobody kept, once per wake. The read path is not
#: affected -- it folds the cell when asked, which is the lazy behaviour -- and a pass that
#: CAN store the cell (a raised ceiling, a state that shrank) clears the entry.
#:
#: BOUNDED, least recently refused first, at :data:`OVERSIZE_SLOT_CELL_LIMIT`. An entry
#: evicted here costs one wasted fold: the next wake for that cell tries it, is refused
#: again, and records it again.
_oversize_slot_cells: "OrderedDict[tuple[str, str, str], int]" = OrderedDict()

#: Most refused cells remembered at once. A refused cell is one at a fold's caps, so even a
#: large fleet holds few; past this the oldest is forgotten and costs one retry.
OVERSIZE_SLOT_CELL_LIMIT: Final[int] = 256


def slot_fold_over_ceiling(slot: str, name: str) -> bool:
    """Whether *slot*'s *name* cell was last refused as too big for the CURRENT ceiling.

    Compared against the ceiling now, not the one in force when the cell was refused, so
    an operator raising ``KIROCREW_SLOT_FOLD_CACHE_BYTES`` lets the next wake advance it.
    """
    key = (str(data_home()), slot, name)
    with _slot_memo_guard:
        weight = _oversize_slot_cells.get(key)
    return weight is not None and weight > slot_fold_cache_bytes()


def _minted_revision() -> int:
    """One fresh revision, taking the guard. For a cell about to be built."""
    with _slot_memo_guard:
        return _next_slot_revision()


def _time_ordered(name: str) -> bool:
    """Whether fold *name* merges its units by entry time rather than folding them in
    list order (:attr:`_Fold.unit_order`)."""
    return _FOLDS[name].unit_order == "time"


class _Step(NamedTuple):
    """One unit a continuation reads: from above *since*, at *resume* when one holds."""

    unit: str
    since: int
    resume: "ResumePoint | None"


def _continuable(
    name: str,
    memo: "_SlotMemo",
    units: "tuple[str, ...]",
    marks: "tuple[_UnitMark, ...]",
) -> "list[_Step] | None":
    """The units a warm continuation of *memo* must read, or ``None`` to fold cold.

    Every unit is compared with what the cell holds for it, and each lands in one of
    three places. UNCHANGED (the whole mark is equal) is not read at all. GROWN (same
    identity, a higher seq, and its folded records proven intact by
    :func:`_folded_prefix_holds`) is read from above the height the cell folded, at the
    point the cell's last walk stopped. NEW (a unit the cell never held) is read from
    its first record. Everything else describes bytes the cell did not fold and returns
    ``None``: a unit that left the list, units that changed order, an unknown or changed
    identity, a seq that went backwards, files that moved with no growth (an in-place
    rewrite keeps every seq), a folded prefix that cannot be proven.

    Which grown or new units may be read depends on the fold's LANES (:func:`_lane_of`).
    A lane is the concatenation of its units, so new entries may only land at a lane's
    END: every unit of a lane after its first one that yields must have held nothing
    before. A unit-ordered fold (the default) is one lane. A conductor's board does not
    grow that way -- the conductor's own unit and each bound worker's are written all the
    time, and they sit in the middle of the list -- which is why the folds that read
    those lists are time-ordered: one lane per slot, merged by time, so an append to the
    newest unit of any slot sorts last, and :func:`_continue_slot_fold` checks that it
    does before driving it.
    """
    if not marks or any(mark.origin is None for mark in marks):
        return None
    held = dict(zip(memo.units, memo.marks))
    if len(held) != len(memo.units):
        return None
    where = {unit: index for index, unit in enumerate(units)}
    if len(where) != len(units):
        return None
    kept = [where.get(unit) for unit in memo.units]
    if any(index is None for index in kept) or kept != sorted(kept):  # type: ignore[type-var]
        return None
    steps: list[_Step] = []
    for unit, mark in zip(units, marks):
        before = held.get(unit)
        if before is None:
            steps.append(_Step(unit, 0, None))
            continue
        if mark == before:
            continue
        if mark.origin != before.origin or mark.last_seq < before.last_seq:
            return None
        if mark.last_seq == before.last_seq and before.size is not None:
            # Same height with files that moved is bytes changing under entries already
            # folded -- an in-place rewrite keeps every seq -- and no append explains it.
            return None
        point = _resume_for(memo, unit, before.last_seq)
        if not _folded_prefix_holds(unit, before, mark, point):
            return None
        if mark.last_seq > before.last_seq:
            steps.append(_Step(unit, before.last_seq, point))
        # Otherwise the cell folded past what its last pass sampled (an append landed
        # mid-pass, see :func:`_folded_marks`), and nothing new is here: the proof held,
        # so only the mark is refreshed.
    lane = _lane_of(name, units, marks)
    for group in {lane[step.unit] for step in steps}:
        # Within one lane the units are concatenated, so new entries may only land at the
        # lane's END: every unit of the lane after its first one that yields must have
        # held nothing before.
        members = [unit for unit in units if lane[unit] == group]
        first = next(index for index, unit in enumerate(members) if unit in {s.unit for s in steps})
        if any(held[unit].last_seq > 0 for unit in members[first + 1 :] if unit in held):
            return None
    return steps


def _lane_of(name: str, units: "Sequence[str]", marks: "Sequence[_UnitMark]") -> "dict[str, str]":
    """Which LANE each unit's entries are folded in, for fold *name*.

    A lane is concatenated: its units are folded whole, in list order. A unit-ordered
    fold is one lane. A time-ordered fold has one lane per slot the unit headers name,
    and merges the lanes by time (:class:`_MergedRead`). Successive units of one slot are
    one session after another, so their order is the list's -- the order the work fold
    already relies on when a header clock steps back between two of them -- while
    different slots are written at the same time and only their entry times can order
    them. A unit whose header names no slot is a lane of its own.
    """
    if not _time_ordered(name):
        return {unit: "" for unit in units}
    return {unit: (mark.slot if mark.slot else f"unit:{unit}") for unit, mark in zip(units, marks)}


def _note_slot_fold_origin(origin: str) -> None:
    """Stamp where this slot pass got its starting state, for the metrics side.

    Function-local import and swallowed failure, the policy
    :mod:`kiro_crew.crew_log.eager` already follows for this same package: a
    telemetry package that will not load costs a fold its metric and nothing
    else, and ``kiro_crew.metrics`` stays out of this family's module-scope
    imports so the projection facade need not re-export it.

    STAMPS, never records: the eager fold worker turns the mark into a point
    after the pass, OUTSIDE the per-cell lock this runs under -- a first
    ``get_recorder()`` builds the telemetry provider synchronously, and doing
    that while holding this lock would stall a reader waiting on the same cell.
    No ``exc_info``: this module's frames bind open ``CrewLog`` handles.
    """
    try:
        from kiro_crew.metrics.crew_log_health import SCOPE_SLOT, note_fold_origin

        note_fold_origin(SCOPE_SLOT, origin)
    except Exception:  # pragma: no cover - a broken install loses the metric only
        logger.debug("crew log fold origin metric unavailable")


def _note_slot_fold_idle() -> None:
    """Stamp that this slot pass advanced nothing. See :func:`_note_slot_fold_origin`."""
    try:
        from kiro_crew.metrics.crew_log_health import SCOPE_SLOT, note_fold_idle

        note_fold_idle(SCOPE_SLOT)
    except Exception:  # pragma: no cover - a broken install loses the metric only
        logger.debug("crew log fold idle metric unavailable")


def fold_slot_warm(name: str, unit_ids: Sequence[str], *, slot: str) -> Checkpoint:
    """*name* folded over *slot*'s units, CONTINUED from where the last read left it.

    :func:`fold_slot_checkpoint`'s answer, reached incrementally. Folding is O(the
    log), not O(the record): the reader walks every line of every unit to find the few
    that move this fold, and a slot's logs carry its message bodies. The readers that
    pay that are loops that wake on a timer, so the fold is kept in memory per slot and
    advanced over the entries that arrived since -- through the projection kernel,
    whose watermark drops an entry already folded, so the resumed answer and the
    from-scratch answer come out of one implementation.

    A warm read reads ONE file: the newest unit, from its remembered position. A cold
    read streams every unit. Both go through the kernel, so there is no second folding
    path that could disagree with the first about the same bytes.

    A continuation also PROVES that one file's already-folded records are still those
    bytes before trusting them (:func:`_folded_prefix_holds`). The store rewrites a
    committed prefix on two recovery paths -- an unreachable chunk group is truncated
    away and closers are appended with seqs continuing from the cut, and a failed append
    is truncated back after its bytes reached the disk -- and a reader holds no append
    lock while either runs. Both leave a file that has grown since the last read and
    reuses seqs the fold already consumed, which is a pure append to every stat and to
    every seq comparison.

    The proof is a COUNT, not a hash: the store raises the unit's cut counter before
    each of those truncations, so two equal readings of a few bytes bracket a window in
    which no committed record changed. That is what keeps a warm read the same price on
    a log of ten entries and a log of a hundred thousand. Hashing the already-folded
    records proves the same thing by re-walking exactly the bytes the reader has paid
    for once, which charges the whole folded prefix per read; it is the fallback for a
    log with no counter and nothing else.

    THE INVARIANT: the heights sampled before a pass reads are its READ PLAN, never the
    cell's claim about itself. What a memo remembers is what the pass actually FOLDED
    (:func:`_folded_marks`). Two things follow. An append landing mid-read is folded in
    and the next read starts above it, so it is neither dropped nor counted twice. And a
    slot that has not moved compares equal and is answered from the cell -- where a memo
    holding the sample would sit permanently one entry short of itself and send every
    later read back to the file for a tail it has already folded.

    PERSISTED, so a second process and a cell that was evicted resume instead of
    folding cold. The savepoint lives beside the newest unit's log and is admitted on
    exactly the shapes :func:`_continuable` admits -- see
    :mod:`kiro_crew.crew_log.checkpoint`'s slot section, which owns the file and every
    condition that retires one. It is an OPTIMIZATION with no answer of its own: a
    missing, stale or unreadable savepoint folds cold, which reaches the same value at
    more cost. What must never happen is a WRONG answer, so every condition
    :func:`_continuable` does not admit refolds from empty rather than carrying state
    that describes other bytes.
    """
    return fold_slot_warm_revised(name, unit_ids, slot=slot)[0]


def fold_slot_warm_revised(
    name: str, unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`, and the REVISION of the cell it answered from.

    Two returns rather than a second lookup, because the pairing has to be atomic. A
    caller that read the checkpoint and then asked the memo table for its revision could
    be handed a number minted by a LATER pass -- and a frame carrying a newer revision
    beside an older value makes the client discard the newer value when it arrives, which
    is the one failure the revision exists to prevent. So the pass that folded reports
    both, inside its own lock.

    :func:`fold_slot_warm` stays the one-value call every existing caller makes: a
    savepoint, a writer continuing a checkpoint and a test have no use for a revision,
    and giving them a tuple to unpack would spread a push's contract over callers that
    do not push.
    """
    require_name(name)
    key = (str(data_home()), slot, name)
    # The whole pass, under this key's own lock. Everything below reads or advances one
    # shared cell and then pairs that cell's state with this pass's own seq, and it is
    # the PAIRING that a second pass would break -- see ``_slot_fold_locks``.
    entry = _acquire_fold_lock(key)
    try:
        with entry.lock:
            return _fold_slot_warm_locked(name, key, unit_ids, slot=slot)
    finally:
        _release_fold_lock(key, entry)


def _fold_slot_warm_locked(
    name: str, key: "tuple[str, str, str]", unit_ids: Sequence[str], *, slot: str
) -> "tuple[Checkpoint, int]":
    """:func:`fold_slot_warm`'s body, with this key's lock already held.

    Split out so the lock's extent is one ``with`` statement rather than an indentation
    a later edit could fall out of: every return below is inside it by construction.
    """
    with _slot_memo_guard:
        memo = _slot_memos.get(key)
    marks = _unit_marks(unit_ids)
    units = tuple(unit_ids)
    known = all(mark.origin is not None for mark in marks)
    ordered = _time_ordered(name)
    if memo is not None and known:
        if memo.units == units and memo.marks == marks:
            # NOTHING FOLDED, so the cell keeps the revision it was built with. Minting
            # one here would move the number on every poll of an idle board, and a client
            # would re-seed from a frame that carries the value it already holds.
            #
            # Counted as a WARM start all the same: the pass started and the cell
            # answered it, which is what the origin reports. ``fold.apply`` is
            # what separates a pass that moved a value from one that did not --
            # which is why this branch, the ONLY one that advances nothing, is also
            # the only one that marks the pass idle.
            _note_slot_fold_origin("warm")
            _note_slot_fold_idle()
            return _slot_checkpoint(name, memo)
        steps = _continuable(name, memo, units, marks)
        if steps is not None and not steps:
            # Nothing to read, only marks to refresh (an append the last pass folded after
            # sampling). The value is the one the cell holds, so it keeps its revision.
            _note_slot_fold_origin("warm")
            _note_slot_fold_idle()
            refreshed = replace(memo, marks=marks)
            _remember_slot_fold(key, refreshed)
            return _slot_checkpoint(name, refreshed)
        grown = None if not steps else _continue_slot_fold(name, memo, units, marks, steps)
        if steps and grown is None:
            # A refused time-ordered continuation may have driven part of its tail into
            # this cell, so the cell is dropped before anything else can serve it.
            with _slot_memo_guard:
                if _slot_memos.get(key) is memo:
                    del _slot_memos[key]
        if grown is not None:
            _note_slot_fold_origin("warm")
            if not ordered:
                grown = _persist_slot_fold(name, grown, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, grown)
            return _slot_checkpoint(name, grown)
    # Nothing warm to carry forward. Ask the DISK before folding every unit: a savepoint
    # beside the newest unit's log reaches the same value over the entries appended since
    # it was written, where the cold fold below streams every unit from its first record.
    #
    # Not for a time-ordered fold. Its savepoint would have to prove the tail above it
    # sorts after everything the savepoint folded, and the savepoint records no merge key
    # to prove that with, so such a fold neither resumes from one nor writes one.
    if known and units and not ordered:
        resumed = _resume_slot_fold(name, units, marks, slot=slot)
        if resumed is not None:
            _note_slot_fold_origin("savepoint")
            resumed = _persist_slot_fold(name, resumed, slot=slot, sampled=marks[-1])
            _remember_slot_fold(key, resumed)
            return _slot_checkpoint(name, resumed)
    # Nothing warm and no savepoint: every unit of the slot is streamed from its
    # first record. The counter exists to make that share visible.
    _note_slot_fold_origin("cold")
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    last_key: "tuple[int, str] | None" = None
    if ordered:
        merged = _MergedRead(
            name, [_Step(unit, 0, None) for unit in units], _lane_of(name, units, marks)
        )
        registry.prime(slot, _merged_ordinals(((u, e, lane) for _k, u, e, lane in merged), {}))
        heights: "dict[str, int]" = dict(merged.heights)
        points: "dict[str, ResumePoint]" = dict(merged.points)
        last_key = merged.last_key
    else:
        cold = _SlotStream(units)
        registry.prime(slot, cold)
        heights = dict(cold.heights)
        points = dict(cold.points)
    fresh = _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        marks=_folded_marks(units, marks, heights),
        reached=heights.get(units[-1], 0) if units else 0,
        # Nothing on disk is standing behind this value: a cold fold is what happens when
        # no savepoint could be used, so the next write is owed for the whole position.
        saved=0,
        resumes=points,
        last_key=last_key,
        # A cold fold is where the three rollover cases land -- a new unit, a unit
        # recreated under the same id, a rewritten prefix -- and every one of them can
        # leave ``reached`` unmoved or lower. The mint is what makes the value after them
        # orderable at all.
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )
    if known:
        if not ordered:
            fresh = _persist_slot_fold(name, fresh, slot=slot, sampled=marks[-1] if units else None)
        _remember_slot_fold(key, fresh)
    return _slot_checkpoint(name, fresh)


class _UnitTail:
    """One unit of a continuation: its entries above ``step.since``, oldest first.

    Opens the unit's log once and resumes at ``step.resume`` when the store confirms it,
    so a unit that grew by one entry costs that entry and the tail read of the open.
    :attr:`reached` is the last seq yielded (``step.since`` when nothing was), and
    :attr:`point` is where the walk stopped, sealed for the next continuation, or
    ``None`` when it yielded nothing.
    """

    def __init__(self, step: _Step) -> None:
        self.step = step
        self.reached = step.since
        self.point: "ResumePoint | None" = None
        self._handle: "CrewLog | None" = None
        self._cursor = ReadCursor()
        self._last: "Entry | None" = None

    def __iter__(self) -> "Iterator[Entry]":
        handle = open_session_log(self.step.unit)
        if handle is None:
            return
        self._handle = handle
        for entry in handle.iter_from(
            self.step.since + 1,
            known=KNOWN_TYPES,
            resume=self.step.resume,
            cursor=self._cursor,
        ):
            self.reached = max(self.reached, entry.seq)
            self._last = entry
            yield entry
        self.point = self.seal_last()

    def seal_last(self) -> "ResumePoint | None":
        """The point at the last entry yielded, sealed; for a walk stopped early too."""
        if self._handle is None or self._cursor.at is None:
            return None
        return self._handle.seal_point(self._cursor.at, expect=self._last)


class _MergedRead:
    """The entries a time-ordered fold takes from *steps*, merged across lanes by time.

    *lanes* maps each unit to its lane (:func:`_lane_of`, one per slot). A lane is its
    units' entries concatenated in list order, and the lanes are merged by ``(entry
    time, lane)`` with :func:`heapq.merge`, which never reorders one lane's entries.
    Entries that cannot move the fold are read and counted in the unit's height but not
    kept: they change no state, so where they sort is no question at all.

    Why the merge makes a middle-of-the-list append continuable. Say the cell holds the
    merge of every lane's old entries, new entries arrive only at the end of a lane
    (:func:`_continuable` refuses anything else), and every new entry sorts above the
    greatest key the cell folded. Then a cold merge of old plus new takes every old entry
    first -- a lane whose old entries ran out offers a new entry, which loses to every
    old head still waiting -- in exactly the order the cell took them, and only then
    merges the new ones. That is the same sequence a continuation produces by merging the new entries
    alone and driving them after the cell's state, so the warm value and the cold value
    are one value. The condition is checked, not assumed (each key against the
    cell's ``last_key``); an entry stamped at or below it -- a clock that stepped back, a
    write committed late -- folds cold instead.
    """

    def __init__(self, name: str, steps: "Sequence[_Step]", lanes: "Mapping[str, str]") -> None:
        self._fold = _FOLDS[name]
        self._steps = tuple(steps)
        self._lanes = lanes
        self.heights: "dict[str, int]" = {}
        self.points: "dict[str, ResumePoint]" = {}
        self.last_key: "tuple[int, str] | None" = None

    def __iter__(self) -> "Iterator[tuple[tuple[int, str], str, Entry, str]]":
        """Every kept entry as ``(key, unit, entry, lane slot)``, in merge order.

        STREAMED: each lane is read in batches of at most :data:`MERGE_BATCH` kept
        entries, and no file stays open between batches, so what the merge holds is a
        batch per lane however long the history is, and one open file at most. The lane
        slot is what a fold's ``from_lane`` hook is handed; empty for a unit whose header
        names no slot (a lane of its own). :attr:`last_key` is the greatest key yielded
        so far.
        """
        lanes: "dict[str, list[_Step]]" = {}
        for step in self._steps:
            lanes.setdefault(self._lanes.get(step.unit, f"unit:{step.unit}"), []).append(step)
        merged = heapq.merge(
            *(self._lane(lane, steps) for lane, steps in lanes.items()),
            key=lambda item: item[0],
        )
        for key, unit, entry in merged:
            if self.last_key is None or key > self.last_key:
                self.last_key = key
            lane = key[1]
            yield key, unit, entry, ("" if lane.startswith("unit:") else lane)

    def _lane(
        self, lane: str, steps: "Sequence[_Step]"
    ) -> "Iterator[tuple[tuple[int, str], str, Entry]]":
        """One lane's kept entries, its units in list order, read a batch at a time."""
        for step in steps:
            since, resume = step.since, step.resume
            self.heights[step.unit] = since
            while True:
                tail = _UnitTail(_Step(step.unit, since, resume))
                batch: "list[tuple[tuple[int, str], str, Entry]]" = []
                walk = iter(tail)
                full = False
                for entry in walk:
                    if self._fold.touched_by(entry):
                        batch.append(((entry.time, lane), step.unit, entry))
                        if len(batch) >= MERGE_BATCH:
                            full = True
                            break
                # Closing the generator closes the file before this lane waits on the
                # merge; the next batch reopens at the point this one sealed.
                close = getattr(walk, "close", None)
                if close is not None:
                    close()
                if tail.point is None and full:
                    tail.point = tail.seal_last()
                self.heights[step.unit] = tail.reached
                if tail.point is not None:
                    self.points[step.unit] = tail.point
                yield from batch
                if not full:
                    break
                since, resume = tail.reached, tail.point

    @property
    def reached(self) -> int:
        """The last unit's folded height, once the merge has been read to the end."""
        return self.heights.get(self._steps[-1].unit, 0) if self._steps else 0


#: Most kept entries one lane of a time merge holds at once (:class:`_MergedRead`).
MERGE_BATCH: Final[int] = 64


def _merged_ordinals(
    lanes: "Iterable[tuple[str, Entry, str]]", heights: "Mapping[str, int]"
) -> "Iterator[_Ordinal]":
    """*lanes* as kernel events, each at the slot's total folded height once it counts.

    *heights* is what the cell had folded of each unit before these entries. One unit's
    entries arrive in its own seq order, so each one raises that unit's height and the
    total with it: the ordinals rise strictly, and the first one lands above every
    ordinal the cell already holds, which never exceeds the total it started from.
    """
    seen = dict(heights)
    total = sum(seen.values())
    for unit, entry, slot in lanes:
        total += entry.seq - seen.get(unit, 0)
        seen[unit] = entry.seq
        yield _Ordinal(total, entry, slot)


def _continue_slot_fold(
    name: str,
    memo: _SlotMemo,
    units: "tuple[str, ...]",
    marks: "tuple[_UnitMark, ...]",
    steps: "Sequence[_Step]",
) -> "_SlotMemo | None":
    """*memo* carried forward over *steps*, the units :func:`_continuable` chose.

    Only those units are opened, each from the point its last walk stopped, so an
    advance costs the units that grew and their new entries. ``None`` when the new
    entries of a time-ordered fold do not all sort above what the cell folded, which
    the caller answers with a cold fold; nothing has been driven by then.
    """
    heights = {unit: mark.last_seq for unit, mark in zip(memo.units, memo.marks)}
    present = set(units)
    points = {unit: point for unit, point in memo.resumes.items() if unit in present}
    last_key = memo.last_key
    if _time_ordered(name):
        merged = _MergedRead(name, steps, _lane_of(name, units, marks))
        floor = last_key
        refused = False

        def admitted() -> "Iterator[tuple[str, Entry, str]]":
            nonlocal refused
            for key, unit, entry, slot in merged:
                if floor is not None and key <= floor:
                    refused = True
                    return
                yield unit, entry, slot

        for event in _merged_ordinals(admitted(), heights):
            memo.registry.drive(memo.store, event)
        if refused:
            # Already-driven entries changed this cell, and the caller folds cold into a
            # fresh registry that replaces it -- the lock this runs under means nothing
            # else reads the cell in between.
            return None
        heights.update(merged.heights)
        points.update(merged.points)
        if merged.last_key is not None:
            last_key = merged.last_key if last_key is None else max(last_key, merged.last_key)
    else:
        for step in steps:
            # Every unit after this one held nothing (:func:`_continuable`), so the units
            # before it are the whole folded height below it: its entries land above the
            # cell's watermark, on the ordinals a cold fold of the same units gives them.
            base = sum(heights.get(unit, 0) for unit in units[: units.index(step.unit)])
            tail = _UnitTail(step)
            for entry in tail:
                memo.registry.drive(memo.store, _Ordinal(base + entry.seq, entry))
            heights[step.unit] = tail.reached
            if tail.point is not None:
                points[step.unit] = tail.point
    return _SlotMemo(
        registry=memo.registry,
        store=memo.store,
        units=units,
        marks=_folded_marks(units, marks, heights),
        reached=heights.get(units[-1], 0),
        saved=memo.saved,
        resumes=points,
        last_key=last_key,
        revision=_minted_revision(),
        weight=_cell_weight(name, memo.registry, memo.store),
    )


def time_merged_read(name: str, unit_ids: Sequence[str]) -> _MergedRead:
    """A from-scratch read of time-ordered fold *name* over *unit_ids*, as a merge.

    The same merge the warm path uses (:class:`_MergedRead`), so the reference fold and
    the warm cell cannot disagree about the order of the same bytes. Iterate it for
    ``(key, unit, entry, lane slot)``; :attr:`_MergedRead.reached` is the newest unit's
    last seq once it is exhausted. A unit with no log is skipped, as everywhere else.
    """
    units = [unit for unit in unit_ids if CrewLog.exists(KIND_SESSION, unit)]
    marks = _unit_marks(units)
    return _MergedRead(name, [_Step(unit, 0, None) for unit in units], _lane_of(name, units, marks))


def _slot_cell(name: str, memo: _SlotMemo) -> "tuple[Any, int] | None":
    """*memo*'s kernel cell for *name* as ``(state, watermark)``, or ``None``.

    The watermark is the ORDINAL the cell stands at, which is the number a savepoint
    resumes it at. It is read off the live cell rather than derived from ``reached``,
    because the cell is what ``prime_checkpointed`` installs and a second derivation
    could disagree with it about the same pass.
    """
    try:
        return memo.registry.cells(memo.store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return None


def _identity_marks(marks: "Sequence[_UnitMark]") -> "list[list[Any]]":
    """*marks* as the JSON a slot savepoint's identity block compares verbatim.

    EVERY field of the mark, including ``cuts``, because the block's job is to be
    admitted on exactly the shapes :func:`_continuable` admits and that function compares
    an earlier unit's mark whole. An earlier unit that was cut therefore retires the
    savepoint here as it folds cold there, rather than the two disagreeing about the same
    unit vector.
    """
    return [[mark.origin, mark.last_seq, mark.size, mark.mtime_ns, mark.cuts] for mark in marks]


def _resume_slot_fold(
    name: str, units: "tuple[str, ...]", marks: "tuple[_UnitMark, ...]", *, slot: str
) -> "_SlotMemo | None":
    """A warm cell for *slot*'s *name*, resumed from the DISK savepoint, or ``None``.

    The same three steps a warm continuation takes, standing on state a previous PROCESS
    folded instead of on a cell in this one: the savepoint is admitted only if it
    describes this unit vector and the newest unit's prefix still hashes to what its
    writer read, the tail above it is folded through the kernel, and the result is a cell
    exactly like the one a continuation builds.

    The tail goes through ``prime_checkpointed`` rather than a seeding path of its own, so
    a resumed fold and a cold fold run one implementation over the same events -- the
    property that makes the resumed answer and the cold answer the same answer.

    ``None`` for no savepoint and for every refusal, which is the cold fold.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    handle = open_session_log(units[-1])
    if handle is None:
        return None
    # Unit *i*'s ordinal base is the total height of the units before it, which is the
    # derivation the warm continuation uses and the one the savepoint's own position is
    # checked against. It is reproducible here only because every earlier mark is in the
    # identity block and compared verbatim.
    base = sum(mark.last_seq for mark in marks[:-1])
    saved = savepoints.load_slot(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
    )
    if saved is None:
        return None
    # The newest unit's own evidence for a later continuation is its cut counter, already
    # in ``marks[-1]``, or -- on a unit without one -- the window the tail's walk seals
    # beside its resume point (below). The prefix under that point was admitted by the
    # savepoint's own witness on the way in.
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(_FOLDS[name], slot))
    tail = _SlotStream(units[-1:], base=base, since=saved.reached)
    try:
        registry.prime_checkpointed(
            slot,
            _HeldCheckpoints(
                {name: Checkpoint(name=name, last_seq=saved.watermark, state=saved.state)}
            ),
            {},
            lambda _floor: tail,
        )
        # RENDERED here, and the value thrown away. It is the admission condition the
        # shape checks cannot be: they read a state's top level, so a value malformed
        # BELOW it -- an event row holding a number where a mapping belongs -- is
        # admitted, survives a tail that touches nothing, and raises in the render the
        # caller performs AFTER this function has returned. The ledger's reader answers
        # that by logging and serving the empty record, so a payload like this would
        # empty a live slot's ledger on every read for the life of the unit. A state no
        # reader can be served is not a state to resume from, and asking costs one render
        # of a bounded value on a read that resumed.
        _FOLDS[name].render(registry.cells(slot)[name][0])
    except Exception:
        # A state that passed every admission condition and then could not be FOLDED or
        # rendered. The file is retired and the caller folds cold, which is the answer
        # ``_drive_session`` gives for a session savepoint and for the same reason --
        # refusing it on the way in would take a per-fold walk of every nested container.
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log slot %s could not fold its resumed state; discarding and folding cold",
            slot,
        )
        savepoints.discard_slot(handle, (name,))
        return None
    # The savepoint was admitted BEFORE this pass, and that window is not empty. The
    # store's supersede repair replaces orphan chunk records at seqs the savepoint
    # already consumed; the tail starts above them so this fold never reads the
    # replacement, and ``before`` was captured after the repair so it matches the
    # repaired file -- every check on this side passes while the state came from bytes
    # the repair removed. So the savepoint is re-admitted against the file as it stands
    # now, and one that fails to describe it costs a cold fold.
    if not savepoints.slot_resume_still_verifies(
        handle,
        name,
        slot=slot,
        units=units,
        marks=_identity_marks(marks[:-1]),
        ordinal_base=base,
        resumed=saved,
    ):
        logger.debug(
            "crew log slot %s moved under its own savepoint while folding; folding cold", slot
        )
        return None
    return _SlotMemo(
        registry=registry,
        store=slot,
        units=units,
        # This pass re-stat'ed every unit, and the savepoint's identity block confirmed
        # the earlier ones are the vector its state was folded over, so only the newest
        # unit's height comes from the stream.
        marks=_folded_marks(units, marks, tail.heights),
        reached=tail.reached,
        saved=saved.watermark,
        # The tail's own point, sealed by the walk that folded it. Without one the next
        # continuation walks the newest unit from its first record, and on a unit with no
        # cut counter it has no evidence at all and folds cold -- after which the cold
        # fold finds the savepoint again and resumes, so every advance would pay the
        # savepoint's whole-prefix check. A tail that yielded nothing issues no point.
        resumes=dict(tail.points),
        revision=_minted_revision(),
        weight=_cell_weight(name, registry, slot),
    )


def _persist_slot_fold(
    name: str, memo: _SlotMemo, *, slot: str, sampled: "_UnitMark | None"
) -> _SlotMemo:
    """*memo*, with its savepoint on disk brought forward when a write is owed.

    How far a fold must have moved to earn a write is the savepoint module's decision
    (``checkpoint.write_is_earned``), not this one's: it is a property of the files, and
    stating it here as well would give two places an answer that has to agree.

    A WARM CONTINUATION WRITES TOO, and that is the one place this departs from the
    session half, where a pass standing on a cached bundle writes nothing. The reason the
    session half refuses is that a cached bundle records no evidence, so nothing a later
    pass can read says anything about the bytes below its seq. A slot cell is different:
    it CARRIES its evidence -- the newest unit's cut count in ``_SlotMemo.marks`` -- and
    :func:`_folded_prefix_holds` re-checks it before carrying the cell forward, so custody
    of the already-folded prefix survives from the pass that folded it to this one. A
    unit that states no count writes no savepoint: the only witness the savepoint format
    has for one is a digest of the whole folded prefix, which is the cost an eager
    advance must not pay.
    Without this, a gateway that stays up leaves the savepoint wherever the process first
    folded, and the restart it exists for replays the whole gap.

    *sampled* is the newest unit's mark as read BEFORE the pass, and three of its fields
    do the work. Its ``origin`` is the identity the pass folded under, which the write
    refuses to differ from. Its ``last_seq`` is the boundary the evidence was taken at, so
    a pass that ended somewhere ELSE -- the file grew under it -- matches nothing and
    writes nothing: the evidence would then cover fewer bytes than the state was folded
    from, which is the one savepoint no later read recovers from. That costs the savepoint
    and never the read, which is already served. And its ``cuts`` is the witness.

    THE EVIDENCE IS CONFIRMED HERE, at the write: the counter is read again
    (:func:`_slot_witness`), and a cut during the pass RAISES it, so a savepoint is never
    written with a witness that agrees with bytes the state did not come from.

    Never for a time-ordered fold (:func:`_time_ordered`), whose savepoint could not
    prove its tail sorts last.
    """
    if not memo.units or sampled is None or sampled.origin is None:
        return memo
    if sampled.cuts is None or _time_ordered(name):
        return memo
    if memo.reached != sampled.last_seq:
        return memo
    if memo.weight > slot_fold_cache_bytes():
        # A cell too big to keep WARM is one whose savepoint the store would refuse too:
        # its cap is :data:`~kiro_crew.crew_log.checkpoint.MAX_CHECKPOINT_BYTES`, two
        # orders of magnitude under this ceiling. The refusal is not the reason to skip --
        # the store swallows it and the read is unaffected -- the MEMORY is: the store
        # decides by serializing first, so attempting a ``radar`` cell at its declared
        # caps would build a 2.4 GiB string to measure it and throw it away. And such a
        # cell is not kept warm (:func:`_remember_slot_fold`), so every read of it would
        # arrive here again. The charge is an upper bound that overestimates by a small
        # factor, so this cannot skip a cell whose payload would really have fit.
        return memo
    cell = _slot_cell(name, memo)
    if cell is None:
        return memo
    _state, watermark = cell
    from kiro_crew.crew_log import checkpoint as savepoints

    if not savepoints.write_is_earned(watermark, memo.saved):
        return memo
    handle = open_session_log(memo.units[-1])
    if handle is None:
        return memo
    witness = _slot_witness(handle, memo, sampled)
    if witness is None:
        return memo
    written = savepoints.save_slot(
        handle,
        name,
        slot=slot,
        units=memo.units,
        marks=_identity_marks(memo.marks[:-1]),
        ordinal_base=sum(mark.last_seq for mark in memo.marks[:-1]),
        state=_state,
        watermark=watermark,
        prefix=witness,
        expect_origin=sampled.origin,
    )
    if not written:
        return memo
    return replace(memo, saved=watermark)


def _slot_witness(handle: CrewLog, memo: _SlotMemo, sampled: _UnitMark) -> "CutWitness | None":
    """The evidence *memo*'s savepoint records, re-confirmed now, or ``None``.

    The cut count where the unit states one: *sampled*'s reading, taken before the pass,
    checked against the live file here. A cut in between raises the live count, so this
    answers ``None`` and the savepoint is skipped rather than written with a witness its
    own resume could never admit.

    ``None`` for a unit with no counter: the digest witness the format also accepts
    would hash every folded record, and this runs on the eager advance.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    if sampled.cuts is None:
        return None
    held = savepoints.CutWitness(seq=memo.reached, cuts=sampled.cuts)
    return held if savepoints.cuts_unchanged(handle, held) else None


def _cell_weight(name: str, registry: ProjectionRegistry, store: str) -> int:
    """What the cell *registry* holds for *name* is charged, read off the live cell.

    Counted from the state the kernel actually holds rather than from the checkpoint,
    because the charge has to describe the object that stays resident -- and that is the
    cell, which the checkpoint only borrows a reference to.
    """
    try:
        state, _watermark = registry.cells(store)[name]
    except Exception:  # pragma: no cover - a cell the registry did not build
        return slot_fold_cell_bytes(name)
    return slot_fold_cell_bytes(name, state)


def _resume_for(memo: "_SlotMemo", unit: str, height: int) -> "ResumePoint | None":
    """*memo*'s resume point for *unit*, but only where it agrees with what was folded.

    The point and the unit's folded *height* are two records of the same fact -- the
    last entry this cell consumed from that unit -- written by the same loop, so they
    agree by construction today. This asserts it anyway, because the two failures are
    not the same size. A point BELOW the height costs a few extra records on the next
    read. A point ABOVE it would make the next read open the file past entries the cell
    never folded and never yield them, which is state silently missing from a fold that
    reports itself complete.

    So a point above the height drops back to the full walk: the store's own checks
    cannot catch this one, since an offset that is a perfectly good record boundary for
    the seq it names is exactly what this would hand them. A point BELOW it is kept: the
    height can sit above the last entry yielded when the records after it were skipped
    (an ignorable type this reader does not know), see :func:`_folded_marks`.
    """
    point = memo.resumes.get(unit)
    if point is None or point.seq > height:
        return None
    return point


def _folded_marks(
    units: "tuple[str, ...]",
    sampled: "tuple[_UnitMark, ...]",
    heights: "Mapping[str, int]",
) -> "tuple[_UnitMark, ...]":
    """*sampled*, with each streamed unit's height replaced by what was FOLDED.

    This is where :func:`fold_slot_warm`'s invariant is applied: the sampled heights are
    that read's plan, and what a memo remembers is what its pass consumed.

    The FINGERPRINT is carried across as sampled, and deliberately not re-stat'ed after
    the pass. A fingerprint taken afterwards would cover bytes this pass may not have
    folded -- an entry appended between the last read and that stat -- and the next read
    would then compare equal and serve a cell that is missing it. Sampled, the error can
    only fall the other way: a slot whose bytes moved mid-pass fails the comparison, and
    the next read folds cold, which costs time and not correctness.

    *heights* is what the pass folded of each unit it read. A unit it did not read keeps
    its sampled mark, which a continuation has already found equal to the cell's own.

    Built by REPLACING one field of the sampled mark rather than by naming the fields to
    keep. A mark is compared whole, so a field this function forgot to carry would read
    as changed on the very next pass and send every warm read of every slot back to a
    cold fold -- a mark rebuilt field by field makes that the default outcome of adding
    one.
    """
    return tuple(
        _folded_mark(sampled[index], heights.get(unit)) for index, unit in enumerate(units)
    )


def _folded_mark(sampled: _UnitMark, folded: "int | None") -> _UnitMark:
    """*sampled* restated for a unit this pass folded through seq *folded*.

    THREE CASES. Folded exactly what was sampled: only the height is restated. Folded
    PAST it -- an append landed between the sample and the walk -- and the sampled
    fingerprint describes fewer bytes than were folded, so it is dropped (``None``): the
    next pass then finds the files moved at the same height and refreshes the mark on
    the cut-count or window proof, where a kept fingerprint would read as an in-place
    rewrite and fold every unit cold on a busy log. Folded SHORT of it: every record
    after the last entry yielded was skipped (an ignorable type this reader does not
    know, or a damaged line), so the unit is folded through the sampled height, and a
    later pass does not re-read that tail as growth.
    """
    if folded is None or folded == sampled.last_seq:
        return sampled if folded is None else replace(sampled, last_seq=folded)
    if folded > sampled.last_seq:
        return replace(sampled, last_seq=folded, size=None, mtime_ns=None)
    return sampled


def _slot_checkpoint(name: str, memo: _SlotMemo) -> "tuple[Checkpoint, int]":
    """*memo*'s kernel cell as the checkpoint this module's callers carry, and its revision.

    ``last_seq`` is the newest unit's OWN seq, not the kernel's ordinal, and that is
    the contract rather than an implementation detail: a writer advances this
    checkpoint over the entry it is appending to answer with the record that entry
    produces, and that entry's seq comes from its file. An ordinal here would sit far
    above it and :func:`advance` would refuse the entry as already folded.

    The state comes back AS THE CELL HOLDS IT, not copied, which is what the folds'
    own discipline makes safe: ``apply`` returns a new object rather than editing this
    one, so a later read replaces the cell's state instead of moving what a caller
    kept, and the callers that continue this checkpoint go through :func:`advance`,
    which copies before its first step.
    """
    state, _watermark = memo.registry.cells(memo.store)[name]
    return Checkpoint(name=name, last_seq=max(memo.reached, 0), state=state), memo.revision
