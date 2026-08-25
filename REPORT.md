# Computer-Use Automation System — design write-up

A model works out how to do a job in a UI that has no API. That run is recorded
as a typed artifact. From then on the job is done by replaying the artifact
with no model involved, under a policy, with a human reachable when it gets
stuck.

Target: **MERIDIAN CORE**, a deliberately legacy servicing console built for
this project — a real `<frameset>`, nested tables, no test IDs, fields named
`f1`/`f2`, menu items that are `<td onclick>`, and seven injectable runtime
faults. Built rather than borrowed because no public demo site produces
"permission denied" on demand, and the error taxonomy is the point.

Python · Playwright · Pydantic v2 · Claude Sonnet. 109 tests, against the live
app rather than mocks.

---

## 1. Architecture

Two execution paths over one schema:

```
DISCOVERY (once, ~$0.03, 25s)        REPLAY (forever, free, 3.7s)
  goal + params                        artifact + params
  Claude ── tool calls                 engine ── reads steps
        ↓                                    ↓
   Recorder → artifact.json ──────────────► executes
```

The load-bearing boundary is `Surface` ([src/cua/surface/base.py](src/cua/surface/base.py)):
fourteen methods — observe, resolve, click, fill, read, settle — expressed in
accessibility roles, names and labels. Everything above it is written against
that protocol; `grep -rl playwright src/` returns exactly one file. The schema,
the engine, the agent and the guardrail contain no browser concepts at all.

**Perception is the accessibility tree, not the DOM** — a few hundred tokens of
roles and names rather than tens of thousands of tokens of table markup. It is
what an operator perceives, what the locator strategies address, and the one
representation a browser and an OS both expose.

**Single process, JSON files, no queues or database.** The abstractions that
would have to survive a real deployment are designed for it; the plumbing is
not built, because building it is explicitly not rewarded.

**Replay was built before the agent.** That forced the schema to be a real
contract rather than a transcript of whatever a model emitted, and it meant
surface bugs surfaced from deterministic tests in seconds instead of from
confusing model runs.

### Stack, and what it was chosen over

| | choice | why, and the alternative rejected |
|---|---|---|
| Language | Python 3.11 | Pydantic v2 makes the artifact one definition that serves three jobs: runtime validation, static types, and a JSON Schema a calling agent can consume as a tool contract. TypeScript + Zod is an equally good answer; the tie-break was fluency. |
| Automation | Playwright | Best accessibility-tree access of the browser drivers, plus per-frame control this frameset needs. Selenium has weaker a11y support; a screenshot-and-coordinates CUA would have been more impressive-sounding and far less defensible — coordinates cannot be recorded into a durable locator, which is the entire deliverable. |
| Model | Claude Sonnet | Short-horizon navigation over a small tool surface. Opus is unnecessary for it and costs more per discovery; the loop reads `CUA_MODEL`, so swapping is config, not code. |
| Storage | JSON files | Diffable in review, language-neutral, and — critically — **not executable**. A model emitting a Playwright script would be arbitrary code you cannot risk-classify before running it. A document can only express the six action kinds the schema permits, which is what lets the guardrail inspect every step in advance. |

**The agent loop is deliberately narrow.** Eight tools mirroring the action
schema one-to-one, so a trajectory is always recordable — the model cannot
reach the goal by a route replay could not reproduce. No `execute_javascript`,
no raw selectors, no coordinates. One action per turn, because each action
changes the screen and a batch is decided against a state that no longer
exists. Perception is the accessibility tree by default with screenshots
available on request, since the tree is both cheaper and more precise. Four
stopping conditions are enforced by the harness rather than trusted to the
model: a step budget, a wall-clock budget, a no-progress detector, and the
model's own `done`/`stuck`.

---

## 2. Artifact schema

An artifact is four things, not a step list ([src/cua/schema/](src/cua/schema/)):

1. **An interface** — typed `inputs` and `outputs`, so a calling agent can
   invoke it without knowing the UI exists.
2. **A plan** — ordered `steps`, each with a target, a checkpoint and a risk
   class.
3. **An error model** — `signals`: the runtime conditions this flow can meet
   and what each one *means*.
4. **Provenance** — which model, which run, transcript digest, approval state.

Choices worth defending:

**Targets are ranked strategy lists, not selectors.** Forced by the surface:
probing showed `<input name="f2">` in a labelled table cell exposes a textbox
with **no accessible name**, so `role_name` cannot address it at all, while
buttons *do* get names from their `value`. One selector kind cannot cover both.
Each target therefore carries strategies ordered by semantic durability plus a
written rationale — §3.2 asks for reasoning about robustness, so it is
structural rather than a comment. Replay records which rank actually resolved,
which is a free drift signal.

**Tagged unions, `extra="forbid"`, parameter *references* rather than string
templates.** A reviewer can tell what a node is from its first field, a
misspelled key is an error rather than silently ignored, and the schema can
validate that every referenced parameter is declared.

