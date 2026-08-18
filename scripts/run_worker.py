"""Run the arq worker under supervision, restarting it if it dies.

Arq does not survive losing Redis: a transient DNS failure took the worker down for real
in this project, it never came back, and the in-flight job sat at `queued` until a human
noticed. aiogram's polling reconnects on its own; arq does not.

Usage (from the repo root):
    python scripts/run_worker.py

Stops cleanly on Ctrl-C. Sleeps between restarts so a persistent failure (bad
credentials, Redis genuinely gone) doesn't spin the CPU.
"""
import subprocess
import sys
import time

WORKER_CMD = [sys.executable, "-m", "arq", "worker.worker.WorkerSettings"]
INITIAL_BACKOFF_SECONDS = 2
MAX_BACKOFF_SECONDS = 60


def main() -> None:
    backoff = INITIAL_BACKOFF_SECONDS
    restarts = 0

    while True:
        started = time.monotonic()
        print(f"[supervisor] starting worker (restart #{restarts})", flush=True)
        try:
            code = subprocess.call(WORKER_CMD)
        except KeyboardInterrupt:
            print("[supervisor] interrupted, stopping", flush=True)
            return

        uptime = time.monotonic() - started
        # A worker that ran a while then died hit something transient; reset the backoff so
        # it comes straight back. One that dies immediately is failing persistently.
        if uptime > 60:
            backoff = INITIAL_BACKOFF_SECONDS
        restarts += 1
        print(
            f"[supervisor] worker exited with code {code} after {uptime:.0f}s; "
            f"restarting in {backoff}s",
            flush=True,
        )
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            print("[supervisor] interrupted, stopping", flush=True)
            return
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
