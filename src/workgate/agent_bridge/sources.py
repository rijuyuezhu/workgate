"""Ordered Skill registry sources for local and session-scoped discovery."""

import os
from collections.abc import Mapping
from pathlib import Path

from .models import SkillRecord, SkillScanResult, SkillSource
from .skills import scan_agent_skills

PROJECT_SKILLS_DIRECTORY = ".agents/skills"
GLOBAL_SKILLS_DIRECTORY = "agents/skills"
MAX_SOURCE_WARNINGS = 100


def _global_config_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return an absolute XDG config home, ignoring invalid relative overrides."""
    active = os.environ if environ is None else environ
    configured = active.get("XDG_CONFIG_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    return (Path.home() / ".config").resolve()


def skill_sources(
    *,
    project_root: Path,
    managed_config_dir: Path,
    managed_directory: str,
    environ: Mapping[str, str] | None = None,
    include_project: bool = True,
) -> tuple[SkillSource, ...]:
    """Return deduplicated Skill roots in project, managed, then global order."""
    candidates = [
        SkillSource("managed", Path(managed_config_dir), managed_directory),
        SkillSource(
            "global", _global_config_home(environ), GLOBAL_SKILLS_DIRECTORY
        ),
    ]
    if include_project:
        candidates.insert(
            0,
            SkillSource(
                "project", Path(project_root), PROJECT_SKILLS_DIRECTORY
            ),
        )
    unique: list[SkillSource] = []
    seen_paths: set[Path] = set()
    for source in candidates:
        path = source.path
        if path in seen_paths:
            continue
        seen_paths.add(path)
        unique.append(source)
    return tuple(unique)


def _append_warning(warnings: list[str], message: str) -> None:
    """Append one warning while keeping aggregate multi-source output bounded."""
    if len(warnings) < MAX_SOURCE_WARNINGS:
        warnings.append(message)
    elif len(warnings) == MAX_SOURCE_WARNINGS:
        warnings.append("Additional Skill source warnings were omitted")


def scan_skill_sources(
    sources: tuple[SkillSource, ...],
    *,
    max_skills: int,
    max_related_files: int,
    max_scan_entries: int,
    max_path_bytes: int,
    max_entry_bytes: int,
) -> SkillScanResult:
    """Merge ordered Skill sources under one shared set of registry budgets."""
    skill_limit = max(0, int(max_skills))
    remaining_scan_entries = max(0, int(max_scan_entries))
    remaining_path_bytes = max(0, int(max_path_bytes))
    accepted: dict[str, SkillRecord] = {}
    warnings: list[str] = []
    scanned_entries = 0

    for source in sources:
        if len(accepted) >= skill_limit:
            _append_warning(
                warnings, f"Skill list truncated at {skill_limit} directories"
            )
            break
        if remaining_scan_entries == 0:
            _append_warning(
                warnings,
                f"Skill directory scan stopped after {max_scan_entries} entries",
            )
            break

        result = scan_agent_skills(
            source.config_dir,
            source.directory,
            source_name=source.name,
            source_path=str(source.path),
            max_skills=max(1, skill_limit - len(accepted)),
            max_related_files=max_related_files,
            max_scan_entries=remaining_scan_entries,
            max_path_bytes=remaining_path_bytes,
            max_entry_bytes=max_entry_bytes,
        )

        scanned_entries += result.scanned_entries
        remaining_scan_entries = max(
            0, remaining_scan_entries - result.scanned_entries
        )
        remaining_path_bytes = max(0, remaining_path_bytes - result.path_bytes)
        for warning in result.warnings:
            _append_warning(warnings, f"{source.name}: {warning}")

        for name, record in result.skills.items():
            if name in accepted:
                _append_warning(
                    warnings,
                    f"{source.name}: skipped duplicate Skill {name!r}; "
                    "a higher-priority source already provides it",
                )

                continue
            accepted[name] = record
            if len(accepted) >= skill_limit:
                break

    return SkillScanResult(
        skills=accepted,
        warnings=warnings,
        scanned_entries=scanned_entries,
        path_bytes=max(0, int(max_path_bytes) - remaining_path_bytes),
    )
