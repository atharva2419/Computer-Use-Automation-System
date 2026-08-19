"""Guardrail and redaction tests (brief 3.4).

Most of these are unit tests against the shipped ``config/policy.yaml`` rather
than against a fixture policy. That is deliberate: the thing being evaluated is
the policy the repo actually ships with, and a test that invents its own
lenient policy proves only that the code can enforce *something*.
"""

from __future__ import annotations

import pytest

from cua.guardrails import (
    GateDecision,
    GateRequest,
    Policy,
    PolicyGate,
    RiskRule,
    control_name,
)
from cua.redaction import Redactor
from cua.schema.capability import (
    AppBinding,
    Capability,
    Checkpoint,
    ClickAction,
    FillAction,
    NavigateAction,
    Step,
)
from cua.schema.common import FrameRef, LiteralValue, TextPresent
from cua.schema.targets import RoleNameStrategy, Target

TOP = FrameRef(kind="top")
MAIN = FrameRef(kind="name", value="main")
BASE = "http://127.0.0.1:5057"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return Policy.load()


@pytest.fixture(scope="module")
def gate(policy: Policy) -> PolicyGate:
    return PolicyGate(policy)


def _target(name: str) -> Target:
    return Target(
        described_as=f"the {name} control",
        frame=MAIN,
        strategies=[RoleNameStrategy(role="button", name=name)],
    )


def _capability(*steps: Step) -> Capability:
    return Capability(
        id="t.cap",
        name="t",
        description="d",
        app=AppBinding(product="meridian-core", entry_url=f"{BASE}/login"),
        steps=list(steps) or [_click_step("Search")],
        success=Checkpoint(description="done", assertion=TextPresent(frame=TOP, text="OK")),
    )


def _click_step(control: str, step_id: str = "s1") -> Step:
    return Step(id=step_id, intent="click it", action=ClickAction(target=_target(control)))


def _request(step: Step, url: str, target_url: str | None = None) -> GateRequest:
    return GateRequest(
        capability=_capability(step),
        step=step,
        step_index=0,
        current_url=url,
        target_url=target_url,
    )


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_permitted_route_is_allowed(gate: PolicyGate) -> None:
    decision = gate.check(_request(_click_step("Search"), f"{BASE}/frame/search"))
    assert decision.allowed and not decision.require_human


def test_foreign_origin_is_denied(gate: PolicyGate) -> None:
    decision = gate.check(_request(_click_step("Search"), "http://evil.example/frame/search"))
    assert not decision.allowed
    assert "origin" in decision.reason


def test_unlisted_route_on_a_permitted_origin_is_denied(gate: PolicyGate) -> None:
    decision = gate.check(_request(_click_step("Search"), f"{BASE}/admin/users"))
    assert not decision.allowed
    assert "allowlist" in decision.reason


def test_fault_injection_endpoint_is_structurally_unreachable(gate: PolicyGate) -> None:
    """The agent must not be able to disable its own error conditions.

    /__chaos sits on an allowed origin and under no allowed route prefix, and
    is additionally named in denied_routes. Belt and braces on purpose: this
    is the one endpoint whose reachability would invalidate every other test.
    """
    step = Step(
        id="s1",
        intent="reach the control endpoint",
        action=NavigateAction(url=LiteralValue(value=f"{BASE}/__chaos"), frame=TOP),
    )
    decision = gate.check(_request(step, f"{BASE}/console", target_url=f"{BASE}/__chaos"))
    assert not decision.allowed
    assert "__chaos" in decision.reason


def test_navigation_destination_is_checked_not_just_the_current_page(
    gate: PolicyGate,
) -> None:
    """Otherwise one navigate step walks the session off the allowlist."""
    step = Step(
        id="s1",
        intent="leave",
        action=NavigateAction(url=LiteralValue(value="http://evil.example/"), frame=TOP),
    )
    decision = gate.check(_request(step, f"{BASE}/console", target_url="http://evil.example/"))
    assert not decision.allowed


def test_first_navigation_from_a_blank_browser_is_allowed(gate: PolicyGate) -> None:
    """Every run starts on about:blank; denying that would deny every run."""
    step = Step(
        id="s1",
        intent="open the console",
        action=NavigateAction(url=LiteralValue(value=f"{BASE}/login"), frame=TOP),
    )
    decision = gate.check(_request(step, "about:blank", target_url=f"{BASE}/login"))
    assert decision.allowed


def test_a_click_with_no_page_loaded_is_denied(gate: PolicyGate) -> None:
    decision = gate.check(_request(_click_step("Search"), "about:blank"))
    assert not decision.allowed
    assert "no page" in decision.reason


def test_unlisted_action_kind_is_denied(policy: Policy) -> None:
    narrowed = policy.model_copy(deep=True)
    narrowed.allowlist.actions = ["navigate", "click"]
    step = Step(
        id="s1",
        intent="type",
        action=FillAction(target=_target("Member ID"), value=LiteralValue(value="1")),
    )
    decision = PolicyGate(narrowed).check(_request(step, f"{BASE}/frame/search"))
    assert not decision.allowed
    assert "fill" in decision.reason


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


