"""Arm or clear fault injection on the running target app.

The target app exposes runtime conditions (brief 3.3) behind an out-of-band
control endpoint so both discovery and replay evidence are reproducible. This
is a thin CLI over it.

Usage:
    python scripts/chaos.py status
    python scripts/chaos.py reset
    python scripts/chaos.py notice          # one maintenance interstitial
    python scripts/chaos.py expire          # expire the operator session once
    python scripts/chaos.py error           # one HTTP 500 fault page
    python scripts/chaos.py slow 2500       # next request stalls 2.5s
    python scripts/chaos.py badvalidation   # sub-account form always rejects
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5057"

PRESETS: dict[str, dict[str, object]] = {
    "notice": {"interstitial": 1},
    "expire": {"expire_session": True},
    "error": {"server_error": 1},
    "badvalidation": {"validation_always_fails": True},
}


def _post(path: str, payload: dict[str, object] | None = None) -> dict:
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as resp:
        return json.loads(resp.read())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    cmd = argv[0]
    try:
        if cmd == "status":
            state = _get("/__chaos")
        elif cmd == "reset":
            state = _post("/__chaos/reset")
        elif cmd == "slow":
            ms = int(argv[1]) if len(argv) > 1 else 2500
            state = _post("/__chaos", {"slow_ms": ms, "slow_requests": 1})
        elif cmd in PRESETS:
            state = _post("/__chaos", PRESETS[cmd])
        else:
            print(f"unknown command {cmd!r}\n")
            print(__doc__)
            return 2
    except urllib.error.URLError as exc:
        print(f"cannot reach the target app at {BASE}: {exc}")
        print("start it with:  python -m target_app.app")
        return 1

    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
