# hermes-swe2 — Devin CLI (SWE-2) model provider for Hermes Agent

Use **SWE-2** (and the rest of Cognition's SWE family) as the model inside
[Hermes Agent](https://github.com/NousResearch/hermes-agent) by routing prompts
through your own locally installed **Devin CLI**.

Cognition publishes no per-token inference API for SWE-2. What it does ship is
`devin acp` — a documented [Agent Client Protocol](https://agentclientprotocol.com)
server built so third-party editors (Zed, etc.) can drive Devin as a subprocess.
This plugin uses that same surface: Hermes acts as the ACP client, exactly like
an editor would, and Devin CLI authenticates with **your** credentials.

## Requirements

- [Devin CLI](https://docs.devin.ai/cli) installed and on `PATH`
  (`curl -fsSL https://cli.devin.ai/install.sh | bash`)
- Authenticated once: `devin auth login` — or `WINDSURF_API_KEY` / `DEVIN_API_KEY`
  exported in the environment
- SWE-2 access on your plan (it is a subscriber-gated model; enterprise admins
  can allowlist models via team settings)

## Install

Pick one:

```bash
# 1. Plugin install (recommended) — lands in $HERMES_HOME/plugins/devin/
hermes plugins install <this-repo-url>

# 2. Manual — model-provider plugin directory
mkdir -p ~/.hermes/plugins/model-providers/devin
cp __init__.py plugin.yaml devin_provider.py devin_acp_client.py ~/.hermes/plugins/model-providers/devin/

# 3. pip entry point — then enable it under plugins.enabled in config.yaml
pip install .
```

Then select it:

```bash
hermes model            # pick "Devin CLI (SWE-2)" → e.g. swe-2
# or
hermes --provider devin --model swe-2
```

## Configuration

| Env var | Purpose |
| --- | --- |
| `HERMES_DEVIN_ACP_COMMAND` / `DEVIN_CLI_PATH` | Override the `devin` binary path |
| `HERMES_DEVIN_ACP_ARGS` | Override the argv (default: `acp`) — shlex-split |
| `DEVIN_API_KEY` / `WINDSURF_API_KEY` | Credential handed to the child + offered via ACP `authenticate` |

Model names are Devin CLI short names (`swe-2`, `swe`, `swe-1-7`, `opus`, …) —
they resolve server-side to the latest in the family. When the ACP session
advertises `availableModels`/`configOptions`, the shim selects via
`session/set_config_option` or `session/set_model`; an unlisted or disabled id
falls back to the session default with a warning.

## How it works

`plugin.yaml` declares `kind: model-provider`, so `hermes plugins install` /
`~/.hermes/plugins/` discovery routes it to the provider registry. The profile
uses `auth_type="external_process"` + `create_client()`, which returns a
self-contained ACP stdio client (`devin_acp_client.py`) instead of an HTTP
client — the same seam as the bundled `copilot-acp` provider.

Each `chat.completions.create()` call spawns `devin acp`, runs
`initialize → session/new → session/set_model → session/prompt`, collects
`session/update` chunks, and maps the result back to the OpenAI response shape.
Hermes tool schemas cross the bridge as `<tool_call>` text blocks
(`agent.acp_openai_bridge`); `session/request_permission` is denied and
`fs/*` requests are cwd-confined, so Devin's own tools can't run — Hermes keeps
executing tools itself.

## Terms-of-use notes

- Bring your own Devin subscription — this plugin never stores, proxies, or
  shares credentials. Each user authenticates their own CLI.
- Using SWE-2 output to build/train a competing model or product is prohibited
  by Cognition's Platform Terms (§2.3). Don't publish benchmarks of the service
  (enterprise MSA §2.3(v)).
- We deliberately use the *documented* `devin acp` interface — no reverse
  engineering of Devin's internals or private endpoints.
- Enterprise users: org-level model allowlists still apply inside Devin CLI.

## Repo layout

```
├── plugin.yaml          # kind: model-provider manifest
├── __init__.py          # directory-plugin entry: imports + runs register()
├── devin_provider.py    # DevinACPProfile + register() (also the pip entry point)
├── devin_acp_client.py  # ACP stdio client (OpenAI-shape shim)
├── pyproject.toml       # optional pip entry-point install
└── tests/               # offline unit tests (no devin binary needed)
```
