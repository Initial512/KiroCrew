"""The ``edition_check`` acceptance kind of the bundled evaluator.

``edition_check`` is the seam through which an installed edition answers for work
the evaluator cannot read itself. The core names no check: it validates two
fields, runs ``kirocrew acceptance-check --json <check> <ref>``, and believes the
answer only when the exit code and the printed report agree. Three properties are
pinned here, each the way the rest of the evaluator's tests pin them:

* **Spec validation.** A malformed field is an ``error`` verdict and costs no
  subprocess.
* **No spec text in argv.** The argv is the fixed template with only the validated
  fields substituted, whatever else the spec carries.
* **Exit-code mapping.** Each agreeing outcome becomes exactly one verdict, and an
  outcome the evaluator cannot vouch for is an ``error``, never ``fail`` or
  ``pass``.

No network and no real ``kirocrew``: ``subprocess.run`` is replaced by a script
that records every argv and answers from a fixed list. The check name and the
reference below are placeholders; no edition's real check is named in the core.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import work_ledger as wl
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SCRIPT = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "scripts" / "accept_eval.py"

CHECK = "example-review"
REF = "ITEM-42"
ARGV = ["kirocrew", "acceptance-check", "--json", CHECK, REF]


class _Proc:
    """The three ``CompletedProcess`` fields the evaluator reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load():
    return load_skill_script("_accept_eval_edition_check_under_test", SCRIPT)


def _wire(mod, monkeypatch, answers):
    """Answer ``subprocess.run`` from ``answers`` in order; return the argvs seen.

    An answer that is an exception instance is raised instead of returned.
    """
    seen: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        answer = answers[len(seen) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return seen


def _spec(**fields):
    return {"accept": {"kind": "edition_check", "check": CHECK, "ref": REF, **fields}}


def _report(verdict, check=CHECK, ref=REF, summary="the summary line"):
    return json.dumps({"check": check, "ref": ref, "verdict": verdict, "summary": summary})


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        7,
        True,
        "Example",  # upper case
        "1example",  # must start with a letter
        "-example",
        "example_review",
        "example review",
        "example\n",
        "a" * 33,
        "examp\u0142e",  # non-ASCII letter
    ],
)
def test_a_malformed_check_name_is_rejected_before_any_subprocess(monkeypatch, bad):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, evidence = mod._evaluate(_spec(check=bad))
    assert verdict == "error", bad
    assert "check name" in evidence
    assert seen == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        42,
        True,
        "-ITEM-1",
        "--help",
        ".hidden",
        "ITEM-1\n",
        " ITEM-1",
        "ITEM-1 --verbose",
        "ITEM/1",
        "https://example.invalid/items/1",
        "I" * 129,
        "ITEM-\u0663\u0661",  # non-ASCII digits
    ],
)
def test_a_malformed_ref_is_rejected_before_any_subprocess(monkeypatch, bad):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    verdict, evidence = mod._evaluate(_spec(ref=bad))
    assert verdict == "error", bad
    assert "needs a ref" in evidence
    assert seen == []


@pytest.mark.parametrize("missing", ["check", "ref"])
def test_both_fields_are_required(monkeypatch, missing):
    mod = _load()
    seen = _wire(mod, monkeypatch, [])
    spec = _spec()
    del spec["accept"][missing]
    verdict, _ = mod._evaluate(spec)
    assert verdict == "error"
    assert seen == []


def test_the_longest_valid_fields_are_accepted(monkeypatch):
    mod = _load()
    check, ref = "a" * 32, "R" * 128
    seen = _wire(mod, monkeypatch, [_Proc(0, _report("pass", check=check, ref=ref))])
    verdict, _ = mod._evaluate(_spec(check=check, ref=ref))
    assert verdict == "pass"
    assert seen == [["kirocrew", "acceptance-check", "--json", check, ref]]


# --------------------------------------------------------------------------- #
# No spec text reaches argv
# --------------------------------------------------------------------------- #

_HOSTILE_EXTRAS = {
    "argv": ["sh", "-c", "id"],
    "command": "id",
    "cmd": "id",
    "shell": "id",
    "args": ["--hostname", "evil.example"],
    "flags": ["--verbose"],
}


def test_only_the_fixed_template_runs(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, _report("pass"))])
    verdict, _ = mod._evaluate(_spec(**_HOSTILE_EXTRAS))
    assert verdict == "pass"
    assert seen == [ARGV]


