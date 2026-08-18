"""Primitives shared across the capability schema.

Everything here is a Pydantic model so the whole artifact round-trips through
JSON and validates on load. Unions are *tagged* (discriminated on a ``kind``
literal) rather than structural: a reviewer reading the JSON can tell what a
node is from its first field, and Pydantic gives a precise error path instead
of "none of the 5 variants matched".
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Base model: reject unknown keys.

    An artifact is a contract. Silently ignoring a misspelled field is how a
    capability ends up quietly not doing what its author thought it did.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


class FrameRef(Strict):
    """Which document within the surface a target lives in.

    Legacy consoles are framesets, so "the page" is not a single document.
    Carrying the frame on every target -- rather than relying on ambient
    "current frame" state -- keeps steps independently resolvable, which is
    what lets replay resume mid-flow after a human handoff.
    """

    kind: Literal["top", "name", "url_glob"] = "top"
    value: str | None = Field(
        default=None,
        description="Frame name for kind=name, glob pattern for kind=url_glob.",
    )


# ---------------------------------------------------------------------------
# Values: literal, or bound to an invocation parameter
# ---------------------------------------------------------------------------


class LiteralValue(Strict):
    kind: Literal["literal"] = "literal"
    value: str


class ParamValue(Strict):
    """A value supplied by the caller at invocation time.

    Discovery produces concrete values ("10001"); the recorder promotes the
    ones the caller should control into parameters. Keeping the reference
    explicit -- rather than string-templating "{{member_id}}" into a literal --
    means the schema can validate that every referenced parameter is declared.
    """

    kind: Literal["param"] = "param"
    param: str


ValueRef = Annotated[Union[LiteralValue, ParamValue], Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Assertions: the vocabulary for checkpoints, signal detection and extraction
# ---------------------------------------------------------------------------


class TextPresent(Strict):
    kind: Literal["text_present"] = "text_present"
    frame: FrameRef = Field(default_factory=FrameRef)
    text: str
    case_sensitive: bool = False


class TextAbsent(Strict):
    kind: Literal["text_absent"] = "text_absent"
    frame: FrameRef = Field(default_factory=FrameRef)
    text: str
    case_sensitive: bool = False


class UrlMatches(Strict):
    kind: Literal["url_matches"] = "url_matches"
    frame: FrameRef = Field(default_factory=FrameRef)
    pattern: str = Field(description="Python regex matched against the frame URL.")


class ElementPresent(Strict):
    """Presence of a control identified by the ranked-target machinery.

    Declared lazily as a forward reference because targets are defined in
    ``targets.py``, which imports from this module.
    """

    kind: Literal["element_present"] = "element_present"
    target: "Target"


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    of: list["Assertion"]


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    of: list["Assertion"]


Assertion = Annotated[
    Union[TextPresent, TextAbsent, UrlMatches, ElementPresent, AllOf, AnyOf],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Risk classification (brief 3.4)
# ---------------------------------------------------------------------------


class RiskClass(str):
    """Marker namespace; the real type is the Literal below."""


Risk = Literal["safe", "reversible_write", "irreversible"]
"""How dangerous an action is if it fires when it should not.

safe
    Reads and navigation. No server-side state change.
reversible_write
    Writes that an operator could undo through the same UI (e.g. editing a
    draft field, setting a filter).
irreversible
    Creates a record, moves money, or sends something outward. In this system
    these never execute unattended without an explicit policy grant.
"""


Sensitivity = Literal["public", "internal", "restricted"]
"""Data classification, used to drive redaction (brief 3.4).

restricted
    Regulated financial data or PII. Never written to an artifact, a log, or a
    prompt in raw form.
"""


from .targets import Target  # noqa: E402  (circular by construction)

ElementPresent.model_rebuild()
AllOf.model_rebuild()
AnyOf.model_rebuild()
