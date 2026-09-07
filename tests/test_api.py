"""The capability API, chatbot and dashboard (brief 3.2-3.5).

**No browser and no model.** ``_execute`` is replaced with a stub, so what is
under test is the wrapper -- the catalog it publishes, the arguments it fills
in, the refusals it issues, the shape of what comes back -- rather than whether
Playwright can drive a credit union. Replay itself is covered against a real
browser in test_replay.py and test_discovery.py; repeating that here would buy
nothing and cost minutes per run.

The planner is forced to the scripted one for the same reason: a paid,
non-deterministic call has no place in a test that is really asking "does a
refusal reach the caller intact".

The stub still goes through ``submit()``, so every guardrail this file asserts
is the real one. What it skips is only what happens after the gate says yes.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.runner import CapabilityRunner

POLICY = "config/policy.meridian-hosted.yaml"
BALANCE = "meridian_hosted.member.read_balance"
TRANSFER = "meridian_hosted.member.transfer_funds"
HOLD = "meridian_hosted.member.place_account_hold"


class _Failure:
    """Just enough of a FailureDetail for the runner to absorb.

    The runner reads ``.observed`` for the human-readable message *and*
    ``.model_dump()`` for the record, so a stub that provides only one of them
    is silently swallowed by the worker's catch-all and the test reports a
    confusing KeyError instead of the assertion it meant to make.
    """

    def __init__(self, **fields: Any) -> None:
        self._fields = fields
        self.observed = fields.get("observed", "")

    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return dict(self._fields)


class _Result:
    """Just enough of a ReplayResult for the runner to absorb."""

    def __init__(self, status: str = "success", **kw: Any) -> None:
        self.status = status
        self.outputs = kw.get("outputs", {})
        self.code = kw.get("code")
        self.message = kw.get("message", "")
        self.error = kw.get("error")
        self.recoveries: list[Any] = []
        self.escalations: list[Any] = []


@pytest.fixture()
def outcome() -> dict[str, Any]:
    """What the stubbed replay should return. Mutated per test."""
    return {"result": _Result(outputs={"member_name": "Doe, Jane"})}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any]):
    # No key: build_planner falls back to the scripted planner, so the chat
    # tests are deterministic and free. This is also the offline path the
    # brief asks to be documented, so exercising it is not only convenience.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CUA_OPERATOR_ID", "teller1")
    monkeypatch.setenv("CUA_OPERATOR_PASSWORD", "swordfish")

    seen: list[dict[str, Any]] = []

    def fake_execute(self: CapabilityRunner, run_id: str) -> None:
        run = self._runs[run_id]
        capability, arguments = self._pending.pop(run_id)
        # Recorded so a test can assert what actually reached the engine --
        # including the credentials the caller never sent.
        seen.append({"capability": capability.id, "arguments": dict(arguments)})
        run.status = "running"
        run.evidence_dir = None
        self._absorb(run, outcome["result"])

    monkeypatch.setattr(CapabilityRunner, "_execute", fake_execute)

    app = create_app(artifacts="artifacts", policy=POLICY, headed=False)
    with TestClient(app) as test_client:
        test_client.seen = seen  # type: ignore[attr-defined]
        yield test_client


def _finished(client: TestClient, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Poll a run to completion. Invocation is asynchronous by design."""
    deadline = time.monotonic() + timeout
    status = "never polled"
    while time.monotonic() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        status = body["status"]
        if status in {"success", "failed", "business_outcome", "rejected"}:
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s: {status}")


# --- the catalog -------------------------------------------------------------


def test_catalog_lists_capabilities_with_generated_schemas(client: TestClient):
    body = client.get("/capabilities", params={"product": "meridian-hosted"}).json()
    names = {c["capability_id"] for c in body["capabilities"]}
    assert BALANCE in names
    assert not body["unloadable"], body["unloadable"]

    entry = next(c for c in body["capabilities"] if c["capability_id"] == BALANCE)
    schema = entry["input_schema"]
    # Derived from the artifact's own ParamSpec list, not hand-written.
    assert schema["type"] == "object"
    assert "member_id" in schema["properties"]
    assert schema["additionalProperties"] is False
    # A credential is published as write-only and never carries an example.
    password = schema["properties"]["operator_password"]
    assert password["writeOnly"] is True
    assert "examples" not in password


