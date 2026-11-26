"""Optional authenticated build provenance."""

import subprocess
from pathlib import Path

import pytest

from workgate import build_info


def test_build_info_with_source_checkout(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        build_info,
        "_git_value",
        lambda _root, *args: "a" * 40 if "rev-parse" in args else "main",
    )
    build_info._checkout_info.cache_clear()
    for name in ("COMMIT", "BRANCH", "DATE"):
        monkeypatch.delenv("WORKGATE_BUILD_" + name, raising=False)
    try:
        assert build_info.build_info() == {"commit": "a" * 40, "branch": "main"}
    finally:
        build_info._checkout_info.cache_clear()


def test_build_info_without_checkout_or_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    for name in ("COMMIT", "BRANCH", "DATE"):
        monkeypatch.delenv("WORKGATE_BUILD_" + name, raising=False)
    build_info._checkout_info.cache_clear()
    try:
        assert build_info.build_info() == {}
    finally:
        build_info._checkout_info.cache_clear()


def test_build_info_from_deployment_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    monkeypatch.setenv("WORKGATE_BUILD_COMMIT", "f" * 40)
    monkeypatch.setenv("WORKGATE_BUILD_BRANCH", "release")
    monkeypatch.setenv("WORKGATE_BUILD_DATE", "2026-11-23T08:00:00Z")
    build_info._checkout_info.cache_clear()
    try:
        assert build_info.build_info() == {
            "commit": "f" * 40,
            "branch": "release",
            "build_date": "2026-11-23T08:00:00+00:00",
        }
    finally:
        build_info._checkout_info.cache_clear()


def test_checkout_info_rejects_invalid_git_commit(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        build_info, "_git_value", lambda _root, *args: "not-a-commit"
    )
    build_info._checkout_info.cache_clear()
    try:
        assert build_info._checkout_info() == {}
    finally:
        build_info._checkout_info.cache_clear()


@pytest.mark.parametrize(
    "error", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 1)]
)
def test_git_value_handles_missing_or_timed_out_git(
    tmp_path, monkeypatch, error
):
    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(build_info.subprocess, "run", raise_error)
    assert build_info._git_value(tmp_path, "rev-parse", "HEAD") is None


def test_build_info_ignores_invalid_deployment_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    monkeypatch.setenv("WORKGATE_BUILD_COMMIT", "not-a-sha")
    monkeypatch.setenv("WORKGATE_BUILD_BRANCH", "   ")
    build_info._checkout_info.cache_clear()
    try:
        for build_date in ("invalid-date", "2026-11-23T08:00:00"):
            monkeypatch.setenv("WORKGATE_BUILD_DATE", build_date)
            assert build_info.build_info() == {}
    finally:
        build_info._checkout_info.cache_clear()


def test_source_root_points_to_project_checkout():
    assert build_info._source_root() == Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "returncode,stdout,expected",
    [
        (0, "  abc  ", "abc"),
        (0, "  ", None),
        (1, "abc", None),
    ],
)
def test_git_value_uses_only_successful_nonempty_output(
    tmp_path, monkeypatch, returncode, stdout, expected
):
    def fake_run(command, **kwargs):
        assert command == ["git", "-C", str(tmp_path), "rev-parse", "HEAD"]
        assert kwargs["timeout"] == 1
        return subprocess.CompletedProcess(command, returncode, stdout=stdout)

    monkeypatch.setattr(build_info.subprocess, "run", fake_run)
    assert build_info._git_value(tmp_path, "rev-parse", "HEAD") == expected


def test_checkout_info_handles_detached_head(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        build_info,
        "_git_value",
        lambda _root, *args: "b" * 40 if "rev-parse" in args else None,
    )
    build_info._checkout_info.cache_clear()
    try:
        assert build_info._checkout_info() == {"commit": "b" * 40}
    finally:
        build_info._checkout_info.cache_clear()


def test_deployment_commit_does_not_inherit_unrelated_branch(monkeypatch):
    monkeypatch.setattr(
        build_info,
        "_checkout_info",
        lambda: {"commit": "a" * 40, "branch": "main"},
    )
    monkeypatch.setenv("WORKGATE_BUILD_COMMIT", "b" * 40)
    monkeypatch.delenv("WORKGATE_BUILD_BRANCH", raising=False)
    monkeypatch.delenv("WORKGATE_BUILD_DATE", raising=False)
    assert build_info.build_info() == {"commit": "b" * 40}


def test_public_version_contract_does_not_include_provenance(monkeypatch):
    from workgate.version import diagnostic_version_info, version_info

    monkeypatch.setattr(build_info, "build_info", lambda: {"commit": "a" * 40})
    assert "commit" not in version_info()
    assert diagnostic_version_info() == {**version_info(), "commit": "a" * 40}
