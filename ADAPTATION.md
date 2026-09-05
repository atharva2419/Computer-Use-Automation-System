# Adaptation: pointing the core at MERIDIAN CORE

The take-home core is *discover → record a typed capability → replay deterministically*, with
guardrails, evidence and escalation. This is what it took to aim it at
`web-sample.interface-hiring.com` and cover its seven functions.

## 1. What adapting actually took

The adapter is two YAML files — `config/policy.meridian-hosted.yaml` (allowlist, `/settings`
denied, risk rules, redaction) and `config/signals/meridian-hosted.yaml` (ten runtime conditions
and how each is detected). Selected with `--policy`. The take-home's policy still drives the local
target unchanged, and both products load side by side in one catalog.

**Core churn: 6 of 24 files in `src/cua/`, `+439 / -16` lines** — almost purely additive. `src/cua/`
names no target. One file of 41 imports Playwright (`surface/web.py`), so every browser concept
stays behind the `Surface` seam.

"A config change" would be too tidy an answer, though. **None of the six core changes were porting
work** — each was a latent defect the take-home target could not expose:

| Core change | What the live target exposed |
|---|---|
| `recorder.py` (+244) — checkpoint and locator discipline | Static demo data hid this. Here balances move while you watch, so a model quoting `"$5.00"` or `"Johnson, Katherine"` records a capability that passes its own recording and works for exactly one member. |
| `loop.py` (+92) — value stabilisation; refusals fed back to the model; recorder notes into evidence | A dropdown label contains a live balance, so the recorded literal rots. And an emission refusal used to kill a paid run the model could have fixed. |
| `tools.py` (+21) — checkpoint guidance | Prompt-level mitigation for the same class. It failed three times, which is why the rules below are structural. |
| `guardrails.py` (+28) — a deny rule must not trap | Always broken; only surfaced when discovery wandered onto a denied route and could not leave. |
| `schema/capability.py` (+11) — outcome codes need not be unique | This host reports one condition on two different screens. |
| `agent/client.py` (new) — workspace header | Environment, not target: an org-scoped API key must name its workspace. |

A live, shared, mutable target breaks assumptions a fixture cannot. Three capabilities *looked*
parameterised and worked for one member each. I fixed that in the recorder, not in prompts:

- **Volatile text** — a checkpoint containing an amount, date or reference number is discarded.
- **Invocation-specific values** — a checkpoint quoting an argument the run was given, or a value
  it read back as an output, is discarded. That is the answer, not the evidence.
- **Circular anchors** — "find the row containing *Johnson, Katherine*, then read the name from
  it" locates the answer by already knowing it. Refused, and the reason goes back to the model
  while it can still re-anchor.
- **Unused declared inputs** — refused. Place Account Hold declared `reason` and never touched the
  dropdown; `FRAUD` is the first option, so it recorded fine and a caller asking for `LEGAL` would
  silently have got `FRAUD`.

What cannot be proven structurally is reported instead: `scripts/audit_capability.py --strict`
flags checkpoints that merely *look* pinned, for the person who must approve an irreversible
capability anyway.

## 2. The capability API and its contract

`GET /capabilities` returns a catalog; `POST /capabilities/{name}/invoke` runs one.

The contract is deliberately **not new**: input JSON Schema is generated from the `ParamSpec` list
each artifact already declares, and a run returns the same `ReplayResult` union the replay engine
always produced. A second contract in front of the first would translate nothing.

- **Asynchronous** — invoke returns `202` and a `run_id`. A replay drives a browser for seconds and
  one that stops for a human may wait minutes; holding the connection open would also make
  escalation inexpressible.
- **Serial** — one worker. That falls out of the control token: a session has a single holder, so
  concurrent invocations would be refused anyway. Serial makes it an honest property rather than an
  error someone hits.
- **Outcome codes are published** (`MEMBER_NOT_FOUND`, `TRANSACTION_REJECTED`, `INPUT_REJECTED`,
  `SUPERVISOR_OVERRIDE_REQUIRED`), so an agent branches on a value instead of parsing prose.

The chatbot and dashboard are **clients of that same API**, holding no reference to the runner. The
model picks a capability; it never narrates the outcome, which is templated from the typed result —
asking a model to restate a result adds a step that can invent a confirmation number.

## 3. Driving the UI, and its exceptional states

