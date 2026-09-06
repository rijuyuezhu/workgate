import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "validation" / "check-coverage.py"


def test_coverage_paths_canonicalize_managed_runtime_sources() -> None:
    config = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    paths = config["tool"]["coverage"]["paths"]["source"]

    assert paths[0] == "src/workgate"
    assert "workgate" in paths
    assert "*/runtime/workgate" in paths
    assert "*/runtimes/*/workgate" in paths


def _entry(percent: float, statements: int = 10) -> dict[str, Any]:
    return {
        "summary": {
            "percent_covered": percent,
            "num_statements": statements,
            "num_branches": 4,
        }
    }


def _report(total: float, files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "totals": {"percent_covered": total},
        "files": files,
    }


def _run(
    tmp_path: Path,
    report: dict[str, Any] | list[dict[str, Any]],
    baseline: dict[str, Any] | None = None,
    *,
    write_baseline: bool = False,
) -> subprocess.CompletedProcess[str]:
    reports = report if isinstance(report, list) else [report]
    report_paths: list[Path] = []
    for index, value in enumerate(reports, start=1):
        report_path = tmp_path / f"coverage-{index}.json"
        report_path.write_text(json.dumps(value), encoding="utf-8")
        report_paths.append(report_path)
    baseline_path = tmp_path / "baseline.json"
    if baseline is not None:
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    command = [
        sys.executable,
        str(CHECKER),
        "--baseline",
        str(baseline_path),
    ]
    for report_path in report_paths:
        command.extend(["--report", str(report_path)])
    if write_baseline:
        command.append("--write-baseline")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _policy() -> dict[str, Any]:
    return {
        "adapter_files": [
            "src/workgate/remote/http.py",
            "src/workgate/remote/responses.py",
            "src/workgate/remote/service.py",
            "src/workgate/tools/local_handlers.py",
        ],
        "adapter_prefixes": [
            "src/workgate/control/",
            "src/workgate/http/",
            "src/workgate/schemas/",
            "src/workgate/tools/registry/",
            "src/workgate/ui/http/",
        ],
        "critical_existing_drift_percent": 0.05,
        "critical_files": sorted(
            [
                "src/workgate/agent_bridge/auth.py",
                "src/workgate/agent_bridge/auth_store.py",
                "src/workgate/agent_bridge/redaction.py",
                "src/workgate/config/settings.py",
                "src/workgate/ops/files.py",
                "src/workgate/jobs/runtime.py",
                "src/workgate/ops/patch/envelope.py",
                "src/workgate/ops/secret_scan.py",
                "src/workgate/ops/shell.py",
                "src/workgate/ops/transfer.py",
                "src/workgate/remote/manager.py",
                "src/workgate/remote/transfer_gateway.py",
                "src/workgate/tool_session/store.py",
                "src/workgate/ui/security.py",
                "src/workgate/utils/path_locks.py",
                "src/workgate/utils/private_files.py",
            ]
        ),
        "critical_prefixes": [
            "src/workgate/audit/",
            "src/workgate/oauth/",
            "src/workgate/remote_worker/",
            "src/workgate/terminal/",
        ],
        "default_existing_drift_percent": 1.0,
        "new_adapter_min_percent": 60.0,
        "new_core_min_percent": 80.0,
        "total_drift_percent": 0.05,
    }


def _baseline(
    total: float,
    files: dict[str, dict[str, int | float]],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "total_percent_covered": total,
        "policy": _policy(),
        "files": files,
    }


def test_write_baseline_and_accept_equal_report(tmp_path: Path) -> None:
    report = _report(
        91.25,
        {
            "src/workgate/a.py": _entry(92.5),
            "src/workgate/empty.py": _entry(100.0, statements=0),
        },
    )
    written = _run(tmp_path, report, write_baseline=True)
    assert written.returncode == 0, written.stderr
    baseline = json.loads((tmp_path / "baseline.json").read_text())
    assert baseline["report_count"] == 1
    assert baseline["total_percent_covered"] == 91.25
    assert baseline["policy"] == _policy()
    assert list(baseline["files"]) == sorted(baseline["files"])

    checked = _run(tmp_path, report, baseline)
    assert checked.returncode == 0, checked.stderr
    assert "coverage ratchet passed" in checked.stdout


def test_write_baseline_uses_cross_environment_minima(tmp_path: Path) -> None:
    first = _report(
        93.0,
        {
            "src/workgate/a.py": _entry(95.0),
            "src/workgate/b.py": _entry(70.0),
        },
    )
    second = _report(
        90.0,
        {
            "src/workgate/a.py": _entry(80.0),
            "src/workgate/b.py": _entry(90.0),
        },
    )
    written = _run(tmp_path, [first, second], write_baseline=True)
    assert written.returncode == 0, written.stderr
    baseline = json.loads((tmp_path / "baseline.json").read_text())
    assert baseline["report_count"] == 2
    assert baseline["total_percent_covered"] == 90.0
    assert baseline["files"]["src/workgate/a.py"]["percent_covered"] == 80.0
    assert baseline["files"]["src/workgate/b.py"]["percent_covered"] == 70.0
    assert _run(tmp_path, first, baseline).returncode == 0
    assert _run(tmp_path, second, baseline).returncode == 0


