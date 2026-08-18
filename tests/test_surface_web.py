"""Surface tests against the live target console.

These are integration tests on purpose. The whole claim of the surface layer
is that the ranked strategies address controls on markup that defeats ordinary
selectors -- and that claim cannot be tested against a mock, only against the
frameset, the nested tables and the unnamed inputs themselves.

The target app is started in-process on a free port, so the suite needs no
external services.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
from werkzeug.serving import make_server

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
from cua.session import Actor, ControlViolation, Session
from cua.surface.base import TargetNotResolved
from cua.surface.web import PlaywrightWebSurface
from target_app.app import create_app
from target_app import data as fixture_data

MAIN = FrameRef(kind="name", value="main")
TOP = FrameRef(kind="top")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    port = _free_port()
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def surface(base_url: str) -> Iterator[PlaywrightWebSurface]:
    s = PlaywrightWebSurface(headless=True).start()
    yield s
    s.close()


@pytest.fixture
def signed_on(surface: PlaywrightWebSurface, base_url: str) -> PlaywrightWebSurface:
    """Sign on and land on the console frameset before each test."""
    fixture_data.reset()
    surface.navigate(f"{base_url}/login")
    surface.fill(_operator_id(), "op.demo")
    surface.fill(_passphrase(), "demo-pass")
    surface.click(_sign_on())
    return surface


# -- targets used across tests ---------------------------------------------


def _operator_id() -> Target:
    return Target(
        described_as="Operator ID field",
        frame=TOP,
        strategies=[LabelCellStrategy(label="Operator ID", control="textbox")],
    )


def _passphrase() -> Target:
    return Target(
        described_as="Passphrase field",
        frame=TOP,
        strategies=[
            LabelCellStrategy(label="Passphrase", control="textbox"),
            CssStrategy(css="input[type=password]"),
        ],
    )


def _sign_on() -> Target:
    return Target(
        described_as="Sign On button",
        frame=TOP,
        strategies=[RoleNameStrategy(role="button", name="Sign On")],
    )


def _member_id_field() -> Target:
    return Target(
        described_as="Member ID search field",
        frame=MAIN,
        strategies=[LabelCellStrategy(label="Member ID", control="textbox")],
    )


def _search_button() -> Target:
    return Target(
        described_as="Search button",
        frame=MAIN,
        strategies=[RoleNameStrategy(role="button", name="Search")],
    )


def _nav_member_search() -> Target:
    return Target(
        described_as="Member Search menu item",
        frame=FrameRef(kind="name", value="nav"),
        strategies=[TextStrategy(text="Member Search", element_role="cell")],
    )


def _savings_balance_cell() -> Target:
    return Target(
        described_as="balance cell of the Savings row",
        frame=MAIN,
        strategies=[RowScopedCellStrategy(row_contains="Savings", cell_index=2)],
    )


# -- perception -------------------------------------------------------------


def test_observe_sees_every_frame_by_name(signed_on: PlaywrightWebSurface) -> None:
    observation = signed_on.observe()
    names = {f.name for f in observation.frames}
    assert {"", "nav", "main"} <= names, "frameset children must be observable"


def test_observation_is_accessibility_tree_not_markup(
    signed_on: PlaywrightWebSurface,
) -> None:
    nav = signed_on.observe().frame("nav")
    assert nav is not None
    # Roles and names, no tags or presentational attributes.
    assert "Member Search" in nav.aria
    assert "<td" not in nav.aria and "bgcolor" not in nav.aria


def test_render_labels_frames_for_the_model(signed_on: PlaywrightWebSurface) -> None:
    text = signed_on.observe().render()
    assert "FRAME name='main'" in text and "FRAME name='nav'" in text


# -- ranked strategy resolution --------------------------------------------


def test_label_cell_resolves_an_input_with_no_accessible_name(
    signed_on: PlaywrightWebSurface, base_url: str
) -> None:
    """The core legacy problem: role+name cannot see these inputs at all."""
    signed_on.click(_nav_member_search())

    unnamed = Target(
        described_as="Member ID via role+name",
        frame=MAIN,
        strategies=[RoleNameStrategy(role="textbox", name="Member ID")],
    )
    with pytest.raises(TargetNotResolved):
        signed_on.resolve(unnamed, timeout_ms=800)

    resolution = signed_on.resolve(_member_id_field(), timeout_ms=3000)
    assert resolution.kind == "label_cell"
    assert resolution.rank == 0


def test_falls_back_down_the_ranked_list_and_records_the_rank(
    signed_on: PlaywrightWebSurface,
) -> None:
    target = Target(
        described_as="Search button with a broken preferred strategy",
        frame=MAIN,
        strategies=[
            RoleNameStrategy(role="button", name="Nonexistent Control"),
            RoleNameStrategy(role="button", name="Search"),
        ],
    )
    signed_on.click(_nav_member_search())
    resolution = signed_on.resolve(target, timeout_ms=2000)

    assert resolution.rank == 1, "should have fallen through to the second strategy"
    assert resolution.used_fallback is True
    assert resolution.attempts[0].detail == "no match"


def test_unresolvable_target_reports_every_strategy_tried(
    signed_on: PlaywrightWebSurface,
) -> None:
    target = Target(
        described_as="a control that is not there",
        frame=MAIN,
        strategies=[
            RoleNameStrategy(role="button", name="Wire Transfer"),
            CssStrategy(css="input[name=nope]"),
        ],
    )
    with pytest.raises(TargetNotResolved) as exc:
        signed_on.resolve(target, timeout_ms=800)

    assert len(exc.value.attempts) == 2
    assert "role_name" in str(exc.value) and "css" in str(exc.value)


def test_text_strategy_reaches_non_semantic_nav_cell(
    signed_on: PlaywrightWebSurface,
) -> None:
    """Nav items are <td onclick>: no button role, no link role, no href."""
    resolution = signed_on.resolve(_nav_member_search(), timeout_ms=3000)
    assert resolution.kind == "text"


def test_nested_tables_are_disambiguated_to_the_innermost_row(
    signed_on: PlaywrightWebSurface, base_url: str
) -> None:
    """The Savings row is nested inside an outer wrapper row that repeats its text.

    A naive "row containing Savings" match hits both the inner data row and
    the outer wrapper row, whose text is the concatenation of the whole table.
    Picking the outer one would read the wrong cell entirely, so the correct
    value here is the assertion that matters.
    """
    _open_member(signed_on, "10001")
    value, resolution = signed_on.read(_savings_balance_cell())

    assert value.strip() == "4210.55", "must read the Savings row, not the wrapper"
    assert resolution.kind == "row_scoped_cell"
    assert resolution.rank == 0

    # The Checking row must resolve independently rather than to the same cell.
    checking = Target(
        described_as="balance cell of the Checking row",
        frame=MAIN,
        strategies=[RowScopedCellStrategy(row_contains="Checking", cell_index=2)],
    )
    assert signed_on.read(checking)[0].strip() == "812.30"


# -- action -----------------------------------------------------------------


def _open_member(surface: PlaywrightWebSurface, member_id: str) -> None:
    surface.click(_nav_member_search())
    surface.fill(_member_id_field(), member_id)
    surface.click(_search_button())
    surface.click(
        Target(
            described_as="Open Record link",
            frame=MAIN,
            strategies=[RoleNameStrategy(role="link", name="Open Record")],
        )
    )


def test_full_flow_reaches_the_member_record(signed_on: PlaywrightWebSurface) -> None:
    _open_member(signed_on, "10001")
    assert evaluate(signed_on, TextPresent(frame=MAIN, text="MEMBER RECORD")).ok
    assert evaluate(signed_on, TextPresent(frame=MAIN, text="Ada Wexler")).ok


def test_select_and_fill_on_the_subaccount_form(
    signed_on: PlaywrightWebSurface, base_url: str
) -> None:
    _open_member(signed_on, "10001")
    signed_on.click(
        Target(
            described_as="Open Sub-Account button",
            frame=MAIN,
            strategies=[RoleNameStrategy(role="button", name="Open Sub-Account")],
        )
    )
    signed_on.select(
        Target(
            described_as="Product Type dropdown",
            frame=MAIN,
            strategies=[LabelCellStrategy(label="Product Type", control="combobox")],
        ),
        "Holiday",
    )
    signed_on.fill(
        Target(
            described_as="Opening Deposit field",
            frame=MAIN,
            strategies=[LabelCellStrategy(label="Opening Deposit", control="textbox")],
        ),
        "150",
    )
    signed_on.click(
        Target(
            described_as="Submit Request button",
            frame=MAIN,
            strategies=[RoleNameStrategy(role="button", name="Submit Request")],
        )
    )
    assert evaluate(signed_on, TextPresent(frame=MAIN, text="REQUEST CONFIRMED")).ok


# -- assertions -------------------------------------------------------------


def test_assertion_reports_what_it_observed_on_failure(
    signed_on: PlaywrightWebSurface,
) -> None:
    signed_on.click(_nav_member_search())
    result = evaluate(signed_on, TextPresent(frame=MAIN, text="TOTALLY ABSENT"))
    assert not result.ok
    assert "MEMBER SEARCH" in result.observed, "should show where we actually landed"


def test_url_assertion_targets_the_frame_not_the_top_document(
    signed_on: PlaywrightWebSurface,
) -> None:
    _open_member(signed_on, "10001")
    assert wait_until(
        signed_on, UrlMatches(frame=MAIN, pattern=r"/frame/member/10001"), 5000
    ).ok
    # The top document stays on /console throughout: on a frameset, "the URL"
    # is per-frame, which is why every assertion carries a frame reference.
    assert not evaluate(signed_on, UrlMatches(frame=TOP, pattern=r"/frame/member")).ok
    assert evaluate(signed_on, UrlMatches(frame=TOP, pattern=r"/console")).ok


# -- control token ----------------------------------------------------------


def test_session_blocks_the_agent_while_a_human_holds_control(
    surface: PlaywrightWebSurface,
) -> None:
    session = Session(surface=surface)
    session.claim(Actor.AGENT)
    with session.acting_as(Actor.AGENT):
        pass

    session.cede(Actor.HUMAN, reason="irreversible step needs approval")
    with pytest.raises(ControlViolation):
        with session.acting_as(Actor.AGENT):
            pass

    session.cede(Actor.AGENT, reason="operator handed back")
    with session.acting_as(Actor.AGENT) as live:
        assert live is surface, "must be the same live session, not a new one"

    assert session.human_touched is True
    assert [t["to"] for t in session.ledger()] == ["agent", "human", "agent"]
