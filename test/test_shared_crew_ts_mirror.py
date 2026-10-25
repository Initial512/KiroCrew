"""Shared Crew repair: the TypeScript capabilities mirror carries reasons and every reason code."""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.shared_crew.admission import (
    ReasonCode,
)

REPO = Path(__file__).resolve().parent.parent


def _union(source: str, name: str) -> set[str]:
    body = source.split(f"export type {name} =", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"'([a-z_]+)'", body))


def test_typescript_capabilities_mirror_names_every_reason():
    from kiro_crew.shared_crew.access import FORWARDED_ATTRIBUTION_REASONS
    from kiro_crew.shared_crew.capabilities import REASON_NO_SOURCE, REASON_SOURCE_FAILED

    source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")
    block = source.split("export interface SharedCrewCapabilities", 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"^\s+reasons: ", block, re.M)
    assert re.search(r"^\s+setup_paths\?: ", block, re.M)
    assert "enterprise" not in block
    assert _union(source, "CapabilityReason") == {
        REASON_NO_SOURCE,
        REASON_SOURCE_FAILED,
        *FORWARDED_ATTRIBUTION_REASONS,
    }
    assert _union(source, "ReasonCode") == {r.value for r in ReasonCode}


def test_typescript_mirror_names_the_setup_paths():
    from kiro_crew.shared_crew.access import SETUP_PATHS

    source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")
    assert _union(source, "SetupPath") == set(SETUP_PATHS)
