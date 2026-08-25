"""A ``Surface`` over a live browser, driven with Playwright.

This is the only module in the system that knows Playwright exists.

Two implementation choices are worth calling out, because both were forced by
what the target console actually looks like rather than chosen in the
abstract:

**Perception is the accessibility tree, not the DOM.** ``aria_snapshot()`` per
frame yields roles, accessible names and values -- a few hundred tokens of
meaning instead of tens of thousands of tokens of table markup. It is what an
operator perceives, it is what the locator strategies address, and it is the
representation a desktop accessibility API would also give us.

**Ranked strategies share a deadline, rather than each owning one.** Trying
five strategies at five seconds apiece would mean a 25-second failure. Instead
each strategy gets a short probe and the whole ranked list is retried until one
overall deadline expires. A control that appears late is still found by the
preferred strategy, and a genuinely missing control fails fast.
"""

from __future__ import annotations

import fnmatch
import re
import time
from typing import Any

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Locator, Page, TimeoutError as PWTimeout
from playwright.sync_api import Browser, Playwright, sync_playwright

from ..schema.common import FrameRef
from ..schema.targets import (
    CssStrategy,
    LabelCellStrategy,
    RoleNameStrategy,
    RowScopedCellStrategy,
    Target,
    TextStrategy,
)
from .base import (
    FrameNotFound,
    FrameView,
    Observation,
    ReadSource,
    Resolution,
    StrategyAttempt,
    SurfaceTimeout,
    SurfaceUnavailable,
    TargetNotResolved,
)

# A single ranked strategy gets this long to match before we move to the next
# one. Small on purpose: it is a probe, not the real timeout.
PROBE_MS = 400


