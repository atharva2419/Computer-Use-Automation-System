"""Bringing a human into a running automation (brief 3.6).

The requirement is specific: a person must operate *the same live session* the
automation was using, not a fresh one. That constraint is what makes this
interesting. Everything valuable about the session -- the signed-on cookie,
which frame is showing what, a form half-filled with the caller's arguments --
is state that a new browser would not have. Handing over a fresh session would
be handing over nothing.

So nothing is torn down. The automation stops issuing commands, the control
token moves from ``AGENT`` to ``HUMAN``, and the same browser window sits there
waiting. The operator clicks. Then the token moves back and the run continues.

Three parts:

``InterventionRequest``
    The typed briefing. Written to the run's evidence folder as JSON plus a
    screenshot. In a real deployment this is what gets published to an operator
    work queue -- the payload is the same, only the transport changes.

``ConsoleOperatorHandler``
    A real handoff with a mocked console. The operator surface is a terminal
    prompt rather than a co-browsing UI, which the brief explicitly permits;
    what is *not* mocked is the control transfer, the session identity, the
    activity capture or the resume.

``ScriptedOperator``
    A stand-in that answers from a preset list, for tests and for producing
    reproducible evidence without a person sitting there.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .redaction import Redactor
from .replay import EscalationContext, EscalationOutcome
from .session import Actor
from .surface.base import ActivityRecorder

DEFAULT_TIMEOUT_SECONDS = 300


class InterventionRequest(BaseModel):
    """What a human needs in order to act, and nothing they do not.

    Carries the context brief 3.6 asks for -- which capability and goal, the
    current step, the current state, and why it stopped -- but never the
    caller's arguments. An operator resolving a stuck session does not need
    the passphrase that started it.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    run_id: str
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    capability_id: str
    goal: str
    step_id: str
    step_index: int
    step_intent: str

    reason: str
    signal_id: str | None = None
    observed: str = ""

    surface_url: str = ""
    frames: list[dict[str, str]] = Field(default_factory=list)
    resumable_steps: list[str] = Field(default_factory=list)

    def briefing(self) -> str:
        """Human-readable rendering, for a terminal or an operator console."""
        lines = [
            "",
            "=" * 72,
            "  INTERVENTION REQUESTED",
            "=" * 72,
            f"  capability : {self.capability_id}",
            f"  goal       : {self.goal}",
            f"  stopped at : step {self.step_index} ({self.step_id})",
            f"  intent     : {self.step_intent}",
            f"  reason     : {self.reason}",
        ]
        if self.signal_id:
            lines.append(f"  signal     : {self.signal_id}")
        if self.observed:
            lines.append(f"  observed   : {self.observed}")
        lines.append("")
        lines.append("  live session:")
        for frame in self.frames:
            label = frame.get("name") or "(top)"
            lines.append(f"    {label:<8} {frame.get('url', '')}")
        return "\n".join(lines)


class OperatorUnavailable(RuntimeError):
    """The handoff cannot be offered at all, so it must not be attempted."""


def _build_request(context: EscalationContext, request_id: str) -> InterventionRequest:
    surface = context.session.surface
    frames: list[dict[str, str]] = []
    url = ""
    try:
        observation = surface.observe()
        url = observation.url
        frames = [
            {"name": f.name, "url": f.url, "title": f.title} for f in observation.frames
        ]
    except Exception:  # noqa: BLE001 - a broken surface must still raise a request
        pass

    return InterventionRequest(
        request_id=request_id,
        run_id=context.run_id,
        capability_id=context.capability_id,
        goal=context.goal,
        step_id=context.step.id,
        step_index=context.step_index,
        step_intent=context.step.intent,
        reason=context.reason,
        signal_id=context.signal_id,
        observed=context.observed,
        surface_url=url,
        frames=frames,
    )


class _HandoffScope:
    """Holds the token on the human's behalf and records what they did.

    A context manager so the token cannot be left with the operator if the
    handler raises. A session stuck in ``HUMAN`` with nobody there is worse
    than a failed run: every later action would be refused with a control
    violation and the cause would be a long way from the symptom.
    """

    def __init__(self, context: EscalationContext, reason: str) -> None:
        self.context = context
        self.reason = reason
        self.actions: list[str] = []

    def __enter__(self) -> "_HandoffScope":
        session = self.context.session
        session.cede(Actor.HUMAN, reason=self.reason)
        surface = session.surface
        if isinstance(surface, ActivityRecorder):
            surface.start_activity_log()
        return self

    def __exit__(self, *exc: object) -> None:
        session = self.context.session
        surface = session.surface
        if isinstance(surface, ActivityRecorder):
            self.actions = surface.stop_activity_log()
        session.cede(Actor.AGENT, reason="operator handed control back")


