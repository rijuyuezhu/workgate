"""Versioned source preparation tests using local Git release tags."""

import importlib.util
import os
import subprocess
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

    def invoke(*args, cwd=None, timeout=600):
        calls.append(args)
        if args[:2] == ("uv", "python"):
            return ""
        if args[:2] == ("uv", "sync"):
            path = module._python(cwd)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            return ""
        if args[1:4] == ("-m", "playwright", "install"):
            if fail[0]:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[1:3] == ("-m", "workgate.main"):
            return "v5.0.0a5"
        return original(*args, cwd=cwd, timeout=timeout)

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


@pytest.mark.parametrize("tag", ["main", "v5.0", "v5.0.0;echo x", "feature/a"])
def test_rejects_branches(tmp_path, preparer, tag):
    module, calls, _fail = preparer
    with pytest.raises(ValueError, match="explicit version tag"):
        module.prepare(tag=tag, role="executor", root=tmp_path)
    assert calls == []
