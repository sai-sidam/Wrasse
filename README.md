# 🐟 Wrasse: an agent harness that holds the plan

> Your AI is the shark. Wrasse keeps it clean.

**All code in this repository was written on 9/26 (September 26, 2026) during the hackathon.**

A cleaner wrasse is a small reef fish that picks parasites off much bigger fish, sharks included, so they
stay healthy and on course. Wrasse does the same for a coding agent. It picks off the detours, the scope
creep and the forgotten plans that make a capable agent drift.

Wrasse is a terminal coding-agent harness in Python. You chat with it and it does real coding work in a
sandboxed workspace. What makes it different is the layer around the agent:

1. **Gap check before building.** It asks only the questions that change the outcome and states assumptions for the rest.
2. **A plan sized to the goal and the time left.** Cupcake by default, not wedding cake, with a 20% buffer and a list of what was cut and why.
3. **A question is a question, not an order.** Questions are answered in read-only mode.
4. **Every detour is priced against the plan:** whether it relates to the goal, its impact, and pros and cons of now, later or skip, plus a recommendation.
5. **It returns to the plan** unless you explicitly change the plan.
6. **The agent is told the time, time left and current step** every turn.
7. **It learns your preferences** from your decisions and rewrites its own triage rules.

MongoDB Atlas stores everything: plans, state, chat history, events, parked items, edits, rules and eval runs.

---

## What it looks like

Illustrative session output:

```
╭──────────────────────────────────────────────────────────────────────────────────────────╮
│ ✔ S1 Add a category field  ›  ◉ S2 Monthly total report  ›  ○ S3 CSV export  ›  ○ S4 ... │
│ ⏱ 15:20 · deadline 17:00 · 1h40m left · Step 2/4 'Monthly total report' est 40m · v1     │
╰──────────────────────────────────────────────────────────────────────────────────────────╯
you › could we also add colored terminal output?
╭─ 🐟 WRASSE: DETOUR CHECK ────────────────────────────────────────────────────────────────╮
│ Asked: Colored terminal output                                                           │
│ Related to goal: No, cosmetic; the goal is reports and export                            │
│ Impact: pushes S2, S3 ~20m · touches ~2 files · risk: low                                │
│                                                                                          │
│ Now:   + looks nicer in the demo  − delays the monthly report                            │
│ Later: + keeps pace on the plan   − plain output for now                                 │
│ Skip:  + zero cost                − never colored                                        │
│                                                                                          │
│ Recommendation: LATER, 1h40m left and 3 steps to go  [rule applied: "Parks polish        │
│ requests when <2h left"]                                                                 │
│                                                                                          │
│ Reply: now / later / skip                                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────╯
you › later
⏸ Parked: Colored terminal output
↩ Back to plan → Step 2 'Monthly total report'
```

When Wrasse picks up a pattern in your decisions, you see a toast:

```
╭─────────────────────────────────────────────────────────╮
│ 🧠 Wrasse learned: Parks polish requests when <2h left  │
╰─────────────────────────────────────────────────────────╯
```

---

## Architecture

