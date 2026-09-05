"""The operator dashboard (brief 3.4).

A small server-rendered UI over the capability API: what the system can do,
what it has done, and what one run actually did step by step -- including the
control an operator uses to answer a run that stopped for a human.

Two decisions worth stating.

**The dashboard is a client of the public API, not a privileged path into the
service.** Every button here calls the same endpoints an agent would call:
``POST /capabilities/{name}/invoke``, ``GET /runs/{id}``,
``POST /runs/{id}/decision``. It holds no reference to the runner and has no
back door, so it cannot do anything the guardrails would refuse to an ordinary
caller -- and a refusal is rendered in full rather than flattened to "error",
because the reason the policy gives is the interesting part.

**No build step.** Jinja templates and a little vanilla JavaScript that polls.
A reviewer clones the repository and runs one command; there is no toolchain
to install and nothing compiled, which matters more for something meant to be
read and checked than a framework would.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .catalog import CapabilityCatalog

TEMPLATE_DIR = Path(__file__).parent / "templates"


def build_router(
    catalog: CapabilityCatalog,
    artifact_dir: Path | str,
    planner_name: str = "",
) -> APIRouter:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    router = APIRouter(tags=["dashboard"], include_in_schema=False)

    @router.get("/", response_class=RedirectResponse)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    @router.get("/ui", response_class=HTMLResponse)
    def index(request: Request, product: str | None = Query(default=None)) -> Any:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "capabilities": catalog.listing(product=product),
                "products": catalog.products(),
                "selected": product,
                "artifact_dir": str(artifact_dir),
            },
        )

    @router.get("/ui/chat", response_class=HTMLResponse)
    def chat(request: Request) -> Any:
        # The planner's identity is rendered on the page so a scripted reply
        # is never mistaken for a model's.
        return templates.TemplateResponse(
            request=request, name="chat.html", context={"planner_name": planner_name}
        )

    @router.get("/ui/runs", response_class=HTMLResponse)
    def runs(request: Request) -> Any:
        return templates.TemplateResponse(request=request, name="runs.html", context={})

    @router.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> Any:
        # The page is rendered before the run is looked up, on purpose: it
        # polls for its own state, so it works for a run that is still queued
        # just as well as for one that finished an hour ago.
        return templates.TemplateResponse(
            request=request, name="run.html", context={"run_id": run_id}
        )

    return router
