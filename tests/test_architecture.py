import ast
from collections import defaultdict
from pathlib import Path

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


def test_http_infrastructure_does_not_depend_on_control_or_ui() -> None:
    forbidden_prefixes = (
        f"{_PACKAGE_NAME}.control.",
        f"{_PACKAGE_NAME}.server.",
        f"{_PACKAGE_NAME}.ui.",
    )
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.http")
        and target.startswith(forbidden_prefixes)
    )

    assert actual == frozenset()


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


def test_ui_http_does_not_depend_on_control() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer.startswith(f"{_PACKAGE_NAME}.ui.http")
        and target.startswith(f"{_PACKAGE_NAME}.control.")
    )

    assert actual == frozenset()


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
        if importer.startswith(f"{_PACKAGE_NAME}.terminal")
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
        if importer == f"{_PACKAGE_NAME}.ops.patch.envelope"
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
        if importer.startswith(f"{_PACKAGE_NAME}.terminal")
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


def test_remote_worker_process_dependencies_are_one_way() -> None:
    layers = {
        f"{_PACKAGE_NAME}.remote_worker.state": 0,
        f"{_PACKAGE_NAME}.remote_worker.lifecycle": 1,
        f"{_PACKAGE_NAME}.remote_worker.runtime": 2,
    }
    violations = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer in layers
        and target in layers
        and layers[target] > layers[importer]
    )

    assert violations == frozenset()


def test_remote_worker_state_contract_is_a_dependency_leaf() -> None:
    actual = frozenset(
        (importer, target)
        for importer, target in _local_imports()
        if importer == f"{_PACKAGE_NAME}.remote_worker.state"
    )

    assert actual == frozenset()


def test_source_dependency_graph_has_no_cycles() -> None:
    assert _dependency_cycles() == _ALLOWED_DEPENDENCY_CYCLES
