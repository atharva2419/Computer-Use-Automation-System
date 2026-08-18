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
        for closer in (self._browser, self._pw):
            try:
                closer.close() if closer else None
            except Exception:  # noqa: BLE001 - teardown must not mask real errors
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

    # -- frames -----------------------------------------------------------

    def _frame(self, ref: FrameRef | None, timeout_ms: int = 5000) -> Frame:
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
        target = self._frame(frame)
        try:
            return target.locator("body").inner_text(timeout=2000)
        except (PWTimeout, PWError):
            # Frameset documents have no body.
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
                self._frame(ref).goto(url, wait_until="load")
        except PWTimeout as exc:
            raise SurfaceTimeout(f"navigation to {url} timed out") from exc
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
