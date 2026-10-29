"""Replace one owned macOS Executor LaunchAgent with a prepared source release."""

import argparse
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from switch import _old_python, _staged_python, _write_launcher

_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?\Z")
_LABEL = "com.workgate.executor"


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, capture_output=True, timeout=120
    ).stdout.strip()


def _status(python: Path, config: Path) -> dict[str, str]:
    output = _run(
        str(python),
        "-m",
        "workgate.main",
        "executor",
        "status",
        "--config",
        str(config),
        cwd=python.parents[2],
    )
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in {
            "State",
            "Backend",
            "Service",
            "Log",
            "Runtime owner",
            "Runtime",
        }:
            if key in result:
                raise RuntimeError("ambiguous Executor native status")
            result[key] = value
    return result


def _binding(python: Path, config: Path, *, current: bool) -> Path:
    status = _status(python, config)
    if (
        status.get("State") != "running"
        or status.get("Backend") != "launchd"
        or status.get("Runtime owner") != "source"
        or (current and status.get("Runtime") != "current")
    ):
        raise RuntimeError("Executor is not a running managed LaunchAgent")
    plist = Path(status.get("Service", ""))
    log = Path(status.get("Log", ""))
    directory = log.parent
    if (
        not plist.is_absolute()
        or plist.is_symlink()
        or plist.name != _LABEL + ".plist"
        or not plist.is_file()
        or not log.is_absolute()
        or log.name != "executor.log"
        or directory.name != "service"
        or directory.parent.name != "executor"
    ):
        raise RuntimeError(
            "unrecognized Executor LaunchAgent or state directory"
        )
    launcher = directory / "launcher.py"
    profile = directory.parent / "profile.json"
    if (
        launcher.is_symlink()
        or not launcher.is_file()
        or profile.is_symlink()
        or not profile.is_file()
    ):
        raise RuntimeError("Executor pairing profile or launcher missing")
    payload = plistlib.loads(plist.read_bytes())
    if not isinstance(payload, dict) or (
        payload.get("Label") != _LABEL
        or payload.get("ProgramArguments") != [str(python), str(launcher)]
    ):
        raise RuntimeError(
            "native LaunchAgent action does not match managed Python"
        )
    # The *loaded* native definition must be the expected plist, not a
    # foreign job with the same label. A saved file alone is insufficient.
    loaded = _run("launchctl", "print", f"gui/{os.getuid()}/{_LABEL}")
    fields = {}
    for line in loaded.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in {"path", "state", "program"}:
            fields[key] = value
    if (
        fields.get("path") != str(plist)
        or fields.get("state") != "running"
        or fields.get("program") != str(python)
    ):
        raise RuntimeError("loaded LaunchAgent does not match owned runtime")
    return profile


def _wait_for_binding(python: Path, config: Path) -> Path:
    """Allow launchd a brief window to start a freshly bootstrapped agent."""
    for _ in range(19):
        try:
            return _binding(python, config, current=True)
        except RuntimeError:
            time.sleep(0.25)
    return _binding(python, config, current=True)


def _install(python: Path, config: Path) -> None:
    _run(
        str(python),
        "-m",
        "workgate.main",
        "executor",
        "install-service",
        "--config",
        str(config),
        cwd=python.parents[2],
    )


def switch(*, root: Path, tag: str, config: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("macOS LaunchAgent cutover requires macOS")
    if not _TAG.fullmatch(tag):
        raise ValueError("select a published Workgate release tag")
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "save Workgate settings in YAML, not environment overrides"
        )
    root = root.expanduser().absolute()
    config = config.expanduser().absolute()
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("managed Executor YAML config missing")

    old = _old_python(root)
    new = _staged_python(root, tag)
    profile = _binding(old, config, current=True)
    identity = profile.read_bytes()
    if old == new:
        print(f"Executor is already running {tag}; no restart")
        return
    before = _status(old, config)
    candidate = _status(new, config)
    if any(candidate.get(key) != before.get(key) for key in ("Service", "Log")):
        raise RuntimeError("staged Executor resolves a different native state")
    try:
        _install(new, config)
        if _wait_for_binding(new, config) != profile:
            raise RuntimeError("new Executor service uses a different identity")
        if profile.read_bytes() != identity:
            raise RuntimeError(
                "Executor pairing identity changed during cutover"
            )
        _write_launcher(root, new)
    except Exception as exc:
        try:
            _install(old, config)
            if _wait_for_binding(old, config) != profile:
                raise RuntimeError("old Executor native service not restored")
            if profile.read_bytes() != identity:
                raise RuntimeError(
                    "Executor pairing identity changed; recovery unsafe"
                )
            _write_launcher(root, old)
        except Exception as recovery:
            raise RuntimeError(
                f"Mac Executor cutover failed; recovery also failed: {type(recovery).__name__}"
            ) from recovery
        raise RuntimeError(
            "Mac Executor cutover failed; old service restored"
        ) from exc
    print(
        f"Executor LaunchAgent switched to {tag}; old source retained for rollback"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--executor-config", type=Path, required=True)
    args = parser.parse_args()
    try:
        switch(
            root=args.install_root, tag=args.tag, config=args.executor_config
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(1, f"Workgate macOS source cutover failed: {exc}\n")


if __name__ == "__main__":
    main()
