# Computer-Use Automation System

An LLM works out how to complete a task inside a legacy UI that has no API. The
successful run is recorded as a typed, versioned **capability**. From then on
that capability replays deterministically, with no model in the decision loop,
and escalates to a human when it must not proceed alone.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

The design write-up is in **[REPORT.md](REPORT.md)**. Evidence from real runs is
in **[evidence/](evidence/README.md)**.

---

## Setup

Python 3.12+. From the repository root:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/Scripts/python.exe -m playwright install chromium
```

On macOS or Linux use `.venv/bin/python` throughout.

**No API key is needed for anything below.** Replay, the guardrails, the human
handoff, the evidence set and the whole test suite run without a model. A key
is only required for a discovery run — copy `.env.example` to `.env` and set
`ANTHROPIC_API_KEY` when you want one.

## Demo path

**Terminal 1 — the target application.** Leave it running.

```bash
python -m target_app.app
```

`MERIDIAN CORE`, a deliberately legacy credit-union servicing console at
<http://127.0.0.1:5057>. Sign on with `op.demo` / `demo-pass` and click
around: **Member Search → `10001` → Open Record**. View source to see what the
automation is up against — a real `<frameset>`, nested tables, no IDs or test
hooks, form fields named `f1`/`f2`/`f3`, navigation via `<td onclick>`, and text
inputs with **no accessible name at all**.

**Terminal 2 — replay a capability.**

```bash
python scripts/replay.py artifacts/meridian.member.read_savings_balance@v1.json \
    --param member_id=10001 \
    --param operator_id=op.demo --param operator_passphrase=demo-pass
```

Returns `member_name`, `savings_balance` (typed as a number, not the string the
screen shows) and `savings_account_number` in about four seconds. Add
`--headed --slow 300` to watch it, or `--evidence demo` to write a run trail.

**Same artifact, different answers.** Nothing changes but the input:

```bash
... --param member_id=99999    # business_outcome MEMBER_NOT_FOUND
... --param member_id=20002    # business_outcome PERMISSION_DENIED
... --param member_id=oops     # failed invalid_input, with zero steps executed
```

The first two are **not failures**. The automation worked; the answer was
negative, and the caller gets a code to branch on.

**Trigger a runtime fault**, then replay again:

```bash
python scripts/chaos.py notice   # a maintenance interstitial -> recovered, still succeeds
python scripts/chaos.py error    # an application fault page  -> failed app_error
python scripts/chaos.py expire   # a lapsed session           -> escalates to a human
python scripts/chaos.py reset
```

**A capability that changes something.** Twelve steps run unattended, then it
stops dead at the click that creates the account:

```bash
python scripts/replay.py artifacts/meridian.member.open_subaccount@v1.json \
    --param member_id=10001 --param product_type=Holiday \
    --param opening_deposit=150.00 --param nickname="Holiday Fund" \
    --param operator_id=op.demo --param operator_passphrase=demo-pass
```

→ `escalation_unresolved`. Policy classifies `Submit Request` as irreversible
and requires a person. Add `--operator` and you become that person: the browser
is handed to you, you approve, and the run completes.

**See the automation reason.** A narrated walk-through of how each control is
located, including a deliberately stale locator degrading to a fallback:

```bash
python scripts/demo_surface.py
```

**Everything at once.**

```bash
python -m pytest -q                        # 96 tests against the real browser, ~2 min
python scripts/make_evidence.py --clean    # regenerate all 10 evidence runs
```

## Repository layout

| Path | What it is |
|---|---|
| `src/cua/schema/` | The capability artifact and the replay result contract |
| `src/cua/surface/` | The perceive/act seam. `web.py` is the only file that imports Playwright |
| `src/cua/replay.py` | Deterministic replay — no model on this path |
| `src/cua/guardrails.py` | Allowlist and risk enforcement, driven by `config/policy.yaml` |
| `src/cua/escalation.py` | Intervention requests and human control transfer |
| `src/cua/evidence.py` | Run trails, screenshots, accessibility snapshots |
| `artifacts/` | Two capabilities: one read-only, one that creates a record |
| `config/policy.yaml` | The entire guardrail, as reviewable configuration |
| `target_app/` | MERIDIAN CORE, the intentionally hostile proxy target |
| `evidence/` | Ten committed runs — [start here](evidence/README.md) |

## Running without live services

`python -m pytest` starts the target application in-process on an ephemeral
port; nothing external is required. `scripts/make_evidence.py` starts it too if
it is not already running.

The one thing that cannot be faked is a discovery run — brief section 4 requires
a genuine LLM-driven run against a live surface, and that needs a model key.

## Design decisions in brief

Full reasoning in [REPORT.md](REPORT.md).

- **The accessibility tree is the perception channel, not the DOM.** The
  console's inputs have no accessible name, so a single locator cannot address
  them. Every target carries a *ranked list* of strategies plus written
  reasoning, and replay records which rank actually matched — a fallback is the
  cheapest drift signal there is.
- **A run ends exactly three ways**: `success`, `business_outcome`, `failed`.
  "Recovered" is not a status; recoveries are recorded in the envelope, because
  by the time a run ends it has still either achieved the goal, learned a
  business answer, or failed.
- **Checkpoints and error signals are raced, not sequenced.** Checking success
  first and looking for errors afterwards misreports a slow "no such member" as
  a timeout — the exact mistake the brief singles out.
- **Risk is assigned by policy, never by the model**, from route, action kind
  and the control being activated. Filling a field is not a write; clicking
  Submit is.
- **Irreversible actions route to a human rather than being blocked.** Blocking
  would make the sub-account flow unrecordable; routing means the flow
  completes, a person owns the decision, and the approval is in the evidence.
- **A handoff never creates a new session.** The control token moves; the
  browser does not. What the human did is captured by watching the live session,
  not by asking them.
