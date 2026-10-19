"""Native Windows ConPTY multi-client streaming, lifecycle, resize and detach."""

import os
import sys
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows required")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.platform.windows_conpty import spawn_conpty  # noqa: E402


def await_text(subscriber, text: str) -> str:
    output = ""
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        chunk, eof = subscriber.read_wait(8192, 300)
        output += chunk.decode("utf-8", errors="replace")
        if text in output:
            return output
        if eof:
            raise AssertionError(f"ConPTY closed before {text!r}: {output!r}")
    raise AssertionError(f"ConPTY output missing {text!r}: {output!r}")


def main() -> None:
    session = spawn_conpty(
        ["powershell.exe", "-NoProfile", "-NoLogo"],
        cwd=str(Path.home()),
    )
    try:
        first = session.subscribe()
        second = session.subscribe()
        session.write("Write-Output 'WINPTY_MULTI_385'\r")
        assert "WINPTY_MULTI_385" in await_text(first, "WINPTY_MULTI_385")
        assert "WINPTY_MULTI_385" in await_text(second, "WINPTY_MULTI_385")
        session.unsubscribe(first)
        session.resize(100, 30)
        session.write("Write-Output 'WINPTY_STILL_ALIVE'\r")
        assert "WINPTY_STILL_ALIVE" in await_text(second, "WINPTY_STILL_ALIVE")
        assert session.process.isalive()
        print("Windows ConPTY independent readers, detach, resize: PASS")
    finally:
        session.close()
    _, eof = second.read_wait(8192, 100)
    assert eof

    natural = spawn_conpty(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Write-Output NATURAL_EOF",
        ],
        cwd=str(Path.home()),
    )
    subscriber = natural.subscribe()
    assert "NATURAL_EOF" in await_text(subscriber, "NATURAL_EOF")
    deadline = time.monotonic() + 10
    while not natural.ended and time.monotonic() < deadline:
        time.sleep(0.05)
    assert natural.ended, "natural ConPTY process did not reach EOF"
    natural.close()
    assert natural.closed


if __name__ == "__main__":
    main()
