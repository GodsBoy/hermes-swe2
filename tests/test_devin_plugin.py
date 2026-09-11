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


def _load_provider_module():
    _load_plugin_module()
    return sys.modules["_test_devin_provider_plugin.devin_provider"]


def test_register_adds_devin_profile():
    _load_plugin_module()
    from providers import get_provider_profile

    profile = get_provider_profile("devin")
    assert profile is not None
    assert profile.name == "devin"
    assert profile.auth_type == "external_process"
    assert profile.process_command == "devin"
    assert profile.process_args == ("acp",)
    assert "swe-2-max" in profile.fallback_models
    assert all(" " not in model for model in profile.fallback_models)
    for alias in ("devin-cli", "swe-2", "cognition"):
        assert get_provider_profile(alias) is profile


def test_register_surfaces_provider_in_model_picker(monkeypatch, tmp_path):
    _load_plugin_module()
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS
    from hermes_cli.providers import _LABEL_OVERRIDES, HERMES_OVERLAYS, get_label

    assert HERMES_OVERLAYS["devin"].auth_type == "external_process"
    assert _LABEL_OVERRIDES["devin"] == "Devin CLI (SWE-2)"
    assert get_label("devin") == "Devin CLI (SWE-2)"
    assert _PROVIDER_MODELS["devin"][0] == "swe-2-max"

    pytest.importorskip("requests", reason="hermes model picker needs requests")
    # The picker row appears once the CLI resolves on PATH, exactly like copilot-acp.
    fake_cli = tmp_path / "devin"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "models" ]; then\n'
        """  printf '%s\\n' '{"models":[{"id":"claude-opus-4-7"},{"id":"swe-2"}]}'\n"""
        "else\n"
        "  echo acp\n"
        "fi\n"
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from hermes_cli.model_switch_providers import list_picker_providers

    rows = {row["slug"]: row for row in list_picker_providers()}
    assert rows["devin"]["name"] == "Devin CLI (SWE-2)"
    assert "swe-2" in rows["devin"]["models"]
    assert "claude-opus-4-7" in rows["devin"]["models"]


def test_fetch_models_returns_none():
    _load_plugin_module()
    from providers import get_provider_profile

    assert get_provider_profile("devin").fetch_models() is None


def test_extract_model_ids_handles_flat_and_family_shapes():
    provider_mod = _load_provider_module()
    payload = {
        "flat": ["swe-2", " opus ", "swe-2"],
        "models": [
            {"id": "claude-opus-4-7", "aliases": ["opus", "claude-opus-4-7"]},
            {"model": "gpt-5", "shortName": "gpt"},
        ],
        "families": [{
            "name": "Claude",
            "models": [{"id": "sonnet", "short_name": ["sonnet", "claude-sonnet-4"]}],
        }, {
            "family_label": "SWE-2",
            "slug": "swe-2",
            "aliases": ["swe"],
            "variants": [{"model_uid": "swe-2-max", "label": "SWE-2 Max"}],
        }],
    }
    model_ids = provider_mod._extract_model_ids(payload)
    assert model_ids == [
        "swe-2", "opus", "claude-opus-4-7", "gpt-5", "gpt", "sonnet", "claude-sonnet-4",
        "swe", "swe-2-max",
    ]
    assert "Claude" not in model_ids
    assert "SWE-2 Max" not in model_ids


