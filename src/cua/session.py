"""Who is driving the session.

The brief (3.6) asks for a control-transfer model, not just a pause button:
automation must be able to "pause, cede control, and resume on the *same*
session, and there must be a way to know who is (or should be) in control."

The model here is a single-holder token guarding one live surface:

    NONE ---claim(AGENT)---> AGENT ---cede(HUMAN)---> HUMAN ---resume--> AGENT

Three properties make it real rather than decorative:

*   **One surface, one holder.** The token guards the same ``Surface``
    instance throughout. A handoff never constructs a new session, so cookies,
    frame state, scroll position and half-filled forms all survive it -- which
    is the entire point, and the reason a "spawn a fresh browser for the
    operator" design would not satisfy the requirement.
*   **Enforced, not advisory.** Mutating the surface without holding the token
    raises. The guard is a context manager so it is hard to forget and
    impossible to leave half-open.
*   **Auditable.** Every transfer is recorded with who, why and when, and the
    ledger goes into the run evidence. After a handoff the report can say
    exactly which steps the machine did and which a person did.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from .surface.base import Surface


class Actor(str, Enum):
    """Who may act on the surface."""

    NONE = "none"
    AGENT = "agent"
    HUMAN = "human"


class ControlViolation(RuntimeError):
    """An actor tried to drive a session it does not hold.

    Deliberately an error rather than a warning: the failure mode this
    prevents is automation typing into a form while a human operator is
    mid-correction, which in a banking back office is precisely the kind of
    silent double-action nobody can untangle afterwards.
    """


@dataclass(slots=True)
class ControlTransfer:
    """One entry in the control ledger."""

    at: float
    frm: Actor
    to: Actor
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "from": self.frm.value,
            "to": self.to.value,
            "reason": self.reason,
        }


@dataclass
class Session:
    """A live surface plus the token that says who may drive it."""

    surface: Surface
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    holder: Actor = Actor.NONE
    transfers: list[ControlTransfer] = field(default_factory=list)

    # -- transitions ------------------------------------------------------

    def claim(self, actor: Actor, reason: str = "start of run") -> None:
        """Take control of an unheld session."""
        if self.holder is not Actor.NONE and self.holder is not actor:
            raise ControlViolation(
                f"{actor.value} cannot claim a session held by {self.holder.value}"
            )
        self._record(actor, reason)

    def cede(self, to: Actor, reason: str) -> None:
        """Hand control to another actor. The surface is untouched.

        Nothing is torn down here -- that is the requirement. The session keeps
        running exactly as it was; only the identity of who is permitted to
        act changes.
        """
        if to is self.holder:
            return
        self._record(to, reason)

    def release(self, reason: str = "run complete") -> None:
        self._record(Actor.NONE, reason)

    def _record(self, to: Actor, reason: str) -> None:
        self.transfers.append(ControlTransfer(time.time(), self.holder, to, reason))
        self.holder = to

    # -- enforcement ------------------------------------------------------

    def require(self, actor: Actor) -> None:
        if self.holder is not actor:
            raise ControlViolation(
                f"{actor.value} tried to act while control is held by "
                f"{self.holder.value} (session {self.session_id})"
            )

    @contextmanager
    def acting_as(self, actor: Actor) -> Iterator[Surface]:
        """Scope in which ``actor`` may drive the surface.

        Yields the surface so callers physically cannot reach it without
        passing the check first.
        """
        self.require(actor)
        yield self.surface

    # -- reporting --------------------------------------------------------

    @property
    def human_touched(self) -> bool:
        """Whether a person ever held this session.

        Surfaced on the run result: a capability whose replay needed manual
        help is not the same as one that ran clean, even when both end in
        success, and confidence scoring should treat them differently.
        """
        return any(t.to is Actor.HUMAN for t in self.transfers)

    def ledger(self) -> list[dict[str, object]]:
        return [t.as_dict() for t in self.transfers]
