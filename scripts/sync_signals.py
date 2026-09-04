"""Refresh the error model embedded in recorded artifacts.

    python scripts/sync_signals.py --product meridian-hosted
    python scripts/sync_signals.py --product meridian-hosted --check

A capability carries its signals inline so that a replay is self-contained --
the artifact is the contract, and it should not silently mean something
different because a config file moved underneath it. But signals describe the
*application*, not the flow: "this host says RECORD NOT FOUND when a record is
missing" is true of every capability recorded against it, and is learned by
hitting the error, which a successful discovery run never does.

So the library is curated per product and copied in at record time. When the
library gains a detection, the artifacts recorded before it are stale. This
re-copies it.

Re-recording would also fix them, and would be the wrong tool: discovery costs
a model run per capability, and would re-derive the steps -- which are correct
and reviewed -- in order to pick up a config change that has nothing to do with
them. Worse, for anything irreversible it would perform the transaction again.

What this deliberately does not touch: steps, inputs, outputs, the success
condition, the version, or the approval state. Adding a detection cannot make
a capability do more than it did -- it can only turn a condition that used to
surface as a locator failure into a named business outcome -- so it is not a
change that needs re-approval. Anything beyond that is a re-recording.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cua.recorder import load_signal_library  # noqa: E402
from cua.schema.capability import Capability  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--product",
        help="Only sync artifacts bound to this product. Default: all products.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change and exit non-zero if anything is stale.",
    )
    args = parser.parse_args()

    libraries: dict[str, list] = {}
    stale = 0

    for path in sorted(args.artifacts.glob("*.json")):
        capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
        product = capability.app.product
        if args.product and product != args.product:
            continue

        if product not in libraries:
            libraries[product] = load_signal_library(product)
        library = libraries[product]
        if not library:
            print(f"{DIM}  {path.name}: no signal library for {product!r}{RESET}")
            continue

        before = {s.id for s in capability.signals}
        after = {s.id for s in library}
        if before == after:
            print(f"{DIM}  {path.name}: up to date ({len(after)} signals){RESET}")
            continue

        stale += 1
        added, removed = sorted(after - before), sorted(before - after)
        detail = "".join(
            [f" {GREEN}+{i}{RESET}" for i in added] + [f" {YELLOW}-{i}{RESET}" for i in removed]
        )
        print(f"  {BOLD}{path.name}{RESET}:{detail}")

        if args.check:
            continue

        capability.signals = list(library)
        # Re-validated on the way out, so a library that would break an
        # artifact's integrity fails here rather than at invocation.
        Capability.model_validate(capability.model_dump(mode="json"))
        path.write_text(
            json.dumps(capability.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )

    if args.check and stale:
        print(f"\n{YELLOW}{stale} artifact(s) stale{RESET}; run without --check to update.")
        return 1
    print(f"\n{GREEN}{'checked' if args.check else 'synced'}{RESET}: {stale} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
