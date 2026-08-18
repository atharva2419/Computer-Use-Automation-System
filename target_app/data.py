"""In-memory fixture data for MERIDIAN CORE.

All data here is synthetic. No real people, no real account numbers, no real
credentials. Member IDs are deliberately outside any real-world numbering
scheme and balances are invented.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Fake operator credentials. Documented in the README; the point of the login
# screen is to exercise a multi-step flow and a session-expiry failure mode,
# not to model real authentication.
OPERATOR_USERNAME = "op.demo"
OPERATOR_PASSWORD = "demo-pass"

_MEMBERS: dict[str, dict[str, Any]] = {
    "10001": {
        "member_id": "10001",
        "name": "Ada Wexler",
        "status": "ACTIVE",
        "branch": "Riverside",
        "restricted": False,
        "accounts": [
            {"kind": "Savings", "number": "SAV-0001", "balance": 4210.55},
            {"kind": "Checking", "number": "CHK-0001", "balance": 812.30},
        ],
    },
    "10002": {
        "member_id": "10002",
        "name": "Bo Ferreira",
        "status": "ACTIVE",
        "branch": "Northgate",
        "restricted": False,
        "accounts": [
            {"kind": "Savings", "number": "SAV-0002", "balance": 15980.00},
        ],
    },
    "10003": {
        "member_id": "10003",
        "name": "Cyril Nakamura",
        "status": "DORMANT",
        "branch": "Riverside",
        "restricted": False,
        "accounts": [
            {"kind": "Savings", "number": "SAV-0003", "balance": 0.00},
        ],
    },
    # Exercises the permission-denial branch: the record exists, but this
    # operator role may not view it. That is distinct from "not found".
    "20002": {
        "member_id": "20002",
        "name": "Restricted Record",
        "status": "ACTIVE",
        "branch": "Executive",
        "restricted": True,
        "accounts": [
            {"kind": "Savings", "number": "SAV-9002", "balance": 1.00},
        ],
    },
}


_PRISTINE: dict[str, dict[str, Any]] = deepcopy(_MEMBERS)


def reset() -> None:
    """Restore fixture data to its pristine state.

    Replay determinism depends on this: a capability that opens a sub-account
    mutates the member record, so repeated replays would otherwise see a
    growing account list. The harness resets between runs.
    """
    global _MEMBERS
    _MEMBERS = deepcopy(_PRISTINE)


def get_member(member_id: str) -> dict[str, Any] | None:
    member = _MEMBERS.get((member_id or "").strip())
    return deepcopy(member) if member else None


def savings_of(member: dict[str, Any]) -> dict[str, Any] | None:
    for account in member["accounts"]:
        if account["kind"] == "Savings":
            return account
    return None


def add_subaccount(member_id: str, kind: str, opening_deposit: float) -> str:
    """Append a sub-account and return its new account number."""
    member = _MEMBERS[member_id]
    number = f"{kind[:3].upper()}-{member_id}{len(member['accounts']) + 1:02d}"
    member["accounts"].append(
        {"kind": kind, "number": number, "balance": opening_deposit}
    )
    return number
