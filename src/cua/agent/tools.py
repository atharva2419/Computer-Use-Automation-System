"""The actions the discovery model may take.

The tool surface is deliberately narrow and mirrors the capability schema
almost one-to-one. Two consequences follow, both intentional:

*   **The model can only express what an artifact can express.** There is no
    "run this JavaScript", no "click at these coordinates", no arbitrary
    selector. A trajectory is therefore always recordable -- the model cannot
    reach a goal by a route the replay engine could not reproduce.
*   **Every action is classifiable before it runs.** The guardrail reads the
    tool name and arguments and decides risk without executing anything.

Note ``value`` versus ``param`` on the typing tools. The model is never shown
a secret; it types one by *naming* the parameter, and the runtime substitutes
the real value on the way to the browser. A passphrase never enters a prompt
or a transcript.
"""

from __future__ import annotations

from typing import Any

FRAME_DESCRIPTION = (
    "Which document to act in. Use the frame name exactly as shown in the "
    "observation, or 'top' for the outermost document. Legacy consoles are "
    "framesets, so this matters on nearly every action."
)

EXPECT_TEXT_DESCRIPTION = (
    "Short, distinctive text you expect to see once this action has worked. "
    "It is verified against the resulting page and, if it holds, becomes the "
    "step's checkpoint on replay. A proposal that does not hold is discarded, "
    "so prefer something you can actually see over something plausible."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Load a URL in a frame. Used to reach the entry point.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "Why, in operator language."},
                "url": {"type": "string"},
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
                "expect_text": {"type": "string", "description": EXPECT_TEXT_DESCRIPTION},
            },
            "required": ["intent", "url"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click a control. Identify it EITHER by role plus accessible name "
            "(preferred, most durable) OR by visible text when the control has "
            "no interactive role -- which is common in legacy consoles, where "
            "menu items are often plain table cells."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
                "role": {"type": "string", "description": "e.g. button, link, cell."},
                "name": {"type": "string", "description": "Accessible name."},
                "text": {"type": "string", "description": "Visible text, when there is no role+name."},
                "expect_text": {"type": "string", "description": EXPECT_TEXT_DESCRIPTION},
            },
            "required": ["intent", "frame"],
        },
    },
    {
        "name": "fill",
        "description": (
            "Type into a text field. Identify it by its visible label -- these "
            "inputs usually have no accessible name. Supply EITHER value (a "
            "literal) OR param (the name of a declared input). Use param for "
            "anything secret: you are not shown those values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
                "label": {"type": "string", "description": "Visible label text."},
                "value": {"type": "string"},
                "param": {"type": "string", "description": "Name of a declared input."},
                "expect_text": {"type": "string", "description": EXPECT_TEXT_DESCRIPTION},
            },
            "required": ["intent", "frame", "label"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown, identified by its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
                "label": {"type": "string"},
                "value": {"type": "string", "description": "Option value to select."},
                "param": {"type": "string"},
                "expect_text": {"type": "string", "description": EXPECT_TEXT_DESCRIPTION},
            },
            "required": ["intent", "frame", "label"],
        },
    },
    {
        "name": "read",
        "description": (
            "Read a value off the screen AND declare it as an output of the "
            "capability. Use this for every piece of information the goal asks "
            "you to return. Locate it by the row it sits in plus the column "
            "position, which is how a person reads a table: row_contains='Savings', "
            "cell_index=2 means 'in the row mentioning Savings, the third cell'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
                "output_name": {
                    "type": "string",
                    "description": "snake_case name the caller will receive.",
                },
                "output_type": {
                    "type": "string",
                    "enum": ["string", "number", "money", "boolean"],
                },
                "description": {"type": "string"},
                "row_contains": {
                    "type": "string",
                    "description": "Distinctive text in the row holding the value.",
                },
                "cell_index": {
                    "type": "integer",
                    "description": "Zero-based cell position within that row.",
                },
            },
            "required": [
                "intent",
                "frame",
                "output_name",
                "output_type",
                "row_contains",
                "cell_index",
            ],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Look at the screen as pixels. The accessibility tree you are given "
            "each turn is more precise and much cheaper, so use this only when "
            "the tree genuinely does not tell you what you need."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "done",
        "description": (
            "Declare the goal achieved. Only call this when the information the "
            "goal asked for is on screen and you have read every output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success_text": {
                    "type": "string",
                    "description": (
                        "Distinctive text on the final screen that proves the "
                        "goal was reached. Verified before the capability is "
                        "recorded; if it does not hold, nothing is saved."
                    ),
                },
                "frame": {"type": "string", "description": FRAME_DESCRIPTION},
            },
            "required": ["summary", "success_text", "frame"],
        },
    },
    {
        "name": "stuck",
        "description": (
            "Stop and ask for a human. Use this when you cannot make progress, "
            "when the screen is not what you expected and you have no safe next "
            "move, or when proceeding would need a decision you should not make "
            "alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

ACTION_TOOLS = {"navigate", "click", "fill", "select"}
TERMINAL_TOOLS = {"done", "stuck"}


def tool_names() -> set[str]:
    return {tool["name"] for tool in TOOLS}
