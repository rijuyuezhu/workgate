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


@pytest.mark.parametrize("activate_launcher", [True, False])
def test_failure_does_not_switch_launcher(
    tmp_path, repo, preparer, activate_launcher
):
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
            tag="v5.0.0a6",
            role="control",
            root=root,
            repository=str(origin),
            activate_launcher=activate_launcher,
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


def test_arm64_relative_sdk_paths_are_absolute_for_uv_cwd(
    tmp_path, preparer, monkeypatch
):
    module, _calls, _fail = preparer
    sdk = tmp_path / "sdk"
    library = sdk / "lib/VC/arm64/MD"
    library.mkdir(parents=True)
    for name in ("libssl_static.lib", "libcrypto_static.lib"):
        (library / name).touch()
    headers = sdk / "include/openssl"
    headers.mkdir(parents=True)
    (headers / "ssl.h").touch()
    (headers / "opensslv.h").write_text(
        "#define OPENSSL_VERSION_MAJOR 3\n#define OPENSSL_VERSION_MINOR 5\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSSL_DIR", "sdk")
    monkeypatch.setenv("OPENSSL_LIB_DIR", "sdk/lib/VC/arm64/MD")
    monkeypatch.setenv("OPENSSL_INCLUDE_DIR", "sdk/include")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/mock/tool")
    env = module._arm64_static_openssl_env(host_os="nt", machine="ARM64")
    assert env is not None
    assert env["OPENSSL_DIR"] == str(sdk)
    assert env["OPENSSL_LIB_DIR"] == str(library)
    assert env["OPENSSL_INCLUDE_DIR"] == str(sdk / "include")


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
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))
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


def test_arm64_wheel_input_requires_explicit_pinned_pair(tmp_path, preparer):
    module, calls, _fail = preparer
    for source, sha in (
        ("wheel.whl", None),
        (None, "a" * 64),
        ("wheel.whl", "bad"),
    ):
        with pytest.raises(ValueError, match="ARM64 wheel"):
            module.prepare(
                tag="v5.0.0a5",
                role="executor",
                root=tmp_path,
                arm64_wheel=source,
                arm64_wheel_sha256=sha,
            )
    assert calls == []


def test_arm64_wheel_rejects_untrusted_source_and_hash(tmp_path, preparer):
    import hashlib

    module, _calls, _fail = preparer
    actual = tmp_path / module._ARM64_CRYPTO_WHEEL
    actual.write_bytes(b"not a wheel")
    directory = tmp_path / "other"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        module._stage_arm64_wheel(str(actual), "a" * 64, directory)

    for url in (
        "http://example.com/" + module._ARM64_CRYPTO_WHEEL,
        "https://user:pass@example.com/" + module._ARM64_CRYPTO_WHEEL,
        "https://example.com/" + module._ARM64_CRYPTO_WHEEL + "?latest=1",
    ):
        with pytest.raises(ValueError, match="pinned HTTPS"):
            module._stage_arm64_wheel(
                url, hashlib.sha256(b"x").hexdigest(), tmp_path
            )


def test_arm64_wheel_sync_after_hash_and_reinstall_on_repeat(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    monkeypatch.setattr(module, "_native_arm64", lambda: True)
    monkeypatch.setattr(
        module,
        "_arm64_static_openssl_env",
        lambda: pytest.fail("unexpected developer-only Cargo SDK fallback"),
    )
    wheel = tmp_path / module._ARM64_CRYPTO_WHEEL
    wheel.write_bytes(b"signed-test-wheel")
    sha = "a" * 64
    checks = []

    def verify(source, digest, directory):
        checks.append((source, digest))
        staged = directory / module._ARM64_CRYPTO_WHEEL
        staged.write_bytes(wheel.read_bytes())
        return staged

    monkeypatch.setattr(module, "_stage_arm64_wheel", verify)
    original_run = module._run

    def run(*args, **kwargs):
        if args[:2] == ("uv", "pip"):
            calls.append(args)
            assert "--no-deps" in args and "--only-binary" in args
            assert "--no-install-package" in next(
                call for call in calls if call[:2] == ("uv", "sync")
            )
            return ""
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module, "_run", run)
    root = tmp_path / "managed"
    for _ in range(2):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=root,
            repository=str(origin),
            arm64_wheel=str(wheel),
            arm64_wheel_sha256=sha,
        )
    assert len(checks) == 2
    assert len([a for a in calls if a[:2] == ("uv", "sync")]) == 2
    assert len([a for a in calls if a[:2] == ("uv", "pip")]) == 2
    assert (
        len(
            [a for a in calls if len(a) > 2 and a[1:3] == ("-c", "import ssl;")]
        )
        == 0
    )
    assert (
        len(
            [
                a
                for a in calls
                if len(a) > 2 and a[1] == "-c" and "Binding()" in a[2]
            ]
        )
        == 4
    )


