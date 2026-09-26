import argparse
import json
import os
import textwrap

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import workgate.agent_bridge.cli as agent_cli
import workgate.control.cli as server_cli
import workgate.executor.cli as executor_cli
import workgate.executor.jobs.cli as jobs_cli
import workgate.main as cli
import workgate.standalone.cli as standalone_cli
import workgate.ui.cli as tui_cli
from workgate import __version__
from workgate.agent_bridge.auth_store import AgentAuthStore
from workgate.app_paths import app_paths
from workgate.config.executor import ExecutorConfig
from workgate.config.roles import (
    CONTROL_SETTING_NAMES,
    EXECUTOR_ONLY_SETTING_NAMES,
    EXECUTOR_SETTING_NAMES,
    SHARED_SETTING_NAMES,
)
from workgate.config.settings import Settings, load_settings
from workgate.config.surface import (
    SETTING_SPECS,
    cli_overrides_from_args,
    register_setting_cli_args,
)


def _command_parser(name: str) -> argparse.ArgumentParser:
    parser = cli._build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return subparsers.choices[name]


class NoBreakHelpFormatter(argparse.HelpFormatter):
    """Formatter used by tests to keep long environment variable names intact."""

    def _split_lines(self, text, width):
        return textwrap.wrap(
            text,
            width,
            break_long_words=False,
            break_on_hyphens=False,
        )

    def _fill_text(self, text, width, indent):
        return "\n".join(
            textwrap.wrap(
                text,
                width,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )


def test_control_subcommand_parses_control_owned_runtime_settings():
    args = cli._build_parser().parse_args(
        [
            "control",
            "--config",
            "config.yaml",
            "--mode",
            "stdio",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--auth-mode",
            "none",
            "--base-url",
            "https://example.com",
            "--oauth-admin-pin",
            "pin",
            "--ui-enabled",
            "false",
            "--max-todos",
            "17",
        ]
    )

    assert args.handler is server_cli.run_control_from_args
    assert args.config == "config.yaml"
    assert args.mode == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.auth_mode == "none"
    assert args.base_url == "https://example.com"
    assert args.oauth_admin_pin == "pin"
    assert args.ui_enabled is False
    assert args.max_todos == 17
    assert not hasattr(args, "workspace_root")
    assert not hasattr(args, "allow_full_control")


def test_legacy_server_command_is_not_registered():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["server"])


def test_standalone_subcommand_keeps_topology_protected():
    args = cli._build_parser().parse_args(
        [
            "standalone",
            "--workspace-root",
            "/tmp/workspace",
            "--port",
            "9999",
        ]
    )

    assert args.handler is standalone_cli.run_standalone_from_args
    assert args.workspace_root == "/tmp/workspace"
    assert args.port == 9999
    for name in (
        "mode",
        "host",
        "base_url",
        "auth_mode",
        "auth_bypass_localhost",
        "oauth_issuer",
        "oauth_resource",
    ):
        assert not hasattr(args, name)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--mode", "stdio"),
        ("--host", "0.0.0.0"),
        ("--base-url", "https://example.com"),
        ("--auth-mode", "none"),
        ("--auth-bypass-localhost", "true"),
        ("--oauth-issuer", "https://example.com"),
        ("--oauth-resource", "https://example.com/mcp"),
    ],
)
def test_standalone_rejects_topology_and_auth_overrides(flag, value):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["standalone", flag, value])


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--workspace-root", "/tmp/work"),
        ("--allow-full-control", "true"),
        ("--command-denylist", "shutdown"),
        ("--shell-executable", "/bin/bash"),
    ],
)
def test_control_rejects_executor_machine_policy_flags(flag, value):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["control", flag, value])


def test_root_parser_requires_an_explicit_command():
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_root_help_lists_registered_commands():
    parser = cli._build_parser()
    help_text = parser.format_help()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    for command in (
        "standalone",
        "control",
        "tui",
        "mcp",
        "executor",
        "version",
        "job-runner",
    ):
        assert command in help_text
        assert command in subparsers.choices
    assert "server" not in subparsers.choices
    assert "Run one durable job attempt (internal)" in help_text


