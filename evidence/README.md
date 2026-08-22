# Evidence

Ten runs against the live console, all produced by one command:

```bash
python scripts/make_evidence.py --clean
```

Every run uses the artifacts and the guardrail policy exactly as they ship.
Faults are injected through the target app's out-of-band control at a named
step — never by weakening an artifact or relaxing the policy.

## What to read first

If you only open one folder, open **`subaccount-approved-by-operator`**. It is
the whole system in one run: twelve steps executed unattended, a hard stop at
the one action that creates a record, a human approval, the run completing, and
an audit trail of who held the session.

Its `result.json` contains:

```json
"control_ledger": [
  { "from_actor": "none",  "to_actor": "agent", "reason": "replay meridian.member.open_subaccount" },
  { "from_actor": "agent", "to_actor": "human", "reason": "irreversible action on 'Submit Request' requires operator approval" },
  { "from_actor": "human", "to_actor": "agent", "reason": "operator handed control back" }
],
"human_touched": true
```

Then open **`subaccount-blocked-no-operator`**: the identical run with nobody
available to approve. It stops at step 12 with `escalation_unresolved`. The
account is not created. That pair is the safety model working in both
directions.

## The runs

### The read capability — `meridian.member.read_savings_balance`

| Folder | Result | Shows |
|---|---|---|
| `replay-success` | `success` | Eight steps, three typed outputs, no model involved |
| `replay-business-not-found` | `business_outcome` `MEMBER_NOT_FOUND` | A legitimate negative answer, stopping at step 6 |
| `replay-business-permission-denied` | `business_outcome` `PERMISSION_DENIED` | Stops at step 7 with a *different* code — the record exists, the operator is not entitled |
| `replay-recovered-interstitial` | `success` | A maintenance interstitial dismissed mid-run; see `recoveries[]` |
| `replay-failure-app-error` | `failed` `app_error` | A fault page, with screenshot and accessibility snapshot |
| `replay-failure-invalid-input` | `failed` `invalid_input` | `steps: []` — rejected before the browser opened |
| `replay-human-handoff` | `success` | Session expires mid-flow, operator re-authenticates, run resumes |

### The write capability — `meridian.member.open_subaccount`

| Folder | Result | Shows |
|---|---|---|
| `subaccount-approved-by-operator` | `success` | Irreversible step approved by a human; account created |
| `subaccount-blocked-no-operator` | `failed` `escalation_unresolved` | Same run, no operator: fails closed |
| `subaccount-validation-rejected` | `business_outcome` `VALIDATION_REJECTED` | Deposit below the product minimum — the institution said no, and nothing was created |

The three sub-account runs are deliberately the same flow with one variable
changed each time. Together they separate *the automation broke*, *nobody was
available to authorise it*, and *the institution declined* — three outcomes
that a system conflating them would report identically.

## What is in each folder

| File | Contents |
|---|---|
| `run.jsonl` | One line per event, in order: run header, every step, fault injections, interventions, result |
| `result.json` | The typed result the caller received, plus the control ledger |
| `intervention-N.json` | The briefing raised to the operator, where one was |
| `failure.png` | Screenshot at the moment of failure |
| `observation.txt` | Accessibility snapshot of every frame at that moment |

Two things to look for in `run.jsonl`:

- **`resolved_by` and `strategy_rank` on every step.** `rank 0` means the
  preferred locator strategy matched. A run that starts reporting `rank 1` has
  not failed, but the UI has moved underneath it — the cheapest drift signal
  the system has.
- **The mix of strategies.** `label_cell` for inputs that have no accessible
  name, `role_name` for buttons, `text` for the non-semantic `<td onclick>`
  menu. That mix is the hostile-markup problem, visible as data.

## On redaction

`result.json` masks outputs by their declared sensitivity:

```json
"outputs": {
  "new_account_number": "[REDACTED:restricted str]",
  "product_type": "Holiday",
  "opening_deposit": "[REDACTED:restricted float]"
}
```

The caller received the real values in memory; the evidence folder records that
they existed, their type, and their names. `product_type` is classified
`internal` and passes through, which is the point — the rule is driven by the
schema, not by blanket masking.

The known gap, stated rather than solved: **screenshots are pixels and nothing
redacts pixels.** `failure.png` for a member record contains that member's
data. Production would crop to the failing region or hold screenshots in a
store with tighter retention.

## Doing the handoff yourself

The `replay-human-handoff` run above uses a scripted stand-in operator so it
regenerates reproducibly — the control transfer and activity capture are real,
only the decision is scripted. To be the operator yourself:

```bash
python -m target_app.app                 # terminal 1

python scripts/chaos.py expire           # terminal 2
python scripts/replay.py artifacts/meridian.member.read_savings_balance@v1.json \
    --param member_id=10001 --param operator_id=op.demo --param operator_passphrase=demo-pass \
    --operator --evidence manual-handoff
```

A real browser opens, sign-on bounces back to the sign-on page, and the run
stops and hands you the window. Sign on by hand with `op.demo` / `demo-pass`,
then type `resume` in **the same terminal** — the engine reads the screen you
left and works out which step to continue from.

Check `escalations[0].human_actions` afterwards. It should list the navigations
you caused, observed from the live session rather than self-reported:

```
navigated top  -> http://127.0.0.1:5057/console
navigated nav  -> http://127.0.0.1:5057/frame/nav
navigated main -> http://127.0.0.1:5057/frame/home
```

An empty list means the browser was never touched — and the handler now says so
before resuming, because resuming an unchanged session fails at the next step
for reasons that look like drift and are not.
