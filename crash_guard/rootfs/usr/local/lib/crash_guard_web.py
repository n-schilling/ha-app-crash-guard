"""A read-only web page behind Home Assistant's ingress, laid out like Home
Assistant's settings: the state of the RAM lock, the last cache check, the
crashes, the known defective pages, the cases with their evidence and the
event log of the guard service.

The guard service writes everything this page shows into the app's private
data: STATUS (the state behind the three problem sensors), the logs and one
folder per case. The page reads those files and takes nothing from the URL but
a case name and a file name, both checked against fixed patterns; files are
opened without following symlinks. The one thing it changes: a POST asks for a
cache check now. The page then leaves REQUEST for the guard service, which
checks without dropping the cache. At most one request per MANUAL_GAP seconds,
and only with the X-Crash-Guard header, which a form of another site cannot
send.

The evidence of a crash (the Home Assistant container log, the fault log, the
kernel and coredump messages) can hold secrets, as Home Assistant's own log
can. The page is reachable only through the ingress, that is by Home
Assistant administrators, and loads a log only when it is opened.

It is rendered on the server and needs nothing beyond Python's standard
library. Its only script takes Home Assistant's theme from the page around
the ingress frame, loads an evidence file when it is opened and sends the
request for a check.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

LOGGER = logging.getLogger("crash_guard_web")

PORT = 8099              # the Supervisor's default ingress port
# The Supervisor's ingress proxy; nothing else may talk to the page
INGRESS_PEER = "172.30.32.2"
DATA = Path("/data/crash_guard")
STATUS = "status.json"
# A check requested on this page: REQUEST is left for the guard service, which
# removes it when it takes the request and keeps RUNNING while it checks;
# REQUESTED holds the time of the last request
REQUEST = "check.request"
RUNNING = "check.running"
REQUESTED = "check.requested"
MANUAL_GAP = 600         # seconds between two requested checks
CASE_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z]+$")
# Files of a case the page shows; nothing else is ever opened from a URL
EVIDENCE = ("container.log", "home-assistant.log.fault", "kernel.txt", "coredumps.txt")
LISTS = ("cache.mismatch", "fresh.mismatch", "ref-inconsistent.txt")
SAVED = ("cache_files", "fresh_files")
FILE_MAX = 200 * 1024    # bytes of a file shown, its end
LIST_MAX = 500           # lines of a mismatch list shown
EVENTS_SHOWN = 100
CHECKS_SHOWN = 20
LAYER = re.compile(r"^/mnt/data/docker/overlay2/[0-9a-f]{64}(-init)?/diff")
# A saved copy is named after the file's path below overlay2, with _ for /
SAVED_LAYER = re.compile(r"^[0-9a-f]{64}(-init)?_diff_")

TEXT = {
    "en": {
        "title": "Crash Guard", "settings": "Settings", "refresh": "Refresh", "as_of": "As of {}",
        "back": "Back", "status": "Status", "loading": "The guard service has not reported yet.",
        "lock": "Memory lock", "lock_on": "In effect: {}", "lock_ready": "Prepared, takes effect after a host reboot: {}",
        "lock_off": "Not in place: {}", "lock_error": "Check failed: {}", "outside": "Defective page outside the lock: {}",
        "no_pages": "No defective pages configured, no memory lock.",
        "cache": "Page cache", "cache_ok": "No corrupted files at the last check", "cache_bad": "{} corrupted file(s) at the last check",
        "cache_none": "No check yet", "checked": "Last check", "next_checks": "Check times", "drop": "Drop the cache at the check",
        "crashes": "Crashes", "crashes_ok": "No crash within {}", "crashes_bad": "Home Assistant {}, programs {} within {}",
        "since_boot": "Program crashes since the host booted", "window": "Window",
        "ha_crashes": "Home Assistant", "program_crashes": "Programs on the host", "process": "Process", "signal": "Signal",
        "time": "Time", "message": "Message", "no_crashes": "No crash within the window.",
    "boot_slot": "Boot slot", "overlay_slot": "Overlay in the boot slot",
        "overlay_other": "Overlay in the other slot", "config_line": "Line in config.txt", "managed": "Managed by the app",
        "lock_checked": "Checked", "yes": "yes", "no": "no",
        "pages": "Defective pages", "pfn": "Page", "occasion": "Found at", "bits": "Bits", "file": "File",
        "no_bad": "No defective page found yet.", "configured": "In bad_pages",
        "cases": "Cases", "kind": "Kind", "no_cases": "No case yet.", "kept": "The newest {} cases are kept.",
        "kinds": {"check": "Cache check", "drop": "Cache drop", "crash": "Home Assistant crash", "ref": "Reference built"},
        "case_unknown": "This case does not exist (any more).", "exit": "Exit code", "error": "Error",
        "started": "Started", "finished": "Finished", "oom": "Out of memory",
        "mismatches": "Files that differed", "where": "Read", "expected": "Expected", "actual": "Read as",
        "in_cache": "from the cache", "fresh": "freshly", "inconsistent": "read inconsistently, excluded",
        "no_mismatch": "No file differed.", "saved": "Saved copies", "size": "Size",
        "evidence": "Evidence", "evidence_info": "Logs can hold secrets, as Home Assistant's own log can. They load when opened.",
        "no_evidence": "No logs in this case.", "load_failed": "Could not load the file.", "more_lines": "{} more lines not shown",
        "events": "Event log", "no_events": "No events yet.", "checks": "Cache checks", "cache_count": "In the cache",
        "fresh_count": "Freshly read", "unknown": "–",
        "days": "d", "hours": "h", "minutes": "min",
        "check_now": "Check now", "check_queued": "Check requested, starts within a few seconds",
        "check_running": "Check running", "check_again": "Possible again at {}",
        "check_failed": "The check could not be requested.",
    },
    "de": {
        "title": "Crash Guard", "settings": "Einstellungen", "refresh": "Aktualisieren", "as_of": "Stand {}",
        "back": "Zurück", "status": "Status", "loading": "Der Guard-Dienst hat sich noch nicht gemeldet.",
        "lock": "Speichersperre", "lock_on": "Greift: {}", "lock_ready": "Vorbereitet, greift nach einem Host-Neustart: {}",
        "lock_off": "Nicht eingerichtet: {}", "lock_error": "Prüfung fehlgeschlagen: {}",
        "outside": "Defekte Seite außerhalb der Sperre: {}", "no_pages": "Keine defekten Seiten eingetragen, keine Speichersperre.",
        "cache": "Page Cache", "cache_ok": "Keine verfälschten Dateien bei der letzten Prüfung",
        "cache_bad": "{} verfälschte Datei(en) bei der letzten Prüfung", "cache_none": "Noch keine Prüfung",
        "checked": "Letzte Prüfung", "next_checks": "Prüfzeiten", "drop": "Cache bei der Prüfung leeren",
        "crashes": "Abstürze", "crashes_ok": "Kein Absturz in {}", "crashes_bad": "Home Assistant {}, Programme {} in {}",
        "since_boot": "Programmabstürze seit dem Host-Start", "window": "Fenster",
        "ha_crashes": "Home Assistant", "program_crashes": "Programme auf dem Host", "process": "Prozess", "signal": "Signal",
        "time": "Zeit", "message": "Meldung", "no_crashes": "Kein Absturz im Fenster.",
"boot_slot": "Boot-Slot", "overlay_slot": "Overlay im Boot-Slot",
        "overlay_other": "Overlay im anderen Slot", "config_line": "Zeile in config.txt", "managed": "Von der App verwaltet",
        "lock_checked": "Geprüft", "yes": "ja", "no": "nein",
        "pages": "Defekte Seiten", "pfn": "Seite", "occasion": "Gefunden bei", "bits": "Bits", "file": "Datei",
        "no_bad": "Noch keine defekte Seite gefunden.", "configured": "In bad_pages",
        "cases": "Fälle", "kind": "Art", "no_cases": "Noch kein Fall.", "kept": "Die neuesten {} Fälle bleiben erhalten.",
        "kinds": {"check": "Cache-Prüfung", "drop": "Cache geleert", "crash": "Absturz von Home Assistant",
                  "ref": "Referenz gebaut"},
        "case_unknown": "Diesen Fall gibt es (nicht mehr).", "exit": "Exit-Code", "error": "Fehler",
        "started": "Gestartet", "finished": "Beendet", "oom": "Speicher voll",
        "mismatches": "Abweichende Dateien", "where": "Gelesen", "expected": "Erwartet", "actual": "Gelesen als",
        "in_cache": "aus dem Cache", "fresh": "frisch", "inconsistent": "uneinheitlich gelesen, ausgeschlossen",
        "no_mismatch": "Keine Datei wich ab.", "saved": "Gesicherte Kopien", "size": "Größe",
        "evidence": "Belege", "evidence_info": "Logs können Geheimnisse enthalten, wie das Protokoll von Home Assistant. Sie werden erst beim Öffnen geladen.",
        "no_evidence": "Keine Logs in diesem Fall.", "load_failed": "Datei konnte nicht geladen werden.",
        "more_lines": "{} weitere Zeilen nicht angezeigt",
        "events": "Ereignisprotokoll", "no_events": "Noch keine Ereignisse.", "checks": "Cache-Prüfungen",
        "cache_count": "Im Cache", "fresh_count": "Frisch gelesen", "unknown": "–",
        "days": "d", "hours": "h", "minutes": "min",
        "check_now": "Jetzt prüfen", "check_queued": "Prüfung angefordert, startet in wenigen Sekunden",
        "check_running": "Prüfung läuft", "check_again": "Wieder möglich um {}",
        "check_failed": "Die Prüfung konnte nicht angefordert werden.",
    },
}

# Material Design Icons (Apache 2.0), as Home Assistant draws them
ICONS = {
    "settings": "M12,8A4,4 0 0,1 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12A4,4 0 0,1 12,8M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10M10,22C9.75,22 9.54,21.82 9.5,21.58L9.13,18.93C8.5,18.68 7.96,18.34 7.44,17.94L4.95,18.95C4.73,19.03 4.46,18.95 4.34,18.73L2.34,15.27C2.21,15.05 2.27,14.78 2.46,14.63L4.57,12.97L4.5,12L4.57,11L2.46,9.37C2.27,9.22 2.21,8.95 2.34,8.73L4.34,5.27C4.46,5.05 4.73,4.96 4.95,5.05L7.44,6.05C7.96,5.66 8.5,5.32 9.13,5.07L9.5,2.42C9.54,2.18 9.75,2 10,2H14C14.25,2 14.46,2.18 14.5,2.42L14.87,5.07C15.5,5.32 16.04,5.66 16.56,6.05L19.05,5.05C19.27,4.96 19.54,5.05 19.66,5.27L21.66,8.73C21.79,8.95 21.73,9.22 21.54,9.37L19.43,11L19.5,12L19.43,13L21.54,14.63C21.73,14.78 21.79,15.05 21.66,15.27L19.66,18.73C19.54,18.95 19.27,19.04 19.05,18.95L16.56,17.95C16.04,18.34 15.5,18.68 14.87,18.93L14.5,21.58C14.46,21.82 14.25,22 14,22H10M11.25,4L10.88,6.61C9.68,6.86 8.62,7.5 7.85,8.39L5.44,7.35L4.69,8.65L6.8,10.2C6.4,11.37 6.4,12.64 6.8,13.8L4.68,15.36L5.43,16.66L7.86,15.62C8.63,16.5 9.68,17.14 10.87,17.38L11.24,20H12.76L13.13,17.39C14.32,17.14 15.37,16.5 16.14,15.62L18.57,16.66L19.32,15.36L17.2,13.81C17.6,12.64 17.6,11.37 17.2,10.2L19.31,8.65L18.56,7.35L16.15,8.39C15.38,7.5 14.32,6.86 13.12,6.62L12.75,4H11.25Z",
    "ok": "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z",
    "alert": "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "info": "M13,9H11V7H13M13,17H11V11H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "refresh": "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
    "back": "M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z",
    "chevron": "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
}


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="{ICONS[name]}"/></svg>'


# Home Assistant's defaults; the script replaces them with the theme in use
STYLE = """
:root { color-scheme: light dark; --primary-background-color: #fafafa; --secondary-background-color: #e5e5e5;
  --card-background-color: #fff; --primary-text-color: #212121; --secondary-text-color: #727272;
  --divider-color: rgba(0,0,0,.12); --primary-color: #03a9f4; --success-color: #4caf50;
  --warning-color: #ff9800; --error-color: #db4437; --disabled-text-color: #bdbdbd;
  --sidebar-background-color: #fff; --sidebar-text-color: #212121; --sidebar-icon-color: rgba(0,0,0,.5);
  --app-header-border-bottom: 1px solid var(--divider-color); --ha-card-border-radius: 12px;
  --ha-card-border-width: 1px; --ha-card-border-color: var(--divider-color); --ha-card-box-shadow: none;
  --ha-font-family-body: Roboto, Noto, sans-serif; --header-height: 56px; }
