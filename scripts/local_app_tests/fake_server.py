"""Test-only stand-in for uvicorn/Vite: serves / and /api/health on 127.0.0.1:<port>. argv: port [exit-immediately]."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

if len(sys.argv) > 2 and sys.argv[2] == "die":
    sys.exit(3)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"service": "rosc"}).encode() if self.path == "/api/health" else b"<html>ok</html>"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
