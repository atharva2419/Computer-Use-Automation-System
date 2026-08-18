"""MERIDIAN CORE -- a deliberately legacy-looking servicing console.

This is the proxy target the automation drives (brief section 4). It is
intentionally hostile in the ways real back-office banking software is
hostile:

  * a real <frameset>, so nothing is reachable from the top-level document
  * table-based layout with nested tables and presentational markup
  * no id/class/data-testid on any interactive control
  * form fields named f1/f2/f3, labelled only by the adjacent <td> text,
    so the accessibility tree exposes textboxes with no accessible name
  * navigation via inline onclick on non-semantic elements

It also exposes the runtime conditions the brief calls out in 3.3 -- record
not found, validation error, permission denial, unexpected interstitial,
session expiry, transient slowness, and outright app error -- under
deterministic control via /__chaos so both discovery and replay evidence are
reproducible.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import data

app = Flask(__name__)
# Fixed dev key so sessions survive a reload. This app holds no real data and
# is never intended to run outside a developer machine.
app.secret_key = "meridian-core-dev-key-not-a-secret"

SESSION_TTL_SECONDS = int(os.environ.get("MERIDIAN_SESSION_TTL", "1800"))


@dataclass
class Chaos:
    """Deterministic fault injection, controlled out-of-band via /__chaos.

    Most knobs are countdowns so a test can arm exactly one occurrence:
    "the next request is slow", "the next page shows an interstitial".
    """

    slow_ms: int = 0
    slow_requests: int = 0
    expire_session: bool = False
    interstitial: int = 0
    server_error: int = 0
    validation_always_fails: bool = False

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


chaos = Chaos()


# --------------------------------------------------------------------------
# Fault injection plumbing
# --------------------------------------------------------------------------


@app.before_request
def _apply_chaos() -> Response | None:
    if request.path.startswith("/__chaos"):
        return None

    if chaos.slow_requests > 0 and chaos.slow_ms > 0:
        chaos.slow_requests -= 1
        time.sleep(chaos.slow_ms / 1000.0)

    if chaos.server_error > 0:
        chaos.server_error -= 1
        return Response(
            render_template("error500.html"),
            status=500,
            mimetype="text/html",
        )

    return None


def _session_live() -> bool:
    if chaos.expire_session:
        chaos.expire_session = False
        session.pop("auth_at", None)
        return False
    auth_at = session.get("auth_at")
    if not auth_at:
        return False
    if time.time() - auth_at > SESSION_TTL_SECONDS:
        session.pop("auth_at", None)
        return False
    return True


def _frame_guard() -> str | None:
    """Return rendered HTML if the frame cannot be served, else None.

    Session expiry renders *inside the frame* rather than redirecting the top
    document -- which is how these apps actually behave, and is the harder
    case to detect from automation.
    """
    if not _session_live():
        return render_template("expired.html")
    return None


def _maybe_interstitial(next_url: str) -> str | None:
    if chaos.interstitial > 0:
        chaos.interstitial -= 1
        return render_template("notice.html", next_url=next_url)
    return None


# --------------------------------------------------------------------------
# Chaos control surface (test-harness only, never agent-reachable)
# --------------------------------------------------------------------------


@app.get("/__chaos")
def chaos_get() -> Response:
    return jsonify(chaos.snapshot())


@app.post("/__chaos")
def chaos_set() -> Response:
    payload = request.get_json(silent=True) or {}
    for key, value in payload.items():
        if hasattr(chaos, key):
            setattr(chaos, key, value)
    return jsonify(chaos.snapshot())


@app.post("/__chaos/reset")
def chaos_reset() -> Response:
    """Clear injected faults *and* restore fixture data.

    Both halves matter for reproducible evidence: faults are one-shot
    countdowns, and the sub-account flow mutates member records.
    """
    global chaos
    chaos = Chaos()
    data.reset()
    return jsonify(chaos.snapshot())


# --------------------------------------------------------------------------
# Login / shell
# --------------------------------------------------------------------------


@app.get("/")
def root() -> Any:
    if _session_live():
        return redirect(url_for("console"))
    return redirect(url_for("login"))


@app.get("/login")
def login() -> str:
    return render_template("login.html", error=None)


@app.post("/login")
def login_post() -> Any:
    username = (request.form.get("f1") or "").strip()
    password = request.form.get("f2") or ""
    if username == data.OPERATOR_USERNAME and password == data.OPERATOR_PASSWORD:
        session["auth_at"] = time.time()
        session["operator"] = username
        return redirect(url_for("console"))
    return render_template("login.html", error="Sign-on failed. Check operator ID.")


@app.get("/console")
def console() -> Any:
    if not _session_live():
        return redirect(url_for("login"))
    return render_template("console.html")


@app.get("/frame/nav")
def frame_nav() -> str:
    return render_template("nav.html")


@app.get("/frame/home")
def frame_home() -> str:
    guard = _frame_guard()
    if guard:
        return guard
    return render_template("home.html", operator=session.get("operator", "?"))


# --------------------------------------------------------------------------
# Member search -> detail -> sub-account -> confirmation
# --------------------------------------------------------------------------


@app.get("/frame/search")
def frame_search() -> str:
    guard = _frame_guard()
    if guard:
        return guard
    interstitial = _maybe_interstitial(url_for("frame_search"))
    if interstitial:
        return interstitial
    return render_template("search.html", result=None, notfound=False, query="")


@app.post("/frame/search")
def frame_search_post() -> str:
    guard = _frame_guard()
    if guard:
        return guard

    query = (request.form.get("f1") or "").strip()
    member = data.get_member(query)
    if member is None:
        # Business outcome, not an error: the caller needs this answer.
        return render_template("search.html", result=None, notfound=True, query=query)
    return render_template("search.html", result=member, notfound=False, query=query)


@app.get("/frame/member/<member_id>")
def frame_member(member_id: str) -> str:
    guard = _frame_guard()
    if guard:
        return guard
    interstitial = _maybe_interstitial(url_for("frame_member", member_id=member_id))
    if interstitial:
        return interstitial

    member = data.get_member(member_id)
    if member is None:
        return render_template("member_missing.html", member_id=member_id)
    if member["restricted"]:
        # Distinct from "not found": the record exists and the operator is
        # simply not entitled to it.
        return render_template("denied.html", member_id=member_id)
    return render_template("member.html", member=member, savings=data.savings_of(member))


@app.get("/frame/subaccount/<member_id>")
def frame_subaccount(member_id: str) -> str:
    guard = _frame_guard()
    if guard:
        return guard
    member = data.get_member(member_id)
    if member is None:
        return render_template("member_missing.html", member_id=member_id)
    if member["restricted"]:
        return render_template("denied.html", member_id=member_id)
    return render_template("subaccount.html", member=member, error=None, form={})


@app.post("/frame/subaccount/<member_id>")
def frame_subaccount_post(member_id: str) -> str:
    guard = _frame_guard()
    if guard:
        return guard

    member = data.get_member(member_id)
    if member is None:
        return render_template("member_missing.html", member_id=member_id)
    if member["restricted"]:
        return render_template("denied.html", member_id=member_id)

    kind = (request.form.get("f1") or "").strip()
    deposit_raw = (request.form.get("f2") or "").strip()
    nickname = (request.form.get("f3") or "").strip()
    form = {"f1": kind, "f2": deposit_raw, "f3": nickname}

    error = _validate_subaccount(kind, deposit_raw)
    if error:
        return render_template("subaccount.html", member=member, error=error, form=form)

    number = data.add_subaccount(member_id, kind, float(deposit_raw))
    return render_template(
        "confirm.html",
        member=member,
        kind=kind,
        nickname=nickname,
        deposit=float(deposit_raw),
        number=number,
    )


def _validate_subaccount(kind: str, deposit_raw: str) -> str | None:
    if chaos.validation_always_fails:
        return "Opening deposit is below the product minimum of 25.00."
    if not kind:
        return "Product type is required."
    if not deposit_raw:
        return "Opening deposit is required."
    try:
        deposit = float(deposit_raw)
    except ValueError:
        return "Opening deposit must be a numeric amount."
    if deposit < 25:
        return "Opening deposit is below the product minimum of 25.00."
    return None


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5057")), threaded=True)
