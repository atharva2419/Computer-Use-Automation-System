"""The capability API (brief 3.2).

Endpoints an agent can call to discover a capability by name, invoke it with
typed arguments, and get a structured result -- with no knowledge that a
browser, a legacy console or a locator strategy exists anywhere underneath.

The contract is deliberately not new. Input schemas are generated from the
``ParamSpec`` list each artifact already declares, and a finished run reports
the same three outcomes the replay engine has always produced: success with
typed outputs, a business outcome with a code the caller branches on, or a
failure carrying the step, what was expected and what was observed. The API is
a doorway onto an existing contract rather than a second one layered on top.

Two properties matter more than the endpoint list:

*   **There is no path that skips the guardrail.** Every invocation goes
    through ``CapabilityRunner`` -> ``ReplayEngine`` -> ``PolicyGate``. The
    wrapper cannot become a way around the policy because it has no other way
    in.
*   **Approval is enforced here too.** A capability that posts an irreversible
    transaction is never auto-verified and stays a draft; the API refuses to
    run one unattended unless the caller explicitly asks for a supervised run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .catalog import CapabilityCatalog, CapabilityNotApproved, CapabilityNotFound
from .dashboard import build_router
from .runner import CapabilityRunner

DEFAULT_POLICY = Path("config/policy.meridian-hosted.yaml")
DEFAULT_ARTIFACTS = Path("artifacts")


class InvokeRequest(BaseModel):
    """Arguments for one invocation."""

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Typed arguments matching the capability's input schema.",
    )
    supervised: bool = Field(
        default=False,
        description=(
            "Run a capability that is still a draft. A draft is one a person "
            "has not signed off -- typically because it posts an irreversible "
            "transaction and so was never auto-verified. Setting this says an "
            "operator is watching and will answer the approval it will stop "
            "for; it does not disable the guardrail."
        ),
    )


class DecisionRequest(BaseModel):
    """An operator's answer to a run that stopped for a human."""

    approve: bool
    resume_from: str | None = Field(
        default=None,
        description=(
            "Step to resume at. Recovering a session can move the flow, so the "
            "operator -- who can see it -- says where to pick up. Omit to "
            "retry the step that stopped."
        ),
    )


def create_app(
    artifacts: Path | str = DEFAULT_ARTIFACTS,
    policy: Path | str = DEFAULT_POLICY,
    headed: bool | None = None,
) -> FastAPI:
    catalog = CapabilityCatalog(artifacts)
    runner = CapabilityRunner(
        catalog=catalog,
        policy_path=policy,
        # Headed by default: an operator cannot take over a browser they
        # cannot see, and the handoff is a required path rather than a
        # nice-to-have. Override with CUA_HEADLESS=1 for unattended use.
        headed=(os.environ.get("CUA_HEADLESS", "") != "1") if headed is None else headed,
    )

    app = FastAPI(
        title="Capability API",
        version="1.0.0",
        description=(
            "Recorded UI capabilities, callable by name with typed arguments. "
            "Each invocation runs a deterministic replay -- no model is in the "
            "decision loop."
        ),
    )
    app.state.catalog = catalog
    app.state.runner = runner

    # -- catalog -----------------------------------------------------------

    @app.get("/capabilities", tags=["catalog"])
    def list_capabilities(product: str | None = Query(default=None)) -> dict[str, Any]:
        return {
            "products": catalog.products(),
            "capabilities": catalog.listing(product=product),
            # A malformed artifact is reported rather than hidden, so a
            # capability missing from the list has a visible reason.
            "unloadable": catalog.errors,
        }

    @app.get("/capabilities/{name}", tags=["catalog"])
    def describe_capability(name: str) -> dict[str, Any]:
        try:
            return catalog.entry(name).describe()
        except CapabilityNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/capabilities/reload", tags=["catalog"])
    def reload_catalog() -> dict[str, Any]:
        catalog.reload()
        return {"loaded": len(catalog), "names": catalog.names(), "errors": catalog.errors}

    # -- invocation --------------------------------------------------------

    @app.post("/capabilities/{name}/invoke", status_code=202, tags=["runs"])
    def invoke(name: str, request: InvokeRequest = Body(default=InvokeRequest())) -> dict[str, Any]:
        """Queue an invocation and return immediately with a run id.

        Asynchronous on purpose. A replay drives a real browser for seconds,
        and one that stops for a human may wait minutes -- holding an HTTP
        connection open for that is the wrong shape, and it would make the
        escalation path impossible to express.
        """
        try:
            run = runner.submit(name, request.arguments, allow_draft=request.supervised)
        except CapabilityNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except CapabilityNotApproved as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"run_id": run.run_id, "status": run.status, "poll": f"/runs/{run.run_id}"}

    @app.get("/runs", tags=["runs"])
    def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {"runs": [r.as_dict() for r in runner.recent(limit)]}

    @app.get("/runs/{run_id}", tags=["runs"])
    def get_run(run_id: str) -> dict[str, Any]:
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        return run.as_dict()

    # -- escalation --------------------------------------------------------

    @app.post("/runs/{run_id}/decision", tags=["escalation"])
    def decide(run_id: str, decision: DecisionRequest) -> dict[str, Any]:
        """Answer a run that stopped for a human.

        The paused run is holding the control token and the live browser. This
        hands control back and tells it where to continue.
        """
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        if not runner.decide(run_id, decision.approve, decision.resume_from):
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id!r} is {run.status}, not waiting for a decision",
            )
        return {"run_id": run_id, "accepted": True, "approve": decision.approve}

    # -- evidence ----------------------------------------------------------

    @app.get("/runs/{run_id}/evidence", tags=["runs"])
    def evidence_listing(run_id: str) -> dict[str, Any]:
        """What a run actually wrote.

        Listed rather than assumed: a successful run has no screenshot and no
        observation dump, and offering links to files that were never written
        would misrepresent what the evidence covers.
        """
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        directory = Path(run.evidence_dir) if run.evidence_dir else None
        if directory is None or not directory.is_dir():
            return {"run_id": run_id, "directory": run.evidence_dir, "files": []}
        return {
            "run_id": run_id,
            "directory": str(directory),
            "files": [
                {"name": p.name, "bytes": p.stat().st_size}
                for p in sorted(directory.iterdir())
                if p.is_file()
            ],
        }

    @app.get("/runs/{run_id}/evidence/{filename}", tags=["runs"])
    def evidence(run_id: str, filename: str) -> Any:
        """Serve one evidence file from a run's directory.

        Path traversal is refused rather than sanitised: the only legitimate
        request is for a plain filename inside that run's own folder.
        """
        run = runner.get(run_id)
        if run is None or not run.evidence_dir:
            raise HTTPException(status_code=404, detail="no evidence for that run")
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="invalid filename")
        path = Path(run.evidence_dir) / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no evidence file {filename!r}")
        return FileResponse(path)

    # -- dashboard ---------------------------------------------------------
    # Mounted last so that every API route is already defined: the dashboard
    # is a consumer of them, and registering it this way makes that ordering
    # visible rather than incidental.
    app.include_router(build_router(catalog, artifacts))

    @app.get("/health", tags=["catalog"])
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "capabilities": len(catalog),
                "policy": str(policy),
                "headed": runner.headed,
            }
        )

    return app
