"""Public Linux bootstrap cannot execute a retargeted or unpinned release."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def pinned_release(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", *args],
            text=True,
            cwd=origin,
        ).strip()

    git("init", "-q")
    git("config", "user.email", "test@invalid.example")
    git("config", "user.name", "Test")
    directory = origin / "deploy" / "source"
    directory.mkdir(parents=True)
    (directory / "install.sh").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$WORKGATE_BOOTSTRAP_TEST_OUTPUT"\n'
    )
    (directory / "install.ps1").write_text(
        "param(\n"
        "  [string]$Role, [string]$Tag, [string]$ExpectedCommit,\n"
        "  [string]$ControlUrl, [string]$Name, [string]$InstallRoot\n"
        ")\n"
        "$ErrorActionPreference = 'Stop'\n"
        "@($Role, $Tag, $ExpectedCommit, $ControlUrl, $Name, $InstallRoot) |\n"
        "  Set-Content -LiteralPath $env:WORKGATE_BOOTSTRAP_TEST_OUTPUT\n"
    )
    git("add", ".")
    git("commit", "-qm", "release")
    git("tag", "v5.0.0a6")
    sha = git("rev-parse", "HEAD")
    original = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "source"
        / "bootstrap.sh"
    ).read_text()
    bootstrap = tmp_path / "bootstrap.sh"
    bootstrap.write_text(
        original.replace(
            "https://github.com/rijuyuezhu/workgate.git", str(origin)
        )
    )
    powershell = tmp_path / "bootstrap.ps1"
    powershell.write_text(
        (Path(__file__).resolve().parents[2] / "deploy/source/bootstrap.ps1")
        .read_text()
        .replace("https://github.com/rijuyuezhu/workgate.git", str(origin))
    )
    return bootstrap, sha


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_verified_public_bootstrap_runs_only_pinned_tag(
    pinned_release, tmp_path
):
    script, sha = pinned_release
    capture = tmp_path / "capture.txt"
    scratch = tmp_path / "temp"
    scratch.mkdir()
    env = os.environ | {
        "WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture),
        "TMPDIR": str(scratch),
    }
    result = subprocess.run(
        [
            "sh",
            str(script),
            "v5.0.0a6",
            sha,
            "--role",
            "executor",
            "--control-url",
            "https://control.example",
            "--name",
            "two words",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "--tag",
        "v5.0.0a6",
        "--expected-commit",
        sha,
        "--role",
        "executor",
        "--control-url",
        "https://control.example",
        "--name",
        "two words",
    ]
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
@pytest.mark.parametrize(
    "tag,sha",
    [
        ("main", "a" * 40),
        ("v5.0.0a6", "b" * 40),
        ("v5.0.0a6", "abc"),
        ("v5.0.0a6;whoami", "a" * 40),
        ("v5.0.0a6", "A" * 40),
    ],
)
def test_unpinned_or_mismatched_public_release_never_runs_installer(
    pinned_release, tmp_path, tag, sha
):
    script, _correct_sha = pinned_release
    capture = tmp_path / "capture.txt"
    result = subprocess.run(
        ["sh", str(script), tag, sha, "--role", "control"],
        env=os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not capture.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_piped_bootstrap_code_never_passes_remaining_script_into_child(
    pinned_release, tmp_path
):
    script, sha = pinned_release
    capture = tmp_path / "capture.txt"
    env = os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)}
    # Pass actual script through stdin exactly as curl | sh -s -- does.
    result = subprocess.run(
        ["sh", "-s", "--", "v5.0.0a6", sha, "--role", "control"],
        input=script.read_text(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--role\ncontrol\n" in capture.read_text()


@pytest.mark.skipif(
    os.name != "nt", reason="Native Windows PowerShell bootstrap"
)
def test_windows_public_bootstrap_clones_verified_release_and_forwards_options(
    pinned_release, tmp_path
):
    _shell, sha = pinned_release
    script = tmp_path / "bootstrap.ps1"
    capture = tmp_path / "capture.txt"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Role",
            "executor",
            "-Tag",
            "v5.0.0a6",
            "-Commit",
            sha,
            "-ControlUrl",
            "https://control.example",
            "-Name",
            "two words",
            "-InstallRoot",
            str(tmp_path / "install root"),
        ],
        env=os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "executor",
        "v5.0.0a6",
        sha,
        "https://control.example",
        "two words",
        str(tmp_path / "install root"),
    ]


@pytest.mark.skipif(
    os.name != "nt", reason="Native Windows PowerShell bootstrap"
)
@pytest.mark.parametrize("commit", ["a" * 40, "a" * 39, "A" * 40])
def test_windows_bootstrap_wrong_sha_never_executes_source(
    pinned_release, tmp_path, commit
):
    _shell, _sha = pinned_release
    capture = tmp_path / "capture.txt"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "bootstrap.ps1"),
            "-Role",
            "control",
            "-Tag",
            "v5.0.0a6",
            "-Commit",
            commit,
        ],
        env=os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode != 0
    assert not capture.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_annotated_release_tag_resolves_to_pinned_commit(
    pinned_release, tmp_path
):
    script, sha = pinned_release
    origin = tmp_path / "origin"
    subprocess.run(
        ["git", "-C", str(origin), "tag", "-fa", "v5.0.0a6", "-m", "annotated"],
        check=True,
    )
    capture = tmp_path / "annotated.txt"
    result = subprocess.run(
        ["sh", str(script), "v5.0.0a6", sha, "--role", "colocate"],
        env=os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines()[-2:] == ["--role", "colocate"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_public_bootstrap_refuses_symlinked_release_installer(
    pinned_release, tmp_path
):
    script, _sha = pinned_release
    origin = tmp_path / "origin"
    installer = origin / "deploy/source/install.sh"
    installer.unlink()
    installer.symlink_to("install.ps1")
    subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(origin), "commit", "-qm", "symlink"], check=True
    )
    subprocess.run(
        ["git", "-C", str(origin), "tag", "-f", "v5.0.0a6"], check=True
    )
    sha = subprocess.check_output(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    capture = tmp_path / "capture.txt"
    result = subprocess.run(
        ["sh", str(script), "v5.0.0a6", sha, "--role", "control"],
        env=os.environ | {"WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "regular source installer" in result.stderr
    assert not capture.exists()


@pytest.fixture
def missing_uv_path(tmp_path):
    """Isolate uv lookup and replace the public download with an offline fake."""
    import shutil

    commands = tmp_path / "commands"
    commands.mkdir()
    for tool in ("git", "grep", "mktemp", "rm", "sh"):
        found = shutil.which(tool)
        assert found is not None
        (commands / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir()
    downloaded = tmp_path / "astral-download.sh"
    downloaded.write_text(
        "#!/bin/sh\n"
        'test "$UV_NO_MODIFY_PATH" = 1 || exit 11\n'
        '/bin/mkdir -p "$UV_INSTALL_DIR"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$UV_INSTALL_DIR/uv\"\n"
        '/bin/chmod +x "$UV_INSTALL_DIR/uv"\n'
    )
    fake_curl = commands / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$UV_TEST_DOWNLOAD_ARGS"\n'
        'test "$1" = -fsSL || exit 9\n'
        'test "$2" = https://astral.sh/uv/install.sh || exit 9\n'
        'test "$3" = -o || exit 9\n'
        '/bin/cat "$UV_TEST_INSTALLER" > "$4"\n'
    )
    fake_curl.chmod(0o755)
    capture = tmp_path / "captured-arguments"
    log = tmp_path / "curl-arguments"
    return (
        {
            "PATH": str(commands),
            "HOME": str(home),
            "UV_INSTALL_DIR": str(home / "private uv bin"),
            "UV_TEST_INSTALLER": str(downloaded),
            "UV_TEST_DOWNLOAD_ARGS": str(log),
            "WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture),
        },
        capture,
        log,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_public_bootstrap_installs_missing_uv_once_before_role_setup(
    pinned_release, missing_uv_path
):
    script, sha = pinned_release
    env, capture, curl_args = missing_uv_path
    env = os.environ | env
    # The test PATH contains no uv even if the host has one.
    for via_pipe in (True, False):
        args = ["sh", "-s", "--"] if via_pipe else ["sh", str(script)]
        result = subprocess.run(
            [*args, "v5.0.0a6", sha, "--role", "control"],
            input=script.read_text() if via_pipe else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert capture.read_text().splitlines()[-2:] == ["--role", "control"]
        if via_pipe:
            assert curl_args.read_text().startswith(
                "-fsSL https://astral.sh/uv/install.sh -o "
            )
            curl_args.unlink()
        else:
            # The second run finds the previously installed user-local uv.
            assert not curl_args.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_uv_bootstrap_download_failure_does_not_run_role_installer(
    pinned_release, missing_uv_path, tmp_path
):
    script, sha = pinned_release
    env, capture, _log = missing_uv_path
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env["TMPDIR"] = str(scratch)
    (Path(env["PATH"]) / "curl").write_text("#!/bin/sh\nexit 19\n")
    result = subprocess.run(
        ["sh", str(script), "v5.0.0a6", sha, "--role", "control"],
        env=os.environ | env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not capture.exists()
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(
    os.name != "nt", reason="Native Windows PowerShell bootstrap"
)
def test_windows_public_bootstrap_installs_uv_when_missing(
    pinned_release, tmp_path
):
    import shutil

    existing_uv = shutil.which("uv")
    if existing_uv is None:
        pytest.skip("a runnable uv.exe is needed to stub Astral installation")
    script, sha = pinned_release
    original = (tmp_path / "bootstrap.ps1").read_text()
    replacement = (
        "Copy-Item -LiteralPath $env:UV_TEST_INSTALLER "
        "-Destination $uvInstaller"
    )
    assert (
        "Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' "
        "-OutFile $uvInstaller"
    ) in original
    script = tmp_path / "bootstrap-no-network.ps1"
    script.write_text(
        original.replace(
            "Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' "
            "-OutFile $uvInstaller",
            replacement,
        )
    )
    fake_installer = tmp_path / "fake-astral-uv.ps1"
    fake_installer.write_text(
        "if ($env:UV_NO_MODIFY_PATH -cne '1') { throw 'modified PATH' }\n"
        "New-Item -ItemType Directory -Force -Path $env:UV_INSTALL_DIR | Out-Null\n"
        "Copy-Item -LiteralPath $env:UV_TEST_SOURCE_EXE -Destination "
        "(Join-Path $env:UV_INSTALL_DIR 'uv.exe')\n"
    )
    # Remove any existing uv executable from the child process PATH,
    # while keeping the system and Git paths intact.
    dirs = [
        path
        for path in os.environ["PATH"].split(os.pathsep)
        if path and not (Path(path) / "uv.exe").exists()
    ]
    if any(
        (Path(path) / "git.exe").exists()
        for path in os.environ["PATH"].split(os.pathsep)
        if path
    ) and not any((Path(path) / "git.exe").exists() for path in dirs):
        pytest.skip("Git and uv share the same tool directory")
    uv_bin = tmp_path / "user uv installation"
    capture = tmp_path / "captured-windows-arguments"
    env = os.environ | {
        "PATH": os.pathsep.join(dirs),
        "UV_INSTALL_DIR": str(uv_bin),
        "UV_TEST_INSTALLER": str(fake_installer),
        "UV_TEST_SOURCE_EXE": existing_uv,
        "WORKGATE_BOOTSTRAP_TEST_OUTPUT": str(capture),
    }
    result = subprocess.run(
        [
            shutil.which("powershell.exe") or "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Role",
            "control",
            "-Tag",
            "v5.0.0a6",
            "-Commit",
            sha,
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (uv_bin / "uv.exe").is_file()
    assert capture.read_text().splitlines()[:3] == [
        "control",
        "v5.0.0a6",
        sha,
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell bootstrap")
def test_mismatched_release_does_not_bootstrap_uv(
    pinned_release, missing_uv_path
):
    script, _sha = pinned_release
    env, capture, curl_args = missing_uv_path
    result = subprocess.run(
        ["sh", str(script), "v5.0.0a6", "f" * 40, "--role", "control"],
        env=os.environ | env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "expected commit differ" in result.stderr
    assert not capture.exists()
    assert not curl_args.exists()
