#!/usr/bin/env python3
"""Smoke the real frozen executable, including a short-lived HTTP control host."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise SystemExit(f"missing standalone executable: {binary}")

    for args in (("--version",), ("version",), ("--help",)):
        subprocess.run(
            [str(binary), *args],
            check=True,
            timeout=45,
            stdout=subprocess.DEVNULL,
        )

    with tempfile.TemporaryDirectory(
        prefix="workgate-binary-smoke-"
    ) as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update(
            WORKGATE_AUTH_MODE="none",
            WORKGATE_HOST="127.0.0.1",
            WORKGATE_PORT=str(port),
            WORKGATE_STATE_DIR=str(root / "state"),
            WORKGATE_DATA_DIR=str(root / "data"),
            WORKGATE_DEFAULT_WORKDIR=str(root),
            WORKGATE_AGENT_BRIDGE_ENABLED="false",
        )
        log = root / "control.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [str(binary), "control", "--mode", "http"],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/healthz", timeout=2
                        ) as response:
                            if (
                                response.status == 200
                                and response.read() == b'{"ok":true}'
                            ):
                                break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError(
                        "frozen HTTP control did not become healthy"
                    )
                if process.poll() is not None:
                    raise RuntimeError(
                        "frozen HTTP control exited before health check"
                    )
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/ui", timeout=5
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            "frozen WebUI did not serve HTTP 200"
                        )
            except Exception:
                output.flush()
                print(log.read_text(errors="replace")[-12000:], file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("Standalone executable CLI, control health, and WebUI smoke passed")


if __name__ == "__main__":
    main()
