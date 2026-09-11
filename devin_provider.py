"""Devin CLI (SWE-2) provider profile for Hermes Agent.

`devin` does not speak OpenAI-over-HTTP: it drives an external ACP subprocess
over stdio (`devin acp`), so the profile supplies its own client via
:meth:`ProviderProfile.create_client`, the same seam the bundled
``copilot-acp`` provider uses.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


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


def register() -> None:
    """Zero-arg registration hook, used by both directory discovery (module
    import side effect via this plugin's ``__init__.py``) and the
    ``hermes_agent.plugins`` pip entry point."""
    register_provider(
        DevinACPProfile(
            name="devin",
            aliases=("devin-cli", "devin-acp", "swe2", "swe-2", "cognition"),
            api_mode="chat_completions",  # ACP subprocess uses chat_completions routing
            display_name="Devin CLI (SWE-2)",
            description="SWE-2 through the Devin CLI ACP server, using your own CLI login",
            signup_url="https://docs.devin.ai/cli",
            env_vars=(),  # Auth is owned by `devin auth login` / WINDSURF_API_KEY, not Hermes
            base_url="acp://devin",  # ACP scheme, lets URL-based profile lookup find us too
            auth_type="external_process",
            # How to launch the CLI; env vars let an operator point at a custom build.
            process_command="devin",
            process_args=("acp",),
            process_command_env_vars=("HERMES_DEVIN_ACP_COMMAND", "DEVIN_CLI_PATH"),
            process_args_env_var="HERMES_DEVIN_ACP_ARGS",
            # Shown in `hermes model` when the ACP session does not advertise a catalog.
            # Short names resolve server-side to the latest in the family.
            fallback_models=(
                "swe-2",
                "swe",
                "swe-1-7",
                "swe-1-7-lightning",
                "swe-1-6-fast",
            ),
        )
    )
