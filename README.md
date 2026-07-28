# AI Coding Agent — Notes App Feature Implementation

An AI agent that explores an unfamiliar repository, decides on a reasonable
product implementation for an under-specified request, and modifies the
codebase itself — using Claude's tool-use (function calling) in a fully
custom, framework-free agent loop.

Target repo: [`callicoder/node-easy-notes-app`](https://github.com/callicoder/node-easy-notes-app)
(Node.js + Express + Mongoose REST API for notes — no frontend in this repo).

Request given to the agent (verbatim, no extra guidance):
> "Improve the application so users can better organise and search their notes."

---

## 1. Architecture

```
ai-coding-agent/
├── agent/
│   ├── tools.py        # Sandboxed file/git tools + Anthropic tool schemas
│   ├── core_agent.py    # Generic Claude tool-use loop (the "engine")
│   ├── prompts.py       # One system prompt per phase
│   └── main.py          # CLI: wires the 4 phases together, writes artifacts
├── target_repo/         # The repo being modified (clone of node-easy-notes-app)
├── verify_setup.py       # Zero-API-cost sanity check of the tool layer
├── requirements.txt
└── .env.example
```

**Design principle:** no LangChain / AutoGPT / CrewAI. The whole agent is
~150 lines of plain Python calling the Anthropic Messages API directly with
`tools=[...]`. For an assignment like this, a framework hides exactly the
part that's being evaluated (the loop, the tool dispatch, the prompt
design), so I built it raw — it's also easier to explain line-by-line in
an interview.

### The engine (`core_agent.py`)

One reusable method, `Agent.run_phase(system_prompt, user_message, allow_tools)`:

1. Send `messages` + `tools` (Anthropic tool schemas) to Claude.
2. If the response contains `tool_use` blocks → execute each one locally
   against `RepoTools`, append the `tool_result`s to the conversation, and
   loop back to step 1.
3. If the response is plain text (no tool calls) → that's the phase's final
   answer; return it.
4. A `MAX_TURNS = 25` safety valve prevents infinite tool-call loops.

This one loop is reused for **all four phases** below — only the system
prompt and the tool whitelist change per phase.

### Tools (`tools.py`)

| Tool | Purpose | Notes |
|---|---|---|
| `list_directory` | Recursive tree listing | Skips `node_modules`, `.git`, etc. |
| `read_file` | Read a file with line numbers | Model must read before editing |
| `search_files` | Regex grep across the repo | Finds where a route/model/symbol is used |
| `write_file` | Create / fully overwrite a file | For new files |
| `edit_file` | Replace one exact snippet | For surgical, minimal diffs to existing files |
| `git` | Read-only (`status`/`diff`/`log`/`show`/`branch`) | Model uses `git diff --stat` to self-verify |

Every tool is bound to a `RepoTools(repo_root)` instance. `_resolve()`
resolves any path against `repo_root` and **raises `PermissionError` if the
resolved path escapes the repo root** — this blocks path traversal
(`../../etc/passwd`) even if the model is prompted (or prompt-injected) into
trying it. `git` is similarly whitelisted to read-only subcommands only, so
the model can inspect its own diff but can never `git push`, change remotes,
or rewrite history.

---

## 2. Agent Workflow (4 phases)

The whole thing intentionally is **not** "throw everything at Claude in one
big loop and hope." It's split into four small, auditable phases, each with
its own system prompt and its own tool whitelist:

| Phase | Tools available | Goal | Output artifact |
|---|---|---|---|
| **1. Explore** | read-only (`list_directory`, `read_file`, `search_files`, `git`) | Understand stack, data model, routes, conventions | `agent_artifacts/01_exploration.md` |
| **2. Plan** | *none* | Decide the actual feature (tags? search? both?) and produce an exact file-by-file plan | `agent_artifacts/02_plan.md` |
| **3. Implement** | full (read + write + edit + git) | Execute the plan, self-verify with `git diff --stat` | `agent_artifacts/03_implementation_log.md` |
| **4. Summarize** | `git` only | Summarize the **actual diff** (not what it thinks it did) | `agent_artifacts/04_summary.md` |

Why split it up like this, instead of one big agentic loop:

- **Planning gets no tools on purpose.** This forces Claude to commit to a
  design on paper before touching any file, instead of plan-while-coding
  (which is how you get half-finished, inconsistent features).
- **Summarize is grounded in `git diff`, not memory.** The model is told to
  call `git diff` itself in this phase rather than recall what it "did" —
  this avoids the classic failure mode of an agent describing a change it
  intended to make but didn't actually apply.
- **Each phase's output is a small Markdown artifact** written to
  `target_repo/agent_artifacts/`. This is what you'd show in the screen
  recording and what a reviewer reads instead of a 2,000-line raw
  transcript — every decision is traceable to a specific phase.

