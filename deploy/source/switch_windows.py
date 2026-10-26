"""Explicit Windows Control/Colocate switch between prepared source releases."""

import argparse
import json
import ntpath
import os
import re
import subprocess
import sys
from pathlib import Path

from prepare import _check_launcher, _default_root, _write_launcher
from switch import _staged_python

_TASKS = {
    "control": ("Workgate Control", "Managed by Workgate: control"),
    "colocate": ("Workgate Colocate", "Managed by Workgate: colocate"),
}
_LAUNCHER = re.compile(r'^"%~dp0([^"]+)" -m workgate\.main %\*$')


def _same_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(
        ntpath.normpath(b)
    )


def _old_python(root: Path) -> Path:
    _check_launcher(root)
    launcher = root / "bin/workgate.cmd"
    if not launcher.is_file():
        raise RuntimeError("managed source command is missing")
    lines = launcher.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3:
        raise RuntimeError("unexpected managed Windows launcher")
    match = _LAUNCHER.fullmatch(lines[2])
    if match is None:
        raise RuntimeError("unexpected managed Windows launcher")
    path = ntpath.normpath(ntpath.join(str(launcher.parent), match[1]))
    candidate = Path(path)
    if (
        not candidate.is_file()
        or candidate.name.lower() != "python.exe"
        or candidate.parent.name.lower() != "scripts"
        or candidate.parent.parent.name != ".venv"
        or candidate.parent.parent.parent.parent != root / "versions"
        or re.fullmatch(r"[0-9a-f]{40}", candidate.parent.parent.parent.name)
        is None
    ):
        raise RuntimeError("stable command is not a managed release Python")
    return candidate


