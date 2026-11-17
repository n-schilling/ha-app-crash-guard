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

Headless Chromium opens it and checks: the header as Home Assistant's own
pages draw it (STANDARD.md §7, hass-tabs-subpage): the menu button first
(shown only when narrow, below 870 px), the h1 with the name of config.yaml,
hidden beside the tabs like the icon, the tabs centred in the header's row
with a line under the active one (narrow: a bar at the bottom, icon over
label), then stamp (hidden when narrow), refresh and the settings link with
target _top and the right slug; on a subpage the back arrow and its title,
no menu, no tabs. Home Assistant's part of the frame (the stand-in plays it,
share/ha-app/ha-frame.js the page's): no kiosk mode on a wide screen; on a
phone (390 x 844, with a notch of 47 px and a home bar of 34 px, and without)
kiosk mode, so no bar of Home Assistant above the app, the insets handled by
the page (the header from the top, 56 px plus the notch, the bar at the
bottom with the home bar's padding, every tab on the screen), the menu button
opening Home Assistant's sidebar, wider and back switching kiosk mode off and
on with one message each way. Further: the version line, no console errors and no CSP violations, light, dark and
phone width, in English and German, that refresh, the check button,
an evidence file and the back arrow work through the ingress, and that the
page takes Home Assistant's language (a stand-in for its frontend in German,
English and French around the frame) over the browser's, reloading once.
Nothing comes from a real host, and nothing is sent to one.

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

from playwright.sync_api import Frame, Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts/page"))

import demo_data  # noqa: E402
import ingress  # noqa: E402

