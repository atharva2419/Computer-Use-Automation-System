"""Executing capabilities behind the API (brief 3.2, 3.4, 3.5).

One worker, one run at a time. That is not a simplification made for the
demo -- it falls out of the control token. A capability drives one browser
session, and the session has a single holder; two invocations racing for it
would be refused by the core anyway. Making the queue serial turns that into
an honest property of the service rather than an error someone hits.

Runs execute in the background and are polled, rather than blocking the HTTP
call for the length of a browser session. That is what lets the dashboard show
a run *while it happens* instead of only after it finishes, and it is also
what makes an escalation expressible: a run can sit in ``awaiting_operator``
indefinitely without a request timing out underneath it.

Nothing here re-implements policy. Every invocation goes through the same
``ReplayEngine`` with the same ``PolicyGate``, so the wrapper cannot become a
way around the guardrails -- it has no path that skips them.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.evidence import FileEvidenceSink
from cua.guardrails import PolicyGate
from cua.redaction import Redactor
from cua.replay import ReplayEngine
from cua.schema.capability import Capability
from cua.schema.result import FailureDetail, StepRecord
from cua.session import Session
from cua.surface.web import PlaywrightWebSurface

from .catalog import CapabilityCatalog
from .operator import DashboardOperator

TERMINAL = {"success", "business_outcome", "failed", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    """One invocation, as the dashboard and the caller see it."""

    run_id: str
    capability_id: str
    status: str = "queued"
    submitted_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    # Argument *names* and non-secret values only. A run record is displayed
    # and kept; a credential passed for one invocation has no business in it.
    arguments: dict[str, str] = field(default_factory=dict)

    steps: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome_code: str | None = None
    message: str = ""
    error: dict[str, Any] | None = None
    intervention: dict[str, Any] | None = None
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    evidence_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "capability_id": self.capability_id,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "arguments": self.arguments,
            "steps": self.steps,
            "outputs": self.outputs,
            "outcome_code": self.outcome_code,
            "message": self.message,
            "error": self.error,
            "intervention": self.intervention,
            "recoveries": self.recoveries,
            "evidence_dir": self.evidence_dir,
        }


class _RunSink:
    """Feeds the live run record while the ordinary evidence files are written.

    Two sinks would mean two chances to forget redaction, so this one wraps
    the file sink rather than replacing it: the engine still scrubs everything
    on the way in, the files are written exactly as before, and the dashboard
    reads the same records.
    """

    def __init__(self, run: Run, files: FileEvidenceSink) -> None:
        self.run = run
        self.files = files

    def on_step(self, run_id: str, record: StepRecord) -> None:
        self.run.steps.append(record.model_dump(mode="json"))
        self.files.on_step(run_id, record)

    def on_failure(
        self, run_id: str, detail: FailureDetail, screenshot: bytes, observation: str = ""
    ) -> list[str]:
        return self.files.on_failure(run_id, detail, screenshot, observation)

    def on_result(self, run_id: str, result: Any, capability: Capability) -> None:
        self.files.on_result(run_id, result, capability)

    def note(self, kind: str, **fields: Any) -> None:
        self.files.note(kind, **fields)

    @property
    def directory(self) -> Path | None:
        return self.files.directory


class CapabilityRunner:
    """Queues invocations and executes them one at a time."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        policy_path: Path | str,
        headed: bool = False,
        evidence_root: Path | str = Path("evidence/runs"),
    ) -> None:
        self.catalog = catalog
        self.policy_path = Path(policy_path)
        self.headed = headed
        self.evidence_root = Path(evidence_root)

        self._runs: dict[str, Run] = {}
        # Arguments are held only until the worker picks the run up, and
        # never stored on the Run itself -- they carry the credential.
        self._pending: dict[str, tuple[Capability, dict[str, Any]]] = {}
        self._order: list[str] = []
        self._operators: dict[str, DashboardOperator] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._serve, daemon=True, name="capability-runner")
        self._worker.start()

    # -- submission --------------------------------------------------------

    def submit(
        self, name: str, arguments: dict[str, Any], allow_draft: bool = False
    ) -> Run:
        """Queue an invocation. Raises if the capability may not be run."""
        capability = self.catalog.resolve_for_invocation(name, allow_draft=allow_draft)

        secret_names = {spec.name for spec in capability.inputs if spec.secret}
        run = Run(
            run_id=uuid.uuid4().hex[:12],
            capability_id=capability.id,
            arguments={
                k: ("[REDACTED:secret]" if k in secret_names else str(v))
                for k, v in arguments.items()
            },
        )
        with self._lock:
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
        self._pending[run.run_id] = (capability, dict(arguments))
        self._queue.put(run.run_id)
        return run

    # -- inspection --------------------------------------------------------

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def recent(self, limit: int = 50) -> list[Run]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
        return [self._runs[i] for i in ids]

    # -- operator decisions ------------------------------------------------

    def decide(self, run_id: str, approve: bool, resume_from: str | None = None) -> bool:
        """Answer a run that is waiting on a human. True if it was waiting."""
        operator = self._operators.get(run_id)
        if operator is None:
            return False
        operator.decide(approve=approve, resume_from=resume_from)
        return True

    # -- the worker --------------------------------------------------------

    def _serve(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                self._execute(run_id)
            except Exception as exc:  # noqa: BLE001 - a worker must not die
                run = self._runs.get(run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = {
                        "category": "internal_error",
                        "expected": "the invocation to complete",
                        "observed": f"{type(exc).__name__}: {exc}",
                    }
                    run.finished_at = _now()
            finally:
                self._queue.task_done()

    def _execute(self, run_id: str) -> None:
        run = self._runs[run_id]
        capability, arguments = self._pending.pop(run_id)

        run.status = "running"
        run.started_at = _now()

        gate = PolicyGate.from_file(self.policy_path)
        redactor: Redactor = gate.policy.redactor()

        files = FileEvidenceSink(root=self.evidence_root, label=f"api-{capability.id.split('.')[-1]}")
        files.open(capability, run_kind="replay")
        run.evidence_dir = str(files.directory)
        sink = _RunSink(run, files)

        operator = DashboardOperator(run=run, sink=sink, redactor=redactor)
        self._operators[run_id] = operator

        surface = PlaywrightWebSurface(headless=not self.headed).start()
        session = Session(surface=surface)
        try:
            result = ReplayEngine(
                session,
                gate=gate,
                sink=sink,
                redactor=redactor,
                escalation=operator,
            ).run(capability, arguments)
        finally:
            session.release()
            surface.close()
            self._operators.pop(run_id, None)

        self._absorb(run, result)

    @staticmethod
    def _absorb(run: Run, result: Any) -> None:
        run.status = result.status
        run.finished_at = _now()
        run.recoveries = [r.model_dump(mode="json") for r in result.recoveries]
        if result.status == "success":
            run.outputs = dict(result.outputs)
            run.message = "Completed."
        elif result.status == "business_outcome":
            run.outputs = dict(result.outputs)
            run.outcome_code = result.code
            run.message = result.message
        else:
            run.error = result.error.model_dump(mode="json")
            run.message = result.error.observed
        if result.escalations:
            run.intervention = result.escalations[-1].model_dump(mode="json")
