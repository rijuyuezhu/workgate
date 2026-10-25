"""Explicitly switch one staged Linux Control source service; no uv sync."""

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from prepare import (
    REPOSITORY,
    _check_launcher,
    _default_root,
    _python,
    _write_launcher,
)

_SERVICE = "workgate-control.service"
_MARKER = "# Managed by workgate control install-service"
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
        try:
            if (
                _run("git", "-C", str(checkout), "remote", "get-url", "origin")
                != REPOSITORY
                or _run("git", "-C", str(checkout), "rev-parse", "HEAD")
                != checkout.name
                or _run(
                    "git",
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--verify",
                    f"refs/tags/{tag}^{{commit}}",
                )
                != checkout.name
            ):
                continue
        except subprocess.CalledProcessError:
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


def _native() -> dict[str, str]:
    output = _run(
        "systemctl",
        "--user",
        "show",
        _SERVICE,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=FragmentPath",
        "--property=ExecStart",
    )
    return dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )


def _matches(
    native: dict[str, str], unit: Path, python: Path, config: Path
) -> bool:
    command = [
        str(python),
        "-m",
        "workgate.main",
        "control",
        "--config",
        str(config),
    ]
    expected = f"{{ path={command[0]} ; argv[]={' '.join(command)} ; ignore_errors=no ; "
    actual = native.get("ExecStart", "")
    return (
        native.get("LoadState") == "loaded"
        and native.get("ActiveState") == "active"
        and native.get("FragmentPath") == str(unit)
        and actual.startswith(expected)
        and actual.count("{ path=") == 1
    )


def _install(python: Path, config: Path) -> None:
    _run(
        str(python),
        "-m",
        "workgate.main",
        "control",
        "install-service",
        "--config",
        str(config),
        cwd=python.parents[2],
    )


def switch(*, root: Path, tag: str, control_config: Path) -> None:
    if sys.platform != "linux":
        raise RuntimeError(
            "this explicit service cutover currently supports Linux only"
        )
    if not _TAG.fullmatch(tag):
        raise ValueError("select an explicit released version tag")
    if any(name.startswith("WORKGATE_") for name in os.environ):
        raise RuntimeError(
            "save Workgate settings in YAML, not environment overrides"
        )
    root = root.expanduser().absolute()
    config = control_config.expanduser().absolute()
    if not config.is_file():
        raise RuntimeError(f"missing durable Control config: {config}")
    old_python = _old_python(root)
    new_python = _staged_python(root, tag)

    native = _native()
    fragment = native.get("FragmentPath", "")
    unit = Path(fragment) if fragment else None
    if (
        unit is None
        or not unit.is_file()
        or unit.is_symlink()
        or unit.parent.name != "user"
        or unit.parent.parent.name != "systemd"
        or not unit.read_text(encoding="utf-8").startswith(_MARKER)
        or not _matches(native, unit, old_python, config)
    ):
        raise RuntimeError(
            "existing Control service binding is not the managed version"
        )

    if old_python == new_python:
        print(f"Control is already running {tag}; no service restart")
        return

    try:
        _install(new_python, config)
        if not _matches(_native(), unit, new_python, config):
            raise RuntimeError(
                "new Control native service did not start with the staged runtime"
            )
        _write_launcher(root, new_python)
    except Exception as exc:
        try:
            _install(old_python, config)
            if not _matches(_native(), unit, old_python, config):
                raise RuntimeError(
                    "old Control runtime is not active after recovery"
                )
            _write_launcher(root, old_python)
        except Exception as recovery:
            raise RuntimeError(
                f"Control cutover failed: {exc}; recovery also failed: {recovery}"
            ) from recovery
        raise RuntimeError(
            f"Control cutover failed, previous service restored: {exc}"
        ) from exc
    print(
        f"Control service switched to {tag}; previous runtime retained for rollback"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["control"], required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--install-root", type=Path, default=_default_root())
    parser.add_argument("--control-config", type=Path, required=True)
    args = parser.parse_args()
    try:
        switch(
            root=args.install_root,
            tag=args.tag,
            control_config=args.control_config,
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
