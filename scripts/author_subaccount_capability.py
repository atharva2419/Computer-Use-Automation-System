"""Hand-author the sub-account capability and write it to artifacts/.

Why a second capability
-----------------------
The first one only reads. This one *writes* -- it opens a real sub-account and
reaches the confirmation screen, which is the brief's own worked example
(section 2) and the case the safety model exists for.

It is the capability that makes several parts of the design do visible work:

*   an ``irreversible`` step, so the guardrail routes the submit to a human for
    approval rather than blocking it or waving it through;
*   a ``reversible_write`` step (choosing the product type), which is permitted
    unattended -- proving the risk classes are not just two labels for one idea;
*   a validation-error signal, the everyday exceptional state that is neither a
    crash nor a successful outcome;
*   a confirmation screen whose generated account number is an output, so the
    caller learns *what* was created rather than merely that something was.

Authored by hand, like the first one, and marked as such in provenance.
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
    SelectAction,
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
#
# The sign-on and search controls are identical to the read capability. They
# are repeated here rather than shared, deliberately: an artifact must be a
# self-contained, reviewable description of one flow. Factoring the common
# prefix into a library would make the JSON unreadable on its own and couple
# two capabilities that should be able to drift apart. Cross-capability reuse
# is what the tenant overlay mechanism is for, and it operates on whole
# artifacts, not on fragments.
# ---------------------------------------------------------------------------

operator_id_box = Target(
    described_as="the Operator ID field on the sign-on form",
    frame=TOP,
    strategies=[LabelCellStrategy(label="Operator ID", control="textbox")],
    rationale=(
        "The input has no accessible name, so role+name cannot address it. "
        "The visible label sits in the preceding table cell, which is how an "
        "operator identifies the field."
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
        "Label cell first; input[type=password] is a rare CSS fallback that is "
        "semantic rather than structural, so it survives re-theming."
    ),
)

sign_on_button = Target(
    described_as="the Sign On button",
    frame=TOP,
    strategies=[RoleNameStrategy(role="button", name="Sign On")],
    rationale="Submit inputs expose their value as the accessible name.",
)

member_search_nav = Target(
    described_as="the Member Search item in the left-hand function menu",
    frame=NAV,
    strategies=[TextStrategy(text="Member Search", element_role="cell")],
    rationale=(
        "Menu items are <td onclick>: there is no link or button role to key "
        "on, so visible text scoped to a cell is the only stable handle."
    ),
)

member_id_box = Target(
    described_as="the Member ID field on the search form",
    frame=MAIN,
    strategies=[LabelCellStrategy(label="Member ID", control="textbox")],
    rationale="Same unnamed-input problem as the sign-on fields.",
)

search_button = Target(
    described_as="the Search button on the member search form",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="button", name="Search")],
    rationale="Stable accessible name from the submit value.",
)

open_record_link = Target(
    described_as="the Open Record link in the search results row",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="link", name="Open Record")],
    rationale="Anchor with stable text; one result row per member id.",
)

open_subaccount_button = Target(
    described_as="the Open Sub-Account button on the member record",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="button", name="Open Sub-Account")],
    rationale=(
        "A button input whose value is its accessible name. Note this only "
        "opens the form -- it creates nothing, so it is a safe action."
    ),
)

product_type_select = Target(
    described_as="the Product Type dropdown on the sub-account form",
    frame=MAIN,
    strategies=[LabelCellStrategy(label="Product Type", control="combobox")],
    rationale=(
        "A <select> with no accessible name, labelled by the adjacent cell. "
        "Role 'combobox' narrows the row match to the control itself."
    ),
)

opening_deposit_box = Target(
    described_as="the Opening Deposit field on the sub-account form",
    frame=MAIN,
    strategies=[LabelCellStrategy(label="Opening Deposit", control="textbox")],
    rationale="Unnamed textbox, addressed by its labelling cell.",
)

nickname_box = Target(
    described_as="the Nickname field on the sub-account form",
    frame=MAIN,
    strategies=[LabelCellStrategy(label="Nickname", control="textbox")],
    rationale="Unnamed textbox, addressed by its labelling cell.",
)

submit_request_button = Target(
    described_as="the Submit Request button on the sub-account form",
    frame=MAIN,
    strategies=[RoleNameStrategy(role="button", name="Submit Request")],
    rationale=(
        "This is the only control in the flow that changes the institution's "
        "records. The guardrail matches on this accessible name to classify "
        "the action irreversible, so the name is load-bearing for safety, not "
        "just for targeting."
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

new_account_number_cell = Target(
    described_as="the New Account Number value on the confirmation screen",
    frame=MAIN,
    strategies=[
        RowScopedCellStrategy(row_contains="New Account Number", cell_index=1)
    ],
    rationale=(
        "Anchored on the label text in its own row rather than a table index, "
        "so inserting a field above it does not read the wrong value."
    ),
)

confirmed_product_cell = Target(
    described_as="the Product Type value on the confirmation screen",
    frame=MAIN,
    strategies=[RowScopedCellStrategy(row_contains="Product Type", cell_index=1)],
    rationale="Same row-anchored pattern as the account number.",
)

confirmed_deposit_cell = Target(
    described_as="the Opening Deposit value on the confirmation screen",
    frame=MAIN,
    strategies=[RowScopedCellStrategy(row_contains="Opening Deposit", cell_index=1)],
    rationale=(
        "Read back from the confirmation rather than echoed from the input: "
        "what the caller needs to know is what the institution recorded, not "
        "what we typed."
    ),
)


# ---------------------------------------------------------------------------
# Capability-wide signals
# ---------------------------------------------------------------------------

signals = [
    SignalRule(
        id="member_not_found",
        description="The searched member ID matches no record.",
        detect=TextPresent(frame=MAIN, text="NO RECORD FOUND"),
        classification="business_outcome",
        outcome_code="MEMBER_NOT_FOUND",
        message="No member matches the supplied member ID.",
    ),
    SignalRule(
        id="permission_denied",
        description="The record exists but this operator may not view it.",
        detect=TextPresent(frame=MAIN, text="PERMISSION DENIED"),
        classification="business_outcome",
        outcome_code="PERMISSION_DENIED",
        message=(
            "The member record exists but the automation operator is not "
            "entitled to it. Distinct from not-found: the caller may need to "
            "route the request to someone who is entitled."
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
            "unattended is deliberately not automated: the system cannot tell "
            "an expiry from a lockout or an MFA challenge, and blindly "
            "resubmitting credentials risks locking the operator account. A "
            "human re-establishes the session and hands control back."
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
# The capability
# ---------------------------------------------------------------------------

capability = Capability(
    id="meridian.member.open_subaccount",
    version=1,
    name="Open a member sub-account",
    description=(
        "Sign on to the MERIDIAN CORE servicing console, look up a member by "
        "ID, open a new sub-account of the requested product type with an "
        "opening deposit, and return the confirmation details including the "
        "new account number."
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
            name="product_type",
            type="string",
            required=True,
            description=(
                "Product to open. Must be one of the values the console "
                "offers: Savings, Checking, Holiday."
            ),
            example="Holiday",
            pattern=r"^(Savings|Checking|Holiday)$",
            sensitivity="internal",
        ),
        ParamSpec(
            name="opening_deposit",
            type="money",
            required=True,
            description=(
                "Opening deposit amount. The product minimum is 25.00; below "
                "that the console rejects the request, which the capability "
                "reports as a validation outcome rather than a failure."
            ),
            example="150.00",
            sensitivity="restricted",
        ),
        ParamSpec(
            name="nickname",
            type="string",
            required=False,
            description="Optional member-facing label for the new account.",
            example="Holiday Fund",
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
            name="new_account_number",
            type="string",
            description="Account number the console assigned to the new sub-account.",
            extract=Extraction(
                target=new_account_number_cell,
                source="text",
                transform=Transform(kind="strip"),
            ),
            required=True,
            sensitivity="restricted",
        ),
        OutputSpec(
            name="product_type",
            type="string",
            description="Product type as recorded on the confirmation screen.",
            extract=Extraction(
                target=confirmed_product_cell,
                source="text",
                transform=Transform(kind="strip"),
            ),
            required=True,
            sensitivity="internal",
        ),
        OutputSpec(
            name="opening_deposit",
            type="money",
            description="Opening deposit as recorded by the institution.",
            extract=Extraction(
                target=confirmed_deposit_cell,
                source="text",
                transform=Transform(kind="money"),
            ),
            required=True,
            sensitivity="restricted",
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
                        "Sign-on was rejected. A configuration fault in the "
                        "invocation, not an answer about a member."
                    ),
                ),
                SignalRule(
                    id="signon_session_not_established",
                    description=(
                        "Sign-on submitted but the console never loaded and no "
                        "credential error was shown."
                    ),
                    detect=UrlMatches(frame=TOP, pattern=r"/login"),
                    classification="hard_failure",
                    escalate=True,
                    message=(
                        "Sign-on returned to the sign-on page without an error. "
                        "The servicing session was not established. A human "
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
                description="Member search form is displayed.",
                assertion=TextPresent(frame=MAIN, text="MEMBER SEARCH"),
            ),
        ),
        Step(
            id="enter_member_id",
            intent="Type the member ID being serviced.",
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
                description="The member record is open.",
                assertion=AllOf(
                    of=[
                        TextPresent(frame=MAIN, text="MEMBER RECORD"),
                        TextPresent(frame=MAIN, text="ACCOUNT TYPE"),
                    ]
                ),
            ),
        ),
        Step(
            id="open_subaccount_form",
            intent="Open the sub-account request form for this member.",
            # Safe: this navigates to a form. Nothing is created until the
            # form is submitted, and classifying it as risky would put an
            # approval in front of an action that changes nothing.
            action=ClickAction(target=open_subaccount_button),
            risk="safe",
            checkpoint=Checkpoint(
                description="The sub-account request form is displayed.",
                assertion=TextPresent(frame=MAIN, text="OPEN SUB-ACCOUNT"),
            ),
        ),
        Step(
            id="choose_product_type",
            intent="Select the product type for the new sub-account.",
            # reversible_write rather than safe: it mutates form state a later
            # step depends on, and an operator could change it back. It is
            # still permitted unattended -- the account does not exist yet.
            action=SelectAction(
                target=product_type_select, value=ParamValue(param="product_type")
            ),
            risk="reversible_write",
        ),
        Step(
            id="enter_opening_deposit",
            intent="Type the opening deposit amount.",
            action=FillAction(
                target=opening_deposit_box, value=ParamValue(param="opening_deposit")
            ),
            risk="safe",
        ),
        Step(
            id="enter_nickname",
            intent="Type the member-facing nickname for the account.",
            action=FillAction(target=nickname_box, value=ParamValue(param="nickname")),
            risk="safe",
        ),
        Step(
            id="submit_subaccount",
            intent="Submit the sub-account request, creating the account.",
            # The one irreversible action in the system. Policy routes it to a
            # human for approval; the artifact declares the same classification
            # so the two agree, and the guardrail takes the worse of the two if
            # they ever diverge.
            action=ClickAction(target=submit_request_button),
            risk="irreversible",
            timeout_ms=15000,
            checkpoint=Checkpoint(
                description="The confirmation screen shows the request was accepted.",
                assertion=TextPresent(frame=MAIN, text="REQUEST CONFIRMED"),
            ),
            signals=[
                SignalRule(
                    id="deposit_below_minimum",
                    description="The console rejected the opening deposit.",
                    detect=TextPresent(frame=MAIN, text="VALIDATION ERROR"),
                    classification="business_outcome",
                    outcome_code="VALIDATION_REJECTED",
                    message=(
                        "The console rejected the request, typically because "
                        "the opening deposit is below the product minimum. A "
                        "business outcome, not a failure: the automation "
                        "worked and the institution said no, which is an "
                        "answer the caller must be able to act on. Nothing "
                        "was created."
                    ),
                ),
            ],
        ),
    ],
    signals=signals,
    success=Checkpoint(
        description=(
            "The confirmation screen is showing a new account number, so the "
            "sub-account was created and the caller can be told what it is."
        ),
        assertion=AllOf(
            of=[
                TextPresent(frame=MAIN, text="REQUEST CONFIRMED"),
                TextPresent(frame=MAIN, text="New Account Number"),
            ]
        ),
        timeout_ms=10000,
    ),
    # Draft, and deliberately so: a capability that creates accounts should not
    # be replayable unattended until a human has reviewed this file. The
    # approval state is part of the contract, not decoration.
    approval="draft",
    provenance=Provenance(
        model="n/a",
        recorded_by="human",
        discovery_run_id=None,
    ),
    tags=["member", "write", "irreversible", "reference"],
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
    print(f"  approval   : {capability.approval}")

    reloaded = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded == capability, "artifact did not round-trip"
    print("  round-trip : OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