@media (prefers-color-scheme: dark) { :root { --primary-background-color: #111; --secondary-background-color: #282828;
  --card-background-color: #1c1c1c; --primary-text-color: #e1e1e1; --secondary-text-color: #9b9b9b;
  --divider-color: rgba(225,225,225,.12); --disabled-text-color: #6f6f6f; --sidebar-background-color: #1c1c1c;
  --sidebar-text-color: #e1e1e1; --sidebar-icon-color: rgba(255,255,255,.5); } }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--primary-background-color); color: var(--primary-text-color);
  font: 14px/1.6 var(--ha-font-family-body); -webkit-font-smoothing: antialiased; }
a { color: var(--primary-color); text-decoration: none; }
svg { width: 24px; height: 24px; fill: currentColor; flex: none; }
header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; height: var(--header-height);
  padding: 0 12px; background: var(--sidebar-background-color); color: var(--sidebar-text-color);
  border-bottom: var(--app-header-border-bottom); }
header h1 { margin: 0 0 0 12px; font-size: 20px; font-weight: 400; line-height: 32px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
header .actions { margin-left: auto; display: flex; align-items: center; }
header .stamp { margin-right: 8px; font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
header a.icon { display: flex; align-items: center; justify-content: center; width: 48px; height: 48px;
  border-radius: 50%; color: var(--sidebar-icon-color); }
header a.icon:hover, tbody tr.link:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
main { margin: 0 auto; padding: 16px; max-width: 1040px; }
.card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius);
  border: var(--ha-card-border-width) solid var(--ha-card-border-color); box-shadow: var(--ha-card-box-shadow);
  margin-bottom: 16px; overflow: hidden; }
