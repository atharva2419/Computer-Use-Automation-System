"""Writing run evidence to disk (brief 3.5).

What a run leaves behind, in ``evidence/runs/<timestamp>-<run_id>/``:

    run.jsonl        one line per event: the ordered story of the run
    result.json      the typed result, as the caller received it
    failure.png      screenshot at the moment of failure
    observation.txt  accessibility snapshot of every frame at that moment

The richer-signal-on-failure the brief asks for is a screenshot *plus* the
accessibility snapshot, rather than a DOM dump. Two reasons. It is the same
representation the automation itself reasons about, so a reviewer reads
exactly what the engine saw rather than having to infer it from markup. And
it is surface-agnostic: a desktop surface can produce an accessibility tree
and cannot produce a DOM, so the evidence format does not quietly assume a
browser the way the rest of the system is careful not to.

Redaction is *not* done here. The engine scrubs text before handing it over,
so this module only writes bytes it was given. Keeping the policy in one place
means a second sink cannot forget to apply it.

The known limit: screenshots are pixels, and nothing here redacts pixels. A
failure screenshot of a member record contains that member's data. Real
deployments would either crop to the failing region, blur by accessibility
node, or keep screenshots in a separate store with tighter retention. Noted
rather than solved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema.capability import Capability
from .schema.result import FailureDetail, StepRecord

DEFAULT_EVIDENCE_ROOT = Path("evidence/runs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class FileEvidenceSink:
    """Writes one directory per run.

    A directory rather than a shared log file: a run is the unit people
    actually investigate, and bundling its trail, its result and its failure
    artefacts together means an evidence folder can be handed to someone
    whole.
    """

    root: Path = DEFAULT_EVIDENCE_ROOT
    label: str = ""
    directory: Path | None = None

    def open(self, capability: Capability, run_kind: str = "replay") -> Path:
        """Create the run directory and write the header line."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{self.label}" if self.label else ""
        self.directory = self.root / f"{stamp}{suffix}"
        self.directory.mkdir(parents=True, exist_ok=True)

        self._append(
            {
                "kind": "run_started",
                "run_kind": run_kind,
                "capability_id": capability.id,
                "capability_version": capability.version,
                "schema_version": capability.schema_version,
                "app": capability.app.model_dump(mode="json"),
                "approval": capability.approval,
                # The arguments themselves are never written -- only which
                # parameters the capability declares, and which are secret.
                "declared_inputs": [
                    {"name": p.name, "type": p.type, "secret": p.secret}
                    for p in capability.inputs
                ],
            }
        )
        return self.directory

    # -- EvidenceSink protocol --------------------------------------------

    def on_step(self, run_id: str, record: StepRecord) -> None:
        payload = record.model_dump(mode="json")
        payload["kind"] = "step"
        payload["run_id"] = run_id
        self._append(payload)

    def on_failure(
        self,
        run_id: str,
        detail: FailureDetail,
        screenshot: bytes,
        observation: str = "",
    ) -> list[str]:
        written: list[str] = []
        if self.directory is None:
            return written

        if screenshot:
            shot = self.directory / "failure.png"
            shot.write_bytes(screenshot)
            written.append(str(shot))

        if observation:
            snap = self.directory / "observation.txt"
            snap.write_text(observation, encoding="utf-8")
            written.append(str(snap))

        payload = detail.model_dump(mode="json")
        payload["kind"] = "failure"
        payload["run_id"] = run_id
        payload["artefacts"] = written
        self._append(payload)
        return written

    def on_result(self, run_id: str, result: Any, capability: Capability) -> None:
        if self.directory is None:
            return

        summary = result.model_dump(mode="json")
        summary["outputs_written"] = False

        # Declared outputs are returned to the caller in memory but are not
        # persisted raw: an evidence folder is read by people who did not make
        # the request. What is recorded is that the outputs existed and what
        # they were called, which is what a reviewer needs to confirm the run
        # produced its contract.
        if "outputs" in summary:
            summary["output_names"] = sorted(summary["outputs"])
            summary["outputs"] = {
                name: _describe(value, _sensitivity(capability, name))
                for name, value in summary["outputs"].items()
            }

        (self.directory / "result.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        self._append({"kind": "run_finished", "run_id": run_id, "status": result.status})

    # -- free-form events --------------------------------------------------

    def note(self, kind: str, **fields: Any) -> None:
        """Record something the engine does not know about.

        Used by the discovery loop and the operator handoff to log model
        decisions and human actions into the same ordered trail as the steps.
        """
        self._append({"kind": kind, **fields})

    # -- internals ---------------------------------------------------------

    def _append(self, payload: dict[str, Any]) -> None:
        if self.directory is None:
            return
        payload = {"at": _now(), **payload}
        with (self.directory / "run.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")


def _sensitivity(capability: Capability, name: str) -> str:
    for spec in capability.outputs:
        if spec.name == name:
            return spec.sensitivity
    return "restricted"


def _describe(value: Any, sensitivity: str) -> Any:
    """Record the shape of a value without recording the value.

    Type and length are enough to confirm the extraction and its transform
    worked -- that a money output really did come back as a number -- without
    persisting the figure itself.
    """
    if sensitivity in ("public", "internal"):
        return value
    return f"[REDACTED:{sensitivity} {type(value).__name__}]"