def test_write_baseline_rejects_mismatched_source_sets(tmp_path: Path) -> None:
    first = _report(90.0, {"src/workgate/a.py": _entry(90.0)})
    second = _report(90.0, {"src/workgate/b.py": _entry(90.0)})
    written = _run(tmp_path, [first, second], write_baseline=True)
    assert written.returncode == 2
    assert "different source set" in written.stderr


def test_rejects_total_coverage_regression(tmp_path: Path) -> None:
    report = _report(80.0, {"src/workgate/a.py": _entry(90.0)})
    baseline = _baseline(
        81.0,
        {
            "src/workgate/a.py": {
                "percent_covered": 90.0,
                "num_statements": 10,
                "num_branches": 4,
            }
        },
    )
    checked = _run(tmp_path, report, baseline)
    assert checked.returncode == 1
    assert "total branch coverage regressed" in checked.stderr


def test_allows_one_point_existing_module_drift(tmp_path: Path) -> None:
    report = _report(95.0, {"src/workgate/a.py": _entry(91.0)})
    baseline = _baseline(
        95.0,
        {
            "src/workgate/a.py": {
                "percent_covered": 92.0,
                "num_statements": 10,
                "num_branches": 4,
            }
        },
    )

    checked = _run(tmp_path, report, baseline)

    assert checked.returncode == 0, checked.stderr


def test_rejects_existing_module_regression_beyond_allowance(
    tmp_path: Path,
) -> None:
    report = _report(95.0, {"src/workgate/a.py": _entry(90.9)})
    baseline = _baseline(
        95.0,
        {
            "src/workgate/a.py": {
                "percent_covered": 92.0,
                "num_statements": 10,
                "num_branches": 4,
            }
        },
    )

    checked = _run(tmp_path, report, baseline)

    assert checked.returncode == 1
    assert "module coverage regressed" in checked.stderr
    assert "1.00 percentage-point allowance" in checked.stderr


def test_critical_modules_keep_near_zero_drift(tmp_path: Path) -> None:
    path = "src/workgate/oauth/core/security.py"
    report = _report(95.0, {path: _entry(91.9)})
    baseline = _baseline(
        95.0,
        {
            path: {
                "percent_covered": 92.0,
                "num_statements": 10,
                "num_branches": 4,
            }
        },
    )

    checked = _run(tmp_path, report, baseline)

    assert checked.returncode == 1
    assert "0.05 percentage-point allowance" in checked.stderr


def test_requires_eighty_percent_for_new_core_modules(tmp_path: Path) -> None:
    tracked = {
        "src/workgate/tracked.py": {
            "percent_covered": 95.0,
            "num_statements": 10,
            "num_branches": 4,
        }
    }
    failing = _report(
        95.0,
        {
            "src/workgate/tracked.py": _entry(95.0),
            "src/workgate/new.py": _entry(79.0),
        },
    )
    passing = _report(
        95.0,
        {
            "src/workgate/tracked.py": _entry(95.0),
            "src/workgate/new.py": _entry(80.0),
        },
    )

    checked = _run(tmp_path, failing, _baseline(95.0, tracked))

    assert checked.returncode == 1
    assert "new core module" in checked.stderr
    assert "80.00%" in checked.stderr
    assert _run(tmp_path, passing, _baseline(95.0, tracked)).returncode == 0


def test_requires_sixty_percent_for_new_adapter_modules(tmp_path: Path) -> None:
    tracked = {
        "src/workgate/tracked.py": {
            "percent_covered": 95.0,
            "num_statements": 10,
            "num_branches": 4,
        }
    }
    adapter = "src/workgate/ui/http/new_adapter.py"
    failing = _report(
        95.0,
        {
            "src/workgate/tracked.py": _entry(95.0),
            adapter: _entry(59.0),
        },
    )
    passing = _report(
        95.0,
        {
            "src/workgate/tracked.py": _entry(95.0),
            adapter: _entry(60.0),
        },
    )

    checked = _run(tmp_path, failing, _baseline(95.0, tracked))

    assert checked.returncode == 1
    assert "new adapter module" in checked.stderr
    assert "60.00%" in checked.stderr
    assert _run(tmp_path, passing, _baseline(95.0, tracked)).returncode == 0


def test_ignores_deleted_and_empty_new_modules(tmp_path: Path) -> None:
    report = _report(
        95.0,
        {"src/workgate/empty.py": _entry(0.0, statements=0)},
    )
    baseline = _baseline(
        95.0,
        {
            "src/workgate/deleted.py": {
                "percent_covered": 100.0,
                "num_statements": 5,
                "num_branches": 0,
            }
        },
    )
    checked = _run(tmp_path, report, baseline)
    assert checked.returncode == 0, checked.stderr
    assert "0 new files" in checked.stdout


def test_rejects_policy_changes_in_baseline(tmp_path: Path) -> None:
    report = _report(95.0, {"src/workgate/a.py": _entry(95.0)})
    baseline = _baseline(95.0, {})
    baseline["policy"]["default_existing_drift_percent"] = 2.0

    checked = _run(tmp_path, report, baseline)

    assert checked.returncode == 2
    assert "policy does not match" in checked.stderr


def test_rejects_unmapped_subprocess_source_paths(tmp_path: Path) -> None:
    report = _report(
        95.0,
        {"/tmp/runtime/workgate/a.py": _entry(95.0)},
    )
    checked = _run(tmp_path, report, _baseline(95.0, {}))
    assert checked.returncode == 2
    assert "unexpected source path" in checked.stderr
