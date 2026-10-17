"""On-demand Executor-local diagnostic snapshot without external probes."""

from typing import Any

from ..agent_bridge.diagnostics import manifest_diagnostics
from ..agent_bridge.sources import scan_skill_sources, skill_sources
from ..agent_bridge.state import load_agent_manifest
from ..config.executor import ExecutorConfig


def executor_diagnostics(config: ExecutorConfig) -> dict[str, Any]:
    """Read bounded local MCP and Skill source metadata without probing peers."""
    bridge = manifest_diagnostics(config.agent_config_dir, owner="executor")
    selected: list[dict[str, str]] = []
    overridden: list[dict[str, str]] = []
    warnings = 0
    duplicates = 0
    skills_enabled = False
    scan_failed = False
    if bridge["status"] in {"loaded", "missing_config"}:
        try:
            manifest = load_agent_manifest(
                config.agent_config_dir, read_only=True
            )
            skills_enabled = manifest.data.skills.enabled
            if skills_enabled:
                candidates = skill_sources(
                    project_root=config.agent_config_dir,
                    managed_config_dir=config.agent_config_dir,
                    managed_directory=manifest.data.skills.directory,
                    include_project=False,
                )
                result = scan_skill_sources(
                    candidates,
                    max_skills=min(32, config.max_skills),
                    max_related_files=min(4, config.max_skill_related_files),
                    max_scan_entries=min(128, config.max_skill_scan_entries),
                    max_path_bytes=min(65_536, config.max_skill_path_bytes),
                    max_entry_bytes=min(4_096, config.max_file_read_bytes),
                )
                selected = [
                    {"name": skill.name[:80], "source": skill.source}
                    for skill in result.skills.values()
                ][:32]
                overridden = [
                    {"name": name[:80], "source": source}
                    for name, source in result.overridden[:32]
                ]
                duplicates = len(result.overridden)
                warnings = min(len(result.warnings), 100)
        except OSError, ValueError:
            scan_failed = True
            warnings += 1
    return {
        "bridge": bridge,
        "skills": {
            "enabled": skills_enabled,
            "unavailable": scan_failed,
            "selected": selected,
            "overridden": overridden,
            "warnings": warnings,
            "duplicates": duplicates,
        },
    }
