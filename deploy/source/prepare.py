"""Prepare one immutable, full-featured Workgate source runtime (stdlib only)."""

import argparse
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

REPOSITORY = "https://github.com/rijuyuezhu/workgate.git"
_LAUNCHER_MARKER = "Managed by Workgate source preparation"


def _run(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _default_root() -> Path:
    if os.name == "nt":
        return (
            Path(
                os.environ.get(
                    "LOCALAPPDATA", Path.home() / "AppData" / "Local"
                )
            )
            / "Workgate"
            / "source"
        )
    return Path.home() / ".local" / "opt" / "workgate"


def _python(checkout: Path) -> Path:
    return (
        checkout
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


def _remove_temporary_checkout(path: Path) -> None:
    # Only this invocation's scratch checkout; installed versions are untouched.
    def retry_readonly(func, name: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(name, stat.S_IWRITE)
        func(name)

    shutil.rmtree(path, onexc=retry_readonly)


def _write_launcher(root: Path, python: Path) -> Path:
    directory = root / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = directory / "workgate.cmd"
        # Keep batch file ASCII-only, including when the user's home path
        # contains non-ASCII characters; CMD expands %~dp0 itself.
        relative = os.path.relpath(python, directory)
        content = (
            f"@echo off\r\nrem {_LAUNCHER_MARKER}\r\n"
            f'"%~dp0{relative}" -m workgate.main %*\r\n'
        )
    else:
        launcher = directory / "workgate"
        content = (
            f"#!/bin/sh\n# {_LAUNCHER_MARKER}\n"
            f'exec {shlex.quote(str(python))} -m workgate.main "$@"\n'
        )
    marker = ("rem " if os.name == "nt" else "# ") + _LAUNCHER_MARKER
    if launcher.is_symlink() or (
        launcher.exists()
        and launcher.read_text(encoding="utf-8", errors="replace").splitlines()[
            :2
        ]
        != content.splitlines()[:1] + [marker]
    ):
        raise RuntimeError(
            f"refusing to replace an unmanaged launcher: {launcher}"
        )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".workgate-",
        delete=False,
    ) as output:
        staged = Path(output.name)
        output.write(content)
    try:
        if os.name != "nt":
            staged.chmod(0o700)
        os.replace(staged, launcher)
    finally:
        staged.unlink(missing_ok=True)
    return launcher


def prepare(
    *, tag: str, role: str, root: Path, repository: str = REPOSITORY
) -> Path:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?", tag):
        raise ValueError(
            "--tag must be an explicit version tag, not main or a branch"
        )
    if role not in {"control", "executor", "colocate"}:
        raise ValueError("choose one of control, executor, colocate")
    for executable in ("git", "uv"):
        if not shutil.which(executable):
            raise RuntimeError(
                f"{executable} not found; install it before running the source preparer"
            )
    root = root.expanduser().absolute()
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    _run("uv", "python", "install", "3.14", timeout=300)
    temporary = Path(tempfile.mkdtemp(prefix=".checkout-", dir=versions))
    try:
        _run(
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            tag,
            "--",
            repository,
            str(temporary),
            timeout=180,
        )
        sha = _run("git", "rev-parse", "HEAD", cwd=temporary, timeout=15)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RuntimeError("Git did not resolve a full release commit")
        tagged = _run(
            "git",
            "rev-parse",
            "--verify",
            f"refs/tags/{tag}^{{commit}}",
            cwd=temporary,
            timeout=15,
        )
        if tagged != sha:
            raise RuntimeError(
                "checkout HEAD does not match the requested release tag"
            )
        if (
            not (temporary / "uv.lock").is_file()
            or not (temporary / "pyproject.toml").is_file()
        ):
            raise RuntimeError(
                "release checkout lacks a uv lockfile or project metadata"
            )
        with (temporary / "pyproject.toml").open("rb") as metadata:
            project = tomllib.load(metadata).get("project", {})
        if (
            project.get("name") != "workgate"
            or project.get("version") != tag[1:]
        ):
            raise RuntimeError(
                "release tag does not match Workgate project metadata"
            )
        target = versions / sha
        if target.is_symlink():
            raise RuntimeError(
                f"refusing to use a symlinked release checkout: {target}"
            )
        if target.exists():
            if (
                not (target / ".git").is_dir()
                or _run(
                    "git", "remote", "get-url", "origin", cwd=target, timeout=15
                )
                != repository
                or _run("git", "rev-parse", "HEAD", cwd=target, timeout=15)
                != sha
            ):
                raise RuntimeError(
                    f"destination is not this managed version: {target}"
                )
            if _run(
                "git",
                "status",
                "--porcelain",
                "--untracked-files=normal",
                cwd=target,
                timeout=15,
            ):
                raise RuntimeError(
                    f"refusing to overwrite modified release checkout: {target}"
                )
        else:
            temporary.rename(target)
        # A failed sync leaves the previous user-facing launcher and any active
        # service unchanged. Re-running retries the same immutable checkout.
        _run(
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--extra",
            "gui",
            "--extra",
            "s3",
            "--python",
            "3.14",
            cwd=target,
        )
        python = _python(target)
        if not python.is_file():
            raise RuntimeError(
                f"uv did not create the expected interpreter: {python}"
            )
        _run(str(python), "-m", "playwright", "install", "chromium", cwd=target)
        version = _run(
            str(python), "-m", "workgate.main", "--version", cwd=target
        )
        if version != f"workgate {tag[1:]}":
            raise RuntimeError(f"installed CLI version does not match {tag}")
        launcher = _write_launcher(root, python)
        print(f"Prepared Workgate {version} ({tag}, {sha}) for {role}")
        print(f"Versioned checkout: {target}")
        print(f"Stable CLI: {launcher}")
        print(
            "No Control/Executor service, configuration or pairing identity was changed."
        )
        for tool in ("git", "tmux", "rg"):
            if not shutil.which(tool):
                print(
                    f"Host prerequisite not found: {tool} (required for corresponding executor features)"
                )
        return target
    finally:
        if temporary.exists():
            _remove_temporary_checkout(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", choices=["control", "executor", "colocate"], required=True
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Explicit GitHub release tag, e.g. v5.0.0a5",
    )
    parser.add_argument("--install-root", type=Path, default=_default_root())
    args = parser.parse_args()
    try:
        prepare(tag=args.tag, role=args.role, root=args.install_root)
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
    ) as exc:
        parser.exit(1, f"Workgate source preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
