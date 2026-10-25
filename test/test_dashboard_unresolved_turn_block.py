"""A fold field that does not resolve is reported to the CREWMATE, never to the person.

The Dashboard frame draws no band about such a field (``test_dashboard_frame.py``
pins that); the ``[DASHBOARD]`` turn block names it instead, with why, and drops it
once it resolves. These cases pin that line and the read behind it.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import dashboard_agentic, dashboard_feed
from kiro_crew.dashboard.handlers import member_dashboard
from kiro_crew.dashboard_feed import FOLD_UNSEEN, OLDER_VALUE, PATH_MISSING, FieldRead
from kiro_crew.dashboard_templates import instance
from kiro_crew.dashboard_templates.manifest import parse_manifest

SLUG = "code-reviewer"
MEMBER = "Code Reviewer"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Own data home, and ``project-report`` as the page named for the crewmate."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(
        instance, "default_template_id", lambda _slug, _ids: "project-report"
    )
    _floor_monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: True)
    dashboard_feed.forget_resolved()
    yield
    dashboard_feed.forget_resolved()


def _fold_fields() -> list[str]:
    page = dashboard_agentic.page_in_force(SLUG)
    assert page is not None
    _record, manifest = page
    names = sorted(name for name, spec in manifest.fields.items() if not spec.agentic)
    assert names, "the page in force binds no fold field; these cases guard nothing"
    return names


#: The prime waits each `_reads` stub saw, so a test can check the turn never waits.
_WAITS: list[list[Any]] = []


def _reads(monkeypatch: pytest.MonkeyPatch, unresolved: dict[str, str]) -> list[Any]:
    """Make the page's own read report *unresolved*; return the calls it saw."""
    calls: list[Any] = []
    waits: list[Any] = []
    _WAITS[:] = []

    def read_fields(slug: str, member: str, manifest: Any, **kw: Any) -> FieldRead:
        calls.append((slug, member))
        waits.append(kw.get("prime_wait"))
        return FieldRead(missing=sorted(unresolved), unresolved=dict(unresolved))

    monkeypatch.setattr(member_dashboard, "read_fields", read_fields)
    _WAITS.append(waits)
    return calls


