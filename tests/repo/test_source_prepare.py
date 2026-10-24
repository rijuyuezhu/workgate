"""Versioned source preparation tests using local Git release tags."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def preparer(monkeypatch):
    file = Path(__file__).resolve().parents[2] / "deploy/source/prepare.py"
    spec = importlib.util.spec_from_file_location("wg_prepare", file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module._run
    calls = []
    fail = [False]

    def invoke(*args, cwd=None, timeout=600, env=None):
        calls.append(args)
        if args[:2] == ("uv", "python"):
            return ""
        if args[:2] == ("uv", "sync"):
            path = module._python(cwd)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            return ""
        if args[1:3] == (
            "-c",
            "import sysconfig; print(sysconfig.get_platform())",
        ):
            return "win-arm64"
        if args[1:2] == ("-c",) and "AESGCM" in args[2]:
            return ""
        if args[1:4] == ("-m", "playwright", "install"):
            if fail[0]:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[1:3] == ("-m", "workgate.main"):
            return "workgate 5.0.0a5"
        return original(*args, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(module, "_run", invoke)
    return module, calls, fail


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=root, text=True
        ).strip()

    git("init", "-q")
    git("config", "user.email", "test@invalid.example")
    git("config", "user.name", "Test")
    (root / "pyproject.toml").write_text(
        "[project]\nname='workgate'\nversion='5.0.0a5'\n"
    )
    (root / "uv.lock").write_text("version = 1\n")
    (root / ".gitignore").write_text(".venv/\n")
    git("add", ".")
    git("commit", "-qm", "release")
    git("tag", "v5.0.0a5")
    return root, git


def test_pinned_full_extras_idempotent(tmp_path, repo, preparer):
    origin, git = repo
    module, calls, _fail = preparer
    root = tmp_path / "install"
    checkout = module.prepare(
        tag="v5.0.0a5", role="executor", root=root, repository=str(origin)
    )
    assert checkout.name == git("rev-parse", "HEAD")
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    if os.name == "nt":
        assert "%~dp0" in launcher.read_text()
        assert checkout.name in launcher.read_text()
    else:
        assert str(module._python(checkout)) in launcher.read_text()
    assert (
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
    ) in calls
    assert any(args[1:4] == ("-m", "playwright", "install") for args in calls)
    contents = launcher.read_bytes()
    assert (
        module.prepare(
            tag="v5.0.0a5", role="executor", root=root, repository=str(origin)
        )
        == checkout
    )
    assert launcher.read_bytes() == contents
    assert len(list((root / "versions").iterdir())) == 1


def test_failure_does_not_switch_launcher(tmp_path, repo, preparer):
    origin, git = repo
    module, _calls, fail = preparer
    root = tmp_path / "install"
    first = module.prepare(
        tag="v5.0.0a5", role="control", root=root, repository=str(origin)
    )
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    before = launcher.read_bytes()
    (origin / "pyproject.toml").write_text(
        "[project]\nname='workgate'\nversion='5.0.0a6'\n"
    )
    git("add", ".")
    git("commit", "-qm", "next")
    git("tag", "v5.0.0a6")
    fail[0] = True
    with pytest.raises(subprocess.CalledProcessError):
        module.prepare(
            tag="v5.0.0a6", role="control", root=root, repository=str(origin)
        )
    assert launcher.read_bytes() == before
    assert first.exists()


def test_modified_checkout_is_not_overwritten(tmp_path, repo, preparer):
    origin, _git = repo
    module, _calls, _fail = preparer
    root = tmp_path / "install"
    checkout = module.prepare(
        tag="v5.0.0a5", role="colocate", root=root, repository=str(origin)
    )
    (checkout / "pyproject.toml").write_text("edited")
    with pytest.raises(RuntimeError, match="modified release checkout"):
        module.prepare(
            tag="v5.0.0a5", role="colocate", root=root, repository=str(origin)
        )


def test_refuses_untracked_source_files(tmp_path, repo, preparer):
    origin, _git = repo
    module, _calls, _fail = preparer
    root = tmp_path / "install"
    checkout = module.prepare(
        tag="v5.0.0a5", role="executor", root=root, repository=str(origin)
    )
    (checkout / "extra.py").write_text(
        "raise RuntimeError('not from release')\n"
    )
    with pytest.raises(RuntimeError, match="modified release checkout"):
        module.prepare(
            tag="v5.0.0a5", role="executor", root=root, repository=str(origin)
        )


def test_tag_must_match_project_version(tmp_path, repo, preparer):
    origin, git = repo
    module, _calls, _fail = preparer
    git("tag", "-d", "v5.0.0a5")
    git("tag", "v5.0.0a6")
    with pytest.raises(RuntimeError, match="does not match"):
        module.prepare(
            tag="v5.0.0a6",
            role="control",
            root=tmp_path,
            repository=str(origin),
        )
    assert not (tmp_path / "bin").exists()


@pytest.mark.parametrize("tag", ["main", "v5.0", "v5.0.0;echo x", "feature/a"])
def test_rejects_branches(tmp_path, preparer, tag):
    module, calls, _fail = preparer
    with pytest.raises(ValueError, match="explicit version tag"):
        module.prepare(tag=tag, role="executor", root=tmp_path)
    assert calls == []


def test_stable_launcher_can_run_real_workgate(tmp_path, preparer):
    module, _calls, _failed = preparer
    path = module._write_launcher(tmp_path, Path(sys.executable))
    result = subprocess.run(
        [str(path), "--version"],
        shell=os.name == "nt",
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "workgate" in result.stdout.lower()


def test_does_not_replace_unmanaged_launcher(tmp_path, repo, preparer):
    origin, _git = repo
    module, _calls, _fail = preparer
    launcher = (
        tmp_path / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    launcher.parent.mkdir()
    launcher.write_text("my unrelated command", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=tmp_path,
            repository=str(origin),
        )
    assert launcher.read_text(encoding="utf-8") == "my unrelated command"


def test_launcher_replaces_only_managed_file(tmp_path, preparer):
    module, _calls, _fail = preparer
    launcher = module._write_launcher(tmp_path, Path(sys.executable))
    assert "Managed by Workgate source preparation" in launcher.read_text()
    if os.name == "nt":
        assert b"\r\r\n" not in launcher.read_bytes()
    updated = module._write_launcher(tmp_path, Path(sys.executable))
    assert updated == launcher
    assert "Managed by Workgate source preparation" in launcher.read_text()


def test_marker_in_unrelated_file_does_not_grant_ownership(tmp_path, preparer):
    module, _calls, _fail = preparer
    launcher = (
        tmp_path / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    launcher.parent.mkdir()
    content = (
        "not a Workgate launcher\nManaged by Workgate source preparation\n"
    )
    launcher.write_text(content)
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        module._write_launcher(tmp_path, Path(sys.executable))
    assert launcher.read_text() == content


def test_mismatched_cli_version_cannot_replace_launcher(
    tmp_path, repo, preparer
):
    origin, _git = repo
    module, calls, _fail = preparer
    original = module._run

    def wrong_version(*args, cwd=None, timeout=600, env=None):
        if args[1:3] == ("-m", "workgate.main"):
            return "workgate 5.0.0a4"
        return original(*args, cwd=cwd, timeout=timeout)

    module._run = wrong_version
    with pytest.raises(RuntimeError, match="CLI version does not match"):
        module.prepare(
            tag="v5.0.0a5",
            role="control",
            root=tmp_path,
            repository=str(origin),
        )
    assert not (tmp_path / "bin").exists()


def test_preparer_rejects_retargeted_release_before_sync(
    tmp_path, repo, preparer
):
    origin, _git = repo
    module, calls, _fail = preparer
    with pytest.raises(RuntimeError, match="not pinned commit"):
        module.prepare(
            tag="v5.0.0a5",
            role="control",
            root=tmp_path / "install",
            repository=str(origin),
            expected_commit="b" * 40,
        )
    assert not (tmp_path / "install" / "bin").exists()
    assert not any(call[:2] == ("uv", "sync") for call in calls)


def test_preparer_accepts_expected_release_commit(tmp_path, repo, preparer):
    origin, git = repo
    module, _calls, _fail = preparer
    sha = git("rev-parse", "HEAD")
    assert (
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=tmp_path / "install",
            repository=str(origin),
            expected_commit=sha,
        ).name
        == sha
    )


@pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "not-a-sha", "a" * 41])
def test_preparer_rejects_invalid_sha_before_git(tmp_path, preparer, sha):
    module, calls, _fail = preparer
    with pytest.raises(ValueError, match="expected-commit"):
        module.prepare(
            tag="v5.0.0a5",
            role="control",
            root=tmp_path,
            expected_commit=sha,
        )
    assert calls == []


def test_arm64_openssl_static_environment_isolated(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    ssl = tmp_path / "openssl"
    library = ssl / "lib" / "VC" / "arm64" / "MD"
    library.mkdir(parents=True)
    for name in ("libssl_static.lib", "libcrypto_static.lib"):
        (library / name).write_bytes(b"mock static lib")
    headers = ssl / "include" / "openssl"
    headers.mkdir(parents=True)
    (headers / "ssl.h").write_text("// mock header")
    (headers / "opensslv.h").write_text(
        "# define OPENSSL_VERSION_MAJOR 3\n# define OPENSSL_VERSION_MINOR 5\n"
    )
    monkeypatch.setenv("OPENSSL_DIR", str(ssl))
    monkeypatch.setattr(module.shutil, "which", lambda _: "/mock/cargo")
    result = module._arm64_static_openssl_env(host_os="nt", machine="ARM64")
    assert result is not None
    assert result["OPENSSL_STATIC"] == "1"
    assert result["OPENSSL_LIBS"] == "libssl_static:libcrypto_static"
    assert result["OPENSSL_LIB_DIR"] == str(library)
    assert result["OPENSSL_INCLUDE_DIR"] == str(ssl / "include")
    assert "OPENSSL_STATIC" not in os.environ


def test_arm64_scoop_generic_openssl_lib_dir_uses_native_static_libs(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    ssl = tmp_path / "openssl"
    native = ssl / "lib" / "VC" / "arm64" / "MD"
    native.mkdir(parents=True)
    (native / "libssl_static.lib").write_bytes(b"test")
    (native / "libcrypto_static.lib").write_bytes(b"test")
    header = ssl / "include" / "openssl"
    header.mkdir(parents=True)
    (header / "ssl.h").write_bytes(b"test")
    (header / "opensslv.h").write_text(
        "# define OPENSSL_VERSION_MAJOR 3\n# define OPENSSL_VERSION_MINOR 5\n"
    )
    monkeypatch.setenv("OPENSSL_DIR", str(ssl))
    monkeypatch.setenv("OPENSSL_LIB_DIR", str(ssl / "lib"))
    monkeypatch.setenv("OPENSSL_INCLUDE_DIR", str(ssl / "include"))
    monkeypatch.setattr(module.shutil, "which", lambda _: "/mock/cargo")
    result = module._arm64_static_openssl_env(host_os="nt", machine="ARM64")
    assert result is not None
    assert result["OPENSSL_LIB_DIR"] == str(native)


def test_static_build_not_enabled_on_other_platforms(preparer):
    module, _calls, _fail = preparer
    assert (
        module._arm64_static_openssl_env(host_os="posix", machine="ARM64")
        is None
    )
    assert (
        module._arm64_static_openssl_env(host_os="nt", machine="AMD64") is None
    )


def test_arm64_source_build_requires_static_libs(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    monkeypatch.setenv("OPENSSL_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="OpenSSL static libraries"):
        module._arm64_static_openssl_env(host_os="nt", machine="ARM64")


def test_arm64_source_build_requires_cargo(tmp_path, preparer, monkeypatch):
    module, _calls, _fail = preparer
    lib = tmp_path / "lib" / "VC" / "arm64" / "MD"
    lib.mkdir(parents=True)
    (lib / "libssl_static.lib").write_bytes(b"")
    (lib / "libcrypto_static.lib").write_bytes(b"")
    headers = tmp_path / "include" / "openssl"
    headers.mkdir(parents=True)
    (headers / "ssl.h").write_bytes(b"")
    (headers / "opensslv.h").write_text(
        "# define OPENSSL_VERSION_MAJOR 3\n# define OPENSSL_VERSION_MINOR 5\n"
    )
    monkeypatch.setenv("OPENSSL_DIR", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Cargo"):
        module._arm64_static_openssl_env(host_os="nt", machine="ARM64")


def test_native_arm64_uv_sync_forces_fresh_static_cryptography(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    env = {
        "OPENSSL_STATIC": "1",
        "OPENSSL_LIBS": "libssl_static:libcrypto_static",
    }
    monkeypatch.setattr(module, "_arm64_static_openssl_env", lambda: env)
    seen = []
    original_run = module._run

    def run(*args, **kwargs):
        if args[:2] == ("uv", "sync"):
            seen.append(kwargs.get("env"))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module, "_run", run)
    module.prepare(
        tag="v5.0.0a5",
        role="executor",
        root=tmp_path / "managed",
        repository=str(origin),
    )
    call = next(args for args in calls if args[:2] == ("uv", "sync"))
    assert call[:11] == (
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
        "--no-cache",
    )
    assert call[11:] == (
        "--no-binary-package",
        "cryptography",
    )
    assert seen == [env]


def test_arm64_refuses_x64_python_emulation_before_browser_or_launcher(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    monkeypatch.setattr(
        module,
        "_arm64_static_openssl_env",
        lambda: {"OPENSSL_STATIC": "1"},
    )
    original_run = module._run

    def emulate_x64(*args, **kwargs):
        if args[1:3] == (
            "-c",
            "import sysconfig; print(sysconfig.get_platform())",
        ):
            return "win-amd64"
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module, "_run", emulate_x64)
    with pytest.raises(RuntimeError, match="native Python"):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=tmp_path / "managed",
            repository=str(origin),
        )
    assert not (tmp_path / "managed" / "bin").exists()
    assert not any(a[1:4] == ("-m", "playwright", "install") for a in calls)


def test_arm64_scoop_openssl_and_cargo_discovery(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    home = tmp_path
    sdk = home / "scoop/apps/openssl-lts/current"
    library = sdk / "lib/VC/arm64/MD"
    library.mkdir(parents=True)
    for name in ("libssl_static.lib", "libcrypto_static.lib"):
        (library / name).write_bytes(b"test")
    headers = sdk / "include/openssl"
    headers.mkdir(parents=True)
    (headers / "ssl.h").write_bytes(b"test")
    (headers / "opensslv.h").write_text(
        "# define OPENSSL_VERSION_MAJOR 3\n# define OPENSSL_VERSION_MINOR 5\n"
    )
    rust = home / "scoop/persist/rustup/.cargo/bin"
    rust.mkdir(parents=True)
    (rust / "cargo.exe").write_bytes(b"test")
    for name in ("OPENSSL_DIR", "OPENSSL_LIB_DIR", "OPENSSL_INCLUDE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: "/fake/link" if name == "link" else None,
    )
    env = module._arm64_static_openssl_env(host_os="nt", machine="ARM64")
    assert env is not None
    assert env["OPENSSL_DIR"] == str(sdk)
    assert env["OPENSSL_LIB_DIR"] == str(library)
    assert env["RUSTUP_HOME"] == str(home / "scoop/persist/rustup/.rustup")
    assert str(rust) in env["PATH"]
    assert env["OPENSSL_STATIC"] == "1"
    assert "OPENSSL_STATIC" not in os.environ


def test_arm64_missing_msvc_fails_before_uv_sync(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    sdk = tmp_path / "openssl"
    libs = sdk / "lib/VC/arm64/MD"
    libs.mkdir(parents=True)
    for name in ("libssl_static.lib", "libcrypto_static.lib"):
        (libs / name).touch()
    headers = sdk / "include/openssl"
    headers.mkdir(parents=True)
    (headers / "ssl.h").touch()
    (headers / "opensslv.h").write_text(
        "# define OPENSSL_VERSION_MAJOR 3\n# define OPENSSL_VERSION_MINOR 5\n"
    )
    monkeypatch.setenv("OPENSSL_DIR", str(sdk))
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: "/fake/cargo" if name == "cargo" else None,
    )
    with pytest.raises(RuntimeError, match="MSVC build shell"):
        module._arm64_static_openssl_env(host_os="nt", machine="ARM64")


@pytest.mark.parametrize("major,minor", [(3, 0), (3, 4), (0, 0)])
def test_arm64_rejects_unsupported_static_openssl_sdk(
    tmp_path, preparer, monkeypatch, major, minor
):
    module, _calls, _fail = preparer
    sdk = tmp_path / "openssl"
    lib = sdk / "lib/VC/arm64/MD"
    lib.mkdir(parents=True)
    for name in ("libssl_static.lib", "libcrypto_static.lib"):
        (lib / name).write_bytes(b"mock")
    inc = sdk / "include/openssl"
    inc.mkdir(parents=True)
    (inc / "ssl.h").write_bytes(b"mock")
    (inc / "opensslv.h").write_text(
        f"# define OPENSSL_VERSION_MAJOR {major}\n"
        f"# define OPENSSL_VERSION_MINOR {minor}\n"
    )
    monkeypatch.setenv("OPENSSL_DIR", str(sdk))
    with pytest.raises(RuntimeError, match="supported OpenSSL"):
        module._arm64_static_openssl_env(host_os="nt", machine="ARM64")


def test_arm64_static_crypto_import_order_checked_before_browser(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    monkeypatch.setattr(
        module, "_arm64_static_openssl_env", lambda: {"OPENSSL_STATIC": "1"}
    )
    module.prepare(
        tag="v5.0.0a5",
        role="executor",
        root=tmp_path / "managed",
        repository=str(origin),
    )
    crypto_calls = [
        (i, a[2])
        for i, a in enumerate(calls)
        if len(a) >= 3 and a[1] == "-c" and "AESGCM" in a[2]
    ]
    assert len(crypto_calls) == 2
    assert crypto_calls[0][1].startswith("from cryptography")
    assert crypto_calls[1][1].startswith("import ssl;")
    browser = next(
        i
        for i, a in enumerate(calls)
        if a[1:4] == ("-m", "playwright", "install")
    )
    assert crypto_calls[-1][0] < browser


def test_arm64_crypto_import_failure_preserves_launcher(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    monkeypatch.setattr(
        module, "_arm64_static_openssl_env", lambda: {"OPENSSL_STATIC": "1"}
    )
    original_run = module._run

    def import_fails(*args, **kwargs):
        if len(args) >= 3 and args[1] == "-c" and "AESGCM" in args[2]:
            raise subprocess.CalledProcessError(1, args)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module, "_run", import_fails)
    with pytest.raises(RuntimeError, match="cryptography runtime verification"):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=tmp_path / "managed",
            repository=str(origin),
        )
    assert not (tmp_path / "managed" / "bin").exists()
    assert not any(a[1:4] == ("-m", "playwright", "install") for a in calls)