OUT = ROOT / "docs/screenshots"
VERSION = re.search(r'^version: "?([^"\n]+)"?$', (ROOT / "crash_guard/config.yaml").read_text(), re.M)[1]
# The name the app store shows; the header must show exactly this
NAME = re.search(r'^name: "?([^"\n]+)"?$', (ROOT / "crash_guard/config.yaml").read_text(), re.M)[1]
HOST = "1a2b3c4d-crash-guard"          # the Supervisor names the container's host after the slug
SLUG = HOST.replace("-", "_")
SUBNET = "172.30.32.0/23"
PEER = "172.30.32.2"
APP_IP = "172.30.33.2"
TZ = "Europe/Berlin"
TEXT = {
    "en": {"as_of": "As of", "refresh": "Refresh", "settings": "Settings", "back": "Back", "menu": "Menu",
           "tabs": ["Overview", "Memory lock", "Crashes", "Cases", "Log"]},
    "de": {"as_of": "Stand", "refresh": "Aktualisieren", "settings": "Einstellungen", "back": "Zurück", "menu": "Menü",
           "tabs": ["Übersicht", "Speichersperre", "Abstürze", "Fälle", "Protokoll"]},
}
# The views (tabs in the header) and where each one is
TABS = (("overview", "?tab=overview"), ("lock", "?tab=lock"), ("crashes", "?tab=crashes"), ("cases", "?tab=cases"), ("log", "?tab=log"))
SHOTS = (  # page, file, colour scheme, width, from this element to the end
    ("", "overview.png", "light", 1040, None),
    ("?tab=lock", "lock.png", "light", 1040, None),
    ("?tab=crashes", "crashes.png", "light", 1040, None),
    ("?tab=cases", "cases.png", "light", 1040, None),
    ("?tab=log", "log.png", "light", 1040, None),
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


def header(page: Page | Frame, lang: str, sub: bool, phone: bool, where: str, tab: str = "overview",
           safe: tuple[int, int] = (0, 0)) -> None:
    """The header as Home Assistant's own pages draw it (hass-tabs-subpage, STANDARD.md §7): phone is
    Home Assistant's narrow (below 870 px), safe the insets top and bottom the page keeps clear of."""
    t = TEXT[lang]
    h = page.evaluate("""() => {
      const header = document.querySelector("body > header");
      if (!header) return null;
      const kids = [...header.children];
      const actions = header.querySelector(":scope > .actions");
      const box = header.getBoundingClientRect(), shown = (e) => !!e && getComputedStyle(e).display !== "none";
      const inside = (e) => { const r = e.getBoundingClientRect();
        return r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5; };
      const m = header.querySelector(":scope > .menu"), h1 = header.querySelector(":scope > h1");
      const back = header.querySelector(":scope > .back");
      return {
        top: box.top, height: box.height, narrow: document.documentElement.classList.contains("narrow"),
        tags: kids.map((k) => k.tagName.toLowerCase() + (k.className ? "." + k.className.split(" ").join(".") : "")),
        h1: h1?.textContent, h1Hidden: !!h1 && h1.getBoundingClientRect().width <= 1 && h1.getBoundingClientRect().height <= 1,
        brand: [...header.querySelectorAll("img")].some(shown),
        menu: m && {first: kids[0] === m, ha: m.hasAttribute("data-ha-menu"), role: m.getAttribute("role"),
                    title: m.title, label: m.getAttribute("aria-label"), icon: !!m.querySelector("svg path"), shown: shown(m)},
        back: back && {first: kids[0] === back, href: back.getAttribute("href"), title: back.title, icon: !!back.querySelector("svg path"),
                       shown: shown(back)},
        // The views: centred in the header's row (narrow: a bar at the bottom of the window)
        tabs: (() => {
          const nav = header.querySelector(":scope > nav.tabs");
          if (!nav) return null;
          if ([...nav.children].some((a) => !a.matches("a.tab"))) return {kids: "not all a.tab"};
          const tabs = [...nav.children], first = tabs[0].getBoundingClientRect(), last = tabs[tabs.length - 1].getBoundingClientRect();
          const r = nav.getBoundingClientRect(), right = actions.getBoundingClientRect();
          const pad = getComputedStyle(nav).paddingBottom;
          return {labels: tabs.map((a) => a.textContent), hrefs: tabs.map((a) => a.getAttribute("href")),
                  active: [...nav.querySelectorAll("a.active[aria-current=page]")].map((a) => a.getAttribute("href")),
                  icons: tabs.every((a) => shown(a.querySelector("svg"))),
                  fixed: getComputedStyle(nav).position === "fixed",
                  row: tabs.every(inside),
                  centred: Math.abs((first.left + last.right) / 2 - (r.left + r.right) / 2) < 1 && r.right <= right.left + 0.5,
                  bottom: r.bottom - innerHeight, pad,
                  onScreen: tabs.every((a) => { const q = a.getBoundingClientRect();
                    return q.left >= 0 && q.right <= innerWidth && q.bottom <= innerHeight - parseFloat(pad) + 0.5; }),
                  line: tabs.map((a) => !["none", ""].includes(getComputedStyle(a, "::after").content))};
        })(),
        // The menu or back arrow and the buttons lie within the header's box (below the notch)
        outside: [m, back, ...(actions ? [...actions.children] : [])].filter(shown).filter((e) => !inside(e))
          .map((el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "")),
        // Nothing else fixed at the bottom of the window (the bottom bar is the tabs)
        bars: [...document.body.querySelectorAll("*")].filter((el) => {
          const st = getComputedStyle(el), r = el.getBoundingClientRect();
          return (st.position === "fixed" || st.position === "sticky") && r.height > 0
            && Math.abs(r.bottom - window.innerHeight) < 1 && !el.matches("header > nav.tabs");
        }).length,
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
    check(h["narrow"] == phone, f"{where}: html.narrow {phone}")
    check(abs(h["top"]) < 0.5 and abs(h["height"] - 56 - safe[0]) < 0.5,
          f"{where}: header from the top, 56 + {safe[0]} px high ({h['top']}, {h['height']})")
    check(h["outside"] == [], f"{where}: menu or back arrow and buttons within the header ({h['outside']} outside)")
    check(h["bars"] == 0, f"{where}: no other bar at the bottom ({h['bars']})")
    if sub:
        b = h["back"] or {}
        check(h["tags"] == ["a.icon-button.back", "h1", "div.actions"]
              and b == {"first": True, "href": "?tab=cases", "title": t["back"], "icon": True, "shown": True},
              f"{where}: back arrow to the cases, the h1, the actions ({h['tags']}, {h['back']})")
        check(h["tabs"] is None and h["menu"] is None, f"{where}: no tabs and no menu button on a subpage")
        # A subpage keeps its own title
        check(h["h1"] == f"{NAME} · {t['tabs'][3]}" and not h["h1Hidden"], f"{where}: h1 is the subpage's title ({h['h1']!r})")
    else:
        check(h["tags"] == ["a.icon-button.menu", "h1.name", "nav.tabs", "div.actions"],
              f"{where}: the menu button, h1, the tabs and the actions ({h['tags']})")
        check(h["h1"] == NAME, f"{where}: h1 is the name of config.yaml ({h['h1']!r} vs {NAME!r})")
        check(h["h1Hidden"] and not h["brand"], f"{where}: with tabs no icon and no visible name")
        m = h["menu"] or {}
        check(m.get("first") and m.get("ha") and m.get("role") == "button" and m.get("title") == t["menu"] == m.get("label")
              and m.get("icon") and m.get("shown") == phone, f"{where}: menu button first, {'shown' if phone else 'hidden'} ({m})")
        tabs = h["tabs"] or {}
        check(tabs.get("labels") == t["tabs"] and tabs.get("hrefs") == [href for _, href in TABS],
              f"{where}: the tabs ({tabs.get('labels')}, {tabs.get('hrefs')})")
        check(tabs.get("active") == [dict(TABS)[tab]], f"{where}: {tab} is the active tab ({tabs.get('active')})")
        check(bool(tabs.get("icons")), f"{where}: every tab with its icon")
        if phone:
            check(tabs.get("fixed") and abs(tabs.get("bottom", 1)) < 0.5 and tabs.get("pad") == f"{safe[1]}px"
                  and tabs.get("onScreen") and not any(tabs.get("line", [])),
                  f"{where}: tabs in a bar at the bottom, {safe[1]} px clear of the home bar, all on the screen ({tabs})")
        else:
            check(not tabs.get("fixed") and tabs.get("row") and tabs.get("centred")
                  and tabs.get("line") == [href == dict(TABS)[tab] for _, href in TABS],
                  f"{where}: tabs centred in the header's row, a line under the active one, no bottom bar ({tabs})")
    check(h["tags"][-1] == "div.actions", f"{where}: .actions last in the header")
    a = h["actions"]
    check(len(a) == 3, f"{where}: exactly stamp, refresh, settings in .actions ({[x['tag'] for x in a]})")
    if len(a) != 3:
        return
    stamp, refresh, settings = a
    check(stamp["tag"] == "span" and stamp["cls"] == "stamp opt"
          and re.fullmatch(rf"{t['as_of']} \d\d:\d\d:\d\d", stamp["text"]) is not None,
          f"{where}: stamp ({stamp['text']!r})")
    check(stamp["shown"] != phone, f"{where}: stamp {'hidden' if phone else 'shown'}")
    check(refresh["tag"] == "a" and refresh["cls"] == "icon-button" and refresh["id"] == "refresh"
          and refresh["href"] == "" and refresh["role"] == "button" and refresh["title"] == t["refresh"]
          and refresh["icon"], f"{where}: refresh button")
    check(settings["tag"] == "a" and settings["cls"] == "icon-button" and settings["target"] == "_top"
          and settings["href"] == f"/config/app/{SLUG}/config" and settings["title"] == t["settings"]
          and settings["icon"], f"{where}: settings link ({settings['href']}, target {settings['target']})")


def in_frame(page: Page, wide: bool, safe: tuple[int, int]) -> Frame:
    """The app's frame in the stand-in for Home Assistant, once the page has subscribed."""
    page.frame_locator("#ingress").locator("body > header h1").wait_for(state="attached")
    page.wait_for_function("window.__safe !== null")
    page.wait_for_timeout(500)  # room for the answer, and for any further message, which must not loop
    app = next(f for f in page.frames if f.parent_frame is not None)
    app.wait_for_selector("html:not(.narrow)" if wide else "html.narrow", state="attached")
    if safe != (0, 0):
        app.wait_for_function("getComputedStyle(document.documentElement)"
                              f".getPropertyValue('--safe-area-inset-top').trim() === '{safe[0]}px'")
    return app


def frame_checks(page: Page, app: Frame, lang: str, wide: bool, safe: tuple[int, int], where: str) -> None:
    """What the page asked of Home Assistant (the stand-in records it) and where its frame sits."""
    seen = page.evaluate("""() => ({kiosk: window.__kiosk, safe: window.__safe, menu: window.__menu,
      messages: window.__messages.map((m) => m.type + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : "")),
      bar: getComputedStyle(document.getElementById("ha-bar")).display !== "none",
      frame: (() => { const r = document.getElementById("ingress").getBoundingClientRect(); return [r.top, r.bottom - innerHeight]; })()})""")
    check(seen["kiosk"] is (not wide), f"{where}: kiosk mode {'not ' if wide else ''}requested ({seen['messages']})")
    check(seen["safe"] is True, f"{where}: the page keeps clear of the notch itself (handleSafeArea)")
    check(not seen["bar"], f"{where}: no bar of Home Assistant above the app")
    check(seen["frame"] == [0, 0], f"{where}: the frame fills the window ({seen['frame']})")
    check(len(seen["messages"]) <= 2, f"{where}: no loop of messages ({seen['messages']})")
    header(app, lang, False, not wide, where, "overview", safe)
    check(app.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{where}: no sideways scrolling")


def ha_frame(browser, c: Containers) -> None:
    """Home Assistant's part of the frame (scripts/page/ingress.py plays it): kiosk mode only
    while narrow, the notch and the home bar by Home Assistant's insets, the menu button."""
    for width, height, safe, lang in ((1200, 760, (0, 0), "en"), (390, 844, (47, 34), "de"), (390, 844, (0, 0), "en")):
        wide = width > 870
        where = f"frame, {width} px" + (f", notch {safe[0]} px, home bar {safe[1]} px" if safe != (0, 0) else "")
        print(f"==> {where}")
        context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark",
                                      locale="de-DE" if lang == "de" else "en-US",
                                      extra_http_headers={"Accept-Language": lang})
        page = context.new_page()
        problems = watch(page)
        page.goto(c.outer() + ("?safe=" + ",".join(map(str, safe)) if safe != (0, 0) else ""))
        app = in_frame(page, wide, safe)
        frame_checks(page, app, lang, wide, safe, where)
        if not wide:
            app.locator("header .menu").click()
            page.wait_for_timeout(300)  # the message reaches Home Assistant after the click
            check(page.evaluate("window.__menu") == 1, f"{where}: the menu button opens Home Assistant's sidebar")
            if safe != (0, 0):
                # Wider than narrow and back: kiosk mode off (Home Assistant's bar back), then on again
                page.set_viewport_size({"width": 1200, "height": height})
                app.wait_for_selector("html:not(.narrow)", state="attached")
                page.wait_for_timeout(300)
                check(page.evaluate("window.__kiosk") is False, f"{where}: wide again, kiosk mode off")
                page.set_viewport_size({"width": width, "height": height})
                app.wait_for_selector("html.narrow", state="attached")
                page.wait_for_timeout(300)
                sent = page.evaluate("""window.__messages.filter((m) => m.type.endsWith("-properties"))
                    .map((m) => m.type.split("/")[1] + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : ""))""")
                check(page.evaluate("window.__kiosk") is True and sent == [
                    "subscribe-properties:true", "unsubscribe-properties", "subscribe-properties:false",
                    "subscribe-properties:true"], f"{where}: narrow again, kiosk mode on, no more messages ({sent})")
                header(app, lang, False, True, where, "overview", safe)
            # A case on the phone: the back arrow below the notch, no menu, no bottom bar
            with page.expect_event("framenavigated", predicate=lambda f, app=app: f is app):
                app.evaluate(f"setTimeout(() => {{ location.href = '?case={demo_data.CRASH}'; }}, 0)")
            app.locator("body > header > a.back").wait_for()
            header(app, lang, True, True, f"{where}, a case", safe=safe)
            check(app.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{where}, a case: no sideways scrolling")
        check(not problems and not csp(page), f"{where}: no console errors, no CSP violations ({problems + csp(page)})")
        context.close()


def look(page: Page, where: str) -> None:
    footer = page.evaluate("""() => { const f = document.body.lastElementChild;
      return f && f.matches("footer.version") ? f.textContent : null; }""")
    check(footer == f"{NAME} {VERSION}", f"{where}: version line last in body ({footer!r})")
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(wide <= 0, f"{where}: no sideways scrolling ({wide} px too wide)")


def run_checks(c: Containers, browser, shots: Path | None) -> None:
    # (label, query, active tab); without tab (and with a bare ?) the first view
    pages = [("no tab", "", "overview"), ("bare ?", "?", "overview"), *((tab, href, tab) for tab, href in TABS),
             ("crash case", f"?case={demo_data.CRASH}", ""), ("check case", f"?case={demo_data.CHECK}", "")]
    backgrounds: dict[str, str] = {}
    for lang in ("en", "de"):
        for scheme, width in (("light", 1040), ("dark", 1040), ("light", 390)):
            phone = width <= 870  # Home Assistant's narrow
            context = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=scheme,
                                          locale="de-DE" if lang == "de" else "en-US",
                                          extra_http_headers={"Accept-Language": lang},
                                          device_scale_factor=2 if phone else 1)
            page = context.new_page()
            problems = watch(page)
            for label, query, tab in pages:
                where = f"{label}, {lang}, {scheme}, {width} px"
                print(f"==> {where}")
                answer = page.goto(c.url(query))
                check(answer is not None and answer.status == 200, f"{where}: status 200")
                header(page, lang, "case=" in query, phone, where, tab)
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

    ha_frame(browser, c)

    print("==> in Home Assistant's frame, through the ingress")
    page = browser.new_page(viewport={"width": 1040, "height": 800}, extra_http_headers={"Accept-Language": "en"})
    problems = watch(page)
    page.goto(c.outer())
    frame = page.frame_locator("#ingress")
    frame.locator("body > header h1").wait_for(state="attached")
    check(frame.locator("body > header h1").text_content() == NAME, "frame: the page shows in the iframe")
    app = next(f for f in page.frames if f.url.startswith(c.url()))
    before = frame.locator("body > header .stamp").text_content()
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        frame.locator("#refresh").click()
    frame.locator("body > header h1").wait_for(state="attached")
    check(app.url == c.url(), f"frame: refresh reloads the same page ({app.url})")
    after = frame.locator("body > header .stamp").text_content()
    check(after != before, f"frame: refresh moves the stamp ({before!r} -> {after!r})")
    button = frame.locator("#check-now")
    check(button.is_enabled(), "frame: check button ready")
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        button.click()
    frame.locator("#check-now").wait_for()
    check(frame.locator("#check-now").is_disabled(), "frame: check requested through the ingress (button now waits)")
    frame.locator("tr[data-href]").first.click()
    frame.locator("body > header > a.back").wait_for()
    check("case=" in app.url, f"frame: a case opens ({app.url})")
    details = frame.locator("details[data-src]").first
    details.locator("summary").click()
    pre = details.locator("pre")
    page.wait_for_function("(el) => el.textContent.length > 0", arg=pre.element_handle())
    check("Segmentation fault" in (pre.text_content() or ""), "frame: an evidence file loads through the ingress")
    frame.locator("body > header > a.back").click()
    frame.locator("body > header > nav.tabs").wait_for()
    check(app.url == c.url("?tab=cases"), f"frame: the back arrow leads to the cases ({app.url})")
    check(frame.locator("nav.tabs > a.tab.active").text_content() == "Cases", "frame: the Cases tab is active")
    # Tabs are links: the view is in the URL, so reload and back keep it
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        frame.locator("nav.tabs > a.tab", has_text="Log").click()
    frame.locator("nav.tabs > a.tab.active", has_text="Log").wait_for()
    check(app.url == c.url("?tab=log"), f"frame: the Log tab opens ?tab=log ({app.url})")
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        frame.locator("#refresh").click()
    frame.locator("nav.tabs > a.tab.active", has_text="Log").wait_for()
    check(app.url == c.url("?tab=log"), f"frame: refresh stays on the tab ({app.url})")
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        app.evaluate("setTimeout(() => history.back(), 0)")  # the browser's back, inside the frame
    frame.locator("nav.tabs > a.tab.active", has_text="Cases").wait_for()
    check(app.url == c.url("?tab=cases"), f"frame: back returns to the previous tab ({app.url})")
    with page.expect_event("framenavigated", predicate=lambda f: f is app):
        frame.locator("nav.tabs > a.tab", has_text="Overview").click()
    frame.locator("#check-now").wait_for()
    check(frame.locator("a.state[href='?tab=lock']").count() == 1, "frame: the status lines lead to their views")
    check(not problems and not csp(page), f"frame: no console errors, no CSP violations ({problems + csp(page)})")
    page.close()

    # The browser asks for English; the page follows Home Assistant's language
    # (share/ha-app/ha-lang.js): one reload when it changes, none when not
    print("==> in Home Assistant's language, not the browser's")
    context = browser.new_context(viewport={"width": 1040, "height": 800}, locale="en-US",
                                  extra_http_headers={"Accept-Language": "en"})
    page = context.new_page()
    problems = watch(page)
    for ha_lang, lang, expected in (("de", "de", 2), ("de", "de", 1), ("en-GB", "en", 2), ("fr", "en", 2),
                                    ("fr", "en", 1)):
        where = f"Home Assistant in {ha_lang}"
        app, loads = in_home_assistant(page, c, ha_lang)
        shown = app.evaluate("[document.documentElement.lang, getComputedStyle(document.documentElement).visibility]")
        check(shown == [lang, "visible"], f"{where}: page in {lang} and shown ({shown})")
        check(loads == expected, f"{where}: loaded {expected} time(s) ({loads})")
        header(app, lang, False, False, where)
        held = [(k["value"], k["path"]) for k in context.cookies() if k["name"] == "ha_lang"]
        check(held == [(ha_lang.split("-")[0], f"{ingress.PREFIX}/")], f"{where}: cookie for the ingress path ({held})")
    check(not problems and not csp(page), f"language: no console errors, no CSP violations ({problems + csp(page)})")
    context.close()


def in_home_assistant(page: Page, c: Containers, lang: str) -> tuple[Frame, int]:
    """Opens the page in a stand-in for Home Assistant's frontend in that language;
    the app's frame and how often it loaded."""
    loads: list[str] = []

    def seen(frame: Frame) -> None:
        if frame.parent_frame is not None and frame.url.startswith(c.url()):
            loads.append(frame.url)

    page.on("framenavigated", seen)
    page.goto(f"{c.outer()}?lang={lang}")
    page.wait_for_timeout(1500)  # room for another reload, which must not come
    page.remove_listener("framenavigated", seen)
    app = next(f for f in page.frames if f.parent_frame is not None)
    app.locator("body > header h1").wait_for(state="attached")
    return app, len(loads)


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
