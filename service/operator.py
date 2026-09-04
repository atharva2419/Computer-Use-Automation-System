"""Escalation through the API and dashboard (brief 3.5).

The core's requirement is that a stuck or risky run stops, hands the live
session to a person, and resumes where they say. That has to survive being put
behind an HTTP API -- a wrapper that quietly auto-approves whatever the policy
stopped would be worse than no guardrail, because it would look like one.

This is the second implementation of the ``EscalationHandler`` seam. The first
prints to a terminal and reads a line; this one publishes the request onto the
run record and blocks until a decision arrives through
``POST /runs/{id}/decision``. Neither the replay engine nor the policy changed
to accommodate it, which is the point: the escalation contract was already the
right shape.

The browser stays open and untouched throughout. An operator working the
dashboard with a headed run is driving the very session the automation paused,
not a copy of it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cua.escalation import InterventionRequest, _build_request
from cua.redaction import Redactor
from cua.replay import EscalationContext, EscalationOutcome
from cua.session import Actor
from cua.surface.base import ActivityRecorder

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .runner import Run

# How long a paused run waits for a person before giving up. Long enough for
# somebody to notice and act, short enough that a forgotten run does not hold
# the single worker forever and block every invocation behind it.
DEFAULT_WAIT_SECONDS = 900

# How often the session is sampled while a human holds it. Sampling is the only
# way to observe what they did -- nothing else re-enters the browser while the
# automation is paused.
SAMPLE_SECONDS = 0.4


class DashboardOperator:
    """Publishes an intervention request and waits for a web decision."""

    def __init__(
        self,
        run: "Run",
        sink: Any = None,
        redactor: Redactor | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> None:
        self.run = run
        self.sink = sink
        self.redactor = redactor or Redactor()
        self.wait_seconds = wait_seconds

        self._answered = threading.Event()
        self._approved = False
        self._resume_from: str | None = None
        self._counter = 0

    # -- called from the API thread ---------------------------------------

    def decide(self, approve: bool, resume_from: str | None = None) -> None:
        """Record the operator's answer and release the paused run."""
        self._approved = approve
        self._resume_from = resume_from
        self._answered.set()

    # -- EscalationHandler protocol ---------------------------------------

    def request(self, context: EscalationContext) -> EscalationOutcome:
        self._counter += 1
        self._answered.clear()

        req = _build_request(context, f"{context.run_id}-{self._counter}")
        self._publish(req, context)

        session = context.session
        surface = session.surface

        # The token moves before the wait, not after: from here until the
        # operator answers, the automation is refused if it tries to act.
        session.cede(Actor.HUMAN, reason=req.reason)
        if isinstance(surface, ActivityRecorder):
            surface.start_activity_log()

        answered = self._wait_while_sampling(surface)

        actions: list[str] = []
        if isinstance(surface, ActivityRecorder):
            actions = surface.stop_activity_log()
        session.cede(Actor.AGENT, reason="operator handed control back")

        if not answered:
            return self._record(
                req,
                EscalationOutcome(
                    resolved=False,
                    resolution="timed_out",
                    actions=actions,
                    note=f"no operator decision within {self.wait_seconds}s",
                ),
            )

        if not self._approved:
            return self._record(
                req,
                EscalationOutcome(
                    resolved=False,
                    resolution="aborted",
                    operator="dashboard",
                    actions=actions,
                    note="operator declined the request",
                ),
            )

        return self._record(
            req,
            EscalationOutcome(
                resolved=True,
                resolution="resumed",
                operator="dashboard",
                actions=actions,
                resume_from_step=self._resume_from,
            ),
        )

    # -- internals ---------------------------------------------------------

    def _wait_while_sampling(self, surface: Any) -> bool:
        """Block until answered, sampling the session as we go.

        A plain ``Event.wait`` would be simpler and would record nothing: the
        browser is only observed when something re-enters it, and while the
        automation is paused nothing does. Waking regularly to sample is what
        makes "what the human did" an observation rather than a claim.
        """
        deadline = self.wait_seconds
        waited = 0.0
        while waited < deadline:
            if self._answered.wait(timeout=SAMPLE_SECONDS):
                return True
            waited += SAMPLE_SECONDS
            if isinstance(surface, ActivityRecorder):
                try:
                    surface.poll_activity()
                except Exception:  # noqa: BLE001 - recording must not break a handoff
                    pass
        return self._answered.is_set()

    def _publish(self, req: InterventionRequest, context: EscalationContext) -> None:
        """Put the request where the dashboard and the caller can see it."""
        self.run.status = "awaiting_operator"
        self.run.intervention = {
            "request_id": req.request_id,
            "step_id": req.step_id,
            "step_index": req.step_index,
            "intent": req.step_intent,
            "reason": self.redactor.text(req.reason),
            "signal_id": req.signal_id,
            "observed": self.redactor.text(req.observed),
            "surface_url": req.surface_url,
            "resumable_steps": req.resumable_steps,
            "raised_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "resolution": "pending",
        }
        if self.sink is not None:
            self.sink.note(
                "intervention_requested",
                request_id=req.request_id,
                step_id=req.step_id,
                reason=self.redactor.text(req.reason),
            )

    def _record(
        self, req: InterventionRequest, outcome: EscalationOutcome
    ) -> EscalationOutcome:
        if self.run.intervention is not None:
            self.run.intervention.update(
                {
                    "resolution": outcome.resolution,
                    "operator": outcome.operator,
                    "human_actions": outcome.actions,
                    "resume_from_step": outcome.resume_from_step,
                    "note": outcome.note,
                }
            )
        if self.run.status == "awaiting_operator":
            self.run.status = "running" if outcome.resolved else self.run.status
        if self.sink is not None:
            self.sink.note(
                "intervention_resolved",
                request_id=req.request_id,
                resolution=outcome.resolution,
                actions=[self.redactor.text(a) for a in outcome.actions],
            )
        return outcome
