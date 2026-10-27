"""Published release pinning and non-activating source preparation."""

import subprocess
from pathlib import Path

import httpx
import pytest

from workgate import __version__
from workgate.executor import source_stage
from workgate.executor.service import ExecutorServiceManager

SHA = "a" * 40
ANNOTATED = "b" * 40
TAG = "v6.0.0a1"


def _api(
    monkeypatch: pytest.MonkeyPatch, payload: dict | None, *, status: int = 200
):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://api.github.com/repos/rijuyuezhu/workgate/releases/tags/"
            + TAG
        )
        return httpx.Response(status, json=payload)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        source_stage.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refs", "commit"),
    [
        (f"{SHA}\trefs/tags/{TAG}\n", SHA),
        (f"{ANNOTATED}\trefs/tags/{TAG}\n{SHA}\trefs/tags/{TAG}^{{}}\n", SHA),
    ],
)
async def test_published_tag_resolves_immutable_commit(
    monkeypatch: pytest.MonkeyPatch, refs: str, commit: str
) -> None:
    _api(
        monkeypatch,
        {"tag_name": TAG, "draft": False, "published_at": "2026-10-27"},
    )

    def fake_run(args: list[str], **kw):
        assert args[:4] == [
            "git",
            "ls-remote",
            "--tags",
            "https://github.com/rijuyuezhu/workgate.git",
        ]
        assert args[-2:] == [f"refs/tags/{TAG}", f"refs/tags/{TAG}^{{}}"]
        assert kw["check"] is True
        assert kw["timeout"] == 25
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return subprocess.CompletedProcess(args, 0, refs, "")

    monkeypatch.setattr(source_stage.subprocess, "run", fake_run)
    assert await source_stage.resolve_published_commit("6.0.0a1") == commit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "refs"),
    [
        (
            {"tag_name": "v6.0.0a2", "draft": False, "published_at": "today"},
            None,
        ),
        ({"tag_name": TAG, "draft": True, "published_at": "today"}, None),
        ({"tag_name": TAG, "draft": False}, None),
        ({"tag_name": TAG, "draft": False, "published_at": "today"}, ""),
        (
            {"tag_name": TAG, "draft": False, "published_at": "today"},
            f"{SHA}\trefs/tags/{TAG[:-1]}\n",
        ),
        (
            {"tag_name": TAG, "draft": False, "published_at": "today"},
            f"{SHA}\trefs/tags/{TAG}\n{ANNOTATED}\trefs/tags/{TAG}\n",
        ),
        (
            {"tag_name": TAG, "draft": False, "published_at": "today"},
            f"bad\trefs/tags/{TAG}\n",
        ),
    ],
)
async def test_untrusted_or_missing_published_tag_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: dict, refs: str | None
) -> None:
    _api(monkeypatch, payload)

    def fake_run(args: list[str], **kw):
        assert refs is not None
        return subprocess.CompletedProcess(args, 0, refs, "")

    monkeypatch.setattr(source_stage.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        await source_stage.resolve_published_commit("6.0.0a1")


@pytest.mark.asyncio
async def test_release_404_or_invalid_version_never_fetches_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api(monkeypatch, {"error": "not found"}, status=404)
    with pytest.raises(httpx.HTTPStatusError):
        await source_stage.resolve_published_commit("6.0.0a1")
    with pytest.raises(ValueError, match="invalid"):
        await source_stage.resolve_published_commit("../../main")


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "source"
    worktree = root / "versions" / ("0" * 40)
    preparer = worktree / "deploy" / "source" / "prepare.py"
    preparer.parent.mkdir(parents=True)
    preparer.write_text("print('would stage')\n")
    python = worktree / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("not called")
    launcher = root / "bin" / "workgate"
    launcher.parent.mkdir()
    launcher.write_text("existing version unchanged\n")
    return worktree, python, launcher


@pytest.mark.asyncio
async def test_stage_passes_exact_commit_and_never_activates(
    checkout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, python, launcher = checkout
    monkeypatch.setattr(
        source_stage,
        "verified_source_checkout",
        lambda p, *, version, system: (
            worktree
            if p == python and version == __version__ and system == "Linux"
            else None
        ),
    )

    async def resolve(version: str) -> str:
        assert version == "6.0.0a1"
        return SHA

    monkeypatch.setattr(source_stage, "resolve_published_commit", resolve)

    def fake_run(args: list[str], **kw):
        assert args == [
            str(python),
            str(worktree / "deploy/source/prepare.py"),
            "--role",
            "executor",
            "--tag",
            TAG,
            "--expected-commit",
            SHA,
            "--install-root",
            str(worktree.parent.parent),
            "--no-activate-launcher",
        ]
        assert kw["cwd"] == worktree
        assert kw["check"] is True
        destination = worktree.parent / SHA / ".venv"
        destination.mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(source_stage.subprocess, "run", fake_run)
    original = launcher.read_bytes()
    staged = await source_stage.prepare_managed_source(
        worktree, version="6.0.0a1", system="Linux"
    )
    assert staged == worktree.parent / SHA
    assert launcher.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["unverified", "same", "no-preparer", "download-failed", "prepare-failed"],
)
async def test_failures_leave_old_launcher_unchanged(
    checkout: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    worktree, _python, launcher = checkout
    monkeypatch.setattr(
        source_stage,
        "verified_source_checkout",
        lambda *_args, **_kw: None if mode == "unverified" else worktree,
    )

    async def resolve(_version: str) -> str:
        if mode == "download-failed":
            raise httpx.ConnectError("network unavailable")
        return SHA

    monkeypatch.setattr(source_stage, "resolve_published_commit", resolve)
    if mode == "no-preparer":
        (worktree / "deploy/source/prepare.py").unlink()

    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "prepare", stderr="build failed")

    monkeypatch.setattr(source_stage.subprocess, "run", fake_run)
    original = launcher.read_bytes()
    with pytest.raises(
        (
            ValueError,
            RuntimeError,
            OSError,
            httpx.ConnectError,
            subprocess.CalledProcessError,
        )
    ):
        await source_stage.prepare_managed_source(
            worktree,
            version=__version__ if mode == "same" else "6.0.0a1",
            system="Linux",
        )
    assert launcher.read_bytes() == original


@pytest.mark.asyncio
async def test_manager_requires_existing_native_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(ExecutorServiceManager)
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: None)
    with pytest.raises(RuntimeError, match="not an owned managed source"):
        await manager.prepare_source_update("6.0.0a1")


@pytest.mark.asyncio
async def test_manager_hands_off_only_verified_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = object.__new__(ExecutorServiceManager)
    manager.system = "Linux"
    root = tmp_path / "checkout"
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: root)
    observed = []

    async def fake_stage(checkout: Path, *, version: str, system: str):
        observed.append((checkout, version, system))
        return tmp_path / "prepared"

    monkeypatch.setattr(source_stage, "prepare_managed_source", fake_stage)
    assert (
        await manager.prepare_source_update("6.0.0a1") == tmp_path / "prepared"
    )
    assert observed == [(root, "6.0.0a1", "Linux")]
