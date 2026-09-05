"""Constructing the Anthropic client.

One place, because there are two callers -- the discovery loop and the
chatbot's planner -- and a credential arrangement that only half the system
honours is worse than one neither does.

The reason this exists at all is the workspace header. An API key can be
scoped to a workspace, in which case the server resolves it from the key and
nothing is needed here. An organisation-level key is not, and every request
must then name the workspace explicitly; without it the API rejects the call
with a 400 rather than picking one. Which kind of key is in use is a property
of how it was issued, so it is read from the environment rather than decided
in code.
"""

from __future__ import annotations

import os
from typing import Any

WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"


def workspace_headers() -> dict[str, str]:
    """The workspace header, if this deployment needs one."""
    workspace = os.environ.get(WORKSPACE_ENV, "").strip()
    return {"anthropic-workspace-id": workspace} if workspace else {}


def build_client(**kwargs: Any) -> Any:
    """An Anthropic client configured for this environment.

    Imported here rather than at module scope so that importing ``cua.agent``
    does not require the SDK to be installed -- the replay path must never
    load a model library, and that guarantee is worth more than the tidiness
    of a top-level import.
    """
    import anthropic

    headers = workspace_headers()
    if headers:
        kwargs.setdefault("default_headers", headers)
    return anthropic.Anthropic(**kwargs)