def test_control_help_omits_executor_machine_policy(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._build_parser().parse_args(["control", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    excluded_flags = {
        spec.cli_flag
        for spec in SETTING_SPECS
        if spec.name not in CONTROL_SETTING_NAMES
    }
    for flag in excluded_flags:
        assert flag not in help_text
    assert "--max-todos" in help_text


def test_control_cli_excludes_every_executor_only_setting():
    parser = _command_parser("control")
    option_strings = {
        option for action in parser._actions for option in action.option_strings
    }
    for spec in SETTING_SPECS:
        assert (spec.cli_flag in option_strings) == (
            spec.name in CONTROL_SETTING_NAMES
        )
    assert EXECUTOR_ONLY_SETTING_NAMES.isdisjoint(CONTROL_SETTING_NAMES)


def test_agent_bridge_cli_exposes_only_control_role_settings():
    parser = _command_parser("mcp")
    option_strings = {
        option for action in parser._actions for option in action.option_strings
    }
    for spec in SETTING_SPECS:
        assert (spec.cli_flag in option_strings) == (
            spec.name in CONTROL_SETTING_NAMES
        )


def test_resolved_control_config_matches_explicit_role_surface():
    from workgate.config.control import ControlConfig

    derived_control_fields = {
        "resolved_base_url",
        "agent_config_dir",
        "agent_auth_dir",
        "audit_log_path",
        "audit_payload_dir",
    }
    assert (
        set(ControlConfig.__dataclass_fields__) - derived_control_fields
    ) == CONTROL_SETTING_NAMES


def test_standalone_executor_child_settings_match_executor_config():
    derived_executor_fields = {
        "agent_auth_dir",
        "agent_config_dir",
        "temp_dir",
    }

    assert (
        set(ExecutorConfig.__dataclass_fields__) - derived_executor_fields
    ) == EXECUTOR_SETTING_NAMES
    assert SHARED_SETTING_NAMES <= EXECUTOR_SETTING_NAMES


def test_executor_cli_exposes_only_executor_role_settings():
    for command in ("connect", "run"):
        executor = _command_parser("executor")
        actions = next(
            action
            for action in executor._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        parser = actions.choices[command]
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        for spec in SETTING_SPECS:
            assert (spec.cli_flag in option_strings) == (
                spec.name in EXECUTOR_SETTING_NAMES
            )


def test_role_cli_loading_does_not_import_foreign_config_values(
    tmp_path, monkeypatch
):
    from workgate.config.cli import settings_from_args

    config = tmp_path / "config.yaml"
    workspace = tmp_path / "executor-workspace"
    config.write_text(
        "\n".join(
            (
                f"workspace_root: {workspace}",
                "oauth_admin_pin: yaml-control-secret",
                "port: 9876",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "env-control-secret")
    monkeypatch.setenv("WORKGATE_COMMAND_DENYLIST", "executor-env-policy")

    executor_args = cli._build_parser().parse_args(
        ["executor", "run", "--config", str(config)]
    )
    executor_settings = settings_from_args(executor_args)
    assert executor_settings.workspace_root == workspace
    assert executor_settings.command_denylist == ["executor-env-policy"]
    assert executor_settings.oauth_admin_pin is None
    assert executor_settings.port == Settings().port

    control_args = cli._build_parser().parse_args(
        ["control", "--config", str(config)]
    )
    control_settings = settings_from_args(control_args)
    assert control_settings.oauth_admin_pin == "env-control-secret"
    assert control_settings.port == 9876
    assert control_settings.workspace_root != workspace
    assert "executor-env-policy" not in control_settings.command_denylist


def test_control_and_executor_discover_distinct_default_yaml_files(
    tmp_path, monkeypatch
):
    from workgate.config.cli import settings_from_args

    config_home = tmp_path / "config-home"
    workgate_config = config_home / "workgate"
    executor_config_dir = workgate_config / "executor"
    executor_config_dir.mkdir(parents=True)
    control_config = workgate_config / "config.yaml"
    executor_config = executor_config_dir / "config.yaml"
    workspace = tmp_path / "executor-workspace"
    control_config.write_text(
        "port: 9876\noauth_admin_pin: control-only-secret\n",
        encoding="utf-8",
    )
    executor_config.write_text(
        f"workspace_root: {workspace}\ncommand_denylist: [executor-only]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_ADMIN_PIN", raising=False)
    monkeypatch.delenv("WORKGATE_COMMAND_DENYLIST", raising=False)
    monkeypatch.delenv("WORKGATE_STATE_DIR", raising=False)

    control_args = cli._build_parser().parse_args(["control"])
    control = settings_from_args(control_args)
    assert control.port == 9876
    assert control.oauth_admin_pin == "control-only-secret"
    assert control.workspace_root != workspace

    executor_args = cli._build_parser().parse_args(["executor", "run"])
    executor = settings_from_args(executor_args)
    assert executor.workspace_root == workspace
    from workgate.app_paths import app_paths

    assert executor.state_dir == app_paths().executor_state_dir
    assert executor.command_denylist == ["executor-only"]
    assert executor.oauth_admin_pin is None
    assert executor.port == Settings().port


def test_workgate_config_overrides_executor_default_yaml(tmp_path, monkeypatch):
    from workgate.config.cli import settings_from_args

    config_home = tmp_path / "config-home"
    workgate_config = config_home / "workgate"
    executor_config_dir = workgate_config / "executor"
    executor_config_dir.mkdir(parents=True)
    (executor_config_dir / "config.yaml").write_text(
        f"workspace_root: {tmp_path / 'default-workspace'}\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "selected.yaml"
    selected_workspace = tmp_path / "selected-workspace"
    explicit.write_text(
        f"workspace_root: {selected_workspace}\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("WORKGATE_CONFIG", str(explicit))
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)

    args = cli._build_parser().parse_args(["executor", "run"])
    assert settings_from_args(args).workspace_root == selected_workspace


def test_version_option_prints_package_version(capsys):
    parser = cli._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"workgate {__version__}\n"


def test_version_subcommand_prints_package_version(capsys):
    args = cli._build_parser().parse_args(["version"])

    args.handler(args)

    assert capsys.readouterr().out.startswith(f"workgate {__version__}")


def test_every_setting_has_generic_cli_option():
    parser = argparse.ArgumentParser(formatter_class=NoBreakHelpFormatter)
    register_setting_cli_args(parser)
    help_text = parser.format_help()

    assert "<object object at" not in help_text
    assert "--audit-log-path" not in help_text
    assert "--agent-config-dir" not in help_text
    for spec in SETTING_SPECS:
        assert spec.cli_flag in help_text
        assert spec.env_var in help_text
        if spec.is_nullable:
            assert spec.unset_cli_flag in help_text
        else:
            assert spec.unset_cli_flag not in help_text


def test_removed_remote_worker_settings_stay_out_of_public_config_surface():
    removed = {
        "remote_enabled",
        "remote_invite_ttl_s",
        "remote_poll_timeout_s",
        "remote_job_timeout_s",
        "remote_max_pending_jobs",
    }
    spec_names = {spec.name for spec in SETTING_SPECS}
    parser = argparse.ArgumentParser()
    register_setting_cli_args(parser)
    help_text = parser.format_help()

    assert removed.isdisjoint(Settings.model_fields)
    assert removed.isdisjoint(spec_names)
    for name in removed:
        assert f"--{name.replace('_', '-')}" not in help_text


def test_nullable_cli_values_can_be_explicitly_unset():
    args = cli._build_parser().parse_args(
        ["control", "--unset-base-url", "--unset-oauth-admin-pin"]
    )

    assert args.base_url is None
    assert args.oauth_admin_pin is None
    assert cli_overrides_from_args(args) == {
        "base_url": None,
        "oauth_admin_pin": None,
    }


def test_nullable_cli_value_and_unset_flag_are_mutually_exclusive():
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "control",
                "--base-url",
                "https://example.com",
                "--unset-base-url",
            ]
        )


def test_bool_cli_values_parse_explicitly():
    parser = cli._build_parser()

    assert (
        parser.parse_args(["control", "--ui-enabled", "true"]).ui_enabled
        is True
    )
    assert (
        parser.parse_args(["control", "--ui-enabled", "false"]).ui_enabled
        is False
    )


def test_removed_remote_transfer_flag_is_not_accepted():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["control", "--remote-http-transfer-enabled", "false"]
        )


def test_executor_connect_subcommand_parses_final_pairing_contract():
    args = cli._build_parser().parse_args(
        [
            "executor",
            "connect",
            "https://example.com",
            "--name",
            "npu-4card",
            "--workspace-root",
            "/home/user/project",
        ]
    )

    assert args.handler is executor_cli._connect_from_args
    assert args.executor_command == "connect"
    assert args.control_url == "https://example.com"
    assert args.name == "npu-4card"
    assert args.workspace_root == "/home/user/project"
    assert not hasattr(args, "invite")


def test_tui_subcommand_parses_loopback_api_base():
    args = cli._build_parser().parse_args(
        ["tui", "--port", "9443", "--api-base", "https://localhost:9443/api/ui"]
    )

    assert args.handler is tui_cli.run_tui_from_args
    assert args.port == 9443
    assert args.api_base == "https://localhost:9443/api/ui"


def test_tui_cli_exposes_only_control_role_settings():
    parser = _command_parser("tui")
    option_strings = {
        option for action in parser._actions for option in action.option_strings
    }
    for spec in SETTING_SPECS:
        assert (spec.cli_flag in option_strings) == (
            spec.name in CONTROL_SETTING_NAMES
        )


def test_tui_handler_uses_configured_port_and_settings(monkeypatch, tmp_path):
    from workgate.config.control import get_control_config
    from workgate.persistence import get_state_store

    calls = []

    def fake_run(api_base, *, settings):
        assert get_control_config() is settings
        assert get_state_store().layout.root == settings.state_dir
        calls.append((api_base, settings.port, settings.ui_tui_command))
        return 0

    monkeypatch.setattr(tui_cli, "run_tui", fake_run)
    state_dir = tmp_path / "tui-state"
    args = cli._build_parser().parse_args(
        [
            "tui",
            "--port",
            "9555",
            "--state-dir",
            str(state_dir),
            "--ui-tui-command",
            "/opt/tui",
        ]
    )

    with pytest.raises(SystemExit) as exc_info:
        args.handler(args)

    assert exc_info.value.code == 0
    assert calls == [("http://127.0.0.1:9555/api/ui", 9555, "/opt/tui")]


def test_main_dispatches_to_argparse_handler(monkeypatch):
    calls = []

    def run_from_args(args):
        calls.append(args.mode)

    monkeypatch.setattr(server_cli, "run_control_from_args", run_from_args)

    cli.main(["control", "--mode", "stdio"])

    assert calls == ["stdio"]


def test_control_dispatches_transport_modes(monkeypatch):
    calls = []
    mode = "http"
    runtime = argparse.Namespace(config=argparse.Namespace(mode=mode))

    monkeypatch.setattr(
        server_cli,
        "build_control_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(
        server_cli,
        "run_http",
        lambda *, runtime: calls.append(("http", runtime)),
    )
    monkeypatch.setattr(
        server_cli,
        "run_mcp",
        lambda *, runtime: calls.append(("mcp", runtime)),
    )
    settings = object()

    server_cli._dispatch_control(settings)
    mode = "mcp"
    runtime.config.mode = mode
    server_cli._dispatch_control(settings)
    mode = "stdio"
    runtime.config.mode = mode
    server_cli._dispatch_control(settings)

    assert calls == [
        ("http", runtime),
        ("mcp", runtime),
        ("mcp", runtime),
    ]

    runtime.config.mode = "both"
    with pytest.raises(SystemExit, match="mode=both is reserved"):
        server_cli._dispatch_control(settings)

    runtime.config.mode = "unexpected"
    with pytest.raises(SystemExit, match="Unsupported mode"):
        server_cli._dispatch_control(settings)


def test_control_handler_initializes_only_control_owned_directories(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "executor-workspace"
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    settings = Settings(
        mode="mcp",
        workspace_root=workspace,
        state_dir=state_dir,
        data_dir=data_dir,
    )
    configured = []
    dispatched = []

    def load_from_args(_args):
        return settings

    monkeypatch.setattr(server_cli, "settings_from_args", load_from_args)
    monkeypatch.setattr(
        server_cli,
        "configure_settings",
        lambda active: configured.append(active),
    )
    monkeypatch.setattr(
        server_cli,
        "_dispatch_control",
        lambda active: dispatched.append(active),
    )

    server_cli.run_control_from_args(argparse.Namespace())

    assert not workspace.exists()
    assert state_dir.is_dir()
    assert data_dir.is_dir()
    assert settings.audit_log_path.parent.is_dir()
    if os.name != "nt":
        assert state_dir.stat().st_mode & 0o777 == 0o700
        assert data_dir.stat().st_mode & 0o777 == 0o700
        assert settings.audit_log_path.parent.stat().st_mode & 0o777 == 0o700
    assert configured == [settings]
    assert dispatched == [settings]


def test_control_handler_rejects_a_second_writer(tmp_path, monkeypatch):
    settings = Settings(
        mode="mcp",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(
        server_cli,
        "settings_from_args",
        lambda _args: settings,
    )
    monkeypatch.setattr(
        server_cli, "configure_settings", lambda _settings: None
    )
    monkeypatch.setattr(
        server_cli,
        "prepare_standalone_control_settings",
        lambda _settings: pytest.fail(
            "duplicate control touched standalone control secrets"
        ),
    )
    monkeypatch.setattr(
        server_cli,
        "_dispatch_control",
        lambda _settings: pytest.fail("duplicate control reached dispatch"),
    )

    with (
        server_cli.control_run_lock(settings.state_dir),
        pytest.raises(SystemExit, match="already active"),
    ):
        server_cli.run_control_from_args(argparse.Namespace())


def test_control_run_lock_preserves_body_timeout_errors(tmp_path):
    with (
        pytest.raises(TimeoutError, match="body timeout"),
        server_cli.control_run_lock(tmp_path / "state"),
    ):
        raise TimeoutError("body timeout")


def test_internal_job_runner_is_dispatched_by_argparse(monkeypatch):
    help_text = cli._build_parser().format_help()
    calls = []

    def run_job_runner(args):
        calls.append(
            (
                args.command_file,
                args.log_file,
                args.status_file,
                args.cwd,
                args.shell,
                args.max_log_bytes,
            )
        )

    monkeypatch.setattr(jobs_cli, "run_job_runner_from_args", run_job_runner)

    assert "job-runner" in help_text
    cli.main(
        [
            "job-runner",
            "--command-file",
            "command.txt",
            "--log-file",
            "job.log",
            "--status-file",
            "status.json",
            "--cwd",
            "/tmp/work",
            "--shell",
            "/bin/sh",
            "--max-log-bytes",
            "1234",
        ]
    )

    assert calls == [
        (
            "command.txt",
            "job.log",
            "status.json",
            "/tmp/work",
            "/bin/sh",
            1234,
        )
    ]


def test_control_overrides_include_only_explicit_values():
    args = cli._build_parser().parse_args(["control", "--mode", "stdio"])

    assert cli_overrides_from_args(args) == {"mode": "stdio"}


def _write_agent_manifest(state_dir, server):
    _ = state_dir
    server = {"integrationId": "docs", **server}
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1, "mcpServers": {"docs": server}}),
        encoding="utf-8",
    )


def test_mcp_credential_subcommands_parse_to_public_handler():
    parser = cli._build_parser()

    auth = parser.parse_args(["mcp", "auth", "docs", "--status"])
    no_open = parser.parse_args(["mcp", "auth", "docs", "--no-open"])
    secret_set = parser.parse_args(
        ["mcp", "secret", "set", "docs", "token", "--stdin"]
    )
    secret_list = parser.parse_args(["mcp", "secret", "list", "docs"])
    secret_delete = parser.parse_args(
        ["mcp", "secret", "delete", "docs", "token"]
    )

    assert auth.handler is agent_cli.run_mcp_cli_from_args
    assert auth.status is True
    assert no_open.no_open is True
    assert secret_set.stdin is True
    assert secret_list.server == "docs"
    assert secret_delete.name == "token"


def test_mcp_auth_actions_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["mcp", "auth", "docs", "--status", "--no-open"]
        )


def test_mcp_secret_set_list_delete_never_print_values(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        },
    )
    parser = cli._build_parser()
    monkeypatch.setattr(
        agent_cli, "_read_secret_stdin", lambda: "private-value"
    )

    set_args = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "set",
            "docs",
            "token",
            "--stdin",
        ]
    )
    set_args.handler(set_args)
    set_output = capsys.readouterr().out
    assert "private-value" not in set_output
    assert json.loads(set_output)["stored"] is True
    store = AgentAuthStore(state_dir / "agent_auth")
    assert store.get_secret("docs", "token") == "private-value"

    list_args = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "secret", "list", "docs"]
    )
    list_args.handler(list_args)
    list_output = capsys.readouterr().out
    assert "private-value" not in list_output
    assert json.loads(list_output) == {"secrets": {"docs": ["token"]}}

    delete_args = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "delete",
            "docs",
            "token",
        ]
    )
    delete_args.handler(delete_args)
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    assert store.list_secrets("docs") == {}


