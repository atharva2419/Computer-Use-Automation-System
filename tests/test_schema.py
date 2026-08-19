"""The artifact schema must reject incoherent capabilities, not just describe them."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from cua.schema import (
    AppBinding,
    Capability,
    Checkpoint,
    ClickAction,
    DismissAndRetry,
    FillAction,
    FrameRef,
    LiteralValue,
    NavigateAction,
    ParamSpec,
    ParamValue,
    RoleNameStrategy,
    SignalRule,
    Step,
    Target,
    TextPresent,
)

SEED = pathlib.Path("artifacts/meridian.member.read_savings_balance@v1.json")

TOP = FrameRef(kind="top")


def _target(name: str = "Go") -> Target:
    return Target(
        described_as=f"the {name} button",
        frame=TOP,
        strategies=[RoleNameStrategy(role="button", name=name)],
    )


def _minimal(**overrides: Any) -> Capability:
    # Annotated dict[str, Any]: this is a kwargs builder, so its values are
    # heterogeneous by construction and a narrower inferred type only fights
    # the splat below.
    base: dict[str, Any] = dict(
        id="t.cap",
        name="t",
        description="d",
        app=AppBinding(product="p", entry_url="http://x/"),
        steps=[
            Step(
                id="s1",
                intent="go",
                action=NavigateAction(url=LiteralValue(value="http://x/"), frame=TOP),
            )
        ],
        success=Checkpoint(
            description="done", assertion=TextPresent(frame=TOP, text="OK")
        ),
    )
    base.update(overrides)
    return Capability(**base)


# --- the seed artifact is real and round-trips -----------------------------


def test_seed_artifact_loads_and_round_trips():
    cap = Capability.model_validate_json(SEED.read_text(encoding="utf-8"))
    assert cap.id == "meridian.member.read_savings_balance"
    assert [o.name for o in cap.outputs] == [
        "member_name",
        "savings_balance",
        "savings_account_number",
    ]
    again = Capability.model_validate(json.loads(json.dumps(cap.model_dump(mode="json"))))
    assert again == cap


def test_seed_declares_all_three_signal_classes():
    """The error model is only useful if it actually distinguishes the classes."""
    cap = Capability.model_validate_json(SEED.read_text(encoding="utf-8"))
    classes = {s.classification for s in cap.signals}
    assert classes == {"business_outcome", "recoverable", "hard_failure"}


def test_seed_secret_param_has_no_example_value():
    """A secret must never carry a persisted sample value."""
    cap = Capability.model_validate_json(SEED.read_text(encoding="utf-8"))
    for param in cap.inputs:
        if param.secret:
            assert param.example is None


# --- referential integrity --------------------------------------------------


def test_rejects_reference_to_undeclared_parameter():
    with pytest.raises(ValidationError, match="undeclared parameter"):
        _minimal(
            steps=[
                Step(
                    id="s1",
                    intent="type",
                    action=FillAction(
                        target=_target(), value=ParamValue(param="nope")
                    ),
                )
            ]
        )


def test_accepts_declared_parameter():
    cap = _minimal(
        inputs=[ParamSpec(name="yes")],
        steps=[
            Step(
                id="s1",
                intent="type",
                action=FillAction(target=_target(), value=ParamValue(param="yes")),
            )
        ],
    )
    assert cap.inputs[0].name == "yes"


def test_rejects_duplicate_step_ids():
    step = Step(
        id="dup",
        intent="click",
        action=ClickAction(target=_target()),
    )
    with pytest.raises(ValidationError, match="unique"):
        _minimal(steps=[step, step.model_copy()])


def test_rejects_duplicate_outcome_codes():
    def sig(sid: str) -> SignalRule:
        return SignalRule(
            id=sid,
            description="d",
            detect=TextPresent(frame=TOP, text="X"),
            classification="business_outcome",
            outcome_code="SAME",
        )

    with pytest.raises(ValidationError, match="outcome codes must be unique"):
        _minimal(signals=[sig("a"), sig("b")])


# --- signal coherence -------------------------------------------------------


def test_business_outcome_requires_outcome_code():
    """Otherwise the caller has nothing to branch on -- the whole point."""
    with pytest.raises(ValidationError, match="requires an outcome_code"):
        SignalRule(
            id="s",
            description="d",
            detect=TextPresent(frame=TOP, text="X"),
            classification="business_outcome",
        )


def test_recoverable_requires_a_recovery_action():
    with pytest.raises(ValidationError, match="requires a recovery action"):
        SignalRule(
            id="s",
            description="d",
            detect=TextPresent(frame=TOP, text="X"),
            classification="recoverable",
        )


def test_recovery_rejected_on_non_recoverable_signal():
    with pytest.raises(ValidationError, match="only meaningful for recoverable"):
        SignalRule(
            id="s",
            description="d",
            detect=TextPresent(frame=TOP, text="X"),
            classification="hard_failure",
            recovery=DismissAndRetry(dismiss=_target()),
        )


def test_escalation_only_on_hard_failure():
    with pytest.raises(ValidationError, match="escalate only applies"):
        SignalRule(
            id="s",
            description="d",
            detect=TextPresent(frame=TOP, text="X"),
            classification="business_outcome",
            outcome_code="C",
            escalate=True,
        )


# --- strictness -------------------------------------------------------------


def test_unknown_field_is_rejected():
    """A misspelled key must fail loudly, not be silently ignored."""
    with pytest.raises(ValidationError):
        Capability.model_validate(
            {
                **_minimal().model_dump(mode="json"),
                "sucess": {"description": "typo", "assertion": {}},
            }
        )


def test_target_requires_at_least_one_strategy():
    with pytest.raises(ValidationError):
        Target(described_as="nothing", frame=TOP, strategies=[])


def test_target_durability_reports_preferred_strategy():
    from cua.schema import CssStrategy, LabelCellStrategy

    t = Target(
        described_as="x",
        frame=TOP,
        strategies=[LabelCellStrategy(label="L"), CssStrategy(css="input")],
    )
    assert t.durability == 1
