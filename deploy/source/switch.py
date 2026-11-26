"""Explicit staged Linux Control/Colocate/Executor source cutover; no uv sync."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from executor_trial import (
    begin_trial,
    executor_switch_lock,
    finish_trial,
    recover_trial,
)
from prepare import (
    REPOSITORY,
    _check_launcher,
    _default_root,
    _managed_release_matches,
    _python,
    _write_launcher,
)

_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def _old_python(root: Path) -> Path:
    _check_launcher(root)
    launcher = root / "bin/workgate"
    if not launcher.is_file():
        raise RuntimeError("a managed stable Workgate command is required")
    lines = launcher.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3:
        raise RuntimeError("unexpected stable launcher")
    command = shlex.split(lines[2])
    if (
        len(command) != 5
        or command[0] != "exec"
        or command[2:] != ["-m", "workgate.main", "$@"]
    ):
        raise RuntimeError("unexpected managed source launcher")
    python = Path(command[1])
    if (
        not python.is_absolute()
        or not _COMMIT.fullmatch(python.parents[2].name)
        or python != _python(root / "versions" / python.parents[2].name)
        or not python.is_file()
    ):
        raise RuntimeError("stable command does not use a managed version")
    return python


def _staged_python(root: Path, tag: str) -> Path:
    versions = root / "versions"
    for checkout in versions.iterdir():
        if (
            not _COMMIT.fullmatch(checkout.name)
            or not checkout.is_dir()
            or checkout.is_symlink()
        ):
            continue
        if not _managed_release_matches(checkout, tag, REPOSITORY, _run):
            continue
        if _run(
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ):
            raise RuntimeError("refusing modified staged release checkout")
        python = _python(checkout)
        if (
            not (checkout / "uv.lock").is_file()
            or not python.is_file()
            or _run(
                str(python), "-m", "workgate.main", "--version", cwd=checkout
            )
            != f"workgate {tag[1:]}"
        ):
            raise RuntimeError("staged runtime is not ready; prepare it first")
        return python
    raise RuntimeError(
        f"no prepared source release for {tag}; run prepare first"
    )


def _native(role: str) -> dict[str, str]:
    output = _run(
        "systemctl",
        "--user",
        "show",
        f"workgate-{role}.service",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=FragmentPath",
        "--property=ExecStart",
    )
    return dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )


def _command(
    python: Path, role: str, control_config: Path, executor_config: Path | None
) -> list[str]:
    command = [str(python), "-m", "workgate.main", role]
    if role == "control":
        command.extend(["--config", str(control_config)])
    else:
        assert executor_config is not None
        command.extend(
            [
                "--control-config",
                str(control_config),
                "--executor-config",
                str(executor_config),
            ]
        )
    return command


def _matches(native: dict[str, str], unit: Path, command: list[str]) -> bool:
    expected = f"{{ path={command[0]} ; argv[]={' '.join(command)} ; ignore_errors=no ; "
    actual = native.get("ExecStart", "")
    return (
        native.get("LoadState") == "loaded"
        and native.get("ActiveState") == "active"
        and native.get("FragmentPath") == str(unit)
        and actual.startswith(expected)
        and actual.count("{ path=") == 1
    )


def _install(
    python: Path, role: str, control_config: Path, executor_config: Path | None
) -> None:
    command = _command(python, role, control_config, executor_config)
    command.insert(4, "install-service")
    _run(*command, cwd=python.parents[2])


def _executor_status(python: Path, config: Path) -> dict[str, str]:
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
                raise RuntimeError("ambiguous native Executor service status")
            result[key] = value
    return result


def _executor_binding(
    python: Path, config: Path, *, current: bool
) -> tuple[Path, Path]:
    status = _executor_status(python, config)
    if (
        status.get("State") != "running"
        or status.get("Backend") != "systemd"
        or status.get("Runtime owner") != "source"
        or (current and status.get("Runtime") != "current")
    ):
        raise RuntimeError(
            "Executor native service is not a running managed source runtime"
        )
    service = Path(status.get("Service", ""))
    log = Path(status.get("Log", ""))
    directory = log.parent
    if (
        not service.is_absolute()
        or service.name != "workgate-executor.service"
        or not service.is_file()
        or directory.name != "service"
        or directory.parent.name != "executor"
        or log.name != "executor.log"
    ):
        raise RuntimeError("unexpected Executor native service or state paths")
    profile = directory.parent / "profile.json"
    if not profile.is_file():
        raise RuntimeError("Executor pairing identity is missing")
    launcher = directory / "launcher.py"
    if not launcher.is_file():
        raise RuntimeError("managed Executor launcher is missing")
    expected = [str(python), str(launcher.resolve())]
    if not _matches(_native("executor"), service, expected):
        raise RuntimeError(
            "Executor loaded native command does not match the managed Python"
        )
    return service, profile


def _controller_poll_confirmed(
    python: Path, profile: Path, started_at: float
) -> bool:
    """Read the candidate's private delivery evidence, not its Hello alone."""
    path = profile.with_name("connection.json")
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as file:
            raw = file.read(1025)
        if len(raw) > 1024:
            return False
        report = json.loads(raw)
    except OSError, UnicodeError, ValueError:
        return False
    if not isinstance(report, dict):
        return False
    runtime_id = hashlib.sha256(
        os.path.normcase(str(python.absolute().parent)).encode("utf-8")
    ).hexdigest()
    poll_at = report.get("last_poll_at")
    updated_at = report.get("updated_at")
    now = time.time()
    return (
        report.get("runtime_id") == runtime_id
        and report.get("phase") == "connected"
        and type(report.get("process_id")) is int
        and report["process_id"] > 0
        and isinstance(poll_at, (int, float))
        and not isinstance(poll_at, bool)
        and isinstance(updated_at, (int, float))
        and not isinstance(updated_at, bool)
        and started_at <= poll_at <= updated_at <= now
        and now - updated_at <= 90
    )