def test_submitting_a_sub_account_requires_a_human(gate: PolicyGate) -> None:
    step = _click_step("Submit Request")
    decision = gate.check(_request(step, f"{BASE}/frame/subaccount/10001"))

    assert decision.allowed, "blocking would make the flow unrecordable"
    assert decision.require_human
    assert decision.risk == "irreversible"


def test_filling_the_same_form_is_not_risky(gate: PolicyGate) -> None:
    """Typing changes nothing server-side; the submit button is the risk.

    Keying risk off the control being activated rather than off "this is a
    form screen" is what keeps the set of actions needing approval small
    enough that approval stays meaningful.
    """
    step = Step(
        id="s1",
        intent="enter the deposit",
        action=FillAction(
            target=Target(
                described_as="Opening Deposit field",
                frame=MAIN,
                strategies=[RoleNameStrategy(role="textbox", name="Opening Deposit")],
            ),
            value=LiteralValue(value="150"),
        ),
    )
    decision = gate.check(_request(step, f"{BASE}/frame/subaccount/10001"))
    assert decision.allowed and not decision.require_human
    assert decision.risk == "safe"


def test_artifact_risk_cannot_lower_the_policy_bar(gate: PolicyGate) -> None:
    """A recording that claims 'safe' does not get to override policy."""
    step = _click_step("Submit Request")
    step.risk = "safe"
    decision = gate.check(_request(step, f"{BASE}/frame/subaccount/10001"))
    assert decision.risk == "irreversible"
    assert decision.require_human


def test_artifact_risk_can_raise_the_bar(gate: PolicyGate) -> None:
    """And a recording that claims worse than policy is believed."""
    step = _click_step("Search")
    step.risk = "irreversible"
    decision = gate.check(_request(step, f"{BASE}/frame/search"))
    assert decision.risk == "irreversible"
    assert decision.require_human


def test_blocking_handling_denies_outright(policy: Policy) -> None:
    strict = policy.model_copy(deep=True)
    strict.risk.handling["irreversible"] = "block"
    step = _click_step("Submit Request")
    decision = PolicyGate(strict).check(_request(step, f"{BASE}/frame/subaccount/10001"))
    assert not decision.allowed
    assert "blocked" in decision.reason


def test_discovery_uses_its_own_handling_table(policy: Policy) -> None:
    relaxed = policy.model_copy(deep=True)
    relaxed.risk.discovery_handling["irreversible"] = "block"
    step = _click_step("Submit Request")
    request = _request(step, f"{BASE}/frame/subaccount/10001")

    assert PolicyGate(relaxed, discovery=False).check(request).allowed
    assert not PolicyGate(relaxed, discovery=True).check(request).allowed


def test_risk_rules_match_in_order(policy: Policy) -> None:
    ordered = policy.model_copy(deep=True)
    ordered.risk.rules = [
        RiskRule(name="first wins", route="/frame/*", action="click", risk="reversible_write"),
        RiskRule(name="never reached", route="/frame/*", action="click", risk="irreversible"),
    ]
    decision = PolicyGate(ordered).check(_request(_click_step("Anything"), f"{BASE}/frame/x"))
    assert decision.risk == "reversible_write"


def test_control_name_comes_from_the_recording_not_the_page() -> None:
    """Policy must be decidable before the action, on trusted input.

    A name read from the live page at action time would be content on an
    untrusted surface, which is exactly what an injected page would target.
    """
    step = _click_step("Submit Request")
    assert control_name(step) == "Submit Request"

    step_without_target = Step(
        id="s2",
        intent="go",
        action=NavigateAction(url=LiteralValue(value=f"{BASE}/login"), frame=TOP),
    )
    assert control_name(step_without_target) is None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_secrets_are_masked_wherever_they_appear() -> None:
    redactor = Redactor()
    redactor.learn_secret("demo-pass")
    text = redactor.text("signing on with demo-pass failed")
    assert "demo-pass" not in text
    assert "[REDACTED:secret]" in text


def test_very_short_secrets_are_not_masked() -> None:
    """Masking a two-character value would destroy every log line it touches."""
    redactor = Redactor()
    redactor.learn_secret("ab")
    assert redactor.text("a table of absolute values") == "a table of absolute values"


def test_patterns_catch_regulated_data_the_system_never_handled(policy: Policy) -> None:
    redactor = policy.redactor()
    text = redactor.text(
        "row: SAV-0001 balance 4210.55 owner ada@example.com ssn 123-45-6789"
    )
    assert "SAV-0001" not in text
    assert "ada@example.com" not in text
    assert "123-45-6789" not in text
    assert "[REDACTED:account_number]" in text


def test_redaction_recurses_through_containers(policy: Policy) -> None:
    redactor = policy.redactor()
    out = redactor.value({"rows": [{"acct": "CHK-0001"}], "note": "ok"})
    assert out["rows"][0]["acct"] == "[REDACTED:account_number]"
    assert out["note"] == "ok"


def test_balances_are_not_redacted(policy: Policy) -> None:
    """A number is not identifying, and the caller asked for it.

    Over-redaction is its own failure: an evidence trail that scrubs every
    figure cannot be used to debug a run about figures.
    """
    redactor = policy.redactor()
    assert redactor.text("balance 4210.55") == "balance 4210.55"
