# Computer-Use Automation System

An LLM works out how to do a job inside a UI that has no API. That run is
recorded as a typed, versioned **capability artifact**. From then on the job is
done by **replaying the artifact with no model involved** — under a policy,
with evidence, and with a human reachable when it gets stuck.

Design write-up: **[REPORT.md](REPORT.md)**. Run evidence: **[evidence/](evidence/)**.

```
DISCOVERY  (once, ~25s)              REPLAY  (forever, ~3.7s, no API key)
  goal in English                      artifact + typed params
  Claude drives the UI                 engine executes recorded steps
        ↓                                     ↓
  artifact.json  ──────────────────────►  success | business outcome | failure
```

The target is **MERIDIAN CORE**, a deliberately legacy credit-union servicing
console built for this project: a real `<frameset>`, nested tables, no test
IDs, form fields named `f1`/`f2`, menu items that are `<td onclick>`, and seven
injectable runtime faults. All data is synthetic.

---

## Setup

Requires Python 3.11+ and about 2 minutes.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

.venv/Scripts/python.exe -m playwright install chromium
```

**Config.** Only the discovery run needs a key. Copy the template and add one:

```bash
cp .env.example .env
```

```
ANTHROPIC_API_KEY=sk-ant-...          # discovery only
CUA_MODEL=claude-sonnet-5             # default if unset
TARGET_BASE_URL=http://127.0.0.1:5057
```

`.env` is gitignored. A discovery run costs a few cents.

**Everything except discovery runs with no API key and no external services** —
the target app is local, and replay, guardrails, the human handoff, evidence
and all 109 tests never load a model library. There is a test that asserts
exactly that: it removes `anthropic` from `sys.modules`, replays a capability,
and checks it never comes back.

---

## Demo path

**Terminal 1 — the target application.** Leave it running.

```bash
.venv/Scripts/python.exe -m target_app.app
```

Visit <http://127.0.0.1:5057> and sign on as `op.demo` / `demo-pass` to see
what the automation is up against. View source anywhere.

**Terminal 2 — everything else.**

### 1. Run the agent on a goal

```bash
.venv/Scripts/python.exe scripts/discover.py \
  --goal "Sign on to the servicing console, look up member {member_id}, open their record, and read the current balance of their Savings account" \
  --id meridian.member.read_savings_balance_discovered \
  --name "Read member savings balance (discovered)" \
  --param member_id=10001 \
  --param operator_id=op.demo \
  --secret operator_passphrase=demo-pass \
  --pattern "member_id=^\d{4,8}$" \
  --evidence discovery
```

Claude drives the console, the recorder writes
`artifacts/<id>@v1.json`, and the run **finishes by replaying what it just
recorded**. A capability that cannot immediately reproduce itself stays a
`draft`; one that does is promoted to `approved`.

Add `--headed` to watch it. The credential is passed with `--secret`: the model
is never shown its value and types it by naming the parameter.

### 2. Replay the resulting artifact

No model, no key:

```bash
.venv/Scripts/python.exe scripts/replay.py \
  artifacts/meridian.member.read_savings_balance_discovered@v1.json \
  --param member_id=10001 --param operator_id=op.demo --param operator_passphrase=demo-pass
```

```
SUCCESS   meridian.member.read_savings_balance_discovered v1   in 3680ms
  ok  0. go_to_the_servicing      -                     1077ms
  ok  1. enter_operator_id        label_cell@rank0       562ms
  ...
  outputs
    savings_balance          4210.55
```

`label_cell@rank0` is which locator strategy resolved and at what rank —
falling through to a lower rank is the drift signal.

### 3. Then the interesting part: the same artifact, different outcomes

```bash
A=artifacts/meridian.member.read_savings_balance@v1.json
C="--param operator_id=op.demo --param operator_passphrase=demo-pass"

# a legitimate negative answer, not a crash
.venv/Scripts/python.exe scripts/replay.py $A --param member_id=99999 $C
# the record exists; this operator may not see it
.venv/Scripts/python.exe scripts/replay.py $A --param member_id=20002 $C
# rejected before the browser opens
.venv/Scripts/python.exe scripts/replay.py $A --param member_id=oops  $C
```

Inject faults, then replay again:

```bash
.venv/Scripts/python.exe scripts/chaos.py notice   # interstitial -> recovered, still succeeds
.venv/Scripts/python.exe scripts/chaos.py error    # HTTP 500     -> hard failure with detail
.venv/Scripts/python.exe scripts/chaos.py slow 2500
.venv/Scripts/python.exe scripts/chaos.py reset
```

### 4. Take control of a live session

```bash
.venv/Scripts/python.exe scripts/chaos.py expire
.venv/Scripts/python.exe scripts/replay.py $A --param member_id=10001 $C \
  --operator --evidence manual-handoff
