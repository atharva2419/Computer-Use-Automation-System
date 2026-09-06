"""Runs that finished before this process started (brief 3.4).

The brief asks the dashboard to show run history for **discovery and replay**.
The runner only knows what it has executed itself, which leaves two holes: a
discovery run is never executed by the service at all, and restarting the
service empties the history. A reviewer opening the dashboard would see a blank
table with every recorded run sitting on disk a directory away.

So the durable record becomes the source. Every run already writes an evidence
directory -- ``run.jsonl`` always, ``result.json`` for a replay -- and this
module reads those back into exactly the same ``Run`` shape the live runner
produces, so the API and the templates cannot tell the difference.

Two shapes exist on disk:

*   **replay** -- ``result.json`` holds the whole typed result, including the
    per-step records the run page renders. Little work to do.
*   **discovery** -- no result, because nothing was replayed. The timeline is
    the model's own actions, so those become the steps, and the recorder's
    notes become visible: this is where "discarded checkpoint quoting
    '102777'" can actually be read by a person rather than described.

The directory name is the id. The internal run id is a random hex that nothing
on the filesystem exposes, whereas the directory name is unique, sortable and
already the thing a reviewer sees; making it the id means a URL from the
dashboard and a folder on disk name the same run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import Run

DEFAULT_EVIDENCE_ROOT = Path("evidence/runs")

# Statuses only an archived discovery run can carry. The replay statuses are
# the core's own and need no translation.
DISCOVERY_STATUSES = {"recorded", "exhausted", "stuck", "failed"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A truncated trailing line means the process died mid-write. The
            # rest of the run is still worth showing.
            continue
    return out


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


class RunArchive:
    """The evidence directory, presented as finished runs."""

    def __init__(self, root: Path | str = DEFAULT_EVIDENCE_ROOT) -> None:
        self.root = Path(root)

    # -- listing -----------------------------------------------------------

    def directories(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        # Newest first. Directory names begin with an ISO-ish timestamp, so
        # a reverse lexical sort is a chronological one.
        return sorted(
            (d for d in self.root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )

    def recent(self, limit: int = 200) -> list[Run]:
        runs: list[Run] = []
        for directory in self.directories()[:limit]:
            run = self.load(directory.name)
            if run is not None:
                runs.append(run)
        return runs

    # -- one run -----------------------------------------------------------

    def load(self, run_id: str) -> Run | None:
        """Read one archived run, or None if that is not a run directory.

        ``run_id`` is a directory name and is checked as one: anything with a
        separator in it is refused rather than resolved, so a crafted id
        cannot walk out of the evidence root.
        """
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            return None
        directory = self.root / run_id
        if not directory.is_dir():
            return None

        records = _read_jsonl(directory / "run.jsonl")
        if not records:
            return None
        started = next((r for r in records if r.get("kind") == "run_started"), {})

        result = _load_json(directory / "result.json")
        if result is not None:
            return self._from_replay(run_id, directory, started, result)
        return self._from_discovery(run_id, directory, started, records)

    # -- shapes ------------------------------------------------------------

    @staticmethod
    def _from_replay(
        run_id: str, directory: Path, started: dict[str, Any], result: dict[str, Any]
    ) -> Run:
        escalations = result.get("escalations") or []
        error = result.get("error")
        return Run(
            run_id=run_id,
            capability_id=result.get("capability_id") or started.get("capability_id", ""),
            status=result.get("status", "unknown"),
            submitted_at=result.get("started_at") or started.get("at", ""),
            started_at=result.get("started_at"),
            finished_at=result.get("finished_at"),
            # Arguments are deliberately never written to evidence -- only the
            # names of the declared inputs -- so there is nothing to restore.
            arguments={},
            steps=list(result.get("steps") or []),
            outputs=dict(result.get("outputs") or {}),
            outcome_code=result.get("code"),
            message=result.get("message") or (error or {}).get("observed", ""),
            error=error,
            intervention=escalations[-1] if escalations else None,
            recoveries=list(result.get("recoveries") or []),
            evidence_dir=str(directory),
            source="archive",
            kind=started.get("run_kind", "replay"),
        )

    @staticmethod
    def _from_discovery(
        run_id: str,
        directory: Path,
        started: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> Run:
        finished = next((r for r in records if r.get("kind") == "discovery_finished"), {})

        # The model's actions are this run's steps. They carry no locator rank
        # or checkpoint -- those belong to replay -- and the template already
        # renders a dash for each.
        steps: list[dict[str, Any]] = []
        for index, record in enumerate(r for r in records if r.get("kind") == "model_action"):
            args = record.get("args") or {}
            steps.append(
                {
                    "index": index,
                    "step_id": record.get("tool", "?"),
                    "intent": args.get("intent", ""),
                    "action_kind": record.get("tool", ""),
                    "status": "ok",
                    "resolved_by": None,
                    "strategy_rank": None,
                    "duration_ms": None,
                    "checkpoint_ok": None,
                    "note": "",
                }
            )

        # What the recorder refused to write down, and why. On a live run this
        # only ever reached the terminal; here it is the most useful thing on
        # the page.
        notes: list[str] = []
        for record in records:
            kind = record.get("kind")
            if kind == "recorder_note":
                notes.append(record.get("detail", ""))
            elif kind == "circular_anchor_refused":
                notes.append(
                    f"output {record.get('output')!r}: refused a locator anchored on "
                    f"{record.get('row_contains')!r} -- that is the value it reads"
                )
            elif kind == "finish_refused":
                notes.append(f"emission refused: {record.get('reason', '')}")

        status = finished.get("status", "unknown")
        return Run(
            run_id=run_id,
            capability_id=started.get("capability_id", ""),
            status=status,
            submitted_at=started.get("at", ""),
            started_at=started.get("at"),
            finished_at=finished.get("at"),
            arguments={},
            steps=steps,
            outputs={},
            outcome_code=None,
            message=finished.get("reason", ""),
            error=None,
            intervention=None,
            recoveries=[],
            evidence_dir=str(directory),
            source="archive",
            kind="discovery",
            notes=notes,
            model_turns=finished.get("model_turns"),
        )
