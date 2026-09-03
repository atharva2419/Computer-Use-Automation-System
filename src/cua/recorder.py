"""Turning a successful discovery run into a capability artifact (brief 3.2).

The division of labour here is the load-bearing decision of the whole
discovery path:

    The model finds the path. The recorder writes it down.

A model asked to emit artifact JSON directly would produce plausible-looking
locators and checkpoints -- text that reads correctly and may not correspond to
anything on the page. Instead the model emits one action at a time in the same
vocabulary the artifact uses, and this module derives the durable parts from
what was *actually observed* when that action succeeded:

*   **Locators are synthesised from the live accessibility tree**, not
    generated. If the model said "the field labelled Member ID" and the tree
    shows that textbox has no accessible name, the recorder knows ``role_name``
    cannot address it and ranks ``label_cell`` first. It then reads the
    element's own identifying attribute for a last-resort fallback. The model
    never sees ``name=f1`` and could not have invented it.
*   **Values become parameters by exact match.** Discovery is invoked with
    typed arguments, so a literal the model typed that equals a supplied
    argument is promoted to a parameter reference. No guessing about which
    ``10001`` mattered.
*   **Checkpoints are proposed by the model and verified here.** A proposal
    that does not actually hold on the page it describes is discarded rather
    than written down. Nothing unverified reaches the artifact.
*   **Signals come from the product's signal library**, because a happy-path
    run cannot observe an error it never triggered.

Everything the model asserts that can be checked, is checked.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .schema.capability import (
    Action,
    AppBinding,
    Capability,
    Checkpoint,
    Extraction,
    OutputSpec,
    ParamSpec,
    Provenance,
    SignalRule,
    Step,
    Transform,
)
from .schema.common import (
    Assertion,
    FrameRef,
    LiteralValue,
    ParamValue,
    Risk,
    TextPresent,
    ValueRef,
)
from .schema.targets import (
    CssStrategy,
    LabelCellStrategy,
    RoleNameStrategy,
    Target,
    TargetStrategy,
    TextStrategy,
)
from .surface.base import Surface, SurfaceError

SIGNAL_LIBRARY_DIR = Path(__file__).resolve().parents[2] / "config" / "signals"

# Attributes worth reading for a last-resort selector, most stable first.
_FALLBACK_ATTRIBUTES = ("data-testid", "id", "name")


# Text that will not mean the same thing on the next run. A checkpoint has to
# survive the data moving underneath it, so these are refused rather than
# recorded -- see Recorder._verified_checkpoint.
_VOLATILE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("a currency amount", re.compile(r"[$£€]\s*\d")),
    ("a decimal figure", re.compile(r"\b\d+\.\d{2}\b")),
    ("a date", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")),
    ("a time", re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")),
    ("a reference number", re.compile(r"\b[A-Z]{2,4}\d{5,}\b")),
]


def _volatile_reason(text: str) -> str | None:
    """Why this text is unsafe as a checkpoint, or None if it is stable."""
    for reason, pattern in _VOLATILE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


class RecorderError(RuntimeError):
    """The trajectory cannot be turned into a valid capability."""


def load_signal_library(product: str, directory: Path | None = None) -> list[SignalRule]:
    """Load the curated runtime conditions for a vendor product.

    Missing library is not an error: a capability with no declared signals is
    weaker, not invalid, and refusing to record one would be worse than
    recording it with an empty error model and saying so.
    """
    path = (directory or SIGNAL_LIBRARY_DIR) / f"{product}.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [SignalRule.model_validate(entry) for entry in raw.get("signals", [])]


class Recorder:
    """Accumulates observed actions, then emits a capability."""

    def __init__(
        self,
        capability_id: str,
        name: str,
        goal: str,
        app: AppBinding,
        inputs: list[ParamSpec],
        bound: dict[str, str],
    ) -> None:
        self.capability_id = capability_id
        self.name = name
        self.goal = goal
        self.app = app
        self.inputs = inputs
        # Reversed map for promoting literals back into parameter references.
        # Secrets are excluded on purpose: their values must never be compared
        # against, echoed, or written, and they are always supplied by name.
        self._by_value = {
            value: spec.name
            for spec in inputs
            if not spec.secret
            for value in [bound.get(spec.name)]
            if value
        }
        self.steps: list[Step] = []
        self.outputs: list[OutputSpec] = []
        self.notes: list[str] = []

    # -- recording ---------------------------------------------------------

    def record(
        self,
        *,
        surface: Surface,
        step_id: str,
        intent: str,
        action: Action,
        risk: Risk,
        expect_text: str | None,
        frame: FrameRef,
    ) -> Step:
        """Append one completed action, with a verified checkpoint if possible."""
        checkpoint = self._verified_checkpoint(surface, expect_text, frame, step_id)
        step = Step(
            id=step_id,
            intent=intent,
            action=action,
            risk=risk,
            checkpoint=checkpoint,
        )
        self.steps.append(step)
        return step

    def record_output(
        self,
        *,
        name: str,
        type_: str,
        description: str,
        target: Target,
        after_step: str | None,
        sensitivity: str = "restricted",
    ) -> None:
        transform = Transform(kind="money" if type_ == "money" else "strip")
        self.outputs.append(
            OutputSpec(
                name=name,
                type=type_,  # type: ignore[arg-type]
                description=description,
                extract=Extraction(target=target, source="text", transform=transform),
                required=True,
                after_step=after_step,
                sensitivity=sensitivity,  # type: ignore[arg-type]
            )
        )

    # -- value binding -----------------------------------------------------

    def value_ref(self, literal: str | None, param: str | None) -> ValueRef:
        """A recorded value: a parameter reference wherever one is justified.

        An explicit parameter always wins -- that is how secrets are supplied,
        since the model is never shown their values. Otherwise a literal that
        exactly equals a supplied argument is promoted, because the caller
        clearly intends to vary it.
        """
        if param:
            return ParamValue(param=param)
        text = literal or ""
        promoted = self._by_value.get(text)
        if promoted:
            return ParamValue(param=promoted)
        return LiteralValue(value=text)

    # -- locator synthesis -------------------------------------------------

    def build_target(
        self,
        surface: Surface,
        *,
        frame: FrameRef,
        described_as: str,
        role: str | None = None,
        accessible_name: str | None = None,
        label: str | None = None,
        text: str | None = None,
        control: str = "textbox",
    ) -> Target:
        """Rank the ways this control could be addressed, most durable first.

        The model supplies one way of describing the control. This adds the
        alternatives, ordered by how likely each is to survive a re-theme or a
        version bump, and records why.
        """
        strategies: list[TargetStrategy] = []
        rationale: list[str] = []

        if role and accessible_name:
            strategies.append(
                RoleNameStrategy(role=role, name=accessible_name, exact=True)
            )
            rationale.append(
                f"Control exposes role {role!r} with accessible name "
                f"{accessible_name!r}; that pair tracks what the control means "
                "rather than how it is built, so it leads."
            )
            strategies.append(TextStrategy(text=accessible_name, exact=True))
            rationale.append(
                "Visible text is kept as a fallback in case the role changes "
                "but the wording does not."
            )
        elif label:
            strategies.append(
                LabelCellStrategy(label=label, control=control, scope="row")
            )
            rationale.append(
                f"The control has no accessible name, so role+name cannot "
                f"address it at all. It is identified the way an operator does: "
                f"by the visible label {label!r} in the same table row."
            )
            strategies.append(
                LabelCellStrategy(label=label, control=control, scope="following_cell")
            )
            rationale.append(
                "A following-cell variant covers layouts where the label and "
                "control are siblings rather than row-mates."
            )
        elif text:
            strategies.append(TextStrategy(text=text, exact=True))
            rationale.append(
                f"Non-semantic control with no interactive role; {text!r} is "
                "the only thing that identifies it, as it is for a human."
            )
        else:
            raise RecorderError(f"no way to identify {described_as!r}")

        target = Target(
            described_as=described_as,
            frame=frame,
            strategies=strategies,
            rationale=" ".join(rationale),
        )

        attribute = self._identifying_attribute(surface, target)
        if attribute:
            key, value = attribute
            target.strategies.append(CssStrategy(css=f"[{key}={value}]"))
            target.rationale += (
                f" A structural selector on {key}={value!r} is appended last: "
                "brittle by nature, but on surfaces this hostile it is "
                "sometimes the only thing left, and replay records when it is "
                "the strategy that had to be used."
            )
        return target

    def _identifying_attribute(
        self, surface: Surface, target: Target
    ) -> tuple[str, str] | None:
        """Read the element's own identifier, for a last-resort selector.

        Read from the page rather than guessed. Best effort: a surface that
        exposes no such attribute simply contributes no fallback.
        """
        for key in _FALLBACK_ATTRIBUTES:
            try:
                value, _ = surface.read(target, source="attribute", attribute=key)
            except (SurfaceError, ValueError):
                continue
            if value:
                return key, value
        return None

    # -- checkpoints -------------------------------------------------------

    def _verified_checkpoint(
        self,
        surface: Surface,
        expect_text: str | None,
        frame: FrameRef,
        step_id: str,
    ) -> Checkpoint | None:
        """Accept the model's proposal only if it holds now *and* will later.

        Two separate tests, and the second one was learned the hard way.

        **True now.** The guard against a confident, wrong assertion being
        written into an artifact that then fails in production for a reason
        nobody can trace back to a sentence a model wrote.

        **Still true later.** A checkpoint that quotes live data passes its
        own recording and then rots. On a target whose dropdowns read
        ``102777-MMKT-3 - Money Market ($5.00)``, a model asked what proves the
        selection worked will naturally quote the option -- balance and all.
        The capability then invalidates itself: posting the transfer changes
        the balance, so the next run cannot match its own checkpoint. The
        take-home target had static demo data, so this never surfaced; a live
        one with moving balances exposed it immediately.

        Volatile text is therefore refused outright rather than checked, and
        the refusal is reported. Amounts, dates and reference numbers are
        *outputs to read*, never evidence that a step worked.
        """
        if not expect_text:
            return None

        volatile = _volatile_reason(expect_text)
        if volatile:
            self.notes.append(
                f"step {step_id}: discarded proposed checkpoint "
                f"{expect_text!r} -- contains {volatile}, which changes "
                "between runs; a checkpoint must stay true, not just be true"
            )
            return None

        from . import assertions  # local import: keeps the schema layer clean

        assertion: Assertion = TextPresent(frame=frame, text=expect_text)
        result = assertions.evaluate(surface, assertion)
        if not result.ok:
            self.notes.append(
                f"step {step_id}: discarded proposed checkpoint "
                f"{expect_text!r} -- not present on the resulting page"
            )
            return None
        return Checkpoint(
            description=f"{expect_text!r} is present after {step_id}.",
            assertion=assertion,
        )

    # -- emission ----------------------------------------------------------

    def finish(
        self,
        *,
        success_text: str,
        success_frame: FrameRef,
        surface: Surface,
        model: str,
        run_id: str,
        transcript: str,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> Capability:
        if not self.steps:
            raise RecorderError("no steps were recorded")

        from . import assertions

        success_assertion: Assertion = TextPresent(
            frame=success_frame, text=success_text
        )
        if not assertions.evaluate(surface, success_assertion).ok:
            raise RecorderError(
                f"the declared success condition {success_text!r} does not hold "
                "on the final screen; refusing to record a capability that "
                "claims an outcome it cannot verify"
            )

        signals = load_signal_library(self.app.product)
        if not signals:
            self.notes.append(
                f"no signal library for product {self.app.product!r}: recorded "
                "with an empty error model"
            )

        return Capability(
            id=self.capability_id,
            version=1,
            name=self.name,
            description=description or self.goal,
            app=self.app,
            inputs=self.inputs,
            outputs=self.outputs,
            steps=self.steps,
            signals=signals,
            success=Checkpoint(
                description=f"Goal reached: {success_text!r} is on screen.",
                assertion=success_assertion,
            ),
            # Draft until a verification replay proves it reproduces. Nothing
            # a model produced is trusted for unattended use on its own say-so.
            approval="draft",
            provenance=Provenance(
                discovered_at=datetime.now(timezone.utc),
                model=model,
                discovery_run_id=run_id,
                transcript_digest=hashlib.sha256(
                    transcript.encode("utf-8")
                ).hexdigest(),
                recorded_by="llm_discovery",
            ),
            tags=tags or [],
        )


def step_id_for(intent: str, index: int, taken: set[str]) -> str:
    """A readable, unique step id derived from the model's stated intent."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in intent).split()][:4]
    base = "_".join(w.lower() for w in words) or f"step_{index}"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken.add(candidate)
    return candidate


__all__ = [
    "Recorder",
    "RecorderError",
    "load_signal_library",
    "step_id_for",
]