def test_mcp_secret_list_keeps_current_server_name_after_manifest_rename(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs2": {
                        "integrationId": "docs",
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "enabled": False,
                        "headers": {"Authorization": {"secret": "token"}},
                        "auth": {"mode": "secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    AgentAuthStore(state_dir / "agent_auth").set_secret(
        "docs", "token", "private-value"
    )
    parser = cli._build_parser()
    args = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "secret", "list", "docs2"]
    )

    args.handler(args)

    output = capsys.readouterr().out
    assert "private-value" not in output
    assert json.loads(output) == {"secrets": {"docs2": ["token"]}}


def test_mcp_secret_cleanup_survives_manifest_removal(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        },
    )
    parser = cli._build_parser()
    monkeypatch.setattr(
        agent_cli, "_read_secret_stdin", lambda: "private-value"
    )
    set_args = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "set",
            "docs",
            "token",
            "--stdin",
        ]
    )
    set_args.handler(set_args)
    capsys.readouterr()

    (app_paths().agent_config_dir / "config.json").unlink()

    list_all_args = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "secret", "list"]
    )
    list_all_args.handler(list_all_args)
    list_all_output = capsys.readouterr().out
    assert "private-value" not in list_all_output
    assert json.loads(list_all_output) == {"secrets": {"docs": ["token"]}}

    list_args = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "secret", "list", "docs"]
    )
    list_args.handler(list_args)
    assert json.loads(capsys.readouterr().out) == {
        "secrets": {"docs": ["token"]}
    }

    delete_args = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "delete",
            "docs",
            "token",
        ]
    )
    delete_args.handler(delete_args)
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    assert AgentAuthStore(state_dir / "agent_auth").list_secrets("docs") == {}


