import ast
import subprocess
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

from workgate.config.roles import EXECUTOR_ONLY_SETTING_NAMES

_PROJECT_ROOT = Path(__file__).parents[1]
_PACKAGE_ROOT = _PROJECT_ROOT / "src" / "workgate"
_PACKAGE_NAME = "workgate"

# `main` composes only domain CLI registrars. Each HTTP-capable control adapter consumes
# exactly one shared Human UI route-composition contract.
_ALLOWED_HTTP_CONTROL_UI_IMPORTS = frozenset(
    {
        (
            "workgate.control.http.app",
            "workgate.ui.http.routes",
        ),
    }
)
_ALLOWED_MCP_CONTROL_UI_IMPORTS = frozenset(
    {
        (
            "workgate.control.mcp.app",
            "workgate.ui.http.routes",
        ),
    }
)
_ALLOWED_NON_CONTROL_TO_CONTROL_IMPORTS = frozenset(
    {
        ("workgate.main", "workgate.control.cli"),
        # Optional hosted adapters consume the stable control-side core but
        # must not become a dependency of control itself.
        ("workgate.hosted.actor", "workgate.control.executor_transport"),
        ("workgate.hosted.actor", "workgate.control.pairing"),
        ("workgate.hosted.actor", "workgate.control.sessions"),
        ("workgate.hosted.actor", "workgate.control.state"),
        ("workgate.hosted.actor", "workgate.control.streams"),
        # These are presentation/download adapters that are part of the control
        # deployment even though their historical package names are shared.
        ("workgate.http.downloads", "workgate.control.download_store"),
        ("workgate.http.downloads", "workgate.control.downloads"),
        ("workgate.ui.http.dashboard", "workgate.control.ui_executor"),
        ("workgate.ui.http.files", "workgate.control.ui_executor"),
        ("workgate.ui.http.terminals", "workgate.control.ui_executor"),
        ("workgate.ui.http.todos", "workgate.control.todos"),
    }
)
_ALLOWED_HTTP_TO_CONTROL_IMPORTS = frozenset(
    {
        ("workgate.http.downloads", "workgate.control.download_store"),
        ("workgate.http.downloads", "workgate.control.downloads"),
    }
)
_ALLOWED_UI_HTTP_TO_CONTROL_IMPORTS = frozenset(
    {
        ("workgate.ui.http.dashboard", "workgate.control.ui_executor"),
        ("workgate.ui.http.files", "workgate.control.ui_executor"),
        ("workgate.ui.http.terminals", "workgate.control.ui_executor"),
        ("workgate.ui.http.todos", "workgate.control.todos"),
    }
)
_ALLOWED_RELEASE_IMPORTS = frozenset(
    {
        (
            "workgate.release.platform_wheel",
            "workgate.ui.contracts",
        ),
    }
)

# Keep dependency cycles explicit. The current architecture has none.
_ALLOWED_DEPENDENCY_CYCLES: frozenset[frozenset[str]] = frozenset()


def _module_name(path: Path) -> str:
    relative = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((_PACKAGE_NAME, *parts))


def _resolve_import_from(
    importer: str,
    importer_path: Path,
    node: ast.ImportFrom,
) -> str | None:
    if node.level == 0:
        return node.module

    importer_parts = importer.split(".")
    package_parts = (
        importer_parts
        if importer_path.name == "__init__.py"
        else importer_parts[:-1]
    )
    parent_count = node.level - 1
    if parent_count > len(package_parts):
        return None

    resolved = package_parts[: len(package_parts) - parent_count]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _source_modules() -> dict[str, Path]:
    return {
        _module_name(path): path for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
    }


def _local_imports() -> set[tuple[str, str]]:
    imports: set[tuple[str, str]] = set()
    for importer, path in _source_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_import_from(importer, path, node)
                if target:
                    targets.append(target)
            for target in targets:
                if target.startswith(f"{_PACKAGE_NAME}."):
                    imports.add((importer, target))
    return imports


def _module_graph() -> dict[str, set[str]]:
    modules = _source_modules()
    graph: dict[str, set[str]] = defaultdict(set)
    for importer, target in _local_imports():
        candidate = target
        while candidate not in modules and "." in candidate:
            candidate = candidate.rsplit(".", 1)[0]
        if candidate in modules and candidate != importer:
            graph[importer].add(candidate)
    return graph