def _wait_for_controller_poll(
    python: Path, profile: Path, started_at: float
) -> None:
    """Bound candidate acceptance; the caller owns the native rollback."""
    deadline = time.monotonic() + 90
    while not _controller_poll_confirmed(python, profile, started_at):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "candidate Executor did not complete a Control poll"
            )
        time.sleep(0.5)


def _switch_executor(
    root: Path,
    old: Path,
    new: Path,
    tag: str,
    config: Path,
    *,
    require_controller_poll: bool = False,
) -> None:
    unit, profile = _executor_binding(old, config, current=True)
    # Check that the new CLI resolves the same persistent service and state.
    prospective = _executor_status(new, config)
    if (
        prospective.get("State") != "running"
        or prospective.get("Service") != str(unit)
        or prospective.get("Log")
        != str(profile.parent / "service" / "executor.log")
        or prospective.get("Backend") != "systemd"
        or prospective.get("Runtime owner") != "source"
    ):
        raise RuntimeError(
            "staged Executor resolves a different service or state directory"
        )
    identity = profile.read_bytes()
    if old == new:
        print(f"Executor is already running {tag}; no service restart")
        return

    def install(python: Path) -> None:
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

    started_at = time.time()
    if require_controller_poll:
        begin_trial(root, old, new, config, profile, started_at)
    try:
        install(new)
        if _executor_binding(new, config, current=True) != (unit, profile):
            raise RuntimeError(
                "new Executor bound to another service or identity directory"
            )
        if require_controller_poll:
            _wait_for_controller_poll(new, profile, started_at)
            if _executor_binding(new, config, current=True) != (unit, profile):
                raise RuntimeError(
                    "candidate Executor binding changed during poll verification"
                )
        if profile.read_bytes() != identity:
            raise RuntimeError(
                "Executor pairing identity changed during cutover"
            )
        _write_launcher(root, new)
    except Exception as exc:
        try:
            install(old)
            if _executor_binding(old, config, current=True) != (unit, profile):
                raise RuntimeError(
                    "old Executor bound to another service or identity directory"
                )
            if profile.read_bytes() != identity:
                raise RuntimeError(
                    "Executor pairing identity changed; recovery cannot confirm it"
                )
            _write_launcher(root, old)
            if require_controller_poll:
                finish_trial(root)
        except Exception as recovery:
            raise RuntimeError(
                f"Executor cutover failed: {exc}; recovery also failed: {recovery}"
            ) from recovery
        raise RuntimeError(
            f"Executor cutover failed, previous service restored: {exc}"
        ) from exc
    if require_controller_poll:
        finish_trial(root)
    print(
        f"Executor service switched to {tag}; previous runtime retained for rollback"
    )