def test_mcp_secret_cleanup_prefers_exact_stored_identity_over_live_label(
    tmp_path, capsys
):
    state_dir = tmp_path / "state"
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "shared": {
                        "integrationId": "current",
                        "type": "http",
                        "url": "https://current.example.test/mcp",
                        "enabled": False,
                        "headers": {"Authorization": {"secret": "token"}},
                        "auth": {"mode": "secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = AgentAuthStore(state_dir / "agent_auth")
    store.set_secret("shared", "retired", "old-private-value")
    store.set_secret("current", "token", "current-private-value")
    parser = cli._build_parser()

    list_args = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "secret", "list", "shared"]
    )
    list_args.handler(list_args)
    output = capsys.readouterr().out
    assert "old-private-value" not in output
    assert "current-private-value" not in output
    assert json.loads(output) == {"secrets": {"shared": ["retired"]}}

    # The detached bucket owns this exact identifier as a whole. A missing
    # secret must not fall through to the live server that reuses the label.
    wrong_bucket_delete = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "delete",
            "shared",
            "token",
        ]
    )
    wrong_bucket_delete.handler(wrong_bucket_delete)
    assert json.loads(capsys.readouterr().out)["deleted"] is False
    assert store.list_secrets("shared") == {"shared": ["retired"]}
    assert store.list_secrets("current") == {"current": ["token"]}

    delete_args = parser.parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "secret",
            "delete",
            "shared",
            "retired",
        ]
    )
    delete_args.handler(delete_args)
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    assert store.list_secrets("shared") == {}
    assert store.list_secrets("current") == {"current": ["token"]}