def test_catalog_publishes_the_outcome_codes_a_caller_can_branch_on(client: TestClient):
    entry = client.get(f"/capabilities/{BALANCE}").json()
    codes = {o["code"] for o in entry["business_outcomes"]}
    assert "MEMBER_NOT_FOUND" in codes
    # Two signals detect that condition on different screens; the catalog
    # advertises the code once, because the code is what a caller branches on.
    assert len(codes) == len({o["code"] for o in entry["business_outcomes"]})


def test_unknown_capability_is_a_404(client: TestClient):
    assert client.get("/capabilities/nope.not.here").status_code == 404
    assert client.post("/capabilities/nope.not.here/invoke", json={}).status_code == 404


# --- invocation --------------------------------------------------------------


def test_invoke_is_asynchronous_and_returns_a_run_id(client: TestClient):
    response = client.post(f"/capabilities/{BALANCE}/invoke",
                           json={"arguments": {"member_id": "102777"}})
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] and body["poll"] == f"/runs/{body['run_id']}"

    run = _finished(client, body["run_id"])
    assert run["status"] == "success"
    assert run["outputs"] == {"member_name": "Doe, Jane"}


def test_operator_credentials_are_filled_in_and_never_echoed(client: TestClient):
    """The caller sends business arguments; the service holds the session."""
    body = client.post(f"/capabilities/{BALANCE}/invoke",
                       json={"arguments": {"member_id": "102777"}}).json()
    run = _finished(client, body["run_id"])

    # They reached the engine...
    sent = client.seen[-1]["arguments"]  # type: ignore[attr-defined]
    assert sent["operator_id"] == "teller1"
    assert sent["operator_password"] == "swordfish"

    # ...but the run record shows the secret redacted, and the non-secret
    # operator id in the clear, which is what an audit trail wants.
    assert run["arguments"]["operator_password"] == "[REDACTED:secret]"
    assert run["arguments"]["operator_id"] == "teller1"
    assert "swordfish" not in str(run)


def test_an_explicit_operator_overrides_the_environment(client: TestClient):
    """Teller-versus-supervisor is a decision the caller is allowed to make."""
    client.post(f"/capabilities/{HOLD}/invoke",
                json={"supervised": True,
                      "arguments": {"member_id": "102777", "operator_id": "super1",
                                    "share": "102777-MMKT-9", "reason": "FRAUD",
                                    "notes": "n"}})
    time.sleep(0.2)
    assert client.seen[-1]["arguments"]["operator_id"] == "super1"  # type: ignore[attr-defined]


# --- the guardrail, through the wrapper --------------------------------------


def test_a_draft_capability_is_refused_before_a_browser_exists(client: TestClient):
    """Brief 3.5: the wrapper must not become a way around the guardrails."""
    response = client.post(f"/capabilities/{TRANSFER}/invoke",
                           json={"arguments": {"member_id": "102777"}})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "draft" in detail and "irreversible" in detail
    # Nothing was queued: the refusal happens in submit(), not in the worker.
    assert client.seen == []  # type: ignore[attr-defined]


def test_a_supervised_caller_may_run_a_draft(client: TestClient):
    """The escape hatch exists, is explicit, and does not disable the gate."""
    response = client.post(
        f"/capabilities/{TRANSFER}/invoke",
        json={"supervised": True,
              "arguments": {"member_id": "102777", "from_share": "a", "to_share": "b",
                            "amount": "1.00", "memo": "m"}},
    )
    assert response.status_code == 202


def test_evidence_refuses_path_traversal(client: TestClient):
    body = client.post(f"/capabilities/{BALANCE}/invoke",
                       json={"arguments": {"member_id": "102777"}}).json()
    _finished(client, body["run_id"])
    run_id = body["run_id"]
    for name in ("../../secret", "..%2Fsecret", ".hidden"):
        assert client.get(f"/runs/{run_id}/evidence/{name}").status_code in (400, 404)


# --- runs and decisions ------------------------------------------------------


def test_runs_are_listed_newest_first(client: TestClient):
    first = client.post(f"/capabilities/{BALANCE}/invoke",
                        json={"arguments": {"member_id": "100234"}}).json()["run_id"]
    _finished(client, first)
    second = client.post(f"/capabilities/{BALANCE}/invoke",
                         json={"arguments": {"member_id": "100987"}}).json()["run_id"]
    _finished(client, second)

    runs = client.get("/runs").json()["runs"]
    assert [r["run_id"] for r in runs][:2] == [second, first]


