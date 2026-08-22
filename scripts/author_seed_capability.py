"""Hand-author the reference capability and write it to artifacts/.

Why this exists
---------------
The schema is the load-bearing piece of this system, so it gets validated
against the real flow *before* anything is built on top of it. Authoring the
capability by hand proves the schema can express the target flow -- including
the parts the accessibility probe showed are awkward (unnamed textboxes,
non-semantic nav, frame-scoped targets) -- and gives the replay engine
something to run against before the discovery agent exists.

The LLM discovery run produces its own artifact in the same schema; this one
is marked ``recorded_by="human"`` so the two are never confused.
"""

from __future__ import annotations

import json
import pathlib
import sys

from cua.schema import (
    AllOf,
    AnyOf,
    AppBinding,
    Capability,
    Checkpoint,
    ClickAction,
    CssStrategy,
    DismissAndRetry,
    Extraction,
    FillAction,
    FrameRef,
    LabelCellStrategy,
    LiteralValue,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    ParamValue,
    Provenance,
    RoleNameStrategy,
    RowScopedCellStrategy,
    SignalRule,
    Step,
    Target,
    TextPresent,
    TextStrategy,
    Transform,
    UrlMatches,
)

BASE = "http://127.0.0.1:5057"
MAIN = FrameRef(kind="name", value="main")
NAV = FrameRef(kind="name", value="nav")
TOP = FrameRef(kind="top")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

operator_id_box = Target(
    described_as="the Operator ID field on the sign-on form",
    frame=TOP,
    strategies=[
        # The probe showed sign-on textboxes have NO accessible name, so
        # role_name is not available here at all. The visible label in the
        # adjacent cell is the only durable handle.
        LabelCellStrategy(label="Operator ID", control="textbox"),
        RoleNameStrategy(role="textbox", name="Operator ID"),
        CssStrategy(css="input[name=f1]"),
    ],
    rationale=(
        "No accessible name on the input, so label_cell leads. role_name is "
        "kept at rank 2 because a vendor accessibility fix would add a proper "
        "label and it would then win. The name=f1 CSS fallback is last and "
        "brittle by construction -- if replay resolves at that rank, the "
        "capability needs re-review."
    ),
)

passphrase_box = Target(
    described_as="the Passphrase field on the sign-on form",
    frame=TOP,
    strategies=[
        LabelCellStrategy(label="Passphrase", control="textbox"),
        CssStrategy(css="input[type=password]"),
    ],
    rationale=(
        "Password inputs are exposed with no role in the a11y tree, so the "
        "label cell plus an input-type fallback. The type=password fallback is "
        "unusually durable here: it is semantic, not structural."
    ),
)

sign_on_button = Target(
    described_as="the Sign On button",
    frame=TOP,
    strategies=[
        RoleNameStrategy(role="button", name="Sign On"),
        TextStrategy(text="Sign On"),
    ],
    rationale=(
        "Submit inputs take their accessible name from the value attribute, "
        "so role_name works and is the most durable option available."
    ),
)

member_search_nav = Target(
    described_as="the Member Search item in the left-hand function menu",
    frame=NAV,
    strategies=[
        # Navigation is <td onclick> -- role 'cell', no interactive role.
        TextStrategy(text="Member Search", element_role="cell"),
        TextStrategy(text="Member Search"),
    ],
    rationale=(
        "Non-semantic navigation: there is no link or button role to target, "
        "only cell text. Scoped to role=cell first to avoid matching a heading "
        "elsewhere on the page."
    ),
)

member_id_box = Target(
    described_as="the Member ID input on the search form",
    frame=MAIN,
    strategies=[
        LabelCellStrategy(label="Member ID", control="textbox"),
        CssStrategy(css="form input[name=f1]"),
    ],
    rationale=(
        "Same unnamed-textbox pattern as sign-on. Note 'Member ID' also appears "
        "as a results-table column header; label_cell resolves within the row "
        "containing the label, and the header row holds no textbox, so the "
        "match is unambiguous."
    ),
)

search_button = Target(
    described_as="the Search button on the member search form",
    frame=MAIN,
    strategies=[
        RoleNameStrategy(role="button", name="Search"),
        TextStrategy(text="Search"),
    ],
    rationale="Submit input with a value attribute; role+name is stable.",
)

open_record_link = Target(
    described_as="the Open Record link on the search result row",
    frame=MAIN,
    strategies=[
        RoleNameStrategy(role="link", name="Open Record"),
        TextStrategy(text="Open Record", element_role="link"),
    ],
    rationale=(
        "A real anchor, so role+name applies. Search is by exact member id and "
        "returns at most one row, so there is no row-disambiguation problem; a "
        "multi-result variant would need a row_scoped target keyed on the id."
    ),
)

acknowledge_button = Target(
    described_as="the Acknowledge button on the system notice interstitial",
    frame=MAIN,
    strategies=[
        RoleNameStrategy(role="button", name="Acknowledge"),
        TextStrategy(text="Acknowledge"),
    ],
    rationale="Dismissal control for the maintenance interstitial.",
)