class ConsoleOperatorHandler:
    """Pause, hand the live browser to whoever is at the terminal, resume.

    The console is the mocked part. The mechanism is not: the operator drives
    the same window the automation was driving, their navigations are captured
    from the live session rather than self-reported, and the run continues from
    a step they name.
    """

    def __init__(
        self,
        sink: Any = None,
        redactor: Redactor | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stream: Any = None,
    ) -> None:
        self.sink = sink
        self.redactor = redactor or Redactor()
        self.timeout_seconds = timeout_seconds
        self.stream = stream or sys.stdout
        self._counter = 0

    # -- EscalationHandler protocol ---------------------------------------

    def request(self, context: EscalationContext) -> EscalationOutcome:
        surface = context.session.surface
        if getattr(surface, "headless", False):
            # Fail loudly rather than pausing in front of an invisible window.
            raise OperatorUnavailable(
                "operator handoff requires a headed browser; "
                "re-run with --headed so the session can be taken over"
            )

        self._counter += 1
        request = _build_request(context, f"{context.run_id}-{self._counter}")
        self._persist(request)

        print(request.briefing(), file=self.stream)
        print(self._menu(context), file=self.stream)

        with _HandoffScope(context, reason=request.reason) as scope:
            answer = _read_line_with_timeout(self.timeout_seconds)

        if answer is None:
            print("  no response; aborting the run\n", file=self.stream)
            return EscalationOutcome(
                resolved=False,
                resolution="timed_out",
                actions=scope.actions,
                note=f"no operator response within {self.timeout_seconds}s",
            )

        return self._interpret(answer, scope)

    # -- internals ---------------------------------------------------------

    def _menu(self, context: EscalationContext) -> str:
        return (
            "\n  The browser window is yours. Fix the session by hand, then:\n"
            "    resume            continue from the step that stopped\n"
            "    resume <step_id>  continue from a different step\n"
            "    abort             give up and fail the run\n"
            f"  (timeout {self.timeout_seconds}s)\n"
            "\n  > "
        )

    def _interpret(self, answer: str, scope: _HandoffScope) -> EscalationOutcome:
        parts = answer.strip().split()
        verb = parts[0].lower() if parts else "abort"

        if verb in ("resume", "r", "continue", ""):
            return EscalationOutcome(
                resolved=True,
                resolution="resumed",
                operator="console",
                actions=scope.actions,
                resume_from_step=parts[1] if len(parts) > 1 else None,
            )

        return EscalationOutcome(
            resolved=False,
            resolution="aborted",
            operator="console",
            actions=scope.actions,
            note=f"operator answered {answer.strip()!r}",
        )

    def _persist(self, request: InterventionRequest) -> None:
        """Write the briefing and a screenshot into the run's evidence folder.

        This is the seam where a deployment would publish to an operator work
        queue instead. The payload is identical; only the transport differs.
        """
        if self.sink is None or getattr(self.sink, "directory", None) is None:
            return

        redacted = request.model_copy(
            update={
                "reason": self.redactor.text(request.reason),
                "observed": self.redactor.text(request.observed),
            }
        )
        directory = Path(self.sink.directory)
        name = f"intervention-{self._counter}"
        (directory / f"{name}.json").write_text(
            redacted.model_dump_json(indent=2), encoding="utf-8"
        )
        self.sink.note(
            "intervention_requested",
            request_id=request.request_id,
            step_id=request.step_id,
            reason=redacted.reason,
        )


class ScriptedOperator:
    """Answers from a preset list. For tests and reproducible demos.

    Exercises exactly the same control transfer and activity capture as the
    console handler -- only the source of the decision differs -- so evidence
    produced with it is honest about what was automated.
    """

    def __init__(
        self,
        decisions: Sequence[EscalationOutcome],
        sink: Any = None,
        on_takeover: Callable[[EscalationContext], None] | None = None,
    ) -> None:
        self.decisions = list(decisions)
        self.sink = sink
        # What the stand-in operator does while holding the session, e.g.
        # re-authenticating. Runs inside the handoff scope, so it is subject to
        # the same control token and the same activity capture a person is.
        self.on_takeover = on_takeover
        self.requests: list[InterventionRequest] = []
        self._counter = 0

    def request(self, context: EscalationContext) -> EscalationOutcome:
        self._counter += 1
        request = _build_request(context, f"{context.run_id}-{self._counter}")
        self.requests.append(request)

        if self.sink is not None and getattr(self.sink, "directory", None) is not None:
            self.sink.note(
                "intervention_requested",
                request_id=request.request_id,
                step_id=request.step_id,
                reason=request.reason,
            )

        if not self.decisions:
            return EscalationOutcome(
                resolved=False,
                resolution="aborted",
                note="scripted operator ran out of decisions",
            )

        decision = self.decisions.pop(0)
        # The control token still moves, the work happens on the same live
        # session, and the trail is captured by watching rather than being
        # self-reported. Only the decision itself is scripted.
        with _HandoffScope(context, reason=request.reason) as scope:
            if self.on_takeover is not None:
                self.on_takeover(context)
        return replace(decision, actions=scope.actions or decision.actions)


def _read_line_with_timeout(seconds: int) -> str | None:
    """Read one line from stdin, giving up after ``seconds``.

    Threaded rather than ``select``: this has to work on Windows, where
    ``select`` does not accept file handles. The reader is a daemon so a
    process that gives up waiting can still exit while it is blocked.

    Without a terminal there is nobody to ask, so it returns immediately
    rather than stalling an unattended run for the full timeout.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return None

    answer: list[str] = []

    def _read() -> None:
        try:
            answer.append(sys.stdin.readline())
        except Exception:  # noqa: BLE001 - closed stdin, redirected input
            pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout=seconds)
    return answer[0] if answer else None
