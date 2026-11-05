"""Loopback MCP tool endpoint for the Project Architect runtime (P019 Gate 0).

OpenCode 2.2.5 can be given a *remote* MCP server at runtime (``PUT /api/experimental/mcp/<name>`` with ``url`` and
``headers``).  ROSC runs one small in-process HTTP endpoint on ``127.0.0.1`` (Streamable-HTTP style JSON-RPC, JSON
responses only) and gives each Architect runtime incarnation its own random bearer token.  The token maps to exactly one
ROSC Architect session id; the model can neither name another session nor another Project: every call is handed to
``ProjectContextBroker`` which re-verifies the live session.  The token is held in memory only, is handed to the child
through its MCP registration (never argv, a URL, a file or a log line) and is revoked on deactivation.  After a backend
restart the token is gone: tools then answer 401 until the Owner activates the Architect again.

The exposed operations are the permission: there is no write tool for other Projects, the source checkout or the global
library, and no generic shell/file/network tool.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)

SERVER_NAME = "rosc"
PERMISSION_PATTERN = f"{SERVER_NAME}_*"  # OpenCode names MCP tools <server>_<tool>
MAX_BODY = 1_000_000
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_PROJECT = {"type": "string", "description": "Project id such as P0002. Omit for the current Project."}
_PAGING = {"offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1}}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


TOOLS: list[dict] = [
    {"name": "briefing", "description": "Current Project briefing: identity, bound source route, document references and "
     "the access matrix. Start here.", "inputSchema": _schema({})},
    {"name": "global_list", "description": "List the read-only global rules/skills/tools definition library.",
     "inputSchema": _schema({})},
    {"name": "global_read", "description": "Read one global definition file (read-only documentation).",
     "inputSchema": _schema({"path": {"type": "string"}, **_PAGING}, ["path"])},
    {"name": "docs_list", "description": "List ROSC-private documents of the current Project (read/write) or of another "
     "registered Project (read-only).", "inputSchema": _schema({
         "project": _PROJECT, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1},
         "include_trash": {"type": "boolean"}})},
    {"name": "docs_search", "description": "Search ROSC-private documents; project may be 'all' for every registered "
     "Project.", "inputSchema": _schema({"query": {"type": "string"}, "project": {
         "type": "string", "description": "Project id, or 'all'."}}, ["query"])},
    {"name": "docs_read", "description": "Read a ROSC-private document (current or other registered Project). Returns "
     "next_offset when more remains.", "inputSchema": _schema({
         "doc_id": {"type": "string"}, "project": _PROJECT, "revision": {"type": "integer", "minimum": 1}, **_PAGING},
         ["doc_id"])},
    {"name": "docs_create", "description": "Create a new document in the CURRENT Project's private workspace.",
     "inputSchema": _schema({"doc_id": {"type": "string"}, "content": {"type": "string"},
                             "logical_path": {"type": "string"}, "media_type": {"type": "string"}},
                            ["doc_id", "content"])},
    {"name": "docs_update", "description": "Replace a document of the CURRENT Project; expected_revision is the "
     "revision you read.", "inputSchema": _schema({"doc_id": {"type": "string"}, "content": {"type": "string"},
                                                   "expected_revision": {"type": "integer", "minimum": 1}},
                                                  ["doc_id", "content", "expected_revision"])},
    {"name": "docs_delete", "description": "Recoverably delete a document of the CURRENT Project (moved to trash with "
     "history).", "inputSchema": _schema({"doc_id": {"type": "string"},
                                           "expected_revision": {"type": "integer", "minimum": 1}},
                                          ["doc_id", "expected_revision"])},
    {"name": "docs_restore", "description": "Restore a trashed document of the CURRENT Project (trash_id from "
     "docs_list include_trash).", "inputSchema": _schema({"trash_id": {"type": "string"}}, ["trash_id"])},
    {"name": "source_list", "description": "List a directory of the current Project's bound source checkout (READ-ONLY).",
     "inputSchema": _schema({"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1}})},
    {"name": "source_read", "description": "Read a text file of the bound source checkout (READ-ONLY, never writable).",
     "inputSchema": _schema({"path": {"type": "string"}, **_PAGING}, ["path"])},
    {"name": "source_search", "description": "Bounded case-insensitive text search in the bound source checkout.",
     "inputSchema": _schema({"query": {"type": "string"}, "path": {"type": "string"}}, ["query"])},
    {"name": "source_git", "description": "Non-executing Git facts of the bound checkout: head, status or log.",
     "inputSchema": _schema({"operation": {"type": "string", "enum": ["head", "status", "log"]},
                             "limit": {"type": "integer", "minimum": 1}})},
]
TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
_DISPATCH_TOOL_ARGS = {tool["name"]: set(tool["inputSchema"]["properties"]) | {"project"} for tool in TOOLS}


@dataclass(frozen=True)
class ToolEndpoint:
    """What a runtime needs to register the ROSC tools in one child process."""

    url: str
    token: str


class ToolHost:
    """Owns the loopback listener and the session-token table; dispatches to ``call(session_id, tool, args)``."""

    def __init__(self, call: Callable[[str, str, dict], dict]) -> None:
        self._call = call
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------------------------------------------------
    def _ensure_server(self) -> ThreadingHTTPServer:
        with self._lock:
            if self._server is None:
                host = self

                class Handler(_Handler):
                    owner = host

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = True
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, name="architect-tools", daemon=True)
                self._thread.start()
            return self._server

    def open(self, session_id: str) -> ToolEndpoint:
        """Issue a fresh per-incarnation token for ``session_id`` (replacing any older one)."""
        server = self._ensure_server()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[session_id] = token
        return ToolEndpoint(f"http://127.0.0.1:{server.server_address[1]}/mcp", token)

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._tokens.pop(session_id, None)

    def active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._tokens

    def shutdown(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self._tokens.clear()
        if server is not None:
            server.shutdown()
            server.server_close()

    def session_for(self, presented: str) -> str | None:
        with self._lock:
            items = list(self._tokens.items())
        found = None
        for session_id, token in items:  # constant-time compare for every entry
            if hmac.compare_digest(token.encode(), presented.encode()):
                found = session_id
        return found

    # -- JSON-RPC ---------------------------------------------------------------------------------------------------
    def handle(self, session_id: str, message: object) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        if "id" not in message:  # notification
            return None
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return self._ok(ident, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                    "serverInfo": {"name": "rosc-project-tools", "version": "1"}})
        if method == "ping":
            return self._ok(ident, {})
        if method == "tools/list":
            return self._ok(ident, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            arguments = params.get("arguments") if isinstance(params, dict) else None
            if name not in _DISPATCH_TOOL_ARGS:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unknown tool"}}
            arguments = {} if arguments is None else arguments
            if not isinstance(arguments, dict) or set(arguments) - _DISPATCH_TOOL_ARGS[name]:
                outcome = {"ok": False, "error": {"code": "invalid_argument", "message": "Unexpected or malformed arguments"}}
            else:
                outcome = self._call(session_id, name, arguments)
            return self._ok(ident, {"content": [{"type": "text", "text": json.dumps(outcome, ensure_ascii=False)}],
                                    "isError": not outcome.get("ok", False)})
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}

    @staticmethod
    def _ok(ident: object, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "result": result}


class _Handler(BaseHTTPRequestHandler):
    owner: ToolHost
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # never log requests: they carry document text
        pass

    def _reply(self, status: int, payload: object | None = None) -> None:
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _session(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        host = self.headers.get("Host") or ""
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}") or self.headers.get("Origin"):
            return None  # not a loopback MCP client (DNS rebinding / browser)
        return self.owner.session_for(token)

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/mcp":
            return self._reply(404)
        session_id = self._session()
        if session_id is None:
            return self._reply(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400)
        if length < 0 or length > MAX_BODY:
            return self._reply(413)
        try:
            message = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if isinstance(message, list):
            replies = [r for r in (self.owner.handle(session_id, item) for item in message) if r is not None]
            return self._reply(200, replies) if replies else self._reply(202)
        response = self.owner.handle(session_id, message)
        return self._reply(202) if response is None else self._reply(200, response)

    def do_GET(self) -> None:  # no server-initiated stream
        self._reply(405)

    def do_DELETE(self) -> None:
        self._reply(405)