.card h2 { margin: 0; padding: 24px 16px 16px; font-size: 24px; font-weight: 400; line-height: 32px; }
.card h3 { margin: 0; padding: 8px 16px; font-size: 14px; font-weight: 500; color: var(--secondary-text-color); }
.card .content { padding: 0 16px 16px; }
.card .content > p { margin: 0; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 0 16px; }
.state { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
.state b { font-weight: 500; }
.ok { color: var(--success-color); } .bad { color: var(--warning-color); } .muted { color: var(--secondary-text-color); }
.state .text { color: var(--primary-text-color); }
.check { display: flex; align-items: center; justify-content: flex-end; gap: 12px; flex-wrap: wrap; margin-top: 8px; }
.check .sub { margin: 0; }
button.text { font: inherit; font-size: 14px; font-weight: 500; color: var(--primary-color); background: none;
  border: 0; border-radius: 18px; padding: 0 16px; height: 36px; cursor: pointer; }
button.text:hover { background: color-mix(in srgb, var(--primary-color) 8%, transparent); }
button.text:disabled { color: var(--disabled-text-color); background: none; cursor: default; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 8px 16px 8px 0; vertical-align: top; }
td { border-top: 1px solid var(--divider-color); }
th { font-weight: 500; font-size: 12px; white-space: nowrap; }
table.kv th { width: 45%; font-size: 14px; font-weight: 400; color: var(--secondary-text-color);
  border-top: 1px solid var(--divider-color); white-space: normal; }
table.kv tr:first-child th, table.kv tr:first-child td { border-top: 0; }
.list th:first-child, .list td:first-child { padding-left: 16px; }
.list th { height: 48px; vertical-align: middle; border-bottom: 1px solid var(--divider-color); }
.list td { vertical-align: middle; font-size: 13px; }
.list tr:first-child td { border-top: 0; }
.list tr.link { cursor: pointer; }
.list td.go { width: 40px; color: var(--secondary-text-color); }
.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.nowrap { white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.sub { display: block; font-size: 12px; line-height: 16px; color: var(--secondary-text-color); }
.scroll { overflow-x: auto; }
.note { padding: 0 16px 16px; font-size: 12px; color: var(--secondary-text-color); }
details { border-top: 1px solid var(--divider-color); }
details summary { display: flex; align-items: center; gap: 8px; padding: 12px 16px; cursor: pointer; list-style: none; }
details summary::-webkit-details-marker { display: none; }
details summary svg { width: 20px; height: 20px; color: var(--secondary-text-color); transition: transform .2s; }
details[open] summary svg { transform: rotate(90deg); }
details pre { margin: 0; padding: 8px 16px 16px; max-height: 480px; overflow: auto; white-space: pre-wrap;
  word-break: break-all; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (max-width: 600px) { .opt { display: none; } }
"""

# Takes Home Assistant's theme from the page around the ingress frame (same
# origin) and follows it; loads an evidence file when it is opened
THEME_VARS = ("primary-background-color", "secondary-background-color", "card-background-color",
              "primary-text-color", "secondary-text-color", "divider-color", "primary-color", "success-color",
              "warning-color", "error-color", "disabled-text-color", "sidebar-background-color",
              "sidebar-text-color", "sidebar-icon-color", "app-header-border-bottom", "ha-card-border-radius",
              "ha-card-border-width", "ha-card-border-color", "ha-card-box-shadow", "ha-font-family-body",
              "header-height")
SCRIPT = """(() => {
  try {
    const outer = window.parent.document.documentElement;
    if (outer !== document.documentElement) {
      const take = () => {
        const theme = getComputedStyle(outer), own = document.documentElement.style;
        for (const n of NAMES) { const v = theme.getPropertyValue("--" + n).trim(); if (v) own.setProperty("--" + n, v); }
        const ha = window.parent.document.querySelector("home-assistant");
        if (ha && ha.hass) own.colorScheme = ha.hass.themes.darkMode ? "dark" : "light";
      };
      take();
      new MutationObserver(take).observe(outer, { attributes: true, attributeFilter: ["style", "class"] });
    }
  } catch (e) {}
  document.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-href]");
    if (row) location.href = row.dataset.href;
    if (e.target.closest("#refresh")) { e.preventDefault(); location.reload(); }
    const button = e.target.closest("#check-now");
    if (button && !button.disabled) {
      button.disabled = true;
      // The header keeps forms of other sites out; they cannot send it
      fetch("", { method: "POST", headers: { "X-Crash-Guard": "check" }, cache: "no-store" })
        .then((answer) => { if (!answer.ok && answer.status !== 429) throw new Error(answer.status); })
        .then(() => location.reload())
        .catch(() => { alert(button.dataset.failed); location.reload(); });
    }
  });
  // An evidence file is loaded once, when it is opened; as text, never as markup
  document.addEventListener("toggle", async (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.open || !d.dataset.src || d.dataset.loaded) return;
    d.dataset.loaded = "1";
    const pre = d.querySelector("pre");
    try {
      const answer = await fetch(d.dataset.src, { cache: "no-store" });
      if (!answer.ok) throw new Error(answer.status);
      pre.textContent = await answer.text();
    } catch (err) {
      pre.textContent = d.dataset.failed;
      delete d.dataset.loaded;
    }
  }, true);
})();""".replace("NAMES", json.dumps(THEME_VARS))

# Nothing but this page and its own requests; no frame of another site
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# --- Reading the private data -----------------------------------------------------------
def read_text(directory: Path, name: str, limit: int = FILE_MAX) -> tuple[str, int] | None:
    """The end of a regular file, at most limit bytes, and how many bytes were
    left out; None when it is missing, a symlink or no regular file. Opens
    every part of the path without following symlinks."""
    fds: list[int] = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        fds.append(fd)
        parts = [*directory.parts[1:], name]
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | (0 if i == len(parts) - 1 else os.O_DIRECTORY)
            fd = os.open(part, flags, dir_fd=fd)
            fds.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        skipped = max(info.st_size - limit, 0)
        os.lseek(fd, skipped, os.SEEK_SET)
        data = os.read(fd, limit)
    except OSError:
        return None
    finally:
        for fd in fds:
            os.close(fd)
    return data.decode("utf-8", errors="replace"), skipped


def lines_of(directory: Path, name: str) -> list[str]:
    read = read_text(directory, name)
    if read is None:
        return []
    text, skipped = read
    lines = text.splitlines()
    # A cut first line is only a part of a line
    return lines[1:] if skipped and lines else lines


def load_status(data: Path) -> dict | None:
    read = read_text(data, STATUS)
    if read is None:
        return None
    try:
        status = json.loads(read[0])
    except ValueError:
        return None
    return status if isinstance(status, dict) else None


# --- A check requested on the page --------------------------------------------------------
REQUEST_LOCK = threading.Lock()


def present(data: Path, name: str) -> bool:
    try:
        os.lstat(data / name)
    except OSError:
        return False
    return True


def last_request(data: Path) -> float:
    read = read_text(data, REQUESTED, 64)
    try:
        return float(read[0].strip()) if read else 0.0
    except ValueError:
        return 0.0


def check_state(data: Path, now: float) -> tuple[str, float]:
    """running, queued, wait (with the time a request is possible again) or ready."""
    if present(data, RUNNING):
        return "running", 0.0
    if present(data, REQUEST):
        return "queued", 0.0
    again = last_request(data) + MANUAL_GAP
    if now < again:
        return "wait", again
    return "ready", 0.0


def request_check(data: Path, now: float) -> int:
    """Leaves a request for the guard service: 202, or 429 within MANUAL_GAP
    of the last request or while one waits or runs."""
    with REQUEST_LOCK:
        if check_state(data, now)[0] != "ready":
            return 429
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            os.close(os.open(data / REQUEST, flags, 0o644))
        except FileExistsError:
            return 429
        stamp = data / f"{REQUESTED}.new"
        try:
            stamp.unlink(missing_ok=True)
            fd = os.open(stamp, flags, 0o644)
            with os.fdopen(fd, "w") as out:
                out.write(f"{now:.0f}\n")
            os.replace(stamp, data / REQUESTED)
        except OSError:
            LOGGER.warning("Time of the request could not be kept")
        LOGGER.info("Cache check requested on the page")
        return 202


def check_row(f: Format, data: Path, now: float) -> str:
    t = f.t
    state, again = check_state(data, now)
    note = {"running": t["check_running"], "queued": t["check_queued"],
            "wait": t["check_again"].format(time.strftime("%H:%M", time.localtime(again)))}.get(state, "")
    note = f'<span class="sub">{esc(note)}</span>' if note else ""
    disabled = "" if state == "ready" else " disabled"
    return (f'<div class="check">{note}<button class="text" id="check-now" type="button"'
            f' data-failed="{esc(t["check_failed"])}"{disabled}>{esc(t["check_now"])}</button></div>')


def parse_time(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def case_time(name: str) -> float | None:
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def entries(directory: Path) -> list[os.DirEntry]:
    try:
        return list(os.scandir(directory))
    except OSError:
        return []


def cases(data: Path) -> list[str]:
    """The case folders, newest first."""
    return sorted((e.name for e in entries(data / "cases")
                   if CASE_NAME.match(e.name) and e.is_dir(follow_symlinks=False)), reverse=True)


def event_of(events: list[str], case: str) -> str:
    """The message of the last event that names the case."""
    for line in reversed(events):
        if case in line:
            return line.split(" ", 1)[1] if " " in line else line
    return ""


def badpages(data: Path) -> list[dict]:
    """badpages.log: time, occasion, pfn=…, phys=…, bits=…, variants, page, file."""
    rows = []
    for line in lines_of(data, "badpages.log"):
        parts = line.split()
        if len(parts) < 3:
            continue
        fields = {k: v for k, _, v in (p.partition("=") for p in parts[2:]) if _}
        rows.append({"time": parts[0], "occasion": parts[1], "pfn": fields.get("pfn", ""),
                     "bits": fields.get("bits", ""), "file": parts[-1] if parts[-1].startswith("/") else ""})
    return rows


def checks(data: Path) -> list[dict]:
    """drops.log: time cache=N fresh=N|-"""
    rows = []
    for line in lines_of(data, "drops.log"):
        parts = line.split()
        if not parts:
            continue
        fields = {k: v for k, _, v in (p.partition("=") for p in parts[1:]) if _}
        rows.append({"time": parts[0], "cache": fields.get("cache", ""), "fresh": fields.get("fresh", "")})
    return rows


# --- Formatting --------------------------------------------------------------------------------
def lang_of(header: str | None) -> str:
    """German if the browser puts German before English, else English."""
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()
        if code.startswith("de"):
            return "de"
        if code.startswith("en"):
            return "en"
    return "en"


class Format:
    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.t = TEXT[lang]

    def int(self, value: int) -> str:
        text = f"{value:,}"
        return text.replace(",", ".") if self.lang == "de" else text

    def hours(self, hours: int) -> str:
        t = self.t
        return f"{hours // 24} {t['days']}" if hours > 48 and hours % 24 == 0 else f"{hours} {t['hours']}"

    def when(self, epoch: float | None) -> str:
        if epoch is None:
            return self.t["unknown"]
        return time.strftime("%d.%m.%Y %H:%M:%S" if self.lang == "de" else "%Y-%m-%d %H:%M:%S",
                             time.localtime(epoch))

    def stamp(self, text: str) -> str:
        """An ISO time from the logs in local time; as it is when it is none."""
        epoch = parse_time(text)
        return self.when(epoch) if epoch is not None else text

    def size(self, n: int) -> str:
        return f"{self.int(n)} B" if n < 1024 else f"{self.int(round(n / 1024))} KB"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def kv(rows: list[tuple[str, str]]) -> str:
    return '<table class="kv">' + "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def card(title: str | None, content: str, padded: bool = True) -> str:
    head = f"<h2>{esc(title)}</h2>" if title else ""
    inner = f'<div class="content">{content}</div>' if padded else content
    return f'<div class="card">{head}{inner}</div>'


def line(kind: str, title: str, text: str, sub: str = "") -> str:
    """A row of the status card: ok, bad or muted."""
    name = {"ok": "ok", "bad": "alert"}.get(kind, "info")
    sub = f'<span class="sub">{sub}</span>' if sub else ""
    return (f'<div class="state {"muted" if kind == "off" else kind}">{icon(name)}<div class="text">'
            f'<b>{esc(title)}</b><span class="sub">{esc(text)}</span>{sub}</div></div>')


def short_path(path: str) -> str:
    """A file of a Home Assistant layer without the layer's host path."""
    return LAYER.sub("", path) or path