def switch(
    *,
    root: Path,
    tag: str,
    control_config: Path | None = None,
    role: str = "control",
    executor_config: Path | None = None,
    require_controller_poll: bool = False,
) -> None:
    if sys.platform != "linux":
        raise RuntimeError(
            "this explicit service cutover currently supports Linux only"
        )
    if role not in {"control", "colocate", "executor"}:
        raise ValueError(
            "explicit Linux cutover supports Control, Colocate and Executor"
        )
    if require_controller_poll and role != "executor":
        raise ValueError(
            "Control poll verification requires a standalone Executor"
        )
    if not _TAG.fullmatch(tag):
        raise ValueError("select an explicit released version tag")
    if any(name.startswith("WORKGATE_") for name in os.environ):
        raise RuntimeError(
            "save Workgate settings in YAML, not environment overrides"
        )
    root = root.expanduser().absolute()
    config = control_config.expanduser().absolute() if control_config else None
    paired_config = (
        executor_config.expanduser().absolute() if executor_config else None
    )
    if role == "executor":
        if config is not None:
            raise ValueError(
                "Control config does not apply to standalone Executor"
            )
        if paired_config is None or not paired_config.is_file():
            raise RuntimeError(
                "Executor cutover requires durable Executor YAML config"
            )
        with executor_switch_lock(root):
            old_python = _old_python(root)
            new_python = _staged_python(root, tag)
            if recover_trial(
                root,
                old_python,
                new_python,
                paired_config,
                binding=lambda python: _executor_binding(
                    python, paired_config, current=True
                )[1],
                wait_for_poll=_wait_for_controller_poll,
                publish=_write_launcher,
            ):
                print(
                    f"Executor interrupted cutover recovered at {tag}; no service restart"
                )
                return
            _switch_executor(
                root,
                old_python,
                new_python,
                tag,
                paired_config,
                require_controller_poll=require_controller_poll,
            )
        return
    if config is None or not config.is_file():
        raise RuntimeError(f"missing durable Control config: {config}")
    if role == "colocate" and (
        paired_config is None or not paired_config.is_file()
    ):
        raise RuntimeError(
            "Colocate cutover requires an existing durable Executor config"
        )
    if role == "control" and paired_config is not None:
        raise ValueError("Executor config applies only to Colocate cutover")

    old_python = _old_python(root)
    new_python = _staged_python(root, tag)
    old_command = _command(old_python, role, config, paired_config)
    new_command = _command(new_python, role, config, paired_config)

    native = _native(role)
    fragment = native.get("FragmentPath", "")
    unit = Path(fragment) if fragment else None
    marker = f"# Managed by workgate {role} install-service"
    if (
        unit is None
        or not unit.is_file()
        or unit.is_symlink()
        or unit.parent.name != "user"
        or unit.parent.parent.name != "systemd"
        or not unit.read_text(encoding="utf-8").startswith(marker)
        or not _matches(native, unit, old_command)
    ):
        raise RuntimeError(
            f"existing {role} service binding is not the managed version"
        )

    if old_python == new_python:
        print(
            f"{role.capitalize()} is already running {tag}; no service restart"
        )
        return

    if (
        role == "control"
        and not (
            new_python.parents[2] / "src/workgate/control/service.py"
        ).is_file()
    ):
        raise RuntimeError(
            "staged Control release lacks native service installation support"
        )

    try:
        _install(new_python, role, config, paired_config)
        if not _matches(_native(role), unit, new_command):
            raise RuntimeError(
                f"new {role} native service did not start with the staged runtime"
            )
        _write_launcher(root, new_python)
    except Exception as exc:
        try:
            _install(old_python, role, config, paired_config)
            if not _matches(_native(role), unit, old_command):
                raise RuntimeError(
                    f"old {role} runtime is not active after recovery"
                )
            _write_launcher(root, old_python)
        except Exception as recovery:
            raise RuntimeError(
                f"{role.capitalize()} cutover failed: {exc}; recovery also failed: {recovery}"
            ) from recovery
        raise RuntimeError(
            f"{role.capitalize()} cutover failed, previous service restored: {exc}"
        ) from exc
    print(
        f"{role.capitalize()} service switched to {tag}; previous runtime retained for rollback"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", choices=["control", "colocate", "executor"], required=True
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--install-root", type=Path, default=_default_root())
    parser.add_argument("--control-config", type=Path)
    parser.add_argument("--executor-config", type=Path)
    parser.add_argument(
        "--require-controller-poll", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    try:
        switch(
            root=args.install_root,
            tag=args.tag,
            control_config=args.control_config,
            role=args.role,
            executor_config=args.executor_config,
            require_controller_poll=args.require_controller_poll,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(1, f"Workgate source cutover failed: {exc}\n")


if __name__ == "__main__":
    main()
