"""Evaluating assertions against any surface.

Checkpoints, signal detection and wait conditions all share one assertion
vocabulary (``cua.schema.common.Assertion``). Evaluating it lives here rather
than in a surface implementation for two reasons: the semantics must be
identical on every surface, and every evaluation must report *what it
observed*, not just true or false.

That second point is why this returns a result object instead of a bool. The
brief asks a replay failure to say "what step, what was expected, what was
observed"; if assertions collapse to booleans, the observed half is gone by
the time anyone needs it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .schema.common import (
    AllOf,
    AnyOf,
    Assertion,
    ElementPresent,
    FrameRef,
    TextAbsent,
    TextPresent,
    UrlMatches,
)
from .surface.base import Surface, SurfaceError

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass(slots=True)
class AssertionResult:
    ok: bool
    expected: str
    observed: str
    children: list["AssertionResult"] = field(default_factory=list)

    def describe(self) -> str:
        return f"expected {self.expected}; observed {self.observed}"


def _excerpt(text: str, needle: str, width: int = 90) -> str:
    """A short window of surrounding text, for the ``observed`` field.

    Whole-frame text is far too long for a failure report and mostly noise.
    When the needle is absent we show the head of the frame instead, which is
    usually the banner and tells a debugger which screen they actually landed
    on.
    """
    flat = _normalise(text)
    if not flat:
        return "(frame had no visible text)"
    idx = flat.lower().find(needle.lower())
    if idx < 0:
        head = flat[:width]
        return f"text not present; frame begins {head!r}"
    start = max(0, idx - width // 3)
    return f"...{flat[start : start + width]}..."


def evaluate(surface: Surface, assertion: Assertion) -> AssertionResult:
    """Evaluate one assertion, capturing what was observed either way."""

    if isinstance(assertion, TextPresent):
        return _text(surface, assertion.frame, assertion.text, assertion.case_sensitive, True)

    if isinstance(assertion, TextAbsent):
        return _text(surface, assertion.frame, assertion.text, assertion.case_sensitive, False)

    if isinstance(assertion, UrlMatches):
        try:
            url = surface.frame_url(assertion.frame)
        except SurfaceError as exc:
            return AssertionResult(False, f"url matching /{assertion.pattern}/", str(exc))
        ok = re.search(assertion.pattern, url) is not None
        return AssertionResult(ok, f"url matching /{assertion.pattern}/", url)

    if isinstance(assertion, ElementPresent):
        desc = assertion.target.described_as
        try:
            present = surface.exists(assertion.target)
        except SurfaceError as exc:
            return AssertionResult(False, f"presence of {desc}", str(exc))
        return AssertionResult(
            present, f"presence of {desc}", "present" if present else "not found"
        )

    if isinstance(assertion, AllOf):
        children = [evaluate(surface, child) for child in assertion.of]
        ok = all(c.ok for c in children)
        failed = next((c for c in children if not c.ok), None)
        return AssertionResult(
            ok,
            f"all of {len(children)} conditions",
            "all held" if ok else f"failed: {failed.describe() if failed else '?'}",
            children,
        )

    if isinstance(assertion, AnyOf):
        children = [evaluate(surface, child) for child in assertion.of]
        ok = any(c.ok for c in children)
        return AssertionResult(
            ok,
            f"any of {len(children)} conditions",
            "one held" if ok else "none held",
            children,
        )

    raise TypeError(f"unsupported assertion {type(assertion).__name__}")


def _text(
    surface: Surface,
    frame: FrameRef,
    needle: str,
    case_sensitive: bool,
    want_present: bool,
) -> AssertionResult:
    verb = "text present" if want_present else "text absent"
    try:
        body = surface.frame_text(frame)
    except SurfaceError as exc:
        return AssertionResult(False, f"{verb}: {needle!r}", str(exc))

    haystack = _normalise(body)
    probe = needle if case_sensitive else needle.lower()
    hay = haystack if case_sensitive else haystack.lower()
    found = probe in hay

    return AssertionResult(
        ok=(found == want_present),
        expected=f"{verb}: {needle!r}",
        observed=_excerpt(body, needle),
    )


def wait_until(
    surface: Surface,
    assertion: Assertion,
    timeout_ms: int = 10000,
    poll_ms: int = 150,
) -> AssertionResult:
    """Poll an assertion until it holds or the deadline passes.

    This is the only waiting primitive in the system, and it is deliberately a
    predicate rather than a duration. Fixed sleeps are the classic source of
    replay flake: too short and the run is nondeterministic, too long and
    every one of thousands of invocations pays for the worst case. Polling a
    condition means a fast surface finishes fast and a slow one still
    succeeds, which is what makes the same artifact deterministic across
    environments.

    Returns the last result either way, so a timeout still reports what was
    observed rather than merely that time ran out.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    result = evaluate(surface, assertion)
    while not result.ok and time.monotonic() < deadline:
        time.sleep(poll_ms / 1000)
        result = evaluate(surface, assertion)
    return result


def holds(surface: Surface, assertion: Assertion) -> bool:
    """Convenience for callers that genuinely only need the boolean."""
    return evaluate(surface, assertion).ok
