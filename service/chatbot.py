"""The chatbot front door (brief 3.3).

A conversational way in: someone types what they want, the right capability is
invoked with typed arguments, and the structured result comes back in plain
language. The brief is explicit that this should stay small -- "a demo driver
over your API, not a second product" -- so this module does exactly three
things and stops.

Three decisions shape it.

**The model chooses a capability. It never narrates the outcome.** Planning is
a judgement call about intent, which is what a model is for. Reporting is not:
the run already produced a typed result, and asking a model to restate it adds
a step that can hallucinate a confirmation number that was never issued. So
the reply is templated from the structured result. That also keeps the core's
central claim honest end to end -- no model sits in the decision loop, and now
none sits in the reporting path either.

**The planner is never shown a credential.** Secret inputs are stripped from
the tool schemas the model sees, and filled in from the operator's environment
at invocation. A password has no business in a chat transcript, a model
request, or the conversation history this keeps.

**Planning is a seam, not a hard dependency.** ``Planner`` has two
implementations: one backed by Claude, one a deterministic keyword matcher.
The brief invites mocking a boundary cleanly when live model access is not
available, and a chatbot that still drives the whole system with no API key is
a better demo artifact than one that cannot start. The scripted planner is
also what the tests use, so the chat path is exercised without a paid call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .catalog import CapabilityCatalog, CatalogEntry

# Sonnet rather than Opus, matching the discovery agent's default (CUA_MODEL).
# Picking one of a handful of named capabilities is a much easier judgement
# than discovering a flow, and the planner is on the demo's critical path where
# latency shows. Override with CUA_CHAT_MODEL.
DEFAULT_CHAT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are the front door to a credit-union servicing system. Each tool is a \
capability that was recorded against the servicing console and now replays \
deterministically.

Choose exactly one tool when the request maps to one, and fill in only the \
arguments the user actually gave you. Do not invent a member number, an \
amount, or an account identifier -- if a required argument is missing, do not \
call a tool: reply asking for that one piece of information.

You will not be given credentials and must never ask for one. The operator is \
already signed in as far as you are concerned.

If no tool fits, say so plainly in one sentence."""


@dataclass
class Plan:
    """What the planner decided to do with one message."""

    capability: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reply: str = ""

    @property
    def invokes(self) -> bool:
        return self.capability is not None


class Planner(Protocol):
    """Turns a request into a capability call, or into a plain reply."""

    def plan(self, message: str, entries: list[CatalogEntry]) -> Plan: ...


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


# Inputs that identify the signed-in operator rather than the request. The
# console has one operator signed on at a time, so these come from the
# environment, not from whoever is typing.
_OPERATOR_INPUTS = frozenset({"operator_id", "operator_password", "operator_passphrase"})


def server_supplied(capability: Any) -> set[str]:
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


def operator_credentials(capability: Any) -> dict[str, str]:
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


def tool_definitions(entries: list[CatalogEntry]) -> list[dict[str, Any]]:
    """The catalog, in the shape the Messages API expects.

    Generated from the same ``ParamSpec`` list that produces the API's JSON
    Schema, so the chatbot and an HTTP caller are looking at one contract.

    Server-supplied inputs are removed rather than marked. A model cannot be
    careless with a field it was never told exists, and the alternative --
    describing the password and trusting the prompt not to ask for it -- makes
    a guarantee out of an instruction.
    """
    tools: list[dict[str, Any]] = []
    for entry in entries:
        secrets = server_supplied(entry.capability)
        schema = entry.input_schema
        tools.append(
            {
                "name": _tool_name(entry.name),
                "description": _tool_description(entry),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        k: v for k, v in schema["properties"].items() if k not in secrets
                    },
                    "required": [r for r in schema["required"] if r not in secrets],
                    "additionalProperties": False,
                },
                # The arguments are about to drive a real transaction, so they
                # are worth having validated against the schema rather than
                # coerced afterwards.
                "strict": True,
            }
        )
    return tools


def _tool_description(entry: CatalogEntry) -> str:
    """What the capability does, plus how it can legitimately not succeed."""
    cap = entry.capability
    parts = [cap.description]
    if cap.max_risk == "irreversible":
        parts.append(
            "This posts an irreversible transaction and stops for human "
            "approval before it completes."
        )
    codes = sorted({s.outcome_code for s in cap.signals if s.outcome_code})
    if codes:
        parts.append("May return a business outcome: " + ", ".join(codes) + ".")
    return " ".join(parts)


# Dotted capability ids are not valid tool names, so they are mapped both ways.
def _tool_name(capability_id: str) -> str:
    return capability_id.replace(".", "__")


def _capability_id(tool_name: str) -> str:
    return tool_name.replace("__", ".")


# ---------------------------------------------------------------------------
# Planners
# ---------------------------------------------------------------------------


class ClaudePlanner:
    """Picks a capability with one Messages API call."""

    def __init__(self, client: Any = None, model: str | None = None) -> None:
        self._client = client
        self.model = model or os.environ.get("CUA_CHAT_MODEL") or DEFAULT_CHAT_MODEL

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # imported late: the API runs without a key

            self._client = anthropic.Anthropic()
        return self._client

    def plan(self, message: str, entries: list[CatalogEntry]) -> Plan:
        # One call, no agentic loop. Choosing among a handful of named
        # capabilities is a single judgement, and a loop here would put a
        # model back in a decision path the core deliberately keeps it out of.
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tool_definitions(entries),
            messages=[{"role": "user", "content": message}],
        )
        text: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                return Plan(
                    capability=_capability_id(block.name),
                    arguments=dict(block.input),
                )
            if block.type == "text":
                text.append(block.text)
        return Plan(reply=" ".join(t.strip() for t in text if t.strip()))


