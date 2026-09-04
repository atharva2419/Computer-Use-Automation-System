"""The goal-driven discovery loop (brief 3.1).

Observe, decide, act, until the goal is met or a stopping condition fires.
The model is the only part of the system that reasons about an unfamiliar UI;
everything it touches -- the surface, the guardrail, the control token, the
evidence sink -- is the same machinery replay uses, which is why this module
is small.

Stopping conditions, all of them enforced here rather than trusted to the
model: a step budget, a wall-clock budget, a no-progress detector, and the
model's own ``done`` / ``stuck``. An agent that cannot stop is an agent that
bills you until someone notices.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..evidence import FileEvidenceSink
from ..guardrails import GateRequest, PolicyGate
from ..recorder import Recorder, RecorderError, step_id_for
from ..redaction import Redactor
from ..replay import EscalationContext, EscalationHandler, NoOperatorAvailable
from ..schema.capability import (
    Capability,
    ClickAction,
    FillAction,
    NavigateAction,
    SelectAction,
    Step,
)
from ..schema.common import FrameRef, LiteralValue
from ..schema.targets import RowScopedCellStrategy, Target
from ..session import Actor, Session
from ..surface.base import Surface, SurfaceError
from . import prompts
from .tools import TOOLS

DEFAULT_MAX_STEPS = 25
DEFAULT_BUDGET_SECONDS = 300
DEFAULT_MAX_TOKENS = 2048


class DiscoveryError(RuntimeError):
    """Discovery could not produce a capability."""


@dataclass
class DiscoveryResult:
    run_id: str
    status: str  # "recorded" | "stuck" | "exhausted" | "failed"
    capability: Capability | None = None
    reason: str = ""
    steps_taken: int = 0
    model_turns: int = 0
    transcript: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    """One tool call, unpacked."""

    tool: str
    args: dict[str, Any]
    use_id: str


class DiscoveryAgent:
    """Drives a model around a live surface and records what worked."""

    def __init__(
        self,
        session: Session,
        client: Any,
        model: str,
        gate: PolicyGate,
        recorder: Recorder,
        sink: FileEvidenceSink | None = None,
        redactor: Redactor | None = None,
        escalation: EscalationHandler | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        budget_seconds: int = DEFAULT_BUDGET_SECONDS,
    ) -> None:
        self.session = session
        self.client = client
        self.model = model
        self.gate = gate
        self.recorder = recorder
        self.sink = sink
        self.redactor = redactor or Redactor()
        self.escalation = escalation or NoOperatorAvailable()
        self.max_steps = max_steps
        self.budget_seconds = budget_seconds

        self.run_id = uuid.uuid4().hex[:12]
        self._messages: list[dict[str, Any]] = []
        self._taken_ids: set[str] = set()
        self._steps_taken = 0
        self._turns = 0
        self._last_signature: str | None = None
        self._repeat_count = 0
        self._bound: dict[str, str] = {}
        self._pending: list[_Pending] = []

    # -- public ------------------------------------------------------------

    def discover(
        self, goal: str, entry_url: str, bound: dict[str, str]
    ) -> DiscoveryResult:
        self.session.claim(Actor.AGENT, reason=f"discovery {self.recorder.capability_id}")
        # Held for substitution only: a parameter reference is what gets
        # recorded, but a real value has to reach the browser.
        self._bound = dict(bound)

        # Learn the secrets here rather than trusting the caller to have wired
        # a redactor. The model is never *told* a credential -- it references
        # them by name -- but it can still be shown one: this console prints
        # the demo operator's password on its own sign-on page, and a real app
        # can echo a value back into a field. Every observation is read from
        # the screen, so the screen is the leak, and the defence belongs where
        # the observations are produced.
        self.redactor.learn_secrets(
            bound.get(spec.name) for spec in self.recorder.inputs if spec.secret
        )
        deadline = time.monotonic() + self.budget_seconds

        described_inputs = [
            f"{spec.name} ({spec.type})"
            + (" -- secret, reference by name only" if spec.secret else f" = {bound.get(spec.name, '')!r}")
            for spec in self.recorder.inputs
        ]
        self._messages.append(
            {
                "role": "user",
                "content": prompts.initial_message(goal, entry_url, described_inputs),
            }
        )

        while True:
            if self._steps_taken >= self.max_steps:
                return self._result("exhausted", f"step budget of {self.max_steps} reached")
            if time.monotonic() > deadline:
                return self._result("exhausted", f"time budget of {self.budget_seconds}s reached")

            try:
                blocks = self._ask_model()
            except Exception as exc:  # noqa: BLE001 - surfaced as a run outcome
                return self._result("failed", f"model call failed: {exc}")

            calls = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            if not calls:
                # No tool call: the model is talking rather than acting. Nudge
                # once, then treat it as a dead end rather than looping on prose.
                self._append_user("You must call exactly one tool. Take the next action.")
                self._turns += 1
                if self._turns > self.max_steps * 2:
                    return self._result("exhausted", "model stopped issuing tool calls")
                continue

            # The model may batch several tool calls into one turn. Only the
            # first is executed -- each action changes the screen, so the rest
            # were decided against a state that no longer exists -- but every
            # one of them still needs a tool_result, because the API rejects a
            # tool_use left unanswered.
            self._pending = [
                _Pending(block.name, dict(block.input or {}), block.id)
                for block in calls
            ]
            terminal = self._handle(self._pending[0], bound)
            if terminal is not None:
                return terminal

    # -- model turn --------------------------------------------------------

    def _ask_model(self) -> list[Any]:
        self._turns += 1
        observation = self._observe_text()
        self._append_user(
            prompts.observation_message(observation, self._steps_taken + 1, self.max_steps)
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=prompts.SYSTEM_PROMPT,
            tools=TOOLS,
            messages=self._messages,
        )
        self._messages.append({"role": "assistant", "content": response.content})
        return list(response.content)

    def _observe_text(self) -> str:
        """What the model is shown: the screen, minus credentials.

        Secrets are masked; business data is not. The agent has to reason
        about the record in front of it, and a model shown a redacted screen
        quotes the redaction markers back -- which is exactly how the first
        real run failed its own success check.
        """
        surface = self._surface()
        try:
            return self.redactor.secrets_only(surface.observe().render())
        except SurfaceError as exc:
            return f"(the surface could not be observed: {exc})"

    # -- dispatch ----------------------------------------------------------

    def _handle(self, call: _Pending, bound: dict[str, str]) -> DiscoveryResult | None:
        tool, args = call.tool, call.args
        self._note("model_action", tool=tool, args=self._loggable(args))

        if tool == "done":
            return self._finish(args)
        if tool == "stuck":
            return self._handle_stuck(call)
        if tool == "screenshot":
            self._respond(call, "Screenshot attached below.", image=True)
            return None
        if tool == "read":
            return self._handle_read(call)

        return self._handle_action(call, bound)

    def _handle_action(self, call: _Pending, bound: dict[str, str]) -> DiscoveryResult | None:
        tool, args = call.tool, call.args
        frame = _frame_ref(args.get("frame"))
        surface = self._surface()

        try:
            target, action = self._build(tool, args, frame, surface, bound)
        except (RecorderError, ValueError) as exc:
            self._respond(call, f"That action could not be formed: {exc}")
            return None

        step_id = step_id_for(args.get("intent", tool), self._steps_taken, self._taken_ids)
        probe = Step(id=step_id, intent=args.get("intent", tool), action=action)

        decision = self.gate.check(
            GateRequest(
                capability=self._probe_capability(probe),
                step=probe,
                step_index=self._steps_taken,
                current_url=self._current_url(),
                target_url=args.get("url") if tool == "navigate" else None,
            )
        )
        if not decision.allowed:
            # Fed back as a tool result rather than raised: a refusal is
            # information the model should adapt to, not a crash.
            self._note("policy_denied", tool=tool, reason=decision.reason)
            self._respond(call, f"Policy refused that action: {decision.reason}")
            return None

        if decision.require_human:
            outcome = self._ask_operator(probe, decision.reason)
            if not outcome:
                return self._result("stuck", f"operator declined: {decision.reason}")

        try:
            self._perform(tool, target, action, surface)
        except SurfaceError as exc:
            self._respond(call, f"That action failed: {exc}")
            return None

        if tool == "select" and target is not None:
            action = self._stabilise_select(action, target)

        self.recorder.record(
            surface=surface,
            step_id=step_id,
            intent=args.get("intent", tool),
            action=action,
            risk=decision.risk,
            expect_text=args.get("expect_text"),
            frame=frame,
        )
        self._steps_taken += 1
        # Answer the tool call before anything else is said. The API requires
        # a tool_result immediately after a tool_use, so any coaching -- the
        # no-progress nudge below -- has to come after it, not before.
        self._respond(call, "Done. The updated surface follows.")
        self._check_progress(tool, args)
        return None

    def _build(
        self,
        tool: str,
        args: dict[str, Any],
        frame: FrameRef,
        surface: Surface,
        bound: dict[str, str],
    ) -> tuple[Target | None, Any]:
        if tool == "navigate":
            url = args.get("url")
            if not url:
                raise ValueError("navigate needs a url")
            return None, NavigateAction(url=LiteralValue(value=url), frame=frame)

        described = args.get("intent", "the control")
        if tool == "click":
            target = self.recorder.build_target(
                surface,
                frame=frame,
                described_as=described,
                role=args.get("role"),
                accessible_name=args.get("name"),
                text=args.get("text"),
            )
            return target, ClickAction(target=target)

        if tool in ("fill", "select"):
            label = args.get("label")
            if not label:
                raise ValueError(f"{tool} needs a label")
            target = self.recorder.build_target(
                surface,
                frame=frame,
                described_as=described,
                label=label,
                control="combobox" if tool == "select" else "textbox",
            )
            value = self.recorder.value_ref(args.get("value"), args.get("param"))
            if tool == "fill":
                return target, FillAction(target=target, value=value)
            return target, SelectAction(target=target, value=value)

        raise ValueError(f"unsupported action tool {tool!r}")

    def _stabilise_select(self, action: Any, target: Target) -> Any:
        """Record the option the application identifies, not the one on screen.

        A dropdown's visible label is written for a person and often carries
        live data with it. This target lists shares as
        ``102777-MMKT-3 - Money Market ($5.00)`` -- the balance is part of the
        label. A capability that selects by that text is self-invalidating:
        posting the transfer changes the balance, so the next run cannot find
        the option it just used.

        The option's underlying value is the stable identifier, and the model
        cannot see it -- the accessibility tree exposes labels, not value
        attributes. So the value is read back off the control after the
        selection lands, which is the same rule the recorder applies to
        locators: what gets written down comes from the page, not from the
        model's description of it.

        A read-back value that matches a supplied argument is then promoted to
        a parameter reference by the ordinary exact-match rule, so the
        capability becomes properly reusable rather than pinned to one share.
        """
        if getattr(action.value, "kind", None) == "param":
            return action  # the model already referenced a parameter
        try:
            actual, _ = self._surface().read(target, source="value")
        except SurfaceError:
            return action  # best effort; keep the label rather than nothing
        if actual and actual != getattr(action.value, "value", None):
            action.value = self.recorder.value_ref(actual, None)
            self._note(
                "select_value_stabilised",
                step=target.described_as,
                recorded=actual,
            )
        return action

    def _perform(self, tool: str, target: Target | None, action: Any, surface: Surface) -> None:
        if tool == "navigate":
            surface.navigate(action.url.value, action.frame)
        elif tool == "click":
            assert target is not None
            surface.click(target)
        elif tool == "fill":
            assert target is not None
            surface.fill(target, self._concrete(action.value))
        elif tool == "select":
            assert target is not None
            surface.select(target, self._concrete(action.value))

    def _concrete(self, value: Any) -> str:
        """The literal text to type, resolving a parameter reference."""
        if getattr(value, "kind", None) == "param":
            return self._bound.get(value.param, "")
        return value.value

    # -- read / outputs ----------------------------------------------------

    def _handle_read(self, call: _Pending) -> DiscoveryResult | None:
        args = call.args
        frame = _frame_ref(args.get("frame"))
        target = Target(
            described_as=args.get("intent", args["output_name"]),
            frame=frame,
            strategies=[
                RowScopedCellStrategy(
                    row_contains=args["row_contains"],
                    cell_index=int(args["cell_index"]),
                )
            ],
            rationale=(
                f"Anchored on the row containing {args['row_contains']!r} rather "
                "than an absolute row index, so inserting or reordering rows "
                "does not read the wrong value."
            ),
        )
        try:
            value, _ = self._surface().read(target)
        except SurfaceError as exc:
            self._respond(call, f"Could not read that: {exc}")
            return None

        self.recorder.record_output(
            name=args["output_name"],
            type_=args.get("output_type", "string"),
            description=args.get("description", ""),
            target=target,
            after_step=self.recorder.steps[-1].id if self.recorder.steps else None,
            # What this reading actually said, so the recorder can tell at
            # emission whether a checkpoint quoted the answer instead of
            # proving the step. Audited, then dropped -- never recorded.
            observed=value,
        )
        self._note("output_declared", name=args["output_name"])
        # The value goes back to the model unredacted -- it needs to confirm it
        # read the right cell -- but the transcript is scrubbed before storage.
        self._respond(call, f"Read {value.strip()!r} and declared it as an output.")
        return None

    # -- terminal states ---------------------------------------------------

    def _finish(self, args: dict[str, Any]) -> DiscoveryResult:
        try:
            capability = self.recorder.finish(
                success_text=args["success_text"],
                success_frame=_frame_ref(args.get("frame")),
                surface=self._surface(),
                model=self.model,
                run_id=self.run_id,
                transcript=self._transcript(),
            )
        except RecorderError as exc:
            return self._result("failed", str(exc))
        return self._result("recorded", args.get("summary", ""), capability=capability)

    def _handle_stuck(self, call: _Pending) -> DiscoveryResult | None:
        """The model has given up. Offer it to a human before ending the run.

        Returning ``None`` means an operator resolved it and the loop should
        continue -- the same shape the action handlers use, so the caller has
        one rule: a result ends the run, ``None`` carries on.
        """
        reason = call.args.get("reason", "the model reported being stuck")
        self._note("model_stuck", reason=reason)
        if self.recorder.steps and self._ask_operator(self.recorder.steps[-1], reason):
            self._respond(
                call,
                "An operator took control, addressed the problem and handed the "
                "session back. Look at the surface again and continue.",
            )
            return None
        self._respond(call, "No operator was available. Ending the run.")
        return self._result("stuck", reason)

    def _ask_operator(self, step: Step, reason: str) -> bool:
        context = EscalationContext(
            run_id=self.run_id,
            capability_id=self.recorder.capability_id,
            goal=self.recorder.goal,
            step=step,
            step_index=self._steps_taken,
            reason=reason,
            signal_id=None,
            observed=self._current_url(),
            session=self.session,
            step_ids=[s.id for s in self.recorder.steps],
        )
        outcome = self.escalation.request(context)
        self._note(
            "intervention",
            reason=self.redactor.text(reason),
            resolution=outcome.resolution,
            operator=outcome.operator,
        )
        return outcome.resolved

    # -- progress and bookkeeping -----------------------------------------

    def _check_progress(self, tool: str, args: dict[str, Any]) -> None:
        """Detect the model doing the same thing over and over.

        A dead end usually shows up as an identical action repeated, not as an
        error -- the click lands, the page does not change, and the model tries
        again. Left alone that burns the whole budget quietly.
        """
        signature = json.dumps([tool, args.get("frame"), args.get("name"), args.get("label"), args.get("text")], sort_keys=True)
        if signature == self._last_signature:
            self._repeat_count += 1
        else:
            self._repeat_count = 0
            self._last_signature = signature
        if self._repeat_count >= 2:
            self._append_user(
                "That is the third time you have taken the same action and the "
                "surface has not changed. Try a different approach, or call "
                "`stuck` if there is none."
            )

    def _probe_capability(self, step: Step) -> Capability:
        """A minimal capability so the gate can judge a step mid-discovery."""
        from ..schema.capability import Checkpoint
        from ..schema.common import TextPresent

        return Capability(
            id=self.recorder.capability_id,
            name=self.recorder.name,
            description=self.recorder.goal,
            app=self.recorder.app,
            inputs=self.recorder.inputs,
            steps=[step],
            success=Checkpoint(
                description="placeholder",
                assertion=TextPresent(frame=FrameRef(), text=""),
            ),
        )

    def _respond(self, call: _Pending, text: str, image: bool = False) -> None:
        """Answer the executed call, and decline any others from the same turn.

        Every tool_use in an assistant message must be answered in the very
        next message, or the API rejects the conversation outright. So the
        results for a whole batch go out together: a real one for the action
        that ran, and a plain refusal for the ones that did not.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image:
            shot = self._surface().screenshot()
            if shot:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(shot).decode("ascii"),
                        },
                    }
                )

        results: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": call.use_id, "content": content}
        ]
        for other in self._pending:
            if other.use_id == call.use_id:
                continue
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": other.use_id,
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Not executed. One action per turn: the screen "
                                "changes after each one, so decide the next "
                                "action from the state below."
                            ),
                        }
                    ],
                }
            )
        self._pending = []
        self._messages.append({"role": "user", "content": results})

    def _append_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def _note(self, kind: str, **fields: Any) -> None:
        if self.sink is not None:
            self.sink.note(kind, **fields)

    def _loggable(self, args: dict[str, Any]) -> dict[str, Any]:
        return {k: self.redactor.text(str(v)) for k, v in args.items()}

    def _transcript(self) -> str:
        return self.redactor.text(json.dumps(self._messages, default=str, indent=2))

    def _surface(self) -> Surface:
        self.session.require(Actor.AGENT)
        return self.session.surface

    def _current_url(self) -> str:
        try:
            return self.session.surface.frame_url(FrameRef())
        except SurfaceError:
            return ""

    def _result(
        self, status: str, reason: str, capability: Capability | None = None
    ) -> DiscoveryResult:
        return DiscoveryResult(
            run_id=self.run_id,
            status=status,
            capability=capability,
            reason=reason,
            steps_taken=self._steps_taken,
            model_turns=self._turns,
            transcript=self._transcript(),
            notes=list(self.recorder.notes),
        )


def _frame_ref(name: str | None) -> FrameRef:
    """Turn the model's frame argument into a reference.

    Anything falsy or literally "top" means the outermost document; everything
    else is a named frame. Kept permissive because the model reads frame names
    out of an observation and will occasionally quote them with stray
    punctuation.
    """
    cleaned = (name or "").strip().strip("'\"")
    if not cleaned or cleaned.lower() == "top":
        return FrameRef(kind="top")
    return FrameRef(kind="name", value=cleaned)


def resolve_model(explicit: str | None = None) -> str:
    return explicit or os.environ.get("CUA_MODEL") or "claude-sonnet-5"
