#!/usr/bin/env python3
"""kai.py — CLI for the KAI HTTP API: configure / debug / eval / analyze agents.

Config & auth come from the environment:
  KAI_API_TOKEN     personal token (kai_...), REQUIRED. Mint it in the UI:
                    /account -> "API Token" -> Generate (shown once).
  KAI_BASE_URL      base URL WITHOUT the /api suffix. Default: https://saas.kaiteam.ru.

Workspace-scoped commands take --workspace <id> (get ids via `workspaces`).

Requires PyYAML (`pip install pyyaml`); otherwise stdlib only.

See SKILL.md for the full guide and gotchas. The API spec is public at ${KAI_BASE_URL}/api/openapi.yaml; if it ever
disagrees with the running server, trust SKILL.md.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    sys.exit("kai.py requires PyYAML — install it with `pip install pyyaml`.")

DEFAULT_BASE_URL = "https://saas.kaiteam.ru"
DEBUG_CHANNEL_SLUG = "debug"
DRAFT_SLUG = "draft"
# read-only metadata kai.py stores in a pulled config file (nested under `_kai`)
META_KEY = "_kai"


# --------------------------------------------------------------------------- #
# transport core
# --------------------------------------------------------------------------- #

def die(msg, code=1):
    print(f"kai: error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"kai: warning: {msg}", file=sys.stderr)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def base_url():
    return (os.environ.get("KAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def token():
    t = os.environ.get("KAI_API_TOKEN")
    if not t:
        die("KAI_API_TOKEN is not set. Mint it in the UI: /account -> API Token -> Generate.")
    return t


def request(method, path, body=None, query=None):
    """Call the API. Returns parsed JSON (or None for empty body). Exits on HTTP/network error."""
    url = base_url() + "/api" + path
    if query:
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
    headers = {"Authorization": f"Bearer {token()}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        detail = raw
        try:
            detail = json.loads(raw).get("error", raw)
        except Exception:
            pass
        hint = ""
        if e.code == 401:
            hint = "  (token rejected: bad/missing KAI_API_TOKEN for this KAI_BASE_URL)"
        elif e.code == 403:
            hint = "  (permission denied: token cannot manage tokens / wrong workspace / not owner)"
        elif e.code == 404:
            hint = "  (not found: wrong --workspace, or the resource is in another workspace)"
        elif e.code == 409:
            hint = "  (conflict: tool_call already has a result — `debug msg-del` it first / slug or login already taken)"
        die(f"{method} {path} -> HTTP {e.code}: {detail}{hint}", code=2)
    except urllib.error.URLError as e:
        die(f"{method} {path} -> connection error: {e.reason} (KAI_BASE_URL={base_url()})", code=3)


def workspace(args):
    """The --workspace id (argparse marks it required on workspace-scoped commands)."""
    return args.workspace


def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def kv_pairs(items):
    """['a=1', 'b=2'] -> {'a':'1','b':'2'}"""
    out = {}
    for it in items or []:
        if "=" not in it:
            die(f"expected key=value, got: {it!r}")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read JSON file {path}: {e}")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def get_agent(ws, agent_id):
    return request("GET", f"/workspaces/{ws}/agents/{agent_id}")


def list_versions(ws, agent_id):
    return request("GET", f"/workspaces/{ws}/agents/{agent_id}/versions") or []


def _vnum(version):
    s = version.get("slug", "")
    return int(s[1:]) if s.startswith("v") and s[1:].isdigit() else -1


def resolve_version(ws, agent_id, mode):
    """Resolve the version row for mode draft|latest (the version endpoints take an id, not a mode)."""
    versions = list_versions(ws, agent_id)
    if not versions:
        die("agent has no versions")
    if mode == "draft":
        v = next((x for x in versions if x["slug"] == DRAFT_SLUG), None)
        return v or die("draft version not found")
    published = [x for x in versions if x["slug"] != DRAFT_SLUG]
    if published:
        return max(published, key=_vnum)
    return next((x for x in versions if x["slug"] == DRAFT_SLUG), None) or die("no version found")


def draft_version_id(ws, agent_id):
    versions = list_versions(ws, agent_id)
    v = next((x for x in versions if x["slug"] == DRAFT_SLUG), None)
    return v["id"] if v else die("draft version not found")


def get_version_config(ws, agent_id, version_id):
    return request("GET", f"/workspaces/{ws}/agents/{agent_id}/versions/{version_id}")


def debug_channel_id(ws):
    channels = request("GET", f"/workspaces/{ws}/channels") or []
    ch = next((c for c in channels if c.get("slug") == DEBUG_CHANNEL_SLUG), None)
    return ch["id"] if ch else die("debug channel not found in this workspace")


def all_messages(ws, conv_id):
    """All messages of a conversation, oldest->newest, following the timestamp+id cursor."""
    out, cur_ts, cur_id = [], None, None
    while True:
        page = request("GET", f"/workspaces/{ws}/conversations/{conv_id}/messages",
                       query={"order": "asc", "limit": 500, "cursor_timestamp": cur_ts, "cursor_id": cur_id})
        page = page or []
        out.extend(page)
        if len(page) < 500:
            return out
        cur_ts, cur_id = page[-1]["timestamp"], page[-1]["id"]


def open_tool_calls(messages):
    """assistant tool_calls that have no matching role=tool result yet."""
    done = {m.get("tool_call_id") for m in messages if m["role"] == "tool" and m.get("tool_call_id")}
    pending = []
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            if tc.get("id") not in done:
                pending.append(tc)
    return pending


def fmt_message(m):
    label = m["role"].upper()
    if m["role"] == "tool":
        label = f"TOOL->{m.get('tool_call_id')}"
    content = (m.get("content") or "").rstrip()
    lines = [f"[{m.get('timestamp', '')}] {label}: {content}".rstrip()]
    for img in (m.get("images") or []):
        lines.append(f"        [image] {img}")
    if m.get("response_timeout"):
        lines.append(f"        (response_timeout={m['response_timeout']})")
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function", {})
        lines.append(f"        ⮡ {fn.get('name')}({fn.get('arguments')})  [tool_call_id={tc.get('id')}]")
    return "\n".join(lines)


def resolve_user_label(ws, user_id):
    if not user_id:
        return None
    users = request("GET", f"/workspaces/{ws}/users", query={"ids": user_id}) or []
    if not users:
        return user_id
    fields = users[0].get("fields", {})
    name = fields.get("fullname")
    return f"{name} <{user_id}>" if name else user_id


# --------------------------------------------------------------------------- #
# misc / discovery
# --------------------------------------------------------------------------- #

def cmd_whoami(args):
    print_json(request("GET", "/members/me"))


def cmd_workspaces(args):
    print_json(request("GET", "/workspaces"))


def cmd_model_presets(args):
    print_json(request("GET", "/model-presets"))


def cmd_channels(args):
    print_json(request("GET", f"/workspaces/{workspace(args)}/channels"))


def cmd_users_get(args):
    ws = workspace(args)
    print_json(request("GET", f"/workspaces/{ws}/users", query={"ids": args.ids}))


def cmd_users_fields(args):
    ws = workspace(args)
    request("PUT", f"/workspaces/{ws}/users/{args.user_id}/fields", body=kv_pairs(args.field))
    print("user fields replaced")


def cmd_users_me(args):
    """Get-or-create the web user of the current member (owner of the web test-chat)."""
    print_json(request("GET", f"/workspaces/{workspace(args)}/users/me"))


def cmd_users_delete(args):
    """Cascade-delete a debug user: its conversations (messages/fields/tags) + own fields/tags."""
    ws = workspace(args)
    request("DELETE", f"/workspaces/{ws}/users/{args.user_id}")
    print(f"deleted user {args.user_id} and all its conversations")


def cmd_members_list(args):
    print_json(request("GET", f"/workspaces/{workspace(args)}/members"))


def cmd_members_add(args):
    print_json(request("POST", f"/workspaces/{workspace(args)}/members", body={"login": args.login}))


def _confirm(banner, args):
    print(banner, file=sys.stderr)
    if args.confirm:
        print("  proceeding (--confirm)\n", file=sys.stderr)
        return
    try:
        ans = input("  type 'yes' to proceed: ").strip().lower()
    except EOFError:
        die("this action needs confirmation; re-run interactively or pass --confirm", code=1)
    if ans not in ("y", "yes"):
        die("aborted by user", code=0)


def cmd_channels_set(args):
    """Change a live channel's agent / version mode / enabled / notifications (PUT takes the full state)."""
    ws = workspace(args)
    channels = request("GET", f"/workspaces/{ws}/channels") or []
    ch = next((c for c in channels if c["id"] == args.channel_id or c.get("slug") == args.channel_id), None)
    if not ch:
        die(f"channel {args.channel_id} not found in workspace {ws}")
    body = {k: ch.get(k) for k in ("slug", "admin_chat_notifications", "agent_id", "agent_version_mode",
                                   "enabled", "bitrix_settings", "amo_settings", "email_settings")}
    changes = {}
    if args.agent_id is not None:
        changes["agent_id"] = args.agent_id
    if args.mode is not None:
        changes["agent_version_mode"] = args.mode
    if args.enabled is not None:
        changes["enabled"] = args.enabled
    if args.admin_chat_notifications is not None:
        changes["admin_chat_notifications"] = args.admin_chat_notifications
    if args.slug is not None:
        changes["slug"] = args.slug
    if not changes:
        die("nothing to change (pass --agent-id / --mode / --enabled|--disabled / --slug / ...)")
    body.update(changes)
    diff = ", ".join(f"{k}: {ch.get(k)!r} -> {v!r}" for k, v in changes.items())
    _confirm(f"\n  UPDATE live channel '{ch.get('slug')}' ({ch['id']}, platform={ch.get('platform')})\n"
             f"  target: {base_url()}  (affects live conversations on this channel)\n"
             f"  {diff}\n", args)
    request("PUT", f"/workspaces/{ws}/channels/{ch['id']}", body=body)
    print(f"updated channel {ch.get('slug')} ({ch['id']}): {diff}")