class TestTheTurnBlock:
    def test_the_turn_never_waits_for_a_cold_fold(self, monkeypatch) -> None:
        """A prompt build must not wait on a background prime."""
        _reads(monkeypatch, {_fold_fields()[0]: PATH_MISSING})
        dashboard_agentic.turn_block(SLUG, MEMBER)
        assert _WAITS[-1] == [0], _WAITS

    def test_an_unresolved_field_is_named_with_its_reason(self, monkeypatch) -> None:
        name = _fold_fields()[0]
        _reads(monkeypatch, {name: PATH_MISSING})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        line = block.splitlines()[-2] if "Upkeep" in block.splitlines()[-1] else block
        assert f"{name} (" in block, block
        assert "has nothing at" in block, block
        assert "dashboard_save" in line and "dashboard_types" in line, line

    def test_a_stale_field_is_named_as_an_older_value(self, monkeypatch) -> None:
        name = _fold_fields()[0]
        _reads(monkeypatch, {name: OLDER_VALUE})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert f"{name} (it stopped resolving, so the page still shows an older value)" in block

    def test_a_fold_never_seen_is_not_called_broken(self, monkeypatch) -> None:
        """A fresh crewmate on its shipped page has folds with no value yet. That is
        not a binding it can fix, so the block says nothing about it."""
        names = _fold_fields()
        _reads(monkeypatch, {name: FOLD_UNSEEN for name in names})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert "did not resolve" not in block, block

    def test_only_the_fixable_fields_are_named_beside_an_unseen_one(self, monkeypatch) -> None:
        first, second = _fold_fields()[:2]
        _reads(monkeypatch, {first: FOLD_UNSEEN, second: PATH_MISSING})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert f"{second} (" in block and f"{first} (" not in block, block

    def test_a_locked_page_makes_no_read_and_no_nudge(self, monkeypatch) -> None:
        """A locked page refuses dashboard_apply, so the fix the line names is one the
        crewmate cannot make; the lock line already says the person holds it."""
        instance.set_structure_lock(SLUG, True)
        calls = _reads(monkeypatch, {_fold_fields()[0]: PATH_MISSING})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert instance.LOCKED_REFUSAL in block, block
        assert "did not resolve" not in block and calls == [], block

    def test_with_the_crew_panel_off_no_nudge_is_made(self, monkeypatch) -> None:
        """No kirocrew-panel means no dashboard_save to call."""
        monkeypatch.setattr(dashboard_agentic, "_crew_panel_on", lambda: False)
        calls = _reads(monkeypatch, {_fold_fields()[0]: PATH_MISSING})
        assert "did not resolve" not in dashboard_agentic.turn_block(SLUG, MEMBER)
        assert calls == []

    def test_the_line_leaves_once_the_field_resolves(self, monkeypatch) -> None:
        name = _fold_fields()[0]
        _reads(monkeypatch, {name: PATH_MISSING})
        assert name + " (" in dashboard_agentic.turn_block(SLUG, MEMBER)
        _reads(monkeypatch, {})
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert "did not resolve" not in block, block
        assert block.startswith("[DASHBOARD]\n"), block

    def test_without_a_member_no_read_is_made(self, monkeypatch) -> None:
        """The apply reply builds the block with no crew name: it reads nothing."""
        calls = _reads(monkeypatch, {_fold_fields()[0]: PATH_MISSING})
        assert "did not resolve" not in dashboard_agentic.turn_block(SLUG)
        assert calls == []

    def test_a_read_that_fails_reports_nothing(self, monkeypatch) -> None:
        """A turn must not call a binding broken on a read that never happened."""

        def boom(*_a: Any, **_kw: Any) -> FieldRead:
            raise OSError("the log is unreadable")

        monkeypatch.setattr(member_dashboard, "read_fields", boom)
        block = dashboard_agentic.turn_block(SLUG, MEMBER)
        assert block.startswith("[DASHBOARD]\n") and "did not resolve" not in block

    def test_the_member_section_passes_the_crew_name(self, monkeypatch) -> None:
        from unittest.mock import patch

        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.context_assembly.member import build_member_section

        calls = _reads(monkeypatch, {_fold_fields()[0]: PATH_MISSING})
        cfg = KiroCrewConfig()
        cfg.agents = {SLUG: KiroCrewAgentConfig(kiro_agent="kirocrew-autofix")}
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=cfg):
            section = build_member_section(SLUG)
        assert calls and calls[0] == (SLUG, SLUG), calls
        assert "Fields on your page that did not resolve" in section


def _manifest(**fields: Any) -> Any:
    return parse_manifest(
        {
            "id": "demo",
            "version": 1,
            "title": "Demo",
            "description": "A demo",
            "source": "user",
            "fields": fields,
        }
    )


class TestTheLine:
    def test_names_are_capped_and_the_rest_counted(self) -> None:
        fields = {
            f"f{i}": {"type": "number", "source": {"fold": "work", "path": f"a.p{i}"}}
            for i in range(5)
        }
        line = dashboard_agentic.unresolved_line(
            _manifest(**fields), {f"f{i}": PATH_MISSING for i in range(5)}
        )
        assert "f0 (" in line and "f2 (" in line and "f3 (" not in line
        assert "and 2 more" in line

    def test_an_agentic_field_is_never_in_the_line(self) -> None:
        """Only fold fields: an agentic field has its own upkeep text."""
        manifest = _manifest(
            n={"type": "number", "source": {"agentic": True}},
        )
        assert dashboard_agentic.unresolved_line(manifest, {"gone": PATH_MISSING}) == ""

    def test_an_unseen_fold_alone_is_no_line(self) -> None:
        manifest = _manifest(n={"type": "number", "source": {"fold": "work", "path": "a.b"}})
        assert dashboard_agentic.unresolved_line(manifest, {"n": FOLD_UNSEEN}) == ""

    def test_nothing_unresolved_is_no_line(self) -> None:
        manifest = _manifest(n={"type": "number", "source": {"fold": "work", "path": "a.b"}})
        assert dashboard_agentic.unresolved_line(manifest, {}) == ""

    def test_the_path_reason_names_the_fold_and_path(self) -> None:
        manifest = _manifest(n={"type": "number", "source": {"fold": "work", "path": "a.b"}})
        line = dashboard_agentic.unresolved_line(manifest, {"n": PATH_MISSING})
        assert "n (the work fold has nothing at a.b)" in line