```

Sign-on bounces back, the run stops, and the terminal hands you the **same
browser window** with a briefing and the steps you may resume from. Sign on by
hand, then type `resume open_member_search`. Your navigations are recorded by
watching the session, not by asking you.

### 5. An irreversible action needs a person

```bash
.venv/Scripts/python.exe scripts/replay.py \
  artifacts/meridian.member.open_subaccount@v1.json \
  --param member_id=10001 --param product_type=Holiday --param opening_deposit=150 $C
```

Fails closed with `escalation_unresolved`: opening an account is classified
irreversible by [config/policy.yaml](config/policy.yaml), and no operator was
attached. Add `--operator` to approve it and let the flow finish.

### Other things to run

```bash
.venv/Scripts/python.exe scripts/demo_surface.py     # watch the locator layer, narrated
.venv/Scripts/python.exe -m pytest -q                # 109 tests, ~3 min, real browser
.venv/Scripts/python.exe -m pyright src/cua tests scripts
```

`demo_surface.py` is the clearest single view of the hard part: it shows
`role+name` **failing** on an input with no accessible name, then `label_cell`
resolving it, and a deliberately stale locator degrading to rank 1.

---

## Repository layout

| Path | |
|---|---|
| [src/cua/schema/](src/cua/schema/) | The capability artifact and the replay result contract |
| [src/cua/surface/](src/cua/surface/) | `Surface` protocol (14 methods) + the only file that imports Playwright |
| [src/cua/replay.py](src/cua/replay.py) | Deterministic execution — no model on this path |
| [src/cua/agent/](src/cua/agent/) | Discovery loop, tool definitions, prompt |
| [src/cua/recorder.py](src/cua/recorder.py) | Trajectory → artifact; synthesises locators from the live page |
| [src/cua/guardrails.py](src/cua/guardrails.py) | Allowlist and risk classification |
| [src/cua/escalation.py](src/cua/escalation.py) | Intervention requests and the live-session handoff |
| [src/cua/redaction.py](src/cua/redaction.py) | Secrets and regulated data |
| [config/policy.yaml](config/policy.yaml) | The guardrail, as reviewable config |
| [config/signals/](config/signals/) | Per-product runtime error taxonomy |
| [target_app/](target_app/) | MERIDIAN CORE, the legacy console |
| [artifacts/](artifacts/) | Saved capabilities |
| [evidence/](evidence/) | Committed run evidence — see [evidence/README.md](evidence/README.md) |

---

## Evidence

Fourteen committed runs, each a folder with `run.jsonl` (ordered trail),
`result.json` (typed result), and on failure a screenshot plus an accessibility
snapshot. Start with:

| Run | Shows |
|---|---|
| `…-discovery` (034705Z) | **The real LLM run.** 8 steps, model transcript, emitted artifact |
| `…-discovery-verify` | That artifact replaying, no model — what promoted it to `approved` |
| `…-discovery` (034500Z) | A discovery run that **failed its own verification**, kept deliberately |
| `…-replay-business-not-found` | A negative answer returned as an outcome, not an error |
| `…-replay-failure-app-error` | A hard failure with step, expected, observed |
| `…-manual-handoff-live` | A real human taking over a live session and handing it back |

The kept failure is the instructive one: the model proposed
`"Savings [REDACTED:account_number] 4210.55"` as proof it had arrived, because
it had been shown a redacted screen — and the recorder refused to save a
capability asserting something that does not hold. It's why observations now
mask secrets but not business data.

---

## Notes

- All data, credentials and account numbers are synthetic. `op.demo` /
  `demo-pass` are printed on the app's own sign-on page.
- `/__chaos` injects faults for demos and tests. It is deliberately **denied**
  by the allowlist, so the automation cannot reach it — there is a test that
  proves a run cannot switch off its own error conditions.
- Replaying the sub-account capability mutates fixture data; `chaos.py reset`
  restores it, and the tests reset between runs.
