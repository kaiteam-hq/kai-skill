---
name: kai-api
description: Work with the KAI API to configure, debug, and eval agents, and to analyze conversations. Use when asked to read/edit an agent's prompt/model/tools/config or version, debug or run a bot against test input, set up or run evals (test cases / judge), or list/dump/analyze conversations & messages via the KAI API.
---

# KAI API (configure / debug / eval / analyze agents)

KAI is a SaaS AI-agent platform. Its workspace API (the same one the web app uses) is
reachable with a **personal API token** (`Authorization: Bearer kai_...`). This skill
ships a single-file CLI — `kai.py` — that wraps the four workflows so you don't
hand-build curl/pagination/poll loops.

The full API spec is **`${KAI_BASE_URL}/api/openapi.yaml`** (RU; public, no auth — browse it as
Swagger UI at `${KAI_BASE_URL}/swagger`) — paths, request/response shapes, the
agent **config schema** (`AgentConfig` / `SingleAgentConfig` / `GraphAgentConfig` / `Tool`
/ `Function`), version/publish endpoints and eval shapes. If it ever disagrees with the
running server, trust this SKILL and its `kai.py`.

## Invocation

The CLI ships inside this plugin at `${CLAUDE_PLUGIN_ROOT}/skills/kai-api/kai.py`. The
examples below write `kai.py` for brevity — run it **cwd-independently** by expanding that
shorthand to the bundled path on every call. `$CLAUDE_PLUGIN_ROOT` is exported into every
shell, but per-shell aliases/vars do **not** persist between separate commands, so don't
rely on them — inline the full path each time:

```bash
python "$CLAUDE_PLUGIN_ROOT/skills/kai-api/kai.py" whoami     # i.e. the `kai.py whoami` below
```

## TL;DR

```bash
export KAI_API_TOKEN=kai_xxx                 # mint in UI: /account -> API Token -> Generate
export KAI_BASE_URL=https://saas.kaiteam.ru  # default if unset

kai.py whoami                               # verify the token  (see Invocation above)
kai.py workspaces                           # discover workspace ids
kai.py agents list --workspace <ws>         # agents in a workspace  (--workspace required)
```

## Setup

| Env | Meaning | Default |
|---|---|---|
| `KAI_API_TOKEN` | personal token `kai_...` | — (required) |
| `KAI_BASE_URL` | base URL **without** `/api` | `https://saas.kaiteam.ru` |

**Requirements:** Python 3.

Mint the token in the UI: click your name in the sidebar → `/account` → **API Token** →
`Generate`. The full `kai_...` value is shown **once** — copy it. The token acts as your
session across **all** your workspaces with full access. It **cannot** manage tokens
(create/revoke must be done in the UI). Details & limits:
`${KAI_BASE_URL}/doc/ru/api-token`. Point `kai.py` at a different environment by setting
`KAI_BASE_URL`.

