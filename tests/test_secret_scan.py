import pytest

from workgate.config.settings import Settings, clear_settings_cache
from workgate.executor.files import files_config_from_settings
from workgate.executor.files_service import FilesService
from workgate.executor.secret_scan import (
    SecretScanService,
    _is_placeholder_secret_match,
)
from workgate.executor.services import build_runtime_services


def _services(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    settings = Settings()
    runtime = build_runtime_services(settings)
    store = runtime.tool_session_store
    session = store.create_session(
        session_id="sess_0000000000000000000001", workdir=tmp_path
    )
    files = FilesService(files_config_from_settings(settings), store)
    scan = SecretScanService(
        files.config, store, settings.rg_bin, settings.max_grep_results
    )
    return session.session_id, files, scan


@pytest.mark.asyncio
async def testsecret_scan(tmp_path, monkeypatch):
    session_id, files, scan = _services(tmp_path, monkeypatch)
    fake_token = "gh" + "p_" + "1234567890123456789012345678901234567890"
    await files.write_file(session_id, "x.py", f"TOKEN = '{fake_token}'")

    result = await scan.scan(session_id)

    assert result.findings


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
