"""Offline unit tests for the Devin ACP model-provider plugin.

These exercise the plugin's pure logic, no `devin` binary, no network. They
need a hermes-agent checkout importable (the client module imports
``agent.*``/``tools.*`` helpers). Point at one with HERMES_REPO, or run from a
checkout that already has hermes-agent on sys.path.
"""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_authenticate_request_prefers_key_or_token_method():
    client_mod = _load_client_module()
    init_result = {
        "authMethods": [
            {"id": "oauth", "name": "OAuth"},
            {"id": "api-key", "name": "API key"},
        ]
    }
    assert client_mod._authenticate_request(init_result, "secret") == {"methodId": "api-key"}
    assert client_mod._authenticate_request({"authMethods": []}, "secret") is None
    assert client_mod._authenticate_request(init_result, "") is None


def test_handle_server_message_answers_requests_and_collects_updates(tmp_path):
    client_mod = _load_client_module()
    client = client_mod.DevinACPClient(command="echo", args=["acp"], acp_cwd=str(tmp_path))
    process = SimpleNamespace(stdin=io.StringIO())
    text_parts, reasoning_parts = [], []

    assert client._handle_server_message(
        {"jsonrpc": "2.0", "id": 1, "method": "session/request_permission", "params": {}},
        process=process, cwd=str(tmp_path), text_parts=text_parts, reasoning_parts=reasoning_parts,
    )
    permission = json.loads(process.stdin.getvalue())
    assert permission["result"]["outcome"]["outcome"] == "cancelled"

    process.stdin = io.StringIO()
    assert client._handle_server_message(
        {
            "jsonrpc": "2.0", "id": 2, "method": "fs/read_text_file",
            "params": {"path": "/etc/passwd"},
        },
        process=process, cwd=str(tmp_path), text_parts=text_parts, reasoning_parts=reasoning_parts,
    )
    outside_error = json.loads(process.stdin.getvalue())
    assert outside_error["error"]["code"] == -32602

    process.stdin = io.StringIO()
    assert client._handle_server_message(
        {"jsonrpc": "2.0", "id": 3, "method": "unknown", "params": {}},
        process=process, cwd=str(tmp_path), text_parts=text_parts, reasoning_parts=reasoning_parts,
    )
    unknown_error = json.loads(process.stdin.getvalue())
    assert unknown_error["error"]["code"] == -32601

    assert client._handle_server_message(
        {
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_message_chunk", "content": {"text": "hello"},
            }},
        },
        process=process, cwd=str(tmp_path), text_parts=text_parts, reasoning_parts=reasoning_parts,
    )
    assert text_parts == ["hello"]
    client.close()


def test_build_subprocess_env_applies_model_and_native_credential(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.setenv("DEVIN_API_KEY", "devin-secret")
    monkeypatch.delenv("WINDSURF_API_KEY", raising=False)
    client = client_mod.DevinACPClient(command="echo", args=["acp"])
    env = client._build_subprocess_env("swe-2")
    assert env["DEVIN_MODEL"] == "swe-2"
    assert env["WINDSURF_API_KEY"] == "devin-secret"

    overridden = client_mod.DevinACPClient(command="echo", args=["acp", "--model", "opus"])
    assert "DEVIN_MODEL" not in overridden._build_subprocess_env("swe-2")
    client.close()
    overridden.close()


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
    assert client_mod._model_selection_request({"sessionId": "s1"}, "swe-2") is None


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


def test_fake_acp_server_end_to_end(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.delenv("DEVIN_MODEL", raising=False)
    fake_server = Path(__file__).with_name("fake_acp_server.py")
    client = client_mod.DevinACPClient(command=sys.executable, args=[str(fake_server)])
    response = client.chat.completions.create(
        model="swe-2", messages=[{"role": "user", "content": "hi"}],
    )
    assert response.choices[0].message.content == "Hello from fake ACP (swe-2)."
    assert response.choices[0].message.reasoning == "Thinking about the request."
    assert response.choices[0].finish_reason == "stop"

    stream = client.chat.completions.create(
        model="swe-2", messages=[{"role": "user", "content": "hi"}], stream=True,
    )
    assert stream
    assert stream[0].choices[0].delta.content == "Hello from fake ACP (swe-2)."
    assert stream[0].choices[0].finish_reason == "stop"
