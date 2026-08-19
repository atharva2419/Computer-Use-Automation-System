"""Validating and binding the parameters a caller supplies to a capability.

This runs *before* the browser opens. A caller that passes a malformed member
id should get a typed rejection in milliseconds, not a browser session, a
partially completed flow and an ambiguous timeout. Keeping the check here also
means the failure category is honestly ``invalid_input`` -- a caller-side
problem -- rather than something that looks like surface drift.
"""

from __future__ import annotations

import re
from typing import Any

from .schema.capability import Capability, ParamSpec
from .schema.common import LiteralValue, ParamValue, ValueRef


class ParamError(ValueError):
    """The supplied arguments do not satisfy the capability contract."""


def validate(capability: Capability, supplied: dict[str, Any]) -> dict[str, str]:
    """Check ``supplied`` against the declared inputs and normalise to strings.

    Values are normalised to text because that is what gets typed into a
    control. The declared ``type`` is a contract for the *caller* -- it says
    what the capability means by this input and lets a calling agent build a
    correct tool schema -- so it is enforced here and then flattened.
    """
    declared = {spec.name: spec for spec in capability.inputs}

    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise ParamError(
            f"unknown parameter(s) {unknown}; capability {capability.id!r} accepts "
            f"{sorted(declared)}"
        )

    bound: dict[str, str] = {}
    problems: list[str] = []

    for name, spec in declared.items():
        if name not in supplied or supplied[name] is None:
            if spec.required:
                problems.append(f"{name!r} is required")
            continue
        try:
            bound[name] = _coerce(spec, supplied[name])
        except ParamError as exc:
            problems.append(str(exc))

    if problems:
        raise ParamError("; ".join(problems))
    return bound


def _coerce(spec: ParamSpec, value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value).strip()

    if spec.type in ("number", "money"):
        try:
            float(text)
        except ValueError:
            raise ParamError(f"{spec.name!r} must be numeric, got {text!r}") from None
    elif spec.type == "boolean" and text.lower() not in {"true", "false"}:
        raise ParamError(f"{spec.name!r} must be a boolean, got {text!r}")

    if spec.pattern and not re.fullmatch(spec.pattern, text):
        # Deliberately does not echo the value for secrets.
        shown = "<redacted>" if spec.secret else repr(text)
        raise ParamError(
            f"{spec.name!r} does not match required pattern /{spec.pattern}/: {shown}"
        )

    return text


def resolve(value: ValueRef, bound: dict[str, str]) -> str:
    """Turn a recorded value reference into the concrete text to type."""
    if isinstance(value, LiteralValue):
        return value.value
    if isinstance(value, ParamValue):
        if value.param not in bound:
            raise ParamError(
                f"step references parameter {value.param!r}, which was not supplied"
            )
        return bound[value.param]
    raise TypeError(f"unsupported value reference {type(value).__name__}")


def secret_names(capability: Capability) -> set[str]:
    return {spec.name for spec in capability.inputs if spec.secret}
