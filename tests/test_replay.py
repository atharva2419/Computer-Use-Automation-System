"""Replay engine tests -- the error taxonomy, end to end, against the live app.

Every runtime condition in brief 3.3 gets a test, and each one asserts the
*classification*, not merely that something went wrong. The distinction the
brief calls the most common design mistake -- a business outcome reported as a
crash -- is only meaningful if it is pinned down here.

Faults are armed through the target app's out-of-band control, at a precise
point in the run: an ``EvidenceSink`` is the natural injection seam, because
the engine already calls it after every step. That lets a test say "expire the
session *after sign-on*" without the engine knowing anything about testing.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from werkzeug.serving import make_server

from cua.guardrails import GateDecision, GateRequest, Policy, PolicyGate
from cua.replay import NullSink, ReplayEngine
from cua.schema.capability import Capability
from cua.schema.result import StepRecord
from cua.session import Session
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
    """The seed artifact, re-pointed at the ephemeral test port.

    Only the entry URL is rewritten. Steps, locators, signals and checkpoints
    are exactly what ships in the repo -- the tests must exercise the real
    artifact, not a convenient variant of it.
    """
    raw = json.loads(ARTIFACT.read_text("utf-8"))
    raw["app"]["entry_url"] = f"{base_url}/login"
    for step in raw["steps"]:
        action = step["action"]
        if action["kind"] == "navigate" and action["url"]["kind"] == "literal":
            action["url"]["value"] = f"{base_url}/login"
    return Capability.model_validate(raw)


@pytest.fixture(autouse=True)
def pristine() -> Iterator[None]:
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()
    yield
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()


@pytest.fixture
def gate(base_url: str) -> PolicyGate:
    """The shipped policy, with the ephemeral test origin allowed.

    Only the origin list is adjusted -- which is genuinely per-deployment
    configuration, not a rule. Routes, denied routes, permitted action kinds
    and every risk rule are exactly what ships in config/policy.yaml, so the
    tests exercise the real guardrail rather than a lenient copy of it.
    """
    policy = Policy.load()
    policy.allowlist.origins = [base_url]
    return PolicyGate(policy)


class Harness:
    """Runs a capability and tears the browser down."""

    def __init__(self, gate: Any = None) -> None:
        self.session: Session | None = None
        self.gate = gate

    def run(self, capability: Capability, params: dict[str, Any], **kwargs: Any):
        kwargs.setdefault("gate", self.gate)
        surface = PlaywrightWebSurface(headless=True).start()
        self.session = Session(surface=surface)
        try:
            return ReplayEngine(self.session, **kwargs).run(capability, params)
        finally:
            surface.close()


@pytest.fixture
def harness(gate: PolicyGate) -> Harness:
    return Harness(gate=gate)


class ArmAfterStep(NullSink):
    """Inject a fault once, immediately after a named step completes.

    Firing only once matters: an escalation may wind the run back and re-run
    that step, and re-arming the fault each time would loop the run forever.
    A real transient fault does not re-arm itself either.
    """

    def __init__(self, step_id: str, **flags: Any) -> None:
        self.step_id = step_id
        self.flags = flags
        self.fired = False

    def on_step(self, run_id: str, record: StepRecord) -> None:
        if record.step_id == self.step_id and not self.fired:
            self.fired = True
            for key, value in self.flags.items():
                setattr(target_module.chaos, key, value)


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_happy_path_returns_typed_outputs(harness: Harness, capability: Capability) -> None:
    result = harness.run(capability, {"member_id": "10001", **CREDS})

    assert result.status == "success", getattr(result, "error", None)
    assert result.outputs["member_name"] == "Ada Wexler"
    assert result.outputs["savings_balance"] == 4210.55
    assert isinstance(result.outputs["savings_balance"], float), "money must be numeric"
    assert result.outputs["savings_account_number"] == "SAV-0001"
    assert [s.step_id for s in result.steps] == [s.id for s in capability.steps]
    assert all(s.status == "ok" for s in result.steps)


def test_parameters_actually_drive_the_run(harness: Harness, capability: Capability) -> None:
    result = harness.run(capability, {"member_id": "10002", **CREDS})
    assert result.status == "success", getattr(result, "error", None)
    assert result.outputs["member_name"] == "Bo Ferreira"
    assert result.outputs["savings_balance"] == 15980.00


def test_replay_is_deterministic(harness: Harness, capability: Capability) -> None:
    first = harness.run(capability, {"member_id": "10001", **CREDS})
    fixture_data.reset()
    second = Harness(gate=harness.gate).run(capability, {"member_id": "10001", **CREDS})

    # Compared separately rather than chained: a chained comparison narrows
    # only its last operand, so `first` would stay the full ReplayResult union
    # and `first.outputs` would not type-check.
    assert first.status == "success", getattr(first, "error", None)
    assert second.status == "success", getattr(second, "error", None)
    assert first.outputs == second.outputs
    assert [s.step_id for s in first.steps] == [s.step_id for s in second.steps]
    assert [s.resolved_by for s in first.steps] == [s.resolved_by for s in second.steps]
    assert [s.strategy_rank for s in first.steps] == [s.strategy_rank for s in second.steps]


def test_no_model_is_loaded_on_the_replay_path(
    harness: Harness, capability: Capability
) -> None:
    """Mechanical proof of 'without the LLM in the decision loop'."""
    sys.modules.pop("anthropic", None)
    result = harness.run(capability, {"member_id": "10001", **CREDS})
    assert result.status == "success"
    assert "anthropic" not in sys.modules


# ---------------------------------------------------------------------------
# Business outcomes -- legitimate answers, not failures
# ---------------------------------------------------------------------------


def test_unknown_member_is_a_business_outcome(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(capability, {"member_id": "99999", **CREDS})

    assert result.status == "business_outcome", "not-found must not be a failure"
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.signal_id == "member_not_found"
    # The run got far enough to ask the question, and says so.
    assert any(s.step_id == "run_search" for s in result.steps)


def test_restricted_record_is_a_distinct_business_outcome(
    harness: Harness, capability: Capability
) -> None:
    """The record exists; the operator is not entitled to it.

    Collapsing this into 'not found' would tell the caller something false.
    """
    result = harness.run(capability, {"member_id": "20002", **CREDS})

    assert result.status == "business_outcome"
    assert result.code == "PERMISSION_DENIED"
    assert result.code != "MEMBER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Recoverable conditions -- handled inline, run still succeeds
# ---------------------------------------------------------------------------


def test_interstitial_is_dismissed_and_the_run_completes(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(
        capability,
        {"member_id": "10001", **CREDS},
        sink=ArmAfterStep("submit_signon", interstitial=1),
    )

    assert result.status == "success", getattr(result, "error", None)
    assert result.outputs["savings_balance"] == 4210.55

    assert len(result.recoveries) == 1, "the recovery must be recorded as evidence"
    recovery = result.recoveries[0]
    assert recovery.signal_id == "system_notice"
    assert recovery.action == "dismiss_and_retry"
    assert recovery.succeeded is True
    # 'recovered' is not a terminal status: the run still ended in success.
    assert result.status == "success"


def test_transient_slow_load_is_waited_out(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(
        capability,
        {"member_id": "10001", **CREDS},
        sink=ArmAfterStep("submit_signon", slow_ms=1200, slow_requests=1),
    )
    assert result.status == "success", getattr(result, "error", None)


# ---------------------------------------------------------------------------
# Hard failures -- stop, and say exactly where and why
# ---------------------------------------------------------------------------


def test_application_fault_is_a_hard_failure_with_debuggable_detail(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(
        capability,
        {"member_id": "10001", **CREDS},
        sink=ArmAfterStep("submit_signon", server_error=1),
    )

    assert result.status == "failed"
    assert result.error.category == "app_error"
    assert result.error.signal_id == "app_error"
    assert result.error.step_id, "must name the step that failed"
    assert result.error.expected and result.error.observed
    assert "MCX-5000" in result.error.observed


def test_session_expiry_escalates_and_fails_closed(
    harness: Harness, capability: Capability
) -> None:
    """Unattended re-authentication is deliberately not automated.

    With no operator handler configured the run must stop rather than press
    on, and the attempted handoff must be recorded.
    """
    result = harness.run(
        capability,
        {"member_id": "10001", **CREDS},
        sink=ArmAfterStep("submit_signon", expire_session=True),
    )

    assert result.status == "failed"
    assert result.error.category == "escalation_unresolved"
    assert len(result.escalations) == 1
    assert result.escalations[0].resolution == "aborted"
    assert "session" in result.escalations[0].reason.lower()


def test_a_resolving_operator_lets_the_run_continue(
    harness: Harness, capability: Capability
) -> None:
    """The same seam, with a handler that fixes the problem and hands back.

    Stands in for step E: it proves the engine resumes the *same* run on the
    *same* session after a human intervenes, rather than aborting.
    """

    class ReAuthenticatingOperator:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, context):
            from cua.replay import EscalationOutcome
            from cua.session import Actor

            self.calls += 1
            session = context.session
            session.cede(Actor.HUMAN, reason="operator taking control")
            surface = session.surface
            # A person re-establishes the session in the live browser.
            surface.navigate(capability.app.entry_url)
            surface.fill(_target(capability, "enter_operator_id"), "op.demo")
            surface.fill(_target(capability, "enter_passphrase"), "demo-pass")
            surface.click(_target(capability, "submit_signon"))
            session.cede(Actor.AGENT, reason="operator handed back")
            return EscalationOutcome(
                resolved=True,
                resolution="resumed",
                operator="human@test",
                actions=["re-authenticated the servicing session"],
                # Re-auth lands on the console home screen, so the flow is no
                # longer where it stopped; wind back to the search step.
                resume_from_step="open_member_search",
            )

    operator = ReAuthenticatingOperator()
    result = harness.run(
        capability,
        {"member_id": "10001", **CREDS},
        escalation=operator,
        sink=ArmAfterStep("open_member_search", expire_session=True),
    )

    assert operator.calls == 1
    assert result.status == "success", getattr(result, "error", None)
    assert len(result.escalations) == 1
    assert result.escalations[0].resolution == "resumed"
    assert result.escalations[0].human_actions == [
        "re-authenticated the servicing session"
    ]
    assert harness.session is not None and harness.session.human_touched


def _target(capability: Capability, step_id: str):
    step = capability.step(step_id)
    assert step is not None
    return step.action.target  # type: ignore[union-attr]


def test_rewinding_across_an_irreversible_step_is_refused(
    harness: Harness, capability: Capability
) -> None:
    """A resume point that would re-execute an irreversible step is rejected.

    Winding back re-runs the steps in between. That is harmless for reads and
    navigation and unacceptable for anything that creates a record or moves
    money: a double-posted transaction is far worse than a failed run. Here
    the search step is marked irreversible to stand in for such an action.
    """
    from cua.replay import EscalationOutcome

    raw = capability.model_dump()
    for step in raw["steps"]:
        if step["id"] == "run_search":
            step["risk"] = "irreversible"
    risky = Capability.model_validate(raw)

    class RewindsTooFar:
        def request(self, context):
            return EscalationOutcome(
                resolved=True,
                resolution="resumed",
                operator="human@test",
                resume_from_step="open_member_search",
            )

    result = harness.run(
        risky,
        {"member_id": "10001", **CREDS},
        escalation=RewindsTooFar(),
        sink=ArmAfterStep("run_search", expire_session=True),
    )

    assert result.status == "failed"
    assert result.error.category == "escalation_unresolved"
    assert "irreversible" in result.error.observed
    assert "run_search" in result.error.observed


# ---------------------------------------------------------------------------
# Guardrails, end to end (brief 3.4)
# ---------------------------------------------------------------------------


def test_an_irreversible_step_routes_to_a_human_mid_run(
    harness: Harness, capability: Capability
) -> None:
    """Risky actions are not blocked -- a person approves them, and it is logged.

    Blocking would make the sub-account flow impossible to run at all. Routing
    to the operator instead means the flow completes, a human owns the
    consequential decision, and the approval lands in the run evidence.
    """
    from cua.replay import EscalationOutcome

    raw = capability.model_dump()
    for step in raw["steps"]:
        if step["id"] == "run_search":
            step["risk"] = "irreversible"
    risky = Capability.model_validate(raw)

    approvals: list[str] = []

    class ApprovingOperator:
        def request(self, context):
            approvals.append(context.reason)
            return EscalationOutcome(
                resolved=True, resolution="resumed", operator="supervisor@test"
            )

    result = harness.run(
        risky, {"member_id": "10001", **CREDS}, escalation=ApprovingOperator()
    )

    assert len(approvals) == 1
    assert "approval" in approvals[0]
    assert result.status == "success", getattr(result, "error", None)
    assert len(result.escalations) == 1
    assert result.escalations[0].operator == "supervisor@test"


def test_the_agent_cannot_reach_the_fault_injection_endpoint(
    harness: Harness, capability: Capability
) -> None:
    """A run must not be able to switch off its own error conditions."""
    raw = capability.model_dump()
    raw["steps"][0]["action"]["url"]["value"] = (
        raw["app"]["entry_url"].replace("/login", "/__chaos")
    )
    tampered = Capability.model_validate(raw)

    result = harness.run(tampered, {"member_id": "10001", **CREDS})

    assert result.status == "failed"
    assert result.error.category == "policy_denied"
    assert "__chaos" in result.error.observed
    assert result.steps == [], "the navigation must not have happened"


def test_failure_diagnostics_are_redacted(
    harness: Harness, capability: Capability
) -> None:
    """Excerpts quoted for debugging get scrubbed; declared outputs do not.

    The failure here is forced on the member record screen, so the excerpt the
    engine captures contains a real account number.
    """
    # Asserting the *absence* of something that is present makes the engine
    # quote it: the excerpt window centres on the text it found, so the
    # captured diagnostic genuinely contains an account number rather than
    # merely the head of the page.
    raw = capability.model_dump()
    raw["success"]["assertion"] = {
        "kind": "text_absent",
        "frame": {"kind": "name", "value": "main"},
        "text": "SAV-0001",
        "case_sensitive": False,
    }
    raw["success"]["timeout_ms"] = 1000
    doomed = Capability.model_validate(raw)

    result = harness.run(doomed, {"member_id": "10001", **CREDS})

    assert result.status == "failed"
    assert "SAV-0001" not in result.error.observed
    assert "[REDACTED:account_number]" in result.error.observed
    assert "demo-pass" not in result.error.observed


# ---------------------------------------------------------------------------
# Caller-side rejection -- before the browser opens
# ---------------------------------------------------------------------------


def test_malformed_parameter_is_rejected_without_touching_the_ui(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(capability, {"member_id": "not-an-id", **CREDS})

    assert result.status == "failed"
    assert result.error.category == "invalid_input"
    assert result.steps == [], "nothing should have executed"
    assert "member_id" in result.error.observed


def test_missing_required_parameter_is_rejected(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(capability, {"operator_id": "op.demo"})
    assert result.status == "failed"
    assert result.error.category == "invalid_input"
    assert "required" in result.error.observed


def test_secret_values_are_never_echoed_in_errors(
    harness: Harness, capability: Capability
) -> None:
    result = harness.run(
        capability,
        {"member_id": "10001", "operator_id": "op.demo", "operator_passphrase": ""},
    )
    assert result.status == "failed"
    assert "hunter2" not in result.error.observed


# ---------------------------------------------------------------------------
# Policy seam
# ---------------------------------------------------------------------------


def test_policy_gate_can_stop_a_step_before_it_acts(
    harness: Harness, capability: Capability
) -> None:
    class DenyNavigation:
        def check(self, request: GateRequest) -> GateDecision:
            if request.step.action.kind == "click" and request.step.id == "run_search":
                return GateDecision(allowed=False, reason="search disabled by policy")
            return GateDecision(allowed=True)

    result = harness.run(capability, {"member_id": "10001", **CREDS}, gate=DenyNavigation())

    assert result.status == "failed"
    assert result.error.category == "policy_denied"
    assert result.error.step_id == "run_search"
    assert "policy" in result.error.observed
