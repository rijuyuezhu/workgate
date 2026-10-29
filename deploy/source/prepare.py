"""Prepare one immutable, full-featured Workgate source runtime (stdlib only)."""

import argparse
import hashlib
import json
import os
import platform
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPOSITORY = "https://github.com/rijuyuezhu/workgate.git"
_LAUNCHER_MARKER = "Managed by Workgate source preparation"


def _run(
    *args: str,
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _managed_release_matches(
    checkout: Path,
    tag: str,
    repository: str,
    run: Callable[..., str],
    *,
    commit: str | None = None,
) -> bool:
    """Verify an existing checkout's origin, HEAD and exact release tag."""
    expected = commit or checkout.name
    try:
        return (
            run("git", "remote", "get-url", "origin", cwd=checkout)
            == repository
            and run("git", "rev-parse", "HEAD", cwd=checkout) == expected
            and run(
                "git",
                "rev-parse",
                "--verify",
                f"refs/tags/{tag}^{{commit}}",
                cwd=checkout,
            )
            == expected
        )
    except subprocess.CalledProcessError:
        return False


def _arm64_static_openssl_env(
    *, host_os: str | None = None, machine: str | None = None
) -> dict[str, str] | None:
    """Native win_arm64 cryptography requires a static OpenSSL source build.

    Its newer releases lack win_arm64 wheels. Dynamic OpenSSL builds can
    conflict with the different OpenSSL runtime bundled with uv CPython.
    """
    if (host_os or os.name) != "nt" or (
        machine or platform.machine()
    ).lower() not in {"arm64", "aarch64"}:
        return None

    openssl = os.environ.get("OPENSSL_DIR")
    if openssl is None:
        # Scoop uses a shim on PATH, not the OpenSSL SDK's real bin directory.
        scoop = Path.home() / "scoop/apps/openssl-lts/current"
        if (scoop / "include/openssl/ssl.h").is_file():
            openssl = str(scoop)
        else:
            executable = shutil.which("openssl")
            if executable:
                openssl = str(Path(executable).resolve().parent.parent)
    if not openssl:
        raise RuntimeError(
            "Windows ARM64 requires an OpenSSL 3 ARM64 SDK with static "
            "libraries (e.g. Scoop openssl-lts), Rust and MSVC; "
            "set OPENSSL_DIR to the OpenSSL installation"
        )
    root = Path(openssl).expanduser().resolve()
    native_libs = root / "lib/VC/arm64/MD"
    library = (
        Path(os.environ.get("OPENSSL_LIB_DIR", str(native_libs)))
        .expanduser()
        .resolve()
    )
    # Scoop publishes OPENSSL_LIB_DIR=.../lib, while ARM64 static .lib
    # files are under the VC/arm64/MD subdirectory of that same SDK.
    if not all(
        (library / name).is_file()
        for name in ("libssl_static.lib", "libcrypto_static.lib")
    ) and all(
        (native_libs / name).is_file()
        for name in ("libssl_static.lib", "libcrypto_static.lib")
    ):
        library = native_libs
    headers = (
        Path(os.environ.get("OPENSSL_INCLUDE_DIR", str(root / "include")))
        .expanduser()
        .resolve()
    )
    required = (
        library / "libssl_static.lib",
        library / "libcrypto_static.lib",
        headers / "openssl/ssl.h",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError(
            "Windows ARM64 needs ARM64 OpenSSL static libraries and headers; "
            f"missing: {', '.join(str(p) for p in required if not p.is_file())}. "
            "Configure OPENSSL_DIR / OPENSSL_LIB_DIR / OPENSSL_INCLUDE_DIR"
        )
    version_header = headers / "openssl/opensslv.h"
    if not version_header.is_file():
        raise RuntimeError(
            "Windows ARM64 cannot verify the OpenSSL SDK version: "
            f"missing {version_header}"
        )
    version_source = version_header.read_text(encoding="utf-8")
    major = re.search(
        r"^#\s*define\s+OPENSSL_VERSION_MAJOR\s+(\d+)\b", version_source, re.M
    )
    minor = re.search(
        r"^#\s*define\s+OPENSSL_VERSION_MINOR\s+(\d+)\b", version_source, re.M
    )
    if not major or not minor or (int(major[1]), int(minor[1])) < (3, 5):
        raise RuntimeError(
            "Windows ARM64 static cryptography requires a supported OpenSSL "
            "SDK (at least 3.5, preferably OpenSSL 3.5 LTS); "
            "OpenSSL 3.0 reached end of security support in September 2026"
        )
    env = {
        **os.environ,
        "OPENSSL_DIR": str(root),
        "OPENSSL_LIB_DIR": str(library),
        "OPENSSL_INCLUDE_DIR": str(headers),
        "OPENSSL_STATIC": "1",
        "OPENSSL_LIBS": "libssl_static:libcrypto_static",
    }
    if shutil.which("cargo") is None:
        # Scoop rustup puts Cargo in its persistent home, not always on PATH.
        rustup = Path.home() / "scoop/persist/rustup"
        cargo_dir = rustup / ".cargo/bin"
        if not (cargo_dir / "cargo.exe").is_file():
            raise RuntimeError(
                "Windows ARM64 needs native Rust/Cargo; install rustup "
                "and select the aarch64-pc-windows-msvc toolchain"
            )
        env["CARGO_HOME"] = str(rustup / ".cargo")
        env["RUSTUP_HOME"] = str(rustup / ".rustup")
        env["PATH"] = str(cargo_dir) + os.pathsep + env.get("PATH", "")
    if shutil.which("link") is None:
        raise RuntimeError(
            "Windows ARM64 needs an initialized native MSVC build shell; "
            "run vcvarsall.bat arm64 before the installer"
        )
    return env


def _native_arm64() -> bool:
    # Emulated x86/x64 processes on Windows ARM64 report their own ISA.
    return os.name == "nt" and (
        platform.machine().lower() in {"arm64", "aarch64"}
        or os.environ.get("PROCESSOR_ARCHITEW6432", "").lower() == "arm64"
    )


_ARM64_CRYPTO_WHEEL = "cryptography-50.0.2-cp314-abi3-win_arm64.whl"


def _stage_arm64_wheel(source: str, sha256: str, directory: Path) -> Path:
    """Accept only an explicitly pinned ARM64 wheel, never a floating index."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("ARM64 wheel SHA256 must be 64 lowercase hex digits")
    destination = directory / _ARM64_CRYPTO_WHEEL
    if "://" in source:
        parsed = urllib.parse.urlsplit(source)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or Path(parsed.path).name != _ARM64_CRYPTO_WHEEL
        ):
            raise ValueError(
                "ARM64 wheel URL must be explicit pinned HTTPS Wheel"
            )
        with urllib.request.urlopen(source, timeout=60) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise RuntimeError("ARM64 wheel redirected outside HTTPS")
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    else:
        path = Path(source).expanduser().resolve()
        if path.name != _ARM64_CRYPTO_WHEEL or not path.is_file():
            raise ValueError(
                "ARM64 wheel must be a local cryptography 50.0.2 win_arm64 wheel"
            )
        shutil.copyfile(path, destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != sha256:
        raise RuntimeError(
            "ARM64 wheel SHA256 mismatch; refusing dependency installation"
        )
    verify = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts/release/verify_arm64_crypto_wheel.py"
        )
    )["verify_wheel"]
    verify(destination)
    return destination


def _release_arm64_wheel(tag: str) -> tuple[str, str]:
    """Resolve the exact versioned GitHub release asset and GitHub SHA256."""
    endpoint = (
        f"https://api.github.com/repos/rijuyuezhu/workgate/releases/tags/{tag}"
    )
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release.get("tag_name") != tag:
        raise RuntimeError("GitHub release tag mismatch")
    assets = [
        item
        for item in release.get("assets", [])
        if item.get("name") == _ARM64_CRYPTO_WHEEL
    ]
    if len(assets) != 1:
        raise RuntimeError(
            f"GitHub Release {tag} has no unique native ARM64 cryptography Wheel"
        )
    asset = assets[0]
    source = asset.get("browser_download_url")
    expected = (
        f"https://github.com/rijuyuezhu/workgate/releases/download/"
        f"{tag}/{_ARM64_CRYPTO_WHEEL}"
    )
    digest = asset.get("digest")
    if (
        source != expected
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        raise RuntimeError(
            "GitHub Release ARM64 Wheel URL or SHA256 is invalid"
        )
    return source, digest.removeprefix("sha256:")


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


def _check_launcher(root: Path) -> None:
    """Fail before role setup if the stable command belongs to another tool."""
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    marker = ("rem " if os.name == "nt" else "# ") + _LAUNCHER_MARKER
    header = "@echo off" if os.name == "nt" else "#!/bin/sh"
    if launcher.is_symlink() or (
        launcher.exists()
        and (
            not launcher.is_file()
            or launcher.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[:2]
            != [header, marker]
        )
    ):
        raise RuntimeError(
            f"refusing to replace an unmanaged launcher: {launcher}"
        )


def _write_launcher(root: Path, python: Path) -> Path:
    _check_launcher(root)
    directory = root / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = directory / "workgate.cmd"
        # Keep batch file ASCII-only, including when the user's home path
        # contains non-ASCII characters; CMD expands %~dp0 itself.
        try:
            target = "%~dp0" + os.path.relpath(python, directory)
        except ValueError:
            # Windows cannot make a relative path across different drives.
            target = str(python)
        content = (
            f"@echo off\r\nrem {_LAUNCHER_MARKER}\r\n"
            f'"{target}" -m workgate.main %*\r\n'
        )
    else:
        launcher = directory / "workgate"
        content = (
            f"#!/bin/sh\n# {_LAUNCHER_MARKER}\n"
            f'exec {shlex.quote(str(python))} -m workgate.main "$@"\n'
        )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",  # Avoid double CRLF translation of Windows .cmd launchers.
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
    *,
    tag: str,
    role: str,
    root: Path,
    repository: str = REPOSITORY,
    activate_launcher: bool = True,
    expected_commit: str | None = None,
    arm64_wheel: str | None = None,
    arm64_wheel_sha256: str | None = None,
) -> Path:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?", tag):
        raise ValueError(
            "--tag must be an explicit version tag, not main or a branch"
        )
    if role not in {"control", "executor", "colocate"}:
        raise ValueError("choose one of control, executor, colocate")
    if expected_commit is not None and not re.fullmatch(
        r"[0-9a-f]{40}", expected_commit
    ):
        raise ValueError(
            "--expected-commit must be a full lowercase 40-digit SHA"
        )
    if bool(arm64_wheel) != bool(arm64_wheel_sha256):
        raise ValueError(
            "ARM64 wheel source and SHA256 must be provided together"
        )
    if arm64_wheel and not _native_arm64():
        raise ValueError(
            "ARM64 wheel is supported only on native Windows ARM64"
        )
    if arm64_wheel_sha256 and not re.fullmatch(
        r"[0-9a-f]{64}", arm64_wheel_sha256
    ):
        raise ValueError("ARM64 wheel SHA256 must be 64 lowercase hex digits")
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
        if not _managed_release_matches(
            temporary,
            tag,
            repository,
            lambda *args, **kw: _run(*args, timeout=15, **kw),
            commit=sha,
        ):
            raise RuntimeError(
                "checkout HEAD does not match the requested release tag"
            )
        if expected_commit is not None and sha != expected_commit:
            raise RuntimeError(
                f"release tag resolves to {sha}, not pinned commit {expected_commit}"
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
            if not (target / ".git").is_dir() or not _managed_release_matches(
                target,
                tag,
                repository,
                lambda *args, **kw: _run(*args, timeout=15, **kw),
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
        # Native ARM64 resolves its Wheel and SHA256 from this exact GitHub
        # Release; ordinary users never need to provide a checksum.
        if _native_arm64() and repository == REPOSITORY and not arm64_wheel:
            arm64_wheel, arm64_wheel_sha256 = _release_arm64_wheel(tag)
        build_env = None if arm64_wheel else _arm64_static_openssl_env()
        sync_args = [
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
        ]
        if build_env is not None:
            # Do not reuse a cached dynamically linked ARM64 source wheel.
            sync_args.extend(
                (
                    "--no-cache",
                    "--no-binary-package",
                    "cryptography",
                )
            )
        if arm64_wheel:
            assert arm64_wheel_sha256 is not None  # validated as a pair above
            # uv's registry-locked cryptography cannot consume an external wheel
            # directly. Each sync intentionally omits it; install the checked
            # wheel *afterwards*, including on repeated preparation.
            sync_args.extend(("--no-install-package", "cryptography"))
            with tempfile.TemporaryDirectory(
                prefix=".wheel-", dir=versions
            ) as scratch:
                wheel = _stage_arm64_wheel(
                    arm64_wheel, arm64_wheel_sha256, Path(scratch)
                )
                _run(*sync_args, cwd=target)
                python = _python(target)
                platform_tag = _run(
                    str(python),
                    "-c",
                    "import sysconfig; print(sysconfig.get_platform())",
                    cwd=target,
                )
                if platform_tag != "win-arm64":
                    raise RuntimeError(
                        f"ARM64 wheel requires native win-arm64 Python, not {platform_tag!r}"
                    )
                _run(
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    "--only-binary",
                    ":all:",
                    str(wheel),
                    cwd=target,
                )
        else:
            _run(*sync_args, cwd=target, env=build_env)
        python = _python(target)
        verify_env = None
        if not python.is_file():
            raise RuntimeError(
                f"uv did not create the expected interpreter: {python}"
            )
        if build_env is not None or arm64_wheel:
            # platform.machine() can report the host under x64 emulation.
            # sysconfig identifies the actual CPython build target.
            python_platform = _run(
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_platform())",
                cwd=target,
            )
            if python_platform != "win-arm64":
                raise RuntimeError(
                    "Windows ARM64 requires a native Python runtime, "
                    f"got {python_platform!r}"
                )
            # A successful source build is not enough: dynamic OpenSSL DLLs
            # can conflict with Python's own SSL module at import time.
            crypto = (
                "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; "
                "AESGCM(bytes(16)).encrypt(bytes(12), b'test', None)"
            )
            if arm64_wheel:
                crypto += (
                    "; import cryptography; from cryptography.hazmat.bindings.openssl.binding import Binding"
                    "; assert cryptography.__version__ == '50.0.2'"
                    "; assert Binding().ffi.string(Binding().lib.OpenSSL_version(0)).decode().startswith('OpenSSL 3.5.9 ')"
                )
            # Keep the user's Scoop OPENSSL_MODULES intact, but prevent
            # cryptography's static OpenSSL from loading its older legacy DLL.
            verify_env = {
                **os.environ,
                "CRYPTOGRAPHY_OPENSSL_NO_LEGACY": "1",
            }
            for script in (f"{crypto}; import ssl", f"import ssl; {crypto}"):
                try:
                    _run(str(python), "-c", script, cwd=target, env=verify_env)
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(
                        "Windows ARM64 cryptography runtime verification failed "
                        "(check static OpenSSL linking)"
                    ) from exc
        _run(
            str(python),
            "-m",
            "playwright",
            "install",
            "chromium",
            cwd=target,
            env=verify_env,
        )
        version = _run(
            str(python),
            "-m",
            "workgate.main",
            "--version",
            cwd=target,
            env=verify_env,
        )
        if version != f"workgate {tag[1:]}":
            raise RuntimeError(f"installed CLI version does not match {tag}")
        print(f"Prepared Workgate {version} ({tag}, {sha}) for {role}")
        print(f"Versioned checkout: {target}")
        if activate_launcher:
            launcher = _write_launcher(root, python)
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
    parser.add_argument(
        "--no-activate-launcher",
        action="store_true",
        help="Stage and verify the release without switching the stable workgate command",
    )
    parser.add_argument(
        "--arm64-wheel",
        help="Explicit HTTPS URL or local pinned cryptography ARM64 wheel",
    )
    parser.add_argument(
        "--arm64-wheel-sha256",
        help="Expected lowercase SHA256 from a trusted source",
    )
    parser.add_argument(
        "--expected-commit", help="Full pinned release commit SHA"
    )
    args = parser.parse_args()
    try:
        prepare(
            tag=args.tag,
            role=args.role,
            root=args.install_root,
            activate_launcher=not args.no_activate_launcher,
            expected_commit=args.expected_commit,
            arm64_wheel=args.arm64_wheel,
            arm64_wheel_sha256=args.arm64_wheel_sha256,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
    ) as exc:
        parser.exit(1, f"Workgate source preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