def test_fetch_cli_models_merges_live_after_curated(monkeypatch, tmp_path):
    provider_mod = _load_provider_module()
    fake_cli = tmp_path / "devin"
    fake_cli.write_text(
        """#!/bin/sh
printf '%s\n' '{"models":[{"id":"claude-opus-4-7"},{"id":"swe-2"}]}'
"""
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_DEVIN_ACP_COMMAND", str(fake_cli))
    models = provider_mod.fetch_cli_models("devin", False)
    assert models[:len(provider_mod.FALLBACK_MODELS)] == list(provider_mod.FALLBACK_MODELS)
    assert models[-2:] == ["claude-opus-4-7", "swe-2"]
    assert models.count("swe-2") == 1


def test_fetch_cli_models_returns_none_on_failure(monkeypatch, tmp_path):
    provider_mod = _load_provider_module()
    failed_cli = tmp_path / "failed-devin"
    failed_cli.write_text("#!/bin/sh\nexit 1\n")
    failed_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_DEVIN_ACP_COMMAND", str(failed_cli))
    assert provider_mod.fetch_cli_models("devin", False) is None

    garbage_cli = tmp_path / "garbage-devin"
    garbage_cli.write_text("#!/bin/sh\necho not-json\n")
    garbage_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_DEVIN_ACP_COMMAND", str(garbage_cli))
    assert provider_mod.fetch_cli_models("devin", False) is None


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
            "params": {"path": "/outside/project/secret.txt"},
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


def test_prompt_trims_oldest_transcript_turns_to_budget(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.setenv("HERMES_DEVIN_PROMPT_TOKENS", "1200")
    messages = [{"role": "user", "content": f"turn {i} " + "x" * 400} for i in range(20)]
    messages.append({"role": "user", "content": "latest question"})
    prompt = client_mod._format_messages_as_prompt(messages, model="swe-2-max")
    assert "latest question" in prompt
    assert "earlier transcript message(s) omitted" in prompt
    assert "turn 0 " not in prompt
    assert len(prompt) < 1200 * client_mod._CHARS_PER_TOKEN + 2000


def test_prompt_truncates_single_oversized_turn(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.setenv("HERMES_DEVIN_PROMPT_TOKENS", "1000")
    prompt = client_mod._format_messages_as_prompt(
        [{"role": "user", "content": "A" * 40000 + "TAIL-KEEPER"}],
        model="swe-2-max",
    )
    assert "TAIL-KEEPER" in prompt
    assert "truncated to fit" in prompt


def test_prompt_token_budget_env_override(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.delenv("HERMES_DEVIN_PROMPT_TOKENS", raising=False)
    assert client_mod._prompt_token_budget() == client_mod._DEFAULT_PROMPT_TOKEN_BUDGET
    monkeypatch.setenv("HERMES_DEVIN_PROMPT_TOKENS", "80000")
    assert client_mod._prompt_token_budget() == 80000
    monkeypatch.setenv("HERMES_DEVIN_PROMPT_TOKENS", "junk")
    assert client_mod._prompt_token_budget() == client_mod._DEFAULT_PROMPT_TOKEN_BUDGET


def test_profile_forwards_reasoning_config_to_client():
    provider_mod = _load_provider_module()
    profile = provider_mod.DevinACPProfile(name="devin")
    extra_body, top_level = profile.build_api_kwargs_extras(reasoning_config={"effort": "high"})
    assert extra_body == {}
    assert top_level == {"reasoning_config": {"effort": "high"}}
    assert profile.build_api_kwargs_extras(reasoning_config=None) == ({}, {})
    assert profile.build_api_kwargs_extras() == ({}, {})


def test_effort_variant_maps_reasoning_onto_devin_tiers():
    client_mod = _load_client_module()
    advertised = {
        "swe-2-medium", "swe-2-high", "swe-2-max",
        "swe-1-7", "swe-1-7-medium",
        "gpt-5.6-sol-none", "gpt-5.6-sol-low", "gpt-5.6-sol-max",
        "gpt-6-astra-low", "gpt-6-astra-max",
        "claude-opus-5-low", "claude-opus-5-max", "claude-opus-5-max-fast",
    }
    # exact tier
    assert client_mod._effort_variant("swe-2", "high", advertised) == "swe-2-high"
    assert client_mod._effort_variant("swe-2-max", "medium", advertised) == "swe-2-medium"
    # aliases resolve to their family
    assert client_mod._effort_variant("swe", "high", advertised) == "swe-2-high"
    assert client_mod._effort_variant("opus", "low", advertised) == "claude-opus-5-low"
    # never escalates: xhigh on a medium/high/max family lands on high
    assert client_mod._effort_variant("swe-2", "xhigh", advertised) == "swe-2-high"
    assert client_mod._effort_variant("swe-2", "max", advertised) == "swe-2-max"
    # below the family's floor picks the lowest advertised tier
    assert client_mod._effort_variant("swe-2", "none", advertised) == "swe-2-medium"
    # bare family id counts as the top tier
    assert client_mod._effort_variant("swe-1-7", "max", advertised) == "swe-1-7"
    assert client_mod._effort_variant("swe-1-7", "medium", advertised) == "swe-1-7-medium"
    # real "-none" tier where the family has one
    assert client_mod._effort_variant("gpt-5.6-sol", "none", advertised) == "gpt-5.6-sol-none"
    # alias maps to its catalog family (gpt -> gpt-6-astra)
    assert client_mod._effort_variant("gpt", "low", advertised) == "gpt-6-astra-low"
    # unmappable input stays out of the way
    assert client_mod._effort_variant("swe-2", "bogus", advertised) is None
    assert client_mod._effort_variant("unknown-model", "high", advertised) is None
    assert client_mod._effort_variant("swe-2", "high", set()) is None


def test_family_base_strips_effort_and_decorator_suffixes():
    client_mod = _load_client_module()
    assert client_mod._family_base("swe-2-max") == "swe-2"
    assert client_mod._family_base("swe-2") == "swe-2"
    assert client_mod._family_base("swe-1-7") == "swe-1-7"
    assert client_mod._family_base("swe-1-7-lightning") == "swe-1-7-lightning"
    assert client_mod._family_base("gpt-5.6-sol-low-priority") == "gpt-5.6-sol"
    assert client_mod._family_base("claude-opus-5-max-fast") == "claude-opus-5"


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
        client_mod._ensure_path_within_cwd("/outside/project/secret.txt", "/home/user/project")
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
    try:
        response = client.chat.completions.create(
            model="swe-2-max", messages=[{"role": "user", "content": "hi"}],
        )
        assert response.choices[0].message.content == "Hello from fake ACP (swe-2-max)."
        assert response.choices[0].message.reasoning == "Thinking about the request."
        assert response.choices[0].finish_reason == "stop"

        stream = client.chat.completions.create(
            model="swe-2-max", messages=[{"role": "user", "content": "hi"},
                                         {"role": "assistant", "content": "hello"}, {"role": "user", "content": "again"}],
            stream=True,
        )
        assert stream
        assert stream[0].choices[0].delta.content == "Hello from fake ACP (swe-2-max)."
        assert stream[0].choices[0].finish_reason == "stop"
    finally:
        client.close()


def test_kept_alive_session_sends_only_new_turns(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.delenv("DEVIN_MODEL", raising=False)
    fake_server = Path(__file__).with_name("fake_acp_server.py")
    client = client_mod.DevinACPClient(command=sys.executable, args=[str(fake_server)])
    try:
        first = client.chat.completions.create(
            model="swe-2-max", messages=[{"role": "user", "content": "hi"}],
        )
        session_id = client._session_id
        proc = client._live_process()
        assert session_id and proc is not None

        # Same session and process serve the follow-up; only the new turns go out.
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": first.choices[0].message.content},
            {"role": "user", "content": "again"},
        ]
        second = client.chat.completions.create(model="swe-2-max", messages=history)
        assert second.choices[0].message.content.startswith("Hello from fake ACP")
        assert client._session_id == session_id
        assert client._live_process() is proc
        assert len(client._sent_hashes) == 3

        # History rewritten by compression (prefix changed) -> fresh session + replay.
        diverged = [{"role": "user", "content": "summary of earlier chat"}]
        third = client.chat.completions.create(model="swe-2-max", messages=diverged)
        assert third.choices[0].message.content.startswith("Hello from fake ACP")
        assert client._session_id != session_id or client._live_process() is not proc
        assert len(client._sent_hashes) == 1
    finally:
        client.close()


def test_fake_acp_server_reasoning_effort_remaps_variant(monkeypatch):
    client_mod = _load_client_module()
    monkeypatch.delenv("DEVIN_MODEL", raising=False)
    fake_server = Path(__file__).with_name("fake_acp_server.py")
    client = client_mod.DevinACPClient(command=sys.executable, args=[str(fake_server)])
    try:
        response = client.chat.completions.create(
            model="swe-2",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_config={"effort": "high"},
        )
        assert response.choices[0].message.content == "Hello from fake ACP (swe-2-high)."

        off = client.chat.completions.create(
            model="swe-2",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_config={"enabled": False},
        )
        assert off.choices[0].message.content == "Hello from fake ACP (swe-2-medium)."

        # An explicitly picked advertised variant already encodes its effort tier:
        # reasoning_config must not remap it.
        explicit = client.chat.completions.create(
            model="swe-2-max",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_config={"effort": "low"},
        )
        assert explicit.choices[0].message.content == "Hello from fake ACP (swe-2-max)."
    finally:
        client.close()
