"""Surface implementations: how the system perceives and acts.

Only ``web`` exists today. It is imported lazily so that code paths which
never touch a browser -- artifact validation, the capability catalog, tests
of the schema -- do not pay for importing Playwright.
"""

from __future__ import annotations

from .base import (
    FrameNotFound,
    FrameView,
    Observation,
    ReadSource,
    Resolution,
    StrategyAttempt,
    Surface,
    SurfaceError,
    SurfaceTimeout,
    SurfaceUnavailable,
    TargetNotResolved,
)

__all__ = [
    "FrameNotFound",
    "FrameView",
    "Observation",
    "ReadSource",
    "Resolution",
    "StrategyAttempt",
    "Surface",
    "SurfaceError",
    "SurfaceTimeout",
    "SurfaceUnavailable",
    "TargetNotResolved",
    "PlaywrightWebSurface",
]


def __getattr__(name: str):  # pragma: no cover - trivial lazy import
    if name == "PlaywrightWebSurface":
        from .web import PlaywrightWebSurface

        return PlaywrightWebSurface
    raise AttributeError(name)
