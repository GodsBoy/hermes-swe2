<div align="center">

<img src="assets/banner.png" alt="hermes-swe2: SWE-2 inside Hermes Agent through Devin CLI ACP" width="100%">

# ⚡ hermes-swe2

**Run Cognition's SWE-2 as a first class model inside [Hermes Agent](https://github.com/NousResearch/hermes-agent), powered by your own [Devin CLI](https://docs.devin.ai/cli).**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?style=for-the-badge&logo=python&logoColor=white)](pyproject.toml)
[![Plugin kind](https://img.shields.io/badge/Hermes%20plugin-model--provider-7c3aed?style=for-the-badge)](plugin.yaml)
[![Backend](https://img.shields.io/badge/backend-devin%20acp-00b4d8?style=for-the-badge)](https://docs.devin.ai/cli/reference/commands#devin-acp)
[![Protocol](https://img.shields.io/badge/wire-ACP%20%2F%20JSON--RPC-f72585?style=for-the-badge)](https://agentclientprotocol.com)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=for-the-badge)](CONTRIBUTING.md)

</div>

---

## 🧠 What is this?

Cognition's **SWE-2** is one of the strongest software engineering models available, but it has **no public inference API**. Cognition provides `devin acp`, a documented **Agent Client Protocol** server that lets third party editors such as Zed and Windsurf drive the CLI as a subprocess.

**hermes-swe2 turns that editor surface into a Hermes model provider.** Hermes talks ACP to your locally installed `devin` binary, the same protocol used by Zed, and SWE-2 becomes a selectable model in `hermes model`, `hermes --provider`, the TUI, and the gateway. It uses the same extension seam as the bundled `copilot-acp` provider and requires no core edits.

> [!IMPORTANT]
> **Bring your own subscription.** The plugin never stores, proxies, or shares credentials. Run `devin auth login`, or set `WINDSURF_API_KEY` or `DEVIN_API_KEY`, on your machine.

## ✨ Features

* 🔌 **Drop in provider:** registers as `devin`; `--provider devin`, `hermes model`, `hermes setup`, and `hermes doctor` pick it up automatically
* 🧬 **SWE family picker:** `swe-2`, `swe`, `swe-1-7`, `swe-1-7-lightning`, and `swe-1-6-fast` are available as fallbacks. Live `availableModels` and `configOptions` are negotiated over ACP when offered
* 🛠️ **Full tool calling:** Hermes tool schemas cross the bridge as `<tool_call>` blocks, while Hermes continues to execute the tools
* 🌊 **Streaming:** `session/update` chunks map back to OpenAI stream chunks
* 🔒 **Fail closed:** permission prompts are denied, and `fs/*` requests are confined to the session working directory
* ⚙️ **Zero configuration overrides:** customize the binary and arguments through environment variables
* 📦 **Three installation paths:** `hermes plugins install`, manual drop in, or pip entry point

## 🏗️ How it works

```mermaid
sequenceDiagram
    participant H as 🪽 Hermes Agent
    participant P as hermes-swe2 shim
    participant D as Devin ACP
    participant C as ☁️ Cognition (SWE-2)

    H->>P: chat.completions.create(model="swe-2")
    P->>D: spawn `devin acp` (stdio JSON-RPC)
    P->>D: initialize (clientCapabilities: fs read/write)
    alt credential in env
        P->>D: authenticate (methodId from authMethods)
    end
    P->>D: session/new (cwd, mcpServers: [])
    P->>D: session/set_model | set_config_option → swe-2
    P->>D: session/prompt (transcript + tool schemas as text)
    D->>C: inference
    loop session/update
        D-->>P: agent_message_chunk / agent_thought_chunk
    end
    D-->>P: session/prompt result
    P-->>H: OpenAI-shaped completion (content + <tool_call>s + stream chunks)
```

Hermes' tool loop is preserved. The CLI's own tools are never invoked because permission requests are cancelled and the file system bridge is confined to the working directory. `<tool_call>` blocks flow back to Hermes, which executes them and continues the loop. Hermes remains the agent, while SWE-2 provides the model intelligence.

## 📦 Install

<details open>
<summary><b>1️⃣ <code>hermes plugins install</code> (recommended)</b></summary>

```bash
hermes plugins install https://github.com/GodsBoy/hermes-swe2
# lands in $HERMES_HOME/plugins/hermes-swe2/
# the model provider manifest routes it to the provider registry automatically
```

</details>

<details>
<summary><b>2️⃣ Manual drop-in</b></summary>

```bash
mkdir -p ~/.hermes/plugins/model-providers/devin
cp __init__.py plugin.yaml devin_provider.py devin_acp_client.py \
   ~/.hermes/plugins/model-providers/devin/
```

</details>

<details>
<summary><b>3️⃣ pip entry point</b></summary>

```bash
pip install git+https://github.com/GodsBoy/hermes-swe2.git
# then add "devin" to plugins.enabled in ~/.hermes/config.yaml
```

</details>

### Prerequisites

```bash
# Devin CLI
curl -fsSL https://cli.devin.ai/install.sh | bash

# authenticate once (or export WINDSURF_API_KEY / DEVIN_API_KEY)
devin auth login
```

### Select the model

```bash
hermes model                      # → "Devin CLI (SWE-2)" → swe-2
hermes --provider devin --model swe-2
hermes doctor                     # confirms the `devin` binary resolves
```

## ⚙️ Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `HERMES_DEVIN_ACP_COMMAND` | `devin` | Override the CLI binary path |
| `DEVIN_CLI_PATH` | Not set | Alternative binary path override |
| `HERMES_DEVIN_ACP_ARGS` | `acp` | Override arguments with shell-style splitting, for example `acp --model swe-2` |
| `DEVIN_API_KEY` / `WINDSURF_API_KEY` | Not set | Credential handed to the child and offered through ACP `authenticate` |

Model ids are Devin CLI short names such as `swe-2`, `swe`, `opus`, `sonnet`, `codex`, and `gemini`. They resolve on the server to the latest family member. Unlisted or policy disabled ids fall back to the session default with a warning in `agent.log`.

## 🔍 Troubleshooting

<details>
<summary><b>"Could not find the 'devin' CLI command"</b></summary>

The `devin` command is not on `PATH` for the Hermes process. Install it, or point at it:

```bash
export HERMES_DEVIN_ACP_COMMAND=/full/path/to/devin
```

</details>

<details>
<summary><b>Model silently stays on the session default</b></summary>

The ACP session advertised a model list that did not include your selected model. For example, an enterprise allowlist may filter SWE-2 out. Check `~/.hermes/logs/agent.log` for the `does not offer model` warning.

</details>

<details>
<summary><b>Auth errors / devin asks to log in</b></summary>

Run `devin auth login` in a terminal first, or export `WINDSURF_API_KEY`. On a remote or SSH machine, run `devin setup --force-manual-token-flow`.

</details>

## ⚖️ Terms-of-use notes

This plugin deliberately uses the **documented** `devin acp` interface, the same interface Cognition built for third party editors. There is no reverse engineering, no use of private endpoints, and no credential extraction.

* ✅ Each user authenticates their own CLI. Credentials are not shared
* ❌ Do not use SWE-2 output to build or train a competing model or product. See Cognition Platform Terms §2.3
* ❌ Do not publicly publish service benchmarks. See Enterprise MSA §2.3(v)
* ℹ️ Enterprise model allowlists and team settings still apply inside the CLI

## 🧪 Tests

```bash
HERMES_REPO=/path/to/hermes-agent \
  uv run --with openai --with pytest --with pyyaml --with httpx \
  python -m pytest tests/ -q
```

The tests run fully offline. They cover registration, the prompt and tool bridge, ACP model selection logic, and working directory confinement. No `devin` binary is required.

## 🗂️ Repo layout

```
├── plugin.yaml          # kind: model-provider manifest
├── __init__.py          # directory plugin entry that calls devin_provider.register()
├── devin_provider.py    # DevinACPProfile + register() (pip entry point target)
├── devin_acp_client.py  # self-contained ACP stdio client (OpenAI-shape shim)
├── pyproject.toml       # optional pip install
├── tests/               # offline unit tests
└── assets/              # banner & friends
```

## 👤 Credits

Built by **[GodsBoy](https://github.com/GodsBoy)**. If you use or adapt this plugin,
attribution is appreciated. A link back to this repository is enough.

## 🤝 Contributing

Issues and pull requests are welcome. Hermes conventions live in
[`plugins/AGENTS.md`](https://github.com/NousResearch/hermes-agent/blob/main/plugins/AGENTS.md).
This is intentionally an **out of tree** plugin, following the project's policy for vendor integrations.

---

<div align="center">

Built for 🪽 [Hermes Agent](https://hermes-agent.nousresearch.com) · Powered by ⚡ [SWE-2](https://cognition.com/blog/swe-2) · MIT Licensed · © 2026 GodsBoy

</div>
