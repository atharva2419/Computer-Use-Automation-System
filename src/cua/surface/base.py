"""The surface seam: how the system perceives and acts, independent of what it
is driving.

Why this boundary exists (brief 3.7)
------------------------------------
The replay engine and the discovery agent are written entirely against this
protocol. Neither imports Playwright, and neither knows what a DOM is. A
``Surface`` speaks only in the vocabulary the capability schema uses --
accessibility roles, accessible names, visible label text, frames -- because
that vocabulary is the one thing a modern web app, a 1998 frameset, and a
Win32 desktop app all genuinely share.

The practical test of the seam: a ``WindowsUIASurface`` implementing these
eleven methods over the OS accessibility API would replay the existing
artifacts unchanged. Nothing above this line would move. Had the schema been
built on CSS selectors, that port would be impossible rather than merely
unwritten.

What deliberately is *not* here: waiting policy, retries, checkpoints, signal
classification, guardrails. Those are decisions, and decisions live in the
engine so that every surface inherits them identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..schema.common import FrameRef
from ..schema.targets import Target

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SurfaceError(Exception):
    """Base class for faults raised by a surface implementation."""


class FrameNotFound(SurfaceError):
    def __init__(self, frame: FrameRef) -> None:
        self.frame = frame
        super().__init__(f"no frame matching {frame.kind}={frame.value!r}")


class TargetNotResolved(SurfaceError):
    """No ranked strategy matched.

    Carries the per-strategy detail so the failure report can say *what was
    tried*, not merely that something failed. The brief asks replay failures
    to record what was expected and what was observed; this is the "expected"
    half for targeting failures.
    """

    def __init__(self, target: Target, attempts: list["StrategyAttempt"]) -> None:
        self.target = target
        self.attempts = attempts
        detail = "; ".join(f"{a.kind}[{a.index}]: {a.detail}" for a in attempts)
        super().__init__(f"could not resolve {target.described_as!r} -- {detail}")


class SurfaceTimeout(SurfaceError):
    """An action or wait exceeded its bound."""


class SurfaceUnavailable(SurfaceError):
    """The application could not be reached at all.

    Connection refused, DNS failure, TLS rejection. Distinguished from other
    surface errors because it is the *application* that is unavailable, not
    the automation that is broken -- and a run that reports "internal error"
    when the server is simply down sends whoever is debugging it to the wrong
    place entirely.
    """


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FrameView:
    """One document within the surface, as roles and names rather than markup.

    ``aria`` is an accessibility-tree rendering, not HTML. That choice is the
    whole design: it is what the LLM reads during discovery and what the
    locator strategies address during replay, so both paths reason about the
    same thing an operator would see.
    """

    name: str
    url: str
    title: str
    aria: str


@dataclass(slots=True)
class Observation:
    """A complete view of the surface at one instant."""

    url: str
    frames: list[FrameView] = field(default_factory=list)

    def frame(self, name: str) -> FrameView | None:
        return next((f for f in self.frames if f.name == name), None)

    def render(self, max_chars_per_frame: int = 6000) -> str:
        """Flatten to text for an LLM prompt or an evidence log.

        Frames are labelled explicitly because on a frameset the model must
        choose one to act in, and that choice is recorded on every target.
        """
        blocks = [f"SURFACE URL: {self.url}"]
        for fv in self.frames:
            label = fv.name or "(top)"
            aria = fv.aria.strip() or "(no accessible content)"
            if len(aria) > max_chars_per_frame:
                aria = aria[:max_chars_per_frame] + "\n... [truncated]"
            blocks.append(
                f"\n--- FRAME name={label!r} url={fv.url} title={fv.title!r} ---\n{aria}"
            )
        return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StrategyAttempt:
    """Outcome of trying one ranked strategy."""

    index: int
    kind: str
    matched: int
    detail: str


@dataclass(slots=True)
class Resolution:
    """Which ranked strategy actually addressed the control.

    ``rank`` is the drift signal. A capability recorded resolving at rank 0
    that begins resolving at rank 2 in production has not failed -- but the
    surface has moved under it, and that is worth knowing before it breaks.
    """

    rank: int
    kind: str
    handle: Any
    ambiguous: bool
    attempts: list[StrategyAttempt]

    @property
    def used_fallback(self) -> bool:
        return self.rank > 0


ReadSource = Literal["text", "value", "attribute"]


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ActivityRecorder(Protocol):
    """Optional capability: log what happened on the surface unprompted.

    Deliberately *not* part of ``Surface``. The core protocol is the set of
    things every surface must do, and a surface that cannot observe activity
    it did not cause should still be usable. Callers check for this at runtime
    and degrade to a free-text note when it is absent.

    Its purpose is the human handoff (brief 3.6, "record what the human did").
    While an operator holds the session the automation is issuing no commands,
    so the only way to know what they did is to watch. A navigation trail is a
    modest but honest record -- it is what actually happened, rather than what
    someone typed into a summary box afterwards.
    """

    def start_activity_log(self) -> None: ...

    def poll_activity(self, settle_ms: int = 150) -> list[str]:
        """Sample for new activity. Must be called periodically.

        Recording is sampled rather than pushed, because a surface cannot
        be assumed to deliver events to a caller that is sitting idle --
        which is exactly what a caller does while waiting for a human.
        """
        ...

    def stop_activity_log(self) -> list[str]: ...


@runtime_checkable
class Surface(Protocol):
    """Perceive and act on one application surface.

    Implementations are stateful (they own a live session) and are expected to
    be driven by exactly one actor at a time -- see ``cua.session``.
    """

    # -- perception -------------------------------------------------------
    def observe(self) -> Observation: ...

    def screenshot(self) -> bytes:
        """Full-surface image, for failure evidence and escalation context."""
        ...

    def frame_text(self, frame: FrameRef) -> str:
        """Visible text of one frame. Backs text-based assertions."""
        ...

    def frame_url(self, frame: FrameRef) -> str: ...

    # -- targeting --------------------------------------------------------
    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolution: ...

    def exists(self, target: Target, timeout_ms: int = 1000) -> bool:
        """Non-raising presence check, for assertions and signal detection."""
        ...

    # -- action -----------------------------------------------------------
    def navigate(self, url: str, frame: FrameRef | None = None) -> None: ...

    def click(self, target: Target, timeout_ms: int = 5000) -> Resolution: ...

    def fill(
        self, target: Target, text: str, clear_first: bool = True, timeout_ms: int = 5000
    ) -> Resolution: ...

    def select(self, target: Target, value: str, timeout_ms: int = 5000) -> Resolution: ...

    def press(self, target: Target, key: str, timeout_ms: int = 5000) -> Resolution: ...

    def read(
        self,
        target: Target,
        source: ReadSource = "text",
        attribute: str | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[str, Resolution]: ...

    # -- lifecycle --------------------------------------------------------
    def settle(self, timeout_ms: int = 10000) -> None:
        """Block until the surface is idle enough to observe.

        Named for intent rather than mechanism: a web surface waits on network
        and load state, a desktop surface might poll for a busy cursor. The
        engine only knows that it should wait for quiet, never how.
        """
        ...

    def close(self) -> None: ...
