"""Allowlist and risk enforcement (brief 3.4).

The gate sits in front of every action on both paths -- discovery and replay --
and answers one question: *may this action happen, and does a person need to
approve it first?*

Three properties are what make it a guardrail rather than a suggestion:

**It is consulted before the action, not after.** The engine calls it with the
step it is about to perform and the URL it is about to perform it on. A denial
means nothing happened.

**The agent has no say in it.** Risk comes from this policy -- route, action
kind, and the control being activated -- never from the model or from the
artifact alone. A model that could self-report "this is safe" would make the
guardrail advisory, and the pages being read are untrusted input.

**Disagreement fails safe.** A step in an artifact carries a recorded risk
class. Policy computes its own. The *more dangerous of the two* applies. A
recording that under-claims cannot lower the bar, and a recording that
over-claims is respected even where policy has no rule yet.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .redaction import Redactor
from .schema.capability import Capability, Step
from .schema.common import RISK_ORDER, Risk
from .schema.targets import RoleNameStrategy, Target, TextStrategy

Handling = Literal["allow", "require_human", "block"]

# Resolved from the package location, not the working directory: a
# guardrail that silently disappears when you run from another folder is
# worse than no guardrail, because it looks like it is there.
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskRule(_Strict):
    """One risk classification rule. First match wins."""

    name: str = ""
    route: str | None = Field(default=None, description="Glob on the URL path.")
    action: str | None = Field(default=None, description="Action kind.")
    control: str | None = Field(
        default=None,
        description="Glob on the control's recorded name. Matching on the "
        "control is what keeps the dangerous set small: a form is not risky, "
        "the button that submits it is.",
    )
    risk: Risk

    def matches(self, path: str, action_kind: str, control: str | None) -> bool:
        if self.action and self.action != action_kind:
            return False
        if self.route and not fnmatch.fnmatch(path, self.route):
            return False
        if self.control:
            if control is None:
                return False
            if not fnmatch.fnmatch(control.lower(), self.control.lower()):
                return False
        return True


class Allowlist(_Strict):
    origins: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    denied_routes: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


class RiskPolicy(_Strict):
    rules: list[RiskRule] = Field(default_factory=list)
    default: Risk = "safe"
    handling: dict[Risk, Handling] = Field(default_factory=dict)
    discovery_handling: dict[Risk, Handling] = Field(default_factory=dict)


class RedactionPolicy(_Strict):
    patterns: list[dict[str, str]] = Field(default_factory=list)


class Policy(_Strict):
    """The parsed guardrail configuration."""

    version: int = 1
    allowlist: Allowlist = Field(default_factory=Allowlist)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_POLICY_PATH) -> "Policy":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    # -- allowlist ---------------------------------------------------------

    def url_allowed(self, url: str) -> tuple[bool, str]:
        """Whether the automation may operate on ``url``.

        Returns ``(allowed, reason)``; the reason is written into the failure
        report, so it names the specific rule rather than saying "denied".
        """
        if not url:
            return False, "no URL to check against the allowlist"

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path or "/"

        if self.allowlist.origins and not any(
            fnmatch.fnmatch(origin, allowed) for allowed in self.allowlist.origins
        ):
            return False, f"origin {origin!r} is not on the allowlist"

        for denied in self.allowlist.denied_routes:
            if fnmatch.fnmatch(path, denied):
                return False, f"route {path!r} matches denied rule {denied!r}"

        if self.allowlist.routes and not any(
            fnmatch.fnmatch(path, allowed) for allowed in self.allowlist.routes
        ):
            return False, f"route {path!r} is not on the allowlist"

        return True, ""

    def action_allowed(self, kind: str) -> tuple[bool, str]:
        if self.allowlist.actions and kind not in self.allowlist.actions:
            return False, f"action kind {kind!r} is not permitted by policy"
        return True, ""

    # -- risk --------------------------------------------------------------

    def classify(self, path: str, action_kind: str, control: str | None) -> Risk:
        for rule in self.risk.rules:
            if rule.matches(path, action_kind, control):
                return rule.risk
        return self.risk.default

    def handling_for(self, risk: Risk, discovery: bool) -> Handling:
        table = self.risk.discovery_handling if discovery else self.risk.handling
        return table.get(risk, "require_human" if risk == "irreversible" else "allow")

    def redactor(self) -> Redactor:
        return Redactor.from_spec(self.redaction.patterns)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GateRequest:
    """Everything the gate needs to judge one action."""

    capability: Capability
    step: Step
    step_index: int
    current_url: str
    target_url: str | None = None
    """For navigations: where the action would go, which must also be allowed."""


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    reason: str = ""
    require_human: bool = False
    risk: Risk = "safe"


class PolicyGate:
    """Enforces a ``Policy``. Injected into the replay engine and agent loop."""

    def __init__(self, policy: Policy, discovery: bool = False) -> None:
        self.policy = policy
        self.discovery = discovery

    @classmethod
    def from_file(
        cls, path: Path | str = DEFAULT_POLICY_PATH, discovery: bool = False
    ) -> "PolicyGate":
        return cls(Policy.load(path), discovery=discovery)

    def check(self, request: GateRequest) -> GateDecision:
        kind = request.step.action.kind

        ok, reason = self.policy.action_allowed(kind)
        if not ok:
            return GateDecision(allowed=False, reason=reason)

        # A freshly started browser sits on about:blank, which is not a page
        # and has no meaningful origin. The first step of every flow is a
        # navigation from exactly that state, so treating it as an off-list
        # origin would deny every run at step 0.
        on_a_page = not _is_blank(request.current_url)

        # Where the action happens, and -- for a navigation -- where it lands.
        # Checking only the current page would let one navigate step walk the
        # session straight off the allowlist; checking only the destination
        # would miss a click on a page we should never have been on.
        to_check = [request.target_url] if request.target_url else []
        if on_a_page:
            to_check.append(request.current_url)
        elif not to_check:
            return GateDecision(
                allowed=False,
                reason=f"no page is loaded, so a {kind} action has no target",
            )

        for url in to_check:
            ok, reason = self.policy.url_allowed(url)
            if not ok:
                return GateDecision(allowed=False, reason=reason)

        # Classify against the page being acted on; for a navigation from a
        # blank start, that is the destination.
        basis = request.current_url if on_a_page else (request.target_url or "")
        path = urlsplit(basis).path or "/"
        control = control_name(request.step)
        policy_risk = self.policy.classify(path, kind, control)
        risk = _worse(policy_risk, request.step.risk)

        handling = self.policy.handling_for(risk, self.discovery)
        if handling == "block":
            return GateDecision(
                allowed=False,
                reason=f"{risk} actions are blocked by policy",
                risk=risk,
            )
        if handling == "require_human":
            return GateDecision(
                allowed=True,
                require_human=True,
                risk=risk,
                reason=(
                    f"{risk} action on {control or request.step.id!r} "
                    f"requires operator approval"
                ),
            )
        return GateDecision(allowed=True, risk=risk)


def control_name(step: Step) -> str | None:
    """The recorded name of the control a step activates, if it has one.

    Read from the target's strategies rather than from the live page: the
    policy must be decidable *before* the action happens, and a name taken
    from the page at action time would be attacker-influenced content on an
    untrusted surface. Falls back to the human description, which is what a
    reviewer reads anyway.
    """
    target: Target | None = getattr(step.action, "target", None)
    if target is None:
        return None
    for strategy in target.strategies:
        if isinstance(strategy, RoleNameStrategy):
            return strategy.name
        if isinstance(strategy, TextStrategy):
            return strategy.text
    return target.described_as


def _is_blank(url: str | None) -> bool:
    """Whether the surface is on a real page yet.

    A new browser context starts on ``about:blank``; some also use a
    ``chrome://`` start page. None of these are the application, and none have
    an origin the allowlist could sensibly judge.
    """
    if not url:
        return True
    return url.startswith(("about:", "chrome:", "edge:", "data:"))


def _worse(left: Risk, right: Risk) -> Risk:
    return left if RISK_ORDER[left] >= RISK_ORDER[right] else right


class AllowAllGate:
    """Permits everything. For tests that are not exercising policy.

    Kept explicit rather than allowing ``gate=None``: a run with no guardrail
    should be a visible choice at the call site.
    """

    def check(self, request: GateRequest) -> GateDecision:  # noqa: ARG002
        return GateDecision(allowed=True)


__all__ = [
    "AllowAllGate",
    "GateDecision",
    "GateRequest",
    "Handling",
    "Policy",
    "PolicyGate",
    "RiskRule",
    "control_name",
]
