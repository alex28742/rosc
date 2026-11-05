"""Test-only stand-in for ``opencode serve``. NOT a product runtime and never imported by ``src/``.

It speaks only the small subset of the OpenCode 2.2.5 HTTP surface the ROSC adapter uses, enforces the Basic-auth
contract (user ``opencode``, password from ``OPENCODE_PASSWORD``) and never calls a model. Behaviour is selected by the
file ``mode`` beside the launcher so a test can simulate refusal, an unrestricted session or no reply.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

PASSWORD = os.environ.get("OPENCODE_PASSWORD", "")


def mode() -> str:
    """Read per request from a ``mode`` file beside the launcher: ROSC passes the child only an allowlisted
    environment, so a test cannot steer it through variables."""
    try:
        with open(os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "mode")) as handle:
            return handle.read().strip() or "ok"
    except OSError:
        return "ok"


STATE: dict = {"session": None, "messages": [], "mcp": {}}


def beside(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), name)


def permission_effect(action: str) -> str:
    """Like the real 2.2.5 evaluator (verified live): the last matching rule wins, no match means deny."""
    effect = "deny"
    for rule in (STATE["session"] or {}).get("permissions") or []:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase("*", rule["resource"]):
            effect = rule["effect"]
    return effect


def mcp_post(config: dict, payload: dict):
    request = urllib.request.Request(config["url"], data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream",
                                              **config.get("headers", {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)


def connect_mcp(name: str) -> None:
    """What OpenCode does on PUT: initialize + tools/list against the remote server with the configured headers."""
    config = STATE["mcp"][name]["config"]
    if mode() == "mcp_fail":
        STATE["mcp"][name]["status"] = "failed"
        return
    try:
        mcp_post(config, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "fake", "version": "0"}}})
        mcp_post(config, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _status, listing = mcp_post(config, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        STATE["mcp"][name]["tools"] = [tool["name"] for tool in listing["result"]["tools"]]
        STATE["mcp"][name]["status"] = "connected"
    except urllib.error.HTTPError as exc:
        STATE["mcp"][name]["status"] = "needs_auth" if exc.code == 401 else "failed"
    except Exception:
        STATE["mcp"][name]["status"] = "failed"


def run_scripted_tool_calls() -> None:
    """Test-only stand-in for the model deciding to call tools: ``tool_calls.json`` beside the launcher lists
    ``{"tool": ..., "args": {...}}`` (an MCP tool) or ``{"native": "read"}`` (an OpenCode builtin).  Each call is
    first checked against the session's permission rules exactly like a real tool call would be."""
    try:
        with open(beside("tool_calls.json"), encoding="utf-8") as handle:
            calls = json.load(handle)
    except (OSError, ValueError):
        return
    with open(beside("tool_log.jsonl"), "a", encoding="utf-8") as log:
        for call in calls:
            if "native" in call:
                log.write(json.dumps({"call": call, "allowed": permission_effect(call["native"]) == "allow"}) + "\n")
                continue
            action = f"rosc_{call['tool']}"
            allowed = permission_effect(action) == "allow" and "rosc" in STATE["mcp"]
            entry = {"call": call, "allowed": allowed}
            if allowed:
                try:
                    _status, answer = mcp_post(STATE["mcp"]["rosc"]["config"], {
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": call["tool"], "arguments": call.get("args", {})}})
                    if "error" in answer:
                        entry["rpc_error"] = answer["error"]["code"]
                    else:
                        entry["result"] = json.loads(answer["result"]["content"][0]["text"])
                        entry["is_error"] = answer["result"]["isError"]
                except urllib.error.HTTPError as exc:
                    entry["http_status"] = exc.code
            log.write(json.dumps(entry) + "\n")


def synthetic_reply() -> dict:
    """``reply.json`` beside the launcher (test-controlled): ``{"parts": [...], "before": [...]}``.  ``parts`` are the
    assistant's text parts for the next prompt; ``before`` are unrelated earlier assistant messages (possibly huge)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "reply.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"parts": [], "before": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence
        pass

    def _send(self, payload, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send({"_tag": "UnauthorizedError"}, 401)
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/info":
            return self._send({"version": "2.2.5", "pid": os.getpid(), "urls": []})
        if path == "/api/model":
            models = [{"providerID": "prov", "id": "model-a"}]
            if os.environ.get("OPENCODE_API_KEY"):  # like the real 2.2.5: the env key makes opencode-go available
                models.append({"providerID": "opencode-go", "id": "deepseek-v4.1-flash"})
            return self._send({"data": models})
        if path == "/api/mcp":
            return self._send({"location": {"directory": "/"}, "data": [
                {"name": name, "status": {"status": item["status"]}} for name, item in STATE["mcp"].items()]})
        if path == "/api/session/ses_fake":
            return self._send({"data": STATE["session"]})
        if path == "/api/session/ses_fake/message":
            query = parse_qs(parsed.query)
            cursor = query.get("cursor", [None])[0]
            offset = int(cursor.removeprefix("cursor-")) if cursor else 0
            if mode() == "pagination_error" and offset:
                return self._send({"_tag": "UnknownError"}, 500)
            limit = min(int(query.get("limit", [200])[0]), 200)
            items = STATE["messages"][offset:offset + limit]
            next_offset = offset + len(items)
            next_cursor = f"cursor-{next_offset}" if next_offset < len(STATE["messages"]) else None
            previous = f"cursor-{max(0, offset - limit)}" if offset else None
            return self._send({"data": items, "cursor": {"previous": previous, "next": next_cursor}})
        self._send({}, 404)

    def do_PUT(self) -> None:
        if not self._authorized():
            return self._send({"_tag": "UnauthorizedError"}, 401)
        path, body = self.path.split("?")[0], self._body()
        prefix = "/api/experimental/mcp/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            STATE["mcp"][name] = {"config": body["config"], "status": "pending", "tools": []}
            connect_mcp(name)
            self.send_response(204)
            self.end_headers()
            return
        self._send({}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send({"_tag": "UnauthorizedError"}, 401)
        path, body = self.path.split("?")[0], self._body()
        if path == "/api/session":
            permissions = [] if mode() == "unrestricted" else body.get("permissions")
            STATE["session"] = {"id": "ses_fake", "permissions": permissions}
            if mode() == "file_reply":
                STATE["messages"] = [
                    {"id": f"msg_before_{index}", "type": "assistant", "finish": "stop", "time": {"completed": 1},
                     "content": [{"type": "text", "text": text}]}
                    for index, text in enumerate(synthetic_reply().get("before", []))]
            if mode() in ("long_history", "long_match", "pagination_error"):
                STATE["messages"] = [
                    {"id": f"msg_history_{index}", "type": "assistant", "finish": "stop",
                     "time": {"completed": 1}, "content": [{"type": "text", "text": f"history {index}"}]}
                    for index in range(450)
                ]
            return self._send({"data": STATE["session"]})
        if path == "/api/session/ses_fake/permission":
            effect = permission_effect(body.get("action", ""))
            if mode() == "leaky_permission" and body.get("action") == "read":
                effect = "allow"
            return self._send({"data": {"id": "per_1", "effect": effect}})
        if path == "/api/session/ses_fake/prompt":
            STATE["messages"].append({"id": body["id"], "type": "user", "text": body["text"]})
            try:
                with open(beside("last_prompt.txt"), "w", encoding="utf-8") as handle:
                    handle.write(body["text"])
            except OSError:
                pass
            run_scripted_tool_calls()
            if mode() in ("ok", "long_match"):
                STATE["messages"].append({"id": "msg_a1", "type": "assistant", "finish": "stop",
                                          "time": {"completed": 1},
                                          "content": [{"type": "text", "text": "Fake architect reply"}]})
            elif mode() == "file_reply":
                STATE["messages"].append({"id": "msg_a1", "type": "assistant", "finish": "stop",
                                          "time": {"completed": 1},
                                          "content": [{"type": "text", "text": part}
                                                      for part in synthetic_reply().get("parts", [])]})
            elif mode() == "refuse":
                STATE["messages"].append({"id": "msg_a1", "type": "assistant", "finish": "error",
                                          "error": {"name": "x"}, "time": {"completed": 1}, "content": []})
            return self._send({"data": {"id": body["id"]}})
        if path == "/api/experimental/session/ses_fake/wait":
            self.send_response(204)
            self.end_headers()
            return
        self._send({}, 404)


def main() -> None:
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