**Workspace:** every workspace-scoped command takes a required `--workspace <ws>` (a token
can see several workspaces, so there's no implicit default). Get the ids from
`kai.py workspaces`. The example blocks below omit `--workspace` for brevity.

## Workflow 1 — configure an agent (file-based loop)

An agent's behavior (prompt / model / tools / extensions / flags) lives in its **config**,
stored per **version**. Each agent always has a mutable `draft` version; publishing
freezes the draft into an immutable `vN`. Edit by pulling the config to a file, editing it,
and pushing it back to the draft — don't poke the API for every tweak.

```bash
kai.py model-presets                                  # valid model_preset values
kai.py agents list                                    # find the agent id
kai.py config pull <agent_id> --mode draft            # -> <slug>.draft.md
#   ... edit the prompt (file body) and fields (frontmatter) in the file ...
kai.py config push <agent_id> --file <slug>.draft.md  # writes the DRAFT (free)
kai.py config show <agent_id> --mode draft            # confirm
kai.py config publish <agent_id>                       # NEW immutable vN — GATED (confirm)
```

`config push` always targets the draft (only the draft is editable; pushing a published
version is rejected with "Only draft version can be updated"). On a rejected push `kai.py`
prints the validation reason (empty/too-long prompt > 100000 chars, bad Jinja, unknown
preset) — fix the file and push again. `PUBLISHED` ⟺ the agent's
`draft_version_hash == latest_version_hash` (so you can tell if the draft has unpublished edits).

### Config file format

`config pull` writes a frontmatter+body file (like this SKILL.md): the big `prompt` is the
**body** (edit as plain text, no escaping); everything else is YAML frontmatter. Read-only
addressing metadata lives under `_kai:` and is ignored on push.

```md
---
_kai:                       # read-only, managed by kai.py
  agent_id: <id>
  slug: support-bot
  mode: draft
  version_id: <uuid>
  base_url: https://saas.kaiteam.ru
type: single_agent
model_preset: <preset>
write_first: true
bot_enabled_by_default: true
time_aware: false
tools: []
extensions: []
---
<prompt — the big Jinja template, edited as the file body>
```

Field reference: the human field guide is `${KAI_BASE_URL}/doc/ru/single-agent-format`
(and `.../graph-agent-format`); exact types & validation are `AgentConfig` & friends in
`${KAI_BASE_URL}/api/openapi.yaml`. For `graph_agent` there is no prompt: `nodes` go in the
frontmatter and the body is empty. The agent **type** cannot be changed.

To fill in `extensions` / `tools` / `on_timeout`, the docs are the source of truth for the
tool/extension model — don't reconstruct it here:
- concepts (what tools/extensions are, AUTO vs HIDDEN, `enabled_functions`, custom `tools`
  vs extension tools): `${KAI_BASE_URL}/doc/ru/tools`
- catalog (every extension's `name` / `settings` / tools / modes / arg shapes, and the
  `{extension, name, args}` call shape): `${KAI_BASE_URL}/doc/ru/agent-extensions`

## Workflow 2 — debug an agent

Run the agent against an ad-hoc conversation on the built-in `debug` channel. `kai.py`
auto-finds that channel and creates a synthetic user for you. `debug say` inserts your
message, triggers a turn, and polls for the agent's reply (the turn runs on a background
worker — see Gotchas).

```bash
kai.py debug new --agent <agent_id> --mode draft       # -> {conversation_id, user_id}
kai.py debug say <conversation_id> "привет"             # insert + run + print the reply
kai.py conversations dump <conversation_id>             # full transcript (incl. tool calls)
```

If a turn pauses on a tool call, the CLI says so — execute it for real and continue:

```bash
kai.py debug tool-exec <conversation_id> <tool_call_id>
kai.py debug run <conversation_id>
```

