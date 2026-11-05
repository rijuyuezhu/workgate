from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings, clear_settings_cache
from workgate.executor import secret_scan as secret_scan_module
from workgate.executor.files import files_config_from_executor_config
from workgate.executor.files_service import FilesService
from workgate.executor.secret_scan import (
    SecretScanService,
    _is_placeholder_secret_match,
)
from workgate.executor.services import build_runtime_services


def _services(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    settings = Settings()
    config = resolve_executor_config(settings)
    runtime = build_runtime_services(config)
    store = runtime.tool_session_store
    session = store.create_session(
        session_id="sess_0000000000000000000001", workdir=tmp_path
    )
    files = FilesService(
        files_config_from_executor_config(config),
        store,
    )
    scan = SecretScanService(
        files.config, store, config.rg_bin, config.max_grep_results
    )
    return session.session_id, files, scan


@pytest.mark.asyncio
async def testsecret_scan(tmp_path, monkeypatch):
    session_id, files, scan = _services(tmp_path, monkeypatch)
    fake_token = "gh" + "p_" + "1234567890123456789012345678901234567890"
    await files.write_file(session_id, "x.py", f"TOKEN = '{fake_token}'")

    result = await scan.scan(session_id)

    assert result.findings
    assert result.skipped_files == 0


@pytest.mark.asyncio
async def test_secret_scan_respects_gitignore(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_RG_BIN", "missing-rg-for-test")
    session_id, files, scan = _services(tmp_path, monkeypatch)
    await files.write_file(session_id, ".gitignore", "ignored.txt\n")
    ignored_token = "gh" + "p_" + "1234567890123456789012345678901234567890"
    visible_token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxy1234567890123"
    await files.write_file(
        session_id, "ignored.txt", f"TOKEN = '{ignored_token}'"
    )
    await files.write_file(
        session_id, "visible.txt", f"TOKEN = '{visible_token}'"
    )

    result = await scan.scan(session_id)

    paths = {finding.path for finding in result.findings}
    assert "visible.txt" in paths
    assert "ignored.txt" not in paths


@pytest.mark.asyncio
async def test_secret_scan_reports_failed_reads_without_hiding_other_findings(
    tmp_path, monkeypatch
):
    session_id, files, scan = _services(tmp_path, monkeypatch)
    await files.write_file(
        session_id, "readable.txt", 'password = "realistic-value-123"'
    )
    await files.write_file(session_id, "unreadable.txt", "no secrets here")

    original_read = secret_scan_module._read_file_local

    def read_or_fail(*args):
        if Path(args[-1]).name == "unreadable.txt":
            raise PermissionError("simulated unreadable file")
        return original_read(*args)

    monkeypatch.setattr(secret_scan_module, "_read_file_local", read_or_fail)

    result = await scan.scan(session_id, glob="*.txt")
    assert [finding.path for finding in result.findings] == ["readable.txt"]
    assert result.skipped_files == 1
    assert result.truncated_files == 0
    assert not result.truncated

    unreadable_only = await scan.scan(session_id, glob="unreadable.txt")
    assert unreadable_only.findings == []
    assert unreadable_only.skipped_files == 1
    assert not unreadable_only.truncated


def test_secret_scan_ignores_obvious_placeholder_assignments():
    assert _is_placeholder_secret_match(
        "generic_assignment", "SECRET = '${EXAMPLE:-dev-change-me}'"
    )
    assert _is_placeholder_secret_match(
        "generic_assignment",
        "OAUTH_SECRET = 'ci-workgate-secret-fixture'",
    )
    assert not _is_placeholder_secret_match(
        "generic_assignment", "SECRET = 'realistic-live-value-123'"
    )
    assert not _is_placeholder_secret_match(
        "github_token", "TOKEN = 'ghp_1234567890123456789012345678901234567890'"
    )
