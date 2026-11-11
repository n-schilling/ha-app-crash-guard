"""Checks the web page of the built image and takes its screenshots.

The CI job Page and scripts/check.sh run it with --check; without it, it also
writes the pictures under docs/screenshots/.

    uv run --no-project --with playwright==1.65.0 python scripts/screenshots.py [--check] [--image NAME]

Without --image it builds the image for this machine first. Everything runs in
throwaway containers on a network of their own, laid out like the
Supervisor's (172.30.32.0/23):

- the app's image with made-up data (scripts/page/serve.py, demo_data.py):
  the page of the image runs as in the app, as user cgweb, with its own
  security headers; the host name gives it the slug 1a2b3c4d_crash_guard;
- a stand-in for the ingress at 172.30.32.2, the one address the page
  answers (scripts/page/ingress.py): the page under the path prefix
  /api/hassio_ingress/<token>/, and at / inside an iframe as in Home Assistant.

Headless Chromium opens it and checks: the header (name, stamp, refresh,
settings link with target _top and the right slug; back arrow on a subpage),
the version line, no console errors and no CSP violations, light, dark and
phone width, in English and German, and that refresh, the check button,
an evidence file and the back arrow work through the ingress. Nothing comes
from a real host, and nothing is sent to one.

Uses the installed Google Chrome when there is one, else Playwright's
Chromium (`python -m playwright install chromium` once).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts/page"))

import demo_data  # noqa: E402
import ingress  # noqa: E402

OUT = ROOT / "docs/screenshots"
VERSION = re.search(r'^version: "?([^"\n]+)"?$', (ROOT / "crash_guard/config.yaml").read_text(), re.M)[1]
NAME = "Crash Guard"
HOST = "1a2b3c4d-crash-guard"          # the Supervisor names the container's host after the slug
SLUG = HOST.replace("-", "_")
SUBNET = "172.30.32.0/23"
PEER = "172.30.32.2"
APP_IP = "172.30.33.2"
TZ = "Europe/Berlin"
TEXT = {
    "en": {"as_of": "As of", "refresh": "Refresh", "settings": "Settings", "back": "Back"},
    "de": {"as_of": "Stand", "refresh": "Aktualisieren", "settings": "Einstellungen", "back": "Zurück"},
}
SHOTS = (  # page, file, colour scheme, width, from this element to the end
    ("", "overview.png", "light", 1040, None),
    ("", "cases.png", "light", 1040, ".card:has(tr.link)"),
    (f"?case={demo_data.CRASH}", "crash.png", "light", 1040, None),
    (f"?case={demo_data.CHECK}", "check.png", "light", 1040, None),
    ("", "overview-dark.png", "dark", 1040, None),
    ("", "overview-mobile.png", "light", 390, None),
)
# Collects what the Content Security Policy blocked; Playwright adds it beside the page's own CSP
CSP_WATCH = """document.addEventListener("securitypolicyviolation", (e) => {
  (window.__csp = window.__csp || []).push(e.violatedDirective + " " + e.blockedURI);
});"""

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"    {'ok' if ok else 'FAILED'}: {what}")
    if not ok:
        failures.append(what)


def docker(*args: str, quiet: bool = True) -> str:
    return subprocess.run(["docker", *args], check=True, text=True,
                          stdout=subprocess.PIPE if quiet else None).stdout or ""


class Containers:
    """The app and the ingress stand-in on a network like the Supervisor's."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.name = f"crash-guard-page-{int(time.time())}"
        self.port = 0

    def __enter__(self) -> Containers:
        page_dir = f"{ROOT / 'scripts/page'}:/page:ro"
        # Another run (of this or another app) may hold the subnet for a moment
        for _ in range(60):
            made = subprocess.run(["docker", "network", "create", "--subnet", SUBNET, self.name],
                                  capture_output=True, text=True)
            if made.returncode == 0:
                break
            print(f"    waiting for {SUBNET}: {made.stderr.strip()}")
            time.sleep(5)
        else:
            raise SystemExit(f"no network {SUBNET}: {made.stderr.strip()}")
        try:
            docker("run", "-d", "--rm", "--name", f"{self.name}-ingress", "--network", self.name,
                   "--ip", PEER, "-p", f"127.0.0.1::{ingress.PORT}", "-v", page_dir,
                   "--entrypoint", "python3", self.image, "/page/ingress.py")
            docker("run", "-d", "--rm", "--name", f"{self.name}-app", "--network", self.name,
                   "--ip", APP_IP, "--network-alias", "app", "--hostname", HOST, "--tmpfs", "/data", "-e", f"TZ={TZ}",
                   "-v", page_dir, "--entrypoint", "python3", self.image, "/page/serve.py")
            self.port = int(docker("port", f"{self.name}-ingress", str(ingress.PORT)).split(":")[-1])
            self._wait()
        except BaseException:
            self.__exit__()
            raise
        return self

    def _wait(self) -> None:
        for _ in range(60):
            try:
                with urllib.request.urlopen(self.url(), timeout=2) as answer:
                    if answer.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        print(docker("logs", f"{self.name}-app"))
        raise SystemExit("the page did not come up")

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{ingress.PREFIX}/{path}"

    def outer(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __exit__(self, *_) -> None:
        for suffix in ("app", "ingress"):
            subprocess.run(["docker", "rm", "-f", f"{self.name}-{suffix}"], capture_output=True)
        subprocess.run(["docker", "network", "rm", self.name], capture_output=True)


def watch(page: Page) -> list[str]:
    """Console errors, uncaught errors and dialogs of the page, as they come."""
    problems: list[str] = []
    page.add_init_script(CSP_WATCH)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"error: {e}"))

    def dialog(d) -> None:
        problems.append(f"dialog: {d.message}")
        d.dismiss()

    page.on("dialog", dialog)
    return problems


