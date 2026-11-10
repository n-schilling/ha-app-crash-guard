"""Tests for the web page. Run inside the built image:
  docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python3 <image> \
    -m unittest discover -s /tests -p 'test_*.py'
The private data is a temporary folder laid out as the guard service writes it.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = ["/usr/local/lib", str(TESTS.parent / "crash_guard/rootfs/usr/local/lib")]

import crash_guard_web as w  # noqa: E402

NOW = 1_800_000_000.0
LAYER = "/mnt/data/docker/overlay2/" + "a" * 64 + "/diff"
CRASH = "20261001-120000-crash"
CHECK = "20261002-043000-check"
STATUS = {
    "lock": {"active": True, "ready": True, "slot": "slot-A", "slot_overlay": True, "other_overlay": True,
             "config": True, "changed": False, "managed": True, "range": "0x1cc400000-0x1cc5fffff (2 MB)",
             "outside": [], "checked": "2026-10-02T05:00:00+02:00"},
    "check": {"corrupted": 0, "when": "2026-10-02T04:30:52+02:00"},
    "crashes": {"window_hours": 24, "boot": 1, "home_assistant": [],
                "programs": [{"process": "python3", "signal": "11/SEGV", "time": "2026-10-02T03:00:00Z"}],
                "problem": True},
    "cases_to_keep": 20, "drop_times": ["04:30"], "drop_on_schedule": False, "bad_pages": ["0x1cc447"],
}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved: the page opens no path through a symlink, also not /tmp -> /private/tmp
        self.root = Path(os.path.realpath(self.tmp.name))
        self.data = self.root / "crash_guard"
        crash = self.data / "cases" / CRASH
        (crash / "cache_files").mkdir(parents=True)
        (self.data / "cases" / CHECK).mkdir()
        (self.data / "status.json").write_text(json.dumps(STATUS))
        (self.data / "events.log").write_text(
            f"2026-10-01T12:01:00+02:00 Crash handled: exit 139, corrupted in cache 1, afterwards 0, "
            f"Home Assistant running again (case {CRASH})\n"
            f"2026-10-02T04:30:52+02:00 Check (no drop): no corrupted files <script>x</script>\n")
        (self.data / "drops.log").write_text("2026-10-02T04:30:52+02:00 cache=0 fresh=- (no drop)\n")
        (self.data / "badpages.log").write_text(
            f"2026-09-29T20:22:00+0000 manual pfn=0x1cc447 phys=0x1cc447000 bits=45 variants=8/8 page=2 {LAYER}/usr/x.json\n")
        (crash / "cache.mismatch").write_text(f"{LAYER}/usr/src/a.py {'1' * 64} {'2' * 64}\n")
        (crash / "state.json").write_text(json.dumps({"ExitCode": 139, "OOMKilled": False, "Error": "",
                                                      "StartedAt": "2026-10-01T09:00:00.1Z",
                                                      "FinishedAt": "2026-10-01T10:00:00.1Z"}))
        (crash / "container.log").write_text("token=<b>secret</b>\n")
        (crash / "cache_files" / ("a" * 64 + "_diff_usr_src_a.py")).write_text("x")
        # A symlink to a secret in place of a log
        self.secret = self.root / "secrets.yaml"
        self.secret.write_text("api_password: hunter2\n")
        (crash / "kernel.txt").symlink_to(self.secret)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def page(self, query: str = "", lang: str = "en") -> str:
        return w.render(self.data, lang, "4a183770_crash_guard", w.parse_qs(query), now=NOW, version="9.8.7")


class Overview(Fixture):
    def test_check_button(self) -> None:
        self.assertRegex(self.page(), r'<button class="button" id="check-now"[^>]*>Check now</button>')
        self.assertNotRegex(self.page(), r'id="check-now"[^>]* disabled')
        (self.data / w.REQUESTED).write_text(f"{NOW - 60:.0f}\n")
        page = self.page(lang="de")
        self.assertIn("Wieder möglich um ", page)
        self.assertRegex(page, r'id="check-now"[^>]* disabled>Jetzt prüfen')
        (self.data / w.REQUEST).touch()
        self.assertIn("Check requested, starts within a few seconds", self.page())
        (self.data / w.RUNNING).touch()
        self.assertIn("Check running", self.page())

    def test_problem_lines(self) -> None:
        page = self.page()
        self.assertIn("In effect: 0x1cc400000-0x1cc5fffff (2 MB)", page)
        self.assertIn("No corrupted files at the last check", page)
        self.assertIn("Home Assistant 0, programs 1 within 24 h", page)

    def test_lock_problems(self) -> None:
        for lock, text in (({"active": False, "ready": True}, "takes effect after a host reboot"),
                           ({"active": False, "ready": False}, "Not in place"),
                           ({"error": "no device tree"}, "Check failed: no device tree"),
                           ({"active": True, "outside": ["0x5000"]}, "outside the lock: 0x5000")):
            (self.data / "status.json").write_text(json.dumps({**STATUS, "lock": {**STATUS["lock"], **lock}}))
            self.assertIn(text, self.page())
        (self.data / "status.json").write_text(json.dumps({**STATUS, "lock": None}))
        self.assertIn("No defective pages configured", self.page())

    def test_crash_list_only_with_crashes(self) -> None:
        self.assertIn("python3", self.page())
        (self.data / "status.json").write_text(json.dumps({**STATUS, "crashes": {**STATUS["crashes"], "programs": []}}))
        page = self.page()
        self.assertIn("No crash within 24 h", page)
        self.assertNotIn("Programs on the host", page)

    def test_version_at_the_bottom(self) -> None:
        for page in (self.page(), self.page(f"case={CRASH}"), self.page(lang="de")):
            self.assertTrue(page.endswith('</main><footer class="version">Crash Guard 9.8.7</footer></body></html>'))
        page = w.render(self.data, "en", None, {}, now=NOW)
        self.assertIn('<footer class="version">Crash Guard</footer>', page)

    def test_failed_check(self) -> None:
        (self.data / "status.json").write_text(
            json.dumps({**STATUS, "check": {"corrupted": None, "when": "2026-10-02T04:30:52+02:00"}}))
        self.assertIn("The last check failed", self.page())
        (self.data / "status.json").write_text(json.dumps({**STATUS, "check": {"corrupted": None, "when": None}}))
        self.assertIn("No check yet", self.page())

    def test_more_program_crashes_than_listed(self) -> None:
        (self.data / "status.json").write_text(
            json.dumps({**STATUS, "crashes": {**STATUS["crashes"], "programs_count": 300}}))
        page = self.page()
        self.assertIn("Home Assistant 0, programs 300 within 24 h", page)
        self.assertIn("The newest 1 of 300", page)

    def test_shared_stylesheet(self) -> None:
        page = self.page()
        self.assertTrue(w.SHARED_STYLE.startswith("/* ha.css"))
        self.assertIn(f"<style>{w.SHARED_STYLE}</style>", page)
        self.assertIn('class="icon-button"', page)
        self.assertIn('<div class="card-actions">', page)

    def test_without_status(self) -> None:
        (self.data / "status.json").unlink()
        self.assertIn("has not reported yet", self.page())

    def test_cases_newest_first_with_their_event(self) -> None:
        page = self.page()
        self.assertLess(page.index(f"case={CHECK}"), page.index(f"case={CRASH}"))
        self.assertIn("Home Assistant running again", page)

    def test_logs_are_escaped(self) -> None:
        page = self.page()
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)

    def test_bad_pages(self) -> None:
        page = self.page()
        self.assertIn("0x1cc447", page)
        self.assertIn("In bad_pages", page)
        self.assertIn("/usr/x.json", page)
        self.assertNotIn(LAYER + "/usr/x.json<", page)

    def test_german(self) -> None:
        page = self.page(lang="de")
        self.assertIn("Speichersperre", page)
        self.assertIn("Greift:", page)

    def test_no_case_symlinks(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.data / "cases" / "20261003-000000-crash").symlink_to(outside)
        self.assertNotIn("20261003-000000-crash", self.page())


class Case(Fixture):
    def test_details(self) -> None:
        page = self.page(f"case={CRASH}")
        self.assertIn("/usr/src/a.py", page)
        self.assertIn("1111111111111111", page)
        self.assertIn("usr_src_a.py", page)
        self.assertIn(">139<", page)
        self.assertIn('data-src="?case=' + CRASH + '&amp;file=container.log"', page)

    def test_logs_not_in_the_page(self) -> None:
        page = self.page(f"case={CRASH}")
        self.assertNotIn("token=", page)
        self.assertNotIn("hunter2", page)
        self.assertNotIn("kernel.txt", page)  # a symlink is no evidence

    def test_unknown_or_malformed(self) -> None:
        self.assertIn("does not exist", self.page("case=20990101-000000-crash"))
        self.assertIn("Event log", self.page("case=../../etc"))

    def test_evidence(self) -> None:
        self.assertEqual(w.evidence_text(self.data, CRASH, "container.log", "en"), "token=<b>secret</b>\n")

    def test_evidence_refused(self) -> None:
        for case, file in ((CRASH, "kernel.txt"), (CRASH, "state.json"), (CRASH, "../../secrets.yaml"),
                           ("../" + CRASH, "container.log"), (CRASH, "cache_files/" + "a" * 64)):
            self.assertIsNone(w.evidence_text(self.data, case, file, "en"), (case, file))

    def test_evidence_end_only(self) -> None:
        (self.data / "cases" / CRASH / "coredumps.txt").write_text("first\n" + "x" * w.FILE_MAX + "\nlast\n")
        text = w.evidence_text(self.data, CRASH, "coredumps.txt", "en")
        self.assertTrue(text.startswith("… 0 KB before this left out\n"))
        self.assertTrue(text.endswith("last\n"))
        self.assertNotIn("first", text)

    def test_read_text_never_follows_a_symlinked_folder(self) -> None:
        link = self.data / "cases" / "20261004-000000-crash"
        link.symlink_to(self.data / "cases" / CRASH)
        self.assertIsNone(w.read_text(link, "container.log"))


class Server(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = w.Server(("127.0.0.1", 0), w.handler(self.data, "slug", {"127.0.0.1"}, "1.2.3"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_page(self) -> None:
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        self.assertIn("Crash Guard 1.2.3</footer>", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_only_the_own_script_runs(self) -> None:
        csp = self.get("/")[1]["Content-Security-Policy"]
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        script = w.SCRIPT.encode()
        digest = w.base64.b64encode(w.hashlib.sha256(script).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", csp)
        self.assertIn(f"<script>{w.SCRIPT}</script>", self.get("/")[2])

    def test_evidence_as_plain_text(self) -> None:
        code, headers, body = self.get(f"/?case={CRASH}&file=container.log")
        self.assertEqual((code, headers["Content-Type"]), (200, "text/plain; charset=utf-8"))
        self.assertEqual(body, "token=<b>secret</b>\n")
        self.assertEqual(self.get(f"/?case={CRASH}&file=kernel.txt")[0], 404)

    def get(self, path: str, method: str = "GET", headers: dict | None = None):
        request = urllib.request.Request(self.url + path, method=method, headers=headers or {},
                                         data=b"" if method != "GET" else None)
        try:
            with urllib.request.urlopen(request) as answer:
                return answer.status, dict(answer.headers), answer.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, dict(err.headers), err.read().decode()

    def test_no_other_changes(self) -> None:
        for method in ("PUT", "DELETE", "PATCH"):
            code, headers, _ = self.get("/", method)
            self.assertEqual((code, headers.get("Allow")), (405, "GET, POST"), method)

    def test_check_request_needs_the_header(self) -> None:
        self.assertEqual(self.get("/", "POST")[0], 403)
        self.assertEqual(self.get("/", "POST", {"X-Crash-Guard": "other"})[0], 403)
        self.assertFalse((self.data / w.REQUEST).exists())

    def test_check_request_once_per_gap(self) -> None:
        check = {"X-Crash-Guard": "check"}
        self.assertEqual(self.get("/", "POST", check)[0], 202)
        self.assertTrue((self.data / w.REQUEST).is_file())
        # still waiting for the guard service
        self.assertEqual(self.get("/", "POST", check)[0], 429)
        # taken by the guard service, but within the gap
        (self.data / w.REQUEST).unlink()
        self.assertEqual(self.get("/", "POST", check)[0], 429)
        self.assertFalse((self.data / w.REQUEST).exists())
        (self.data / w.REQUESTED).write_text(f"{w.time.time() - w.MANUAL_GAP - 1:.0f}\n")
        self.assertEqual(self.get("/", "POST", check)[0], 202)

    def test_check_request_never_follows_a_symlink(self) -> None:
        target = self.root / "elsewhere"
        (self.data / w.REQUEST).symlink_to(target)
        self.assertEqual(self.get("/", "POST", {"X-Crash-Guard": "check"})[0], 429)
        self.assertFalse(target.exists())

    def test_other_paths(self) -> None:
        self.assertEqual(self.get("/cases/" + CRASH)[0], 404)
        self.assertEqual(self.get("/../status.json")[0], 404)

    def test_only_the_ingress_proxy(self) -> None:
        self.server.RequestHandlerClass = w.handler(self.data, "slug", {"172.30.32.2"})
        self.assertEqual(self.get("/")[0], 403)
        self.assertEqual(self.get("/", "POST")[0], 403)


if __name__ == "__main__":
    unittest.main()