### How the repository is explored

Exploration is **fully agentic, not a hardcoded static scan** — the "explore
this codebase" system prompt in Phase 1 only tells Claude *what to figure
out* (tech stack, data model, relevant routes/controllers, existing
conventions), not *which files to open*. Claude itself decides, in order:

1. `list_directory('.')` to get the lay of the land.
2. `read_file` on whatever looks structurally important (`package.json`,
   `README`, then whatever `list_directory` revealed — e.g. `app/models/`,
   `app/routes/`, `app/controllers/`).
3. `search_files` if it needs to confirm how something is used elsewhere
   (e.g. is `note.model.js` imported anywhere else besides the controller).

This generalizes to **any** repo shape (it would explore a Java/Spring repo
or a React frontend the same way) — nothing about the exploration is
hardcoded to `node-easy-notes-app` specifically, which is exactly what the
follow-up interview will stress-test with a new request.

---

## 3. The Implementation Decision (what the agent actually builds)

Given the one-liner *"organise and search their notes"* and a codebase that
is: Express + Mongoose, single `Note { title, content, timestamps }` model,
5 REST routes, **no frontend, no auth, no multi-tenancy** — the agent is
prompted (in Phase 2) to pick something that is genuinely useful, fits the
existing shape, and doesn't invent scope that isn't there. The expected/
intended decision (confirmed by re-running the exploration by hand while
building this):

- **Add `tags: [String]`** to the `Note` schema — the natural "organise"
  primitive for a schema-less notes app, and backward compatible (existing
  notes without tags simply default to `[]`).
- **Extend `GET /notes`** to accept optional `?q=` (searches `title` +
  `content` via a Mongoose text index) and `?tag=` query params, combinable,
  with pagination-friendly sorting — instead of adding a brand-new route,
  since this preserves the existing endpoint contract for every current
  caller (backward compatible: no params = same behavior as before).
- **Add `GET /tags`** — a small new read endpoint to list all tags in use,
  which is what a real "browse by tag" UI would need.
- **Add `title`/`content`/`tags` MongoDB indexes** so search stays fast as
  notes grow.

This is a deliberate choice, not the only valid one — a "categories"
model, or full client-side full-text search, would also satisfy the prompt.
I picked **tags + combined search** because it needs no new collections, no
migration for existing data, and no new frontend that doesn't exist in this
repo already.

---

## 4. Assumptions & Trade-offs

- **No frontend exists in this repo** (`node-easy-notes-app` is the backend
  API only, per its own README — the React client is a separate repo in the
  original tutorial series). The agent is explicitly told not to invent one;
  scope is backend-only (schema + routes + indexes).
- **No auth/multi-tenancy** in the original app, so tags/search are global,
  not per-user — adding user accounts would be scope creep the one-line
  request doesn't ask for.
- **Mongoose `$text` index** is used for search rather than a search engine
  (Elasticsearch/Atlas Search) — appropriate for this app's scale, avoids
  adding new infra dependencies.
- **`edit_file` requires an exact, unique snippet match** rather than doing
  line-number-based patching — this is safer against the model
  mis-remembering line numbers, but means the model must re-`read_file`
  immediately before editing (enforced in the Phase-3 prompt) if the file
  may have changed since it was last read.
- **`MAX_TURNS = 25` per phase** is a blunt safety valve, not a smart
  budget/cost optimizer — for a repo this small it's more than enough
  headroom; a larger repo would need a smarter context/summarization
  strategy (out of scope for a 2-3 hour assignment).
- **No automated test run** after implementation (repo ships with no test
  suite — `npm test` is a stub). The agent verifies itself via
  `git diff --stat` + manual diff review, not by running the app.

---

## 5. How to Run

```bash
git clone https://github.com/callicoder/node-easy-notes-app.git target_repo
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# Optional, zero-cost check that the tool layer itself works:
python verify_setup.py --repo ./target_repo

# The real thing:
python -m agent.main --repo ./target_repo \
  --request "Improve the application so users can better organise and search their notes."
```

Output:
- Live console log of every phase and every tool call (`[tool] read_file(...)`, etc.)
- `target_repo/agent_artifacts/01_exploration.md` … `04_summary.md`
- The actual modified files inside `target_repo/`, verifiable with `git diff`
  inside `target_repo/`.

---

## 6. Generalizing to a New Request (follow-up interview)

Nothing in `tools.py` or `core_agent.py` is specific to notes/tags. To hand
the agent a brand-new request against the same repo, only the `--request`
string changes — the same explore → plan → implement → summarize pipeline
re-derives a fresh exploration + plan for whatever the new feature is. The
Phase-2 prompt is the one place product judgment happens; Phases 1, 3, 4 are
fully generic.
