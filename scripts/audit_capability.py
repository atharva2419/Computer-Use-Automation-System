"""Read a recorded capability the way its approver should.

    python scripts/audit_capability.py artifacts/*.json
    python scripts/audit_capability.py --strict artifacts/some.json

A capability that posts an irreversible transaction is never auto-verified --
replaying it to check would perform the transaction again -- so it stays a
draft until a person approves it. This prints what that person needs to look
at, in the order it matters.

The one thing worth checking by eye is whether the capability is really
parameterised. An artifact declares its inputs, so it *looks* reusable; but a
checkpoint or a locator that quotes the record it happened to be recorded
against will pass every replay for that record and fail for every other one.
The recorder discards what it can prove is invocation-specific (an argument it
was given, a value it read back as an output). What it cannot prove -- a name
on a screen the flow never reads -- surfaces here instead.

Exit code is 1 under --strict if anything is flagged, so this can gate a
commit; without it, the audit is advisory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cua.recorder import suspect_checkpoints  # noqa: E402
from cua.schema.capability import Capability  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


def audit(path: Path, strict: bool) -> int:
    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    risk_colour = RED if capability.max_risk == "irreversible" else DIM
    approval_colour = GREEN if capability.approval == "approved" else YELLOW

    print(f"\n{BOLD}{capability.id}{RESET}  v{capability.version}")
    print(
        f"  {approval_colour}{capability.approval}{RESET}"
        f"  {risk_colour}{capability.max_risk}{RESET}"
        f"  {len(capability.steps)} steps"
        f"  {DIM}{capability.app.product} {capability.app.product_version}{RESET}"
    )

    inputs = ", ".join(
        f"{s.name}{'*' if s.secret else ''}" for s in capability.inputs
    )
    print(f"  {DIM}inputs {RESET} {inputs or '(none)'}   {DIM}* = secret{RESET}")
    print(f"  {DIM}outputs{RESET} {', '.join(o.name for o in capability.outputs) or '(none)'}")

    risky = [s for s in capability.steps if s.risk != "safe"]
    if risky:
        print(f"  {DIM}risk   {RESET} " + ", ".join(f"{s.id} ({s.risk})" for s in risky))

    without = [s.id for s in capability.steps if s.checkpoint is None]
    print(
        f"  {DIM}checks {RESET} {len(capability.steps) - len(without)}"
        f"/{len(capability.steps)} steps have a checkpoint"
        + (f"   {DIM}none: {', '.join(without)}{RESET}" if without else "")
    )

    suspects = suspect_checkpoints(capability)
    if not suspects:
        print(f"  {GREEN}no checkpoint looks pinned to one record{RESET}")
        return 0

    print(f"\n  {YELLOW}{BOLD}{len(suspects)} checkpoint(s) may be pinned to one record{RESET}")
    for step_id, text, why in suspects:
        print(f"    {BOLD}{step_id}{RESET}")
        print(f"      asserts {text!r}")
        print(f"      {YELLOW}{why}{RESET}")
    print(
        f"    {DIM}If these are record data rather than screen furniture, this\n"
        f"    capability only works for the record it was recorded against.\n"
        f"    Re-record, or approve knowingly.{RESET}"
    )
    return 1 if strict else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any capability has a suspected pinned checkpoint.",
    )
    args = parser.parse_args()

    worst = 0
    for path in args.paths:
        if not path.is_file():
            print(f"{RED}no such artifact: {path}{RESET}")
            worst = max(worst, 2)
            continue
        worst = max(worst, audit(path, args.strict))
    print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