def test_mcp_auth_status_reports_only_safe_metadata(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "auth": {"mode": "oauth", "scopes": ["tools.read"]},
        },
    )
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--status"]
    )

    args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "authorized": False,
        "client_registered": False,
        "expires_at": None,
        "mode": "oauth",
        "server": "docs",
        "status": "unauthorized",
    }


def test_mcp_auth_no_open_runs_interactive_authorization(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "auth": {"mode": "oauth"},
        },
    )
    calls = []

    async def fake_authorize(settings, server_name, server, *, no_open):
        calls.append((settings.state_dir, server_name, server.url, no_open))
        return {"server": server_name, "authorized": True}

    monkeypatch.setattr(agent_cli, "authorize_server", fake_authorize)
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--no-open"]
    )

    args.handler(args)

    assert calls == [
        (state_dir.resolve(), "docs", "https://example.test/mcp", True)
    ]
    assert json.loads(capsys.readouterr().out)["authorized"] is True


def test_mcp_logout_reports_revocation_and_clears_local_credentials(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "auth": {"mode": "oauth"},
        },
    )
    store = AgentAuthStore(state_dir / "agent_auth")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "access", "token_type": "Bearer"}
        ),
    )

    async def fake_revoke(_store, server_name, server):
        assert server_name == "docs"
        assert server.url == "https://example.test/mcp"
        return agent_cli.RevocationResult(
            "unsupported", "no revocation endpoint advertised"
        )

    monkeypatch.setattr(agent_cli, "revoke_stored_oauth", fake_revoke)
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )

    args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "unsupported"
    assert payload["local_credentials_cleared"] is True
    assert store.get_tokens("docs") is None


