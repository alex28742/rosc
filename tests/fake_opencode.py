"""Test-only stand-in for ``opencode serve``. NOT a product runtime and never imported by ``src/``.

It speaks only the small subset of the OpenCode 2.2.5 HTTP surface the ROSC adapter uses, enforces the Basic-auth
contract (user ``opencode``, password from ``OPENCODE_PASSWORD``) and never calls a model. Behaviour is selected by the
file ``mode`` beside the launcher so a test can simulate refusal, an unrestricted session or no reply.
"""
from __future__ import annotations

import base64
import json
import os
import sys
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


STATE: dict = {"session": None, "messages": []}


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
            effect = "allow" if mode() == "leaky_permission" and body.get("action") == "read" else "deny"
            return self._send({"data": {"id": "per_1", "effect": effect}})
        if path == "/api/session/ses_fake/prompt":
            STATE["messages"].append({"id": body["id"], "type": "user", "text": body["text"]})
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