def _dependency_cycles() -> frozenset[frozenset[str]]:
    graph = _module_graph()
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    cycles: set[frozenset[str]] = set()

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        low_links[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)

        for dependency in graph.get(module, ()):
            if dependency not in indices:
                visit(dependency)
                low_links[module] = min(
                    low_links[module], low_links[dependency]
                )
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indices[dependency])

        if low_links[module] != indices[module]:
            return

        component: set[str] = set()
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.add(dependency)
            if dependency == module:
                break
        if len(component) > 1:
            cycles.add(frozenset(component))

    for module in _source_modules():
        if module not in indices:
            visit(module)
    return frozenset(cycles)


def test_main_cli_stays_a_thin_composition_root() -> None:
    main = f"{_PACKAGE_NAME}.main"
    actual = {
        target for importer, target in _local_imports() if importer == main
    }

    assert actual
    assert all(
        target == f"{_PACKAGE_NAME}.version" or target.endswith(".cli")
        for target in actual
    )


def test_control_imports_match_explicit_process_composition() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if target.startswith(f"{_PACKAGE_NAME}.control.")
        and not importer.startswith(f"{_PACKAGE_NAME}.control.")
    )

    assert actual == _ALLOWED_NON_CONTROL_TO_CONTROL_IMPORTS


def test_final_control_executor_roots_are_explicit() -> None:
    assert (_PACKAGE_ROOT / "control").is_dir()
    assert (_PACKAGE_ROOT / "executor").is_dir()
    assert not (_PACKAGE_ROOT / "executors").exists()


def test_control_does_not_depend_on_executor_composition() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.control")
        and target.startswith(f"{_PACKAGE_NAME}.executor")
    )

    assert actual == frozenset()


def test_standalone_supervisor_has_no_control_or_executor_implementation_dependency() -> (
    None
):
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.standalone")
        and (
            target.startswith(f"{_PACKAGE_NAME}.control")
            or target.startswith(f"{_PACKAGE_NAME}.executor")
        )
    )

    assert actual == frozenset()


def test_hosted_adapter_does_not_depend_on_executor_implementation() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.hosted")
        and target.startswith(f"{_PACKAGE_NAME}.executor")
    )

    assert actual == frozenset()


def test_hosted_adapter_has_no_provider_sdk_dependency() -> None:
    forbidden = (
        "workers",
        "cloudflare",
        "redis",
        "threading",
        "multiprocessing",
    )
    imports: set[tuple[str, str]] = set()
    for module, path in _source_modules().items():
        if not module.startswith(f"{_PACKAGE_NAME}.hosted"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update((module, alias.name) for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
            ):
                imports.add((module, node.module))

    assert not {
        (module, target)
        for module, target in imports
        if any(
            target == name or target.startswith(f"{name}.")
            for name in forbidden
        )
    }


def test_executor_has_no_legacy_remote_worker_dependency() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.executor")
        and target.startswith(f"{_PACKAGE_NAME}.remote_worker")
    )

    assert actual == frozenset()


def test_mcp_control_has_only_the_explicit_ui_route_dependency() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.control.mcp")
        and (
            target.startswith(f"{_PACKAGE_NAME}.server.")
            or target.startswith(f"{_PACKAGE_NAME}.ui.")
        )
    )

    assert actual == _ALLOWED_MCP_CONTROL_UI_IMPORTS


def test_http_control_has_only_the_explicit_ui_route_dependency() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.control.http")
        and (
            target.startswith(f"{_PACKAGE_NAME}.server.")
            or target.startswith(f"{_PACKAGE_NAME}.ui.")
        )
    )

    assert actual == _ALLOWED_HTTP_CONTROL_UI_IMPORTS


def test_http_has_only_explicit_control_owned_download_dependencies() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.http")
        and target.startswith(f"{_PACKAGE_NAME}.control.")
    )

    assert actual == _ALLOWED_HTTP_TO_CONTROL_IMPORTS


def test_telemetry_does_not_depend_on_ui_or_transport_adapters() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.http.",
        f"{_PACKAGE_NAME}.server.",
        f"{_PACKAGE_NAME}.ui.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.telemetry")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


def test_ui_core_does_not_depend_on_control_or_http_adapters() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.server.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.ui")
        and not importer.startswith(f"{_PACKAGE_NAME}.ui.http")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


def test_ui_http_has_only_explicit_control_adapter_dependencies() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.ui.http")
        and target.startswith(f"{_PACKAGE_NAME}.control.")
    )

    assert actual == _ALLOWED_UI_HTTP_TO_CONTROL_IMPORTS


def test_terminal_does_not_depend_on_transports_or_ui() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.http.",
        f"{_PACKAGE_NAME}.server.",
        f"{_PACKAGE_NAME}.ui.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.executor.terminal")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


