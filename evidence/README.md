# Evidence

Recorded runs of the system against the MERIDIAN CORE target console. Every
folder here was produced by the shipped code, not written by hand:

```
python scripts/replay.py artifacts/meridian.member.read_savings_balance@v1.json \
    --param member_id=10001 --param operator_id=op.demo --param operator_passphrase=demo-pass \
    --evidence replay-success
```

## What is in a run folder

| File | Contents |
|---|---|
| `run.jsonl` | Ordered event trail: run header, one line per step, failures, close |
| `result.json` | The typed result the caller received |
| `failure.png` | Screenshot at the moment of failure (failed runs only) |
| `observation.txt` | Accessibility snapshot of every frame at that moment (failed runs only) |

The richer failure signal is a screenshot **plus an accessibility snapshot**
rather than a DOM dump. The accessibility tree is the representation the
automation itself reasons about, so a reviewer sees what the engine saw; and
unlike a DOM it is producible on a desktop surface too, so the evidence format
does not assume a browser.

## The runs

| Folder | Result | What it demonstrates |
|---|---|---|
| `replay-success` | `success` | The full flow with typed outputs extracted |
| `replay-business-not-found` | `business_outcome` `MEMBER_NOT_FOUND` | An unknown member is a legitimate answer, not a crash |
| `replay-business-permission-denied` | `business_outcome` `PERMISSION_DENIED` | The record exists but is not viewable — a *different* answer from not-found |
| `replay-recovered-interstitial` | `success` | A maintenance interstitial was dismissed mid-run and the step retried; the recovery is recorded but the run still ends in success |
| `replay-failure-app-error` | `failed` `app_error` | A fault page stops the run, with a screenshot and snapshot of the screen it died on |
| `replay-failure-invalid-input` | `failed` `invalid_input` | A malformed argument is rejected before the browser opens — note `steps: []` |
| `replay-human-handoff` | `success` | The session expired mid-run, a human took over the *same* live session, fixed it, handed back, and the run completed |

The three failure-ish rows are deliberately three *different* classifications.
Separating "the answer is no", "the app is broken", and "the caller asked
wrongly" is the distinction the whole result contract exists to make.

## Reading the handoff run

`replay-human-handoff` is the one worth reading line by line. Its `run.jsonl`
shows the shape of an interrupted-then-resumed run:

```
step 0..5              agent runs normally
intervention_requested step=run_search   <- session expired
step 4..7              re-run after the human handed back
run_finished           success
```

The run winds back to step 4 rather than retrying step 6, because
re-authenticating lands the console on its home screen — the control step 6
wanted no longer exists. The operator, who can see the live session, names the
resume point.

`control-ledger.json` records the transfer itself:

```
none -> agent    replay meridian.member.read_savings_balance
agent -> human   the servicing session expired mid-flow ...
human -> agent   operator handed control back
```

And `result.json` carries `human_actions` — captured by watching the live
session's navigations, not typed in afterwards:

```
navigated top  -> /login
navigated top  -> /console
navigated nav  -> /frame/nav
navigated main -> /frame/home
```

**What is scripted here.** The operator's *decision* ("resume from
`open_member_search`") comes from a `ScriptedOperator` so the run is
reproducible without a person sitting at a terminal. Everything else is real:
the session genuinely expires, the control token genuinely moves, the
re-authentication happens in the same live browser, and the trail is observed
rather than declared.

## Doing the handoff by hand

```bash
python -m target_app.app                 # terminal 1

python scripts/chaos.py expire           # terminal 2 -- arm the session failure
python scripts/replay.py artifacts/meridian.member.read_savings_balance@v1.json \
    --param member_id=10001 --param operator_id=op.demo --param operator_passphrase=demo-pass \
    --operator --evidence manual-handoff
```

A real browser opens. Sign-on bounces straight back to the sign-on page, the
run stops, and the terminal prints a briefing listing every step you may
resume from. The window is now yours: sign on by hand with `op.demo` /
`demo-pass`, then type

```
resume open_member_search
```

and the run finishes from there. `resume` alone retries the step that stopped,
and `abort` fails the run.

Note *which* step it stops on. Arming the expiry before the run means the
session dies during sign-on, so the flow never gets past step 3 and you resume
from the search step. The committed `replay-human-handoff` run arms it later
instead, mid-flow, which is the more realistic shape -- and it is why the
capability declares two signals on the sign-on step: an outright credential
rejection says *"Sign-on failed"* on the page, whereas an expired session
silently returns you to the login screen with nothing to read. Only the second
one is escalated to a human, because only that one is something a person can
fix.

## Reproducing them

Start the target app, then use `scripts/chaos.py` to arm the runtime
conditions:

```bash
python -m target_app.app                 # terminal 1

python scripts/chaos.py notice           # arms the interstitial once
python scripts/chaos.py error            # arms one fault page
python scripts/chaos.py reset            # clears faults and restores fixture data
```

## What is deliberately not here

**Output values.** `result.json` records that `savings_balance` came back and
that it was a `float`, but not the figure. Declared outputs are returned to the
caller in memory; an evidence folder is read by people who did not make the
request, so restricted outputs are described rather than persisted. Outputs
declared `internal` or `public` are written in full.

**Credentials.** The run header lists which parameters the capability declares
and which are secret. It never lists what was passed.

**Redacted screenshots.** `failure.png` is pixels, and nothing here redacts
pixels — a failure screenshot of a member record contains that member's data.
A real deployment would crop to the failing region, blur by accessibility node,
or hold screenshots in a store with tighter retention. Called out rather than
solved.
