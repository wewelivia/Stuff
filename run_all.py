"""
MonitorHub — run all House View dashboard backends at once.

    python run_all.py                 # starts every monitor in monitors.yaml
    python run_all.py market-monitor  # start a subset by name

One console, prefixed logs, Ctrl+C stops everything. Each backend runs
in its own working directory on its own port, so the three projects
never interfere with each other (no module or config collisions).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml")
    raise SystemExit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "monitors.yaml")

COLOURS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m"]
RESET = "\033[0m"


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) != 0


def stream(proc: subprocess.Popen, prefix: str) -> None:
    for line in iter(proc.stdout.readline, b""):
        sys.stdout.write(f"{prefix} {line.decode(errors='replace')}")
        sys.stdout.flush()


def main() -> None:
    with open(CONFIG, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    default_host = cfg.get("host", "0.0.0.0")
    wanted = set(sys.argv[1:])
    monitors = [m for m in cfg.get("monitors", [])
                if not wanted or m["name"] in wanted]
    if not monitors:
        print(f"No monitors matched {wanted or 'config'} — check monitors.yaml")
        raise SystemExit(1)

    # Windows consoles need this for ANSI colours; harmless elsewhere
    os.system("")

    procs: list[tuple[str, subprocess.Popen]] = []
    for i, m in enumerate(monitors):
        name, port = m["name"], int(m["port"])
        host = m.get("host", default_host)
        directory = os.path.normpath(os.path.join(HERE, m["dir"])) \
            if not os.path.isabs(m["dir"]) else m["dir"]
        colour = COLOURS[i % len(COLOURS)]
        prefix = f"{colour}[{name:>15}]{RESET}"

        if not os.path.isdir(directory):
            print(f"{prefix} SKIPPED — directory not found: {directory}")
            continue
        if not port_free(host, port):
            print(f"{prefix} SKIPPED — port {port} already in use "
                  f"(is it running elsewhere?)")
            continue

        command = m.get("command") or [
            sys.executable, "-m", "uvicorn", m["app"],
            "--host", host, "--port", str(port)]
        proc = subprocess.Popen(
            command, cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=stream, args=(proc, prefix), daemon=True).start()
        procs.append((name, proc))
        print(f"{prefix} started on http://{host}:{port} (pid {proc.pid}, cwd {directory})")

    if not procs:
        print("Nothing started.")
        raise SystemExit(1)

    print(f"\n{len(procs)} backend(s) running — Ctrl+C stops them all. "
          f"Open hub.html for the landing page.\n")

    try:
        while True:
            time.sleep(2)
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"⚠  {name} exited with code {proc.returncode} "
                          f"(others keep running)")
                    procs.remove((name, proc))
            if not procs:
                print("All backends have exited.")
                return
    except KeyboardInterrupt:
        print("\nStopping all backends…")
        for _, proc in procs:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT
                                 if os.name == "nt" else signal.SIGINT)
            except (OSError, ValueError, AttributeError):
                proc.terminate()
        deadline = time.time() + 8
        for _, proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Done.")


if __name__ == "__main__":
    main()
