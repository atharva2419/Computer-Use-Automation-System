"""Replay a saved capability against the live target -- the production path.

This is the command an AI agent's tool call would ultimately trigger: give it
an artifact and typed arguments, get back a typed result. No model is loaded
and no API key is needed.

Usage:
    python scripts/replay.py artifacts/meridian.member.read_savings_balance@v1.json \
        --param member_id=10001 --param operator_id=op.demo --param operator_passphrase=demo-pass

    ... --headed          watch it run
    ... --json            machine-readable result on stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cua.evidence import FileEvidenceSink
from cua.guardrails import PolicyGate
from cua.replay import ReplayEngine
from cua.schema.capability import Capability
from cua.session import Session
from cua.surface.web import PlaywrightWebSurface

GREEN, YELLOW, RED, BOLD, DIM, RESET = (
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[1m",
    "\033[2m",
    "\033[0m",
)

STATUS_COLOUR = {"success": GREEN, "business_outcome": YELLOW, "failed": RED}


def parse_params(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        out[name.strip()] = value
    return out


def render(result: Any) -> None:
    colour = STATUS_COLOUR.get(result.status, "")
    print(f"\n{BOLD}{'=' * 68}{RESET}")
    print(f"{colour}{BOLD}{result.status.upper()}{RESET}"
          f"   {result.capability_id} v{result.capability_version}"
          f"   {DIM}run {result.run_id} in {result.duration_ms}ms{RESET}")
    print(f"{BOLD}{'=' * 68}{RESET}\n")

    for record in result.steps:
        tone = GREEN if record.status == "ok" else RED
        via = f"{record.resolved_by}@rank{record.strategy_rank}" if record.resolved_by else "-"
        print(
            f"  {tone}{record.status:<8}{RESET} {record.index}. {record.step_id:<20}"
            f" {DIM}{via:<22} {record.duration_ms:>5}ms{RESET}"
        )
        if record.note:
            print(f"           {DIM}{record.note}{RESET}")

    if result.recoveries:
        print(f"\n  {YELLOW}recoveries{RESET}")
        for rec in result.recoveries:
            mark = "ok" if rec.succeeded else "failed"
            print(f"    {rec.signal_id} -> {rec.action} (attempt {rec.attempt}, {mark})")

    if result.escalations:
        print(f"\n  {YELLOW}escalations{RESET}")
        for esc in result.escalations:
            print(f"    {esc.step_id}: {esc.reason} -> {esc.resolution}")

    if result.status == "success":
        print(f"\n  {BOLD}outputs{RESET}")
        for name, value in result.outputs.items():
            print(f"    {name:<24} {GREEN}{value!r}{RESET}")

    elif result.status == "business_outcome":
        print(f"\n  {BOLD}outcome code{RESET}  {YELLOW}{result.code}{RESET}")
        print(f"  {result.message}")
        print(f"  {DIM}this is a legitimate answer, not a failure{RESET}")

    else:
        err = result.error
        print(f"\n  {BOLD}failure{RESET}  {RED}{err.category}{RESET}")
        if err.step_id:
            print(f"    step      {err.step_index}. {err.step_id}  {DIM}{err.intent}{RESET}")
        print(f"    expected  {err.expected}")
        print(f"    observed  {err.observed}")
        if err.strategies_tried:
            print(f"    tried     {', '.join(err.strategies_tried)}")
        if err.evidence:
            print(f"    evidence  {', '.join(err.evidence)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow", type=int, default=0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--evidence",
        nargs="?",
        const="replay",
        default=None,
        metavar="LABEL",
        help="write a run trail to evidence/runs/<timestamp>-<label>/",
    )
    args = parser.parse_args()

    capability = Capability.model_validate_json(args.artifact.read_text("utf-8"))
    supplied = parse_params(args.param)

    gate = PolicyGate.from_file()
    sink = None
    if args.evidence:
        sink = FileEvidenceSink(label=args.evidence)
        directory = sink.open(capability, run_kind="replay")
        print(f"{DIM}evidence -> {directory}{RESET}")

    surface = PlaywrightWebSurface(
        headless=not args.headed, slow_mo_ms=args.slow
    ).start()
    session = Session(surface=surface)
    try:
        result = ReplayEngine(session, gate=gate, sink=sink).run(capability, supplied)
    finally:
        session.release()
        surface.close()

    if args.as_json:
        print(result.model_dump_json(indent=2))
    else:
        render(result)

    return 0 if result.status in ("success", "business_outcome") else 1


if __name__ == "__main__":
    sys.exit(main())
