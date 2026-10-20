"""Preserve subprocess secret filtering with Windows environment key semantics."""

import pytest

import workgate.utils.processes as processes
from workgate.platform.host import HostOS


@pytest.mark.parametrize(
    "name",
    [
        "pythonpath",
        "PyThOnPaTh",
        "workgate_secret",
        "docker_host",
        "private_token",
        "custom_password",
    ],
)
def test_windows_env_blocklist_is_case_insensitive(monkeypatch, name):
    monkeypatch.setattr(processes, "host_os", lambda: HostOS.WINDOWS)
    assert processes.user_subprocess_env_name_blocked(
        name,
        blocked_names=("CUSTOM_PASSWORD",),
        blocked_prefixes=("PRIVATE_",),
    )


def test_posix_env_blocklist_remains_case_sensitive(monkeypatch):
    monkeypatch.setattr(processes, "host_os", lambda: HostOS.LINUX)
    assert not processes.user_subprocess_env_name_blocked("workgate_secret")
    assert not processes.user_subprocess_env_name_blocked(
        "private_token",
        blocked_prefixes=("PRIVATE_",),
    )


def test_windows_env_filter_removes_case_variant_secrets(monkeypatch):
    monkeypatch.setattr(processes, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setenv("workgate_hidden_secret", "secret")
    monkeypatch.setenv("pRiVaTe_KeY", "secret")
    monkeypatch.setenv("visible_windows_test", "safe")
    cleaned = processes.user_subprocess_env(
        blocked_prefixes=("PRIVATE_",),
        frozen=False,
    )
    # Windows normalizes environment names case-insensitively; POSIX preserves
    # caller casing. Neither changes the filtering guarantee.
    folded = {key.casefold(): value for key, value in cleaned.items()}
    assert "workgate_hidden_secret" not in folded
    assert "private_key" not in folded
    assert folded["visible_windows_test"] == "safe"
