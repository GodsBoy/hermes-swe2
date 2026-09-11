"""Devin CLI (SWE-2) provider profile for Hermes Agent.

`devin` does not speak OpenAI-over-HTTP: it drives an external ACP subprocess
over stdio (`devin acp`), so the profile supplies its own client via
:meth:`ProviderProfile.create_client`, the same seam the bundled
``copilot-acp`` provider uses.
"""

import json
import logging
import subprocess
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

PROVIDER_NAME = "devin"
DISPLAY_NAME = "Devin CLI (SWE-2)"
BASE_URL = "acp://devin"
# ACP `session/set_config_option` only accepts exact variant ids, so the fallback
# list carries the real `model_uid` values, not family slugs or aliases. Family
# names still work when typed manually: `DEVIN_MODEL` resolves them server-side.
FALLBACK_MODELS = (
    "swe-2-max",
    "swe-2-high",
    "swe-2-medium",
    "swe-1-7",
    "swe-1-7-medium",
    "swe-1-7-lightning",
    "swe-1-7-lightning-medium",
    "swe-1-6",
    "swe-1-6-fast",
)


class DevinACPProfile(ProviderProfile):
    """Devin CLI ACP, external process, no REST models endpoint."""

    def create_client(self, **client_kwargs: Any) -> Any:
        """Build the ACP stdio shim rather than an HTTP client."""
        try:
            from .devin_acp_client import DevinACPClient
        except ImportError:
            # pip-installed / flat top-level module path
            from devin_acp_client import DevinACPClient  # type: ignore[no-redef]

        return DevinACPClient(**client_kwargs)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """Model listing is owned by the `devin acp` session (availableModels)."""
        return None

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Forward Hermes' ``/reasoning`` config to the ACP client, which maps the
        effort level onto Devin's variant-encoded tiers (``swe-2`` + ``high`` ->
        ``swe-2-high``)."""
        extras = {"reasoning_config": reasoning_config} if isinstance(reasoning_config, dict) else {}
        return {}, extras


def _extract_model_ids(payload: object) -> list[str]:
    """Extract model ids from flat and family-grouped CLI catalog responses."""
    model_ids: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        model_id = value.strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            model_ids.append(model_id)

    def walk(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add(item)
                else:
                    walk(item)
            return
        if not isinstance(value, dict):
            return
        has_nested_list = any(isinstance(item, list) for item in value.values())
        for key in ("id", "model_uid", "slug", "model", "name"):
            item = value.get(key)
            if isinstance(item, str) and (key != "name" or not has_nested_list):
                add(item)
                break
        for key in ("aliases", "shortName", "short_name"):
            item = value.get(key)
            if isinstance(item, str):
                add(item)
            elif isinstance(item, list):
                for alias in item:
                    if isinstance(alias, str):
                        add(alias)
        for item in value.values():
            if isinstance(item, (dict, list)):
                walk(item)

    walk(payload)
    return model_ids


def fetch_cli_models(normalized: str, force_refresh: bool) -> list[str] | None:
    """Fetch and merge the account model catalog exposed by the Devin CLI."""
    try:
        try:
            from .devin_acp_client import _build_subprocess_env, _resolve_command
        except ImportError:
            from devin_acp_client import (  # type: ignore[no-redef]
                _build_subprocess_env,
                _resolve_command,
            )

        result = subprocess.run(
            [_resolve_command(), "models", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_build_subprocess_env(None, []),
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            logger.debug("Devin CLI model catalog exited with status %s", result.returncode)
            return None
        live_models = _extract_model_ids(json.loads(result.stdout))
        if not live_models:
            logger.debug("Devin CLI model catalog returned no model ids")
            return None
        merged = list(FALLBACK_MODELS)
        merged.extend(model_id for model_id in live_models if model_id not in merged)
        return merged
    except Exception as exc:
        logger.debug("Devin CLI model catalog fetch failed: %s", exc)
        return None


def register_picker_entries() -> None:
    """Surface the provider in ``/model`` and ``hermes model``.

    Hermes builds its picker from ``HERMES_OVERLAYS`` (credential check through
    ``get_auth_status``, which for external process providers means the CLI
    resolves on PATH), takes model names from the static catalog, and supports
    a live catalog fetcher. External process plugin profiles are otherwise
    skipped by the picker on purpose. Best effort: any Hermes internals
    mismatch only disables the menu entry, the provider itself still resolves
    by name.
    """
    try:
        from hermes_cli.models import _PROVIDER_CATALOG_FETCHERS
        from hermes_cli.models_catalog_static import _PROVIDER_MODELS
        from hermes_cli.providers import (
            _LABEL_OVERRIDES,
            HERMES_OVERLAYS,
            HermesOverlay,
        )
    except ImportError as exc:
        logger.debug("devin picker registration skipped: %s", exc)
        return
    HERMES_OVERLAYS.setdefault(
        PROVIDER_NAME,
        HermesOverlay(auth_type="external_process", base_url_override=BASE_URL),
    )
    _LABEL_OVERRIDES.setdefault(PROVIDER_NAME, DISPLAY_NAME)
    _PROVIDER_MODELS.setdefault(PROVIDER_NAME, list(FALLBACK_MODELS))
    _PROVIDER_CATALOG_FETCHERS.setdefault(PROVIDER_NAME, fetch_cli_models)


def register() -> None:
    """Zero-arg registration hook, used by both directory discovery (module
    import side effect via this plugin's ``__init__.py``) and the
    ``hermes_agent.plugins`` pip entry point."""
    register_provider(
        DevinACPProfile(
            name=PROVIDER_NAME,
            aliases=("devin-cli", "devin-acp", "swe2", "swe-2", "cognition"),
            api_mode="chat_completions",  # ACP subprocess uses chat_completions routing
            display_name=DISPLAY_NAME,
            description="SWE-2 through the Devin CLI ACP server, using your own CLI login",
            signup_url="https://docs.devin.ai/cli",
            env_vars=(),  # Auth is owned by `devin auth login` / WINDSURF_API_KEY, not Hermes
            base_url=BASE_URL,  # ACP scheme, lets URL-based profile lookup find us too
            auth_type="external_process",
            # How to launch the CLI; env vars let an operator point at a custom build.
            process_command="devin",
            process_args=("acp",),
            process_command_env_vars=("HERMES_DEVIN_ACP_COMMAND", "DEVIN_CLI_PATH"),
            process_args_env_var="HERMES_DEVIN_ACP_ARGS",
            # Shown in `hermes model` when the ACP session does not advertise a catalog.
            # Short names resolve server-side to the latest in the family.
            fallback_models=FALLBACK_MODELS,
        )
    )
    register_picker_entries()