# --------------------------------------------------------------------------- #
# configure: agents + versions + config files
# --------------------------------------------------------------------------- #

def cmd_agents_list(args):
    print_json(request("GET", f"/workspaces/{workspace(args)}/agents"))


def cmd_agents_get(args):
    print_json(get_agent(workspace(args), args.agent_id))


def cmd_agents_create(args):
    ws = workspace(args)
    body = {"slug": args.slug, "type": args.type}
    if args.start_agent_id:
        body["start_agent_id"] = args.start_agent_id
    print_json(request("POST", f"/workspaces/{ws}/agents", body=body))


def cmd_agents_rename(args):
    ws = workspace(args)
    request("PATCH", f"/workspaces/{ws}/agents/{args.agent_id}", body={"slug": args.slug})
    print(f"renamed agent {args.agent_id} -> {args.slug}")


def cmd_versions_list(args):
    print_json(list_versions(workspace(args), args.agent_id))


def cmd_config_show(args):
    ws = workspace(args)
    version = resolve_version(ws, args.agent_id, args.mode)
    full = get_version_config(ws, args.agent_id, version["id"])
    if args.json:
        print_json(full)
    else:
        print_json(full["config"])


def cmd_config_pull(args):
    ws = workspace(args)
    agent = get_agent(ws, args.agent_id)
    version = resolve_version(ws, args.agent_id, args.mode)
    full = get_version_config(ws, args.agent_id, version["id"])
    config = dict(full["config"])

    meta = {
        "agent_id": args.agent_id,
        "slug": agent["slug"],
        "mode": args.mode,
        "version_id": version["id"],
        "version_slug": version["slug"],
        "base_url": base_url(),
    }
    prompt = config.pop("prompt", None) if config.get("type") == "single_agent" else None
    frontmatter = {META_KEY: meta, **config}
    text = ("---\n"
            + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
            + "---\n")
    if prompt is not None:
        text += prompt

    out = args.output or f"{agent['slug']}.{args.mode}.md"
    if out == "-":
        sys.stdout.write(text)
        return
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {out}  (agent={agent['slug']} mode={args.mode} version={version['slug']})")