# ---------------------------------------------------------------------------
# Signals -- the declared error model
# ---------------------------------------------------------------------------

SIGNALS = [
    SignalRule(
        id="member_not_found",
        description="Search returned no member for the supplied id.",
        detect=TextPresent(frame=MAIN, text="NO RECORD FOUND"),
        classification="business_outcome",
        outcome_code="MEMBER_NOT_FOUND",
        message="No member matches the supplied member ID.",
    ),
    SignalRule(
        id="permission_denied",
        description="Record exists but this operator role is not entitled to it.",
        detect=TextPresent(frame=MAIN, text="PERMISSION DENIED"),
        classification="business_outcome",
        outcome_code="PERMISSION_DENIED",
        message=(
            "The member record exists but the automation operator is not "
            "entitled to view it. A caller may legitimately need to know this, "
            "so it is an outcome rather than a failure -- but it also means the "
            "answer was not obtained, and the caller must branch on it."
        ),
    ),
    SignalRule(
        id="system_notice",
        description="Unscheduled maintenance interstitial blocking the flow.",
        detect=TextPresent(frame=MAIN, text="SYSTEM NOTICE"),
        classification="recoverable",
        recovery=DismissAndRetry(dismiss=acknowledge_button, max_attempts=2),
        message="Dismissed the maintenance interstitial and retried the step.",
    ),
    SignalRule(
        id="session_expired",
        description="Operator sign-on lapsed mid-flow.",
        detect=TextPresent(frame=MAIN, text="SESSION EXPIRED"),
        classification="hard_failure",
        escalate=True,
        message=(
            "The servicing session expired mid-flow. Re-authenticating "
            "unattended is deliberately not automated: it would mean the "
            "system holds and replays operator credentials outside the "
            "invocation that supplied them. A human re-establishes the session "
            "and hands control back."
        ),
    ),
    SignalRule(
        id="app_error",
        description="The servicing subsystem returned a fault page.",
        detect=AnyOf(
            of=[
                TextPresent(frame=MAIN, text="APPLICATION ERROR"),
                TextPresent(frame=TOP, text="APPLICATION ERROR"),
            ]
        ),
        classification="hard_failure",
        message="The application returned fault MCX-5000.",
    ),
]


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

