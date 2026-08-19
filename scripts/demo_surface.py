"""Watch the surface layer drive the console, with its reasoning printed.

There is nothing to see in the target app that was not there before -- the
point of this script is to make the *automation* visible. It opens a real
browser window, performs the member-lookup flow one action at a time, and for
each action reports which ranked locator strategy actually resolved the
control and what the alternatives did.

This is the layer the replay engine and the discovery agent will both sit on
top of. Neither exists yet: every step below is hand-written here, not read
from an artifact and not decided by a model.

Usage (with the target app already running on :5057):
    python scripts/demo_surface.py
    python scripts/demo_surface.py --headless
    python scripts/demo_surface.py --slow 600
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

from cua.assertions import evaluate, wait_until
from cua.schema.common import FrameRef, TextPresent, UrlMatches
from cua.schema.targets import (
    CssStrategy,
    LabelCellStrategy,
    RoleNameStrategy,
    RowScopedCellStrategy,
    Target,
    TextStrategy,
)
from cua.session import Actor, Session
from cua.surface.base import Resolution, Surface, TargetNotResolved
from cua.surface.web import PlaywrightWebSurface

BASE = "http://127.0.0.1:5057"
TOP = FrameRef(kind="top")
NAV = FrameRef(kind="name", value="nav")
MAIN = FrameRef(kind="name", value="main")

DIM, BOLD, GREEN, YELLOW, RED, RESET = (
    "\033[2m",
    "\033[1m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)


def banner(text: str) -> None:
    print(f"\n{BOLD}{'=' * 72}\n{text}\n{'=' * 72}{RESET}")


def report(action: str, target: Target, resolution: Resolution) -> None:
    """Show how the control was found, not just that it was."""
    colour = GREEN if resolution.rank == 0 else YELLOW
    print(f"\n{BOLD}{action}{RESET}  {DIM}{target.described_as}{RESET}")
    print(f"  frame        : {target.frame.kind}={target.frame.value!r}")
    for attempt in resolution.attempts:
        mark = "->" if attempt.index == resolution.rank else "  "
        tone = colour if attempt.index == resolution.rank else DIM
        print(
            f"  {mark} rank {attempt.index} {tone}{attempt.kind:<16}{RESET}"
            f" {DIM}{attempt.detail}{RESET}"
        )
    if resolution.used_fallback:
        print(
            f"  {YELLOW}fell back to rank {resolution.rank}"
            f" -- preferred strategy no longer matches (drift signal){RESET}"
        )


def check(surface: Surface, description: str, assertion) -> None:
    result = wait_until(surface, assertion, timeout_ms=5000)
    tone = GREEN if result.ok else RED
    verdict = "HELD" if result.ok else "FAILED"
    print(f"  {tone}checkpoint {verdict}{RESET}  {description}")
    if not result.ok:
        print(f"    expected: {result.expected}")
        print(f"    observed: {result.observed}")


# -- the controls this flow touches ----------------------------------------

OPERATOR_ID = Target(
    described_as="Operator ID field on the sign-on form",
    frame=TOP,
    strategies=[LabelCellStrategy(label="Operator ID", control="textbox")],
    rationale="Input has no accessible name; the visible label sits in the "
    "preceding table cell, which is how an operator identifies it.",
)

PASSPHRASE = Target(
    described_as="Passphrase field on the sign-on form",
    frame=TOP,
    strategies=[
        LabelCellStrategy(label="Passphrase", control="textbox"),
        CssStrategy(css="input[type=password]"),
    ],
    rationale="Label cell first; input[type=password] is a semantic fallback, "
    "unusually durable for a CSS strategy.",
)

SIGN_ON = Target(
    described_as="Sign On button",
    frame=TOP,
    strategies=[RoleNameStrategy(role="button", name="Sign On")],
    rationale="Submit inputs expose value as the accessible name.",
)

NAV_SEARCH = Target(
    described_as="Member Search item in the function menu",
    frame=NAV,
    strategies=[TextStrategy(text="Member Search", element_role="cell")],
    rationale="Menu items are <td onclick>: no link or button role exists, so "
    "visible text scoped to a cell is the only handle.",
)

MEMBER_ID = Target(
    described_as="Member ID field on the search form",
    frame=MAIN,
    strategies=[LabelCellStrategy(label="Member ID", control="textbox")],
    rationale="Same unnamed-input problem as sign-on.",
)

SEARCH_BUTTON = Target(
    described_as="Search button",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="button", name="Search")],
    rationale="Stable accessible name.",
)

OPEN_RECORD = Target(
    described_as="Open Record link in the results row",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="link", name="Open Record")],
    rationale="Anchor with stable text.",
)

SAVINGS_BALANCE = Target(
    described_as="balance cell of the Savings row",
    frame=MAIN,
    strategies=[RowScopedCellStrategy(row_contains="Savings", cell_index=2)],
    rationale="Anchored on the row's own data value rather than a row index, "
    "so reordering accounts does not read the wrong balance.",
)

DELIBERATELY_BROKEN = Target(
    described_as="Search button, with a stale preferred strategy",
    frame=MAIN,
    strategies=[
        RoleNameStrategy(role="button", name="Find Member"),
        RoleNameStrategy(role="button", name="Search"),
    ],
    rationale="Simulates a renamed control: rank 0 no longer matches, rank 1 "
    "still does. Replay survives and records that it degraded.",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--slow", type=int, default=350, help="ms between actions")
    parser.add_argument("--member", default="10001")
    args = parser.parse_args()

    try:
        urllib.request.urlopen(f"{BASE}/login", timeout=3)
    except urllib.error.URLError:
        print(f"{RED}target app is not running at {BASE}{RESET}")
        print("start it in another terminal:  python -m target_app.app")
        return 1

    surface = PlaywrightWebSurface(
        headless=args.headless, slow_mo_ms=args.slow
    ).start()
    session = Session(surface=surface)
    session.claim(Actor.AGENT, reason="surface demo")

    try:
        with session.acting_as(Actor.AGENT) as live:
            banner("1. SIGN ON  (top document -- no frames yet)")
            live.navigate(f"{BASE}/login")
            report("FILL  ", OPERATOR_ID, live.fill(OPERATOR_ID, "op.demo"))
            report("FILL  ", PASSPHRASE, live.fill(PASSPHRASE, "*" * 9))
            print(f"  {DIM}(passphrase is masked here; secrets never reach a log){RESET}")
            live.fill(PASSPHRASE, "demo-pass")
            report("CLICK ", SIGN_ON, live.click(SIGN_ON))
            check(live, "console frameset reached", UrlMatches(frame=TOP, pattern=r"/console"))

            banner("2. THE SURFACE IS NOW A FRAMESET")
            observation = live.observe()
            for view in observation.frames:
                print(f"  frame {view.name or '(top)':<8} {view.url}")
            print(f"\n{DIM}What the automation perceives in the nav frame -- roles and")
            print(f"names from the accessibility tree, not markup:{RESET}\n")
            nav_view = observation.frame("nav")
            if nav_view:
                for line in nav_view.aria.strip().splitlines()[:14]:
                    print(f"    {line}")

            banner("3. NAVIGATE VIA A NON-SEMANTIC MENU ITEM")
            report("CLICK ", NAV_SEARCH, live.click(NAV_SEARCH))
            check(live, "search form shown", TextPresent(frame=MAIN, text="MEMBER SEARCH"))

            banner("4. RANKED FALLBACK  (rank 0 deliberately stale)")
            report("RESOLVE", DELIBERATELY_BROKEN, live.resolve(DELIBERATELY_BROKEN))

            banner("5. AN INPUT WITH NO ACCESSIBLE NAME")
            print(f"{DIM}role+name cannot address this control at all:{RESET}")
            unnamed = Target(
                described_as="Member ID via role+name",
                frame=MAIN,
                strategies=[RoleNameStrategy(role="textbox", name="Member ID")],
            )
            try:
                live.resolve(unnamed, timeout_ms=900)
                print(f"  {RED}unexpectedly resolved{RESET}")
            except TargetNotResolved as exc:
                print(f"  {RED}TargetNotResolved{RESET} {DIM}{exc}{RESET}")
            print(f"\n{DIM}the label-cell strategy does:{RESET}")
            report("FILL  ", MEMBER_ID, live.fill(MEMBER_ID, args.member))

            banner("6. SEARCH -> RESULT -> RECORD")
            report("CLICK ", SEARCH_BUTTON, live.click(SEARCH_BUTTON))
            found = evaluate(live, TextPresent(frame=MAIN, text="NO RECORD FOUND"))
            if found.ok:
                print(f"  {YELLOW}business outcome: no member {args.member}{RESET}")
                print(f"  {DIM}a legitimate answer, not a crash -- the replay engine")
                print(f"  will return this as a typed outcome{RESET}")
                return 0
            report("CLICK ", OPEN_RECORD, live.click(OPEN_RECORD))
            check(live, "member record reached",
                  UrlMatches(frame=MAIN, pattern=rf"/frame/member/{args.member}"))

            banner("7. EXTRACT THE OUTPUT")
            value, resolution = live.read(SAVINGS_BALANCE)
            report("READ  ", SAVINGS_BALANCE, resolution)
            print(f"\n  {GREEN}{BOLD}savings_balance = {value.strip()}{RESET}")
            print(f"  {DIM}typed as `money` in the artifact; classified restricted,")
            print(f"  so redaction will govern where this value may be written{RESET}")

            banner("8. CONTROL LEDGER")
            for entry in session.ledger():
                print(f"  {entry['from']:>5} -> {entry['to']:<5}  {entry['reason']}")
            print(f"\n  human_touched = {session.human_touched}")

        if not args.headless:
            input(f"\n{BOLD}press Enter to close the browser{RESET} ")
    finally:
        session.release()
        surface.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