def csp(page: Page) -> list[str]:
    return [v for frame in page.frames for v in (frame.evaluate("window.__csp || []") or [])]


def header(page: Page, lang: str, sub: bool, phone: bool, where: str) -> None:
    t = TEXT[lang]
    h = page.evaluate("""() => {
      const header = document.querySelector("body > header");
      if (!header) return null;
      const kids = [...header.children];
      const actions = header.querySelector(":scope > .actions");
      return {
        height: header.getBoundingClientRect().height,
        tags: kids.map((k) => k.tagName.toLowerCase() + (k.className ? "." + k.className.split(" ").join(".") : "")),
        h1: header.querySelector(":scope > h1")?.textContent,
        back: kids[0].matches("a.icon-button") ? {href: kids[0].getAttribute("href"), title: kids[0].title} : null,
        actions: actions ? [...actions.children].map((a) => ({
          tag: a.tagName.toLowerCase(), cls: a.className, id: a.id, text: a.textContent.trim(),
          href: a.getAttribute("href"), target: a.getAttribute("target"), role: a.getAttribute("role"),
          title: a.getAttribute("title"), shown: getComputedStyle(a).display !== "none",
          icon: !!a.querySelector("svg path"),
        })) : [],
      };
    }""")
    if h is None:
        check(False, f"{where}: a header")
        return
    check(abs(h["height"] - 56) < 1, f"{where}: header 56 px high ({h['height']})")
    if sub:
        check(h["tags"][:2] == ["a.icon-button", "h1"] and h["back"] == {"href": "?", "title": t["back"]},
              f"{where}: back arrow before the h1 ({h['tags']}, {h['back']})")
    else:
        check(h["tags"][0] == "h1", f"{where}: h1 first in the header ({h['tags']})")
    check((h["h1"] or "").startswith(NAME), f"{where}: h1 names the app ({h['h1']!r})")
    check(h["tags"][-1] == "div.actions", f"{where}: .actions last in the header")
    a = h["actions"]
    check(len(a) == 3, f"{where}: exactly stamp, refresh, settings in .actions ({[x['tag'] for x in a]})")
    if len(a) != 3:
        return
    stamp, refresh, settings = a
    check(stamp["tag"] == "span" and stamp["cls"] == "stamp opt"
          and re.fullmatch(rf"{t['as_of']} \d\d:\d\d:\d\d", stamp["text"]) is not None,
          f"{where}: stamp ({stamp['text']!r})")
    check(stamp["shown"] != phone, f"{where}: stamp {'hidden' if phone else 'shown'} at this width")
    check(refresh["tag"] == "a" and refresh["cls"] == "icon-button" and refresh["id"] == "refresh"
          and refresh["href"] == "" and refresh["role"] == "button" and refresh["title"] == t["refresh"]
          and refresh["icon"], f"{where}: refresh button")
    check(settings["tag"] == "a" and settings["cls"] == "icon-button" and settings["target"] == "_top"
          and settings["href"] == f"/config/app/{SLUG}/config" and settings["title"] == t["settings"]
          and settings["icon"], f"{where}: settings link ({settings['href']}, target {settings['target']})")


def look(page: Page, where: str) -> None:
    footer = page.evaluate("""() => { const f = document.body.lastElementChild;
      return f && f.matches("footer.version") ? f.textContent : null; }""")
    check(footer == f"{NAME} {VERSION}", f"{where}: version line last in body ({footer!r})")
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(wide <= 0, f"{where}: no sideways scrolling ({wide} px too wide)")


