# Computer-Use Automation System

An LLM works out how to do a job inside a UI that has no API. That run is
recorded as a typed, versioned **capability artifact**. From then on the job is
done by **replaying the artifact with no model involved** — under a policy,
with evidence, and with a human reachable when it gets stuck.

Design write-up: **[REPORT.md](REPORT.md)**. Run evidence: **[evidence/](evidence/)**.

This branch also points the same core at a **live hosted target** and wraps it in a
capability API, a chatbot and a dashboard — jump to
[the hosted target](#the-hosted-target--capability-api-chatbot-dashboard), or read
**[ADAPTATION.md](ADAPTATION.md)** for what that took.

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

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate           # macOS/Linux

pip install -r requirements.txt       # includes `-e .`, so `cua` is importable
python -m playwright install chromium
```

Activate the venv in every new terminal. Without it you get
`ModuleNotFoundError: No module named 'cua'`.

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
and all 197 tests never load a model library. There is a test that asserts
exactly that: it removes `anthropic` from `sys.modules`, replays a capability,
and checks it never comes back.

---

## Demo path — the local target

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
.venv/Scripts/python.exe -m pytest -q                # 197 tests, ~4 min, real browser
.venv/Scripts/python.exe -m pyright --pythonpath .venv/Scripts/python.exe \n    src/cua service tests scripts
```

`demo_surface.py` is the clearest single view of the hard part: it shows
`role+name` **failing** on an input with no accessible name, then `label_cell`
resolving it, and a deliberately stale locator degrading to rank 1.

---

## The hosted target — capability API, chatbot, dashboard

Everything above is the core driving the local console. The **same core** is
pointed at the hosted MERIDIAN CORE at <https://web-sample.interface-hiring.com>
by two config files and nothing else. Seven capabilities are recorded against
it, exposed as an HTTP API, and driven from a chatbot and a dashboard.

What that adaptation actually took, and the six core changes it provoked:
**[ADAPTATION.md](ADAPTATION.md)**.

### Config

Add to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...       # discovery, and the chatbot's planner
ANTHROPIC_WORKSPACE_ID=wrkspc_...  # only if the key is organisation-scoped
CUA_OPERATOR_ID=teller1            # the operator the service signs on as
CUA_OPERATOR_PASSWORD=password
```

Operator credentials are read here and injected server-side after planning.
They are never typed into the chat, never sent to the model, and appear in run
records as `[REDACTED:secret]`.

**Running with no API key.** The chatbot falls back to a deterministic keyword
planner and the whole system still works end to end; each page prints which
planner answered, so a scripted reply is never mistaken for a model's. Replay
never needed a key in the first place.

### Start the service

```bash
.venv/Scripts/python.exe scripts/serve.py
```

One process, all three surfaces. **Headed by default** — a real browser window
opens and you watch it drive the console, which is the point. `--headless`
hides it; `--port N` moves it.

| | |
|---|---|
| <http://127.0.0.1:8000/ui> | capability catalog, one invoke form each |
| <http://127.0.0.1:8000/ui/chat> | the chatbot |
| <http://127.0.0.1:8000/ui/runs> | run history |
| <http://127.0.0.1:8000/docs> | OpenAPI, with try-it-out |

### Demo path

Open **`/ui/chat`** and type these in order. Each is a real replay against the
live host; watch the browser window follow along.

**1. A successful capability.**

> What is the balance for member 102777?

```
Done. Member name: Johnson, Katherine. Regular shares account number:
102777-S0001. Regular shares balance: $41,980.00.
```

Follow **steps, evidence and screenshots →** for the per-step trail: which
locator strategy resolved each step, its rank, and the checkpoint result.

**2. A business outcome — the host says no, and that is an answer.**

> check the balance for member 999999

```
No member record matches the supplied identifier. (MEMBER_NOT_FOUND)
```

The badge reads `business_outcome`, not `failed`.

**3. The guardrail refusing.**

> Transfer 1.00 from 102777-MMKT-4 to 102777-MMKT-3 for member 102777 with memo demo

```
I can't run that unattended. 'meridian_hosted.member.transfer_funds' is draft,
not approved for unattended use (max risk: irreversible)...
```

No run starts. The chatbot has no path around the gate.

**4. Escalation — the same request, supervised.** Tick **"An operator is
supervising"** and send it again. It runs 14 of 15 steps and stops:

> **Waiting for a human** — `post_the_transfer` · *irreversible action on 'Post
> Transfer' requires operator approval*

The browser is paused on the confirmation screen holding the control token, one
click short of moving money. Open the run page for **Approve & resume** /
**Abort run**, plus a dropdown to choose the resume step. Approving finishes it
with a confirmation reference.

**5. A supervisor-only action attempted by a teller.** On `/ui`, invoke
`place_account_hold` with `operator_id` `teller1` — it stops at step 12 of 13
with `SUPERVISOR_OVERRIDE_REQUIRED`, before the irreversible step. Re-run with
`super1` and it pauses for approval instead, then completes.

### The same thing over HTTP

```bash
# the catalog an agent would read
curl -s localhost:8000/capabilities?product=meridian-hosted | python -m json.tool

# invoke -> 202 and a run id; poll it
curl -s -X POST localhost:8000/capabilities/meridian_hosted.member.read_balance/invoke \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"member_id":"102777"}}'
curl -s localhost:8000/runs/<run_id> | python -m json.tool