def _native(python: Path, role: str) -> dict:
    """Use the existing WindowsUserTask ownership and native action query."""
    name, description = _TASKS[role]
    code = (
        "import json,subprocess;"
        "from workgate.platform.windows_task import WindowsUserTask;"
        f"t=WindowsUserTask({name!r},description={description!r},"
        "run=lambda a:subprocess.run(a,check=True,capture_output=True,text=True,"
        "timeout=30));"
        "print(json.dumps(t.status()))"
    )
    output = subprocess.run(
        [str(python), "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        cwd=python.parents[2],
    ).stdout
    result = json.loads(output)
    if not isinstance(result, dict) or not isinstance(result.get("state"), str):
        raise RuntimeError(
            "Windows native owned task is missing or unrecognized"
        )
    return result


def _binding(
    native: dict,
    python: Path,
    role: str,
    directory: Path,
    control_config: Path,
    executor_config: Path | None,
) -> bool:
    action = native.get("action")
    launcher = directory / "launcher.py"
    if role == "control":
        entry = f"main(['control', '--config', {str(control_config)!r}])"
    else:
        assert executor_config is not None
        entry = (
            "main("
            + repr(
                [
                    "colocate",
                    "--control-config",
                    str(control_config),
                    "--executor-config",
                    str(executor_config),
                ]
            )
            + ")"
        )
    return (
        native.get("state", "").lower() == "running"
        and isinstance(action, dict)
        and launcher.is_file()
        and entry in launcher.read_text(encoding="utf-8")
        and _same_path(
            str(action.get("execute", "")), str(python.with_name("pythonw.exe"))
        )
        and action.get("arguments") == subprocess.list2cmdline([str(launcher)])
        and _same_path(str(action.get("working_directory", "")), str(directory))
    )


def _install(
    python: Path, role: str, control_config: Path, executor_config: Path | None
) -> None:
    command = [str(python), "-m", "workgate.main", role, "install-service"]
    if role == "control":
        command += ["--config", str(control_config)]
    else:
        assert executor_config is not None
        command += [
            "--control-config",
            str(control_config),
            "--executor-config",
            str(executor_config),
        ]
    subprocess.run(command, check=True, cwd=python.parents[2], timeout=120)


def _stop(python: Path, role: str) -> None:
    subprocess.run(
        [str(python), "-m", "workgate.main", role, "stop-service"],
        check=True,
        cwd=python.parents[2],
        timeout=60,
    )


def _ensure_stopped(python: Path, role: str) -> None:
    state = _native(python, role).get("state", "").lower()
    if state in {"running", "queued", ""}:
        raise RuntimeError("previous Windows task has not stopped")


def switch(
    *,
    root: Path,
    tag: str,
    role: str,
    control_config: Path,
    executor_config: Path | None = None,
    service_dir: Path | None = None,
) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows Task Scheduler is required")
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "source cutover requires durable YAML, not WORKGATE_* overrides"
        )
    if role not in _TASKS:
        raise ValueError(
            "Windows switch currently supports Control and Colocate"
        )
    from switch import _TAG

    if not _TAG.fullmatch(tag):
        raise ValueError("select an explicit released version tag")
    root = root.expanduser().absolute()
    control_config = control_config.expanduser().absolute()
    executor_config = (
        executor_config.expanduser().absolute() if executor_config else None
    )
    if (
        not control_config.is_file()
        or (
            role == "colocate"
            and (executor_config is None or not executor_config.is_file())
        )
        or (role == "control" and executor_config is not None)
    ):
        raise RuntimeError(
            "expected unchanged durable YAML config for this role"
        )

    old = _old_python(root)
    new = _staged_python(root, tag)
    if not new.with_name("pythonw.exe").is_file():
        raise RuntimeError("prepared Windows runtime is missing pythonw.exe")

    # The directory is derived from the already installed native action; never
    # infer it from the new release or relocate saved pairing credentials.
    before = _native(old, role)
    action = before.get("action")
    if not isinstance(action, dict):
        raise RuntimeError("expected one owned native scheduled task action")
    if service_dir is None:
        service_dir = Path(str(action.get("working_directory", "")))
    if (
        not service_dir.is_absolute()
        or service_dir.name != "service"
        or service_dir.parent.name != role
        or not _binding(
            before, old, role, service_dir, control_config, executor_config
        )
    ):
        raise RuntimeError(
            "existing Windows task is not bound to the managed source runtime"
        )
    launcher = service_dir / "launcher.py"
    previous_launcher = launcher.read_bytes()

    if old == new:
        print(f"{role.capitalize()} is already running {tag}; no task restart")
        return
    try:
        _stop(old, role)
        _ensure_stopped(old, role)
        _install(new, role, control_config, executor_config)
        if not _binding(
            _native(new, role),
            new,
            role,
            service_dir,
            control_config,
            executor_config,
        ):
            raise RuntimeError(
                "new Windows scheduled task is not running the staged version"
            )
        _write_launcher(root, new)
    except Exception as exc:
        try:
            _stop(old, role)
            _ensure_stopped(old, role)
            _install(old, role, control_config, executor_config)
            if not _binding(
                _native(old, role),
                old,
                role,
                service_dir,
                control_config,
                executor_config,
            ):
                raise RuntimeError(
                    "previous Windows scheduled task did not recover"
                )
            # Reinstall writes the same generated service launcher.
            if launcher.read_bytes() != previous_launcher:
                raise RuntimeError("service launcher changed during recovery")
            _write_launcher(root, old)
        except Exception as recovery:
            raise RuntimeError(
                f"{role.capitalize()} switch failed: {exc}; recovery also failed: {recovery}"
            ) from recovery
        raise RuntimeError(
            f"{role.capitalize()} switch failed, previous task restored: {exc}"
        ) from exc
    print(
        f"{role.capitalize()} switched to {tag}; previous release retained for rollback"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=list(_TASKS))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--install-root", type=Path, default=_default_root())
    parser.add_argument("--control-config", required=True, type=Path)
    parser.add_argument("--executor-config", type=Path)
    args = parser.parse_args()
    try:
        switch(
            root=args.install_root,
            tag=args.tag,
            role=args.role,
            control_config=args.control_config,
            executor_config=args.executor_config,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(1, f"Workgate Windows source switch failed: {exc}\n")


if __name__ == "__main__":
    main()