def _xpath_literal(value: str) -> str:
    """Quote a string for XPath, which has no escape character."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


class PlaywrightWebSurface:
    """Drives one browser page, including its frames."""

    def __init__(
        self,
        headless: bool = True,
        slow_mo_ms: int = 0,
        viewport: tuple[int, int] = (1280, 900),
    ) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._headless = headless
        self._activity: list[str] | None = None
        self._activity_seen: dict[str, str] = {}
        self._slow_mo = slow_mo_ms
        self._viewport = viewport

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "PlaywrightWebSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless, slow_mo=self._slow_mo
        )
        context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]}
        )
        self._page = context.new_page()
        return self

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("surface not started; call start() first")
        return self._page

    def close(self) -> None:
        """Tear down the browser and the Playwright driver.

        The driver needs ``stop()``, not ``close()``. Getting that wrong leaks
        the event loop that ``sync_playwright().start()`` created, and the next
        surface in the same thread fails with "Sync API inside the asyncio
        loop" -- a confusing error a long way from its cause. Each half is
        guarded separately so a failure in one still releases the other.
        """
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - teardown must not mask real errors
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._page = self._browser = self._pw = None

    def settle(self, timeout_ms: int = 10000) -> None:
        """Wait for the surface, *including every child frame*, to go quiet.

        The page-level wait alone is not enough on a frameset. Clicking a link
        inside the ``main`` frame navigates only that frame; the top document
        is already loaded and the network can read as idle before the child
        has committed its new document. Observing at that moment yields the
        previous screen -- a race that shows up as an intermittently wrong
        frame URL, which is exactly the class of flake that makes a replay
        engine untrustworthy.

        Best-effort by design: a surface that never goes idle is still
        observable, and the caller's checkpoint is the real synchronisation
        point. This just removes the common races cheaply.
        """
        deadline = time.monotonic() + timeout_ms / 1000

        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except (PWTimeout, PWError):
            pass

        for frame in list(self.page.frames):
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                return
            try:
                frame.wait_for_load_state("load", timeout=remaining)
            except (PWTimeout, PWError):
                # Frame detached mid-wait, or never settles. Neither is fatal.
                continue

    @property
    def headless(self) -> bool:
        """Whether there is a window a human could actually take over.

        A handoff to an operator is meaningless on a headless browser, so the
        escalation path checks this up front rather than pausing forever in
        front of an invisible session.
        """
        return self._headless

    # -- activity recording (optional capability) --------------------------

    def start_activity_log(self) -> None:
        """Begin recording navigations this surface did not initiate.

        Sampling, not event subscription. The obvious implementation hooks
        ``page.on("framenavigated")``, and it does not work for this use case
        for two independent reasons, both verified:

        1. Sync Playwright only dispatches events when the caller re-enters
           the library. A handoff blocks on operator input, so nothing
           re-enters and the queued events never fire -- the trail comes back
           empty from a session the human demonstrably used.
        2. ``frame.url`` read inside the callback is the *previous* document,
           the same frameset staleness that makes the cached property
           unreliable everywhere else. A wrong URL is worse than none.

        Sampling sidesteps both: ``poll_activity`` re-enters Playwright (which
        is required anyway) and reads the live URL from the document. The
        trade-off is that navigations between two polls are missed -- fine
        here, because this records a person clicking, not a redirect chain.
        """
        self._activity = []
        self._activity_seen = self._frame_urls()

    def poll_activity(self, settle_ms: int = 150) -> list[str]:
        """Sample the surface and append anything that moved.

        Must be called periodically while activity is being recorded. Doubles
        as the pump that lets Playwright process whatever else is pending.
        """
        if self._activity is None:
            return []
        try:
            self.page.wait_for_timeout(settle_ms)
        except PWError:
            return self._activity

        current = self._frame_urls()
        for name, url in current.items():
            if self._activity_seen.get(name) != url:
                self._activity.append(f"navigated {name} -> {url}")
        self._activity_seen = current
        return self._activity

    def stop_activity_log(self) -> list[str]:
        self.poll_activity()
        recorded, self._activity = self._activity or [], None
        self._activity_seen = {}
        return recorded

    def _frame_urls(self) -> dict[str, str]:
        try:
            return {
                (frame.name or "top"): self._live_url(frame)
                for frame in self.page.frames
            }
        except PWError:
            return {}

    # -- frames -----------------------------------------------------------

    def _frame(self, ref: FrameRef | None, timeout_ms: int = 0) -> Frame:
        """Find a frame. Does *not* wait by default -- deliberately.

        Every caller that needs a frame is already inside a polling loop: the
        engine races checkpoints against signals, and ``resolve`` retries its
        ranked strategies against a deadline. A blocking wait in here would be
        a second, nested retry, and the costs multiply.

        Concretely: the sign-on page has no ``main`` frame, and a capability
        may declare several signals that watch it. At a 5s wait per lookup one
        signal sweep costs 25 seconds. Non-blocking lookup plus a single outer
        loop is both faster and easier to reason about.

        ``navigate`` passes an explicit timeout, because there a frame genuinely
        must exist before the call can mean anything.
        """
        ref = ref or FrameRef()
        deadline = time.monotonic() + timeout_ms / 1000

        while True:
            if ref.kind == "top":
                return self.page.main_frame
            if ref.kind == "name":
                found = self.page.frame(name=ref.value or "")
                if found is not None:
                    return found
            elif ref.kind == "url_glob":
                for frame in self.page.frames:
                    if fnmatch.fnmatch(self._live_url(frame), ref.value or "*"):
                        return frame
            if time.monotonic() > deadline:
                raise FrameNotFound(ref)
            self.page.wait_for_timeout(100)

    def _live_url(self, frame: Frame) -> str:
        """The frame's real current URL, read from the browser.

        ``Frame.url`` is a cached property, and inside a legacy ``<frameset>``
        Playwright does not reliably refresh it: after a link click in a child
        frame it keeps reporting the *previous* document indefinitely, while
        that frame's content and locators are already the new one. Verified
        directly -- ``Frame.url`` said ``/frame/search`` while
        ``location.href`` in the same frame said ``/frame/member/10001``.

        Trusting the cached value would make every URL-based checkpoint and
        signal silently wrong by exactly one navigation, which on a
        record-once/replay-many system is the worst possible kind of bug: it
        passes during recording and misfires in production. So we ask the
        document itself, and fall back to the cached value only when the frame
        cannot be evaluated in (detached, or cross-origin).
        """
        try:
            return str(frame.evaluate("() => location.href"))
        except PWError:
            return frame.url

    def frame_url(self, frame: FrameRef) -> str:
        return self._live_url(self._frame(frame))

    def frame_text(self, frame: FrameRef) -> str:
        """Visible text of one frame, or '' if it has none.

        The presence check before the read is not a micro-optimisation. A
        ``<frameset>`` document has no ``<body>`` at all, so reading one waits
        out the full locator timeout and returns nothing. Signals are evaluated
        on every poll of every step, so a single assertion that happens to
        watch the top document was adding two seconds *per step* -- invisible
        as a bug, obvious as a 2s-per-step tax. ``count()`` answers immediately
        and the polling loop supplies the retry.
        """
        target = self._frame(frame)
        try:
            body = target.locator("body")
            if body.count() == 0:
                return ""
            return body.first.inner_text(timeout=2000)
        except (PWTimeout, PWError):
            return ""

    # -- perception -------------------------------------------------------

    def observe(self) -> Observation:
        views: list[FrameView] = []
        for frame in self.page.frames:
            views.append(
                FrameView(
                    name=frame.name or "",
                    url=self._live_url(frame),
                    title=self._frame_title(frame),
                    aria=self._aria(frame),
                )
            )
        return Observation(url=self.page.url, frames=views)

    def _frame_title(self, frame: Frame) -> str:
        try:
            return frame.title()
        except PWError:
            return ""

    def _aria(self, frame: Frame) -> str:
        """Accessibility-tree rendering of one frame, or '' if it has none."""
        for root in ("body", "html"):
            try:
                locator = frame.locator(root)
                if locator.count() == 0:
                    continue
                snapshot = locator.first.aria_snapshot(timeout=2000)
                if snapshot.strip():
                    return snapshot
            except (PWTimeout, PWError):
                continue
        return ""

    def screenshot(self) -> bytes:
        try:
            return self.page.screenshot()
        except PWError:
            return b""

    # -- targeting --------------------------------------------------------

    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolution:
        deadline = time.monotonic() + timeout_ms / 1000
        attempts: list[StrategyAttempt] = []

        while True:
            attempts = []
            for rank, strategy in enumerate(target.strategies):
                try:
                    locator = self._locator(target.frame, strategy)
                except FrameNotFound as exc:
                    attempts.append(StrategyAttempt(rank, strategy.kind, 0, str(exc)))
                    continue

                try:
                    count = self._probe(locator)
                except (PWTimeout, PWError) as exc:
                    attempts.append(
                        StrategyAttempt(rank, strategy.kind, 0, _brief(exc))
                    )
                    continue

                if count == 0:
                    attempts.append(StrategyAttempt(rank, strategy.kind, 0, "no match"))
                    continue

                chosen, ambiguous = self._disambiguate(locator, strategy, count)
                attempts.append(
                    StrategyAttempt(
                        rank, strategy.kind, count, "matched" if not ambiguous else
                        f"matched {count}, narrowed to innermost"
                    )
                )
                return Resolution(
                    rank=rank,
                    kind=strategy.kind,
                    handle=chosen,
                    ambiguous=ambiguous,
                    attempts=attempts,
                )

            if time.monotonic() > deadline:
                raise TargetNotResolved(target, attempts)
            self.page.wait_for_timeout(150)

    def _probe(self, locator: Locator) -> int:
        """How many elements match, waiting only briefly."""
        try:
            locator.first.wait_for(state="attached", timeout=PROBE_MS)
        except (PWTimeout, PWError):
            return 0
        return locator.count()

    def _disambiguate(
        self, locator: Locator, strategy: Any, count: int
    ) -> tuple[Locator, bool]:
        """Pick one element when a strategy matches several.

        Nested tables make over-matching the norm rather than the exception:
        an outer row wrapping an inner table contains every inner row's text,
        so a text filter hits both. The innermost element -- the one with the
        least text of its own -- is virtually always the control the author
        meant. Text-free controls (inputs) all tie at zero, so the first wins.
        """
        if count == 1:
            return locator.first, False

        if isinstance(strategy, (RoleNameStrategy, CssStrategy)):
            return locator.first, True

        best_index, best_len = 0, None
        for i in range(min(count, 12)):
            try:
                length = len(locator.nth(i).inner_text(timeout=500))
            except (PWTimeout, PWError):
                continue
            if best_len is None or length < best_len:
                best_index, best_len = i, length
        return locator.nth(best_index), True

    def _locator(self, frame_ref: FrameRef, strategy: Any) -> Locator:
        frame = self._frame(frame_ref)

        if isinstance(strategy, RoleNameStrategy):
            return frame.get_by_role(
                strategy.role, name=strategy.name, exact=strategy.exact  # type: ignore[arg-type]
            )

        if isinstance(strategy, LabelCellStrategy):
            return self._label_cell(frame, strategy)

        if isinstance(strategy, RowScopedCellStrategy):
            row = self._innermost_row(frame, strategy.row_contains, strategy.exact_row_text)
            return row.get_by_role("cell").nth(strategy.cell_index)

        if isinstance(strategy, TextStrategy):
            if strategy.element_role:
                return frame.get_by_role(strategy.element_role).filter(  # type: ignore[arg-type]
                    has_text=_has_text(strategy.text, strategy.exact)
                )
            return frame.get_by_text(strategy.text, exact=strategy.exact)

        if isinstance(strategy, CssStrategy):
            return frame.locator(strategy.css)

        raise TypeError(f"unsupported strategy {type(strategy).__name__}")

    def _label_cell(self, frame: Frame, strategy: LabelCellStrategy) -> Locator:
        """Find a control labelled only by neighbouring cell text.

        This is the strategy that makes the legacy console automatable at all:
        its inputs have no accessible name, so role+name cannot address them,
        but a human finds them by reading the label to their left.
        """
        if strategy.scope == "row":
            row = self._innermost_row(frame, strategy.label, strategy.exact_label)
            return row.get_by_role(strategy.control).nth(strategy.occurrence)  # type: ignore[arg-type]

        literal = _xpath_literal(strategy.label)
        predicate = (
            f"normalize-space(.)={literal}"
            if strategy.exact_label
            else f"contains(normalize-space(.), {literal})"
        )
        cell = frame.locator(
            f"xpath=.//td[{predicate}]/following-sibling::td[1] "
            f"| .//th[{predicate}]/following-sibling::td[1]"
        )
        return cell.get_by_role(strategy.control).nth(strategy.occurrence)  # type: ignore[arg-type]

    def _innermost_row(self, frame: Frame, text: str, exact: bool) -> Locator:
        rows = frame.get_by_role("row").filter(has_text=_has_text(text, exact))
        count = rows.count()
        if count <= 1:
            return rows.first
        best_index, best_len = 0, None
        for i in range(min(count, 12)):
            try:
                length = len(rows.nth(i).inner_text(timeout=500))
            except (PWTimeout, PWError):
                continue
            if best_len is None or length < best_len:
                best_index, best_len = i, length
        return rows.nth(best_index)

    def exists(self, target: Target, timeout_ms: int = 1000) -> bool:
        try:
            self.resolve(target, timeout_ms=timeout_ms)
        except (TargetNotResolved, FrameNotFound):
            return False
        return True

    # -- action -----------------------------------------------------------

    def navigate(self, url: str, frame: FrameRef | None = None) -> None:
        ref = frame or FrameRef()
        try:
            if ref.kind == "top":
                self.page.goto(url, wait_until="load")
            else:
                self._frame(ref, timeout_ms=5000).goto(url, wait_until="load")
        except PWTimeout as exc:
            raise SurfaceTimeout(f"navigation to {url} timed out") from exc
        except PWError as exc:
            # Connection refused, DNS failure, TLS error. The application is
            # not reachable -- which is the app's problem, not a defect in the
            # artifact, and the failure report should say so.
            raise SurfaceUnavailable(f"navigation to {url} failed: {_brief(exc)}") from exc
        self.settle()

    def click(self, target: Target, timeout_ms: int = 5000) -> Resolution:
        resolution = self.resolve(target, timeout_ms)
        self._do(lambda: resolution.handle.click(timeout=timeout_ms), target)
        self.settle()
        return resolution

    def fill(
        self, target: Target, text: str, clear_first: bool = True, timeout_ms: int = 5000
    ) -> Resolution:
        resolution = self.resolve(target, timeout_ms)

        def action() -> None:
            if clear_first:
                resolution.handle.fill(text, timeout=timeout_ms)
            else:
                resolution.handle.type(text, timeout=timeout_ms)

        self._do(action, target)
        return resolution

    def select(self, target: Target, value: str, timeout_ms: int = 5000) -> Resolution:
        resolution = self.resolve(target, timeout_ms)
        self._do(
            lambda: resolution.handle.select_option(value, timeout=timeout_ms), target
        )
        return resolution

    def press(self, target: Target, key: str, timeout_ms: int = 5000) -> Resolution:
        resolution = self.resolve(target, timeout_ms)
        self._do(lambda: resolution.handle.press(key, timeout=timeout_ms), target)
        self.settle()
        return resolution

    def read(
        self,
        target: Target,
        source: ReadSource = "text",
        attribute: str | None = None,
        timeout_ms: int = 5000,
    ) -> tuple[str, Resolution]:
        resolution = self.resolve(target, timeout_ms)
        handle: Locator = resolution.handle
        try:
            if source == "text":
                value = handle.inner_text(timeout=timeout_ms)
            elif source == "value":
                value = handle.input_value(timeout=timeout_ms)
            else:
                if not attribute:
                    raise ValueError("source='attribute' requires an attribute name")
                value = handle.get_attribute(attribute, timeout=timeout_ms) or ""
        except PWTimeout as exc:
            raise SurfaceTimeout(
                f"reading {target.described_as!r} timed out"
            ) from exc
        return value, resolution

    def _do(self, action: Any, target: Target) -> None:
        try:
            action()
        except PWTimeout as exc:
            raise SurfaceTimeout(
                f"acting on {target.described_as!r} timed out"
            ) from exc


def _has_text(text: str, exact: bool) -> Any:
    """Playwright ``has_text`` value: substring, or an anchored regex if exact."""
    if not exact:
        return text
    return re.compile(rf"^\s*{re.escape(text)}\s*$")


def _brief(exc: Exception) -> str:
    return str(exc).splitlines()[0][:160]
