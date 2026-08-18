"""How a capability identifies a control on the surface.

Design note (brief 3.2 asks for "reasoning about robustness")
-------------------------------------------------------------
A single selector is the wrong unit. Probing the target console showed why:

  * ``<input type=submit value="Search">`` exposes ``button "Search"`` -- a
    role+name target works and is highly stable.
  * ``<input type=text name="f2">`` in a table cell, labelled only by the text
    in the *previous cell*, exposes a bare ``textbox`` with **no accessible
    name**. Role+name cannot address it at all.
  * Navigation is ``<td onclick=...>`` -- role ``cell``, not button or link.

So a target is a *ranked list* of strategies plus a human-readable
description. Replay tries them in order and records which one resolved. That
gives three things at once: it works on hostile markup, it degrades instead of
breaking when the preferred strategy stops matching, and the "which rank
resolved" signal is a cheap drift detector -- if production replays start
falling through to rank 2, the surface changed.

Strategies are ordered by *semantic durability*: how likely the thing they
key on is to survive a re-theme, a version bump, or a per-tenant
configuration. Accessible role and name track what the control *means*;
CSS paths track how it happens to be built today.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleNameStrategy(_Strict):
    """Accessibility role + accessible name. Most durable; try first.

    Survives re-theming, layout changes and markup rewrites, because it keys
    on what the control means to a user rather than how it is built. This is
    the only strategy that would port unchanged to a desktop surface, where
    the OS accessibility API exposes the same role/name pair.
    """

    kind: Literal["role_name"] = "role_name"
    role: str = Field(description="ARIA role, e.g. button, link, textbox.")
    name: str = Field(description="Accessible name.")
    exact: bool = True


class LabelCellStrategy(_Strict):
    """A control labelled only by adjacent table-cell text.

    The workhorse for legacy layouts: locate the cell whose text is the label,
    then take the control of the requested role within the same row (or the
    following cell). Durable against restyling and against the field's ``name``
    attribute changing, because it keys on the visible label an operator reads.
    Vulnerable to relabelling and to translation -- which is exactly the kind
    of per-tenant variation the overlay mechanism exists to absorb.
    """

    kind: Literal["label_cell"] = "label_cell"
    label: str = Field(description="Visible text of the labelling cell.")
    control: str = Field(
        default="textbox",
        description="ARIA role of the control to find, e.g. textbox, combobox.",
    )
    scope: Literal["row", "following_cell"] = "row"
    occurrence: int = Field(
        default=0, description="Index when the scope holds several matches."
    )
    exact_label: bool = False


class RowScopedCellStrategy(_Strict):
    """A cell picked positionally within the row that contains an anchor text.

    The read-oriented counterpart to ``label_cell``: "in the row that mentions
    Savings, take the third cell". Keys on a data value plus column position,
    which is how an operator reads a table. Used mainly for extraction.
    """

    kind: Literal["row_scoped_cell"] = "row_scoped_cell"
    row_contains: str
    cell_index: int = Field(description="Zero-based cell index within the row.")
    exact_row_text: bool = False


class TextStrategy(_Strict):
    """Any element carrying the given visible text.

    Needed for non-semantic controls -- the ``<td onclick>`` navigation items,
    which have no interactive role at all. Weaker than role+name because text
    can repeat, so ``element_role`` narrows it where possible.
    """

    kind: Literal["text"] = "text"
    text: str
    element_role: str | None = None
    exact: bool = True


class CssStrategy(_Strict):
    """Structural CSS. Last resort, and recorded as such.

    Included deliberately rather than banned: on the worst surfaces it is
    sometimes the only thing that works. Ranking it last and logging when it
    resolves means the system can report "this capability is relying on
    brittle targeting" instead of hiding it.
    """

    kind: Literal["css"] = "css"
    css: str


TargetStrategy = Annotated[
    Union[
        RoleNameStrategy,
        LabelCellStrategy,
        RowScopedCellStrategy,
        TextStrategy,
        CssStrategy,
    ],
    Field(discriminator="kind"),
]


# Semantic durability ranking, used to sort strategies and to score how
# robust a recorded target is. Lower is more durable.
STRATEGY_DURABILITY: dict[str, int] = {
    "role_name": 0,
    "label_cell": 1,
    "row_scoped_cell": 2,
    "text": 3,
    "css": 4,
}


class Target(_Strict):
    """A control, described several ways in decreasing order of durability."""

    described_as: str = Field(
        description="Human-readable description for reviewers, logs and "
        "escalation context, e.g. 'the Member ID input on the search form'."
    )
    frame: "FrameRef" = Field(default_factory=lambda: FrameRef())
    strategies: list[TargetStrategy] = Field(
        min_length=1,
        description="Ranked; replay tries them in order and records the hit.",
    )
    rationale: str = Field(
        default="",
        description="Why these strategies, and what would break them. Written "
        "at record time so a reviewer can judge the capability without "
        "replaying it.",
    )

    @property
    def durability(self) -> int:
        """Durability of the preferred strategy (lower is better)."""
        return STRATEGY_DURABILITY[self.strategies[0].kind]


from .common import FrameRef  # noqa: E402  (circular by construction)

Target.model_rebuild()