**Checkpoint ≠ success condition.** The last step asserts the click landed; the
capability's `success` asserts the business goal happened. Conflating them
produces capabilities that "succeed" on the wrong screen.

**Draft until proven.** Discovery emits `draft`; a verification replay promotes
it to `approved`. Nothing a model produced is trusted for unattended use on its
own say-so.

---

## 3. Determinism & error handling

**Every wait is a polled predicate, never a sleep.** Fixed sleeps are the
classic replay flake: too short is nondeterministic, too long makes thousands
of invocations pay for the worst case.

**The decision I would defend hardest: a step races its checkpoint against
every declared signal.** The obvious implementation checks the checkpoint and
looks for errors only on failure. That is wrong in a way that appears only in
production — right after an action the surface may still be loading, so a "NO
RECORD FOUND" page rendering 200ms late is missed, the checkpoint times out,
and an ordinary business answer is reported as a failure. Polling both together
catches whichever state the app reaches. When both match the signal wins: it
names a specific state, the checkpoint asks a generic question.

**Three terminal statuses, and only three:**

| | |
|---|---|
| `success` | goal met, typed outputs returned |
| `business_outcome` | a legitimate negative answer, with a code the caller branches on |
| `failed` | `category`, `step_id`, `expected`, `observed` — mandatory fields, not a stack trace |

"Recoverable" is deliberately **not** a status. A recovery happens *along the
way*; the run still ends in one of the three. Returning "recovered" would force
every caller to ask "recovered from what, and did it finish?" Recoveries are
envelope records instead — so a capability quietly recovering more often each
week shows up as a rising recovery count on a still-successful capability, a
signal that would be invisible if collapsed into the status.

**UI drift is handled by degradation, not healing.** A renamed control falls
through to the next ranked strategy and the run survives; the rank that
resolved is recorded, so drift is visible before it breaks. Nothing repairs an
artifact automatically — a self-healing locator silently changes what a
capability does, which here is worse than failing.

The most instructive bug: **`Frame.url` is stale inside a frameset**, reporting
`/frame/search` indefinitely while `location.href` in the same frame said
`/frame/member/10001`. Every URL checkpoint would have been wrong by one
navigation — passing at record time, misfiring in production. Three others
(child-frame waits, a 25s-per-step signal cost, a 2s timeout reading a `<body>`
framesets do not have) shared a cause: this surface breaks assumptions the
tooling makes.

Determinism is asserted: a test replays twice and requires identical outputs,
step order **and resolution ranks**. Another pops `anthropic` from
`sys.modules`, replays, and checks it never returns.

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** A `WindowsUIASurface` over the OS accessibility API
would replay the existing artifacts unchanged, because the schema speaks in
roles, names and labels — vocabulary UIA and AX also have. Nothing above the
seam would move. Built on CSS selectors, that port would be impossible rather
than merely unwritten. The honest cost: `CssStrategy` is web-shaped and inert
on desktop, which is why it ranks last and why replay records when it was used.

**Multi-tenant reuse.** `AppBinding` binds a capability to *product +
version*, with `tenant` nullable — a base artifact is recorded once against the
vendor product, not per institution. Per-tenant variation is expressed as an
`Overlay` that may re-target a step or change a value and **may not** add,
remove or reorder steps, or touch the typed interface. That constraint is the
point: `base + overlay` guarantees an identical contract across every tenant,
so the calling agent never knows which institution it is hitting, and drift
becomes a small diffable patch instead of a re-recording.

**Drift detection is already emitted.** Every step records which strategy rank
resolved. A capability that resolved at rank 0 for six months and starts
resolving at rank 2 has not broken, but the UI moved underneath it — visible
before it fails. Aggregating that across tenants is the natural next step and
is not built.

**Signals are per product, not per capability.** `config/signals/meridian-core.yaml`
holds the app's runtime conditions and every capability recorded against that
product inherits them — because they are properties of the application, not of
any one flow.

---

## 5. Escalation & handoff

Three triggers, one mechanism: a replay hits an unrecoverable condition, policy
requires approval for a risky action, or the discovery agent gets stuck.

**Detect and route.** A `hard_failure` signal with `escalate: true`, or a gate
decision of `require_human`, raises a typed `InterventionRequest` — capability,
goal, step, why it stopped, live frame URLs, and the steps it may resume from.
It is written to the run's evidence folder as JSON plus a screenshot; that file
*is* the seam where a deployment would publish to an operator work queue,
unchanged in payload.

**Take control of the live session.** Nothing is torn down. A single-holder
token moves `AGENT → HUMAN`, and acting without it raises rather than warns —
the failure that prevents is automation typing into a form while an operator is
mid-correction. The same browser, cookie jar and frame state persist, which is
the whole requirement: a fresh session would have none of them.