def test_arm64_wheel_failed_verification_keeps_previous_launcher(
    tmp_path, repo, preparer, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    root = tmp_path / "managed"
    module.prepare(
        tag="v5.0.0a5", role="executor", root=root, repository=str(origin)
    )
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    before = launcher.read_bytes()
    monkeypatch.setattr(module, "_native_arm64", lambda: True)

    def reject(*args):
        raise RuntimeError("ARM64 wheel SHA256 mismatch")

    monkeypatch.setattr(module, "_stage_arm64_wheel", reject)
    start = len(calls)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=root,
            repository=str(origin),
            arm64_wheel="bad.whl",
            arm64_wheel_sha256="a" * 64,
        )
    assert launcher.read_bytes() == before
    assert not any(a[:2] == ("uv", "sync") for a in calls[start:])


def test_arm64_wheel_https_download_requires_hash_and_https_redirect(
    tmp_path, preparer, monkeypatch
):
    import hashlib
    import io

    module, _calls, _fail = preparer
    data = b"verified-test-wheel"
    expected = hashlib.sha256(data).hexdigest()
    source = "https://trusted.example/" + module._ARM64_CRYPTO_WHEEL
    inspected = []

    class Response(io.BytesIO):
        destination_url = source

        def geturl(self):
            return self.destination_url

    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda url, timeout: Response(data)
    )
    monkeypatch.setattr(
        module.runpy,
        "run_path",
        lambda path: {
            "verify_wheel": lambda wheel: inspected.append(wheel.read_bytes())
        },
    )
    wheel = module._stage_arm64_wheel(source, expected, tmp_path)
    assert wheel.read_bytes() == data
    assert inspected == [data]

    class UnsafeRedirect(Response):
        destination_url = (
            "http://untrusted.example/" + module._ARM64_CRYPTO_WHEEL
        )

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda url, timeout: UnsafeRedirect(data),
    )
    with pytest.raises(RuntimeError, match="outside HTTPS"):
        module._stage_arm64_wheel(source, expected, tmp_path)


def test_arm64_release_metadata_supplies_hash_without_user_input(
    preparer, monkeypatch
):
    import io
    import json

    module, _calls, _fail = preparer
    tag = "v5.0.0a6"
    wheel = module._ARM64_CRYPTO_WHEEL
    url = f"https://github.com/rijuyuezhu/workgate/releases/download/{tag}/{wheel}"
    expected = {
        "tag_name": tag,
        "assets": [
            {
                "name": wheel,
                "browser_download_url": url,
                "digest": "sha256:" + "a" * 64,
            }
        ],
    }
    requested = []

    def respond(request, timeout):
        requested.append((request.full_url, timeout))
        return io.BytesIO(json.dumps(expected).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", respond)
    assert module._release_arm64_wheel(tag) == (url, "a" * 64)
    assert requested == [
        (
            f"https://api.github.com/repos/rijuyuezhu/workgate/releases/tags/{tag}",
            30,
        )
    ]

    bad = [
        {**expected, "tag_name": "v5.0.0a5"},
        {**expected, "assets": []},
        {**expected, "assets": [expected["assets"][0]] * 2},
        {
            **expected,
            "assets": [{**expected["assets"][0], "digest": "sha256:invalid"}],
        },
        {
            **expected,
            "assets": [
                {
                    **expected["assets"][0],
                    "browser_download_url": "https://attacker.example/a.whl",
                }
            ],
        },
    ]
    for invalid in bad:
        monkeypatch.setattr(
            module.urllib.request,
            "urlopen",
            lambda request, timeout, response=invalid: io.BytesIO(
                json.dumps(response).encode()
            ),
        )
        with pytest.raises(
            RuntimeError, match="tag mismatch|no unique|URL or SHA256"
        ):
            module._release_arm64_wheel(tag)


def test_windows_arm64_emulated_shell_uses_native_wheel(monkeypatch, preparer):
    from types import SimpleNamespace

    module, _calls, _fail = preparer
    monkeypatch.setattr(module.platform, "machine", lambda: "x86")
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(name="nt", environ={"PROCESSOR_ARCHITEW6432": "ARM64"}),
    )
    assert module._native_arm64()
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(name="nt", environ={}),
    )
    assert not module._native_arm64()
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            name="posix", environ={"PROCESSOR_ARCHITEW6432": "ARM64"}
        ),
    )
    assert not module._native_arm64()