def _tree_manifest() -> Any:
    """A package-shaped manifest binding one TREE fold, which has no bus scope."""
    from types import SimpleNamespace

    from kiro_crew.crew_log.projection import WORKTREE_FOLD_NAME

    spec = SimpleNamespace(fold=WORKTREE_FOLD_NAME, path="boards", agentic=False)
    return SimpleNamespace(fields={"boards": spec}, folds=frozenset({WORKTREE_FOLD_NAME}))


class TestATreeField:
    """The feed cannot subscribe to a tree fold, so it reports the field unresolved;
    each reader that can read the tree takes it back out once it resolves."""

    def test_the_turn_block_reads_the_tree_and_drops_a_field_that_resolves(
        self, monkeypatch
    ) -> None:
        from kiro_crew.crew_log import projection

        manifest = _tree_manifest()
        _reads(monkeypatch, {"boards": FOLD_UNSEEN})
        monkeypatch.setattr(member_dashboard, "_dashboard_slot", lambda _m, _s: "slot-a")
        monkeypatch.setattr(
            projection,
            "read_tree_projection",
            lambda _root, _fold: type("P", (), {"value": {"boards": []}})(),
        )
        assert dashboard_agentic._unresolved_fold_fields(SLUG, MEMBER, manifest) == {}
        assert "boards" in dashboard_feed.resolved_before("slot-a")

    def test_the_turn_block_keeps_a_tree_field_whose_read_fails(self, monkeypatch) -> None:
        from kiro_crew.crew_log import projection

        def boom(_root: str, _fold: str) -> Any:
            raise OSError("unreadable")

        _reads(monkeypatch, {"boards": OLDER_VALUE})
        monkeypatch.setattr(member_dashboard, "_dashboard_slot", lambda _m, _s: "slot-a")
        monkeypatch.setattr(projection, "read_tree_projection", boom)
        found = dashboard_agentic._unresolved_fold_fields(SLUG, MEMBER, _tree_manifest())
        assert found == {"boards": OLDER_VALUE}

    def test_the_live_page_takes_a_resolved_tree_field_out_of_unresolved(self) -> None:
        import threading
        from types import SimpleNamespace

        from kiro_crew.crew_log.projection import WORKTREE_FOLD_NAME
        from kiro_crew.dashboard.handlers.member_dashboard_fields import LivePage

        page = SimpleNamespace(
            _lock=threading.RLock(),
            _trees={WORKTREE_FOLD_NAME: {"boards": [1]}},
            _manifest=_tree_manifest(),
            _tree_folds=frozenset({WORKTREE_FOLD_NAME}),
            _feed=SimpleNamespace(slot="slot-b"),
        )
        read = FieldRead(missing=["boards"], unresolved={"boards": FOLD_UNSEEN})
        LivePage._resolve_trees(page, read)  # type: ignore[arg-type]
        assert read.fields == {"boards": [1]}
        assert read.missing == [] and read.unresolved == {}
        assert "boards" in dashboard_feed.resolved_before("slot-b")