**Hand back and resume.** Retrying the step that failed is the obvious
behaviour and is usually wrong — recovering a lapsed session puts the console
on its home screen, so the control that step wanted no longer exists. The
operator names a resume point; the engine also *proposes* one by evaluating
each step's checkpoint to find the furthest state that already holds. And
because rewinding re-executes intervening steps, the engine **refuses a resume
point that would cross an irreversible step**: a double-posted transaction is
worse than a failed run.

**Recording what the human did** is observed, not self-reported. Event
subscription failed twice over — sync Playwright only dispatches events when
the caller re-enters the library, and a handoff blocks on operator input, so a
session the human demonstrably used produced an empty trail. It samples now,
yielding `navigated top → /console`, `nav → /frame/nav`, `main → /frame/home`.

**Mocked:** the operator console is a terminal prompt. **Not mocked:** the
control-transfer model, session identity, activity capture, the resume
contract, and the audit ledger (`none → agent → human → agent`, persisted into
the run result).

---

## 6. Safety

**The allowlist is configuration, not code** ([config/policy.yaml](config/policy.yaml)):
permitted origins, route globs, denied routes, permitted action kinds. At a
bank these decisions belong to risk and compliance, and a change should be a
reviewable diff. Proof it is not decorative — the target app's `/__chaos`
fault-injection endpoint sits on an allowed origin, and a test tampers with an
artifact to reach it and asserts the run dies with `policy_denied` and **zero
steps executed**. A run cannot switch off its own error conditions.

**The agent never classifies its own actions.** Risk comes from policy — route,
action kind, and the control being activated. A model that could self-report
"this is safe" would make the guardrail advisory, and the pages it reads are
untrusted input. Where artifact and policy disagree, the more dangerous wins.

**Filling a field is not a write.** Typing into the deposit box changes nothing
server-side; clicking `Submit Request` creates the account. Keying risk off the
control being activated keeps the approval set small enough that approval stays
meaningful.

**Irreversible actions require a human rather than being blocked.** Blocking
would make the sub-account flow unrecordable, and "reach the confirmation
screen" is exactly what this system is for. A person owns the decision, the
flow still completes, and the approval lands in the evidence.

**Redaction has one rule: the declared interface returns data, everything
written down is scrubbed.** A caller who asked for the balance gets it; the same
value in a log or failure excerpt is masked. Both sources are needed —
exact-match on `secret` parameters (no pattern recognises a passphrase) and
patterns for regulated data that merely appeared on a captured screen. A test
asserts `4210.55` survives while `SAV-0001` does not: an evidence trail that
scrubs every figure cannot debug a run about figures.

The live discovery run forced a distinction I had missed. I was redacting
observations *before showing them to the model*, which duly proposed `"Savings
[REDACTED:account_number] 4210.55"` as proof it had arrived — and my own
verification refused to record it. Redacting a model's perception makes it
reason about a screen that does not exist. So **secrets are never shown to the
model; business data is**, with full redaction on the way to disk.

**Limits.** Nothing redacts pixels, so a failure screenshot of a member record
contains that member's data. Reversibility is a human-authored claim in the
policy file; nothing verifies it. Sending observations to a model provider is a
disclosure, controlled contractually rather than by this code. And the guardrail
constrains *actions*, not *reasoning*: a prompt-injected page cannot take the
agent off the allowlist, but it could influence which permitted action it picks.

---

## 7. Cuts

**Deliberately not built:** a co-browsing operator console; a desktop surface;
multi-tenant plumbing, queues or a database; vision-first control; backtracking
or multi-candidate exploration during discovery; auto-discovered signals; retry
across models.

**Known rough edges:** `settle()` is best-effort and checkpoints are the real
synchronisation primitive (Playwright's frameset lifecycle events are not
trustworthy); nested-row disambiguation happens inside a strategy so it is not
visible in the returned resolution; the activity sampler misses navigations
between polls.

**What I would build next, in order.** The strongest argument for the first
item comes from comparing the hand-authored artifact with the one Claude
produced for the same goal:

| | hand-authored | model-discovered |
|---|---|---|
| steps, kinds, order | 8 | **identical** |
| outputs declared | 3 | 1 |
| verified checkpoints | 5 / 8 | 3 / 8 |
| passphrase fallback | `input[type=password]` | `[name=f2]` |

The model reproduced the *flow* exactly. It produced a materially *weaker
artifact*: fewer outputs (it read literally what the goal named), fewer
checkpoints (two proposals failed verification and were discarded), and a
structural fallback where a human picked a semantic one — `type=password`
survives a field rename, `name=f2` does not.

That is the case for **artifact review tooling**: discovery drafts, a human
strengthens and approves. The `draft → approved` gate already exists; what is
missing is the diff view and the review workflow around it. After that: a probe
phase that deliberately submits a bad input so the signal library can be
learned rather than written; cross-tenant overlay resolution end to end; and
drift telemetry aggregated across replays.