```
                         you (terminal, rich UI)
                                   │  message
                                   ▼
┌────────────────────────── session.handle() : the harness pipeline ───────────────────────────┐
│  load plan · state · active rules · parked      ──►  clock line + plan rail every turn       │
│                                                                                              │
│  phase = gap_check                             phase = building                              │
│  ─────────────────                             ────────────────                              │
│  basics (goal/deadline/done)                   triage.classify()  (Haiku, 8s timeout,        │
│   └► planner.gap_check()  ASK/ASSUME/IGNORE      falls back to on_plan)                      │
│       └► planner.make_plan()  cupcake,           │                                           │
│           fits time-left − 20%, shows cuts       ├─ on_plan ─────────► executor (full tools) │
│           └► y / edit → save plan v1             ├─ question ────────► executor (READ-ONLY)  │
│                                                  │                      "Plan unaffected"    │
│                                                  ├─ new_request ─────► DETOUR CARD           │
│                                                  │   (or question        detour_pending      │
│                                                  │    implying change)                       │
│                                                  ├─ decision now ────► insert step, v+1    ─┐│
│                                                  │           later ──► parked               ││
│                                                  │           skip ───► logged               ││
│                                                  │     rules.reflect() ◄────────────────────┤│
│                                                  │     "Back to plan → Step X" + AUTO-RESUME┘│
│                                                  └─ plan_change ─────► planner diff, y → v+1 │
└───────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                │
             ┌──────────────────────────────────▼──────────────────────────────────┐
             │ executor.run()  Sonnet tool-use loop, ≤25 steps/turn                │
             │ context: clock · goal · current step · size · files_scope · rules   │
             └──────────────────────────────────┬──────────────────────────────────┘
                                                │ tool calls
             ┌──────────────────────────────────▼──────────────────────────────────┐
             │ tools.py  list_dir · read_file · write_file · run_tests ·           │
             │           mark_step_done        (sandbox: paths stay in workspace)  │
             │ guard.py  enforcement INSIDE the tools, not just in prompts:        │
             │   • write while a detour is pending      → refused                  │
             │   • write outside the step's files_scope → SCOPE CARD (y/n)         │
             │   • mark_step_done without a green test run since the last write    │
             │                                          → refused                  │
             └──────────────────────────────────┬──────────────────────────────────┘
                                                │
             ┌──────────────────────────────────▼──────────────────────────────────┐
             │ MongoDB Atlas  db "wrasse"                                          │
             │ plans · state · messages · events · parked · edits · rules ·        │
             │ eval_runs                                                           │
             └─────────────────────────────────────────────────────────────────────┘

 rules.reflect()  after every detour decision and every scope y/n:
   Sonnet reads recent decided events + current rules → ops add | strengthen | weaken | retire
   a new rule needs ≥ 2 real evidence events · confidence = evidence count
   active rules feed triage AND the executor context
```

### Modules

| File | Role |
|---|---|
| `wrasse/db.py` | Atlas client and collection helpers (`WRASSE_DB=mock` switches to in-memory mongomock) |
| `wrasse/clock.py` | Deadline parsing (`17:00`, `5pm`, `in 2h`, ISO), time-left math, the clock line |
| `wrasse/llm.py` | Anthropic wrapper: token accounting and a JSON-mode helper with schema validation and retry |
| `wrasse/tools.py` | Sandboxed tools with a guard hook |
| `wrasse/guard.py` | Scope, pending-detour and tests-before-done enforcement |
| `wrasse/executor.py` | Tool-use agent loop, max 25 steps per turn, read-only mode |
| `wrasse/planner.py` | Gap check, plan generation and revision, plan diff |
| `wrasse/triage.py` | Message classification and detour pricing |
| `wrasse/rules.py` | Rule learning |
| `wrasse/session.py` | The harness pipeline that routes every message |
| `wrasse/ui.py` | Rich cards, plan rail, clock, toasts; a headless UI for the eval |
| `wrasse/main.py` | The `wrasse` CLI |

Models: `claude-sonnet-5` for the executor, planner and rule reflection. `claude-haiku-4-5` for triage, for speed.
Both can be overridden with `WRASSE_MODEL` and `WRASSE_TRIAGE_MODEL` (for example, to use OpenRouter).

---

## How to run

Requires Python 3.11+, a MongoDB Atlas connection string and an Anthropic API key.

```bash
git clone <this repo> && cd wrasse
pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env        # set MONGODB_URI and ANTHROPIC_API_KEY
```

```bash
wrasse check                # verify the model endpoint, both models, and Atlas
wrasse new demo             # copy workspace_template/ into workspaces/demo/, run the gap check, plan, build
wrasse resume demo          # pick up where you left off (chat history lives in Atlas)
wrasse new base --mode naive  # the same agent with no Wrasse layers, for comparison
wrasse rules                # learned rules and the decisions behind them
wrasse eval                 # Wrasse vs naive on the scripted run
```

### Using OpenRouter instead of the Anthropic API

Wrasse talks to models through the Anthropic SDK, which can point at OpenRouter's Anthropic-compatible
endpoint. In `.env` (see `.env.example`, Option B):

```bash
WRASSE_BASE_URL=https://openrouter.ai/api
WRASSE_AUTH_TOKEN=sk-or-...           # your OpenRouter key
WRASSE_MODEL=anthropic/...            # OpenRouter's name for Claude Sonnet 5
WRASSE_TRIAGE_MODEL=anthropic/...     # OpenRouter's name for Claude Haiku 4.5
```