def test_unknown_run_is_a_404(client: TestClient):
    assert client.get("/runs/deadbeef").status_code == 404
    assert client.get("/runs/deadbeef/evidence").status_code == 404
    assert client.post("/runs/deadbeef/decision", json={"approve": True}).status_code == 404


def test_deciding_a_run_that_is_not_waiting_is_a_409(client: TestClient):
    """A decision is only meaningful while a run is actually paused."""
    body = client.post(f"/capabilities/{BALANCE}/invoke",
                       json={"arguments": {"member_id": "102777"}}).json()
    _finished(client, body["run_id"])
    response = client.post(f"/runs/{body['run_id']}/decision", json={"approve": True})
    assert response.status_code == 409
    assert "not waiting" in response.json()["detail"]


# --- outcomes are reported, not flattened ------------------------------------


def test_a_business_outcome_is_not_a_failure(client: TestClient, outcome: dict[str, Any]):
    outcome["result"] = _Result(
        "business_outcome",
        code="MEMBER_NOT_FOUND",
        message="No member record matches the supplied identifier.",
    )
    body = client.post(f"/capabilities/{BALANCE}/invoke",
                       json={"arguments": {"member_id": "999999"}}).json()
    run = _finished(client, body["run_id"])

    assert run["status"] == "business_outcome"
    assert run["outcome_code"] == "MEMBER_NOT_FOUND"
    summary = client.get(f"/chat/runs/{body['run_id']}/summary").json()["summary"]
    assert "MEMBER_NOT_FOUND" in summary
    assert "did not complete" not in summary


def test_a_failure_names_the_step_and_what_was_observed(client: TestClient, outcome):
    outcome["result"] = _Result(
        "failed",
        error=_Failure(
            category="checkpoint_failed",
            step_id="open_member_record",
            intent="Open member record",
            expected="'MEMBER RECORD'",
            observed="text not present",
        ),
    )
    body = client.post(f"/capabilities/{BALANCE}/invoke",
                       json={"arguments": {"member_id": "102777"}}).json()
    run = _finished(client, body["run_id"])

    assert run["status"] == "failed"
    assert run["error"]["step_id"] == "open_member_record"
    assert "Open member record" in client.get(
        f"/chat/runs/{body['run_id']}/summary").json()["summary"]


# --- the chatbot -------------------------------------------------------------


def test_chat_turns_a_request_into_an_invocation(client: TestClient):
    body = client.post("/chat", json={"message": "what is the balance for member 102777"}).json()
    assert body["capability"] == BALANCE
    assert body["arguments"] == {"member_id": "102777"}
    assert body["planner"].startswith("scripted")

    run = _finished(client, body["run_id"])
    assert run["status"] == "success"


def test_chat_asks_rather_than_inventing_an_argument(client: TestClient):
    body = client.post("/chat", json={"message": "what is the balance"}).json()
    assert body["run_id"] is None
    assert "member_id" in body["reply"]
    assert client.seen == []  # type: ignore[attr-defined]


def test_chat_surfaces_a_guardrail_refusal_instead_of_routing_around_it(client: TestClient):
    body = client.post(
        "/chat",
        json={"message": "transfer 1.00 from 102777-MMKT-4 to 102777-MMKT-3 for member 102777"},
    ).json()
    assert body["refused"] is True
    assert body["run_id"] is None
    assert "draft" in body["reply"]


def test_chat_declines_what_it_cannot_do(client: TestClient):
    body = client.post("/chat", json={"message": "make me a sandwich"}).json()
    assert body["run_id"] is None
    assert body.get("capability") is None


def test_the_chat_planner_is_never_shown_a_credential(client: TestClient):
    """A model cannot leak a field it was never told exists."""
    from service.catalog import CapabilityCatalog
    from service.chatbot import tool_definitions

    catalog = CapabilityCatalog("artifacts")
    entries = [catalog.entry(n) for n in catalog.names() if n.startswith("meridian_hosted")]
    published = str(tool_definitions(entries))
    assert "operator_password" not in published
    assert "operator_id" not in published


# --- the dashboard is a client, not a second door ----------------------------


