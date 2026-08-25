"""The LLM-driven discovery path (brief 3.1).

Imported lazily so that the replay path -- which must never load a model
library -- does not pull in the Anthropic SDK by importing ``cua``.
"""

from __future__ import annotations

__all__ = ["DiscoveryAgent", "DiscoveryResult", "TOOLS", "resolve_model"]


def __getattr__(name: str):  # pragma: no cover - trivial lazy import
    if name in ("DiscoveryAgent", "DiscoveryResult", "resolve_model"):
        from . import loop

        return getattr(loop, name)
    if name == "TOOLS":
        from .tools import TOOLS

        return TOOLS
    raise AttributeError(name)