capability = Capability(
    id="meridian.member.read_savings_balance",
    version=1,
    name="Read member savings balance",
    description=(
        "Sign on to the MERIDIAN CORE servicing console, look up a member by "
        "ID, open their record, and return the member name plus the current "
        "balance and account number of their Savings account."
    ),
    app=AppBinding(
        product="meridian-core",
        product_version="r7.4.1",
        tenant=None,
        entry_url=f"{BASE}/login",
        surface="legacy_web",
    ),
    inputs=[
        ParamSpec(
            name="member_id",
            type="string",
            required=True,
            description="Institution member number.",
            example="10001",
            pattern=r"^\d{4,8}$",
            sensitivity="restricted",
        ),
        ParamSpec(
            name="operator_id",
            type="string",
            required=True,
            description="Servicing operator sign-on id.",
            example="op.demo",
            sensitivity="internal",
        ),
        ParamSpec(
            name="operator_passphrase",
            type="string",
            required=True,
            description="Servicing operator passphrase.",
            sensitivity="restricted",
            secret=True,
        ),
    ],
    outputs=[
        OutputSpec(
            name="member_name",
            type="string",
            description="Name on the member record.",
            sensitivity="restricted",
            extract=Extraction(
                target=Target(
                    described_as="the Name cell on the member record header",
                    frame=MAIN,
                    strategies=[
                        RowScopedCellStrategy(row_contains="Member ID", cell_index=4)
                    ],
                    rationale=(
                        "Header is a flat label/value table. Anchored on the "
                        "'Member ID' label text rather than an absolute table "
                        "index, so inserting a table above it does not shift it."
                    ),
                ),
                transform=Transform(kind="strip"),
            ),
        ),
        OutputSpec(
            name="savings_balance",
            type="money",
            description="Current balance of the member's Savings account.",
            sensitivity="restricted",
            extract=Extraction(
                target=Target(
                    described_as="the balance cell of the Savings row",
                    frame=MAIN,
                    strategies=[
                        RowScopedCellStrategy(row_contains="Savings", cell_index=2)
                    ],
                    rationale=(
                        "Row anchored on the account-type value, column by "
                        "position. Robust to the member holding a different "
                        "number of accounts, which an absolute row index is not. "
                        "Nested tables mean the outer wrapper row also contains "
                        "'Savings'; resolution takes the most specific "
                        "(shortest-text) matching row."
                    ),
                ),
                transform=Transform(kind="money"),
            ),
        ),
        OutputSpec(
            name="savings_account_number",
            type="string",
            description="Account number of the Savings account.",
            sensitivity="restricted",
            extract=Extraction(
                target=Target(
                    described_as="the account-number cell of the Savings row",
                    frame=MAIN,
                    strategies=[
                        RowScopedCellStrategy(row_contains="Savings", cell_index=1)
                    ],
                    rationale="Same row anchor as the balance, adjacent column.",
                ),
                transform=Transform(kind="strip"),
            ),
        ),
    ],
    steps=[
        Step(
            id="open_signon",
            intent="Open the servicing console sign-on page.",
            action=NavigateAction(url=LiteralValue(value=f"{BASE}/login"), frame=TOP),
            risk="safe",
            checkpoint=Checkpoint(
                description="Sign-on form is displayed.",
                assertion=TextPresent(frame=TOP, text="OPERATOR SIGN ON"),
            ),
        ),
        Step(
            id="enter_operator_id",
            intent="Type the operator sign-on id.",
            action=FillAction(
                target=operator_id_box, value=ParamValue(param="operator_id")
            ),
            risk="safe",
        ),
        Step(
            id="enter_passphrase",
            intent="Type the operator passphrase.",
            action=FillAction(
                target=passphrase_box, value=ParamValue(param="operator_passphrase")
            ),
            risk="safe",
        ),
        Step(
            id="submit_signon",
            intent="Submit the sign-on form.",
            action=ClickAction(target=sign_on_button),
            risk="reversible_write",
            checkpoint=Checkpoint(
                description="Console frameset loaded after successful sign-on.",
                assertion=UrlMatches(frame=TOP, pattern=r"/console$"),
            ),
            signals=[
                SignalRule(
                    id="signon_rejected",
                    description="Operator credentials were rejected.",
                    detect=TextPresent(frame=TOP, text="Sign-on failed"),
                    classification="hard_failure",
                    message=(
                        "Sign-on was rejected. Treated as a hard failure rather "
                        "than a business outcome: the caller supplied bad "
                        "operator credentials, which is a configuration fault "
                        "in the invocation, not an answer about a member."
                    ),
                ),
                SignalRule(
                    id="signon_session_not_established",
                    description=(
                        "Sign-on submitted but the console never loaded and no "
                        "credential error was shown."
                    ),
                    # Evaluated after signon_rejected, so a genuine bad-password
                    # bounce is claimed by the more specific signal first. What
                    # is left is the quiet case: correct credentials, but the
                    # session did not survive -- the app simply returns you to
                    # the sign-on page with nothing to read. Without this the
                    # condition degrades into a generic checkpoint timeout,
                    # which tells an operator nothing about what to do.
                    detect=UrlMatches(frame=TOP, pattern=r"/login"),
                    classification="hard_failure",
                    escalate=True,
                    message=(
                        "Sign-on returned to the sign-on page without an error. "
                        "The servicing session was not established -- typically "
                        "an expired or gateway-rejected session. A human "
                        "re-establishes it and hands control back."
                    ),
                ),
            ],
        ),
        Step(
            id="open_member_search",
            intent="Open Member Search from the function menu.",
            action=ClickAction(target=member_search_nav),
            risk="safe",
            checkpoint=Checkpoint(
                description="Member search form is displayed in the main frame.",
                assertion=TextPresent(frame=MAIN, text="MEMBER SEARCH"),
            ),
        ),
        Step(
            id="enter_member_id",
            intent="Type the member ID being looked up.",
            action=FillAction(
                target=member_id_box, value=ParamValue(param="member_id")
            ),
            risk="safe",
        ),
        Step(
            id="run_search",
            intent="Run the member search.",
            action=ClickAction(target=search_button),
            risk="safe",
            checkpoint=Checkpoint(
                description="A matching member row is listed.",
                assertion=TextPresent(frame=MAIN, text="Open Record"),
            ),
        ),
        Step(
            id="open_record",
            intent="Open the member record from the results row.",
            action=ClickAction(target=open_record_link),
            risk="safe",
            checkpoint=Checkpoint(
                description="Member record with the account table is displayed.",
                assertion=AllOf(
                    of=[
                        TextPresent(frame=MAIN, text="MEMBER RECORD"),
                        TextPresent(frame=MAIN, text="ACCOUNT TYPE"),
                    ]
                ),
            ),
        ),
    ],
    signals=SIGNALS,
    success=Checkpoint(
        description=(
            "The member record is open and the account table shows a Savings "
            "row, so the requested balance is on screen."
        ),
        assertion=AllOf(
            of=[
                TextPresent(frame=MAIN, text="CURRENT BALANCE"),
                TextPresent(frame=MAIN, text="Savings"),
            ]
        ),
    ),
    approval="draft",
    provenance=Provenance(
        model="n/a",
        recorded_by="human",
        discovery_run_id=None,
    ),
    tags=["member", "read-only", "reference"],
)


def main() -> int:
    out_dir = pathlib.Path("artifacts")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{capability.id}@v{capability.version}.json"
    path.write_text(
        json.dumps(capability.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")
    print(f"  steps      : {len(capability.steps)}")
    print(f"  inputs     : {[p.name for p in capability.inputs]}")
    print(f"  outputs    : {[o.name for o in capability.outputs]}")
    print(f"  signals    : {[s.id for s in capability.signals]}")
    print(f"  max risk   : {capability.max_risk}")

    # Round-trip: the artifact on disk must validate back into the model.
    reloaded = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded == capability, "artifact did not round-trip"
    print("  round-trip : OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