def yes(t: dict, value) -> str:
    return t["unknown"] if value is None else t["yes"] if value else t["no"]


# --- The overview ---------------------------------------------------------------------------
def lock_line(f: Format, status: dict) -> str:
    t, lock = f.t, status.get("lock")
    if not lock:
        return line("off", t["lock"], t["no_pages"])
    rng = lock.get("range") or ""
    if lock.get("error"):
        return line("bad", t["lock"], t["lock_error"].format(lock["error"]))
    outside = lock.get("outside") or []
    sub = esc(t["outside"].format(", ".join(outside))) if outside else ""
    if lock.get("active"):
        return line("bad" if outside else "ok", t["lock"], t["lock_on"].format(rng), sub)
    text = t["lock_ready"] if lock.get("ready") else t["lock_off"]
    return line("bad", t["lock"], text.format(rng), sub)


def cache_line(f: Format, status: dict) -> str:
    t, check = f.t, status.get("check") or {}
    n = check.get("corrupted")
    if n is None:
        return line("off", t["cache"], t["cache_none"])
    when = f'{esc(t["checked"])} {esc(f.stamp(check.get("when") or ""))}'
    if n == 0:
        return line("ok", t["cache"], t["cache_ok"], when)
    return line("bad", t["cache"], t["cache_bad"].format(n), when)