def test_mcp_logout_cleans_detached_oauth_state_without_remote_revocation(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    store = AgentAuthStore(state_dir / "agent_auth")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "detached-access",
                "refresh_token": "detached-refresh",
                "token_type": "Bearer",
            }
        ),
    )
    store.set_client_info(
        "docs",
        OAuthClientInformationFull.model_validate(
            {
                "client_id": "client-1",
                "client_secret": "detached-client-secret",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }
        ),
    )

    async def unexpected_revoke(*_args):
        raise AssertionError(
            "detached cleanup must not attempt remote revocation"
        )

    monkeypatch.setattr(agent_cli, "revoke_stored_oauth", unexpected_revoke)
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )

    args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "unavailable"
    assert payload["local_credentials_cleared"] is True
    assert store.get_tokens("docs") is None
    assert store.get_client_info("docs") is None
    assert store.has_oauth_state("docs") is False


def test_mcp_logout_resolves_live_oauth_by_stable_integration_id_after_rename(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs-renamed": {
                        "integrationId": "docs-stable",
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "enabled": False,
                        "auth": {"mode": "oauth"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = AgentAuthStore(state_dir / "agent_auth")
    store.set_tokens(
        "docs-stable",
        OAuthToken.model_validate(
            {"access_token": "access", "token_type": "Bearer"}
        ),
    )
    calls = []

    async def fake_revoke(_store, server_name, server):
        calls.append((server_name, server.integration_id))
        return agent_cli.RevocationResult("revoked")

    monkeypatch.setattr(agent_cli, "revoke_stored_oauth", fake_revoke)
    args = cli._build_parser().parse_args(
        [
            "mcp",
            "--state-dir",
            str(state_dir),
            "auth",
            "docs-stable",
            "--logout",
        ]
    )

    args.handler(args)

    assert calls == [("docs-renamed", "docs-stable")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "revoked"
    assert payload["local_credentials_cleared"] is True
    assert store.has_oauth_state("docs-stable") is False

    # With the bucket now empty, the stable integrationId still resolves the
    # renamed live server instead of being treated as an unknown label.
    calls.clear()
    args.handler(args)
    assert calls == [("docs-renamed", "docs-stable")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "revoked"
    assert payload["local_credentials_cleared"] is False


def test_mcp_logout_detached_identity_wins_over_reused_live_display_name(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "integrationId": "current",
            "type": "http",
            "url": "https://current.example.test/mcp",
            "enabled": False,
            "auth": {"mode": "oauth"},
        },
    )
    store = AgentAuthStore(state_dir / "agent_auth")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "retired-access", "token_type": "Bearer"}
        ),
    )
    store.set_tokens(
        "current",
        OAuthToken.model_validate(
            {"access_token": "current-access", "token_type": "Bearer"}
        ),
    )

    async def unexpected_revoke(*_args):
        raise AssertionError(
            "stale identity must not revoke the reused live label"
        )

    monkeypatch.setattr(agent_cli, "revoke_stored_oauth", unexpected_revoke)
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )

    args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "unavailable"
    assert payload["local_credentials_cleared"] is True
    assert store.has_oauth_state("docs") is False
    assert store.get_tokens("current") is not None


