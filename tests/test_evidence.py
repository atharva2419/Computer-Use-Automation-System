"""Evidence tests (brief 3.5).

The claim being tested is not "a file appeared". It is that a run leaves
behind enough to reconstruct what happened and why, *and* that it does not
leave behind regulated data while doing so. Those two pull against each other,
which is why both directions are asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.evidence import FileEvidenceSink
from cua.redaction import Redactor
from cua.schema.capability import (
    AppBinding,
    Capability,
    Checkpoint,
    Extraction,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    Step,
)
from cua.schema.common import FrameRef, LiteralValue, TextPresent
from cua.schema.result import FailureDetail, ReplaySuccess, StepRecord
from cua.schema.targets import RoleNameStrategy, Target

TOP = FrameRef(kind="top")


def _capability() -> Capability:
    return Capability(
        id="t.read_balance",
        name="t",
        description="d",
        app=AppBinding(product="meridian-core", entry_url="http://127.0.0.1:5057/login"),
        inputs=[
            ParamSpec(name="member_id", pattern=r"^\d+$"),
            ParamSpec(name="passphrase", secret=True, sensitivity="restricted"),
        ],
        outputs=[
            OutputSpec(
                name="balance",
                type="money",
                sensitivity="restricted",
                extract=Extraction(
                    target=Target(
                        described_as="balance cell",
                        frame=TOP,
                        strategies=[RoleNameStrategy(role="cell", name="balance")],
                    )
                ),
            ),
            OutputSpec(
                name="branch",
                sensitivity="internal",
                extract=Extraction(
                    target=Target(
                        described_as="branch cell",
                        frame=TOP,
                        strategies=[RoleNameStrategy(role="cell", name="branch")],
                    )
                ),
            ),
        ],
        steps=[
            Step(
                id="s1",
                intent="open the console",
                action=NavigateAction(
                    url=LiteralValue(value="http://127.0.0.1:5057/login"), frame=TOP
                ),
            )
        ],
        success=Checkpoint(description="done", assertion=TextPresent(frame=TOP, text="OK")),
    )


@pytest.fixture
def sink(tmp_path: Path) -> FileEvidenceSink:
    s = FileEvidenceSink(root=tmp_path, label="unit")
    s.open(_capability())
    return s


def _lines(sink: FileEvidenceSink) -> list[dict]:
    assert sink.directory is not None
    return [
        json.loads(line)
        for line in (sink.directory / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _record(step_id: str = "s1", status: str = "ok") -> StepRecord:
    return StepRecord(
        step_id=step_id,
        index=0,
        intent="open the console",
        action_kind="navigate",
        status=status,  # type: ignore[arg-type]
        resolved_by="role_name",
        strategy_rank=0,
        duration_ms=12,
        checkpoint_ok=True,
    )


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------


def test_run_header_records_what_was_run(sink: FileEvidenceSink) -> None:
    header = _lines(sink)[0]
    assert header["kind"] == "run_started"
    assert header["capability_id"] == "t.read_balance"
    assert header["capability_version"] == 1
    assert header["schema_version"] == "1.0.0"
    assert header["approval"] == "draft"


def test_header_names_the_inputs_but_never_their_values(sink: FileEvidenceSink) -> None:
    """Which parameters exist is useful; what was passed is not ours to keep."""
    header = _lines(sink)[0]
    names = {i["name"]: i for i in header["declared_inputs"]}
    assert names["passphrase"]["secret"] is True
    assert "value" not in names["passphrase"]
    assert "demo-pass" not in json.dumps(header)


def test_steps_are_appended_in_order_with_their_resolution(
    sink: FileEvidenceSink,
) -> None:
    sink.on_step("run1", _record("s1"))
    sink.on_step("run1", _record("s2"))

    steps = [line for line in _lines(sink) if line["kind"] == "step"]
    assert [s["step_id"] for s in steps] == ["s1", "s2"]
    # The drift signal has to survive into the evidence, or it cannot be
    # trended across runs.
    assert steps[0]["resolved_by"] == "role_name"
    assert steps[0]["strategy_rank"] == 0


def test_freeform_notes_share_the_same_ordered_trail(sink: FileEvidenceSink) -> None:
    """Model decisions and human actions interleave with steps, not beside them."""
    sink.on_step("run1", _record("s1"))
    sink.note("model_decision", reasoning="clicked Search because the form was ready")
    sink.on_step("run1", _record("s2"))

    kinds = [line["kind"] for line in _lines(sink)]
    assert kinds == ["run_started", "step", "model_decision", "step"]


# ---------------------------------------------------------------------------
# Failure artefacts
# ---------------------------------------------------------------------------


def test_failure_writes_a_screenshot_and_an_accessibility_snapshot(
    sink: FileEvidenceSink,
) -> None:
    detail = FailureDetail(
        category="checkpoint_failed",
        step_id="s1",
        step_index=0,
        expected="the member record",
        observed="the search form",
    )
    written = sink.on_failure("run1", detail, b"\x89PNG-not-really", "- table:\n  - row")

    assert sink.directory is not None
    assert (sink.directory / "failure.png").read_bytes() == b"\x89PNG-not-really"
    assert "table" in (sink.directory / "observation.txt").read_text(encoding="utf-8")
    assert len(written) == 2

    entry = [line for line in _lines(sink) if line["kind"] == "failure"][0]
    assert entry["expected"] == "the member record"
    assert entry["observed"] == "the search form"
    assert entry["artefacts"] == written


def test_failure_with_no_visual_capture_still_records_the_reason(
    sink: FileEvidenceSink,
) -> None:
    """A rejected argument never reaches the app, so there is nothing to shoot."""
    detail = FailureDetail(
        category="invalid_input",
        step_id=None,
        step_index=None,
        expected="a valid member id",
        observed="'oops' does not match the pattern",
    )
    written = sink.on_failure("run1", detail, b"", "")

    assert written == []
    assert sink.directory is not None
    assert not (sink.directory / "failure.png").exists()
    entry = [line for line in _lines(sink) if line["kind"] == "failure"][0]
    assert entry["category"] == "invalid_input"


# ---------------------------------------------------------------------------
# The result, and what it is allowed to persist
# ---------------------------------------------------------------------------


def _success() -> ReplaySuccess:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return ReplaySuccess(
        run_id="run1",
        capability_id="t.read_balance",
        capability_version=1,
        started_at=now,
        finished_at=now,
        duration_ms=10,
        outputs={"balance": 4210.55, "branch": "Riverside"},
    )


def test_restricted_outputs_are_described_not_persisted(
    sink: FileEvidenceSink,
) -> None:
    """The caller gets the figure; the evidence folder gets its shape.

    Recording the type is enough to confirm the extraction and its transform
    worked -- that a money output really came back as a number -- without
    writing a member's balance to a file read by people who did not make the
    request.
    """
    sink.on_result("run1", _success(), _capability())

    assert sink.directory is not None
    written = json.loads((sink.directory / "result.json").read_text(encoding="utf-8"))

    assert written["outputs"]["balance"] == "[REDACTED:restricted float]"
    assert "4210.55" not in json.dumps(written)
    # But a reviewer can still see the contract was fulfilled.
    assert written["output_names"] == ["balance", "branch"]
    assert written["status"] == "success"


def test_non_restricted_outputs_survive(sink: FileEvidenceSink) -> None:
    """Over-redaction is its own failure mode."""
    sink.on_result("run1", _success(), _capability())
    assert sink.directory is not None
    written = json.loads((sink.directory / "result.json").read_text(encoding="utf-8"))
    assert written["outputs"]["branch"] == "Riverside"


def test_result_closes_the_trail(sink: FileEvidenceSink) -> None:
    sink.on_result("run1", _success(), _capability())
    last = _lines(sink)[-1]
    assert last["kind"] == "run_finished"
    assert last["status"] == "success"


# ---------------------------------------------------------------------------
# Redaction happens upstream
# ---------------------------------------------------------------------------


def test_the_sink_writes_what_it_is_given(sink: FileEvidenceSink) -> None:
    """Redaction is the engine's job, applied once at the boundary.

    Asserted explicitly because the alternative -- every sink redacting for
    itself -- is the design where the second sink someone adds forgets to.
    """
    detail = FailureDetail(
        category="app_error",
        step_id="s1",
        step_index=0,
        expected="no fault",
        observed=Redactor.from_spec(
            [{"name": "account_number", "regex": r"\b(?:SAV|CHK)-\d{3,}\b"}]
        ).text("row SAV-0001 failed"),
    )
    sink.on_failure("run1", detail, b"", "")

    entry = [line for line in _lines(sink) if line["kind"] == "failure"][0]
    assert entry["observed"] == "row [REDACTED:account_number] failed"