class ScriptedPlanner:
    """A deterministic planner for when there is no model available.

    Not a simulation of the model -- it makes no attempt to be clever. It
    recognises the demo's intents by keyword, pulls the obvious arguments out
    with regexes, and otherwise says it did not understand. That is enough to
    drive the whole system end to end with no API key, which is what makes it
    worth having: the seam stays exercised, and the tests get a chat path they
    can assert on without a paid, non-deterministic call.
    """

    # Ordered: the first capability whose keywords all appear wins, so a more
    # specific intent can be listed ahead of a more general one.
    INTENTS: list[tuple[str, tuple[str, ...]]] = [
        ("transfer_funds", ("transfer",)),
        ("transfer_funds", ("move", "from")),
        ("read_balance", ("balance",)),
        ("read_balance", ("how much",)),
        ("place_account_hold", ("hold",)),
        ("open_new_share", ("open", "share")),
        ("update_member_information", ("update",)),
        ("find_member_by_name", ("find", "name")),
        ("find_member_by_number", ("find",)),
    ]

    def plan(self, message: str, entries: list[CatalogEntry]) -> Plan:
        text = message.casefold()
        by_suffix = {e.name.rsplit(".", 1)[-1]: e for e in entries}

        for suffix, keywords in self.INTENTS:
            entry = by_suffix.get(suffix)
            if entry is None or not all(k in text for k in keywords):
                continue
            arguments = self._arguments(message, entry)
            supplied = server_supplied(entry.capability)
            missing = [
                name
                for name in entry.input_schema["required"]
                if name not in arguments and name not in supplied
            ]
            if missing:
                return Plan(
                    reply=(
                        f"I can run {entry.name} for that, but I still need: "
                        + ", ".join(missing)
                        + "."
                    )
                )
            return Plan(capability=entry.name, arguments=arguments)

        return Plan(
            reply=(
                "I did not recognise that as one of the capabilities I can run. "
                "Try asking for a member's balance, or a transfer between shares."
            )
        )

    @staticmethod
    def _arguments(message: str, entry: CatalogEntry) -> dict[str, Any]:
        found: dict[str, Any] = {}
        members = re.findall(r"\b\d{6}\b", message)
        shares = re.findall(r"\b\d{6}-[A-Z]{1,6}-?\d*\b", message)
        amounts = re.findall(r"\$?\b(\d+\.\d{2})\b", message)

        names = {spec.name for spec in entry.capability.inputs}
        if "member_id" in names and members:
            found["member_id"] = members[0]
        if "from_share" in names and len(shares) >= 1:
            found["from_share"] = shares[0]
        if "to_share" in names and len(shares) >= 2:
            found["to_share"] = shares[1]
        if "amount" in names and amounts:
            found["amount"] = amounts[0]
        if "memo" in names:
            found["memo"] = "chatbot request"
        return found


def build_planner(catalog: CapabilityCatalog) -> tuple[Planner, str]:
    """The planner to use, and the name to show so it is never in doubt.

    Falls back rather than failing. A reviewer without a key still gets a
    working chatbot, and the dashboard says which planner answered so a
    scripted reply is never mistaken for a model's.
    """
    del catalog  # selection depends on credentials, not on what is loaded
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudePlanner(), f"claude ({ClaudePlanner().model})"
    return ScriptedPlanner(), "scripted (no ANTHROPIC_API_KEY set)"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarise(run: Any, entry: CatalogEntry | None = None) -> str:
    """A finished run, in plain language.

    Templated, not generated. Every number here came out of the replay's typed
    outputs; nothing in this function can invent one.
    """
    status = run.status

    if status == "success":
        if not run.outputs:
            return "Done."
        return "Done. " + " ".join(_output_lines(run.outputs, entry))

    if status == "business_outcome":
        # Not a failure. The system did its job and the host said no, which is
        # a legitimate answer to the request rather than a breakdown.
        return f"{run.message} ({run.outcome_code})"

    if status == "awaiting_operator":
        reason = (run.intervention or {}).get("reason", "")
        return "Paused and handed to a human. " + reason

    if status in {"queued", "running"}:
        return "Working on it..."

    error = run.error or {}
    where = error.get("intent") or error.get("step_id") or "an early step"
    observed = error.get("observed", "")
    detail = f" Expected {error['expected']}, but saw {observed}." if error.get("expected") else ""
    return f"That did not complete. It stopped at {where}.{detail}"


def _output_lines(outputs: dict[str, Any], entry: CatalogEntry | None) -> list[str]:
    """One sentence per output, using the artifact's own descriptions."""
    described = {}
    if entry is not None:
        described = {o.name: (o.description, o.type) for o in entry.capability.outputs}

    lines = []
    for name, value in outputs.items():
        description, type_ = described.get(name, (name.replace("_", " "), "string"))
        label = description.rstrip(".") or name.replace("_", " ")
        lines.append(f"{label[:1].upper()}{label[1:]}: {_format(value, type_)}.")
    return lines


def _format(value: Any, type_: str) -> str:
    if type_ == "money" and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    return str(value)
