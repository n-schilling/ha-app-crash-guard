"""Made-up data for the page, laid out as the guard service writes it: a
defective page under the RAM lock, one Home Assistant crash, one check that
found a corrupted file, program crashes on the host. Nothing comes from a real
host. Standard library only: it runs inside the app's image (scripts/page/serve.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# A fixed moment, so the pictures only change when the page does
NOW = datetime.fromisoformat("2026-11-10T09:12:40+01:00").timestamp()
LAYER = "/mnt/data/docker/overlay2/" + "3f" * 32 + "/diff"
OTHER = "/mnt/data/docker/overlay2/" + "8c" * 32 + "/diff"
CRASH = "20261107-031522-crash"
CHECK = "20261106-163051-check"
REF = "20261104-043112-ref"


def iso(day: int, clock: str) -> str:
    return f"2026-11-{day:02d}T{clock}+01:00"


def write(data: Path) -> None:
    """Made-up findings: a defective page under the lock, one Home Assistant
    crash, one check that found a corrupted file, program crashes on the host."""
    status = {
        "lock": {"active": True, "ready": True, "slot": "slot-B", "slot_overlay": True, "other_overlay": True,
                 "config": True, "changed": False, "managed": True, "range": "0x7a1200000-0x7a13fffff (2 MB)",
                 "outside": [], "checked": iso(10, "09:00:12")},
        "check": {"corrupted": 0, "when": iso(10, "04:30:48")},
        "crashes": {"window_hours": 24, "boot": 3, "programs_count": 2, "problem": True,
                    "programs": [{"process": "python3", "signal": "11/SEGV", "time": "2026-11-10T02:41:09Z"},
                                 {"process": "node", "signal": "7/BUS", "time": "2026-11-09T19:03:55Z"}],
                    "home_assistant": []},
        "cases_to_keep": 20, "drop_times": ["04:30", "16:30"], "drop_on_schedule": False,
        "bad_pages": ["0x7a1234"],
    }
    (data / "status.json").write_text(json.dumps(status))
    (data / "badpages.log").write_text(
        f"{iso(6, '16:31:40')} check-cache pfn=0x7a1234 phys=0x7a1234000 bits=38 variants=5/5 page=14 "
        f"{LAYER}/usr/local/lib/python3.14/site-packages/aiohttp/web_protocol.py\n"
        f"{iso(7, '03:15:58')} crash-cache pfn=0x7a1234 phys=0x7a1234000 bits=41 variants=5/5 page=3 "
        f"{OTHER}/usr/src/homeassistant/homeassistant/core.py\n")
    (data / "drops.log").write_text("".join(
        f"{iso(d, t)} cache={n} fresh=- (no drop)\n"
        for d, t, n in ((5, "04:30:51", 0), (5, "16:30:49", 0), (6, "04:30:50", 0), (6, "16:30:51", 1),
                        (7, "04:30:47", 0), (7, "16:30:52", 0), (8, "04:30:49", 0), (8, "16:30:50", 0),
                        (9, "04:30:48", 0), (9, "16:30:51", 0), (10, "04:30:48", 0))))
    (data / "events.log").write_text("".join(f"{line}\n" for line in (
        f"{iso(4, '04:31:12')} Reference built: 48211 files, 1 read inconsistently (excluded) ({REF})",
        f"{iso(4, '04:31:12')} RAM lock in effect: 0x7a1200000-0x7a13fffff (2 MB) is locked for Linux",
        f"{iso(6, '16:30:51')} Physical addresses located (check-cache): pages=1 skipped=0 -> 0x7a1234(1)",
        f"{iso(6, '16:30:51')} Check (no drop): CORRUPTED in cache 1, cache deliberately NOT dropped (case {CHECK})",
        f"{iso(7, '03:15:58')} Physical addresses located (crash-cache): pages=1 skipped=0 -> 0x7a1234(2)",
        f"{iso(7, '03:17:31')} Crash handled: exit 139, corrupted in cache 1, afterwards 0, "
        f"Home Assistant running again (case {CRASH})",
        f"{iso(8, '11:02:07')} Reference updated: 3 file(s) changed on the drive, not in RAM",
        f"{iso(10, '04:30:48')} Check (no drop): no corrupted files in the cache",
    )))
    cases = data / "cases"
    crash = cases / CRASH
    (crash / "cache_files").mkdir(parents=True)
    (crash / "state.json").write_text(json.dumps({
        "ExitCode": 139, "OOMKilled": False, "Error": "",
        "StartedAt": "2026-11-06T22:40:03.5Z", "FinishedAt": "2026-11-07T02:15:21.9Z"}))
    (crash / "cache.mismatch").write_text(
        f"{OTHER}/usr/src/homeassistant/homeassistant/core.py {'a41c' * 16} {'9e07' * 16}\n")
    (crash / "cache_files" / ("8c" * 32 + "_diff_usr_src_homeassistant_homeassistant_core.py")).write_bytes(
        b"x" * 118_000)
    (crash / "container.log").write_text("Fatal Python error: Segmentation fault\n" * 40)
    (crash / "home-assistant.log.fault").write_text("Fatal Python error: Segmentation fault\n" * 12)
    (crash / "kernel.txt").write_text("python3[4211]: segfault at 7f3a1c0 ip 7f3a1c0 sp 7ffd error 6\n" * 3)
    (crash / "coredumps.txt").write_text("Process 4211 (python3) of user 0 dumped core.\n")
    check = cases / CHECK
    (check / "cache_files").mkdir(parents=True)
    (check / "cache.mismatch").write_text(
        f"{LAYER}/usr/local/lib/python3.14/site-packages/aiohttp/web_protocol.py {'07bd' * 16} {'c2f0' * 16}\n")
    (check / "cache_files" / ("3f" * 32 + "_diff_usr_local_lib_python3.14_site-packages_aiohttp_web_protocol.py")
     ).write_bytes(b"x" * 27_400)
    ref = cases / REF
    ref.mkdir()
    (ref / "ref-inconsistent.txt").write_text(f"{LAYER}/usr/lib/libcrypto.so.3\n")