def test_secret_stdin_reader_validates_tty_size_encoding_and_newlines(
    monkeypatch,
):
    import io
    from types import SimpleNamespace

    def install(data: bytes, *, tty: bool = False):
        monkeypatch.setattr(
            agent_cli.sys,
            "stdin",
            SimpleNamespace(isatty=lambda: tty, buffer=io.BytesIO(data)),
        )

    install(b"ignored", tty=True)
    with pytest.raises(ValueError, match="interactive terminal"):
        agent_cli._read_secret_stdin()

    install(b"x" * 65_537)
    with pytest.raises(ValueError, match="exceeds"):
        agent_cli._read_secret_stdin()

    install(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        agent_cli._read_secret_stdin()

    install(b"value\r\n")
    assert agent_cli._read_secret_stdin() == "value"
    install(b"value\n")
    assert agent_cli._read_secret_stdin() == "value"
    install(b"")
    with pytest.raises(ValueError, match="must not be empty"):
        agent_cli._read_secret_stdin()


def test_mcp_cli_reports_manifest_and_server_errors(tmp_path, capsys):
    state_dir = tmp_path / "state"
    parser = cli._build_parser()
    missing_manifest = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--status"]
    )
    with pytest.raises(SystemExit, match="2"):
        missing_manifest.handler(missing_manifest)
    assert "manifest is unavailable" in capsys.readouterr().err

    missing_manifest_logout = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )
    with pytest.raises(SystemExit, match="2"):
        missing_manifest_logout.handler(missing_manifest_logout)
    assert "manifest is unavailable" in capsys.readouterr().err

    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
        },
    )
    unknown = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "unknown", "--status"]
    )
    with pytest.raises(SystemExit, match="2"):
        unknown.handler(unknown)
    assert "Unknown Agent Bridge MCP server" in capsys.readouterr().err

    unknown_logout = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "unknown", "--logout"]
    )
    with pytest.raises(SystemExit, match="2"):
        unknown_logout.handler(unknown_logout)
    assert "Unknown Agent Bridge MCP server" in capsys.readouterr().err

    non_oauth = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--status"]
    )
    with pytest.raises(SystemExit, match="2"):
        non_oauth.handler(non_oauth)
    assert "not configured for OAuth" in capsys.readouterr().err

    non_oauth_logout = parser.parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )
    with pytest.raises(SystemExit, match="2"):
        non_oauth_logout.handler(non_oauth_logout)
    assert "not configured for OAuth" in capsys.readouterr().err


