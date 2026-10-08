"""Trusted recipe subprocess. Project text is data: never eval/import/exec it."""

import ast
import json
import os
import resource
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def serve(payload):
    files = {"/" + f["path"]: f["content"].encode("utf-8") for f in payload["files"]}
    health = json.dumps({"run_id": payload["run_id"], "instance_id": payload["instance_id"]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(1)

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == "/__factory_health":
                raw, kind, status = health, "application/json", 200
            else:
                path = "/index.html" if path == "/" else path
                found = files.get(path)
                status = 200 if found is not None else 404
                kind = "text/css" if path.endswith(".css") else "text/html"
                raw = found if found is not None else b"Not found"
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "sandbox allow-same-origin; default-src 'none'; style-src 'self'; script-src 'none'; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

    try:
        server = HTTPServer(("127.0.0.1", payload["port"]), Handler)
    except OSError:
        emit({"kind": "error", "code": "PORT_IN_USE"})
        return 98
    emit({"kind": "ready", "port": payload["port"]})
    server.timeout = 0.25
    deadline = time.monotonic() + 600

    def watch_parent():
        while time.monotonic() < deadline:
            if os.getppid() != payload["parent_pid"]:
                os._exit(90)
            time.sleep(0.25)
        os._exit(91)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        while True:
            server.handle_request()
    finally:
        server.server_close()


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with open("input.json", encoding="utf-8") as stream:
        payload = json.load(stream)
    recipe = sys.argv[1]
    if recipe == "static_preview":
        return serve(payload)
    failed = False
    for index, file in enumerate(payload["files"]):
        result = {
            "kind": "check",
            "index": index,
            "valid": True,
            "line": None,
            "column": None,
            "error_code": None,
        }
        try:
            if recipe == "python_syntax":
                ast.parse(file["content"])
            elif recipe == "json_validate":
                json.loads(file["content"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            else:
                return 64
        except SyntaxError as exc:
            result.update(valid=False, line=exc.lineno, column=exc.offset, error_code="PYTHON_SYNTAX")
            failed = True
        except json.JSONDecodeError as exc:
            result.update(valid=False, line=exc.lineno, column=exc.colno, error_code="JSON_SYNTAX")
            failed = True
        except (ValueError, RecursionError):
            result.update(valid=False, error_code="INVALID_STRUCTURE")
            failed = True
        emit(result)
    emit({"kind": "done", "count": len(payload["files"])})
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        emit({"kind": "error", "code": "RECIPE_FAILED"})
        sys.exit(70)
