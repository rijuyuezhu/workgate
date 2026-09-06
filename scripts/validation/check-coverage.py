#!/usr/bin/env python3
"""Enforce aggregate and risk-weighted branch-coverage baselines.

Aggregate package coverage remains a strict ratchet. Security and runtime
modules keep near-zero per-file drift, while ordinary existing modules tolerate
small denominator movement. New core modules require meaningful coverage and
thin adapter/schema modules use a lower threshold so relocation and wiring work
do not encourage low-value tests.
"""

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
DEFAULT_REPORT = Path("coverage.json")
DEFAULT_BASELINE = Path("scripts/validation/coverage-baseline.json")
SOURCE_PREFIX = "src/workgate/"
TOTAL_DRIFT_PERCENT = 0.05
CRITICAL_EXISTING_DRIFT_PERCENT = 0.05
DEFAULT_EXISTING_DRIFT_PERCENT = 1.0
NEW_CORE_MIN_PERCENT = 80.0
NEW_ADAPTER_MIN_PERCENT = 60.0
CRITICAL_PREFIXES = (
    f"{SOURCE_PREFIX}audit/",
    f"{SOURCE_PREFIX}oauth/",
    f"{SOURCE_PREFIX}remote_worker/",
    f"{SOURCE_PREFIX}terminal/",
)
CRITICAL_FILES = frozenset(
    {
        f"{SOURCE_PREFIX}agent_bridge/auth.py",
        f"{SOURCE_PREFIX}agent_bridge/auth_store.py",
        f"{SOURCE_PREFIX}agent_bridge/redaction.py",
        f"{SOURCE_PREFIX}config/settings.py",
        f"{SOURCE_PREFIX}ops/files.py",
        f"{SOURCE_PREFIX}jobs/runtime.py",
        f"{SOURCE_PREFIX}ops/patch/envelope.py",
        f"{SOURCE_PREFIX}ops/secret_scan.py",
        f"{SOURCE_PREFIX}ops/shell.py",
        f"{SOURCE_PREFIX}ops/transfer.py",
        f"{SOURCE_PREFIX}remote/manager.py",
        f"{SOURCE_PREFIX}remote/transfer_gateway.py",
        f"{SOURCE_PREFIX}tool_session/store.py",
        f"{SOURCE_PREFIX}ui/security.py",
        f"{SOURCE_PREFIX}utils/path_locks.py",
        f"{SOURCE_PREFIX}utils/private_files.py",
    }
)
ADAPTER_PREFIXES = (
    f"{SOURCE_PREFIX}control/",
    f"{SOURCE_PREFIX}http/",
    f"{SOURCE_PREFIX}schemas/",
    f"{SOURCE_PREFIX}tools/registry/",
    f"{SOURCE_PREFIX}ui/http/",
)
ADAPTER_FILES = frozenset(
    {
        f"{SOURCE_PREFIX}remote/http.py",
        f"{SOURCE_PREFIX}remote/responses.py",
        f"{SOURCE_PREFIX}remote/service.py",
        f"{SOURCE_PREFIX}tools/local_handlers.py",
    }
)


class CoverageDataError(ValueError):
    """Raised when a coverage report or baseline has an invalid shape."""


def coverage_policy() -> dict[str, Any]:
    """Return the serialized risk-weighted policy embedded in each baseline."""
    return {
        "adapter_files": sorted(ADAPTER_FILES),
        "adapter_prefixes": list(ADAPTER_PREFIXES),
        "critical_existing_drift_percent": CRITICAL_EXISTING_DRIFT_PERCENT,
        "critical_files": sorted(CRITICAL_FILES),
        "critical_prefixes": list(CRITICAL_PREFIXES),
        "default_existing_drift_percent": DEFAULT_EXISTING_DRIFT_PERCENT,
        "new_adapter_min_percent": NEW_ADAPTER_MIN_PERCENT,
        "new_core_min_percent": NEW_CORE_MIN_PERCENT,
        "total_drift_percent": TOTAL_DRIFT_PERCENT,
    }


def _matches_path_policy(
    path: str,
    *,
    prefixes: tuple[str, ...],
    files: frozenset[str],
) -> bool:
    return path in files or path.startswith(prefixes)