Craft history / state without generating: `debug msg-add` (insert any role),
`debug msg-edit` / `debug msg-del`, `debug fields <conv> k=v ...` (replace conversation_fields),
`users fields <user_id> k=v ...` (replace the synthetic user's fields — debug users only).

## Workflow 3 — eval an agent

Offline scoring: run test **cases** (a scripted conversation + pass/fail `criteria`)
through the agent, judged by an LLM whose prompt/model are the agent's **eval settings**.

**Add cases to pin behavior down.** Evals are the regression net for a prompt: whenever the
agent is supposed to do (or not do) something — hold a tone, refuse a request, call a
particular tool, fill a field — add a case that asserts it in `criteria`. Then every later
prompt/model/tool edit is re-checkable against the whole suite instead of eyeballing a few
manual chats, and you can tell a real fix from a regression. The loop: pull → edit the draft
(Workflow 1) → `eval run --mode draft --poll` → add/fix cases for anything that slipped →
publish only when green.

```bash
kai.py eval settings get <agent_id>
kai.py eval settings put <agent_id> --judge-prompt '...{{criteria}}...' --judge-model-preset <preset>
kai.py eval cases list <agent_id>
kai.py eval cases create <agent_id> --file case.json   # EvalCaseUpsert JSON
kai.py eval run <agent_id> --mode draft --poll         # start, wait, print passed/failed/errored
kai.py eval run <agent_id> --case-ids <id1>,<id2> --poll  # partial run — only these cases (default all)
kai.py eval runs <agent_id>                            # history
kai.py eval case-runs <agent_id> <run_id>              # per-case verdicts + judge explanations
```

`eval run --poll` waits until the run leaves `running`, then prints a per-case table
(`PASSED/FAILED/ERROR`, judge explanation, error category). **Cases in a run execute one at
a time (sequentially)** — a run takes about as long as the sum of its cases, so a full suite
gets slow as it grows. Pass `--case-ids id1,id2` (ids from `eval cases list`) to run only the
case(s) you're iterating on and get a verdict in a fraction of the time; omit it to run all
cases (e.g. the final pre-publish check). Case shapes: `EvalCaseUpsert` in `${KAI_BASE_URL}/api/openapi.yaml`.

## Workflow 4 — analyze conversations

```bash
kai.py conversations list --limit 20                   # newest first (cursor-paginated)
kai.py conversations list --all --channel-id <ch>      # all pages for a channel
kai.py conversations get <conversation_id>             # metadata + fields + tags
kai.py conversations dump <conversation_id>            # readable transcript (resolves user name)
kai.py conversations dump <conversation_id> --json     # full structured export (for bulk analysis)
kai.py users get <id1,id2>                             # resolve user names/contacts
kai.py users fields <user_id> k=v ...                  # replace user fields (full set; debug users only)
```

`dump` is the unit of analysis: it follows the message cursor to the end, stitches
`tool_calls` to their results, and resolves `user_id` → name. Use `--json` to feed a whole
conversation into further programmatic analysis.

## Gotchas

- **Running a turn is asynchronous.** `debug say` / `debug run` kick off generation and then
  **poll for the reply** — the turn is produced by a background **queue worker**, which must
  be running, or `debug say` will report "no new messages within …s".
- **Turns pause on tool calls in debug.** Real tools are not auto-executed; an assistant
  message with open `tool_calls` ends the polled turn → `debug tool-exec` then `debug run`.
- **The debug channel** is the one with `slug == "debug"` (`kai.py` finds it for you).
  Inserting messages / running / editing only works there (and on your own web test-chat);
  on any other conversation those commands are rejected.
- **`external_id` is required on every inserted message** (`debug say`/`msg-add` synthesize
  one). `role=tool` additionally needs `tool_call_id`.
- **Lists are paginated, and `kai.py` walks the pages for you** — `conversations list --all`
  and `dump` follow them to the end; without `--all`, `list` returns one page (newest first).
- **`agent_version_mode` (draft|latest)** is a property of a conversation/eval-run. You pass
  `--mode draft|latest` and `kai.py` resolves it to the concrete version for you.
- **Every write touches real data** (live conversations, LLM tokens spent on eval runs).
  Only **`config publish`** asks for confirmation; every other write runs immediately.
  Conversation bodies contain customer PII — don't paste raw dumps into PRs/issues/external
  services.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Token rejected / auth fails | `KAI_API_TOKEN` missing or invalid for this `KAI_BASE_URL` (tokens are per-environment). Re-mint and re-export. |
| Permission denied | Tried to manage tokens over the API (do it in the UI), wrong workspace, or you're not the chat owner. |
| "not found" on a workspace command | Wrong `--workspace`, or the resource lives in another workspace. `kai.py workspaces` to list. |
| `config push` rejected: "Only draft version can be updated" | Push only edits the **draft**; a published `vN` is immutable. Re-pull with `--mode draft` and push that. |
| `config push` rejected: prompt / preset / Jinja | Config validation. Fix the file (prompt ≤100000 chars, valid Jinja, `model_preset` from `kai.py model-presets`) and push again. |
| `debug say` prints "no new messages within Ns" | Queue worker not consuming the turn; check the worker is up, or raise `--timeout`. |
| Connection error | `KAI_BASE_URL` is wrong or unreachable. |

## References

- API spec — paths, request/response shapes, agent config schema (`AgentConfig` & friends),
  version/publish endpoints, eval shapes (`EvalCaseUpsert`, `EvalSettings`, `EvalMessage`):
  `${KAI_BASE_URL}/api/openapi.yaml` (RU; trust this SKILL on any conflict with the running server).
- API-token guide (mint / limits / revoke): `${KAI_BASE_URL}/doc/ru/api-token`.
- Config field guide (`single_agent` / `graph_agent` fields, defaults): `${KAI_BASE_URL}/doc/ru/single-agent-format`.
- Tools & extensions model (tools/extensions, AUTO/HIDDEN, custom `tools`): `${KAI_BASE_URL}/doc/ru/tools`.
- Extensions catalog (every extension's `name` / `settings` / tools / modes / args —
  needed to write `extensions` / `tools` / `on_timeout`): `${KAI_BASE_URL}/doc/ru/agent-extensions`.
