"""On-demand Executor-local diagnostic snapshot without external probes."""

from typing import Any

from ..agent_bridge.diagnostics import manifest_diagnostics
from ..agent_bridge.sources import scan_skill_sources, skill_sources
from ..agent_bridge.state import load_agent_manifest
from ..config.executor import ExecutorConfig
from .dashboard import dashboard_snapshot


def executor_diagnostics(config: ExecutorConfig) -> dict[str, Any]:
    """Read bounded filesystem source metadata and cached platform metrics."""
    bridge = manifest_diagnostics(config.agent_config_dir, owner="executor")
    sources: list[dict[str, Any]] = []
    selected: list[dict[str, str]] = []
    warnings = 0
    duplicates = 0
    if bridge["status"] == "loaded":
        try:
            manifest = load_agent_manifest(
                config.agent_config_dir, read_only=True
            )
            candidates = skill_sources(
                project_root=config.agent_config_dir,
                managed_config_dir=config.agent_config_dir,
                managed_directory=manifest.data.skills.directory,
                include_project=False,
            )
            sources = [
                {"source": source.name, "available": source.path.is_dir()}
                for source in candidates
            ]
            if manifest.data.skills.enabled:
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
                warnings = min(len(result.warnings), 100)
                duplicates = sum(
                    "skipped duplicate Skill" in note
                    for note in result.warnings
                )
            skills_enabled = manifest.data.skills.enabled
        except OSError, ValueError:
            skills_enabled = False
            warnings += 1
    else:
        skills_enabled = False
    # Dashboard already owns resource sampling. A single on-demand call avoids
    # duplicate polling, and only its non-sensitive scalar metrics are exposed.
    try:
        system = dashboard_snapshot(config).get("system", {})
        health = {
            key: system.get(key)
            for key in (
                "cpu_percent",
                "memory_percent",
                "disk_percent",
                "memory_total_bytes",
                "disk_total_bytes",
            )
            if type(system.get(key)) in (float, int)
        }
    except OSError, ValueError:
        health = {}
    return {
        "agent": {
            "status": "not_reported",
            "note": "No independent Agent runtime status is currently reported",
        },
        "bridge": bridge,
        "skills": {
            "enabled": skills_enabled,
            "sources": sources,
            "selected": selected,
            "warnings": warnings,
            "duplicates": duplicates,
            "project_scope": "session_specific",
            "control_scope": "pending_354",
        },
        "health": health,
    }
