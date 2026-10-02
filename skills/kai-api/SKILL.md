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
/ `Function`), version/publish endpoints and eval shapes. The spec itself warns that the
contract is **not frozen** and may change incompatibly; this SKILL and `kai.py` track spec
`0.8.7`. If they ever disagree with the running server, trust this SKILL and its `kai.py`.
A few discovery endpoints `kai.py` uses (`/members/me`, `/workspaces`, `/model-presets`) are
not in the spec but are live.

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
kai.py channels list --workspace <ws>       # channels (platform, bound agent, draft|latest)
kai.py members list --workspace <ws>        # member accounts of the workspace
```

## Setup

| Env | Meaning | Default |
|---|---|---|
| `KAI_API_TOKEN` | personal token `kai_...` | — (required) |
| `KAI_BASE_URL` | base URL **without** `/api` | `https://saas.kaiteam.ru` |

**Requirements:** Python 3.

Token management (`/members/api-token`: view / create / revoke) is cookie-session only —
with a Bearer token the server answers 403, so `kai.py` has no command for it; use the UI.

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

### Which version is live where

A channel runs an agent in `agent_version_mode` `latest` (newest published `vN`) or
`draft`. To point a channel at another agent, flip it to the draft, or disable it:

```bash
kai.py channels list                                   # id / slug / platform / agent / mode / enabled / settings
kai.py channels set <channel_id|slug> --mode draft     # GATED (confirm) — affects live traffic
kai.py channels set <channel_id|slug> --agent-id <id> --enabled|--disabled
kai.py channels set <channel_id|slug> --settings-file amo.json   # replace the platform block (see below)
kai.py channels create --file channel.json             # new email|bitrix channel (server verifies mailbox/portal)
```

`channels set` reads the channel, merges your flags and PUTs the full state back (channel
platform settings — email/bitrix/amo — are carried over untouched; write-only passwords stay
as they are). `--settings-file` replaces the platform block instead: bitrix
`{"ignore_filter": [...], "webhook_url": null}` (non-empty `webhook_url` is re-verified on
the portal), amo `{"disabled_sources": [...], "source_id": null|int}`, email
`EmailSettingsInput` (omit passwords to keep them). A channel with no agent bound (e.g.
`debug`) can only be PUT together with `--agent-id`. The `debug` channel cannot be renamed
or disabled. `channels create` takes the POST body as JSON — `slug`, `platform`
(`email`|`bitrix`), `agent_id`, optional `agent_version_mode`, plus `email_settings`
(`EmailSettingsInput`, passwords required) or `bitrix_settings` (`webhook_url`,
`openline_id`, `ignore_filter`); the server does a loopback/portal check (up to 30 s) and
returns 400 without creating anything if it fails. Other platforms (tg, vk, web, ...) are
created in the UI.

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
`${KAI_BASE_URL}/api/openapi.yaml`. `single_agent` fields beyond the example above (all
optional, defaults shown by `config pull`): `hello_msg`, `timeout` (ISO-8601 timedelta,
e.g. `PT30S`) + `on_timeout` (extension `Function`s), `on_channel_not_available`,
`possible_attachments`, `imitate_human` + `imitate_human_timings`, `ping` (`const` |
`delays`, `start`/`end` window, `approve`). For `graph_agent` there is no prompt: `nodes`
(`start` node required; `agent` / `condition` node types, no cycles) go in the frontmatter
and the body is empty. The agent **type** cannot be changed.

To fill in `extensions` / `tools` / `on_timeout`, the docs are the source of truth for the
tool/extension model — don't reconstruct it here:
- concepts (what tools/extensions are, AUTO vs HIDDEN, `enabled_functions`, custom `tools`
  vs extension tools): `${KAI_BASE_URL}/doc/ru/tools`
- catalog (every extension's `name` / `settings` / tools / modes / arg shapes, and the
  `{extension, name, args}` call shape): `${KAI_BASE_URL}/doc/ru/agent-extensions`

### Writing the prompt — OpenAI per-model guides

Agents run on OpenAI GPT models; the preset name encodes the model (`kai.py model-presets`).
Before a substantial prompt rewrite or a `model_preset` switch, read the **Prompting best
practices** section (plus **Migration quickstart** when switching) of OpenAI's guide for
that model. `<base>` = `https://developers.openai.com/api/docs/guides` (pages are plain
markdown):

| `model_preset` | Model | Guide |
|---|---|---|
| `gpt-6-luna` | GPT-6 Luna (reasoning) | `<base>/latest-model/gpt-6-astra.md` — one page for the GPT-6 family, examples are Astra-centric |
| `gpt-5-6-luna` | GPT-5.6 Luna (reasoning) | `<base>/latest-model/gpt-5.6.md` |
| `gpt-5-mini` | GPT-5 mini (reasoning) | `<base>/latest-model/gpt-5.md` |
| `gpt-4-1-mini` | GPT-4.1 mini | `<base>/latest-model/gpt-4.1.md` |
| `gpt-4o-mini` | GPT-4o mini | no dedicated guide — `<base>/prompt-engineering.md` |

A preset not listed here maps the same way (`gpt-X-Y-<tier>` → `latest-model/gpt-X.Y.md`;
there is no page per tier). `<base>/prompt-engineering.md` covers model-agnostic basics
(roles, structure, few-shot examples) for any preset.

What works in a prompt shifts between generations, so a prompt rarely carries over as is.
GPT-4o/4.1 are non-reasoning: they need explicit, step-level instructions, and 4.1 follows
them more literally than 4o (implicit rules are no longer inferred). The reasoning models do
better with the goal and constraints than with a prescribed procedure. GPT-5.5+ prefer
short outcome-first prompts over walls of ALWAYS/NEVER/MUST, and GPT-5.6 is terser by
default, so a blanket "be brief" can clip its replies.