def _is_critical(path: str) -> bool:
    return _matches_path_policy(
        path, prefixes=CRITICAL_PREFIXES, files=CRITICAL_FILES
    )


def _is_adapter(path: str) -> bool:
    return _matches_path_policy(
        path, prefixes=ADAPTER_PREFIXES, files=ADAPTER_FILES
    )


def _new_file_minimum(path: str) -> tuple[str, float]:
    if not _is_critical(path) and _is_adapter(path):
        return "adapter", NEW_ADAPTER_MIN_PERCENT
    return "core", NEW_CORE_MIN_PERCENT


def _existing_drift_allowance(path: str) -> float:
    if _is_critical(path):
        return CRITICAL_EXISTING_DRIFT_PERCENT
    return DEFAULT_EXISTING_DRIFT_PERCENT


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageDataError(f"{context} must be a JSON object")
    return value


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CoverageDataError(f"{context} must be a number")
    return float(value)


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoverageDataError(f"{context} must be an integer")
    if value < 0:
        raise CoverageDataError(f"{context} must not be negative")
    return value


def _source_path(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CoverageDataError(f"{context} must be a non-empty string")
    if not value.startswith(SOURCE_PREFIX):
        raise CoverageDataError(
            f"unexpected source path in {context}: {value}; "
            f"expected prefix {SOURCE_PREFIX}"
        )
    return value


def load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoverageDataError(f"missing coverage file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CoverageDataError(f"invalid JSON in {path}: {exc}") from exc
    return _mapping(value, str(path))


def snapshot_report(report: Mapping[str, Any]) -> dict[str, Any]:
    totals = _mapping(report.get("totals"), "coverage report totals")
    total_percent = _number(
        totals.get("percent_covered"),
        "coverage report total percent_covered",
    )
    report_files = _mapping(report.get("files"), "coverage report files")
    files: dict[str, dict[str, int | float]] = {}
    for raw_path, raw_entry in sorted(report_files.items()):
        path = _source_path(raw_path, "coverage report")
        entry = _mapping(raw_entry, f"coverage report entry {path}")
        summary = _mapping(entry.get("summary"), f"coverage summary {path}")
        files[path] = {
            "percent_covered": round(
                _number(
                    summary.get("percent_covered"),
                    f"coverage percent for {path}",
                ),
                6,
            ),
            "num_statements": _integer(
                summary.get("num_statements"),
                f"statement count for {path}",
            ),
            "num_branches": _integer(
                summary.get("num_branches", 0),
                f"branch count for {path}",
            ),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "total_percent_covered": round(total_percent, 6),
        "policy": coverage_policy(),
        "files": files,
    }


def merge_snapshots(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        raise CoverageDataError("at least one coverage report is required")

    file_maps = [
        _mapping(snapshot.get("files"), f"coverage snapshot {index} files")
        for index, snapshot in enumerate(snapshots, start=1)
    ]
    expected_paths = set(file_maps[0])
    for index, files in enumerate(file_maps[1:], start=2):
        paths = set(files)
        if paths != expected_paths:
            missing = sorted(expected_paths - paths)
            extra = sorted(paths - expected_paths)
            raise CoverageDataError(
                f"coverage snapshot {index} has a different source set; "
                f"missing={missing[:5]!r}, extra={extra[:5]!r}"
            )

    merged_files: dict[str, dict[str, int | float]] = {}
    for path in sorted(expected_paths):
        entries = [
            _mapping(files[path], f"coverage snapshot entry {path}")
            for files in file_maps
        ]
        statement_counts = {
            _integer(
                entry.get("num_statements"),
                f"statement count for {path}",
            )
            for entry in entries
        }
        branch_counts = {
            _integer(
                entry.get("num_branches"),
                f"branch count for {path}",
            )
            for entry in entries
        }
        if len(statement_counts) != 1 or len(branch_counts) != 1:
            raise CoverageDataError(
                f"coverage reports describe different source revisions for {path}"
            )
        merged_files[path] = {
            "percent_covered": min(
                _number(
                    entry.get("percent_covered"),
                    f"coverage percent for {path}",
                )
                for entry in entries
            ),
            "num_statements": statement_counts.pop(),
            "num_branches": branch_counts.pop(),
        }

    total_percent = min(
        _number(
            snapshot.get("total_percent_covered"),
            "coverage snapshot total_percent_covered",
        )
        for snapshot in snapshots
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "report_count": len(snapshots),
        "total_percent_covered": round(total_percent, 6),
        "policy": coverage_policy(),
        "files": merged_files,
    }


def write_baseline(report_paths: list[Path], baseline_path: Path) -> None:
    snapshot = merge_snapshots(
        [snapshot_report(load_json(path)) for path in report_paths]
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "wrote coverage baseline: "
        f"{baseline_path} ({len(snapshot['files'])} files, "
        f"{snapshot['total_percent_covered']:.2f}% total, "
        f"{snapshot['report_count']} reports)"
    )


def evaluate(
    report: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> tuple[list[str], str]:
    schema_version = baseline.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise CoverageDataError(
            "unsupported coverage baseline schema: "
            f"expected {SCHEMA_VERSION}, got {schema_version!r}"
        )
    baseline_policy = _mapping(baseline.get("policy"), "baseline policy")
    expected_policy = coverage_policy()
    if dict(baseline_policy) != expected_policy:
        raise CoverageDataError(
            "baseline coverage policy does not match the checker"
        )

    current = snapshot_report(report)
    current_total = float(current["total_percent_covered"])
    baseline_total = _number(
        baseline.get("total_percent_covered"),
        "baseline total_percent_covered",
    )
    baseline_files = _mapping(baseline.get("files"), "baseline files")
    for path in baseline_files:
        _source_path(path, "coverage baseline")
    current_files = _mapping(current["files"], "current coverage files")

    failures: list[str] = []
    if current_total + TOTAL_DRIFT_PERCENT < baseline_total:
        failures.append(
            "total branch coverage regressed: "
            f"{current_total:.2f}% < {baseline_total:.2f}%"
        )

    tracked = 0
    new_files = 0
    for path, raw_current in current_files.items():
        current_entry = _mapping(raw_current, f"current coverage entry {path}")
        current_percent = _number(
            current_entry.get("percent_covered"),
            f"current coverage percent for {path}",
        )
        statements = _integer(
            current_entry.get("num_statements"),
            f"current statement count for {path}",
        )
        raw_baseline = baseline_files.get(path)
        if raw_baseline is None:
            if statements == 0:
                continue
            new_files += 1
            category, minimum = _new_file_minimum(path)
            if current_percent + TOTAL_DRIFT_PERCENT < minimum:
                failures.append(
                    f"new {category} module {path} is below {minimum:.2f}%: "
                    f"{current_percent:.2f}%"
                )
            continue
        tracked += 1
        baseline_entry = _mapping(
            raw_baseline,
            f"baseline coverage entry {path}",
        )
        baseline_percent = _number(
            baseline_entry.get("percent_covered"),
            f"baseline coverage percent for {path}",
        )
        allowance = _existing_drift_allowance(path)
        if current_percent + allowance < baseline_percent:
            failures.append(
                f"module coverage regressed for {path}: "
                f"{current_percent:.2f}% < {baseline_percent:.2f}% "
                f"beyond {allowance:.2f} percentage-point allowance"
            )

    summary = (
        f"total {current_total:.2f}% (baseline {baseline_total:.2f}%); "
        f"{tracked} tracked files; {new_files} new files"
    )
    return failures, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        help=(
            "coverage JSON report; repeat when writing a cross-environment "
            "baseline (defaults to coverage.json)"
        ),
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "replace the baseline with per-module and aggregate minima from "
            "all supplied reports"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_paths = args.report or [DEFAULT_REPORT]
    try:
        if args.write_baseline:
            write_baseline(report_paths, args.baseline)
            return 0
        if len(report_paths) != 1:
            raise CoverageDataError(
                "coverage checking accepts exactly one --report"
            )
        failures, summary = evaluate(
            load_json(report_paths[0]),
            load_json(args.baseline),
        )
    except CoverageDataError as exc:
        print(f"coverage gate error: {exc}", file=sys.stderr)
        return 2

    if failures:
        print("coverage ratchet failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print(summary, file=sys.stderr)
        return 1
    print(f"coverage ratchet passed: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
