"""Scan workspace text files for credential-like strings."""

import asyncio
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec

from ..schemas.result_models.secret_scan import SecretFinding, SecretScanOutput
from .files import (
    FilesConfig,
    _display_file,
    _read_file_local,
    _resolve_file_path,
)
from .tool_session.resolver import SessionResolver
from .tool_session.store import ToolSessionStore

SECRET_PATTERNS = {
    "github_token": r"gh[pousr]_[A-Za-z0-9_]{36,}",
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "private_key": r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
    "generic_assignment": r"(?i)(token|secret|password|passwd|api_key|apikey)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
}


GENERIC_ASSIGNMENT_VALUE_RE = re.compile(r"[:=]\s*['\"]([^'\"]+)['\"]")
PLACEHOLDER_SECRET_RE = re.compile(
    r"(?i)(\$\{[^}]+\}|change[-_ ]?me|replace[-_ ]?me|placeholder|"
    r"example|dummy|fake|fixture|test[-_ ]?only|local[-_ ]?only|"
    r"ci[-_ ][a-z0-9_.-]*fixture|dev[-_ ]?secret)"
)


def _is_placeholder_secret_match(secret_type: str, matched_text: str) -> bool:
    """Return whether a generic assignment match is an obvious fixture."""
    if secret_type != "generic_assignment":
        return False
    value_match = GENERIC_ASSIGNMENT_VALUE_RE.search(matched_text)
    value = value_match.group(1) if value_match else matched_text
    stripped = value.strip()
    if not stripped:
        return True
    if "${" in stripped or "$<" in stripped:
        return True
    if PLACEHOLDER_SECRET_RE.search(stripped):
        return True
    repeated_chars = {char for char in stripped.lower() if char.isalnum()}
    return bool(repeated_chars) and len(repeated_chars) <= 2


def _load_gitignore_specs(base: Path) -> list[tuple[Path, PathSpec]]:
    specs: list[tuple[Path, PathSpec]] = []
    seen: set[Path] = set()
    for ignore_file in [base / ".gitignore", *base.rglob(".gitignore")]:
        if ignore_file in seen:
            continue
        seen.add(ignore_file)
        if not ignore_file.is_file() or ".git" in ignore_file.parts:
            continue
        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = ignore_file.read_text(errors="ignore").splitlines()
        specs.append(
            (ignore_file.parent, PathSpec.from_lines("gitignore", lines))
        )
    return specs


def _is_gitignored(path: Path, specs: list[tuple[Path, PathSpec]]) -> bool:
    for ignore_root, spec in specs:
        try:
            relative = path.relative_to(ignore_root)
        except ValueError:
            continue
        if spec.match_file(relative.as_posix()):
            return True
    return False


def _matches_glob(path: Path, base: Path, glob: str | None) -> bool:
    if glob is None:
        return True
    try:
        relative = path.relative_to(base)
    except ValueError:
        relative = path
    return relative.match(glob) or path.match(glob)


def _candidate_paths_from_rg(
    base: Path, glob: str | None, rg_bin: str
) -> list[Path] | None:
    args = [rg_bin, "--files", "--hidden", "--glob", "!.git/**"]
    if glob:
        args.extend(["--glob", glob])
    try:
        result = subprocess.run(
            args,
            cwd=base,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode not in {0, 1}:
        return None
    return [base / line for line in result.stdout.splitlines() if line]


def _iter_candidate_paths(
    base: Path, glob: str | None, rg_bin: str
) -> Iterable[Path]:
    rg_paths = _candidate_paths_from_rg(base, glob, rg_bin)
    if rg_paths is not None:
        yield from (path for path in rg_paths if path.is_file())
        return

    specs = _load_gitignore_specs(base)
    for path in base.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if _is_gitignored(path, specs):
            continue
        if not _matches_glob(path, base, glob):
            continue
        yield path


@dataclass(frozen=True)
class SecretScanService:
    """Scan files inside one explicit executor session from injected policy."""

    files: FilesConfig
    store: ToolSessionStore
    rg_bin: str
    max_findings: int

    def _scan_sync(
        self,
        session_id: str,
        cwd: str = ".",
        glob: str | None = None,
        max_results: int = 200,
    ) -> SecretScanOutput:
        binding = SessionResolver(self.store).resolve_active_binding(session_id)
        base = _resolve_file_path(self.files, binding, cwd, must_exist=True)
        limit = max(1, min(max_results, self.max_findings))
        findings: list[SecretFinding] = []
        truncated_files = 0
        for path in _iter_candidate_paths(base, glob, self.rg_bin):
            try:
                data = _read_file_local(
                    self.files, self.store, binding, str(path)
                )
            except Exception:
                continue
            if data.truncated:
                truncated_files += 1
            text = data.content
            for name, pattern in SECRET_PATTERNS.items():
                for match in re.finditer(pattern, text):
                    if _is_placeholder_secret_match(name, match.group(0)):
                        continue
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        SecretFinding(
                            type=name,
                            path=_display_file(self.files, path),
                            line=line,
                        )
                    )
                    if len(findings) >= limit:
                        return SecretScanOutput(
                            findings=findings,
                            truncated=True,
                            truncated_files=truncated_files,
                        )
        return SecretScanOutput(
            findings=findings,
            truncated=False,
            truncated_files=truncated_files,
        )

    async def scan(
        self,
        session_id: str,
        cwd: str = ".",
        glob: str | None = None,
        max_results: int = 200,
    ) -> SecretScanOutput:
        return await asyncio.to_thread(
            self._scan_sync, session_id, cwd, glob, max_results
        )