The guides target API developers, and much of them is about coding agents and API
parameters (`reasoning.effort`, `text.verbosity`, tool APIs). A KAI config has no such
fields — the preset fixes them — so take only the advice about the prompt text. Check
every guide-driven rewrite against the eval suite (Workflow 3) before publishing.

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

Re-running a tool call whose result already exists is rejected with 409 — `debug msg-del` the
old tool message first.

Craft history / state without generating: `debug msg-add` (insert any role),
`debug msg-edit` / `debug msg-del`, `debug fields <conv> k=v ...` (replace conversation_fields),
`users fields <user_id> k=v ...` (replace the synthetic user's fields — debug users only).
Known user field keys (rendered into the prompt): `fullname`, `phone`, `email`, `language`,
`country`; `debug new --field k=v` sets them at creation.

**Clean up** when done: every `debug new` creates a synthetic user + conversation.
`kai.py users delete <user_id>` cascade-deletes the debug user with all its conversations
(messages, fields, tags); the server refuses it for non-debug users.

**Web test-chat** (a real web channel instead of `debug`): `kai.py conversations new
--channel <web_channel> --agent <id> --mode latest` creates a conversation owned by your own
web user (`kai.py users me`, get-or-create, non-debug) — the bot greets on creation, and only
you can push messages into it. `--channel debug` is equivalent to `debug new`. Such
conversations are not deleted by `users delete` (your user is not a debug user).

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
kai.py eval runs <agent_id>                            # history (seq DESC; --all to page, --seq N for one)
kai.py eval case-runs <agent_id> <run_id>              # per-case verdicts + judge explanations
kai.py eval cancel <agent_id> <run_id>                 # stop a run; finished cases keep their verdicts
```

`eval run --poll` waits until the run leaves `running`, then prints a per-case table
(`PASSED/FAILED/ERROR`, judge explanation, error category). **Cases in a run execute one at
a time (sequentially)** — a run takes about as long as the sum of its cases, so a full suite
gets slow as it grows. Pass `--case-ids id1,id2` (ids from `eval cases list`) to run only the
case(s) you're iterating on and get a verdict in a fraction of the time; omit it to run all
cases (e.g. the final pre-publish check). Case shapes: `EvalCaseUpsert` in `${KAI_BASE_URL}/api/openapi.yaml`
— `slug` (kebab-case, unique per agent), `criteria` (substituted for `{{criteria}}` in the
judge prompt), `messages` (`user`/`assistant` only; a tool result lives inside the assistant
message's `tool_calls[].response`), `conversation_fields`, `user_fields`. For a
`graph_agent`, an assistant message that carries `tool_calls` must also set `agent_id` to
the graph node that emitted it.

`ERROR` verdicts carry a category: `agent_error` (generation raised), `agent_no_message`
(turn produced nothing), `judge_error`, `agent_loop_limit` (tool-call loop),
`tool_simulator_error` (LLM tool simulator failed). Runs are identified by UUID `id` in the
API and by the 1-based `seq` in the UI — `eval runs --seq N` maps one to the other.

## Workflow 4 — analyze conversations

```bash
kai.py conversations list --limit 20                   # newest first (cursor-paginated)
kai.py conversations list --all --channel-id <ch>      # all pages for a channel
kai.py conversations list --channel-id <ch> --agent-id <a>   # agent filter is channel-scoped (needs --channel-id)
kai.py conversations get <conversation_id>             # metadata + fields + tags + channel_link
kai.py conversations dump <conversation_id>            # readable transcript (resolves user name)
kai.py conversations dump <conversation_id> --json     # full structured export (for bulk analysis)
kai.py users get <id1,id2>                             # resolve user names/contacts (≤200 ids; foreign ids are silently omitted)
kai.py users fields <user_id> k=v ...                  # replace user fields (full set; debug users only)
kai.py users delete <user_id>                          # cascade-delete a debug user + its conversations
```

`dump` is the unit of analysis: it follows the message cursor to the end, stitches
`tool_calls` to their results, lists attached `images` URLs and the time-aware
`response_timeout`, and resolves `user_id` → name. Use `--json` to feed a whole
conversation into further programmatic analysis. Conversation `status` values:
`active`, `closing`, `fast_closing`, `closed`, `timeout`, `bot_is_disabled`, `bot_is_blocked`.

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
  Only **`config publish`** and **`channels set`** ask for confirmation (`--confirm` to skip);
  every other write runs immediately. `users delete` is irreversible but the server limits
  it to debug users.
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
| `debug tool-exec` → HTTP 409 | The tool_call already has a result. `debug msg-del` that tool message, then re-run. |
| `conversations list` → 400 "agent_id filter requires channel_id" | `--agent-id` only works together with `--channel-id`. |
| `debug new` → 400 "no published version" | `--mode latest` on an agent that was never published; use `--mode draft` or `config publish`. |
| `users delete` / `users fields` → 400 | Only debug (synthetic) users can be deleted/edited; real end-users are read-only. |
| `channels set` → "no agent bound" / 400 "Agent not found" | PUT needs a full state incl. `agent_id`; pass `--agent-id`. |
| `channels create` → 400 | Validation or the loopback/portal check failed (`{"error": ...}` says which); nothing was created. |
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
- OpenAI prompting guide for the preset's model (best practices, migration between models):
  `https://developers.openai.com/api/docs/guides/latest-model/<model>.md` — see the
  preset → guide table in "Writing the prompt" (Workflow 1).