def test_audit_does_not_depend_on_delivery_or_terminal_layers() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.http.",
        f"{_PACKAGE_NAME}.server.",
        f"{_PACKAGE_NAME}.terminal.",
        f"{_PACKAGE_NAME}.ui.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.audit")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


def test_patch_mechanics_stay_below_delivery_layers() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.http.",
        f"{_PACKAGE_NAME}.server.",
        f"{_PACKAGE_NAME}.ui.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer == f"{_PACKAGE_NAME}.executor.patch.envelope"
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


def test_release_uses_only_the_ui_artifact_contract() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.release")
    )

    assert actual == _ALLOWED_RELEASE_IMPORTS


def test_ui_artifact_contract_is_a_dependency_leaf() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer == f"{_PACKAGE_NAME}.ui.contracts"
    )

    assert actual == frozenset()


def test_terminal_uses_only_low_level_ops_helpers() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.executor.terminal")
        and target.startswith(f"{_PACKAGE_NAME}.ops.")
    )

    assert all(
        target.startswith(f"{_PACKAGE_NAME}.ops.utils.")
        for _importer, target in actual
    )


def test_agent_bridge_data_dependencies_follow_layering() -> None:
    layers = {
        f"{_PACKAGE_NAME}.agent_bridge.models": 0,
        f"{_PACKAGE_NAME}.agent_bridge.redaction": 0,
        f"{_PACKAGE_NAME}.agent_bridge.auth": 1,
        f"{_PACKAGE_NAME}.agent_bridge.skills": 1,
        f"{_PACKAGE_NAME}.agent_bridge.sources": 2,
        f"{_PACKAGE_NAME}.agent_bridge.status": 2,
    }
    violations = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer in layers
        and target in layers
        and layers[target] > layers[importer]
    )

    assert violations == frozenset()


def test_agent_bridge_models_are_a_dependency_leaf() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer == f"{_PACKAGE_NAME}.agent_bridge.models"
    )

    assert actual == frozenset()


_EXECUTOR_POLICY_FIELDS = EXECUTOR_ONLY_SETTING_NAMES


def test_machine_session_and_shell_job_implementations_are_executor_owned() -> (
    None
):
    assert (_PACKAGE_ROOT / "executor" / "tool_session").is_dir()
    assert not (_PACKAGE_ROOT / "tool_session").exists()

    executor_jobs = _PACKAGE_ROOT / "executor" / "jobs"
    shared_jobs = _PACKAGE_ROOT / "jobs"
    for name in (
        "shell.py",
        "lifecycle.py",
        "runner.py",
        "runner_bootstrap.py",
    ):
        assert (executor_jobs / name).is_file()
        assert not (shared_jobs / name).exists()


def test_shared_mechanism_layers_do_not_depend_on_executor_implementation() -> (
    None
):
    shared_prefixes = (
        f"{_PACKAGE_NAME}.agent_bridge",
        f"{_PACKAGE_NAME}.jobs",
        f"{_PACKAGE_NAME}.tools",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(shared_prefixes)
        and target.startswith(f"{_PACKAGE_NAME}.executor")
    )

    assert actual == frozenset()


def test_control_shared_http_and_public_tools_do_not_read_executor_policy() -> (
    None
):
    violations: list[tuple[str, int, str]] = []
    roots = (
        _PACKAGE_ROOT / "control",
        _PACKAGE_ROOT / "http",
        _PACKAGE_ROOT / "tools" / "registry",
    )
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in _EXECUTOR_POLICY_FIELDS
                ):
                    violations.append(
                        (
                            str(path.relative_to(_PROJECT_ROOT)),
                            node.lineno,
                            node.attr,
                        )
                    )

    assert violations == []


def test_legacy_execution_namespaces_are_physically_absent() -> None:
    for name in ("ops", "remote", "remote_worker"):
        assert not (_PACKAGE_ROOT / name).exists()


