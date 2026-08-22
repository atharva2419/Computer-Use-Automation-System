"""What a replay returns to its caller.

Three terminal statuses, and only three
---------------------------------------
``success`` | ``business_outcome`` | ``failed``

"Recoverable" is deliberately *not* a status. A recoverable condition is
something the run handles and continues through -- an interstitial dismissed,
a slow load waited out -- so by the time the run terminates it has still
either achieved the goal, learned a business answer, or failed. Returning
"recoverable" would force every caller to ask "recovered from what, and did
the flow actually finish?", which is the question the result exists to answer.

Recoveries and escalations are therefore *envelope records*: they describe what
happened along the way without muddying what happened in the end. That also
keeps the taxonomy honest -- a recovery that silently masks a broken flow
shows up as a growing recovery count on a still-successful capability, which
is a signal worth alerting on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


FailureCategory = Literal[
    "target_not_found",
    "checkpoint_failed",
    "timeout",
    "app_error",
    "session_lost",
    "policy_denied",
    "invalid_input",
    "escalation_unresolved",
    "internal_error",
]
"""Why a run stopped. Distinguishes *our* faults from the *app's* faults.

target_not_found / checkpoint_failed
    The surface did not match the recorded flow. Suggests drift; the failure
    detail names the strategies tried.
app_error / session_lost
    The application misbehaved or logged us out. Not a defect in the artifact.
policy_denied / invalid_input
    The request was rejected before touching the UI. Caller-side problem.
escalation_unresolved
    A human was asked to take over and did not resolve it.
"""


class RecoveryRecord(_Strict):
    """A recoverable condition that was detected and cleared mid-run."""

    step_id: str
    signal_id: str
    action: str = Field(description="Recovery kind applied, e.g. dismiss_and_retry.")
    attempt: int
    succeeded: bool
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EscalationRecord(_Strict):
    """A handoff to a human operator during this run."""

    request_id: str
    step_id: str
    reason: str
    opened_at: datetime
    resolved_at: datetime | None = None
    resolution: Literal["resumed", "aborted", "timed_out", "pending"] = "pending"
    operator: str | None = None
    human_actions: list[str] = Field(
        default_factory=list,
        description="What the human did while holding control, captured from "
        "the live session.",
    )


class StepRecord(_Strict):
    """Per-step execution trace. The debuggable core of a failure report."""

    step_id: str
    index: int
    intent: str
    action_kind: str
    status: Literal["ok", "recovered", "failed", "skipped"]
    resolved_by: str | None = Field(
        default=None,
        description="Which target strategy resolved, e.g. 'role_name' or "
        "'label_cell'. Falling through to a lower-ranked strategy is the "
        "system's cheapest drift signal.",
    )
    strategy_rank: int | None = None
    duration_ms: int = 0
    checkpoint_ok: bool | None = None
    note: str = ""


class FailureDetail(_Strict):
    """Everything needed to debug without re-running.

    The brief asks for "what step, what was expected, what was observed" --
    these three fields are mandatory rather than free-text, so a failure report
    cannot degrade into a stack trace.
    """

    category: FailureCategory
    step_id: str | None
    step_index: int | None
    intent: str = ""
    expected: str
    observed: str
    signal_id: str | None = None
    strategies_tried: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(
        default_factory=list, description="Paths to screenshot / DOM / trace files."
    )


class ControlTransferRecord(_Strict):
    """One movement of the control token during the run (brief 3.6).

    The escalation records imply that control moved; this states it. Together
    they answer "who was in control, when, and why" without inference -- which
    is the question an audit of a part-manual run actually asks, and the one
    thing a reviewer cannot reconstruct from step records alone.
    """

    at: datetime
    from_actor: str
    to_actor: str
    reason: str


class _Envelope(_Strict):
    run_id: str
    capability_id: str
    capability_version: int
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    steps: list[StepRecord] = Field(default_factory=list)
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    escalations: list[EscalationRecord] = Field(default_factory=list)
    control_ledger: list[ControlTransferRecord] = Field(default_factory=list)
    human_touched: bool = Field(
        default=False,
        description="Whether a person ever held this session. A capability "
        "that needed manual help is not the same as one that ran clean, even "
        "when both end in success, and confidence scoring should say so.",
    )
    evidence_dir: str | None = None


class ReplaySuccess(_Envelope):
    """The goal was achieved and the success checkpoint verified."""

    status: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)


class ReplayBusinessOutcome(_Envelope):
    """The flow ran correctly and produced a legitimate negative answer.

    This is *not* a failure. The automation worked; the answer is that the
    member does not exist, or the operator is not entitled to the record.
    Callers branch on ``code``.
    """

    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    signal_id: str
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Any outputs extracted before the outcome was detected.",
    )


class ReplayFailure(_Envelope):
    """The flow could not be completed and no business answer was obtained."""

    status: Literal["failed"] = "failed"
    error: FailureDetail


ReplayResult = Annotated[
    Union[ReplaySuccess, ReplayBusinessOutcome, ReplayFailure],
    Field(discriminator="status"),
]