def test_mcp_cli_failed_remote_revocation_exits_one(
    monkeypatch, tmp_path, capsys
):
    state_dir = tmp_path / "state"
    _write_agent_manifest(
        state_dir,
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "auth": {"mode": "oauth"},
        },
    )

    async def fake_revoke(*_args):
        return agent_cli.RevocationResult("failed", "remote unavailable")

    monkeypatch.setattr(agent_cli, "revoke_stored_oauth", fake_revoke)
    args = cli._build_parser().parse_args(
        ["mcp", "--state-dir", str(state_dir), "auth", "docs", "--logout"]
    )
    with pytest.raises(SystemExit, match="1"):
        args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_revocation"] == "failed"
    assert payload["detail"] == "remote unavailable"


def test_mcp_cli_rejects_unknown_secret_dispatch(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    state_dir = tmp_path / "state"
    args = Namespace(
        config=None,
        state_dir=str(state_dir),
        mcp_command="secret",
        secret_command="unknown",
        server="docs",
        name="token",
    )
    monkeypatch.setattr(
        agent_cli,
        "_settings_from_args",
        lambda _args: load_settings(None, {"state_dir": str(state_dir)}),
    )
    with pytest.raises(SystemExit, match="2"):
        agent_cli.run_mcp_cli_from_args(args)
    assert "unsupported secret command" in capsys.readouterr().err
