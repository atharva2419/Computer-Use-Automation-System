"""Human-in-the-loop handoff tests (brief 3.6).

The claim being tested is narrow and specific: a person operates *the same
live session* the automation was using, and the run continues afterwards. So
these tests assert on session identity, on the control ledger, and on activity
captured from the browser -- not merely that a handler was called.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from werkzeug.serving import make_server

from cua.escalation import (
    ConsoleOperatorHandler,
    InterventionRequest,
    OperatorUnavailable,
    ScriptedOperator,
    _HandoffScope,
)
from cua.evidence import FileEvidenceSink
from cua.guardrails import Policy, PolicyGate
from cua.redaction import Redactor
from cua.replay import EscalationContext, EscalationOutcome, ReplayEngine
from cua.schema.capability import Capability
from cua.schema.result import StepRecord
from cua.session import Actor, ControlViolation, Session
from cua.surface.base import ActivityRecorder
from cua.surface.web import PlaywrightWebSurface
from target_app import app as target_module
from target_app import data as fixture_data

ARTIFACT = Path("artifacts/meridian.member.read_savings_balance@v1.json")
CREDS = {"operator_id": "op.demo", "operator_passphrase": "demo-pass"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    port = _free_port()
    server = make_server("127.0.0.1", port, target_module.create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def capability(base_url: str) -> Capability:
    raw = json.loads(ARTIFACT.read_text("utf-8"))
    raw["app"]["entry_url"] = f"{base_url}/login"
    for step in raw["steps"]:
        action = step["action"]
        if action["kind"] == "navigate" and action["url"]["kind"] == "literal":
            action["url"]["value"] = f"{base_url}/login"
    return Capability.model_validate(raw)


@pytest.fixture
def gate(base_url: str) -> PolicyGate:
    policy = Policy.load()
    policy.allowlist.origins = [base_url]
    return PolicyGate(policy)


@pytest.fixture(autouse=True)
def pristine() -> Iterator[None]:
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()
    yield
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()


class ArmAfterStep:
    """Inject a fault once, after a named step. Also a minimal EvidenceSink."""

    def __init__(self, step_id: str, **flags: Any) -> None:
        self.step_id = step_id
        self.flags = flags
        self.fired = False

    def on_step(self, run_id: str, record: StepRecord) -> None:
        if record.step_id == self.step_id and not self.fired:
            self.fired = True
            for key, value in self.flags.items():
                setattr(target_module.chaos, key, value)

    def on_failure(self, run_id, detail, screenshot, observation="") -> list[str]:
        return []

    def on_result(self, run_id, result, capability) -> None:
        return None


def _target_of(capability: Capability, step_id: str):
    """The control a step acts on.

    Navigations and waits have no target, so the union does not guarantee one
    -- worth asserting rather than assuming, since a renamed step would
    otherwise fail somewhere less obvious.
    """
    step = capability.step(step_id)
    assert step is not None, f"no step {step_id!r}"
    target = getattr(step.action, "target", None)
    assert target is not None, f"step {step_id!r} has no target"
    return target


def _reauthenticate(capability: Capability):
    """What a human does when the servicing session has lapsed."""

    def _do(context: EscalationContext) -> None:
        surface = context.session.surface
        surface.navigate(capability.app.entry_url)
        surface.fill(_target_of(capability, "enter_operator_id"), "op.demo")
        surface.fill(_target_of(capability, "enter_passphrase"), "demo-pass")
        surface.click(_target_of(capability, "submit_signon"))

    return _do


# ---------------------------------------------------------------------------
# The control-transfer model
# ---------------------------------------------------------------------------


def test_handoff_scope_returns_control_even_if_the_handler_raises() -> None:
    """A session left in HUMAN with nobody there is worse than a failed run.

    Every later action would be refused with a control violation, a long way
    from the cause. The scope is a context manager precisely so this cannot
    happen.
    """

    class Recorder:
        def __init__(self) -> None:
            self.started = self.stopped = False

        def start_activity_log(self) -> None:
            self.started = True

        def stop_activity_log(self) -> list[str]:
            self.stopped = True
            return ["navigated top -> /login"]

    surface = Recorder()
    session = Session(surface=surface)  # type: ignore[arg-type]
    session.claim(Actor.AGENT)
    context = _context(session)

    with pytest.raises(RuntimeError):
        with _HandoffScope(context, reason="test"):
            raise RuntimeError("operator console crashed")

    assert session.holder is Actor.AGENT, "control must come back"
    assert surface.started and surface.stopped


def test_activity_is_captured_by_watching_not_by_self_report(
    base_url: str,
) -> None:
    """The record of what a human did comes from the live session itself."""
    surface = PlaywrightWebSurface(headless=True).start()
    try:
        assert isinstance(surface, ActivityRecorder)
        surface.navigate(f"{base_url}/login")

        surface.start_activity_log()
        surface.navigate(f"{base_url}/login")  # stands in for a human click
        trail = surface.stop_activity_log()

        assert any("/login" in entry for entry in trail)
        assert all(entry.startswith("navigated ") for entry in trail)
        assert surface.stop_activity_log() == [], "log resets after collection"
    finally:
        surface.close()


def _context(session: Session) -> EscalationContext:
    from cua.schema.capability import NavigateAction, Step
    from cua.schema.common import FrameRef, LiteralValue

    step = Step(
        id="s1",
        intent="do the thing",
        action=NavigateAction(
            url=LiteralValue(value="http://127.0.0.1/"), frame=FrameRef()
        ),
    )
    return EscalationContext(
        run_id="run1",
        capability_id="t.cap",
        goal="a goal",
        step=step,
        step_index=0,
        reason="stuck",
        signal_id=None,
        observed="",
        session=session,
    )


# ---------------------------------------------------------------------------
# The console handler
# ---------------------------------------------------------------------------


def test_headless_sessions_refuse_the_handoff() -> None:
    """Nobody can take over a browser with no window."""
    surface = PlaywrightWebSurface(headless=True)
    session = Session(surface=surface)  # type: ignore[arg-type]
    session.claim(Actor.AGENT)

    with pytest.raises(OperatorUnavailable, match="headed"):
        ConsoleOperatorHandler().request(_context(session))


def test_no_terminal_means_the_run_fails_closed() -> None:
    """An unattended run must not stall for the full timeout waiting on stdin."""

    class Headed:
        headless = False

    session = Session(surface=Headed())  # type: ignore[arg-type]
    session.claim(Actor.AGENT)

    started = time.monotonic()
    outcome = ConsoleOperatorHandler(
        timeout_seconds=30, stream=io.StringIO()
    ).request(_context(session))

    assert outcome.resolved is False
    assert outcome.resolution == "timed_out"
    assert time.monotonic() - started < 5, "should not have waited the full timeout"
    assert session.holder is Actor.AGENT


@pytest.mark.parametrize(
    ("answer", "resolved", "resume"),
    [
        ("resume", True, None),
        ("resume open_member_search", True, "open_member_search"),
        ("abort", False, None),
    ],
)
def test_operator_answers_are_interpreted(
    answer: str, resolved: bool, resume: str | None
) -> None:
    handler = ConsoleOperatorHandler(stream=io.StringIO())

    class Scope:
        actions = ["navigated top -> /login"]

    outcome = handler._interpret(answer, Scope())  # type: ignore[arg-type]

    assert outcome.resolved is resolved
    assert outcome.resume_from_step == resume
    assert outcome.actions == ["navigated top -> /login"]


def test_intervention_briefing_carries_what_an_operator_needs() -> None:
    request = InterventionRequest(
        request_id="r1",
        run_id="run1",
        capability_id="meridian.member.read_savings_balance",
        goal="Read a member's savings balance",
        step_id="run_search",
        step_index=6,
        step_intent="Run the member search.",
        reason="The servicing session expired mid-flow.",
        signal_id="session_expired",
        observed="SESSION EXPIRED",
        frames=[{"name": "main", "url": "http://x/frame/search", "title": "Search"}],
    )
    text = request.briefing()

    for expected in (
        "meridian.member.read_savings_balance",
        "step 6 (run_search)",
        "session_expired",
        "SESSION EXPIRED",
        "http://x/frame/search",
    ):
        assert expected in text


# ---------------------------------------------------------------------------
# End to end: a real interruption, a real takeover, a completed run
# ---------------------------------------------------------------------------


def test_operator_takes_over_the_same_session_and_the_run_completes(
    capability: Capability, gate: PolicyGate, tmp_path: Path
) -> None:
    """The full loop: expire mid-run, hand over, fix it, hand back, finish."""
    sink = FileEvidenceSink(root=tmp_path, label="handoff")
    sink.open(capability, run_kind="replay")

    operator = ScriptedOperator(
        decisions=[
            EscalationOutcome(
                resolved=True,
                resolution="resumed",
                operator="supervisor@test",
                # Re-auth lands on the console home screen, so the flow is no
                # longer where it stopped.
                resume_from_step="open_member_search",
            )
        ],
        sink=sink,
        on_takeover=_reauthenticate(capability),
    )

    surface = PlaywrightWebSurface(headless=True).start()
    session = Session(surface=surface)
    try:
        result = ReplayEngine(
            session,
            gate=gate,
            escalation=operator,
            sink=_Combined(sink, ArmAfterStep("open_member_search", expire_session=True)),
        ).run(capability, {"member_id": "10001", **CREDS})
    finally:
        surface.close()

    assert result.status == "success", getattr(result, "error", None)
    assert result.outputs["savings_balance"] == 4210.55

    # The handoff happened, and is recorded.
    assert len(result.escalations) == 1
    record = result.escalations[0]
    assert record.resolution == "resumed"
    assert record.operator == "supervisor@test"
    assert record.human_actions, "what the human did must be captured"
    assert any("/login" in a for a in record.human_actions)

    # Control moved and came back, on one session.
    assert session.human_touched
    assert [t["to"] for t in session.ledger()] == ["agent", "human", "agent"]
    assert session.holder is Actor.AGENT

    # And the operator was briefed with the live state.
    assert operator.requests[0].signal_id == "session_expired"
    assert operator.requests[0].step_id


def test_intervention_request_is_written_to_evidence(
    capability: Capability, tmp_path: Path
) -> None:
    sink = FileEvidenceSink(root=tmp_path, label="briefing")
    directory = sink.open(capability, run_kind="replay")

    handler = ConsoleOperatorHandler(
        sink=sink, redactor=Redactor(), stream=io.StringIO()
    )

    class Headed:
        headless = False

    session = Session(surface=Headed())  # type: ignore[arg-type]
    session.claim(Actor.AGENT)
    handler.request(_context(session))

    written = directory / "intervention-1.json"
    assert written.exists()
    payload = json.loads(written.read_text("utf-8"))
    assert payload["step_id"] == "s1"
    assert payload["reason"] == "stuck"

    trail = (directory / "run.jsonl").read_text("utf-8")
    assert "intervention_requested" in trail


def test_secrets_never_reach_a_persisted_briefing(
    capability: Capability, tmp_path: Path
) -> None:
    sink = FileEvidenceSink(root=tmp_path, label="redacted")
    directory = sink.open(capability, run_kind="replay")

    redactor = Redactor()
    redactor.learn_secret("demo-pass")
    handler = ConsoleOperatorHandler(
        sink=sink, redactor=redactor, stream=io.StringIO()
    )

    class Headed:
        headless = False

    session = Session(surface=Headed())  # type: ignore[arg-type]
    session.claim(Actor.AGENT)
    context = _context(session)
    context.reason = "sign-on with demo-pass lapsed"
    handler.request(context)

    payload = json.loads((directory / "intervention-1.json").read_text("utf-8"))
    assert "demo-pass" not in payload["reason"]
    assert "[REDACTED:secret]" in payload["reason"]


class _Combined:
    """Fan one sink call out to several. Keeps the test harness honest."""

    def __init__(self, *sinks: Any) -> None:
        self.sinks = sinks

    def on_step(self, run_id: str, record: StepRecord) -> None:
        for sink in self.sinks:
            sink.on_step(run_id, record)

    def on_failure(self, run_id, detail, screenshot, observation="") -> list[str]:
        written: list[str] = []
        for sink in self.sinks:
            written.extend(sink.on_failure(run_id, detail, screenshot, observation))
        return written

    def on_result(self, run_id, result, capability) -> None:
        for sink in self.sinks:
            sink.on_result(run_id, result, capability)