def crash_line(f: Format, status: dict) -> str:
    t, c = f.t, status.get("crashes") or {}
    window = f.hours(int(c.get("window_hours") or 24))
    ha, programs = len(c.get("home_assistant") or []), len(c.get("programs") or [])
    sub = esc(f"{t['since_boot']}: {c.get('boot', t['unknown'])}")
    if ha or programs:
        return line("bad", t["crashes"], t["crashes_bad"].format(ha, programs, window), sub)
    return line("ok", t["crashes"], t["crashes_ok"].format(window), sub)


def lock_card(f: Format, status: dict, data: Path) -> str:
    t, lock = f.t, status.get("lock") or {}
    body = ""
    if lock:
        # The locked range is in the status card already
        body += kv([
            (t["boot_slot"], esc(lock.get("slot") or t["unknown"])),
            (t["overlay_slot"], esc(yes(t, lock.get("slot_overlay")))),
            (t["overlay_other"], esc(yes(t, lock.get("other_overlay")))),
            (t["config_line"], esc(yes(t, lock.get("config")))),
            (t["managed"], esc(yes(t, lock.get("managed")))),
            (t["lock_checked"], esc(f.stamp(lock.get("checked") or ""))),
        ])
    configured = set(status.get("bad_pages") or [])
    rows = "".join(
        f'<tr><td class="nowrap">{esc(f.stamp(p["time"]))}<span class="sub">{esc(p["occasion"])}</span></td>'
        f'<td class="mono nowrap">{esc(p["pfn"])}<span class="sub">'
        f'{esc(t["configured"]) if p["pfn"] in configured else ""}</span></td>'
        f'<td class="opt nowrap">{esc(p["bits"])}</td>'
        f'<td class="mono opt" title="{esc(p["file"])}">{esc(short_path(p["file"]))}</td></tr>'
        for p in reversed(badpages(data)))
    rows = rows or f'<tr><td colspan="4" class="muted">{esc(t["no_bad"])}</td></tr>'
    table = (f'<h3>{esc(t["pages"])}</h3><div class="scroll"><table class="list"><thead><tr>'
             f'<th>{esc(t["time"])}</th><th>{esc(t["pfn"])}</th><th class="opt">{esc(t["bits"])}</th>'
             f'<th class="opt">{esc(t["file"])}</th></tr></thead><tbody>{rows}</tbody></table></div>')
    return card(t["lock"], (f'<div class="content">{body}</div>' if body else "") + table, padded=False)


