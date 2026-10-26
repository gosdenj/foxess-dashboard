#!/usr/bin/env python3
"""Run the FoxESS dashboard as a kiosk on a Raspberry Pi.

Starts (or attaches to) the dashboard server and puts a browser in kiosk mode
on the attached LCD, restarting the browser if it ever exits.

    python3 pi_kiosk.py                 # start the server too
    python3 pi_kiosk.py --no-server     # the server is already running
    python3 pi_kiosk.py --url http://other-pi:8765

With a Pi 7" touchscreen you also want:

    export DISPLAY=:0
    xset s off -dpms        # stop the screensaver blanking the panel
    xset -dpms s 0

A systemd unit for an unattended boot is in display/README.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CHROME_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "chrome",
)

#: Flags tuned for a Raspberry Pi 3 (1.2 GHz Cortex-A53, 1 GB RAM) driving the
#: official 7" touch display at 800x480.
CHROME_FLAGS = [
    "--kiosk",
    "--noerrdialogs",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-pinch",
    "--overscroll-history-navigation=0",
    "--disable-session-crashed-bubble",
    "--disable-infobars",
    "--disable-dev-shm-usage",     # /dev/shm is tiny on a Pi; avoids crashes
    "--disable-features=Translate,BackForwardCache,MediaRouter",
    "--check-for-update-interval=31536000",
]

#: Extra flags applied only when the user asks for them.
OPTIONAL_FLAGS = {
    "--no-sandbox": "Chromium's sandbox needs user namespaces, which some Pi "
                    "kernels lack - add this if Chromium refuses to start.",
    "--disable-gpu": "Force software rendering. Usually NOT wanted on a Pi 3: "
                     "the VideoCore IV GPU makes the CSS animations smoother.",
    "--window-size=800,480": "Match the panel exactly.",
}


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_server(host: str, port: int, seconds: int = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def start_server(project: Path, config: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    if config:
        env["FOXESS_DASHBOARD_CONFIG"] = config
    print(f"starting the dashboard server from {project}")
    return subprocess.Popen(
        [sys.executable, str(project / "run.py")],
        cwd=str(project), env=env,
    )


def run_browser(chrome: str, url: str, extra: list[str]) -> int:
    print(f"launching kiosk browser -> {url}")
    flags = CHROME_FLAGS + extra
    while True:
        proc = subprocess.Popen([chrome, *flags, url])
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 0
        print("browser exited, restarting in 2s")
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="FoxESS dashboard kiosk for a Pi")
    ap.add_argument("--url", help="dashboard URL (default: this Pi)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", help="path to the settings JSON file")
    ap.add_argument("--no-server", action="store_true",
                    help="do not start the server, just the browser")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="add --no-sandbox (needed on some Pi kernels)")
    ap.add_argument("--disable-gpu", action="store_true",
                    help="force software rendering")
    ap.add_argument("--window-size", default=None,
                    help="e.g. 800,480 to match the panel exactly")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("No Chromium/Chrome found. On Raspberry Pi OS:\n"
              "    sudo apt install chromium\n")
        return 1

    extra = []
    if args.no_sandbox:
        extra.append("--no-sandbox")
    if args.disable_gpu:
        extra.append("--disable-gpu")
    if args.window_size:
        extra.append(f"--window-size={args.window_size}")

    project = Path(__file__).resolve().parent.parent
    server = None

    if not args.no_server and not port_open(args.host, args.port):
        server = start_server(project, args.config)
        if not wait_for_server(args.host, args.port, 40):
            print("the dashboard server did not come up")
            if server:
                server.terminate()
            return 1

    url = args.url or f"http://{args.host}:{args.port}/"

    def shutdown(*_):
        if server:
            server.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        return run_browser(chrome, url, extra)
    finally:
        if server:
            server.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