# a draft capability, refused with 409 before a browser exists
curl -i -X POST localhost:8000/capabilities/meridian_hosted.member.transfer_funds/invoke \
  -H 'Content-Type: application/json' -d '{"arguments":{}}'
```

Runs execute one at a time — that falls out of the control token, since a
session has a single holder.

### Recording a new capability against the hosted target

```bash
.venv/Scripts/python.exe scripts/discover.py \
  --id meridian_hosted.member.read_balance \
  --name "Read a member's share balance" \
  --product meridian-hosted --product-version v4.2.1 \
  --entry-url https://web-sample.interface-hiring.com/signon \
  --policy config/policy.meridian-hosted.yaml \
  --param member_id=102777 --pattern member_id='^\d{6}$' \
  --param operator_id=teller1 \
  --secret operator_password=password \
  --goal "Sign on as teller1, look up member 102777 by member number, open the
          member record, and read the name plus the account number, balance and
          status of the Regular Shares account"
```

`--policy` is how a target is selected: the core has no idea this host exists.
Add `--approve-irreversible` for a flow that posts something — you are standing
in for the operator, and it is recorded in the evidence like any other
intervention.

Two maintenance tools:

```bash
# review a recorded capability the way its approver should
.venv/Scripts/python.exe scripts/audit_capability.py --strict artifacts/meridian_hosted*.json

# re-copy the product's error taxonomy into artifacts recorded before it changed
.venv/Scripts/python.exe scripts/sync_signals.py --product meridian-hosted
```

`audit_capability.py` is the one to run before trusting a capability: it prints
approval, risk, checkpoint coverage, and flags any checkpoint that looks pinned
to the record it was recorded against.

### Capabilities

| Capability | | Risk |
|---|---|---|
| `session.sign_on` | operator, password, branch | safe |
| `member.find_by_name` | search by last name | safe |
| `member.read_balance` | shares, balance, status | safe |
| `member.update_information` | email, phone, address | reversible_write |
| `member.transfer_funds` | from, to, amount, memo → review → post | **irreversible** |
| `member.open_new_share` | type, deposit → review → post | **irreversible** |
| `member.place_account_hold` | share, reason, notes → review → post | **irreversible**, supervisor-gated |

The three irreversible ones stay `draft`: verifying by replay would perform the
transaction again, so they need a person. The other four were auto-verified by
replay and promoted to `approved`.

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
| [service/](service/) | Capability API, chatbot and dashboard — clients of the core, not part of it |
| [config/policy.meridian-hosted.yaml](config/policy.meridian-hosted.yaml) | The adapter for the hosted target |
| [target_app/](target_app/) | MERIDIAN CORE, the local legacy console |
| [artifacts/](artifacts/) | Saved capabilities |
| [evidence/](evidence/) | Committed run evidence — see [evidence/README.md](evidence/README.md) |

---

## Evidence

Every run writes a folder: `run.jsonl` (ordered trail), `result.json` (typed
result), and on failure a screenshot plus an accessibility snapshot. All of it
is committed.

**Against the local console** (`20260822*`, `20260823*`):

| Run | Shows |
|---|---|
| `…-discovery` (034705Z) | **The real LLM run.** 8 steps, model transcript, emitted artifact |
| `…-discovery-verify` | That artifact replaying, no model — what promoted it to `approved` |
| `…-discovery` (034500Z) | A discovery run that **failed its own verification**, kept deliberately |
| `…-replay-business-not-found` | A negative answer returned as an outcome, not an error |
| `…-replay-failure-app-error` | A hard failure with step, expected, observed |
| `…-manual-handoff-live` | A real human taking over a live session and handing it back |

**Against the hosted target** (`20260904*`, `20260905*`):

| Run | Shows |
|---|---|
| `…-discovery` (213107Z) | Recording `open_new_share` — the model driving a live review→post flow |
| `…-api-transfer_funds` (044300Z) | **Escalation end to end**: paused at the irreversible step, approved, completed |
| `…-api-transfer_funds` (232545Z) | A real overdraw returned as `TRANSACTION_REJECTED`, no funds moved |
| `…-api-place_account_hold` (084504Z) | A teller refused a supervisor-only action, before the irreversible step |
| `…-api-place_account_hold` (084511Z) | The same capability completing under `super1` |
| `…-api-update_information` (212017Z) | Field validation returned as `INPUT_REJECTED` |
| `…-api-read_balance` (013316Z) | `MEMBER_NOT_FOUND` — a lookup that legitimately found nothing |

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