def crashes_card(f: Format, status: dict) -> str:
    """The crashes within the window; none is said by the status card already."""
    t, c = f.t, status.get("crashes") or {}
    if not (c.get("home_assistant") or c.get("programs")):
        return ""
    ha = "".join(f'<tr><td class="nowrap">{esc(f.stamp(e.get("time", "")))}</td><td>{esc(e.get("message", ""))}</td></tr>'
                 for e in c.get("home_assistant") or [])
    programs = "".join(f'<tr><td class="nowrap">{esc(f.stamp(e.get("time", "")))}</td>'
                       f'<td>{esc(e.get("process", ""))}</td><td>{esc(e.get("signal", ""))}</td></tr>'
                       for e in c.get("programs") or [])
    none = f'<p class="muted">{esc(t["no_crashes"])}</p>'
    body = (f'<h3>{esc(t["ha_crashes"])}</h3>'
            + (f'<div class="scroll"><table class="list"><tbody>{ha}</tbody></table></div>' if ha
               else f'<div class="content">{none}</div>')
            + f'<h3>{esc(t["program_crashes"])}</h3>'
            + (f'<div class="scroll"><table class="list"><thead><tr><th>{esc(t["time"])}</th>'
               f'<th>{esc(t["process"])}</th><th>{esc(t["signal"])}</th></tr></thead><tbody>{programs}</tbody></table></div>'
               if programs else f'<div class="content">{none}</div>'))
    window = f.hours(int(c.get("window_hours") or 24))
    return card(f'{t["crashes"]} · {window}', body, padded=False)


