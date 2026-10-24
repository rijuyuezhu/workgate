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
