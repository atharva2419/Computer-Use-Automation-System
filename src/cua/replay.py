"""Deterministic replay: the production execution path (brief 3.3).

No model is consulted here. Every decision the engine makes is read from the
artifact -- which control to address, what proves the step worked, which
runtime conditions are known and what each one means. That is the point of the
split: discovery is expensive, slow and probabilistic, and it happens once;
this path is cheap, fast and repeatable, and it happens thousands of times.

The one genuinely interesting design decision is how a step decides it is done.

The obvious implementation checks the checkpoint, and if that fails, looks for
known error signals. It is wrong, and wrong in a way that only shows up in
production: immediately after an action the surface may still be loading, so a
"NO RECORD FOUND" page that takes 200ms to render is missed, the checkpoint
times out, and a perfectly ordinary business answer is reported as a failure.

So the engine *races* them. It polls the success checkpoint and every declared
signal together, and takes whichever the application actually reaches. A slow
"not found" is still a business outcome; a fast one is too. Where both match,
the signal wins, because a signal describes a specific known state and a
checkpoint only asks the generic question.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from . import assertions, extract, params
from .guardrails import GateDecision, GateRequest, PolicyGate
from .redaction import Redactor
from .schema.capability import (
    Capability,
    Checkpoint,
    ClickAction,
    DismissAndRetry,
    FillAction,
    NavigateAction,
    PressAction,
    ReloadAndRetry,
    SelectAction,
    SignalRule,
    Step,
    WaitAndRetry,
    WaitForAction,
)
from .schema.result import (
    EscalationRecord,
    FailureCategory,
    FailureDetail,
    RecoveryRecord,
    ReplayBusinessOutcome,
    ReplayFailure,
    ReplayResult,
    ReplaySuccess,
    StepRecord,
)
from .session import Actor, Session
from .surface.base import (
    Resolution,
    SurfaceError,
    SurfaceTimeout,
    SurfaceUnavailable,
    TargetNotResolved,
)

POLL_MS = 200


# ---------------------------------------------------------------------------
# Seams: policy, escalation, evidence
# ---------------------------------------------------------------------------


class Gate(Protocol):
    """Consulted before every action. Implemented in ``cua.guardrails``."""

    def check(self, request: GateRequest) -> GateDecision: ...


@dataclass(slots=True)
class EscalationContext:
    """What a human needs to act on an intervention request (brief 3.6)."""

    run_id: str
    capability_id: str
    goal: str
    step: Step
    step_index: int
    reason: str
    signal_id: str | None
    observed: str
    session: Session


@dataclass(slots=True)
class EscalationOutcome:
    resolved: bool
    resolution: str = "aborted"
    operator: str | None = None
    actions: list[str] = field(default_factory=list)
    note: str = ""
    resume_from_step: str | None = None
    """Step to resume at, when the failed step is no longer the right one.

    Retrying the step that failed is the obvious behaviour and it is often
    wrong. Recovering a lapsed session puts the console back on its home
    screen, so the control the failed step was reaching for does not exist any
    more; resuming there fails again immediately. The operator -- who can see
    the live session -- is the one who knows which step the flow is now at, so
    they say, and the engine winds back to it.
    """


class EscalationHandler(Protocol):
    def request(self, context: EscalationContext) -> EscalationOutcome: ...


class NoOperatorAvailable:
    """Default handler: records the request and declines it.

    Replaced in step E by a handler that actually transfers control. Failing
    closed matters -- an unattended replay that cannot reach a human must stop
    and say so, not press on past a condition it already decided it could not
    handle.
    """

    def request(self, context: EscalationContext) -> EscalationOutcome:
        return EscalationOutcome(
            resolved=False,
            resolution="aborted",
            note="no operator handler configured for this run",
        )


class EvidenceSink(Protocol):
    """Where the run trail goes. Implemented for files in ``cua.evidence``.

    Everything handed to a sink has already been redacted by the engine, so a
    sink only writes what it is given. Keeping the policy on this side means a
    future sink -- a database, a log shipper -- cannot forget to apply it.
    """

    def on_step(self, run_id: str, record: StepRecord) -> None: ...

    def on_failure(
        self,
        run_id: str,
        detail: FailureDetail,
        screenshot: bytes,
        observation: str = "",
    ) -> list[str]: ...

    def on_result(
        self, run_id: str, result: Any, capability: Capability
    ) -> None: ...


class NullSink:
    def on_step(self, run_id: str, record: StepRecord) -> None:
        return None

    def on_failure(
        self,
        run_id: str,
        detail: FailureDetail,
        screenshot: bytes,
        observation: str = "",
    ) -> list[str]:
        return []

    def on_result(self, run_id: str, result: Any, capability: Capability) -> None:
        return None


# ---------------------------------------------------------------------------
# Internal control flow
# ---------------------------------------------------------------------------


class _Terminate(Exception):
    """Unwind the run with a finished result."""

    def __init__(self, result_kind: str, payload: Any) -> None:
        self.result_kind = result_kind
        self.payload = payload
        super().__init__(result_kind)


class _ResumeAt(Exception):
    """Unwind to the run loop and continue from a different step."""

    def __init__(self, index: int) -> None:
        self.index = index
        super().__init__(index)


@dataclass(slots=True)
class _Outcome:
    """How a step's post-action wait ended."""

    kind: str  # "checkpoint" | "signal" | "timeout"
    signal: SignalRule | None = None
    expected: str = ""
    observed: str = ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ReplayEngine:
    def __init__(
        self,
        session: Session,
        gate: Gate | None = None,
        escalation: EscalationHandler | None = None,
        sink: EvidenceSink | None = None,
        redactor: Redactor | None = None,
        max_duration_ms: int = 120_000,
    ) -> None:
        self.session = session
        # Defaults to the shipped policy, and raises if it is missing rather
        # than silently running unguarded. An unguarded run should require
        # someone to say so at the call site.
        self.gate = gate if gate is not None else PolicyGate.from_file()
        self.escalation = escalation or NoOperatorAvailable()
        self.sink = sink or NullSink()
        # Redaction patterns live in the same policy file as the allowlist, so
        # take them from the gate by default. Otherwise every caller would have
        # to remember to wire redaction up separately, and the one that forgets
        # is the one that writes a member's account number into a log.
        if redactor is not None:
            self.redactor = redactor
        else:
            policy = getattr(self.gate, "policy", None)
            self.redactor = policy.redactor() if policy is not None else Redactor()
        self.max_duration_ms = max_duration_ms

        self.run_id = uuid.uuid4().hex[:12]
        self._steps: list[StepRecord] = []
        self._recoveries: list[RecoveryRecord] = []
        self._escalations: list[EscalationRecord] = []
        self._outputs: dict[str, Any] = {}
        self._started = datetime.now(timezone.utc)
        self._deadline = 0.0

    # -- public ------------------------------------------------------------

    def run(self, capability: Capability, supplied: dict[str, Any]) -> ReplayResult:
        """Execute a capability and record the run.

        A thin wrapper around ``_execute`` purely so that every exit -- success,
        business outcome, failure, or an exception nobody predicted -- passes
        through one place that hands the result to the evidence sink. Recording
        at each return site would eventually miss one, and the run that goes
        unrecorded is invariably the interesting one.
        """
        result = self._execute(capability, supplied)
        self.sink.on_result(self.run_id, result, capability)
        return result

    def _execute(self, capability: Capability, supplied: dict[str, Any]) -> ReplayResult:
        self._started = datetime.now(timezone.utc)
        self._deadline = time.monotonic() + self.max_duration_ms / 1000

        try:
            bound = params.validate(capability, supplied)
        except params.ParamError as exc:
            # Before the surface is ever touched: honestly a caller-side fault.
            return self._fail(
                capability,
                FailureDetail(
                    category="invalid_input",
                    step_id=None,
                    step_index=None,
                    expected="arguments satisfying the declared inputs",
                    observed=str(exc),
                ),
            )

        # Teach the redactor the credential-like values this invocation was
        # given, so they can never surface in a log, an evidence file or an
        # escalation record. They are held for the run only.
        self.redactor.learn_secrets(
            bound.get(name) for name in params.secret_names(capability)
        )

        self.session.claim(Actor.AGENT, reason=f"replay {capability.id}")

        try:
            index = 0
            while index < len(capability.steps):
                try:
                    self._run_step(capability, capability.steps[index], index, bound)
                except _ResumeAt as resume:
                    index = resume.index
                    continue
                index += 1
            return self._finish(capability)
        except _Terminate as stop:
            if stop.result_kind == "business_outcome":
                return stop.payload
            return stop.payload
        except Exception as exc:  # noqa: BLE001 - last resort, must not escape
            return self._fail(
                capability,
                FailureDetail(
                    category="internal_error",
                    step_id=None,
                    step_index=None,
                    expected="replay to complete",
                    observed=f"{type(exc).__name__}: {exc}",
                ),
            )

    # -- step execution ----------------------------------------------------

    def _run_step(
        self, capability: Capability, step: Step, index: int, bound: dict[str, str]
    ) -> None:
        started = time.monotonic()
        surface = self._surface()

        decision = self.gate.check(
            GateRequest(
                capability=capability,
                step=step,
                step_index=index,
                current_url=self._current_url(),
                target_url=self._target_url(step, bound),
            )
        )
        if not decision.allowed:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="policy_denied",
                    step_id=step.id,
                    step_index=index,
                    intent=step.intent,
                    expected=f"policy to permit a {decision.risk} {step.action.kind}",
                    observed=decision.reason or "denied by policy",
                ),
            )
        if decision.require_human:
            # Risky-but-permitted: the flow continues, but a person owns the
            # decision. Same mechanism as a stuck run, and the approval lands
            # in the run evidence.
            self._escalate(
                capability,
                step,
                index,
                reason=decision.reason or "policy requires human confirmation",
                signal_id=None,
                observed=f"awaiting operator approval for a {decision.risk} action",
            )

        attempts_by_signal: dict[str, int] = {}
        resolution: Resolution | None = None

        while True:
            self._check_deadline(capability, step, index)

            try:
                resolution = self._act(step, bound)
            except TargetNotResolved as exc:
                self._terminate_failure(
                    capability,
                    FailureDetail(
                        category="target_not_found",
                        step_id=step.id,
                        step_index=index,
                        intent=step.intent,
                        expected=f"a control matching {exc.target.described_as!r}",
                        observed="; ".join(
                            f"{a.kind}: {a.detail}" for a in exc.attempts
                        ),
                        strategies_tried=[a.kind for a in exc.attempts],
                    ),
                )
            except SurfaceTimeout as exc:
                self._terminate_failure(
                    capability,
                    FailureDetail(
                        category="timeout",
                        step_id=step.id,
                        step_index=index,
                        intent=step.intent,
                        expected=f"step {step.id!r} to complete",
                        observed=str(exc),
                    ),
                )
            except SurfaceUnavailable as exc:
                # The application is down or unreachable. Not our defect, and
                # not drift -- categorising it as an internal error would send
                # whoever debugs it looking at the artifact.
                self._terminate_failure(
                    capability,
                    FailureDetail(
                        category="app_error",
                        step_id=step.id,
                        step_index=index,
                        intent=step.intent,
                        expected="the application to be reachable",
                        observed=str(exc),
                    ),
                )
            except SurfaceError as exc:
                self._terminate_failure(
                    capability,
                    FailureDetail(
                        category="internal_error",
                        step_id=step.id,
                        step_index=index,
                        intent=step.intent,
                        expected=f"step {step.id!r} to complete",
                        observed=f"{type(exc).__name__}: {exc}",
                    ),
                )

            outcome = self._await_outcome(capability, step)

            if outcome.kind == "signal" and outcome.signal is not None:
                rule = outcome.signal
                if rule.classification == "business_outcome":
                    self._record_step(step, index, "ok", resolution, started, True,
                                      note=f"business outcome {rule.outcome_code}")
                    self._terminate_business(capability, rule, outcome)
                if rule.classification == "recoverable":
                    count = attempts_by_signal.get(rule.id, 0) + 1
                    attempts_by_signal[rule.id] = count
                    if self._recover(rule, step, count):
                        continue  # re-run the step
                    self._terminate_failure(
                        capability,
                        FailureDetail(
                            category="app_error",
                            step_id=step.id,
                            step_index=index,
                            intent=step.intent,
                            signal_id=rule.id,
                            expected=f"{rule.id} to clear after recovery",
                            observed=f"still present after {count} attempt(s)",
                        ),
                    )
                # hard failure
                self._handle_hard_failure(capability, step, index, rule, outcome)
                continue  # only reached when a human resolved it

            if outcome.kind == "timeout":
                self._record_step(step, index, "failed", resolution, started, False)
                self._terminate_failure(
                    capability,
                    FailureDetail(
                        category="checkpoint_failed",
                        step_id=step.id,
                        step_index=index,
                        intent=step.intent,
                        expected=outcome.expected,
                        observed=outcome.observed,
                    ),
                )

            break  # checkpoint held

        self._record_step(step, index, "ok", resolution, started, True)
        self._extract_after(capability, step, index)

    # -- the checkpoint / signal race --------------------------------------

    def _await_outcome(self, capability: Capability, step: Step) -> _Outcome:
        """Poll the step checkpoint and every declared signal together."""
        surface = self._surface()
        checkpoint: Checkpoint | None = step.checkpoint
        timeout_ms = checkpoint.timeout_ms if checkpoint else step.timeout_ms
        deadline = time.monotonic() + timeout_ms / 1000

        # Step-local signals first: a step may specialise a shared condition.
        rules = list(step.signals) + list(capability.signals)
        last = _Outcome(kind="timeout", expected="", observed="")

        while True:
            for rule in rules:
                result = assertions.evaluate(surface, rule.detect)
                if result.ok:
                    return _Outcome(
                        kind="signal",
                        signal=rule,
                        expected=rule.description,
                        observed=result.observed,
                    )

            if checkpoint is None:
                return _Outcome(kind="checkpoint")

            result = assertions.evaluate(surface, checkpoint.assertion)
            if result.ok:
                return _Outcome(kind="checkpoint")
            last = _Outcome(
                kind="timeout",
                expected=f"{checkpoint.description} ({result.expected})",
                observed=result.observed,
            )

            if time.monotonic() >= deadline:
                return last
            time.sleep(POLL_MS / 1000)

    # -- actions -----------------------------------------------------------

    def _act(self, step: Step, bound: dict[str, str]) -> Resolution | None:
        surface = self._surface()
        action = step.action

        if isinstance(action, NavigateAction):
            surface.navigate(params.resolve(action.url, bound), action.frame)
            return None
        if isinstance(action, ClickAction):
            return surface.click(action.target, timeout_ms=step.timeout_ms)
        if isinstance(action, FillAction):
            return surface.fill(
                action.target,
                params.resolve(action.value, bound),
                clear_first=action.clear_first,
                timeout_ms=step.timeout_ms,
            )
        if isinstance(action, SelectAction):
            return surface.select(
                action.target, params.resolve(action.value, bound), step.timeout_ms
            )
        if isinstance(action, PressAction):
            return surface.press(action.target, action.key, step.timeout_ms)
        if isinstance(action, WaitForAction):
            assertions.wait_until(surface, action.condition, step.timeout_ms)
            return None
        raise TypeError(f"unsupported action {type(action).__name__}")

    # -- recovery ----------------------------------------------------------

    def _recover(self, rule: SignalRule, step: Step, attempt: int) -> bool:
        recovery = rule.recovery
        assert recovery is not None  # guaranteed by SignalRule validation
        surface = self._surface()

        if attempt > getattr(recovery, "max_attempts", 1):
            self._recoveries.append(
                RecoveryRecord(
                    step_id=step.id,
                    signal_id=rule.id,
                    action=recovery.kind,
                    attempt=attempt,
                    succeeded=False,
                )
            )
            return False

        ok = True
        try:
            if isinstance(recovery, DismissAndRetry):
                surface.click(recovery.dismiss)
            elif isinstance(recovery, WaitAndRetry):
                time.sleep(recovery.wait_ms / 1000)
            elif isinstance(recovery, ReloadAndRetry):
                surface.navigate(surface.frame_url(recovery.frame), recovery.frame)
            surface.settle()
        except SurfaceError:
            ok = False

        self._recoveries.append(
            RecoveryRecord(
                step_id=step.id,
                signal_id=rule.id,
                action=recovery.kind,
                attempt=attempt,
                succeeded=ok,
            )
        )
        return ok

    # -- hard failure and escalation --------------------------------------

    def _handle_hard_failure(
        self,
        capability: Capability,
        step: Step,
        index: int,
        rule: SignalRule,
        outcome: _Outcome,
    ) -> None:
        if rule.escalate:
            self._escalate(
                capability,
                step,
                index,
                reason=rule.message or rule.description,
                signal_id=rule.id,
                observed=outcome.observed,
            )
            return  # resolved by a human; caller re-runs the step

        self._terminate_failure(
            capability,
            FailureDetail(
                category=_category_for(rule),
                step_id=step.id,
                step_index=index,
                intent=step.intent,
                signal_id=rule.id,
                expected=f"not to encounter {rule.id}",
                observed=rule.message or outcome.observed,
            ),
        )

    def _escalate(
        self,
        capability: Capability,
        step: Step,
        index: int,
        reason: str,
        signal_id: str | None,
        observed: str,
    ) -> None:
        request_id = uuid.uuid4().hex[:10]
        opened = datetime.now(timezone.utc)

        context = EscalationContext(
            run_id=self.run_id,
            capability_id=capability.id,
            goal=capability.description,
            step=step,
            step_index=index,
            reason=reason,
            signal_id=signal_id,
            observed=observed,
            session=self.session,
        )
        outcome = self.escalation.request(context)

        # The live handler was given the unredacted context deliberately: the
        # operator is looking at the real screen, so scrubbing their briefing
        # would be theatre. What gets *persisted* is scrubbed, because the
        # record outlives the intervention and is read by others.
        self._escalations.append(
            EscalationRecord(
                request_id=request_id,
                step_id=step.id,
                reason=self.redactor.text(reason),
                opened_at=opened,
                resolved_at=datetime.now(timezone.utc),
                resolution=outcome.resolution,  # type: ignore[arg-type]
                operator=outcome.operator,
                human_actions=[self.redactor.text(a) for a in outcome.actions],
            )
        )

        if not outcome.resolved:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="escalation_unresolved",
                    step_id=step.id,
                    step_index=index,
                    intent=step.intent,
                    signal_id=signal_id,
                    expected="a human operator to resolve the blocked step",
                    observed=outcome.note or f"escalation {outcome.resolution}",
                ),
            )

        if outcome.resume_from_step is not None:
            self._resume_at(capability, step, index, outcome.resume_from_step)

    def _resume_at(
        self, capability: Capability, step: Step, index: int, step_id: str
    ) -> None:
        """Wind the run back to ``step_id`` after a human intervention.

        Rewinding re-executes the steps in between, which is safe for reads
        and navigation and emphatically not safe for anything that creates a
        record or moves money. So the rewind is refused if it would cross an
        irreversible step: the human is told to finish the flow manually
        rather than have the machine risk submitting the same request twice.
        A double-posted transaction is far worse than a failed run.
        """
        ids = [s.id for s in capability.steps]
        if step_id not in ids:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="escalation_unresolved",
                    step_id=step.id,
                    step_index=index,
                    intent=step.intent,
                    expected="the operator to name a step in this capability",
                    observed=f"unknown resume point {step_id!r}",
                ),
            )

        target = ids.index(step_id)
        crossed = [
            s.id
            for s in capability.steps[target : index + 1]
            if s.risk == "irreversible"
        ]
        if crossed:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="escalation_unresolved",
                    step_id=step.id,
                    step_index=index,
                    intent=step.intent,
                    expected=f"a resume point that does not re-run {crossed}",
                    observed=(
                        f"resuming at {step_id!r} would re-execute irreversible "
                        f"step(s) {crossed}; refused"
                    ),
                ),
            )

        raise _ResumeAt(target)

    # -- outputs and completion -------------------------------------------

    def _extract_after(self, capability: Capability, step: Step, index: int) -> None:
        for spec in capability.outputs:
            if spec.after_step != step.id:
                continue
            self._extract_one(capability, spec, step.id, index)

    def _extract_one(
        self, capability: Capability, spec: Any, step_id: str | None, index: int | None
    ) -> None:
        try:
            value, _ = extract.extract(self._surface(), spec)
        except (extract.ExtractionError, SurfaceError) as exc:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="checkpoint_failed",
                    step_id=step_id,
                    step_index=index,
                    expected=f"declared output {spec.name!r} to be readable",
                    observed=str(exc),
                ),
            )
        else:
            if value is not None:
                self._outputs[spec.name] = value

    def _finish(self, capability: Capability) -> ReplayResult:
        surface = self._surface()
        success = assertions.wait_until(
            surface, capability.success.assertion, capability.success.timeout_ms
        )
        if not success.ok:
            return self._fail(
                capability,
                FailureDetail(
                    category="checkpoint_failed",
                    step_id=None,
                    step_index=None,
                    expected=f"{capability.success.description} ({success.expected})",
                    observed=success.observed,
                ),
            )

        for spec in capability.outputs:
            if spec.after_step is None and spec.name not in self._outputs:
                self._extract_one(capability, spec, None, None)

        missing = [
            spec.name
            for spec in capability.outputs
            if spec.required and spec.name not in self._outputs
        ]
        if missing:
            return self._fail(
                capability,
                FailureDetail(
                    category="checkpoint_failed",
                    step_id=None,
                    step_index=None,
                    expected=f"required outputs {missing}",
                    observed="not present on the final screen",
                ),
            )

        return ReplaySuccess(**self._envelope(capability), outputs=dict(self._outputs))

    # -- result assembly ---------------------------------------------------

    def _envelope(self, capability: Capability) -> dict[str, Any]:
        finished = datetime.now(timezone.utc)
        return {
            "run_id": self.run_id,
            "capability_id": capability.id,
            "capability_version": capability.version,
            "started_at": self._started,
            "finished_at": finished,
            "duration_ms": int((finished - self._started).total_seconds() * 1000),
            "steps": list(self._steps),
            "recoveries": list(self._recoveries),
            "escalations": list(self._escalations),
        }

    def _terminate_business(
        self, capability: Capability, rule: SignalRule, outcome: _Outcome
    ) -> None:
        raise _Terminate(
            "business_outcome",
            ReplayBusinessOutcome(
                **self._envelope(capability),
                code=rule.outcome_code or rule.id.upper(),
                message=rule.message or rule.description,
                signal_id=rule.id,
                outputs=dict(self._outputs),
            ),
        )

    def _terminate_failure(
        self, capability: Capability, detail: FailureDetail
    ) -> None:
        raise _Terminate("failed", self._fail(capability, detail))

    def _fail(self, capability: Capability, detail: FailureDetail) -> ReplayFailure:
        # Failure diagnostics quote the screen, and the screen is regulated
        # data. Declared outputs are returned to the caller unredacted -- they
        # asked for them -- but an excerpt captured for debugging is scrubbed,
        # because it is written to disk and read by people who did not make the
        # request.
        detail.expected = self.redactor.text(detail.expected)
        detail.observed = self.redactor.text(detail.observed)

        # Both captures are best-effort and separately guarded: evidence
        # collection must never replace the fault it is documenting.
        #
        # Nothing visual is captured when the run never reached the
        # application -- a rejected argument or a denied first step leaves the
        # browser on about:blank, and a folder of blank screenshots teaches a
        # reader to ignore screenshots.
        shot, observation = b"", ""
        if self._touched_the_app:
            try:
                shot = self.session.surface.screenshot()
            except Exception:  # noqa: BLE001
                shot = b""
            try:
                observation = self.redactor.text(
                    self.session.surface.observe().render()
                )
            except Exception:  # noqa: BLE001
                observation = ""

        detail.evidence = self.sink.on_failure(
            self.run_id, detail, shot, observation
        )
        return ReplayFailure(**self._envelope(capability), error=detail)

    def _record_step(
        self,
        step: Step,
        index: int,
        status: str,
        resolution: Resolution | None,
        started: float,
        checkpoint_ok: bool | None,
        note: str = "",
    ) -> None:
        record = StepRecord(
            step_id=step.id,
            index=index,
            intent=step.intent,
            action_kind=step.action.kind,
            status=status,  # type: ignore[arg-type]
            resolved_by=resolution.kind if resolution else None,
            strategy_rank=resolution.rank if resolution else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            checkpoint_ok=checkpoint_ok,
            note=self.redactor.text(note),
        )
        self._steps.append(record)
        self.sink.on_step(self.run_id, record)

    # -- helpers -----------------------------------------------------------

    def _surface(self):
        self.session.require(Actor.AGENT)
        return self.session.surface

    @property
    def _touched_the_app(self) -> bool:
        """Whether the run ever reached a real page.

        Distinguishes faults the application produced from faults we produced
        before contacting it -- an invalid argument, a policy denial at step
        zero. Visual evidence is only meaningful for the former.
        """
        if self._steps:
            return True
        url = self._current_url()
        return bool(url) and not url.startswith(("about:", "chrome:", "data:"))

    def _current_url(self) -> str:
        try:
            return self.session.surface.frame_url(_TOP)
        except SurfaceError:
            return ""

    def _target_url(self, step: Step, bound: dict[str, str]) -> str | None:
        """Where a navigation would land, for the allowlist check.

        Checking only the current page would let one navigate step walk the
        session straight off the allowlist -- the check has to happen before
        the jump, not after it.
        """
        if not isinstance(step.action, NavigateAction):
            return None
        try:
            return params.resolve(step.action.url, bound)
        except params.ParamError:
            return None

    def _check_deadline(self, capability: Capability, step: Step, index: int) -> None:
        if time.monotonic() > self._deadline:
            self._terminate_failure(
                capability,
                FailureDetail(
                    category="timeout",
                    step_id=step.id,
                    step_index=index,
                    intent=step.intent,
                    expected=f"run to finish within {self.max_duration_ms}ms",
                    observed="overall run deadline exceeded",
                ),
            )


def _category_for(rule: SignalRule) -> FailureCategory:
    """Map a hard-failure signal onto a debuggable category."""
    slug = rule.id.lower()
    if "session" in slug or "expired" in slug or "signed_out" in slug:
        return "session_lost"
    return "app_error"


from .schema.common import FrameRef  # noqa: E402

_TOP = FrameRef()