def test_native_arm64_automatically_installs_release_wheel_without_checksum(
    repo, preparer, tmp_path, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    monkeypatch.setattr(module, "REPOSITORY", str(origin))
    monkeypatch.setattr(module, "_native_arm64", lambda: True)
    monkeypatch.setattr(
        module,
        "_arm64_static_openssl_env",
        lambda: pytest.fail("ARM64 default must not require source compiler"),
    )
    resolved = []
    url = (
        "https://github.com/rijuyuezhu/workgate/releases/download/"
        "v5.0.0a5/" + module._ARM64_CRYPTO_WHEEL
    )

    def resolve(tag):
        resolved.append(tag)
        return url, "a" * 64

    monkeypatch.setattr(module, "_release_arm64_wheel", resolve)
    staged = []

    def stage(source, sha256, directory):
        staged.append((source, sha256))
        wheel = directory / module._ARM64_CRYPTO_WHEEL
        wheel.write_bytes(b"checked Wheel")
        return wheel

    monkeypatch.setattr(module, "_stage_arm64_wheel", stage)
    orig = module._run

    def run(*args, **kwargs):
        if args[:2] == ("uv", "pip"):
            calls.append(args)
            return ""
        return orig(*args, **kwargs)

    monkeypatch.setattr(module, "_run", run)
    for _ in range(2):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=tmp_path / "managed",
            repository=str(origin),
        )
    assert resolved == ["v5.0.0a5"] * 2
    assert staged == [(url, "a" * 64)] * 2
    assert len([a for a in calls if a[:2] == ("uv", "pip")]) == 2
    assert all(
        "--no-install-package" in a for a in calls if a[:2] == ("uv", "sync")
    )


def test_missing_release_wheel_fails_before_uv_sync_and_keeps_launcher(
    repo, preparer, tmp_path, monkeypatch
):
    origin, _git = repo
    module, calls, _fail = preparer
    root = tmp_path / "managed"
    module.prepare(
        tag="v5.0.0a5",
        role="executor",
        root=root,
        repository=str(origin),
    )
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    before = launcher.read_bytes()
    monkeypatch.setattr(module, "REPOSITORY", str(origin))
    monkeypatch.setattr(module, "_native_arm64", lambda: True)

    def reject(tag):
        raise RuntimeError("GitHub Release lacks native ARM64 Wheel")

    monkeypatch.setattr(module, "_release_arm64_wheel", reject)
    call_count = len(calls)
    with pytest.raises(RuntimeError, match="lacks native ARM64 Wheel"):
        module.prepare(
            tag="v5.0.0a5",
            role="executor",
            root=root,
            repository=str(origin),
        )
    assert launcher.read_bytes() == before
    assert not any(a[:2] == ("uv", "sync") for a in calls[call_count:])


