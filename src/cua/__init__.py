"""Computer-Use Automation.

An LLM works out how to do a job inside a UI that has no API. That run is
recorded as a typed, versioned **capability artifact**. From then on the job is
done by replaying the artifact with no model involved.

Two paths, one schema
---------------------

    DISCOVERY (once)                     REPLAY (forever)
      goal in English                      artifact + typed params
      cua.agent  -- Claude, tool calls     cua.replay -- reads recorded steps
      cua.recorder -- writes it down       returns success | business
                   |                       outcome | failure
                   +--> artifacts/*.json --+

Discovery is slow, probabilistic and costs money, so it happens once per
capability. Replay is fast, deterministic and free, so it happens thousands of
times. The artifact is the contract between them, and it is data rather than
code: a model authoring a *document* can only express what the schema permits,
which is what lets the guardrail inspect every step before it runs.

Where things live
-----------------

``cua.schema``
    The capability artifact and the replay result contract. Start here -- it
    is the shape everything else serves.
``cua.surface``
    The seam. A ``Surface`` is fourteen methods expressed in accessibility
    roles, names and labels; ``cua.surface.web`` is the only module in the
    package that imports Playwright. Nothing above this line knows what a
    browser is, which is what would make a desktop surface a new file rather
    than a rewrite.
``cua.replay``
    Deterministic execution. Races each step's checkpoint against the
    capability's declared error signals, so a legitimate negative answer is
    reported as an outcome rather than a timeout.
``cua.agent`` / ``cua.recorder``
    The discovery loop, and the translation of a trajectory into an artifact.
    The model finds the path; the recorder derives the durable locators from
    what was actually on the page.
``cua.guardrails`` / ``cua.redaction``
    What the automation is permitted to do, and what may be written down.
``cua.escalation`` / ``cua.session``
    Bringing a human into a running automation, and the token that records who
    is allowed to drive.

Nothing here imports an LLM library at module scope: ``cua.agent`` is loaded
lazily, so the replay path cannot pull in a model client by accident. There is
a test that asserts exactly that.
"""

from __future__ import annotations

__version__ = "0.1.0"
