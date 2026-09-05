"""The capability catalog (brief 3.2).

A directory of artifact files, presented as a set of callable capabilities: a
name, a description, a typed input schema, a typed output shape, and whether
the capability is cleared to run unattended. An agent picks one by name and
supplies typed arguments; it never learns that a browser was involved.

Two things this module deliberately does *not* do.

It does not define a new contract. The input schema is generated from the
``ParamSpec`` list the artifact already carries, and an invocation returns the
``ReplayResult`` union the replay engine already produces -- success, business
outcome, or failure with debuggable detail. Inventing a second contract to sit
in front of the first would be a translation layer with nothing to translate.

It does not execute anything. Running a capability belongs to the replay
engine, reached through ``service.runner``. The catalog only answers "what can
be called, and with what".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cua.schema.capability import Capability, ParamSpec

DEFAULT_ARTIFACT_DIR = Path("artifacts")

_JSON_TYPES = {
    "string": "string",
    "number": "number",
    "money": "number",
    "boolean": "boolean",
}


# Inputs that identify the signed-in operator rather than the request. The
# console has one operator signed on at a time, so these come from the
# environment, not from whoever is typing.
_OPERATOR_INPUTS = frozenset({"operator_id", "operator_password", "operator_passphrase"})


def server_supplied(capability: Capability) -> set[str]:
    """Inputs the service fills in, so nobody is asked for them.

    Secrets, and the operator identity that goes with them. Signing on as one
    operator with another's id is not a request a caller should be able to
    make in passing, and a password is not something a conversation should
    ever carry -- so both are decided by the service, once.
    """
    return {
        spec.name
        for spec in capability.inputs
        if spec.secret or spec.name in _OPERATOR_INPUTS
    }


def operator_credentials(capability: Capability) -> dict[str, str]:
    """The signed-in operator's credentials for one capability.

    Read from the environment at invocation, after planning. The planner
    proposed a capability and its business arguments; it never saw these and
    could not have supplied them.
    """
    env = {
        "operator_id": os.environ.get("CUA_OPERATOR_ID", ""),
        "operator_password": os.environ.get("CUA_OPERATOR_PASSWORD", ""),
        "operator_passphrase": os.environ.get("CUA_OPERATOR_PASSWORD", ""),
    }
    supplied = server_supplied(capability)
    return {
        name: env[name] for name in supplied if env.get(name)
    }


class CapabilityNotFound(KeyError):
    """No capability is registered under that name."""


class CapabilityNotApproved(PermissionError):
    """The capability exists but is not cleared to run unattended.

    Raised rather than silently running, because the whole point of the
    approval state is that something a human has not signed off on does not
    execute just because it was asked for through a different door.
    """


@dataclass(slots=True)
class CatalogEntry:
    """One capability, as a caller sees it."""

    capability: Capability

    @property
    def name(self) -> str:
        return self.capability.id

    @property
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the arguments, derived from the declared inputs.

        Emitted in the shape a tool-calling model expects, so the same
        description serves an HTTP client and an LLM without a second
        definition drifting away from the first.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for spec in self.capability.inputs:
            properties[spec.name] = _property_for(spec)
            if spec.required:
                required.append(spec.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                out.name: {
                    "type": _JSON_TYPES.get(out.type, "string"),
                    "description": out.description,
                }
                for out in self.capability.outputs
            },
        }

    def describe(self) -> dict[str, Any]:
        """The catalog listing for one capability."""
        cap = self.capability
        return {
            "name": cap.name,
            "capability_id": cap.id,
            "version": cap.version,
            "description": cap.description,
            "approval": cap.approval,
            "max_risk": cap.max_risk,
            "invocable_unattended": cap.approval == "approved",
            "product": cap.app.product,
            "product_version": cap.app.product_version,
            # The application this drives. A catalog can hold capabilities for
            # more than one target, and a caller deserves to know which.
            "entry_url": cap.app.entry_url,
            # Inputs the service fills in rather than the caller: the operator
            # identity and its credential. Published so a UI knows not to ask
            # and an agent knows not to try.
            "server_supplied": sorted(server_supplied(cap)),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            # What the caller may get back other than success. Publishing the
            # outcome codes is what lets an agent branch without parsing prose.
            # Deduplicated by code: several signals may detect one condition on
            # different screens, but the caller branches on the code, so the
            # catalog advertises each code once.
            "business_outcomes": _outcomes(cap),
            "steps": len(cap.steps),
            "tags": cap.tags,
        }


def _outcomes(capability: Capability) -> list[dict[str, str]]:
    """The distinct business outcomes a caller may receive.

    First description wins for a given code. Signal order in the artifact is
    the order they are evaluated, so the first is the primary phrasing of the
    condition and the later ones are alternate detections of it.
    """
    seen: dict[str, str] = {}
    for signal in capability.signals:
        if signal.classification != "business_outcome" or not signal.outcome_code:
            continue
        seen.setdefault(signal.outcome_code, signal.description)
    return [{"code": code, "meaning": meaning} for code, meaning in seen.items()]


def _property_for(spec: ParamSpec) -> dict[str, Any]:
    prop: dict[str, Any] = {
        "type": _JSON_TYPES.get(spec.type, "string"),
        "description": spec.description or f"{spec.name} for this invocation.",
    }
    if spec.pattern:
        prop["pattern"] = spec.pattern
    # An example is only ever published for a non-secret input; ParamSpec
    # forbids one on a secret, and repeating that here keeps a credential out
    # of the catalog even if an artifact were hand-edited.
    if spec.example and not spec.secret:
        prop["examples"] = [spec.example]
    if spec.secret:
        prop["writeOnly"] = True
        prop["description"] += " Credential: supplied per invocation, never stored."
    return prop


class CapabilityCatalog:
    """Everything the system knows how to do, loaded from artifact files."""

    def __init__(self, directory: Path | str = DEFAULT_ARTIFACT_DIR) -> None:
        self.directory = Path(directory)
        self._entries: dict[str, CatalogEntry] = {}
        self.errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read the directory.

        A malformed artifact is recorded rather than raised: one bad file
        should not take the whole catalog offline, and the reason belongs
        somewhere a reviewer can see it.
        """
        self._entries.clear()
        self.errors.clear()
        for path in sorted(self.directory.glob("*.json")):
            try:
                capability = Capability.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                self.errors[path.name] = str(exc).splitlines()[0][:200]
                continue
            self._entries[capability.id] = CatalogEntry(capability=capability)

    # -- lookup ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def names(self) -> list[str]:
        return sorted(self._entries)

    def entry(self, name: str) -> CatalogEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise CapabilityNotFound(
                f"no capability named {name!r}; known: {', '.join(self.names())}"
            ) from None

    def get(self, name: str) -> Capability:
        return self.entry(name).capability

    def resolve_for_invocation(self, name: str, allow_draft: bool = False) -> Capability:
        """The capability to run, refusing anything not cleared for it.

        ``allow_draft`` exists so a supervised run can exercise a draft on
        purpose -- recording a capability that posts a transfer leaves it a
        draft, and someone has to be able to try it. It defaults to off, so
        the ordinary path through the API cannot reach one by accident.
        """
        capability = self.get(name)
        if capability.approval != "approved" and not allow_draft:
            raise CapabilityNotApproved(
                f"{name!r} is {capability.approval}, not approved for unattended "
                f"use (max risk: {capability.max_risk}). A capability that posts "
                "an irreversible transaction is not auto-verified, because "
                "verifying it would perform the transaction again -- it stays a "
                "draft until a person approves it."
            )
        return capability

    def listing(self, product: str | None = None) -> list[dict[str, Any]]:
        entries = self._entries.values()
        if product:
            entries = [e for e in entries if e.capability.app.product == product]
        return sorted(
            (e.describe() for e in entries), key=lambda d: str(d["capability_id"])
        )

    def products(self) -> list[str]:
        return sorted({e.capability.app.product for e in self._entries.values()})
