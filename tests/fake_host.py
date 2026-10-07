#!/usr/bin/env python3
"""A fake host for the guard service: the Docker API on /run/docker.sock and
the Supervisor (with the Home Assistant API behind it) on port 80, for a
container started with --add-host supervisor:127.0.0.1.

Every request is appended to <dir>/requests.jsonl. The test steers the fake
through files in <dir>:
  events.jsonl  Docker events of the Home Assistant container, one per line
  protected     present: the app runs in protection mode
  ha_stopped    present: the Home Assistant container is not running; a
                POST /core/start removes it
What a case would publish to /share is copied to <dir>/published.
Helper containers answer with the last line their command would print.
"""

import json
import os
import shutil
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR = Path(sys.argv[1])
SOCKET = "/run/docker.sock"
LOCK = threading.Lock()
HELPERS: dict[str, dict] = {}


def record(api, method, path, body):
    with LOCK, open(DIR / "requests.jsonl", "a") as log:
        log.write(json.dumps({"api": api, "method": method, "path": path, "body": body}) + "\n")


def helper_output(entrypoint, cmd):
    """The last line the helper's command would print."""
    if entrypoint == "/usr/local/bin/cg-hash.sh":
        return "hashed=3 mismatches=0"
    if entrypoint == "/usr/local/bin/cg-pfn.py":
        return "pages=0"
    if entrypoint == "/usr/local/bin/cg-lock.py":
        return json.dumps({"active": True, "ready": True, "slot": "slot-A", "slot_overlay": True,
                           "other_overlay": True, "config": True, "changed": False})
    script = " ".join(cmd)
    if "drop_caches" in script:
        return "dropped"
    if "crash_guard" in script:      # to_share
        return "done"
    return ""


class Handler(BaseHTTPRequestHandler):
    api = ""

    def address_string(self):
        return self.api

    def log_message(self, *args):
        pass

    def reply(self, status=200, body=None, text=None):
        data = text.encode() if text is not None else json.dumps(body if body is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if text is not None else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode(errors="replace")

    def handle_any(self):
        body = self.body()
        url = urllib.parse.urlsplit(self.path)
        record(self.api, self.command, url.path, body)
        self.route(self.command, url.path, urllib.parse.parse_qs(url.query), body)

    do_GET = do_POST = do_DELETE = handle_any


class Docker(Handler):
    api = "docker"

    def route(self, method, path, query, body):
        if path == "/containers/json":
            return self.reply(body=[{"Names": ["/app_c3edd230_crash_guard"], "Image": "crash-guard:test"}])
        if path == "/containers/selfcid/json":
            return self.reply(body={"Image": "sha256:feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface",
                                    "Mounts": [{"Destination": "/data",
                                                "Source": "/mnt/data/supervisor/apps/data/c3edd230_crash_guard"}]})
        if path == "/containers/homeassistant/json":
            return self.reply(body={
                "Image": "sha256:0123456789abcdef0123456789abcdef",
                "State": {"Running": not (DIR / "ha_stopped").exists(), "ExitCode": 0},
                "GraphDriver": {"Data": {"LowerDir": "/mnt/data/docker/overlay2/a/diff",
                                         "UpperDir": "/mnt/data/docker/overlay2/b/diff"}}})
        if path == "/containers/homeassistant/logs":
            return self.reply(text="2026-10-07T12:00:00Z Segmentation fault\n")
        if path == "/events":
            since, until = int(query["since"][0]), int(query["until"][0])
            lines = []
            if (DIR / "events.jsonl").exists():
                for line in (DIR / "events.jsonl").read_text().splitlines():
                    event = json.loads(line)
                    if since <= event["timeNano"] // 1_000_000_000 <= until:
                        lines.append(line)
            return self.reply(text="".join(f"{line}\n" for line in lines))
        if path == "/containers/create":
            helper_id = f"helper{len(HELPERS) + 1}"
            HELPERS[helper_id] = body
            cmd = body.get("Cmd") or []
            # publish_case: keep what would reach /share, for the test to look at
            # (Cmd: -c <script> to_share <case name> <case folder> ...)
            if any("mkdir --" in part for part in cmd) and len(cmd) >= 5 and os.path.isdir(cmd[4]):
                shutil.copytree(cmd[4], DIR / "published" / cmd[3], symlinks=True)
            return self.reply(201, {"Id": helper_id})
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "containers" and parts[1] in HELPERS:
            spec = HELPERS[parts[1]]
            if parts[2:] == ["wait"]:
                return self.reply(body={"StatusCode": 0})
            if parts[2:] == ["logs"]:
                return self.reply(text=helper_output(spec["Entrypoint"][0], spec.get("Cmd") or []) + "\n")
            return self.reply(200, {})
        if method == "DELETE":
            return self.reply(404, {"message": "no such container"})
        return self.reply(404, {"message": f"fake: {path}"})


class Supervisor(Handler):
    api = "supervisor"

    def route(self, method, path, query, body):
        if path == "/addons/self/info":
            return self.reply(body={"data": {"protected": (DIR / "protected").exists(),
                                             "slug": "c3edd230_crash_guard", "version": "1.3.2"}})
        if path == "/jobs/info":
            return self.reply(body={"data": {"jobs": []}})
        if path == "/core/start":
            (DIR / "ha_stopped").unlink(missing_ok=True)
            return self.reply(body={"result": "ok"})
        if path == "/core/api/" or path.startswith("/core/api/services/"):
            return self.reply(body={"message": "API running."})
        if path.startswith("/core/api/states/"):
            return self.reply(200 if method != "GET" else 404, {})
        return self.reply(404, {"message": f"fake: {path}"})


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("docker", 0)


def main():
    if os.path.exists(SOCKET):
        os.remove(SOCKET)
    docker = UnixServer(SOCKET, Docker)
    supervisor = ThreadingHTTPServer(("127.0.0.1", 80), Supervisor)
    threading.Thread(target=docker.serve_forever, daemon=True).start()
    (DIR / "ready").touch()
    supervisor.serve_forever()


if __name__ == "__main__":
    main()