def test_staged_prepare_preserves_launcher_and_paired_user_files(
    tmp_path, repo, preparer
):
    origin, git = repo
    module, calls, _fail = preparer
    root = tmp_path / "install"
    first = module.prepare(
        tag="v5.0.0a5", role="control", root=root, repository=str(origin)
    )
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    before = launcher.read_bytes()
    # A real prepared source release, followed by a different pinned Git tag.
    (origin / "pyproject.toml").write_text(
        "[project]\nname='workgate'\nversion='5.0.0a6'\n"
    )
    git("add", ".")
    git("commit", "-qm", "next")
    git("tag", "v5.0.0a6")
    previous_run = module._run

    def next_release_version(*args, **kwargs):
        if args[1:3] == ("-m", "workgate.main"):
            return "workgate 5.0.0a6"
        return previous_run(*args, **kwargs)

    module._run = next_release_version
    # Model a user-owned config/pairing identity that version changes cannot edit.
    state = root / "user-pairing.json"
    state.write_text('{"identity":"unchanged"}')
    stage = module.prepare(
        tag="v5.0.0a6",
        role="control",
        root=root,
        repository=str(origin),
        activate_launcher=False,
    )
    assert stage.name == git("rev-parse", "HEAD")
    assert stage != first and module._python(stage).exists()
    assert launcher.read_bytes() == before
    assert state.read_text() == '{"identity":"unchanged"}'
    assert any(call[1:4] == ("-m", "playwright", "install") for call in calls)
    # Re-staging the same release must not switch the active CLI either.
    assert (
        module.prepare(
            tag="v5.0.0a6",
            role="control",
            root=root,
            repository=str(origin),
            activate_launcher=False,
        )
        == stage
    )
    assert launcher.read_bytes() == before


def test_preparer_cli_forwards_no_activate_launcher(
    monkeypatch, preparer, tmp_path
):
    module, _calls, _fail = preparer
    passed = []
    monkeypatch.setattr(
        module, "prepare", lambda **kwargs: passed.append(kwargs)
    )
    for flag in (False, True):
        args = [
            "prepare.py",
            "--role",
            "executor",
            "--tag",
            "v5.0.0a5",
            "--install-root",
            str(tmp_path),
        ]
        if flag:
            args.append("--no-activate-launcher")
        monkeypatch.setattr(sys, "argv", args)
        module.main()
        assert passed[-1]["activate_launcher"] is not flag


def test_first_stage_does_not_create_stable_launcher(tmp_path, repo, preparer):
    origin, _git = repo
    module, _calls, _fail = preparer
    root = tmp_path / "install"
    module.prepare(
        tag="v5.0.0a5",
        role="colocate",
        root=root,
        repository=str(origin),
        activate_launcher=False,
    )
    assert not (root / "bin" / "workgate").exists()
    assert not (root / "bin" / "workgate.cmd").exists()


@pytest.mark.parametrize("tamper", ["origin", "head", "tag"])
def test_prepared_checkout_identity_is_rechecked_before_reuse(
    tmp_path, repo, preparer, monkeypatch, tamper
):
    origin, _git = repo
    module, _calls, _fail = preparer
    # Checkout identity is OS-independent; ARM64 wheel setup is tested separately.
    monkeypatch.setattr(module, "_native_arm64", lambda: False)
    root = tmp_path / "managed"
    checkout = module.prepare(
        tag="v5.0.0a5", role="control", root=root, repository=str(origin)
    )
    if tamper == "origin":
        subprocess.run(
            ["git", "remote", "set-url", "origin", str(tmp_path / "foreign")],
            cwd=checkout,
            check=True,
        )
    elif tamper == "head":
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@invalid.example",
                "commit",
                "--allow-empty",
                "-qm",
                "unexpected HEAD",
            ],
            cwd=checkout,
            check=True,
        )
    else:
        subprocess.run(
            ["git", "tag", "-d", "v5.0.0a5"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    ).read_bytes()
    with pytest.raises(RuntimeError, match="not this managed version"):
        module.prepare(
            tag="v5.0.0a5", role="control", root=root, repository=str(origin)
        )
    assert (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    ).read_bytes() == launcher