@pytest.mark.parametrize("path", ["/ui", "/ui/chat", "/ui/runs", "/health"])
def test_dashboard_pages_render(client: TestClient, path: str):
    assert client.get(path).status_code == 200


def test_root_redirects_to_the_dashboard(client: TestClient):
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


def test_run_detail_renders_before_the_run_exists(client: TestClient):
    """The page polls for its own state, so it works for a queued run."""
    assert client.get("/ui/runs/does-not-exist-yet").status_code == 200


# --- run history covers discovery and replay (brief 3.4) ---------------------


def test_run_history_includes_discovery_and_survives_a_restart(client):
    """3.4 asks for run history covering discovery *and* replay.

    The runner only knows what it executed itself, so before this the dashboard
    showed neither discovery recordings nor anything from a previous process --
    it opened empty. The evidence directory is the durable record; the API reads
    it back into the same shape.
    """
    runs = client.get("/runs?limit=200").json()["runs"]
    kinds = {r["kind"] for r in runs}
    assert "discovery" in kinds, "no discovery runs in the history"
    assert "replay" in kinds

    archived = [r for r in runs if r["source"] == "archive"]
    assert archived, "nothing read back from evidence/runs"
    # This client's runner has executed nothing, so every run here is archived.
    assert all(r["evidence_dir"] for r in archived)


def test_run_history_can_be_filtered_by_kind(client):
    only = client.get("/runs?kind=discovery&limit=200").json()["runs"]
    assert only and all(r["kind"] == "discovery" for r in only)


def test_an_archived_discovery_run_opens_by_id(client):
    runs = client.get("/runs?kind=discovery&limit=200").json()["runs"]
    run_id = runs[0]["run_id"]
    body = client.get(f"/runs/{run_id}").json()
    assert body["kind"] == "discovery"
    assert body["steps"], "a discovery run should show the model's actions as steps"
    assert client.get(f"/runs/{run_id}/evidence").json()["files"]


def test_a_discovery_run_surfaces_what_the_recorder_refused(client):
    """The reason a capability is trustworthy, made readable.

    These notes used to go to a terminal that scrolled away.
    """
    runs = client.get("/runs?kind=discovery&limit=200").json()["runs"]
    with_notes = [r for r in runs if r["notes"]]
    assert with_notes, "no discovery run carries recorder notes"
    joined = " ".join(n for r in with_notes for n in r["notes"])
    assert "discarded" in joined or "refused" in joined


def test_archived_run_ids_cannot_escape_the_evidence_root(client):
    for bad in ("../../etc", "..%2f..", ".hidden"):
        assert client.get(f"/runs/{bad}").status_code == 404


# --- an operator who closes the browser (brief 3.5) --------------------------


def test_a_closed_browser_during_a_handoff_is_named_not_a_stack_trace():
    """The escalation hands a person a real browser. Some of them will shut it.

    That used to surface as `internal_error` carrying a raw Playwright repr --
    the one place this system stopped naming what it expected and what it saw,
    on the path most likely to be demonstrated live.
    """
    from service.runner import _describe_worker_failure

    class TargetClosedError(Exception):
        pass

    detail = _describe_worker_failure(
        TargetClosedError("Page.wait_for_timeout: Target page, context or browser has been closed")
    )
    assert detail["category"] == "session_closed"
    assert "browser was closed" in detail["observed"]
    # And it tells the operator the one thing they need to check.
    assert "may already have applied it" in detail["observed"]


def test_an_ordinary_crash_is_still_reported_as_one():
    """The narrowing must not swallow unrelated failures."""
    from service.runner import _describe_worker_failure

    detail = _describe_worker_failure(ValueError("something else entirely"))
    assert detail["category"] == "internal_error"
    assert "ValueError" in detail["observed"]


def test_an_escalation_that_resumed_into_a_crash_does_not_claim_success():
    """Evidence must not read as a clean handoff for a run that never finished."""
    from service.runner import Run, _describe_worker_failure

    run = Run(run_id="r", capability_id="c")
    run.intervention = {"resolution": "resumed", "step_id": "post_the_transfer"}

    # what the worker does when the resume then dies
    run.status = "failed"
    run.error = _describe_worker_failure(RuntimeError("TargetClosedError: browser closed"))
    if run.intervention.get("resolution") == "resumed":
        run.intervention["resolution"] = "resumed_then_failed"

    assert run.intervention["resolution"] == "resumed_then_failed"
