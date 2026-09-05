"""Discovery loop and recorder tests, with a scripted model.

The model is faked here on purpose. What needs testing is everything *around*
the model -- that a trajectory becomes a valid artifact, that unverifiable
claims are discarded, that the guardrail still bites, that budgets stop the
loop -- and none of that should depend on a paid, non-deterministic call. The
one genuine LLM run lives in /evidence, where the brief asks for it.

The scripted model returns real tool calls against the real console, so the
surface, the recorder and the policy are all exercised for real.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from werkzeug.serving import make_server

from cua.agent.loop import DiscoveryAgent
from cua.agent.tools import TOOLS, tool_names
from cua.guardrails import Policy, PolicyGate
from cua.recorder import (
    Recorder,
    circular_anchor,
    load_signal_library,
    step_id_for,
)
from cua.replay import ReplayEngine
from cua.schema.capability import (
    AppBinding,
    Checkpoint,
    ClickAction,
    ParamSpec,
    Step,
    Target,
)
from cua.schema import FrameRef, RoleNameStrategy, TextPresent
from cua.session import Session
from cua.surface.web import PlaywrightWebSurface
from target_app import app as target_module
from target_app import data as fixture_data


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


@pytest.fixture(autouse=True)
def pristine() -> Iterator[None]:
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()
    yield
    target_module.chaos = target_module.Chaos()
    fixture_data.reset()


# ---------------------------------------------------------------------------
# A scripted stand-in for the model
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, name: str, payload: dict[str, Any], index: int) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = payload
        self.id = f"call_{index}"


class _Response:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ScriptedModel:
    """Replays a fixed list of tool calls, ignoring what it is told.

    Deliberately blind to the observation: these tests are about the harness,
    not about whether a model can navigate. A call that returns no more script
    yields prose instead, which exercises the no-tool-call branch.
    """

    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs: Any) -> _Response:
        self.calls += 1
        if not self.script:
            return _Response([type("T", (), {"type": "text", "text": "no idea"})()])
        name, payload = self.script.pop(0)
        return _Response([_Block(name, payload, self.calls)])


def _agent(
    base_url: str,
    script: list[tuple[str, dict[str, Any]]],
    *,
    capability_id: str = "t.discovered",
    max_steps: int = 25,
) -> tuple[DiscoveryAgent, Session, PlaywrightWebSurface, Recorder]:
    policy = Policy.load()
    policy.allowlist.origins = [base_url]

    inputs = [
        ParamSpec(name="member_id", type="string", example="10001", pattern=r"^\d{4,8}$"),
        ParamSpec(name="operator_id", type="string", example="op.demo"),
        ParamSpec(name="operator_passphrase", type="string", secret=True),
    ]
    recorder = Recorder(
        capability_id=capability_id,
        name="Discovered capability",
        goal="read the savings balance",
        app=AppBinding(
            product="meridian-core",
            product_version="r7.4.1",
            entry_url=f"{base_url}/login",
            surface="legacy_web",
        ),
        inputs=inputs,
        bound={
            "member_id": "10001",
            "operator_id": "op.demo",
            "operator_passphrase": "demo-pass",
        },
    )
    surface = PlaywrightWebSurface(headless=True).start()
    session = Session(surface=surface)
    agent = DiscoveryAgent(
        session=session,
        client=ScriptedModel(script),
        model="scripted-test-model",
        gate=PolicyGate(policy, discovery=True),
        recorder=recorder,
        max_steps=max_steps,
    )
    return agent, session, surface, recorder


BOUND = {
    "member_id": "10001",
    "operator_id": "op.demo",
    "operator_passphrase": "demo-pass",
}


def _happy_script(base_url: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("navigate", {"intent": "Open the sign-on page", "url": f"{base_url}/login",
                      "frame": "top", "expect_text": "OPERATOR SIGN ON"}),
        ("fill", {"intent": "Type the operator id", "frame": "top",
                  "label": "Operator ID", "value": "op.demo"}),
        ("fill", {"intent": "Type the passphrase", "frame": "top",
                  "label": "Passphrase", "param": "operator_passphrase"}),
        ("click", {"intent": "Submit sign on", "frame": "top", "role": "button",
                   "name": "Sign On", "expect_text": "WORKSPACE"}),
        ("click", {"intent": "Open member search", "frame": "nav",
                   "text": "Member Search", "expect_text": "MEMBER SEARCH"}),
        ("fill", {"intent": "Type the member id", "frame": "main",
                  "label": "Member ID", "value": "10001"}),
        ("click", {"intent": "Run the search", "frame": "main", "role": "button",
                   "name": "Search", "expect_text": "Open Record"}),
        ("click", {"intent": "Open the member record", "frame": "main", "role": "link",
                   "name": "Open Record", "expect_text": "MEMBER RECORD"}),
        ("read", {"intent": "Read the savings balance", "frame": "main",
                  "output_name": "savings_balance", "output_type": "money",
                  "description": "Current savings balance.",
                  "row_contains": "Savings", "cell_index": 2}),
        ("done", {"summary": "Read the balance", "success_text": "MEMBER RECORD",
                  "frame": "main"}),
    ]


# ---------------------------------------------------------------------------
# The loop end to end
# ---------------------------------------------------------------------------


def test_a_trajectory_becomes_a_replayable_artifact(base_url: str) -> None:
    """The whole point: what discovery emits, replay can execute."""
    agent, session, surface, _ = _agent(base_url, _happy_script(base_url))
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    assert result.status == "recorded", result.reason
    capability = result.capability
    assert capability is not None
    assert len(capability.steps) == 8
    assert [o.name for o in capability.outputs] == ["savings_balance"]

    # Recorded against a real product, so the curated error model comes along.
    assert {s.id for s in capability.signals} >= {"member_not_found", "session_expired"}

    # Untrusted until it reproduces.
    assert capability.approval == "draft"
    assert capability.provenance.recorded_by == "llm_discovery"
    assert capability.provenance.transcript_digest

    # And now the actual claim: replay it, with no model anywhere.
    policy = Policy.load()
    policy.allowlist.origins = [base_url]
    fixture_data.reset()
    surface2 = PlaywrightWebSurface(headless=True).start()
    try:
        outcome = ReplayEngine(
            Session(surface=surface2), gate=PolicyGate(policy)
        ).run(capability, BOUND)
    finally:
        surface2.close()

    assert outcome.status == "success", getattr(outcome, "error", None)
    assert outcome.outputs["savings_balance"] == 4210.55


def test_secrets_are_referenced_never_recorded(base_url: str) -> None:
    """The model names a credential; it never sees or stores the value."""
    agent, session, surface, _ = _agent(base_url, _happy_script(base_url))
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    capability = result.capability
    assert capability is not None
    serialised = capability.model_dump_json()
    assert "demo-pass" not in serialised
    assert "demo-pass" not in result.transcript

    passphrase_step = capability.step("type_the_passphrase")
    assert passphrase_step is not None
    assert passphrase_step.action.value.kind == "param"  # type: ignore[union-attr]
    assert passphrase_step.action.value.param == "operator_passphrase"  # type: ignore[union-attr]


def test_literals_matching_an_argument_become_parameters(base_url: str) -> None:
    """Record once with 10001, replay for anybody."""
    agent, session, surface, _ = _agent(base_url, _happy_script(base_url))
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    capability = result.capability
    assert capability is not None
    member_step = capability.step("type_the_member_id")
    assert member_step is not None
    assert member_step.action.value.kind == "param"  # type: ignore[union-attr]
    assert member_step.action.value.param == "member_id"  # type: ignore[union-attr]

    # The operator id was also supplied, so it is parameterised too.
    operator_step = capability.step("type_the_operator_id")
    assert operator_step is not None
    assert operator_step.action.value.param == "operator_id"  # type: ignore[union-attr]


def test_locators_are_synthesised_from_the_page_not_the_model(base_url: str) -> None:
    """The model said 'the field labelled Member ID' and nothing more.

    Everything else in the recorded target -- that role+name cannot address
    it, the ranked fallbacks, the structural selector naming a form field the
    model never saw -- was derived from the live accessibility tree.
    """
    agent, session, surface, _ = _agent(base_url, _happy_script(base_url))
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    capability = result.capability
    assert capability is not None
    step = capability.step("type_the_member_id")
    assert step is not None
    strategies = step.action.target.strategies  # type: ignore[union-attr]

    assert strategies[0].kind == "label_cell"
    assert len(strategies) > 1, "a single strategy is not a fallback chain"
    assert step.action.target.rationale  # type: ignore[union-attr]

    # The last-resort selector quotes an attribute read off the element.
    css = [s for s in strategies if s.kind == "css"]
    if css:
        assert "f1" in css[0].css, "structural fallback must come from the page"


def test_an_unverifiable_checkpoint_is_discarded(base_url: str) -> None:
    """A confident, wrong claim must not reach the artifact.

    The model asserts text that is nowhere on the resulting page. The step is
    still recorded -- the action worked -- but without a checkpoint, and the
    discard is reported rather than hidden.
    """
    script = _happy_script(base_url)
    script[0] = (
        "navigate",
        {"intent": "Open the sign-on page", "url": f"{base_url}/login", "frame": "top",
         "expect_text": "WELCOME TO THE PORTAL"},
    )
    agent, session, surface, recorder = _agent(base_url, script)
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    capability = result.capability
    assert capability is not None
    first = capability.steps[0]
    assert first.checkpoint is None, "an unverifiable claim must be dropped"
    assert any("discarded proposed checkpoint" in n for n in result.notes)


def test_a_false_success_claim_refuses_to_record(base_url: str) -> None:
    """If the goal cannot be shown to be met, nothing is saved."""
    script = _happy_script(base_url)
    script[-1] = (
        "done",
        {"summary": "all good", "success_text": "TRANSFER COMPLETE", "frame": "main"},
    )
    agent, session, surface, _ = _agent(base_url, script)
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    assert result.status == "failed"
    assert result.capability is None
    assert "does not hold" in result.reason


# ---------------------------------------------------------------------------
# Guardrails and stopping conditions
# ---------------------------------------------------------------------------


def test_policy_refusal_is_returned_to_the_model_not_raised(base_url: str) -> None:
    """A denial is information the model can act on, not a crash.

    It also must not be recorded: a step the policy refused never happened.
    """
    script = [
        ("navigate", {"intent": "Reach the fault injection endpoint",
                      "url": f"{base_url}/__chaos", "frame": "top"}),
        *_happy_script(base_url),
    ]
    agent, session, surface, _ = _agent(base_url, script)
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    assert result.status == "recorded", result.reason
    capability = result.capability
    assert capability is not None
    assert all("__chaos" not in str(s.action) for s in capability.steps)
    assert len(capability.steps) == 8, "the refused action must not be recorded"


def test_the_step_budget_stops_the_loop(base_url: str) -> None:
    """An agent that cannot stop bills you until somebody notices."""
    script = _happy_script(base_url)
    agent, session, surface, _ = _agent(base_url, script, max_steps=3)
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    assert result.status == "exhausted"
    assert result.steps_taken == 3
    assert "step budget" in result.reason


def test_a_model_that_stops_calling_tools_is_stopped(base_url: str) -> None:
    agent, session, surface, _ = _agent(base_url, [], max_steps=2)
    try:
        result = agent.discover("read the savings balance", f"{base_url}/login", BOUND)
    finally:
        surface.close()

    assert result.status == "exhausted"
    assert "tool calls" in result.reason


# ---------------------------------------------------------------------------
# Recorder units
# ---------------------------------------------------------------------------


def test_signal_library_loads_the_product_error_model() -> None:
    signals = load_signal_library("meridian-core")
    ids = {s.id for s in signals}
    assert {"member_not_found", "permission_denied", "system_notice",
            "session_expired", "app_error"} == ids

    business = [s for s in signals if s.classification == "business_outcome"]
    assert {s.outcome_code for s in business} == {"MEMBER_NOT_FOUND", "PERMISSION_DENIED"}

    recoverable = [s for s in signals if s.classification == "recoverable"]
    assert recoverable and all(s.recovery is not None for s in recoverable)


def test_unknown_product_yields_an_empty_error_model() -> None:
    """Weaker, but not invalid -- and the caller is told."""
    assert load_signal_library("no-such-product") == []


def test_step_ids_are_readable_and_unique() -> None:
    taken: set[str] = set()
    assert step_id_for("Type the member ID being looked up", 0, taken) == "type_the_member_id"
    assert step_id_for("Type the member ID again", 1, taken) == "type_the_member_id_2"
    assert step_id_for("", 7, taken) == "step_7"


def test_every_tool_maps_to_something_the_loop_handles() -> None:
    """The model must not be offered a tool the harness cannot service."""
    handled = {"navigate", "click", "fill", "select", "read", "screenshot", "done", "stuck"}
    assert tool_names() == handled
    for tool in TOOLS:
        assert tool["description"], f"{tool['name']} needs a description"
        assert "intent" in tool["input_schema"]["properties"] or tool["name"] in (
            "screenshot", "done", "stuck",
        ), f"{tool['name']} should ask for an intent"


# --- invocation-specific checkpoints ----------------------------------------
#
# The third checkpoint test, and the one no pattern can catch. A checkpoint
# that quotes the *answer* -- the member's name, the balance that was read --
# passes its own recording and then works for exactly one set of arguments.


def _bare_recorder(bound: dict[str, str] | None = None) -> Recorder:
    return Recorder(
        capability_id="t.cap",
        name="t",
        goal="g",
        app=AppBinding(product="t", entry_url="http://localhost/"),
        inputs=[
            ParamSpec(name="member_id", type="string", example="102777"),
            ParamSpec(name="operator_password", type="string", secret=True),
        ],
        bound=bound or {"member_id": "102777", "operator_password": "hunter2"},
    )


def _step_with(text: str, step_id: str = "s1") -> Step:
    return Step(
        id=step_id,
        intent="do a thing",
        action=ClickAction(
            target=Target(
                described_as="a button",
                frame=FrameRef(),
                strategies=[RoleNameStrategy(role="button", name="Go")],
            )
        ),
        checkpoint=Checkpoint(
            description=f"{text!r} is present.",
            assertion=TextPresent(frame=FrameRef(), text=text),
        ),
    )


def test_drops_checkpoint_quoting_a_read_output():
    """The name read off the record cannot also prove the record opened."""
    recorder = _bare_recorder()
    recorder._observed_outputs.append("Johnson, Katherine")
    recorder.steps.append(_step_with("Johnson, Katherine"))

    recorder._drop_invocation_specific_checkpoints()

    assert recorder.steps[0].checkpoint is None
    assert "Johnson, Katherine" in recorder.notes[0]
    assert "one member" in recorder.notes[0]


def test_drops_checkpoint_quoting_a_fragment_of_an_output():
    """Quoting half the cell pins the capability just as firmly."""
    recorder = _bare_recorder()
    recorder._observed_outputs.append("Johnson, Katherine")
    recorder.steps.append(_step_with("Johnson"))

    recorder._drop_invocation_specific_checkpoints()
    assert recorder.steps[0].checkpoint is None


def test_drops_checkpoint_quoting_an_argument():
    """The member number supplied for the recording is equally invocation-specific."""
    recorder = _bare_recorder()
    recorder.steps.append(_step_with("Member 102777"))

    recorder._drop_invocation_specific_checkpoints()
    assert recorder.steps[0].checkpoint is None


def test_keeps_structural_checkpoint():
    """A heading is true for every member, so it survives."""
    recorder = _bare_recorder()
    recorder._observed_outputs.append("Johnson, Katherine")
    recorder.steps.append(_step_with("MEMBER RECORD"))

    recorder._drop_invocation_specific_checkpoints()

    assert recorder.steps[0].checkpoint is not None
    assert recorder.notes == []


def test_secret_arguments_are_never_compared_against():
    """A credential is excluded from the audit, not matched against it."""
    recorder = _bare_recorder()
    assert "hunter2" not in recorder._invocation_values()


# --- circular locators ------------------------------------------------------
#
# The same mistake as a checkpoint quoting its own result, one layer down: a
# row located by the value it is meant to read resolves during the recording
# and finds nothing for any other record.


def test_anchor_equal_to_the_reading_is_circular():
    assert circular_anchor("Johnson, Katherine", "Johnson, Katherine")


def test_anchor_contained_in_the_reading_is_circular():
    """Half the cell pins the locator just as firmly as all of it."""
    assert circular_anchor("Johnson", "Johnson, Katherine")


def test_label_anchor_is_not_circular():
    """The correct shape: find the row by its label, read the cell beside it."""
    assert not circular_anchor("Regular Shares", "102777-S0001")
    assert not circular_anchor("Member No.:", "Johnson, Katherine")
    assert not circular_anchor("Regular Shares", "$41,980.00")


def test_short_values_are_left_alone():
    """A two-character coincidence is likelier than a real quotation."""
    assert not circular_anchor("No", "No")
