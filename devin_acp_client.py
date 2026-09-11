"""OpenAI-compatible shim that forwards Hermes requests to `devin acp`.

Each request starts a short-lived ACP session over stdio against the Cognition
Devin CLI, sends the formatted conversation as one prompt, collects text chunks,
and returns the minimal OpenAI-client shape Hermes' chat-completions transport
expects. Mirrors ``agent/copilot_acp_client.py`` (the in-tree copilot-acp shim),
adapted for the ``devin`` binary and its auth surface:

* Credentials are owned by Devin CLI itself (``devin auth login``) or by
  ``WINDSURF_API_KEY`` / ``DEVIN_API_KEY`` in the environment. When the server
  advertises ``authMethods`` and a key is available, the shim also answers with
  an ACP ``authenticate`` request after ``initialize``.
* Model selection goes through ACP ``session/set_config_option`` (stable v1) or
  ``session/set_model``, whichever the session advertises.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.acp_openai_bridge import (
    completion_to_stream_chunks as _completion_to_stream_chunks,
)
from agent.acp_openai_bridge import (
    extract_tool_calls_from_text as _extract_tool_calls_from_text,
)
from agent.acp_openai_bridge import (
    render_tool_bridge_sections as _render_tool_bridge_sections,
)
from agent.file_safety import (
    get_read_block_error,
    get_write_denied_error,
    is_write_approval_required,
)
from agent.redact import redact_sensitive_text
from tools.environments.local import hermes_subprocess_env

ACP_MARKER_BASE_URL = "acp://devin"
logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 900.0
# The whole Hermes transcript is replayed as one ACP prompt, and Devin counts it
# against the model's context window together with its own rules, skills and tool
# schemas (SWE-2 family: 262k tokens total). The transcript is trimmed to this
# budget, oldest turns first, leaving headroom for that server-side overhead.
_DEFAULT_PROMPT_TOKEN_BUDGET = 150_000
_CHARS_PER_TOKEN = 4
_OMISSION_NOTE = "[... {count} earlier transcript message(s) omitted to fit the model context window ...]"
_TRUNCATION_NOTE = "[... earlier content truncated to fit the model context window ...]\n"
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool", "context": "Context"}
# Probe verdicts per binary path (~50ms --help paid once per process). Only definitive
# True/False is cached, so a CLI installed mid-session is picked up.
_ACP_PROBE_CACHE: dict[str, bool] = {}
# Env vars (priority order) that carry a Devin/Windsurf credential we hand to the
# child and offer through ACP `authenticate` when the server asks for one.
_DEVIN_KEY_ENV_VARS = ("WINDSURF_API_KEY", "DEVIN_API_KEY")
_PROMPT_PREAMBLE = (
    "You are being used as the active ACP agent backend for Hermes.",
    "Use ACP capabilities to complete tasks.",
    (
        "Do not use your own built-in tools; permission requests are denied in this "
        "bridge. If a tool is needed, you MUST output it as a <tool_call>{...}</tool_call> "
        "block with JSON exactly in OpenAI function-call shape, and Hermes will execute it "
        "for you."
    ),
    "If no tool is needed, answer normally.",
)
_INITIALIZE_PARAMS = {
    "protocolVersion": 1,
    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
    "clientInfo": {"name": "hermes-agent", "title": "Hermes Agent", "version": "0.0.0"},
}
_INSTALL_ERROR = (
    "Hermes could not start the Devin CLI ACP server ('%s').\n\n"
    "Install Devin CLI by following https://docs.devin.ai/cli and verify with: devin --help\n\n"
    "Authenticate it with your own Devin account (`devin auth login`) or export "
    "WINDSURF_API_KEY / DEVIN_API_KEY.\n\n"
    "If `devin` already resolves but you still see this, point Hermes at it explicitly:\n"
    "  export HERMES_DEVIN_ACP_COMMAND=/path/to/devin\n"
    "  export HERMES_DEVIN_ACP_ARGS='acp'   # custom argv if needed\n\nOriginal error:\n"
)


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_DEVIN_ACP_COMMAND", "").strip()
        or os.getenv("DEVIN_CLI_PATH", "").strip()
        or "devin"
    )


def _resolve_args() -> list[str]:
    return shlex.split(os.getenv("HERMES_DEVIN_ACP_ARGS", "").strip()) or ["acp"]


def _resolve_devin_key() -> str:
    for var in _DEVIN_KEY_ENV_VARS:
        if val := os.getenv(var, "").strip():
            return val
    return ""


def _acp_supported(command: str, args: list[str]) -> bool | None:
    """Tri-state probe: True = help advertises an ``acp`` subcommand; False = help ran
    cleanly without it (caller fast-fails); None = inconclusive (binary missing / help
    failed → normal spawn error)."""
    if "acp" not in args:
        return True
    if (cached := _ACP_PROBE_CACHE.get(command)) is not None:
        return cached
    try:
        probe = subprocess.run(
            [command, "--help"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, stdin=subprocess.DEVNULL, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    verdict = _ACP_PROBE_CACHE[command] = bool(
        re.search(r"(?:^|[\s])acp(?:[\s\],]|$)", probe.stdout, re.MULTILINE))
    return verdict


def _resolve_home_dir() -> str:
    """Stable HOME for child ACP processes; /tmp as a last resort so the child never starts HOME-less."""
    if home := os.environ.get("HOME", "").strip():
        return home
    if (expanded := os.path.expanduser("~")) and expanded != "~":
        return expanded
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir.strip() or "/tmp"  # POSIX fallback inside try (pwd missing on Windows)
    except Exception:
        return "/tmp"


def _build_subprocess_env(requested_model: str | None = None, args: list[str] | None = None) -> dict[str, str]:
    from hermes_constants import apply_subprocess_home_env

    # The ACP child drives a model and needs the user's Devin credential; the central
    # helper strips Tier-1 secrets (bot tokens, GitHub auth, infra). Re-assert the
    # Devin vars explicitly in case a future tier list sweeps them up.
    env = hermes_subprocess_env(inherit_credentials=True)
    for var in _DEVIN_KEY_ENV_VARS:
        if val := os.getenv(var, "").strip():
            env[var] = val
    if (devin_key := os.getenv("DEVIN_API_KEY", "").strip()) and not os.getenv("WINDSURF_API_KEY", "").strip():
        env["WINDSURF_API_KEY"] = devin_key
    resolved_args = _resolve_args() if args is None else args
    if requested_model and requested_model != "devin" and "--model" not in resolved_args:
        env["DEVIN_MODEL"] = requested_model
    env["HOME"] = _resolve_home_dir()
    apply_subprocess_home_env(env)
    return env


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _is_disabled(entry: Any) -> bool:
    """Vendor-neutral version of Copilot's ``_meta.copilotEnablement`` check: an entry is
    disabled when any ``_meta`` value is the literal string ``disabled``."""
    meta = entry.get("_meta") if isinstance(entry, dict) else None
    if not isinstance(meta, dict):
        return False
    return any(str(v).strip().lower() == "disabled" for v in meta.values())


def _enabled_ids(entries: Any, key: str) -> set[str]:
    return {str(e.get(key) or "").strip() for e in (entries or [])
            if isinstance(e, dict) and not _is_disabled(e)}


def _model_selection_request(session: dict[str, Any], requested_model: str) -> tuple[str, dict[str, Any]] | None:
    """ACP request selecting ``requested_model`` for ``session``: stable v1
    ``session/set_config_option``, else ``session/set_model`` when no model config
    option is advertised. A reported model list is authoritative: unknown and
    policy-disabled ids return None instead of being sent."""
    session_id = str(session.get("sessionId") or "").strip()
    requested_model = str(requested_model or "").strip()
    if not session_id or not requested_model or requested_model == "devin":
        return None
    options = [o for o in (session.get("configOptions") or [])
               if isinstance(o, dict) and "model" in (o.get("category"), o.get("id"))]
    if options:
        if requested_model not in _enabled_ids(options[0].get("options"), "value"):
            return None
        return "session/set_config_option", {
            "sessionId": session_id,
            "configId": str(options[0].get("id") or "model"),
            "value": requested_model,
        }
    available = _enabled_ids((session.get("models") or {}).get("availableModels"), "modelId")
    if not available or requested_model not in available:
        return None
    return "session/set_model", {"sessionId": session_id, "modelId": requested_model}


def _authenticate_request(init_result: dict[str, Any], api_key: str) -> dict[str, Any] | None:
    """Pick an ``authenticate`` params dict when the server advertises authMethods and we
    hold a key. Prefers a method whose id/name mentions a key/token; falls back to the
    first advertised method."""
    methods = [m for m in (init_result.get("authMethods") or []) if isinstance(m, dict)]
    if not methods or not api_key:
        return None
    chosen = methods[0]
    for method in methods:
        label = f"{method.get('id', '')} {method.get('name', '')} {method.get('title', '')}".lower()
        if any(hint in label for hint in ("key", "token", "windsurf", "devin")):
            chosen = method
            break
    method_id = str(chosen.get("id") or chosen.get("methodId") or "").strip()
    return {"methodId": method_id} if method_id else None


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _prompt_token_budget() -> int:
    raw = os.getenv("HERMES_DEVIN_PROMPT_TOKENS", "").strip()
    try:
        return max(1_000, int(raw)) if raw else _DEFAULT_PROMPT_TOKEN_BUDGET
    except ValueError:
        return _DEFAULT_PROMPT_TOKEN_BUDGET


def _fit_transcript_turns(turns: list[str], token_budget: int) -> list[str]:
    """Keep the newest turns that fit the token budget, dropping oldest first."""
    kept: list[str] = []
    used = 0
    for turn in reversed(turns):
        cost = _estimate_tokens(turn)
        if kept and used + cost > token_budget:
            break
        kept.append(turn)
        used += cost
    kept.reverse()
    dropped = len(turns) - len(kept)
    if kept and used > token_budget:
        # The newest turn alone exceeds the budget: keep its tail, where the
        # latest instructions live.
        kept[0] = _TRUNCATION_NOTE + kept[0][-(token_budget * _CHARS_PER_TOKEN):]
    if dropped:
        kept.insert(0, _OMISSION_NOTE.format(count=dropped))
    return kept


def _format_messages_as_prompt(
    messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    # Deliberately no "requested model" line: the model is applied for real via ACP
    # session/set_model; a prompt-text mention makes a substituted backend model falsely
    # self-identify as the requested one.
    sections: list[str] = [*_PROMPT_PREAMBLE, *_render_tool_bridge_sections(tools, tool_choice)]
    transcript: list[str] = []
    for message in (m for m in messages if isinstance(m, dict)):
        role = str(message.get("role") or "unknown").strip().lower()
        if rendered := _render_message_content(message.get("content")):
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
    closing = "Continue the conversation from the latest user request."
    if transcript:
        fixed_cost = _estimate_tokens("\n\n".join(sections)) + _estimate_tokens(closing)
        fitted = _fit_transcript_turns(transcript, max(1_000, _prompt_token_budget() - fixed_cost))
        sections.append("Conversation transcript:\n\n" + "\n\n".join(fitted))
    sections.append(closing)
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        return content["content"].strip() if isinstance(content.get("content"), str) else json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts = [item if isinstance(item, str) else item["text"].strip() for item in content if isinstance(item, str)
                 or (isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip())]
        return "\n".join(parts).strip()
    return str(content).strip()


def _ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    if not Path(path_text).is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved, root = Path(path_text).resolve(), Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


def _effective_timeout(timeout: Any) -> float:
    """Normalise a float or httpx.Timeout-like object to wall-clock seconds (largest component wins)."""
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    return max((float(v) for v in candidates if isinstance(v, (int, float))), default=_DEFAULT_TIMEOUT_SECONDS)


def _fs_read_text_file(params: dict[str, Any], cwd: str) -> Any:
    path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
    if block_error := get_read_block_error(str(path)):
        raise PermissionError(block_error)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    line, limit = params.get("line"), params.get("limit")
    if isinstance(line, int) and line > 1:
        end = line - 1 + limit if isinstance(limit, int) and limit > 0 else None
        content = "".join(content.splitlines(keepends=True)[line - 1:end])
    return {"content": redact_sensitive_text(content, force=True) if content else content}


def _fs_write_text_file(params: dict[str, Any], cwd: str) -> Any:
    path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
    if denied := get_write_denied_error(str(path)):
        raise PermissionError(denied)
    if is_write_approval_required(str(path)):  # the ACP shim has no human channel → fail closed
        raise PermissionError(f"Write denied: '{path}' requires interactive approval and cannot be written through the ACP file bridge.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(params.get("content") or ""), encoding="utf-8")
    return None


_FS_HANDLERS = {"fs/read_text_file": _fs_read_text_file, "fs/write_text_file": _fs_write_text_file}


class DevinACPClient:
    """Minimal OpenAI-client-compatible facade for `devin acp`."""

    # Declared for agent/auxiliary_client.py: this shim drives an ACP subprocess over
    # stdio, so it is already a complete client (never re-dispatch through a wire
    # adapter) and async-safe as-is.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None, acp_args: list[str] | None = None,
        acp_cwd: str | None = None, command: str | None = None,
        args: list[str] | None = None, **_: Any,
    ):
        self.api_key, self.base_url = api_key or "devin", base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        self._acp_args = list(acp_args or args or _resolve_args())
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed, self._active_process = False, None
        self._active_process_lock = threading.Lock()

    def close(self) -> None:
        with self._active_process_lock:
            proc, self._active_process = self._active_process, None
        self.is_closed = True
        try:
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def _create_chat_completion(
        self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None, tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None, stream: bool = False, **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(messages or [], model=model, tools=tools, tool_choice=tool_choice)
        response_text, reasoning, stop_reason = self._run_prompt(
            prompt_text, timeout_seconds=_effective_timeout(timeout), model=model
        )
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        if stop_reason in ("refusal", "cancelled") and not cleaned_text:
            raise RuntimeError(
                f"Devin ACP ended the turn with stopReason={stop_reason!r} and no content."
            )
        message = SimpleNamespace(
            content=cleaned_text, tool_calls=tool_calls, reasoning=reasoning or None,
            reasoning_content=reasoning or None, reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(
                message=message,
                finish_reason="tool_calls" if tool_calls else ("length" if stop_reason == "max_tokens" else "stop"),
            )],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
            model=model or "devin",
        )
        return _completion_to_stream_chunks(completion) if stream else completion

    def _build_subprocess_env(self, requested_model: str | None = None) -> dict[str, str]:
        return _build_subprocess_env(requested_model, self._acp_args)

    def _spawn(self, requested_model: str | None = None) -> subprocess.Popen[str]:
        # Fast-fail when the CLI has no acp subcommand (else the parent waits the full
        # child timeout for stdout that never arrives).
        if _acp_supported(self._acp_command, self._acp_args) is False:
            raise RuntimeError(_INSTALL_ERROR % self._acp_command + "the `--help` output does not list an `acp` subcommand.")
        try:
            from hermes_cli._subprocess_compat import (
                windows_hide_flags,  # hide the Windows console flash; pipes intact for the ACP wire
            )

            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=self._acp_cwd, env=self._build_subprocess_env(requested_model), creationflags=windows_hide_flags(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_INSTALL_ERROR % self._acp_command + str(exc)) from exc
        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Devin ACP process did not expose stdin/stdout pipes.")
        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc
        return proc

    def _run_prompt(
        self, prompt_text: str, *, timeout_seconds: float, model: str | None = None
    ) -> tuple[str, str, str]:
        requested_model = str(model or "").strip()
        proc = self._spawn(requested_model)
        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _decode(line: str) -> dict[str, Any]:
            try:
                return json.loads(line)
            except Exception:
                return {"raw": line.rstrip("\n")}

        def _pump(stream, sink) -> None:
            for line in stream or ():
                sink(line)

        threading.Thread(target=_pump, args=(proc.stdout, lambda line: inbox.put(_decode(line))), daemon=True).start()
        threading.Thread(target=_pump, args=(proc.stderr, lambda line: stderr_tail.append(line.rstrip("\n"))), daemon=True).start()
        request_ids = iter(range(1, 1 << 62))

        def _request(method: str, params: dict[str, Any], *, text_parts: list[str] | None = None,
                     reasoning_parts: list[str] | None = None) -> Any:
            request_id = next(request_ids)
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._handle_server_message(
                    msg, process=proc, cwd=self._acp_cwd, text_parts=text_parts, reasoning_parts=reasoning_parts
                ) or msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(f"Devin ACP {method} failed: {err.get('message') or err}")
                return msg.get("result")
            stderr_text = "\n".join(stderr_tail).strip()
            if proc.poll() is not None and stderr_text:
                raise RuntimeError(f"Devin ACP process exited early: {stderr_text}")
            raise TimeoutError(f"Timed out waiting for Devin ACP response to {method}.")

        try:
            init_result = _request("initialize", _INITIALIZE_PARAMS) or {}
            if auth_params := _authenticate_request(init_result, _resolve_devin_key()):
                try:
                    _request("authenticate", auth_params)
                except Exception as exc:
                    logger.debug("Devin ACP authenticate request failed; relying on stored CLI credentials: %s", exc)
            session = _request("session/new", {"cwd": self._acp_cwd, "mcpServers": []}) or {}
            session_id = str(session.get("sessionId") or "").strip()
            if not session_id:
                raise RuntimeError("Devin ACP did not return a sessionId.")
            if requested_model and requested_model != "devin":
                try:
                    if (selection := _model_selection_request(session, requested_model)) is not None:
                        _request(*selection)
                    elif not any(
                        isinstance(option, dict) and "model" in (option.get("category"), option.get("id"))
                        for option in (session.get("configOptions") or [])
                    ) and not (session.get("models") or {}).get("availableModels"):
                        logger.debug(
                            "Devin ACP did not advertise model options; DEVIN_MODEL=%r was applied.",
                            requested_model,
                        )
                    else:
                        logger.warning(
                            "Devin ACP does not advertise model %r in its model options; relying on "
                            "the CLI-level model (DEVIN_MODEL/--model) resolved at spawn.",
                            requested_model,
                        )
                except Exception as exc:
                    logger.warning("Devin ACP model selection for %r failed; continuing with the session default: %s", requested_model, exc)
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]}
            prompt_result = _request("session/prompt", prompt, text_parts=text_parts, reasoning_parts=reasoning_parts) or {}
            return (
                "".join(text_parts),
                "".join(reasoning_parts),
                str(prompt_result.get("stopReason") or ""),
            )
        finally:
            self.close()

    def _handle_server_message(
        self, msg: dict[str, Any], *, process: subprocess.Popen[str], cwd: str,
        text_parts: list[str] | None, reasoning_parts: list[str] | None,
    ) -> bool:
        """Consume a server->client message; True when handled (notification or request answered)."""
        method = msg.get("method")
        if not isinstance(method, str):
            return False
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            content = update.get("content") or {}
            chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
            sinks = {"agent_message_chunk": text_parts, "agent_thought_chunk": reasoning_parts}
            if chunk_text and (sink := sinks.get(str(update.get("sessionUpdate") or "").strip())) is not None:
                sink.append(chunk_text)
            return True
        if process.stdin is None:
            return True
        message_id = msg.get("id")
        if method == "session/request_permission":
            response = _jsonrpc_result(message_id, {"outcome": {"outcome": "cancelled"}})
        elif method in _FS_HANDLERS:
            try:
                response = _jsonrpc_result(message_id, _FS_HANDLERS[method](msg.get("params") or {}, cwd))
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(message_id, -32601, f"ACP client method '{method}' is not supported by Hermes yet.")
        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