def run_checks(c: Containers, browser, shots: Path | None) -> None:
    pages = [("overview", ""), ("crash case", f"?case={demo_data.CRASH}"), ("check case", f"?case={demo_data.CHECK}")]
    backgrounds: dict[str, str] = {}
    for lang in ("en", "de"):
        for scheme, width in (("light", 1040), ("dark", 1040), ("light", 390)):
            phone = width < 600
            context = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=scheme,
                                          locale="de-DE" if lang == "de" else "en-US",
                                          extra_http_headers={"Accept-Language": lang},
                                          device_scale_factor=2 if phone else 1)
            page = context.new_page()
            problems = watch(page)
            for label, query in pages:
                where = f"{label}, {lang}, {scheme}, {width} px"
                print(f"==> {where}")
                answer = page.goto(c.url(query))
                check(answer is not None and answer.status == 200, f"{where}: status 200")
                header(page, lang, bool(query), phone, where)
                look(page, where)
                if not query and not phone:
                    backgrounds[scheme] = page.evaluate("getComputedStyle(document.body).backgroundColor")
            check(not problems and not csp(page), f"{lang}, {scheme}, {width} px: no console errors, "
                  f"no CSP violations ({problems + csp(page)})")
            context.close()
    check(backgrounds.get("light") != backgrounds.get("dark"),
          f"dark mode changes the colours ({backgrounds})")

    # Pictures before the check button changes the overview
    if shots:
        for query, file, scheme, width, selector in SHOTS:
            page = browser.new_page(viewport={"width": width, "height": 800}, color_scheme=scheme,
                                    extra_http_headers={"Accept-Language": "en"},
                                    device_scale_factor=2 if width < 600 else 1)
            page.goto(c.url(query))
            if selector:
                top = page.locator(selector).bounding_box()["y"] - 16
                height = page.evaluate("document.documentElement.scrollHeight") - top
                page.screenshot(path=str(shots / file), full_page=True,
                                clip={"x": 0, "y": top, "width": width, "height": height})
            else:
                page.screenshot(path=str(shots / file), full_page=True)
            page.close()
            print(f"    docs/screenshots/{file}")

    print("==> in Home Assistant's frame, through the ingress")
    page = browser.new_page(viewport={"width": 1040, "height": 800}, extra_http_headers={"Accept-Language": "en"})
    problems = watch(page)
    page.goto(c.outer())
    frame = page.frame_locator("iframe")
    frame.locator("body > header h1").wait_for()
    check(frame.locator("body > header h1").text_content() == NAME, "frame: the page shows in the iframe")
    app = next(f for f in page.frames if f.url.startswith(c.url()))
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        frame.locator("#refresh").click()
    frame.locator("body > header h1").wait_for()
    check(app.url == c.url(), f"frame: refresh reloads the same page ({app.url})")
    button = frame.locator("#check-now")
    check(button.is_enabled(), "frame: check button ready")
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        button.click()
    frame.locator("#check-now").wait_for()
    check(frame.locator("#check-now").is_disabled(), "frame: check requested through the ingress (button now waits)")
    frame.locator("tr[data-href]").first.click()
    frame.locator("body > header > a.icon-button").wait_for()
    check("case=" in app.url, f"frame: a case opens ({app.url})")
    details = frame.locator("details[data-src]").first
    details.locator("summary").click()
    pre = details.locator("pre")
    page.wait_for_function("(el) => el.textContent.length > 0", arg=pre.element_handle())
    check("Segmentation fault" in (pre.text_content() or ""), "frame: an evidence file loads through the ingress")
    frame.locator("body > header > a.icon-button").click()
    frame.locator("#check-now").wait_for()
    check(app.url == c.url("?"), f"frame: the back arrow leads to the start page ({app.url})")
    check(not problems and not csp(page), f"frame: no console errors, no CSP violations ({problems + csp(page)})")
    page.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="check only, write no pictures")
    parser.add_argument("--image", help="the built image (else it is built here)")
    args = parser.parse_args()
    image = args.image
    if not image:
        image = "crash-guard:page"
        docker("build", "-q", "--build-arg", f"BUILD_VERSION={VERSION}", "-t", image, str(ROOT / "crash_guard"))
    shots = None if args.check else OUT
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    chrome = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
    with Containers(image) as c, sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome" if chrome else None)
        try:
            run_checks(c, browser, shots)
        finally:
            browser.close()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for what in failures:
            print(f"  - {what}")
        raise SystemExit(1)
    print("Page: all checks passed")


if __name__ == "__main__":
    main()
