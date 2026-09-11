# Contributing

Issues and pull requests are welcome.

## Ground rules

- **Stay inside the plugin contract.** This repo works within Hermes'
  `ProviderProfile` / `create_client()` / `auth_type="external_process"` seam.
  If a change needs more from Hermes core, propose widening the generic plugin
  surface upstream. Never special case here against core internals beyond the
  documented hooks.
- **No credentials in the repo.** Ever. Keys stay in user env or `devin auth
  login` storage.
- **Match upstream style.** `devin_acp_client.py` intentionally mirrors
  `agent/copilot_acp_client.py` in hermes-agent. Broad exception guards at the
  plugin boundary are on purpose (a plugin must never take the agent down).
- **Offline tests.** `tests/` must keep passing without a `devin` binary or
  network:

  ```bash
  HERMES_REPO=/path/to/hermes-agent \
    uv run --with openai --with pytest --with pyyaml --with httpx \
    python -m pytest tests/ -q
  ```
- **Lint:** `ruff check .` should stay clean (see `[tool.ruff]` in
  `pyproject.toml`).