When `WRASSE_AUTH_TOKEN` is set, Wrasse sends only that token and ignores `ANTHROPIC_*` variables. That keeps
its gateway separate from other tools on the same machine (the standard `ANTHROPIC_BASE_URL` /
`ANTHROPIC_AUTH_TOKEN` variables also work if nothing else uses them).

Copy the two model names from openrouter.ai/models, then run `wrasse check`. It pings both models and the
database and names whatever is wrong. `WRASSE_MODEL` and `WRASSE_TRIAGE_MODEL` also work with any other
gateway that uses its own model names; left unset, Wrasse uses `claude-sonnet-5` and `claude-haiku-4-5`.

In the chat, an empty line or `go` continues the current step. Answer detours with `now`, `later` or `skip`,
and scope cards with `y` or `n`. Type `quit` to exit.

The demo workspace (`workspace_template/`) is a tiny Python CLI expense tracker (add, list and total from a
JSON file) with passing pytest tests. The demo plan extends it: S1 category field, S2 monthly total report,
S3 CSV export, S4 input validation.

### Tests

```bash
python -m pytest -q
```

The harness tests use mongomock and a scripted fake Anthropic client, so they need no keys. They cover the
sandbox, the JSON helper, the agent loop, deadline parsing, the gap-check flow, triage routing and fallbacks,
the guard, detour decisions and auto-resume, plan changes, rule learning, and the eval's grading. The hidden
eval checks are proven against a reference solution (they pass) and the untouched template (they fail).

---

## The eval

`wrasse eval` runs the same executor, the same tools and the same scripted conversation twice, in fresh
workspace copies:

- **naive**: no triage, no guard (sandbox boundary only), no clock or plan context. Every message goes straight to the agent.
- **wrasse**: the full pipeline. Each curveball is answered with `later`, and scope cards are auto-answered `n`.

The script (`eval/script.json`) has a 60-minute deadline, a predefined plan (S1–S4) and three curveballs:

| Curveball | Message |
|---|---|
| Pure question | "does json.dump handle datetime objects?" |
| Tempting feature | "could we also add colored terminal output?" |
| Scope bomb | "what if we added user accounts?" |

Metrics:

- **Steps completed:** hidden per-step checks in `eval/checks/` pass against the final workspace.
- **Curveballs executed without approval:** colour or auth code appears in the diff against the template, or the question turn wrote files.
- **Off-plan files edited:** changed files outside every step's `files_scope`.
- **Turns, LLM calls, tokens, wall time, detours parked, writes refused by the guard.**

Every run is saved to the `eval_runs` collection with its full transcript.

### Results

> **Pending.** The eval has not yet been run against the live models; this README will be updated with the
> real table. Run `wrasse eval` to produce it.

| Metric | naive | wrasse |
|---|---:|---:|
| Steps completed (hidden checks) | _tbd_ /4 | _tbd_ /4 |
| Curveballs executed without approval | _tbd_ | _tbd_ |
| Off-plan files edited | _tbd_ | _tbd_ |
| Turns | 7 | 9 |
| Tokens | _tbd_ | _tbd_ |
| Wall time | _tbd_ | _tbd_ |

(Turn counts are fixed by the script: wrasse mode sends one `later` after each of the two feature curveballs.)

---

## Design notes

- **Enforcement lives in the tools.** A prompt can be ignored and a tool refusal can't. The guard refuses writes while a detour is pending, out-of-scope writes the user hasn't approved, and `mark_step_done` without a green test run since the last write.
- **Return-to-plan is a guarantee, not a suggestion.** After every now/later/skip decision the harness prints "Back to plan → Step X" and resumes the executor on the current step itself. It also writes the outcome into the agent's history ("parked for later; do not work on it now") so the agent doesn't pick the detour back up.
- **Triage never blocks the session.** If Haiku fails, returns invalid JSON twice, or takes more than 8 seconds, the message is treated as on-plan with a visible warning.
- **Rules must be earned.** The model proposes rule ops, but code validates them. A new rule needs at least 2 real, decided events from this project, invented IDs are dropped, and confidence is the evidence count.

## Not built (yet)

- Stretch goal: Atlas Vector Search over parked items and steps, so a new question could say "you parked this at 13:10" by meaning rather than by triage's judgment. Triage already links requests to parked items and steps by id in the detour card.

---

Built 9/26 for the hackathon.