def test_every_argv_element_is_a_template_literal_or_a_validated_field(monkeypatch):
    mod = _load()
    seen = _wire(mod, monkeypatch, [_Proc(0, _report("pass"))])
    mod._evaluate(_spec(**_HOSTILE_EXTRAS))
    [argv] = seen
    assert argv[0] in mod._SELF_BUILT_COMMANDS
    for element in argv:
        assert element in {"kirocrew", "acceptance-check", "--json", CHECK, REF}, element


def test_the_self_built_commands_are_exactly_gh_and_kirocrew():
    mod = _load()
    assert mod._SELF_BUILT_COMMANDS == {"gh", "kirocrew"}


def test_the_ledger_mirror_uses_the_evaluators_own_patterns():
    """``is_acceptance_concrete`` duplicates both across a process boundary, so they
    are pinned equal rather than trusted to stay in step."""
    mod = _load()
    assert wl.ACCEPTANCE_CHECK_NAME.pattern == mod._CHECK_NAME.pattern
    assert wl.ACCEPTANCE_CHECK_REF.pattern == mod._CHECK_REF.pattern


# --------------------------------------------------------------------------- #
# Exit-code mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("returncode", "report_verdict", "verdict"),
    [
        (0, "pass", "pass"),
        (8, "pending", "pending"),
        (1, "fail", "fail"),
        (3, "needs_person", "refused"),
    ],
)
def test_each_agreeing_outcome_maps_to_one_verdict(
    monkeypatch, returncode, report_verdict, verdict
):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(returncode, _report(report_verdict, summary="why it reads so"))])
    got, evidence = mod._evaluate(_spec())
    assert got == verdict
    assert "why it reads so" in evidence


def test_a_needs_person_refusal_says_a_person_decides(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(3, _report("needs_person", summary="landed without X"))])
    verdict, evidence = mod._evaluate(_spec())
    assert verdict == "refused"
    assert "a person decides" in evidence


def test_exit_2_means_no_edition_provides_the_check(monkeypatch):
    """The public ``kirocrew`` has no ``acceptance-check``: argparse exits 2. An
    edition's command exits 2 too for a check name it does not know."""
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(2, "", "kirocrew: error: argument <command>: invalid choice")])
    verdict, evidence = mod._evaluate(_spec())
    assert verdict == "error"
    assert f"no installed edition provides the acceptance check {CHECK!r}" in evidence


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        # A crash also exits 1; with no report it must not read as a failed check.
        (1, "Traceback (most recent call last):\n  ...\nKeyError: 'x'\n"),
        # The exit code and the report disagree, in both directions.
        (0, _report("fail")),
        (1, _report("pass")),
        (8, _report("pass")),
        # A report about some other check or reference.
        (0, _report("pass", check="other-check")),
        (0, _report("pass", ref="ITEM-43")),
        # Not one JSON object.
        (0, "pass"),
        (0, json.dumps(["pass"])),
        (0, _report("pass") + "\nextra line"),
        (0, "[" * 100_000),
        # An exit code with no meaning in the contract.
        (4, _report("pass")),
        (137, ""),
    ],
    ids=[
        "crash",
        "pass-exit-fail-report",
        "fail-exit-pass-report",
        "pending-exit-pass-report",
        "other-check",
        "other-ref",
        "bare-word",
        "json-list",
        "trailing-line",
        "deeply-nested",
        "unknown-exit",
        "killed",
    ],
)
def test_an_outcome_the_evaluator_cannot_vouch_for_is_an_error(monkeypatch, returncode, stdout):
    mod = _load()
    _wire(mod, monkeypatch, [_Proc(returncode, stdout)])
    verdict, _ = mod._evaluate(_spec())
    assert verdict == "error", (returncode, stdout[:80])


def test_no_kirocrew_on_path_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [FileNotFoundError("kirocrew")])
    verdict, evidence = mod._evaluate(_spec())
    assert verdict == "error"
    assert "not found on PATH" in evidence


def test_a_hung_check_is_an_error(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [subprocess.TimeoutExpired(ARGV, mod.TIMEOUT_SECS)])
    verdict, evidence = mod._evaluate(_spec())
    assert verdict == "error"
    assert "timed out" in evidence


# --------------------------------------------------------------------------- #
# The whole script, over stdin
# --------------------------------------------------------------------------- #


def test_a_bad_edition_check_spec_is_an_error_end_to_end(tmp_path):
    """Through the real script: a malformed spec comes back ``error`` beside a
    sibling that still evaluates."""
    items = [
        {"id": "bad", "accept": {"kind": "edition_check", "check": "Bad", "ref": REF}},
        {"id": "human", "accept": {"kind": "human_approval"}},
    ]
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"items": items}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    out = {row["id"]: row["verdict"] for row in json.loads(proc.stdout)["results"]}
    assert out == {"bad": "error", "human": "pending"}
