"""Run the addon: python run.py [--with-sidecar]

Single-window mode (--with-sidecar, or WITH_SIDECAR=1): run.py starts the
Node sidecar as a child process, prefixes its logs with [sidecar], waits
until its port accepts connections, then serves the addon in the foreground.
Ctrl+C stops both.

Two-window/manual mode (default): only the addon is served. The sidecar must
be running separately at CINESRC_URL (default http://127.0.0.1:8001) or
/stream returns no results.

Env: PORT (default 7001), SIDECAR_PORT (default 8001), CINESRC_URL.
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _pump(stream, prefix: str) -> None:
    for line in iter(stream.readline, ""):
        sys.stdout.write(f"{prefix}{line}")
        sys.stdout.flush()


def start_sidecar() -> subprocess.Popen:
    env = dict(os.environ)
    env.setdefault("SIDECAR_PORT", "8001")
    try:
        proc = subprocess.Popen(
            ["node", os.path.join("sidecar", "src", "server.js")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        sys.exit("[ERROR] Node.js not found on PATH. Install Node.js 20+ and retry.")
    port = int(env["SIDECAR_PORT"])
    threading.Thread(target=_pump, args=(proc.stdout, "[sidecar] "),
                     daemon=True).start()
    print(f"Waiting for sidecar on 127.0.0.1:{port} ...", flush=True)
    if not _wait_for_port("127.0.0.1", port):
        proc.terminate()
        sys.exit(f"[ERROR] Sidecar did not come up on port {port}. "
                 f"Check the [sidecar] logs above.")
    print(f"Sidecar up on 127.0.0.1:{port}.", flush=True)
    return proc


def main() -> None:
    ap = argparse.ArgumentParser(description="CineSrc Stremio addon")
    ap.add_argument("--with-sidecar", action="store_true",
                    default=os.getenv("WITH_SIDECAR", "0") == "1",
                    help="also run the Node sidecar in this window")
    args = ap.parse_args()

    proc = start_sidecar() if args.with_sidecar else None
    port = int(os.getenv("PORT", "7001"))
    workers = int(os.getenv("WORKERS", "1"))
    try:
        print(f"Serving addon on 0.0.0.0:{port} "
              f"(manifest: http://127.0.0.1:{port}/manifest.json) [granian x{workers}]",
              flush=True)
        from granian import Granian

        Granian(
            "app.main:app",
            address="0.0.0.0",
            port=port,
            workers=workers,
            interface="asgi",
        ).serve()
    finally:
        if proc is not None:
            print("Stopping sidecar ...", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