class TestTheRetainedNames:
    """``remember_resolved`` keeps a bounded set per slot."""

    def test_a_name_the_page_no_longer_declares_is_forgotten(self) -> None:
        dashboard_feed.remember_resolved("slot-c", ["old"], declared=["old"])
        dashboard_feed.remember_resolved("slot-c", ["new"], declared=["new"])
        assert dashboard_feed.resolved_before("slot-c") == {"new"}

    def test_one_slot_never_holds_more_than_the_cap(self) -> None:
        cap = dashboard_feed._RESOLVED_NAMES_MAX
        for batch in range(4):
            names = [f"f{batch}-{i}" for i in range(cap)]
            dashboard_feed.remember_resolved("slot-d", names)
        assert len(dashboard_feed.resolved_before("slot-d")) <= cap

    def test_the_feed_forgets_a_renamed_field(self) -> None:
        from types import SimpleNamespace

        def manifest(path_field: str) -> Any:
            return _manifest(
                **{path_field: {"type": "number", "source": {"fold": "work", "path": "a.b"}}}
            )

        feed = dashboard_feed.DashboardFeed(slot="slot-e")
        feed._cells["work"] = dashboard_feed._Cell()
        feed._accept("work", SimpleNamespace(revision=1, seq=1, value={"a": {"b": 1}}))
        feed.read(manifest("first"))
        feed.read(manifest("second"))
        assert dashboard_feed.resolved_before("slot-e") == {"second"}


class TestThePanelField:
    def test_a_panel_never_published_is_unseen_not_a_bad_path(self) -> None:
        manifest = _manifest(
            note={"type": "string", "source": {"fold": "panel", "path": "data.note"}}
        )
        read = dashboard_feed.DashboardFeed(slot="slot-f").read(manifest)
        assert read.unresolved == {"note": FOLD_UNSEEN}

    def test_a_published_panel_without_the_path_is_a_bad_path(self) -> None:
        manifest = _manifest(
            note={"type": "string", "source": {"fold": "panel", "path": "data.note"}}
        )
        record = {"template": "t", "data": {}, "published_at": "2026-10-25T00:00:00Z"}
        read = dashboard_feed.DashboardFeed(slot="slot-g", panel_record=record).read(manifest)
        assert read.unresolved == {"note": PATH_MISSING}


class TestAMisspelledTreePath:
    """A tree fold has no feed cell, so the feed says ``fold_unseen``. When the tree
    read HAS a value and the path is absent, both readers turn that into
    ``path_missing`` so the crewmate is told."""

    def test_the_turn_block_names_a_misspelled_tree_path(self, monkeypatch) -> None:
        from kiro_crew.crew_log import projection

        _reads(monkeypatch, {"boards": FOLD_UNSEEN})
        monkeypatch.setattr(member_dashboard, "_dashboard_slot", lambda _m, _s: "slot-h")
        monkeypatch.setattr(
            projection,
            "read_tree_projection",
            lambda _root, _fold: type("P", (), {"value": {"board": []}})(),
        )
        found = dashboard_agentic._unresolved_fold_fields(SLUG, MEMBER, _tree_manifest())
        assert found == {"boards": PATH_MISSING}

    def test_the_live_page_marks_a_misspelled_tree_path(self) -> None:
        import threading
        from types import SimpleNamespace

        from kiro_crew.crew_log.projection import WORKTREE_FOLD_NAME
        from kiro_crew.dashboard.handlers.member_dashboard_fields import LivePage

        page = SimpleNamespace(
            _lock=threading.RLock(),
            _trees={WORKTREE_FOLD_NAME: {"board": []}},
            _manifest=_tree_manifest(),
            _tree_folds=frozenset({WORKTREE_FOLD_NAME}),
            _feed=SimpleNamespace(slot="slot-i"),
        )
        read = FieldRead(missing=["boards"], unresolved={"boards": FOLD_UNSEEN})
        LivePage._resolve_trees(page, read)  # type: ignore[arg-type]
        assert read.missing == ["boards"] and read.unresolved == {"boards": PATH_MISSING}
