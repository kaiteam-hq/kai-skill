# kai-skill

Claude Code plugin marketplace for the **KAI** agent platform.

It ships one plugin — `kai-api` — whose skill lets Claude Code **configure, debug, and
eval** KAI agents and **analyze** conversations through the public KAI API, using only a
personal API token. No access to the KAI source repo is needed.

## Install

```bash
/plugin marketplace add https://sourcecraft.dev/kaiteam/kai-skill.git
/plugin install kai-api@kai
```

> The marketplace must be cloneable by your client. Anonymous HTTPS clone requires the
> `kaiteam` org to be public; until then, members can use the SSH remote
> `ssh://ssh.sourcecraft.dev/kaiteam/kai-skill.git`.

Then provide your token (and, if not prod, a base URL):

```bash
export KAI_API_TOKEN=kai_xxx                 # mint in the UI: /account -> API Token -> Generate
export KAI_BASE_URL=https://saas.kaiteam.ru  # default if unset
```

The skill auto-activates when you ask Claude Code to work with KAI agents or conversations.

## What's inside

- `skills/kai-api/SKILL.md` — the guide: four workflows (configure / debug / eval / analyze).
- `skills/kai-api/kai.py` — a stdlib-only CLI over the API. PyYAML is needed only for the
  `config pull/push/publish` file commands; everything else runs with any `python3`.

## Reference

- Swagger UI: <https://saas.kaiteam.ru/swagger>
- OpenAPI spec: <https://saas.kaiteam.ru/api/openapi.yaml>
- Docs: <https://saas.kaiteam.ru/doc/ru/>
