"""Run an LLM against the live console and record what it learns.

This is the expensive path, and it runs once per capability. It needs an
ANTHROPIC_API_KEY in .env; nothing else in this repo does.

    python scripts/discover.py \
        --goal "Sign on, look up member {member_id}, and read their savings balance" \
        --id meridian.member.read_savings_balance \
        --name "Read member savings balance" \
        --param member_id=10001 \
        --param operator_id=op.demo \
        --secret operator_passphrase=demo-pass \
        --evidence discovery

The run ends with a verification replay of whatever was recorded. A capability
that cannot immediately reproduce itself stays a draft, and says why.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cua.agent.loop import DiscoveryAgent, resolve_model
from cua.escalation import ConsoleOperatorHandler
from cua.evidence import FileEvidenceSink
from cua.guardrails import DEFAULT_POLICY_PATH, PolicyGate
from cua.recorder import Recorder
from cua.replay import ReplayEngine
from cua.schema.capability import AppBinding, ParamSpec
from cua.session import Session
from cua.surface.web import PlaywrightWebSurface

GREEN, YELLOW, RED, BOLD, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m",
)


def _pairs(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--{what} expects name=value, got {item!r}")
        name, _, value = item.partition("=")
        out[name.strip()] = value
    return out


def build_inputs(
    params: dict[str, str], secrets: dict[str, str], patterns: dict[str, str]
) -> list[ParamSpec]:
    specs: list[ParamSpec] = []
    for name, value in params.items():
        specs.append(
            ParamSpec(
                name=name,
                type="string",
                required=True,
                description=f"Supplied at discovery as {value!r}.",
                example=value,
                pattern=patterns.get(name),
                sensitivity="restricted" if name.endswith("_id") else "internal",
            )
        )
    for name in secrets:
        specs.append(
            ParamSpec(
                name=name,
                type="string",
                required=True,
                description="Operator credential.",
                # No example: the artifact is committed, and a credential
                # example in a committed file is a credential in the repo.
                example=None,
                sensitivity="restricted",
                secret=True,
            )
        )
    return specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--id", required=True, dest="capability_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--product", default="meridian-core")
    parser.add_argument("--product-version", default="r7.4.1")
    parser.add_argument("--entry-url", default="http://127.0.0.1:5057/login")
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--secret", action="append", default=[])
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--budget-seconds", type=int, default=300)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--operator", action="store_true")
    parser.add_argument("--evidence", nargs="?", const="discovery", default="discovery")
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        metavar="FILE",
        help="guardrail policy to enforce; defaults to config/policy.yaml. "
             "Pointing the core at a different target is a policy + signal "
             "library change, so this is how a new target is selected.",
    )
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{RED}ANTHROPIC_API_KEY is not set.{RESET}")
        print("Copy .env.example to .env and put your key in it.")
        return 2

    try:
        import anthropic
    except ImportError:
        print(f"{RED}the anthropic package is not installed{RESET}")
        return 2

    params = _pairs(args.param, "param")
    secrets = _pairs(args.secret, "secret")
    patterns = _pairs(args.pattern, "pattern")
    bound = {**params, **secrets}

    model = resolve_model(args.model)
    app = AppBinding(
        product=args.product,
        product_version=args.product_version,
        entry_url=args.entry_url,
        surface="legacy_web",
    )
    inputs = build_inputs(params, secrets, patterns)

    goal = args.goal.format(**params) if "{" in args.goal else args.goal

    policy_path = args.policy or DEFAULT_POLICY_PATH
    gate = PolicyGate.from_file(policy_path, discovery=True)
    redactor = gate.policy.redactor()
    redactor.learn_secrets(secrets.values())

    sink = FileEvidenceSink(label=args.evidence)

    recorder = Recorder(
        capability_id=args.capability_id,
        name=args.name,
        goal=goal,
        app=app,
        inputs=inputs,
        bound=bound,
    )

    surface = PlaywrightWebSurface(
        headless=not (args.headed or args.operator)
    ).start()
    session = Session(surface=surface)
    escalation = (
        ConsoleOperatorHandler(sink=sink, redactor=redactor) if args.operator else None
    )

    print(f"{BOLD}discovery{RESET}  model={model}  goal={goal!r}")
    directory = sink.open(_placeholder(recorder), run_kind="discovery")
    print(f"{DIM}evidence -> {directory}{RESET}\n")

    try:
        agent = DiscoveryAgent(
            session=session,
            client=anthropic.Anthropic(),
            model=model,
            gate=gate,
            recorder=recorder,
            sink=sink,
            redactor=redactor,
            escalation=escalation,
            max_steps=args.max_steps,
            budget_seconds=args.budget_seconds,
        )
        result = agent.discover(goal, args.entry_url, bound)
    finally:
        session.release()
        surface.close()

    # newline="\n" matters here: Path.write_text translates \n to \r\n on
    # Windows by default, and the artifact's provenance records a SHA-256 of
    # this transcript. Without it the digest never matches the file on disk,
    # so the one field a reviewer might independently verify silently fails.
    (directory / "transcript.json").write_text(
        result.transcript, encoding="utf-8", newline="\n"
    )
    sink.note(
        "discovery_finished",
        status=result.status,
        steps=result.steps_taken,
        model_turns=result.model_turns,
        reason=redactor.text(result.reason),
    )

    tone = GREEN if result.status == "recorded" else RED
    print(f"\n{tone}{BOLD}{result.status.upper()}{RESET}  "
          f"{result.steps_taken} steps, {result.model_turns} model turns")
    if result.reason:
        print(f"  {result.reason}")
    for note in result.notes:
        print(f"  {YELLOW}note{RESET} {note}")

    if result.capability is None:
        return 1

    path = args.out / f"{result.capability.id}@v{result.capability.version}.json"
    path.write_text(
        result.capability.model_dump_json(indent=2), encoding="utf-8", newline="\n"
    )
    print(f"\n  artifact -> {path}")
    print(f"  steps    : {[s.id for s in result.capability.steps]}")
    print(f"  outputs  : {[o.name for o in result.capability.outputs]}")
    print(f"  signals  : {[s.id for s in result.capability.signals]}")
    print(f"  approval : {result.capability.approval}")

    if args.no_verify:
        return 0
    return _verify(result.capability, bound, policy_path, redactor, path)


def _placeholder(recorder: Recorder) -> Any:
    """A stand-in capability so evidence can be opened before one exists.

    The run folder has to exist from the first model turn -- a discovery run
    that crashes early is exactly the one worth having a trail for.
    """
    from cua.schema.capability import Capability, Checkpoint, NavigateAction, Step
    from cua.schema.common import FrameRef, LiteralValue, TextPresent

    return Capability(
        id=recorder.capability_id,
        name=recorder.name,
        description=recorder.goal,
        app=recorder.app,
        inputs=recorder.inputs,
        steps=[
            Step(
                id="pending",
                intent="not yet discovered",
                action=NavigateAction(
                    url=LiteralValue(value=recorder.app.entry_url), frame=FrameRef()
                ),
            )
        ],
        success=Checkpoint(
            description="pending", assertion=TextPresent(frame=FrameRef(), text="")
        ),
    )


def _verify(capability, bound, policy_path, redactor, path: Path) -> int:
    """Replay what was just recorded, before anyone trusts it.

    A model-authored artifact is a hypothesis until it reproduces. This is
    cheap -- the replay engine already exists -- and it is what earns the
    move from draft to approved.
    """
    print(f"\n{BOLD}verifying by replay{RESET} {DIM}(no model in this path){RESET}")
    verify_sink = FileEvidenceSink(label="discovery-verify")
    verify_sink.open(capability, run_kind="replay")

    surface = PlaywrightWebSurface(headless=True).start()
    session = Session(surface=surface)
    try:
        outcome = ReplayEngine(
            session,
            gate=PolicyGate.from_file(policy_path),
            sink=verify_sink,
            redactor=redactor,
        ).run(capability, bound)
    finally:
        session.release()
        surface.close()

    if outcome.status == "success":
        capability.approval = "approved"
        path.write_text(
            capability.model_dump_json(indent=2), encoding="utf-8", newline="\n"
        )
        print(f"  {GREEN}reproduced{RESET}: outputs {sorted(outcome.outputs)}")
        print(f"  approval promoted to {GREEN}approved{RESET}")
        return 0

    print(f"  {RED}did not reproduce{RESET} ({outcome.status})")
    if outcome.status == "failed":
        print(f"    step     {outcome.error.step_id}")
        print(f"    expected {outcome.error.expected}")
        print(f"    observed {outcome.error.observed}")
    print(f"  artifact left as {YELLOW}draft{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
