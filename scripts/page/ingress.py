"""Stands in for Home Assistant's ingress, inside a throwaway container.

Run by scripts/screenshots.py at 172.30.32.2, the Supervisor's address on its
network 172.30.32.0/23 and the only client the page answers:

    /                                   a page with the app in an iframe,
                                        as Home Assistant's panel shows it
    /?lang=de                           the same with a stand-in for the
                                        frontend's <home-assistant> element,
                                        whose hass names that language (and
                                        light mode), as the page reads it
    /?safe=47,34                        the same on a phone with a notch: the
                                        safe-area insets top and bottom in px
                                        that Home Assistant passes on
    /api/hassio_ingress/<TOKEN>/...     passed on to http://app:8099/...
                                        without the prefix, like the Supervisor

The outer page plays Home Assistant's part (its app panel, ha-panel-app) for
the page's ha-frame.js: it answers subscribe-properties with properties
(narrow below 870 px, the insets, the route) and again on every resize; it
shows its 40 px bar #ha-bar above the frame while narrow and not in kiosk
mode, and pads the frame by the insets unless the page handles them
(handleSafeArea). What the page asked for lands in window.__kiosk,
window.__safe and window.__menu (toggle-menu clicks), every message in
window.__messages.

Standard library only; it runs with the python3 of the app's image.
"""

from __future__ import annotations

import http.client
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

TOKEN = "Zm9vYmFyLWRlbW8tdG9rZW4"
PREFIX = f"/api/hassio_ingress/{TOKEN}"
APP = ("app", 8099)
PORT = 8000
# Not passed on, in either direction
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length",
       "proxy-connection", "te", "trailer"}
OUTER = f"""<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title><link rel="icon" href="data:,">
<style>html, body {{ margin: 0; height: 100%; }}
body {{ display: flex; flex-direction: column; }}
#ha-bar {{ flex: none; height: 40px; background: #888; }}
#ha-app {{ flex: 1; min-height: 0; box-sizing: border-box; }}
iframe {{ border: 0; width: 100%; height: 100%; display: block; }}</style>
<script>{{FRAME}}</script></head><body>HASS<div id="ha-bar" hidden></div>
<div id="ha-app"><iframe id="ingress" src="{PREFIX}/" title="App"></iframe></div></body></html>"""
# Home Assistant's app panel as far as ha-frame.js talks to it; in the head, so
# it listens before the frame loads
FRAME = """(() => {
  const safe = (new URLSearchParams(location.search).get("safe") || "0,0").split(",");
  const inset = (n) => (parseInt(n, 10) || 0) + "px";
  const insets = { top: inset(safe[0]), right: "0px", bottom: inset(safe[1]), left: "0px" };
  const frame = { get contentWindow() { return document.getElementById("ingress").contentWindow; } };
  Object.assign(window, { __kiosk: null, __safe: null, __menu: 0, __messages: [] });
  let subscribed = false;
  const narrow = () => matchMedia("(max-width: 870px)").matches;
  const layout = () => {
    const bar = document.getElementById("ha-bar"), box = document.getElementById("ha-app");
    if (!bar || !box) return;
    bar.hidden = !(narrow() && !window.__kiosk);
    box.style.paddingTop = window.__safe || !bar.hidden ? "0px" : insets.top;
    box.style.paddingBottom = window.__safe ? "0px" : insets.bottom;
  };
  const send = () => {
    if (subscribed) frame.contentWindow.postMessage({ type: "home-assistant/properties", narrow: narrow(),
      route: { prefix: "/app/demo", path: "" }, safeAreaInsets: insets }, location.origin);
  };
  window.addEventListener("message", (e) => {
    if (e.source !== frame.contentWindow || !e.data) return;
    window.__messages.push(e.data);
    if (e.data.type === "home-assistant/subscribe-properties") {
      subscribed = true;
      window.__kiosk = !!e.data.kioskMode;
      window.__safe = !!e.data.handleSafeArea;
      layout();
      send();
    } else if (e.data.type === "home-assistant/unsubscribe-properties") {
      subscribed = false;
      window.__kiosk = false;
      window.__safe = false;
      layout();
    } else if (e.data.type === "home-assistant/toggle-menu") {
      window.__menu += 1;
    }
  });
  window.addEventListener("resize", () => { layout(); send(); });
  document.addEventListener("DOMContentLoaded", layout);
})();"""

# Home Assistant's frontend as far as the page reads it: the user's language
HASS = """<home-assistant></home-assistant><script>document.querySelector("home-assistant").hass =
{ language: "LANG", locale: { language: "LANG" }, themes: { darkMode: false } };</script>"""
LANG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")


def outer(query: str) -> bytes:
    """The page around the iframe; with ?lang=xx as Home Assistant in that language."""
    lang = (parse_qs(query).get("lang") or [""])[0]
    hass = HASS.replace("LANG", lang) if LANG.fullmatch(lang) else ""
    return OUTER.replace("{FRAME}", FRAME).replace("HASS", hass).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", ""):
            self._reply(200, [("Content-Type", "text/html; charset=utf-8")], outer(query))
            return
        if path == "/favicon.ico":
            # Home Assistant has one; without it Chrome may log a 404 as a console error
            self._reply(204, [], b"")
            return
        if self.path != PREFIX and not self.path.startswith(PREFIX + "/") and not self.path.startswith(PREFIX + "?"):
            self._reply(404, [("Content-Type", "text/plain")], b"not found")
            return
        rest = self.path[len(PREFIX):]
        rest = rest if rest.startswith("/") else "/" + rest
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["X-Ingress-Path"] = PREFIX
        headers["X-Forwarded-For"] = self.client_address[0]
        conn = http.client.HTTPConnection(*APP, timeout=30)
        try:
            conn.request(self.command, rest, body=body, headers=headers)
            answer = conn.getresponse()
            data = answer.read()
            self._reply(answer.status, [(k, v) for k, v in answer.getheaders() if k.lower() not in HOP], data)
        except OSError as err:
            self._reply(502, [("Content-Type", "text/plain")], f"app unreachable: {err}".encode())
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

    def _reply(self, code: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(code)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