def test_control_builds_without_executor_or_native_terminal_dependencies(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        f"""
        import importlib.abc
        from pathlib import Path

        class BlockExecutorImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "winpty" or fullname.startswith("workgate.executor"):
                    raise ModuleNotFoundError(f"blocked PR7 executor dependency: {{fullname}}")
                return None

        import sys
        sys.meta_path.insert(0, BlockExecutorImports())

        from workgate.config.settings import Settings
        from workgate.control.runtime import build_control_runtime

        root = Path({str(tmp_path)!r})
        runtime = build_control_runtime(
            Settings(
                workspace_root=root / "executor-workspace-must-not-be-needed",
                state_dir=root / "control-state",
                auth_mode="none",
                ui_enabled=False,
            )
        )
        assert runtime.state_store.layout.root == root / "control-state"
        assert not hasattr(runtime, "tool_session_store")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_source_dependency_graph_has_no_cycles() -> None:
    assert _dependency_cycles() == _ALLOWED_DEPENDENCY_CYCLES


def test_executor_runtime_never_imports_monolithic_settings_authority() -> None:
    """Executor code may use bootstrap helpers, but not ambient Settings authority."""
    forbidden = {"Settings", "get_settings", "configure_settings"}
    actual: list[tuple[str, str]] = []
    root = _PACKAGE_ROOT / "executor"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _resolve_import_from(_module_name(path), path, node)
            if target != f"{_PACKAGE_NAME}.config.settings":
                continue
            actual.extend(
                (str(path.relative_to(_PROJECT_ROOT)), alias.name)
                for alias in node.names
                if alias.name in forbidden
            )
    assert actual == []


def test_executor_production_code_has_no_tool_session_global_api() -> None:
    names = ("configure_tool_session_store", "get_tool_session_store")
    violations: list[tuple[str, str]] = []
    for path in sorted((_PACKAGE_ROOT / "executor").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in names:
            if name in source:
                violations.append((str(path.relative_to(_PROJECT_ROOT)), name))

    assert violations == []


def test_production_has_no_process_global_runtime_owner_bindings() -> None:
    forbidden = (
        "configure_state_store",
        "_STATE_STORE =",
        "configure_oauth_state",
        "_OAUTH_STATE =",
        "configure_managed_jobs_runtime",
        "_MANAGED_JOBS_RUNTIME =",
        "configure_human_ui_runtime",
        "_HUMAN_UI_RUNTIME =",
        "install_control_services",
        "install_runtime_services",
        "configure_conpty_registry",
        "_CONPTY_REGISTRY =",
        "configure_terminal_bridge_registry",
        "_TERMINAL_BRIDGE_REGISTRY =",
    )
    violations: list[tuple[str, str]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                violations.append((str(path.relative_to(_PROJECT_ROOT)), token))

    assert violations == []


def test_normal_runtime_policy_does_not_read_ambient_settings() -> None:
    """Only explicit config/bootstrap composition may access the ambient getter."""
    allowed = {
        Path("config/control.py"),
        Path("config/role_config.py"),
        Path("config/settings.py"),
        Path("control/http/app.py"),
        Path("control/mcp/app.py"),
    }
    settings_module = f"{_PACKAGE_NAME}.config.settings"
    config_module = f"{_PACKAGE_NAME}.config"
    violations: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT)
        if relative in allowed:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == settings_module:
                        violations.append(
                            f"{relative}:{node.lineno}: imports ambient settings module"
                        )
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_import_from(module, path, node)
                if target == settings_module and any(
                    alias.name == "get_settings" for alias in node.names
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: imports ambient get_settings"
                    )
                if target == config_module and any(
                    alias.name == "settings" for alias in node.names
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: imports ambient settings module"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_settings"
            ):
                violations.append(
                    f"{relative}:{node.lineno}: ambient get_settings()"
                )

    assert violations == []


def test_only_control_bootstrap_installs_ambient_settings() -> None:
    """Process-wide Settings installation is a control-bootstrap compatibility seam."""
    allowed = {
        Path("config/settings.py"),
        Path("control/cli.py"),
    }
    settings_module = f"{_PACKAGE_NAME}.config.settings"
    violations: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT)
        if relative in allowed:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_import_from(module, path, node)
                if target == settings_module and any(
                    alias.name == "configure_settings" for alias in node.names
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: imports configure_settings"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "configure_settings"
            ):
                violations.append(
                    f"{relative}:{node.lineno}: installs ambient Settings"
                )

    assert violations == []


def test_removed_control_executor_migration_surfaces_do_not_return() -> None:
    """Retired split-migration shims must not become normal architecture again."""
    forbidden = (
        "WORKGATE_REMOTE_WORKER_RUNTIME",
        "ControlSettingsView",
        "ui.dashboard.snapshot",
        "call_local_tool",
        "UnknownLocalToolError",
    )
    violations: list[tuple[str, str]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                violations.append((str(path.relative_to(_PROJECT_ROOT)), token))

    assert not (_PACKAGE_ROOT / "tools" / "local_handlers.py").exists()
    assert violations == []


def test_control_cli_imports_without_executor_dependencies() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockExecutorImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "winpty" or fullname.startswith("workgate.executor"):
                    raise ModuleNotFoundError(f"blocked executor dependency: {fullname}")
                return None

        sys.meta_path.insert(0, BlockExecutorImports())
        import workgate.control.cli
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_config_layer_does_not_depend_on_runtime_role_packages() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.executor.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.config")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()