def _parse_config_file(path):
    """Parse a frontmatter+body config file into (config_dict, meta_dict)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    if not raw.startswith("---"):
        die(f"{path}: missing YAML frontmatter (file must start with '---')")
    # split on the first two '---' fences; body keeps any later '---' (prompt may contain them)
    parts = raw.split("\n---", 1)
    if len(parts) != 2:
        die(f"{path}: malformed frontmatter (no closing '---')")
    fm_text = parts[0][len("---"):]
    body = parts[1]
    if body.startswith("\n"):
        body = body[1:]
    body = body.rstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        die(f"{path}: invalid YAML frontmatter: {e}")
    if not isinstance(fm, dict):
        die(f"{path}: frontmatter must be a mapping")
    meta = fm.pop(META_KEY, {}) or {}
    config = fm
    if config.get("type") == "single_agent":
        config["prompt"] = body
    return config, meta


def _check_meta(args, ws, meta):
    if meta.get("base_url") and meta["base_url"] != base_url():
        warn(f"file was pulled from {meta['base_url']} but KAI_BASE_URL={base_url()}")
    if meta.get("agent_id") and meta["agent_id"] != args.agent_id:
        warn(f"file's agent_id ({meta['agent_id']}) != argument ({args.agent_id})")
    if meta.get("mode") and meta["mode"] != DRAFT_SLUG:
        warn(f"file was pulled from mode={meta['mode']}; push always targets the draft version")


def cmd_config_push(args):
    ws = workspace(args)
    config, meta = _parse_config_file(args.file)
    _check_meta(args, ws, meta)
    vid = draft_version_id(ws, args.agent_id)
    updated = request("PUT", f"/workspaces/{ws}/agents/{args.agent_id}/versions/{vid}",
                      body={"config": config})
    print(f"pushed draft for agent {args.agent_id} (version {updated['slug']})")


def _confirm_publish(agent_slug, args):
    _confirm(f"\n  PUBLISH new immutable version of agent '{agent_slug}'\n"
             f"  target: {base_url()}  (affects live conversations)\n"
             f"  this creates v(N) and becomes a candidate for 'latest'.\n", args)


def cmd_config_publish(args):
    ws = workspace(args)
    agent = get_agent(ws, args.agent_id)
    if args.file:
        config, meta = _parse_config_file(args.file)
        _check_meta(args, ws, meta)
    else:
        vid = draft_version_id(ws, args.agent_id)
        config = get_version_config(ws, args.agent_id, vid)["config"]
    _confirm_publish(agent["slug"], args)
    created = request("POST", f"/workspaces/{ws}/agents/{args.agent_id}/versions",
                      body={"config": config})
    print(f"published {created['slug']} for agent {agent['slug']} ({args.agent_id})")


# --------------------------------------------------------------------------- #
# debug
# --------------------------------------------------------------------------- #

def cmd_debug_new(args):
    ws = workspace(args)
    channel_id = debug_channel_id(ws)
    user = request("POST", f"/workspaces/{ws}/users", body={"fields": kv_pairs(args.field)})
    conv = request("POST", f"/workspaces/{ws}/conversations", body={
        "user_id": user["id"],
        "agent_id": args.agent,
        "agent_version_mode": args.mode,
        "channel_id": channel_id,
        "tags": [],
        "conversation_fields": {},
    })
    print_json({"conversation_id": conv["id"], "user_id": user["id"],
                "agent_id": args.agent, "mode": args.mode})


def _insert_messages(ws, conv_id, drafts):
    return request("POST", f"/workspaces/{ws}/conversations/{conv_id}/messages", body=drafts)


def _run_and_poll(ws, conv_id, timeout):
    known = {m["id"] for m in all_messages(ws, conv_id)}
    request("POST", f"/workspaces/{ws}/conversations/{conv_id}/run")  # 202, empty body
    deadline = time.monotonic() + timeout
    interval = 1.0
    new = []
    while True:
        msgs = all_messages(ws, conv_id)
        new = [m for m in msgs if m["id"] not in known]
        if new:
            pending = open_tool_calls(msgs)
            if pending:
                return new, pending
            if msgs[-1]["role"] == "assistant":
                return new, []
        if time.monotonic() >= deadline:
            return new, []
        time.sleep(interval)
        interval = min(interval * 1.6, 4.0)


def _print_turn(new, pending, timeout):
    if not new:
        print(f"(no new messages within {timeout}s — is the queue worker running?)",
              file=sys.stderr)
        return
    for m in new:
        print(fmt_message(m))
    if pending:
        ids = ", ".join(tc.get("id") for tc in pending)
        print(f"\n(turn paused on open tool_calls: {ids})", file=sys.stderr)
        print(f"(execute them: `kai.py debug tool-exec <conv> <tool_call_id>`, then `kai.py debug run <conv>`)",
              file=sys.stderr)


def cmd_debug_say(args):
    ws = workspace(args)
    _insert_messages(ws, args.conversation_id, [{
        "role": "user",
        "content": args.text,
        "timestamp": now_iso(),
        "external_id": f"kai-cli-{uuid.uuid4()}",
    }])
    new, pending = _run_and_poll(ws, args.conversation_id, args.timeout)
    _print_turn(new, pending, args.timeout)


def cmd_debug_run(args):
    ws = workspace(args)
    new, pending = _run_and_poll(ws, args.conversation_id, args.timeout)
    _print_turn(new, pending, args.timeout)


def cmd_debug_tool_exec(args):
    ws = workspace(args)
    msg = request("POST",
                  f"/workspaces/{ws}/conversations/{args.conversation_id}/tool-calls/{args.tool_call_id}/execute")
    print(fmt_message(msg))


def cmd_debug_msg_add(args):
    ws = workspace(args)
    draft = {
        "role": args.role,
        "content": args.content,
        "timestamp": args.timestamp or now_iso(),
        "external_id": args.external_id or f"kai-cli-{uuid.uuid4()}",
    }
    if args.tool_call_id:
        draft["tool_call_id"] = args.tool_call_id
    if args.tool_calls:
        draft["tool_calls"] = read_json_file(args.tool_calls)
    print_json(_insert_messages(ws, args.conversation_id, [draft]))


def cmd_debug_msg_edit(args):
    ws = workspace(args)
    body = {"content": args.content}
    if args.tool_calls:
        body["tool_calls"] = read_json_file(args.tool_calls)
    print_json(request("PUT",
                       f"/workspaces/{ws}/conversations/{args.conversation_id}/messages/{args.message_id}",
                       body=body))


def cmd_debug_msg_del(args):
    ws = workspace(args)
    request("DELETE", f"/workspaces/{ws}/conversations/{args.conversation_id}/messages/{args.message_id}")
    print(f"deleted message {args.message_id}")


def cmd_debug_fields(args):
    ws = workspace(args)
    request("PUT", f"/workspaces/{ws}/conversations/{args.conversation_id}/fields",
            body=kv_pairs(args.field))
    print("conversation fields replaced")


# --------------------------------------------------------------------------- #
# eval
# --------------------------------------------------------------------------- #

def _eval_base(ws, agent_id):
    return f"/workspaces/{ws}/agents/{agent_id}"


def cmd_eval_settings_get(args):
    ws = workspace(args)
    print_json(request("GET", f"{_eval_base(ws, args.agent_id)}/eval-settings"))


def cmd_eval_settings_put(args):
    ws = workspace(args)
    if args.file:
        body = read_json_file(args.file)
    else:
        if not args.judge_prompt or not args.judge_model_preset:
            die("provide --file, or both --judge-prompt and --judge-model-preset")
        body = {"judge_prompt": args.judge_prompt, "judge_model_preset": args.judge_model_preset}
    print_json(request("PUT", f"{_eval_base(ws, args.agent_id)}/eval-settings", body=body))


def cmd_eval_cases_list(args):
    ws = workspace(args)
    print_json(request("GET", f"{_eval_base(ws, args.agent_id)}/eval-cases"))


def cmd_eval_cases_get(args):
    ws = workspace(args)
    print_json(request("GET", f"{_eval_base(ws, args.agent_id)}/eval-cases/{args.case_id}"))


def cmd_eval_cases_create(args):
    ws = workspace(args)
    print_json(request("POST", f"{_eval_base(ws, args.agent_id)}/eval-cases",
                       body=read_json_file(args.file)))


def cmd_eval_cases_update(args):
    ws = workspace(args)
    print_json(request("PUT", f"{_eval_base(ws, args.agent_id)}/eval-cases/{args.case_id}",
                       body=read_json_file(args.file)))


def cmd_eval_cases_delete(args):
    ws = workspace(args)
    request("DELETE", f"{_eval_base(ws, args.agent_id)}/eval-cases/{args.case_id}")
    print(f"deleted case {args.case_id}")


def cmd_eval_runs_list(args):
    ws = workspace(args)
    path = f"{_eval_base(ws, args.agent_id)}/eval-runs"
    if args.seq is not None:
        print_json(request("GET", path, query={"seq": args.seq}))
        return
    out, after_seq = [], None
    while True:
        page = request("GET", path, query={"limit": args.limit, "after_seq": after_seq}) or []
        out.extend(page)
        if not args.all or len(page) < args.limit:
            break
        after_seq = page[-1]["seq"]
    print_json(out)


def cmd_eval_run_get(args):
    ws = workspace(args)
    print_json(request("GET", f"{_eval_base(ws, args.agent_id)}/eval-runs/{args.run_id}"))


def cmd_eval_case_runs(args):
    ws = workspace(args)
    print_json(request("GET", f"{_eval_base(ws, args.agent_id)}/eval-runs/{args.run_id}/case-runs"))


def cmd_eval_cancel(args):
    ws = workspace(args)
    request("POST", f"{_eval_base(ws, args.agent_id)}/eval-runs/{args.run_id}/cancel")
    print(f"cancel requested for run {args.run_id}")


def cmd_eval_run(args):
    ws = workspace(args)
    base = _eval_base(ws, args.agent_id)
    body = {"agent_version_mode": args.mode}
    if args.case_ids:
        body["case_ids"] = [c.strip() for c in args.case_ids.split(",") if c.strip()]
    run = request("POST", f"{base}/eval-runs", body=body)
    print(f"started run seq={run['seq']} id={run['id']} mode={args.mode} "
          f"cases={run['cases_total']}", file=sys.stderr)
    if not args.poll:
        print_json(run)
        return

    deadline = time.monotonic() + args.timeout
    interval = 1.5
    while run["status"] == "running":
        if time.monotonic() >= deadline:
            print(f"(still running after {args.timeout}s; check later with "
                  f"`kai.py eval run-get {args.agent_id} {run['id']}`)", file=sys.stderr)
            break
        time.sleep(interval)
        interval = min(interval * 1.4, 5.0)
        run = request("GET", f"{base}/eval-runs/{run['id']}")

    case_runs = request("GET", f"{base}/eval-runs/{run['id']}/case-runs") or []
    slugs = {c["id"]: c["slug"] for c in (request("GET", f"{base}/eval-cases") or [])}
    print(f"run seq={run['seq']} status={run['status']}  "
          f"total={run['cases_total']} passed={run['cases_passed']} "
          f"failed={run['cases_failed']} errored={run['cases_errored']}")
    for cr in case_runs:
        name = slugs.get(cr["case_id"], cr["case_id"])
        line = f"  {cr['status'].upper():8} {name}"
        if cr.get("error"):
            line += f"  [error={cr['error']}]"
        if cr.get("explanation"):
            line += f"  — {cr['explanation']}"
        print(line)


# --------------------------------------------------------------------------- #
# analyze: conversations + users
# --------------------------------------------------------------------------- #

def cmd_conversations_list(args):
    ws = workspace(args)
    if args.agent_id and not args.channel_id:
        die("--agent-id is a channel-scoped filter: pass --channel-id as well (server returns 400 otherwise)")
    base_path = f"/workspaces/{ws}/conversations"
    out = []
    cur_ts = cur_id = after = None
    while True:
        q = {"order": args.order, "limit": args.limit,
             "channel_id": args.channel_id, "agent_id": args.agent_id}
        if args.channel_id:
            q["after_updated_at"] = after
        else:
            q["cursor_updated_at"] = cur_ts
            q["cursor_id"] = cur_id
        page = request("GET", base_path, query=q) or []
        out.extend(page)
        if not args.all or len(page) < args.limit or not page:
            break
        last = page[-1]
        cur_ts, cur_id = last["updated_at"], last["id"]
        after = last["updated_at"]
    print_json(out)


def cmd_conversations_get(args):
    ws = workspace(args)
    print_json(request("GET", f"/workspaces/{ws}/conversations/{args.conversation_id}"))


def cmd_conversations_dump(args):
    ws = workspace(args)
    conv = request("GET", f"/workspaces/{ws}/conversations/{args.conversation_id}")
    msgs = all_messages(ws, args.conversation_id)
    if args.json:
        print_json({"conversation": conv, "messages": msgs})
        return
    user = resolve_user_label(ws, conv.get("user_id"))
    print(f"# conversation {conv['id']}")
    print(f"# agent={conv.get('agent_id')} mode={conv.get('agent_version_mode')} "
          f"channel={conv.get('channel_id')} status={conv.get('status')}")
    if user:
        print(f"# user: {user}")
    if conv.get("tags"):
        print(f"# tags: {', '.join(conv['tags'])}")
    if conv.get("fields"):
        print(f"# fields: {json.dumps(conv['fields'], ensure_ascii=False)}")
    print(f"# {len(msgs)} messages")
    print()
    for m in msgs:
        print(fmt_message(m))


# --------------------------------------------------------------------------- #
# argument parser
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="kai.py",
        description="CLI for the KAI API: configure / debug / eval / analyze agents.",
        epilog="Auth via env KAI_API_TOKEN; base via KAI_BASE_URL; workspace via --workspace (get ids from `kai.py workspaces`).",
    )
    ws = argparse.ArgumentParser(add_help=False)
    ws.add_argument("--workspace", required=True, help="workspace id (get ids from `kai.py workspaces`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="current member").set_defaults(func=cmd_whoami)
    sub.add_parser("workspaces", help="list your workspaces").set_defaults(func=cmd_workspaces)
    sub.add_parser("model-presets", help="list valid model_preset names").set_defaults(func=cmd_model_presets)
    chs = sub.add_parser("channels", help="list channels / change a channel's agent & mode").add_subparsers(dest="sub", required=True)
    chs.add_parser("list", parents=[ws], help="list channels (find the debug channel)").set_defaults(func=cmd_channels)
    g = chs.add_parser("set", parents=[ws], help="update a live channel (GATED — confirmation)")
    g.add_argument("channel_id", help="channel id or slug")
    g.add_argument("--agent-id", dest="agent_id")
    g.add_argument("--mode", choices=["draft", "latest"], help="agent_version_mode")
    g.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    g.add_argument("--disabled", dest="enabled", action="store_false")
    g.add_argument("--admin-chat-notifications", dest="admin_chat_notifications", action="store_true", default=None)
    g.add_argument("--no-admin-chat-notifications", dest="admin_chat_notifications", action="store_false")
    g.add_argument("--slug")
    g.add_argument("--confirm", action="store_true", help="skip the interactive prompt")
    g.set_defaults(func=cmd_channels_set)

    # members ----------------------------------------------------------------
    me = sub.add_parser("members", help="workspace members (accounts, not end-users)").add_subparsers(dest="sub", required=True)
    me.add_parser("list", parents=[ws]).set_defaults(func=cmd_members_list)
    g = me.add_parser("add", parents=[ws], help="add an existing member to the workspace by login")
    g.add_argument("--login", required=True); g.set_defaults(func=cmd_members_add)

    # agents -----------------------------------------------------------------
    ag = sub.add_parser("agents", help="agent CRUD").add_subparsers(dest="sub", required=True)
    ag.add_parser("list", parents=[ws]).set_defaults(func=cmd_agents_list)
    g = ag.add_parser("get", parents=[ws]); g.add_argument("agent_id"); g.set_defaults(func=cmd_agents_get)
    g = ag.add_parser("create", parents=[ws])
    g.add_argument("--slug", required=True)
    g.add_argument("--type", choices=["single_agent", "graph_agent"], default="single_agent")
    g.add_argument("--start-agent-id", dest="start_agent_id", help="required for graph_agent")
    g.set_defaults(func=cmd_agents_create)
    g = ag.add_parser("rename", parents=[ws]); g.add_argument("agent_id"); g.add_argument("--slug", required=True)
    g.set_defaults(func=cmd_agents_rename)

    # versions ---------------------------------------------------------------
    ve = sub.add_parser("versions", help="agent versions").add_subparsers(dest="sub", required=True)
    g = ve.add_parser("list", parents=[ws]); g.add_argument("agent_id"); g.set_defaults(func=cmd_versions_list)

    # config -----------------------------------------------------------------
    cf = sub.add_parser("config", help="read/edit/publish agent config (prompt/model/tools)").add_subparsers(dest="sub", required=True)
    g = cf.add_parser("show", parents=[ws], help="print config to stdout")
    g.add_argument("agent_id"); g.add_argument("--mode", choices=["draft", "latest"], default="draft")
    g.add_argument("--json", action="store_true", help="include version envelope")
    g.set_defaults(func=cmd_config_show)
    g = cf.add_parser("pull", parents=[ws], help="write config to a frontmatter+prompt file")
    g.add_argument("agent_id"); g.add_argument("--mode", choices=["draft", "latest"], default="draft")
    g.add_argument("-o", "--output", help="output file (default <slug>.<mode>.md; '-' = stdout)")
    g.set_defaults(func=cmd_config_pull)
    g = cf.add_parser("push", parents=[ws], help="write a config file back to the DRAFT (free)")
    g.add_argument("agent_id"); g.add_argument("--file", required=True)
    g.set_defaults(func=cmd_config_push)
    g = cf.add_parser("publish", parents=[ws], help="create a new immutable version (GATED — confirmation)")
    g.add_argument("agent_id"); g.add_argument("--file", help="config file (else publishes current draft)")
    g.add_argument("--confirm", action="store_true", help="skip the interactive prompt")
    g.set_defaults(func=cmd_config_publish)

    # debug ------------------------------------------------------------------
    db = sub.add_parser("debug", help="run an agent against an ad-hoc debug conversation").add_subparsers(dest="sub", required=True)
    g = db.add_parser("new", parents=[ws], help="create a debug conversation (+synthetic user)")
    g.add_argument("--agent", required=True, help="agent id")
    g.add_argument("--mode", choices=["draft", "latest"], default="draft")
    g.add_argument("--field", action="append", help="user field key=value (repeatable)")
    g.set_defaults(func=cmd_debug_new)
    g = db.add_parser("say", parents=[ws], help="insert a user message, run a turn, print the response")
    g.add_argument("conversation_id"); g.add_argument("text")
    g.add_argument("--timeout", type=float, default=60.0)
    g.set_defaults(func=cmd_debug_say)
    g = db.add_parser("run", parents=[ws], help="run a turn (e.g. after tool-exec), print the response")
    g.add_argument("conversation_id"); g.add_argument("--timeout", type=float, default=60.0)
    g.set_defaults(func=cmd_debug_run)
    g = db.add_parser("tool-exec", parents=[ws], help="execute a pending tool_call for real")
    g.add_argument("conversation_id"); g.add_argument("tool_call_id")
    g.set_defaults(func=cmd_debug_tool_exec)
    g = db.add_parser("msg-add", parents=[ws], help="insert a message without running (craft history)")
    g.add_argument("conversation_id")
    g.add_argument("--role", required=True, choices=["user", "assistant", "tool"])
    g.add_argument("--content")
    g.add_argument("--timestamp", help="ISO-8601 (default now)")
    g.add_argument("--external-id", dest="external_id")
    g.add_argument("--tool-call-id", dest="tool_call_id", help="required for role=tool")
    g.add_argument("--tool-calls", dest="tool_calls", help="JSON file with a tool_calls array")
    g.set_defaults(func=cmd_debug_msg_add)
    g = db.add_parser("msg-edit", parents=[ws]); g.add_argument("conversation_id"); g.add_argument("message_id")
    g.add_argument("--content"); g.add_argument("--tool-calls", dest="tool_calls")
    g.set_defaults(func=cmd_debug_msg_edit)
    g = db.add_parser("msg-del", parents=[ws]); g.add_argument("conversation_id"); g.add_argument("message_id")
    g.set_defaults(func=cmd_debug_msg_del)
    g = db.add_parser("fields", parents=[ws], help="replace conversation_fields (full set)")
    g.add_argument("conversation_id"); g.add_argument("field", nargs="*", help="key=value ...")
    g.set_defaults(func=cmd_debug_fields)

    # eval -------------------------------------------------------------------
    ev = sub.add_parser("eval", help="evaluate an agent against test cases").add_subparsers(dest="sub", required=True)
    settings = ev.add_parser("settings").add_subparsers(dest="sub2", required=True)
    g = settings.add_parser("get", parents=[ws]); g.add_argument("agent_id"); g.set_defaults(func=cmd_eval_settings_get)
    g = settings.add_parser("put", parents=[ws]); g.add_argument("agent_id")
    g.add_argument("--judge-prompt", dest="judge_prompt"); g.add_argument("--judge-model-preset", dest="judge_model_preset")
    g.add_argument("--file", help="JSON {judge_prompt, judge_model_preset}")
    g.set_defaults(func=cmd_eval_settings_put)
    cases = ev.add_parser("cases").add_subparsers(dest="sub2", required=True)
    g = cases.add_parser("list", parents=[ws]); g.add_argument("agent_id"); g.set_defaults(func=cmd_eval_cases_list)
    g = cases.add_parser("get", parents=[ws]); g.add_argument("agent_id"); g.add_argument("case_id"); g.set_defaults(func=cmd_eval_cases_get)
    g = cases.add_parser("create", parents=[ws]); g.add_argument("agent_id"); g.add_argument("--file", required=True); g.set_defaults(func=cmd_eval_cases_create)
    g = cases.add_parser("update", parents=[ws]); g.add_argument("agent_id"); g.add_argument("case_id"); g.add_argument("--file", required=True); g.set_defaults(func=cmd_eval_cases_update)
    g = cases.add_parser("delete", parents=[ws]); g.add_argument("agent_id"); g.add_argument("case_id"); g.set_defaults(func=cmd_eval_cases_delete)
    g = ev.add_parser("run", parents=[ws], help="start an eval run; --poll to wait for results")
    g.add_argument("agent_id"); g.add_argument("--mode", choices=["draft", "latest"], default="draft")
    g.add_argument("--case-ids", dest="case_ids", help="comma-separated case ids (default all)")
    g.add_argument("--poll", action="store_true"); g.add_argument("--timeout", type=float, default=300.0)
    g.set_defaults(func=cmd_eval_run)
    g = ev.add_parser("runs", parents=[ws], help="list eval runs (seq DESC)"); g.add_argument("agent_id")
    g.add_argument("--limit", type=int, default=50); g.add_argument("--all", action="store_true", help="follow after_seq to the end")
    g.add_argument("--seq", type=int, help="look up a single run by its UI number (seq)")
    g.set_defaults(func=cmd_eval_runs_list)
    g = ev.add_parser("run-get", parents=[ws]); g.add_argument("agent_id"); g.add_argument("run_id"); g.set_defaults(func=cmd_eval_run_get)
    g = ev.add_parser("case-runs", parents=[ws]); g.add_argument("agent_id"); g.add_argument("run_id"); g.set_defaults(func=cmd_eval_case_runs)
    g = ev.add_parser("cancel", parents=[ws]); g.add_argument("agent_id"); g.add_argument("run_id"); g.set_defaults(func=cmd_eval_cancel)

    # conversations / users --------------------------------------------------
    cv = sub.add_parser("conversations", help="browse & analyze conversations").add_subparsers(dest="sub", required=True)
    g = cv.add_parser("list", parents=[ws])
    g.add_argument("--channel-id", dest="channel_id"); g.add_argument("--agent-id", dest="agent_id", help="requires --channel-id")
    g.add_argument("--order", choices=["asc", "desc"], default="desc")
    g.add_argument("--limit", type=int, default=50); g.add_argument("--all", action="store_true")
    g.set_defaults(func=cmd_conversations_list)
    g = cv.add_parser("get", parents=[ws]); g.add_argument("conversation_id"); g.set_defaults(func=cmd_conversations_get)
    g = cv.add_parser("dump", parents=[ws], help="full transcript (any conversation); --json for raw export")
    g.add_argument("conversation_id"); g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_conversations_dump)

    us = sub.add_parser("users", help="resolve / manage end-users").add_subparsers(dest="sub", required=True)
    g = us.add_parser("get", parents=[ws]); g.add_argument("ids", help="comma-separated user ids (max 200)")
    g.set_defaults(func=cmd_users_get)
    g = us.add_parser("me", parents=[ws], help="your own web user (get-or-create; owner of the web test-chat)")
    g.set_defaults(func=cmd_users_me)
    g = us.add_parser("fields", parents=[ws], help="replace user fields (full set; debug users only)")
    g.add_argument("user_id"); g.add_argument("field", nargs="*", help="key=value ...")
    g.set_defaults(func=cmd_users_fields)
    g = us.add_parser("delete", parents=[ws], help="cascade-delete a debug user + its conversations (debug users only)")
    g.add_argument("user_id"); g.set_defaults(func=cmd_users_delete)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
