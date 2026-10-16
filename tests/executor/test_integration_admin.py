"""Owner-side admin RPC rejects malformed untrusted inputs safely."""

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.integration_admin import manage_executor_integration


@pytest.mark.asyncio
async def test_integration_admin_rejects_malformed_arguments(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config = resolve_executor_config(
        Settings(default_workdir=workdir, state_dir=tmp_path / "state")
    )
    cases = [
        ("ui.skill.manage", {"action": 17, "name": "x"}),
        ("ui.skill.manage", {"action": "get", "name": []}),
        ("ui.skill.manage", {"action": "update", "name": "x", "content": []}),
        ("ui.mcp.auth", {"action": "status"}),
        ("ui.mcp.auth", {"action": "status", "name": "x", "secret_name": []}),
        (
            "ui.mcp.auth",
            {"action": "set_secret", "name": "x", "secret_value": []},
        ),
        ("invalid", {"action": "list"}),
        ("ui.mcp.manage", {"action": "register", "name": "x", "config": "bad"}),
        ("ui.mcp.manage", {"action": "list", "expected_revision": []}),
    ]
    for op, args in cases:
        with pytest.raises(ValueError):
            await manage_executor_integration(config, op, args)
