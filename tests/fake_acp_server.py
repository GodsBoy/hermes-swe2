"""Small stdlib-only ACP server used by the offline integration test."""

import json
import os
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


session_id = "fake-session"
selected_model = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"authMethods": [{"id": "api-key", "name": "API key"}]},
        })
    elif method == "authenticate":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/new":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "sessionId": session_id,
                "configOptions": [{
                    "id": "model",
                    "category": "model",
                    "type": "select",
                    "currentValue": "swe-2-max",
                    "options": [
                        {"value": "swe-2-medium", "name": "SWE-2 Medium"},
                        {"value": "swe-2-high", "name": "SWE-2 High"},
                        {"value": "swe-2-max", "name": "SWE-2 Max"},
                        {"value": "swe-1-7", "name": "SWE-1.7 Max"},
                        {"value": "swe-1-7-medium", "name": "SWE-1.7 Medium"},
                        {"value": "gpt-5.6-sol-none", "name": "GPT-5.6 Sol No Thinking"},
                        {"value": "gpt-5.6-sol-max", "name": "GPT-5.6 Sol Max Thinking"},
                    ],
                }],
            },
        })
    elif method == "session/set_config_option":
        params = request.get("params") or {}
        if params.get("configId") == "model":
            selected_model = params.get("value")
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/set_model":
        selected_model = (request.get("params") or {}).get("modelId")
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/prompt":
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Thinking about the request."},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Hello from fake ACP ("},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": f"{selected_model or os.environ.get('DEVIN_MODEL', '')}).",
                    },
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"stopReason": "end_turn"},
        })
    else:
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        })