def cases_card(f: Format, status: dict, data: Path, events: list[str]) -> str:
    t = f.t
    rows = []
    for name in cases(data):
        kind = name.rsplit("-", 1)[1]
        href = "?" + urlencode({"case": name})
        rows.append(f'<tr class="link" data-href="{esc(href)}"><td class="nowrap">{esc(f.when(case_time(name)))}'
                    f'<span class="sub">{esc(t["kinds"].get(kind, kind))}</span></td>'
                    f'<td class="opt">{esc(event_of(events, name))}</td>'
                    f'<td class="go"><a href="{esc(href)}" aria-label="{esc(name)}">{icon("chevron")}</a></td></tr>')
    body = "".join(rows) or f'<tr><td colspan="3" class="muted">{esc(t["no_cases"])}</td></tr>'
    keep = status.get("cases_to_keep")
    note = f'<div class="note">{esc(t["kept"].format(keep))}</div>' if keep else ""
    return card(t["cases"], f'<div class="scroll"><table class="list"><tbody>{body}</tbody></table></div>{note}',
                padded=False)


def log_card(f: Format, data: Path, events: list[str]) -> str:
    t = f.t
    rows = "".join(f'<tr><td class="nowrap">{esc(f.stamp(e.split(" ", 1)[0]))}</td>'
                   f'<td>{esc(e.split(" ", 1)[1] if " " in e else "")}</td></tr>'
                   for e in reversed(events[-EVENTS_SHOWN:]))
    rows = rows or f'<tr><td colspan="2" class="muted">{esc(t["no_events"])}</td></tr>'
    drops = "".join(f'<tr><td class="nowrap">{esc(f.stamp(c["time"]))}</td><td class="n">{esc(c["cache"])}</td>'
                    f'<td class="n">{esc(c["fresh"])}</td></tr>' for c in reversed(checks(data)[-CHECKS_SHOWN:]))
    events_html = card(t["events"], f'<div class="scroll"><table class="list"><tbody>{rows}</tbody></table></div>',
                       padded=False)
    if not drops:
        return events_html
    return events_html + card(t["checks"], (
        f'<div class="scroll"><table class="list"><thead><tr><th>{esc(t["time"])}</th>'
        f'<th class="n">{esc(t["cache_count"])}</th><th class="n">{esc(t["fresh_count"])}</th></tr></thead>'
        f'<tbody>{drops}</tbody></table></div>'), padded=False)


def overview(f: Format, data: Path, now: float) -> str:
    t = f.t
    status = load_status(data)
    events = lines_of(data, "events.log")
    if status is None:
        return card(t["status"], f'<p class="muted">{esc(t["loading"])}</p>') + log_card(f, data, events)
    state = card(t["status"], lock_line(f, status) + cache_line(f, status) + crash_line(f, status)
                 + check_row(f, data, now))
    return (f'<div class="cards"><div>{state}{crashes_card(f, status)}</div>'
            f'<div>{lock_card(f, status, data)}</div></div>'
            + cases_card(f, status, data, events) + log_card(f, data, events))


# --- A case ------------------------------------------------------------------------------------
def mismatch_rows(f: Format, folder: Path) -> str:
    t, rows = f.t, []
    for name, where in (("cache.mismatch", t["in_cache"]), ("fresh.mismatch", t["fresh"])):
        lines = lines_of(folder, name)
        for entry in lines[:LIST_MAX]:
            path, _, rest = entry.partition(" ")
            expected, _, actual = rest.partition(" ")
            rows.append(f'<tr><td class="mono" title="{esc(path)}">{esc(short_path(path))}</td>'
                        f'<td class="nowrap">{esc(where)}</td>'
                        f'<td class="mono opt">{esc(expected[:16])}<span class="sub">{esc(actual[:16])}</span></td></tr>')
        if len(lines) > LIST_MAX:
            rows.append(f'<tr><td colspan="3" class="muted">{esc(t["more_lines"].format(len(lines) - LIST_MAX))}</td></tr>')
    for path in lines_of(folder, "ref-inconsistent.txt")[:LIST_MAX]:
        rows.append(f'<tr><td class="mono" title="{esc(path)}">{esc(short_path(path))}</td>'
                    f'<td colspan="2">{esc(t["inconsistent"])}</td></tr>')
    return "".join(rows)


def case_page(f: Format, data: Path, name: str) -> str:
    t = f.t
    folder = data / "cases" / name
    if name not in cases(data):
        return card(None, f'<p class="muted">{esc(t["case_unknown"])}</p>')
    kind = name.rsplit("-", 1)[1]
    events = lines_of(data, "events.log")
    facts = [(t["time"], esc(f.when(case_time(name)))), (t["kind"], esc(t["kinds"].get(kind, kind)))]
    message = event_of(events, name)
    if message:
        facts.append((t["message"], esc(message)))
    state = read_text(folder, "state.json")
    if state is not None:
        try:
            s = json.loads(state[0])
        except ValueError:
            s = None
        if isinstance(s, dict):
            facts += [(t["exit"], esc(s.get("ExitCode", t["unknown"]))),
                      (t["oom"], esc(yes(t, s.get("OOMKilled")))),
                      (t["started"], esc(f.stamp(str(s.get("StartedAt", ""))[:19] + "+00:00"))),
                      (t["finished"], esc(f.stamp(str(s.get("FinishedAt", ""))[:19] + "+00:00")))]
            if s.get("Error"):
                facts.append((t["error"], esc(s["Error"])))
    body = card(name, kv(facts))

    rows = mismatch_rows(f, folder)
    rows = rows or f'<tr><td colspan="3" class="muted">{esc(t["no_mismatch"])}</td></tr>'
    body += card(t["mismatches"], (
        f'<div class="scroll"><table class="list"><thead><tr><th>{esc(t["file"])}</th><th>{esc(t["where"])}</th>'
        f'<th class="opt">{esc(t["expected"])} / {esc(t["actual"])}</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'), padded=False)

    saved = []
    for sub in SAVED:
        for e in sorted(entries(folder / sub), key=lambda e: e.name):
            if e.is_file(follow_symlinks=False):
                size = e.stat(follow_symlinks=False).st_size
                saved.append(f'<tr><td class="mono">{esc(SAVED_LAYER.sub("", e.name))}</td>'
                             f'<td class="nowrap">{esc(t["in_cache"] if sub == "cache_files" else t["fresh"])}</td>'
                             f'<td class="n">{esc(f.size(size))}</td></tr>')
    if saved:
        body += card(t["saved"], (
            f'<div class="scroll"><table class="list"><thead><tr><th>{esc(t["file"])}</th><th>{esc(t["where"])}</th>'
            f'<th class="n">{esc(t["size"])}</th></tr></thead><tbody>{"".join(saved)}</tbody></table></div>'),
            padded=False)

    logs = []
    for file in EVIDENCE:
        info = file_info(folder, file)
        if info is None:
            continue
        src = "?" + urlencode({"case": name, "file": file})
        logs.append(f'<details data-src="{esc(src)}" data-failed="{esc(t["load_failed"])}"><summary>{icon("chevron")}'
                    f'<span class="mono">{esc(file)}</span><span class="muted">{esc(f.size(info))}</span></summary>'
                    f'<pre></pre></details>')
    evidence = "".join(logs) or f'<div class="content"><p class="muted">{esc(t["no_evidence"])}</p></div>'
    body += card(t["evidence"], f'<div class="note">{esc(t["evidence_info"])}</div>{evidence}', padded=False)
    return body


