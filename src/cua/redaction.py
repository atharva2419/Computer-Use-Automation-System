"""Keeping regulated data out of everything the system writes down.

The rule this module implements is narrow and deliberate:

    The declared interface returns data. Everything else is scrubbed.

A caller that invokes "read the member's savings balance" is entitled to the
balance -- redacting a capability's own declared outputs would make it
useless. But the balance has no business appearing in a log line, a failure
excerpt, an escalation payload sent to an operator's screen, or a recorded
artifact. Those are the places data leaks from, because they are written for
debugging and nobody reviews them the way they review a return value.

Two sources of things to hide:

*   **Known secret values.** Whatever was passed for a parameter marked
    ``secret`` in the capability. Exact-match masking, which is the only
    reliable way to catch a passphrase -- no pattern can recognise one.
*   **Patterns.** Account numbers, card-like digit runs, SSNs, emails. These
    catch regulated data the system never handled directly but that appeared
    on a screen it captured.

Both are needed. Patterns alone miss the credentials the caller supplied;
secrets alone miss the other member's account number that happened to be
visible in a table when a screenshot excerpt was taken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# A secret shorter than this is not masked. Masking a one- or two-character
# value would replace half the text in every log line with markers and destroy
# the diagnostics the log exists for -- and a secret that short is not one.
MIN_SECRET_LENGTH = 4

SECRET_MASK = "[REDACTED:secret]"


@dataclass(slots=True)
class NamedPattern:
    name: str
    regex: re.Pattern[str]

    @classmethod
    def build(cls, name: str, pattern: str) -> "NamedPattern":
        return cls(name=name, regex=re.compile(pattern))


@dataclass
class Redactor:
    """Masks secrets and regulated-looking data in text destined for storage."""

    patterns: list[NamedPattern] = field(default_factory=list)
    _secrets: list[str] = field(default_factory=list)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_spec(cls, specs: Iterable[Mapping[str, str]]) -> "Redactor":
        return cls(
            patterns=[NamedPattern.build(s["name"], s["regex"]) for s in specs]
        )

    def learn_secret(self, value: str | None) -> None:
        """Register a concrete value to mask wherever it appears.

        Called with the arguments bound to parameters the capability declared
        ``secret``. The value is held only for the lifetime of the run and is
        never itself written anywhere.
        """
        if not value or len(value) < MIN_SECRET_LENGTH:
            return
        if value not in self._secrets:
            self._secrets.append(value)

    def learn_secrets(self, values: Iterable[str | None]) -> None:
        for value in values:
            self.learn_secret(value)

    # -- application -------------------------------------------------------

    def text(self, value: str | None) -> str:
        """Full redaction: secrets and regulated-looking data. For storage."""
        out = self.secrets_only(value)
        for pattern in self.patterns:
            out = pattern.regex.sub(f"[REDACTED:{pattern.name}]", out)
        return out

    def secrets_only(self, value: str | None) -> str:
        """Mask credentials but leave business data intact.

        For what the *model* is shown during discovery, which is a different
        problem from what gets written down.

        A credential is never needed to reason about a screen -- the agent
        types one by naming a parameter -- so it is masked even when the
        application prints it, as this console does on its own sign-on page.

        Regulated business data is the opposite: the agent cannot navigate a
        member record it is not allowed to read. Masking it produced a real
        failure -- the model quoted ``"Savings [REDACTED:account_number]
        4210.55"`` as the text proving it had arrived, because that is
        genuinely what it had been shown, and the assertion then could not
        hold against the actual page. Redacting a model's perception makes it
        reason about a screen that does not exist.

        So the data reaches the model and is scrubbed on the way to disk. The
        real limit that leaves is disclosure to the provider, which is a
        contractual control (zero-retention terms) rather than one this code
        can enforce -- recorded in the write-up rather than papered over.
        """
        if not value:
            return value or ""
        out = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, SECRET_MASK)
        return out

    def value(self, value: Any) -> Any:
        """Redact recursively through the containers evidence is built from."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.value(v) for v in value)
        return value

    def __bool__(self) -> bool:
        return bool(self.patterns or self._secrets)
