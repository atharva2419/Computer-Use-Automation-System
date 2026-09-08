"""Show a member's shares on the hosted target, sorted by what you can do with them.

    python scripts/target_state.py                 # member 102777
    python scripts/target_state.py --member 100987

The sample host is shared and its data is mutable, so the state you recorded a
capability against is not the state you will replay it into. Balances move and
shares acquire holds. A share on hold cannot be transferred from, and a hold
cannot be released through the console at all, so choosing arguments from
memory is how a working invocation turns into a puzzling failure.

Reads the member page and sorts the shares into the three groups that matter:
what you can move money from, what is free to act on, and what is already
frozen.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

HOST = "https://web-sample.interface-hiring.com"

ROW = re.compile(
    r"<tr><td>(\d{6}-[A-Z0-9-]+)</td><td>([^<]*)</td>"
    r"<td[^>]*>([^<]*)</td><td>(.*?)</td>",
    re.S,
)


def money(text: str) -> float:
    try:
        return float(text.replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def fetch(member: str, operator: str, password: str) -> str:
    """Sign on and return the member page HTML."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )
    opener.open(
        urllib.request.Request(
            f"{HOST}/signon",
            data=urllib.parse.urlencode(
                {"operator": operator, "password": password}
            ).encode(),
        ),
        timeout=20,
    ).read()
    with opener.open(f"{HOST}/members/{member}", timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def summarise(html: str) -> int:
    rows = ROW.findall(html)
    if not rows:
        print("     could not parse the share table -- is the page an error screen?")
        return 1

    usable: list[tuple[str, float]] = []
    held: list[tuple[str, float]] = []
    for share, _kind, balance, status in rows:
        clean = re.sub(r"<[^>]*>", "", status).strip()
        (held if "HOLD" in clean.upper() else usable).append((share, money(balance)))

    funded = sorted((s for s in usable if s[1] > 0), key=lambda r: -r[1])
    empty = [s for s, b in usable if b == 0]

    print("     can transfer FROM (open and funded):")
    for share, balance in funded[:4]:
        print(f"       {share:<22} ${balance:,.2f}")
    if not funded:
        print("       none funded -- a transfer would be refused as an overdraw")

    print(
        "     free to place a hold on (open, empty): "
        + (", ".join(empty[:3]) or "use a high-numbered open share")
    )
    # Named rather than counted. A hold cannot be released through this
    # console, so a held share is permanently out of play -- both for holding
    # again and as the source of a transfer.
    print(f"     already on hold, unusable ({len(held)}):")
    names = [s for s, _ in held]
    for i in range(0, len(names), 5):
        print(f"       {', '.join(names[i:i + 5])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member", default="102777")
    parser.add_argument("--operator", default="teller1")
    parser.add_argument("--password", default="password")
    args = parser.parse_args()

    if not sys.stdin.isatty() and not sys.stdin.closed:
        piped = sys.stdin.read()
        if piped.strip():
            return summarise(piped)
    return summarise(fetch(args.member, args.operator, args.password))


if __name__ == "__main__":
    raise SystemExit(main())
