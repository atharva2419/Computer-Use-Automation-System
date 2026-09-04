"""The capability artifact: a recorded flow an AI agent can invoke by name.

Shape of the contract
---------------------
A capability is deliberately *not* a step list. It is four things:

  1. an interface  -- typed ``inputs`` and ``outputs``, so a calling agent can
     discover and invoke it without knowing anything about the UI;
  2. a plan        -- ordered ``steps``, each with a target, a checkpoint and
     a risk class;
  3. an error model -- ``signals``, the declared runtime conditions this flow
     can encounter and what each one *means*; and
  4. provenance    -- which model discovered it, against which app version,
     and whether a human has approved it.

The error model is the part that makes this production-shaped rather than a
macro recorder. Signals are declared once at capability level and evaluated
after every step, because "session expired" and "application error" can
happen anywhere; step-local signals handle the conditions that are only
meaningful at one point in the flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import RISK_ORDER, Assertion, FrameRef, Risk, Sensitivity, ValueRef
from .targets import Target

SCHEMA_VERSION = "1.0.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Interface: typed inputs and outputs
# ---------------------------------------------------------------------------

ParamType = Literal["string", "number", "boolean", "money"]


class ParamSpec(_Strict):
    """One input the calling agent supplies per invocation."""

    name: str
    type: ParamType = "string"
    required: bool = True
    description: str = ""
    example: str | None = Field(
        default=None,
        description="Illustrative value for catalog display. Must be synthetic: "
        "this is persisted in the artifact.",
    )
    pattern: str | None = Field(
        default=None, description="Regex the value must match before replay starts."
    )
    sensitivity: Sensitivity = "internal"
    secret: bool = Field(
        default=False,
        description="Credential-like. Never logged, never captured in evidence, "
        "never echoed into an escalation payload.",
    )


class Transform(_Strict):
    """Normalisation applied to extracted text before it is returned."""

    kind: Literal["none", "strip", "money", "number", "regex"] = "strip"
    pattern: str | None = Field(
        default=None, description="For kind=regex: first capture group is taken."
    )


class Extraction(_Strict):
    """Where an output value is read from."""

    target: Target
    source: Literal["text", "value", "attribute"] = "text"
    attribute: str | None = None
    transform: Transform = Field(default_factory=Transform)


class OutputSpec(_Strict):
    """One value the capability returns to its caller."""

    name: str
    type: ParamType = "string"
    description: str = ""
    extract: Extraction
    required: bool = True
    after_step: str | None = Field(
        default=None,
        description="Read immediately after this step id. Needed when the value "
        "is only on screen mid-flow. Defaults to after the final step.",
    )
    sensitivity: Sensitivity = "internal"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class NavigateAction(_Strict):
    kind: Literal["navigate"] = "navigate"
    url: ValueRef
    frame: FrameRef = Field(default_factory=FrameRef)


class ClickAction(_Strict):
    kind: Literal["click"] = "click"
    target: Target


class FillAction(_Strict):
    kind: Literal["fill"] = "fill"
    target: Target
    value: ValueRef
    clear_first: bool = True


class SelectAction(_Strict):
    kind: Literal["select"] = "select"
    target: Target
    value: ValueRef


class PressAction(_Strict):
    kind: Literal["press"] = "press"
    target: Target
    key: str


class WaitForAction(_Strict):
    """An explicit wait on a condition -- never a fixed sleep.

    Sleeps are the classic source of replay flake: too short and the run is
    nondeterministic, too long and every invocation pays for the worst case.
    Every wait in this schema is a predicate with a timeout.
    """

    kind: Literal["wait_for"] = "wait_for"
    condition: Assertion


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        FillAction,
        SelectAction,
        PressAction,
        WaitForAction,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


class Checkpoint(_Strict):
    """Proof that a step actually produced the state it was supposed to.

    Asserting after every step -- rather than only at the end -- is what makes
    a failure debuggable: the result names the first step whose world did not
    match expectation, instead of reporting that the flow ended somewhere odd.
    """

    description: str
    assertion: Assertion
    timeout_ms: int = 8000


# ---------------------------------------------------------------------------
# Signals: the declared error model (brief 3.3)
# ---------------------------------------------------------------------------

SignalClass = Literal["business_outcome", "recoverable", "hard_failure"]
"""Three-way classification of a detected runtime condition.