def file_info(folder: Path, name: str) -> int | None:
    """The size of a regular file in a case, None when there is none."""
    try:
        info = os.stat(folder / name, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def evidence_text(data: Path, case: str, file: str, lang: str) -> str | None:
    """An evidence file as plain text, its last FILE_MAX bytes."""
    if not CASE_NAME.match(case) or file not in EVIDENCE or case not in cases(data):
        return None
    read = read_text(data / "cases" / case, file)
    if read is None:
        return None
    text, skipped = read
    if skipped:
        lines = text.split("\n", 1)
        text = lines[1] if len(lines) > 1 else text
        note = "… {} KB davor ausgelassen\n" if lang == "de" else "… {} KB before this left out\n"
        text = note.format(round(skipped / 1024)) + text
    return text


def render(data: Path, lang: str, slug: str | None, query: dict[str, list[str]], now: float | None = None) -> str:
    f = Format(lang)
    t = f.t
    case = (query.get("case") or [""])[0]
    if case and CASE_NAME.match(case):
        body = case_page(f, data, case)
        start = (f'<a class="icon" href="?" title="{esc(t["back"])}">{icon("back")}</a>'
                 f'<h1>{esc(t["title"])} · {esc(t["cases"])}</h1>')
    now = time.time() if now is None else now
    if not (case and CASE_NAME.match(case)):
        body = overview(f, data, now)
        start = f'<h1>{esc(t["title"])}</h1>'
    actions = f'<span class="stamp opt">{esc(t["as_of"].format(time.strftime("%H:%M:%S", time.localtime(now))))}</span>'
    actions += f'<a class="icon" href="" id="refresh" title="{esc(t["refresh"])}" role="button">{icon("refresh")}</a>'
    # The settings are Home Assistant's own page, outside the ingress frame
    actions += (f'<a class="icon" href="/config/app/{esc(slug)}/config" target="_top" title="{esc(t["settings"])}">'
                f'{icon("settings")}</a>' if slug else "")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(t['title'])}</title><style>{STYLE}</style><script>{SCRIPT}</script></head><body>"
            f'<header>{start}<div class="actions">{actions}</div></header><main>{body}</main></body></html>')


# --- The server ------------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer looks up its own name here, which can wait for DNS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "crash-guard-web", self.server_address[1]


def handler(data: Path, slug: str | None, allowed: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "crash-guard-web"

        def _refused(self) -> bool:
            if allowed and self.client_address[0] not in allowed:
                self._send(403, "text/plain", b"only through Home Assistant")
            elif self.path.split("?", 1)[0] != "/":
                self._send(404, "text/plain", b"not found")
            else:
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
            if self._refused():
                return
            query = parse_qs(self.path.partition("?")[2])
            lang = lang_of(self.headers.get("Accept-Language"))
            file = (query.get("file") or [""])[0]
            if file:
                text = evidence_text(data, (query.get("case") or [""])[0], file, lang)
                if text is None:
                    self._send(404, "text/plain", b"not found")
                else:
                    self._send(200, "text/plain; charset=utf-8", text.encode())
                return
            self._send(200, "text/html; charset=utf-8", render(data, lang, slug, query).encode())

        def do_POST(self) -> None:  # noqa: N802
            if self._refused():
                return
            if self.headers.get("X-Crash-Guard") != "check":
                self._send(403, "text/plain", b"only from the page")
                return
            code = request_check(data, time.time())
            self._send(code, "text/plain", b"requested" if code == 202 else b"not now")

        def _not_allowed(self) -> None:
            if not self._refused():
                self._send(405, "text/plain", b"not allowed", {"Allow": "GET, POST"})

        do_PUT = do_DELETE = do_PATCH = _not_allowed

        def _send(self, code: int, kind: str, body: bytes, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # no access log
            pass

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    # The app's slug, e.g. 4a183770_crash_guard; the Supervisor names the container's host after it
    slug = os.environ.get("HOSTNAME", "").replace("-", "_") or None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), handler(DATA, slug, {INGRESS_PEER}))
    LOGGER.info("Web page on port %d (ingress)", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
