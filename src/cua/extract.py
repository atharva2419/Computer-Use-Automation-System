"""Reading declared outputs off the surface and normalising them.

An output is not "whatever text happened to be at that spot". It has a
declared type, a locator with the same ranked-fallback machinery the actions
use, and a transform that turns what a legacy screen renders into something a
calling agent can use. ``4,210.55`` on screen becomes the number ``4210.55``
in the result, because a caller comparing balances should not have to know how
this particular console formats money.
"""

from __future__ import annotations

import re
from typing import Any

from .schema.capability import OutputSpec, Transform
from .surface.base import Resolution, Surface, SurfaceError

_MONEY_CHARS = re.compile(r"[^0-9.\-]")


class ExtractionError(RuntimeError):
    """A required output could not be read."""


def extract(surface: Surface, spec: OutputSpec) -> tuple[Any, Resolution | None]:
    """Read one declared output. Returns ``(value, resolution)``.

    A missing *optional* output yields ``None`` rather than failing the run:
    plenty of real screens omit a field when it does not apply, and that is
    information, not a fault. A missing *required* output is a genuine failure,
    because the caller's contract said it would be there.
    """
    try:
        raw, resolution = surface.read(
            spec.extract.target,
            source=spec.extract.source,
            attribute=spec.extract.attribute,
        )
    except SurfaceError as exc:
        if spec.required:
            raise ExtractionError(
                f"required output {spec.name!r} could not be read: {exc}"
            ) from exc
        return None, None

    return _apply(spec, raw), resolution


def _apply(spec: OutputSpec, raw: str) -> Any:
    text = _transform(spec.extract.transform, raw)

    if spec.type in ("number", "money"):
        cleaned = _MONEY_CHARS.sub("", text)
        if not cleaned or cleaned in {"-", "."}:
            raise ExtractionError(
                f"output {spec.name!r} is typed {spec.type} but read {raw!r}"
            )
        try:
            return float(cleaned)
        except ValueError:
            raise ExtractionError(
                f"output {spec.name!r} is typed {spec.type} but read {raw!r}"
            ) from None

    if spec.type == "boolean":
        return text.strip().lower() in {"true", "yes", "y", "1"}

    return text


def _transform(transform: Transform, raw: str) -> str:
    if transform.kind == "none":
        return raw
    if transform.kind == "strip":
        return raw.strip()
    if transform.kind in ("money", "number"):
        return _MONEY_CHARS.sub("", raw.strip())
    if transform.kind == "regex":
        if not transform.pattern:
            raise ExtractionError("regex transform requires a pattern")
        match = re.search(transform.pattern, raw)
        if not match:
            raise ExtractionError(
                f"regex /{transform.pattern}/ did not match {raw.strip()[:60]!r}"
            )
        return match.group(1) if match.groups() else match.group(0)
    raise ExtractionError(f"unsupported transform {transform.kind!r}")