business_outcome
    A legitimate answer the caller asked for. "No such member" is not a crash;
    the replay *succeeded* at finding out that the member does not exist.
    Terminates the run and returns an outcome code.
recoverable
    A condition the flow knows how to clear and continue through: an
    interstitial to dismiss, a transient slow load to wait out. Handled inline;
    the run continues and the recovery is recorded as evidence.
hard_failure
    Something the flow cannot safely proceed through. Stops the run and
    reports where and why -- optionally routing to a human first.
"""


class DismissAndRetry(_Strict):
    """Clear a blocking overlay/interstitial, then re-run the current step."""

    kind: Literal["dismiss_and_retry"] = "dismiss_and_retry"
    dismiss: Target
    max_attempts: int = 2


class WaitAndRetry(_Strict):
    """Wait out a transient condition, then re-run the current step."""

    kind: Literal["wait_and_retry"] = "wait_and_retry"
    wait_ms: int = 1500
    max_attempts: int = 3


class ReloadAndRetry(_Strict):
    kind: Literal["reload_and_retry"] = "reload_and_retry"
    frame: FrameRef = Field(default_factory=FrameRef)
    max_attempts: int = 2


Recovery = Annotated[
    Union[DismissAndRetry, WaitAndRetry, ReloadAndRetry],
    Field(discriminator="kind"),
]


class SignalRule(_Strict):
    """One runtime condition this capability knows how to recognise."""

    id: str = Field(description="Stable slug, e.g. member_not_found.")
    description: str
    detect: Assertion
    classification: SignalClass
    outcome_code: str | None = Field(
        default=None,
        description="Machine-readable code returned to the caller for "
        "classification=business_outcome, e.g. MEMBER_NOT_FOUND.",
    )
    recovery: Recovery | None = None
    escalate: bool = Field(
        default=False,
        description="Route to a human operator before giving up. Only "
        "meaningful for hard_failure.",
    )
    message: str = Field(default="", description="Human-readable explanation.")

    @model_validator(mode="after")
    def _coherent(self) -> "SignalRule":
        if self.classification == "business_outcome" and not self.outcome_code:
            raise ValueError(
                f"signal {self.id!r}: business_outcome requires an outcome_code, "
                "otherwise the caller cannot branch on it"
            )
        if self.classification == "recoverable" and self.recovery is None:
            raise ValueError(
                f"signal {self.id!r}: recoverable requires a recovery action"
            )
        if self.classification != "recoverable" and self.recovery is not None:
            raise ValueError(
                f"signal {self.id!r}: recovery is only meaningful for recoverable"
            )
        if self.escalate and self.classification != "hard_failure":
            raise ValueError(
                f"signal {self.id!r}: escalate only applies to hard_failure"
            )
        return self


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Step(_Strict):
    id: str = Field(description="Stable within the capability; used to resume.")
    intent: str = Field(
        description="What this step is for, in operator language. Carried into "
        "logs and into the escalation payload a human reads."
    )
    action: Action
    risk: Risk = "safe"
    checkpoint: Checkpoint | None = None
    timeout_ms: int = 10000
    signals: list[SignalRule] = Field(
        default_factory=list,
        description="Step-local conditions, evaluated before capability-level "
        "signals so a step can specialise the meaning of a shared condition.",
    )


# ---------------------------------------------------------------------------
# App binding and tenant overlays (brief 3.7)
# ---------------------------------------------------------------------------


class AppBinding(_Strict):
    """Which application this capability was recorded against.

    The multi-tenant seam. A capability is recorded against a *product* at a
    *version*, not against a tenant. ``tenant`` is null for the base artifact
    that every institution running the product inherits; a tenant that needs
    something different publishes an overlay rather than a fork, so an upstream
    fix to the base flow reaches all of them.
    """

    product: str = Field(description="Vendor product id, e.g. meridian-core.")
    product_version: str = "unknown"
    tenant: str | None = Field(
        default=None, description="Null for the vendor-level base capability."
    )
    entry_url: str = Field(description="Where the flow starts.")
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"


class Overlay(_Strict):
    """Per-tenant specialisation of a base capability.

    Deliberately narrow: an overlay may re-target a control or change a
    literal, and nothing else. It cannot add, remove or reorder steps, and it
    cannot change the typed interface or the risk classification. That
    restriction is the point -- an overlay that could rewrite the flow is a
    fork with extra steps, and would let a tenant silently diverge from the
    reviewed base.
    """

    base_capability_id: str
    base_capability_version: int
    tenant: str
    notes: str = ""
    target_overrides: dict[str, Target] = Field(
        default_factory=dict,
        description="step_id -> replacement target for that step's action.",
    )
    value_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="step_id -> replacement literal value.",
    )
    entry_url: str | None = None


# ---------------------------------------------------------------------------
# Provenance and approval
# ---------------------------------------------------------------------------


class Provenance(_Strict):
    """Where this capability came from, without the transcript.

    The brief asks for the artifact to be decoupled from the raw model
    transcript. It is: the transcript stays in the evidence bundle and is
    referenced by run id and digest, so the artifact stays reviewable and
    diffable while the discovery trail remains auditable.
    """

    discovered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    model: str = "unknown"
    discovery_run_id: str | None = None
    transcript_digest: str | None = Field(
        default=None, description="SHA-256 of the stored transcript."
    )
    recorded_by: Literal["llm_discovery", "human", "imported"] = "llm_discovery"


ApprovalState = Literal["draft", "approved", "revoked"]


class Capability(_Strict):
    """A versioned, reviewable, agent-invocable automation capability."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(description="Stable dotted id, e.g. meridian.member.read_balance.")
    version: int = Field(default=1, description="Bumped on any change to steps.")
    name: str
    description: str = Field(
        description="What the capability does, in terms a calling agent can "
        "match a user intent against."
    )

    app: AppBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    signals: list[SignalRule] = Field(
        default_factory=list,
        description="Capability-wide conditions, checked after every step.",
    )
    success: Checkpoint = Field(
        description="The final condition that defines the goal as met. Separate "
        "from the last step's checkpoint: the last step asserts that the click "
        "landed, this asserts that the business outcome happened."
    )

    approval: ApprovalState = "draft"
    provenance: Provenance = Field(default_factory=Provenance)
    tags: list[str] = Field(default_factory=list)

    # -- integrity ---------------------------------------------------------

    @model_validator(mode="after")
    def _referential_integrity(self) -> "Capability":
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique within a capability")

        declared = {p.name for p in self.inputs}
        for step in self.steps:
            for ref in _param_refs(step.action):
                if ref not in declared:
                    raise ValueError(
                        f"step {step.id!r} references undeclared parameter {ref!r}"
                    )

        for out in self.outputs:
            if out.after_step is not None and out.after_step not in step_ids:
                raise ValueError(
                    f"output {out.name!r} refers to unknown step {out.after_step!r}"
                )

        # Signal *ids* must be unique -- they name a detection, and evidence
        # refers to them. Outcome *codes* deliberately need not be: a code is
        # the caller's vocabulary for a business condition, and one condition
        # can surface on more than one screen. The hosted console reports a
        # missing member two ways -- an inline "no records matched" on the
        # search screen, and a full Record Not Found page on retrieval -- and
        # forcing distinct codes there would either lose a detection or invent
        # a distinction the caller has no reason to branch on.
        signal_ids = [s.id for s in self.signals]
        if len(set(signal_ids)) != len(signal_ids):
            raise ValueError("signal ids must be unique within a capability")
        return self

    # -- convenience -------------------------------------------------------

    def input(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @property
    def max_risk(self) -> Risk:
        """The riskiest thing this capability does.

        Drives approval: a read-only capability can reasonably be replayed
        unattended, one that creates records should not be until a human has
        approved it.
        """
        worst: Risk = "safe"
        for step in self.steps:
            if RISK_ORDER[step.risk] > RISK_ORDER[worst]:
                worst = step.risk
        return worst


def _param_refs(action: Action) -> list[str]:
    """Parameter names referenced by an action."""
    refs: list[str] = []
    for attr in ("url", "value"):
        val = getattr(action, attr, None)
        if val is not None and getattr(val, "kind", None) == "param":
            refs.append(val.param)
    return refs
