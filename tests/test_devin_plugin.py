"""Offline unit tests for the Devin ACP model-provider plugin.

These exercise the plugin's pure logic — no `devin` binary, no network. They
need a hermes-agent checkout importable (the client module imports
``agent.*``/``tools.*`` helpers). Point at one with HERMES_REPO, or run from a
checkout that already has hermes-agent on sys.path.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_REPO = Path(os.environ.get("HERMES_REPO", "/home/ubuntu/repos/hermes-agent"))

if str(HERMES_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_REPO))

pytest.importorskip("providers", reason="hermes-agent repo not importable")


def _load_plugin_module():
    """Load the plugin's __init__.py the way providers._import_plugin_dir does."""
    spec = importlib.util.spec_from_file_location(
        "_test_devin_provider_plugin",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_client_module():
    spec = importlib.util.spec_from_file_location(
        "_test_devin_acp_client",
        PLUGIN_DIR / "devin_acp_client.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_register_adds_devin_profile():
    _load_plugin_module()
    from providers import get_provider_profile

    profile = get_provider_profile("devin")
    assert profile is not None
    assert profile.name == "devin"
    assert profile.auth_type == "external_process"
    assert profile.process_command == "devin"
    assert profile.process_args == ("acp",)
    assert "swe-2" in profile.fallback_models
    for alias in ("devin-cli", "swe-2", "cognition"):
        assert get_provider_profile(alias) is profile


def test_fetch_models_returns_none():
    _load_plugin_module()
    from providers import get_provider_profile

    assert get_provider_profile("devin").fetch_models() is None


def test_prompt_includes_transcript_and_tool_bridge():
    client_mod = _load_client_module()
    prompt = client_mod._format_messages_as_prompt(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Rename main() to entrypoint()"},
        ],
        model="swe-2",
        tools=[{
            "type": "function",
            "function": {"name": "edit_file", "parameters": {"type": "object"}},
        }],
    )
    assert "tool_call" in prompt  # tool bridge contract present
    assert "Rename main() to entrypoint()" in prompt
    assert "System:" in prompt and "User:" in prompt


def test_model_selection_prefers_config_options():
    client_mod = _load_client_module()
    session = {
        "sessionId": "s1",
        "configOptions": [{
            "id": "model", "category": "model",
            "options": [{"value": "swe-2"}, {"value": "opus", "_meta": {"x": "disabled"}}],
        }],
    }
    method, params = client_mod._model_selection_request(session, "swe-2")
    assert method == "session/set_config_option"
    assert params["value"] == "swe-2"
    # disabled entries are treated as unavailable
    assert client_mod._model_selection_request(session, "opus") is None


def test_model_selection_falls_back_to_set_model():
    client_mod = _load_client_module()
    session = {"sessionId": "s1", "models": {"availableModels": [{"modelId": "swe-2"}]}}
    method, params = client_mod._model_selection_request(session, "swe-2")
    assert method == "session/set_model"
    assert params["modelId"] == "swe-2"
    # unadvertised model -> None (use session default)
    assert client_mod._model_selection_request(session, "gpt-5") is None


def test_cwd_confinement():
    client_mod = _load_client_module()
    with pytest.raises(PermissionError):
        client_mod._ensure_path_within_cwd("/etc/passwd", "/home/user/project")
    ok = client_mod._ensure_path_within_cwd("/home/user/project/a.py", "/home/user/project")
    assert ok.name == "a.py"


def test_client_shape():
    client_mod = _load_client_module()
    client = client_mod.DevinACPClient(command="echo", args=["acp"])
    assert client.HERMES_SKIP_TRANSPORT_WRAP is True
    assert callable(client.chat.completions.create)
    client.close()
    assert client.is_closed is True
