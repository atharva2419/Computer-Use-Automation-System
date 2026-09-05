"""Chatbot tests (brief 3.3).

The planner is scripted here rather than mocked at the HTTP layer. What needs
testing is the boundary around the model -- that a credential never reaches
it, that a refusal is reported rather than swallowed, that a summary is read
off the run instead of invented -- and none of that should depend on a paid,
non-deterministic call.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from cua.schema.capability import Capability

from service.catalog import CatalogEntry
from service.chatbot import (
    ClaudePlanner,
    Plan,
    ScriptedPlanner,
    operator_credentials,
    server_supplied,
    summarise,
    tool_definitions,
)


@pytest.fixture()
def entry() -> CatalogEntry:
    """The recorded balance capability, as the service loads it."""
    path = "artifacts/meridian_hosted.member.read_balance@v1.json"
    with open(path, encoding="utf-8") as handle:
        return CatalogEntry(capability=Capability.model_validate_json(handle.read()))


class _Run:
    """Just enough of a Run for the summariser."""

    def __init__(self, **kw: Any) -> None:
        self.status = kw.get("status", "success")
        self.outputs = kw.get("outputs", {})
        self.outcome_code = kw.get("outcome_code")
        self.message = kw.get("message", "")
        self.error = kw.get("error")
        self.intervention = kw.get("intervention")


# --- what the model is allowed to see ---------------------------------------


def test_credentials_are_withheld_from_the_model(entry: CatalogEntry):
    """A model cannot leak a field it was never told exists."""
    withheld = server_supplied(entry.capability)
    assert "operator_password" in withheld
    assert "operator_id" in withheld

    schema = tool_definitions([entry])[0]["input_schema"]
    assert "operator_password" not in schema["properties"]
    assert "operator_id" not in schema["properties"]
    assert "operator_password" not in schema["required"]
    assert "member_id" in schema["properties"]


def test_tool_description_publishes_the_outcome_codes(entry: CatalogEntry):
    """So the caller can be told what "not found" looks like before it happens."""
    description = tool_definitions([entry])[0]["description"]
    assert "MEMBER_NOT_FOUND" in description


def test_credentials_come_from_the_environment(entry: CatalogEntry, monkeypatch):
    monkeypatch.setenv("CUA_OPERATOR_ID", "teller1")
    monkeypatch.setenv("CUA_OPERATOR_PASSWORD", "swordfish")
    filled = operator_credentials(entry.capability)
    assert filled == {"operator_id": "teller1", "operator_password": "swordfish"}


def test_no_credentials_when_the_environment_is_empty(entry: CatalogEntry, monkeypatch):
    """Missing config must not become an empty password quietly sent to the host."""
    monkeypatch.delenv("CUA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("CUA_OPERATOR_PASSWORD", raising=False)
    assert operator_credentials(entry.capability) == {}


# --- planning ----------------------------------------------------------------


def test_scripted_planner_extracts_the_member(entry: CatalogEntry):
    plan = ScriptedPlanner().plan("what is the balance for member 102777", [entry])
    assert plan.capability == "meridian_hosted.member.read_balance"
    assert plan.arguments == {"member_id": "102777"}


def test_scripted_planner_asks_rather_than_inventing(entry: CatalogEntry):
    """A missing member number is a question, not a guess."""
    plan = ScriptedPlanner().plan("what is the balance", [entry])
    assert not plan.invokes
    assert "member_id" in plan.reply


def test_scripted_planner_declines_what_it_does_not_know(entry: CatalogEntry):
    plan = ScriptedPlanner().plan("make me a sandwich", [entry])
    assert not plan.invokes
    assert plan.capability is None


def test_claude_planner_returns_the_tool_call(entry: CatalogEntry):
    """One call, one decision -- no agentic loop back into the decision path."""

    class _Block:
        type = "tool_use"
        name = "meridian_hosted__member__read_balance"
        input = {"member_id": "102777"}

    class _Client:
        def __init__(self) -> None:
            self.messages = self
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return type("R", (), {"content": [_Block()]})()

    client = _Client()
    plan = ClaudePlanner(client=client).plan("balance for 102777", [entry])

    assert plan.capability == "meridian_hosted.member.read_balance"
    assert plan.arguments == {"member_id": "102777"}
    assert len(client.calls) == 1

    # The credential must not appear anywhere in what was sent.
    sent = str(client.calls[0])
    assert "operator_password" not in sent


# --- reporting ---------------------------------------------------------------


def test_summary_reads_the_outputs_it_was_given(entry: CatalogEntry):
    run = _Run(
        status="success",
        outputs={"member_name": "Doe, Jane", "regular_shares_balance": 1234.5},
    )
    summary = summarise(run, entry)
    assert "Doe, Jane" in summary
    assert "1,234.50" in summary  # money formatted from the declared output type


def test_business_outcome_is_reported_as_an_answer_not_a_failure():
    run = _Run(
        status="business_outcome",
        message="No member record matches the supplied identifier.",
        outcome_code="MEMBER_NOT_FOUND",
    )
    summary = summarise(run)
    assert "MEMBER_NOT_FOUND" in summary
    assert "did not complete" not in summary


def test_failure_says_where_it_stopped():
    run = _Run(
        status="failed",
        error={"intent": "Open member record", "expected": "a record", "observed": "nothing"},
    )
    summary = summarise(run)
    assert "Open member record" in summary


def test_escalation_is_reported_with_its_reason():
    run = _Run(
        status="awaiting_operator",
        intervention={"reason": "The servicing session ended mid-flow."},
    )
    summary = summarise(run)
    assert "handed to a human" in summary
    assert "session ended mid-flow" in summary


def test_summary_invents_nothing():
    """The reporting path has no model in it, so it cannot produce a number.

    A run that returned no outputs must not be described as though it did --
    this is the whole reason the reply is templated rather than generated.
    """
    assert summarise(_Run(status="success", outputs={})) == "Done."