Perception is the **accessibility tree**, not the DOM: this console is table-layout HTML with no
test IDs, and the tree gives roles and labels that survive markup churn. Targets carry ranked
strategies (`role_name`, `label_cell`, `row_scoped_cell`, `text`, `css`). Across all recorded
evidence, **248 located steps resolved at rank 0 and none below it** — they transferred with no
code change, the clearest early signal the seam was in the right place. The per-transaction hidden
`_token` was a non-issue: it is a session-scoped form field, and the browser submits it because we
click the real button.

Replay races each step's checkpoint against the declared signals; a signal wins ties. Signals are
curated per product because a successful discovery run never sees an error. **Detection strings
were the biggest source of bugs**, all from one root cause — I verified the six `?inject=` faults
and assumed the natural errors matched:

| Condition | Injected | Natural | Consequence |
|---|---|---|---|
| Member not found | `RECORD NOT FOUND` | `No member records matched your search` | missed → locator failure |
| Transaction rejected | `TRANSACTION REJECTED` | `The transaction could not be validated` | missed → real overdraw reported as a crash |
| Invalid field | — | `Please correct the following` | missed entirely |
| Supervisor required | `SUPERVISOR OVERRIDE REQUIRED` | same heading **also on the form itself** | false positive on every run |

The last is instructive. The Place Account Hold *form*, served `200` to an entitled supervisor,
carries the banner `RESTRICTED FUNCTION - SUPERVISOR OVERRIDE REQUIRED`. Matching the heading fired
for anyone who opened the screen: the capability became impossible to complete and the
teller-blocked demo passed for the wrong reason. It now matches `is not authorized to perform this`
— on the real `403`, on neither form.

Outcomes stay three-way: **business outcome** (the host said no — an answer), **recoverable**
(dismiss a known interstitial, retry), **hard failure** (naming the step, the expectation, the
observation).

## 4. Safety, evidence and escalation through the new surface

**There is no second path in.** Every invocation — HTTP, chat, dashboard — goes through
`CapabilityRunner → ReplayEngine → PolicyGate`.

- **Approval is enforced at the door.** An irreversible capability is never auto-verified —
  verifying would perform the transaction again — so it stays `draft`. The API refuses it with
  `409` before a browser exists; the chatbot surfaces the refusal in full.
- **Risk keys on the route, not the button.** An earlier rule matched `control: "post*"`, from the
  transfer's `Post Transfer`. The others are `Apply Hold` and `Open Share` — so **Open New Share
  would have posted with no approval at all**. All three confirmation screens live at `*/review`,
  so "the submit on a confirmation screen" is now the rule; twelve tests pin every real control.
- **A deny rule is a fence, not a trap.** A click can never be checked against its destination, so
  one click can always land the session on denied `/settings` — and refusing *everything* there,
  including leaving, stranded discovery for fifty model turns. Navigation to a permitted URL is now
  allowed from a denied page; everything else there stays refused.
- **Credentials never reach the planner.** Secret and operator-identity inputs are stripped from the
  tool schemas the model sees and filled from the environment after planning.
- **Escalation survived HTTP without touching the engine.** `DashboardOperator` is a second
  implementation of the existing `EscalationHandler` seam: it publishes the request onto the run and
  blocks until `POST /runs/{id}/decision`. The browser stays open and paused holding the control
  token, and the operator's actions are sampled while they hold it.

Redaction is unchanged and applies to everything written down. Evidence is written for every run,
not only failures, and served at `/runs/{id}/evidence/{file}`.

## 5. What I cut, and what I would build next

**Cut deliberately.** No separate find-by-number capability — the function is covered by
`find_by_name` and number lookup runs inside five others; a near-duplicate is breadth, not depth.
The chatbot is single-turn: one model call, one capability, no chaining. Run history is in memory,
because the durable record is the evidence directory. No general retry/backoff — the maintenance
interstitial is handled, transient-fault retry is not.

**Next, in order.** (1) Close the pinned-checkpoint gap structurally: record each capability twice
with different arguments and keep only assertions true in both, turning a heuristic into a
measurement. (2) Make `operator_id` a first-class caller choice — teller-versus-supervisor is
business-meaningful and the dashboard hides it. (3) Optional inputs: `discover.py` marks every
parameter required, so the chatbot stops to ask for a memo the host does not require. (4) A
capability-level test against a recorded HTTP fixture, so replay regressions are caught without a
live target or model spend.

**Known limitation.** The target is shared and mutable — other candidates' data changes underneath.
Capabilities assert structure rather than values precisely because of this, but balances and share
lists drift between runs.
