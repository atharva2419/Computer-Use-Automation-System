"""Regenerate the whole evidence set in one command.

Brief section 6 asks for committed evidence of the end-to-end flow, including
a replay that hits an exceptional state. Producing that by hand is not
reproducible -- a reviewer cannot tell whether a folder is current, and neither
can I. So the set is generated, deterministically, from the artifacts and the
policy that ship in the repo.

Each scenario is a real run against the real console. Faults are injected
through the target app's out-of-band control at a precise step, never by
weakening the artifact or the guardrail.

    python scripts/make_evidence.py            # all scenarios
    python scripts/make_evidence.py --only approval
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from cua.escalation import ScriptedOperator
from cua.evidence import FileEvidenceSink
from cua.guardrails import Policy, PolicyGate
from cua.replay import EscalationContext, EscalationOutcome, ReplayEngine
from cua.schema.capability import Capability
from cua.session import Session
from cua.surface.web import PlaywrightWebSurface

BASE = "http://127.0.0.1:5057"
EVIDENCE_ROOT = Path("evidence/runs")

READ = Path("artifacts/meridian.member.read_savings_balance@v1.json")
SUBACCOUNT = Path("artifacts/meridian.member.open_subaccount@v1.json")

CREDS = {"operator_id": "op.demo", "operator_passphrase": "demo-pass"}
GREEN, YELLOW, RED, BOLD, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m",
)


# ---------------------------------------------------------------------------
# Target app + fault injection
# ---------------------------------------------------------------------------


def _app_is_up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/login", timeout=2)
        return True
    except (urllib.error.URLError, socket.timeout):
        return False


def ensure_app() -> None:
    """Use a running console, or start one in-process.

    Deliberately on the standard port rather than an ephemeral one, so the runs
    are produced under exactly the allowlist that ships in config/policy.yaml
    rather than a loosened copy of it.
    """
    if _app_is_up():
        print(f"{DIM}using the console already running on :5057{RESET}")
        return

    from werkzeug.serving import make_server
    from target_app.app import create_app

    server = make_server("127.0.0.1", 5057, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    print(f"{DIM}started the console in-process on :5057{RESET}")


def chaos(**flags: Any) -> None:
    body = json.dumps(flags).encode()
    request = urllib.request.Request(
        f"{BASE}/__chaos", data=body, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=5).read()


def chaos_reset() -> None:
    urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/__chaos/reset", method="POST"), timeout=5
    ).read()


class ArmingSink:
    """An evidence sink that also injects one fault after a named step.

    Wrapping the sink is how a scenario says "expire the session *after*
    sign-on" without the engine knowing anything about fault injection: it
    already calls the sink after every step, so that is the natural hook.
    """

    def __init__(self, inner: FileEvidenceSink, step_id: str, **flags: Any) -> None:
        self.inner = inner
        self.step_id = step_id
        self.flags = flags
        self.fired = False

    @property
    def directory(self) -> Path | None:
        return self.inner.directory

    def note(self, kind: str, **fields: Any) -> None:
        self.inner.note(kind, **fields)

    def on_step(self, run_id: str, record: Any) -> None:
        self.inner.on_step(run_id, record)
        if record.step_id == self.step_id and not self.fired:
            self.fired = True
            chaos(**self.flags)
            self.inner.note("fault_injected", after_step=self.step_id, **self.flags)

    def on_failure(self, run_id: str, detail: Any, screenshot: bytes, observation: str = "") -> list[str]:
        return self.inner.on_failure(run_id, detail, screenshot, observation)

    def on_result(self, run_id: str, result: Any, capability: Capability) -> None:
        self.inner.on_result(run_id, result, capability)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def reauthenticate(capability: Capability) -> Callable[[EscalationContext], None]:
    """What a person does when the servicing session has lapsed."""

    def _target(step_id: str):
        step = capability.step(step_id)
        assert step is not None
        return getattr(step.action, "target")

    def _do(context: EscalationContext) -> None:
        surface = context.session.surface
        surface.navigate(capability.app.entry_url)
        surface.fill(_target("enter_operator_id"), "op.demo")
        surface.fill(_target("enter_passphrase"), "demo-pass")
        surface.click(_target("submit_signon"))

    return _do


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_scenario(
    label: str,
    artifact: Path,
    params: dict[str, Any],
    *,
    arm_after: tuple[str, dict[str, Any]] | None = None,
    operator: Any = None,
    operator_factory: Callable[[Capability, FileEvidenceSink], Any] | None = None,
) -> str:
    chaos_reset()
    capability = Capability.model_validate_json(artifact.read_text("utf-8"))

    sink: Any = FileEvidenceSink(label=label)
    directory = sink.open(capability, run_kind="replay")
    if arm_after is not None:
        step_id, flags = arm_after
        sink = ArmingSink(sink, step_id, **flags)

    if operator_factory is not None:
        operator = operator_factory(capability, sink)

    surface = PlaywrightWebSurface(headless=True).start()
    session = Session(surface=surface)
    try:
        result = ReplayEngine(
            session,
            gate=PolicyGate.from_file(),
            sink=sink,
            escalation=operator,
        ).run(capability, params)
    finally:
        surface.close()

    tone = {"success": GREEN, "business_outcome": YELLOW}.get(result.status, RED)
    detail = ""
    if result.status == "business_outcome":
        detail = result.code
    elif result.status == "failed":
        detail = result.error.category
    print(f"  {tone}{result.status:<17}{RESET} {detail:<24} {DIM}{directory}{RESET}")
    return result.status


SCENARIOS: dict[str, Callable[[], Any]] = {}


def scenario(name: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def register(fn: Callable[[], Any]) -> Callable[[], Any]:
        SCENARIOS[name] = fn
        return fn

    return register


# -- the read capability ----------------------------------------------------


@scenario("success")
def _success() -> None:
    run_scenario("replay-success", READ, {"member_id": "10001", **CREDS})


@scenario("not-found")
def _not_found() -> None:
    run_scenario("replay-business-not-found", READ, {"member_id": "99999", **CREDS})


@scenario("permission-denied")
def _denied() -> None:
    run_scenario(
        "replay-business-permission-denied", READ, {"member_id": "20002", **CREDS}
    )


@scenario("recovered")
def _recovered() -> None:
    run_scenario(
        "replay-recovered-interstitial",
        READ,
        {"member_id": "10001", **CREDS},
        arm_after=("submit_signon", {"interstitial": 1}),
    )


@scenario("app-error")
def _app_error() -> None:
    run_scenario(
        "replay-failure-app-error",
        READ,
        {"member_id": "10001", **CREDS},
        arm_after=("submit_signon", {"server_error": 1}),
    )


@scenario("invalid-input")
def _invalid() -> None:
    run_scenario(
        "replay-failure-invalid-input", READ, {"member_id": "not-an-id", **CREDS}
    )


@scenario("handoff")
def _handoff() -> None:
    def make_operator(capability: Capability, sink: Any) -> ScriptedOperator:
        return ScriptedOperator(
            decisions=[
                EscalationOutcome(
                    resolved=True,
                    resolution="resumed",
                    operator="stand-in operator (scripted)",
                    resume_from_step="open_member_search",
                )
            ],
            sink=sink,
            on_takeover=reauthenticate(capability),
        )

    run_scenario(
        "replay-human-handoff",
        READ,
        {"member_id": "10001", **CREDS},
        arm_after=("open_member_search", {"expire_session": True}),
        operator_factory=make_operator,
    )


# -- the write capability ---------------------------------------------------

SUBACCOUNT_PARAMS = {
    "member_id": "10001",
    "product_type": "Holiday",
    "opening_deposit": "150.00",
    "nickname": "Holiday Fund",
    **CREDS,
}


@scenario("approval-required")
def _approval_required() -> None:
    """Twelve steps unattended, then a hard stop at the account-creating click."""
    run_scenario(
        "subaccount-blocked-no-operator", SUBACCOUNT, dict(SUBACCOUNT_PARAMS)
    )


@scenario("approval")
def _approved() -> None:
    """The same run with a person available to approve it."""

    def make_operator(capability: Capability, sink: Any) -> ScriptedOperator:
        return ScriptedOperator(
            decisions=[
                EscalationOutcome(
                    resolved=True,
                    resolution="resumed",
                    operator="supervisor (scripted)",
                    note="approved opening a Holiday sub-account for member 10001",
                )
            ],
            sink=sink,
        )

    run_scenario(
        "subaccount-approved-by-operator",
        SUBACCOUNT,
        dict(SUBACCOUNT_PARAMS),
        operator_factory=make_operator,
    )


@scenario("validation")
def _validation() -> None:
    """The institution says no. A business outcome, not a failure."""

    def make_operator(capability: Capability, sink: Any) -> ScriptedOperator:
        return ScriptedOperator(
            decisions=[
                EscalationOutcome(
                    resolved=True, resolution="resumed", operator="supervisor (scripted)"
                )
            ],
            sink=sink,
        )

    run_scenario(
        "subaccount-validation-rejected",
        SUBACCOUNT,
        {**SUBACCOUNT_PARAMS, "opening_deposit": "5.00"},
        operator_factory=make_operator,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete existing generated runs first (manual ones are kept)",
    )
    args = parser.parse_args()

    ensure_app()

    if args.clean:
        for path in EVIDENCE_ROOT.glob("*"):
            if path.is_dir() and "manual" not in path.name:
                shutil.rmtree(path)
        print(f"{DIM}cleared previously generated runs{RESET}")

    chosen = args.only or list(SCENARIOS)
    print(f"\n{BOLD}generating {len(chosen)} evidence run(s){RESET}\n")
    for name in chosen:
        SCENARIOS[name]()

    chaos_reset()
    print(f"\n{BOLD}done{RESET} -- see evidence/README.md\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
