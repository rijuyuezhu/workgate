"""Prepare a pinned source release, then initialize one native Workgate role.

The service commands run from the verified versioned .venv, not via uv run,
to avoid silently losing GUI/S3 extras when starting a service.
"""

import argparse
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from prepare import (
    _check_launcher,
    _default_root,
    _python,
    _write_launcher,
    prepare,
)


def _validate_control_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and bool(parsed.port is None or parsed.port > 0)
        )
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "--control-url must be HTTPS, or HTTP on a loopback host, "
            "with no credentials, query or fragment"
        )


def role_commands(
    *,
    role: str,
    control_url: str | None,
    name: str | None,
    control_config: Path | None,
    executor_config: Path | None,
    host: str | None,
    port: int | None,
    base_url: str | None,
) -> list[list[str]]:
    """Validate all role options *before* changing the managed runtime."""
    if role not in {"control", "executor", "colocate"}:
        raise ValueError("choose one of control, executor, colocate")
    if role != "executor" and control_url:
        raise ValueError("--control-url is only for the executor role")
    if role == "executor":
        if not control_url:
            raise ValueError("--control-url is required for executor setup")
        _validate_control_url(control_url)
    if role != "control" and any(
        setting is not None for setting in (host, port, base_url)
    ):
        raise ValueError(
            "--host, --port and --base-url are Control-only options"
        )
    if role == "control" and (executor_config or name):
        raise ValueError("--executor-config and --name are not Control options")
    if role == "executor" and control_config:
        raise ValueError("--control-config is not an Executor option")
    if role == "colocate" and control_url:
        raise ValueError("--control-url is not used for local Colocate pairing")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if (
        role == "control"
        and host not in {None, "127.0.0.1", "::1", "localhost"}
        and not base_url
    ):
        raise ValueError("non-loopback Control requires --base-url")

    if role == "control":
        init = ["control", "init-config"]
        if control_config:
            init += ["--config", str(control_config)]
        if host:
            init += ["--host", host]
        if port is not None:
            init += ["--port", str(port)]
        if base_url:
            init += ["--base-url", base_url]
        service = ["control", "install-service"]
        if control_config:
            service += ["--config", str(control_config)]
        return [init, service]

    if role == "executor":
        setup = ["executor", "setup", str(control_url)]
        if name:
            setup += ["--name", name]
        if executor_config:
            setup += ["--config", str(executor_config)]
        return [setup]

    setup = ["colocate", "setup"]
    if name:
        setup += ["--name", name]
    if control_config:
        setup += ["--control-config", str(control_config)]
    if executor_config:
        setup += ["--executor-config", str(executor_config)]
    return [setup]


def _check_native_services(
    executable: Path,
    checkout: Path,
    role: str,
    executor_config: Path | None,
) -> None:
    """Never implicitly replace an already-installed native user service."""
    roles = {
        "control": ("control", "colocate"),
        "executor": ("executor", "colocate"),
        "colocate": ("control", "executor", "colocate"),
    }[role]
    for candidate in roles:
        subcommand = "status" if candidate == "executor" else "status-service"
        argv = [str(executable), "-m", "workgate.main", candidate, subcommand]
        if candidate == "executor" and executor_config is not None:
            argv.extend(("--config", str(executor_config)))
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=checkout,
        )
        lines = result.stdout.strip().splitlines()
        first_line = lines[:1]
        prefix = (
            "State: "
            if candidate == "executor"
            else f"{candidate.capitalize()} service: "
        )
        if (
            not first_line
            or not first_line[0].startswith(prefix)
            or (candidate != "executor" and len(lines) != 1)
        ):
            raise RuntimeError(
                f"unexpected {candidate} status response; refusing service changes"
            )
        if first_line[0] != prefix + "not-installed":
            raise RuntimeError(
                f"{candidate} service is already installed; "
                "automatic reinstall/upgrade is not yet safe. "
                "Use the existing role service commands explicitly."
            )


def install(
    *,
    role: str,
    tag: str,
    root: Path,
    control_url: str | None = None,
    name: str | None = None,
    control_config: Path | None = None,
    executor_config: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    base_url: str | None = None,
    expected_commit: str | None = None,
) -> Path:
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "Workgate source install requires durable YAML, not WORKGATE_* "
            "environment overrides"
        )
    commands = role_commands(
        role=role,
        control_url=control_url,
        name=name,
        control_config=control_config,
        executor_config=executor_config,
        host=host,
        port=port,
        base_url=base_url,
    )
    checkout = prepare(
        tag=tag,
        role=role,
        root=root,
        activate_launcher=False,
        expected_commit=expected_commit,
    )
    executable = _python(checkout)
    # Do not complete pairing or install a service if its stable command
    # would be blocked by an unrelated existing launcher.
    _check_launcher(root.expanduser().absolute())
    _check_native_services(executable, checkout, role, executor_config)
    for command in commands:
        subprocess.run(
            [str(executable), "-m", "workgate.main", *command],
            check=True,
            cwd=checkout,
        )
    launcher = _write_launcher(root.expanduser().absolute(), executable)
    print(
        f"Workgate {role} initialized using verified source checkout: {checkout}"
    )
    print(f"Stable CLI: {launcher}")
    return checkout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", choices=["control", "executor", "colocate"], required=True
    )
    parser.add_argument(
        "--tag", required=True, help="Explicit released Workgate Git tag"
    )
    parser.add_argument("--install-root", type=Path, default=_default_root())
    parser.add_argument(
        "--expected-commit", help="Full pinned release commit SHA"
    )
    parser.add_argument(
        "--control-url", help="Executor-only: Control URL for approval"
    )
    parser.add_argument("--name", help="Executor/Colocate name")
    parser.add_argument("--control-config", type=Path)
    parser.add_argument("--executor-config", type=Path)
    parser.add_argument("--host", help="Control HTTP bind address")
    parser.add_argument("--port", type=int, help="Control HTTP port")
    parser.add_argument(
        "--base-url", help="Public URL for Control OAuth metadata"
    )
    args = parser.parse_args()
    try:
        install(
            role=args.role,
            tag=args.tag,
            root=args.install_root,
            control_url=args.control_url,
            name=args.name,
            control_config=args.control_config,
            executor_config=args.executor_config,
            host=args.host,
            port=args.port,
            base_url=args.base_url,
            expected_commit=args.expected_commit,
        )
    except (
        subprocess.SubprocessError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        parser.exit(1, f"Workgate {args.role} installation failed: {exc}\n")


if __name__ == "__main__":
    main()
