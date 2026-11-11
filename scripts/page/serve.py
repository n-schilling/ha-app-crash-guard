"""Starts the app's web page with made-up data, inside the built image.

Run by scripts/screenshots.py in a throwaway container (`--tmpfs /data`):

    docker run --entrypoint python3 -v "$PWD/scripts/page:/page:ro" <image> /page/serve.py

It does what the service web/run does before the page starts (the data is
root's and readable for the group cgweb, the page's own folder is cgweb's),
then drops to the user cgweb and runs the page of the image,
/usr/local/lib/crash_guard_web.py, unchanged. The clock of the page stands
still at demo_data.NOW, so the pictures only change when the page does.
"""

from __future__ import annotations

import os
import pwd
import runpy
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_data  # noqa: E402

DATA = Path("/data/crash_guard")
USER = "cgweb"


def main() -> None:
    user = pwd.getpwnam(USER)
    DATA.mkdir(parents=True)
    demo_data.write(DATA)
    for path in [DATA, *DATA.rglob("*")]:
        os.chown(path, 0, user.pw_gid)
        os.chmod(path, 0o2750 if path.is_dir() else 0o640)
    web = DATA / "web"
    web.mkdir(mode=0o750)
    os.chown(web, user.pw_uid, user.pw_gid)

    os.environ.pop("SUPERVISOR_TOKEN", None)
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    time.time = lambda: demo_data.NOW
    runpy.run_path("/usr/local/lib/crash_guard_web.py", run_name="__main__")


if __name__ == "__main__":
    main()
