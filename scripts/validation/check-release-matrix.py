#!/usr/bin/env python3

import re
import tomllib
from pathlib import Path
from runpy import run_path

REPO = Path(__file__).resolve().parents[2]
_TUI_CONTRACT = run_path(str(REPO / "src" / "workgate" / "ui" / "contracts.py"))
POSIX_TUI_EXECUTABLE_NAME = str(_TUI_CONTRACT["POSIX_TUI_EXECUTABLE_NAME"])
WINDOWS_TUI_EXECUTABLE_NAME = str(_TUI_CONTRACT["WINDOWS_TUI_EXECUTABLE_NAME"])
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO / "pyproject.toml"
EXPECTED_BINARY_ARTIFACTS = {
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-aarch64",
    "windows-x86_64",
}
EXPECTED_PLATFORM_WHEELS = {
    "linux-x86_64": "linux_x86_64",
    "linux-aarch64": "linux_aarch64",
    "macos-x86_64": "macosx_10_15_x86_64",
    "macos-aarch64": "macosx_11_0_arm64",
    "windows-x86_64": "win_amd64",
}


def _section(text: str, start_pattern: str, end_pattern: str) -> str:
    start = re.search(start_pattern, text, flags=re.MULTILINE)
    if start is None:
        raise SystemExit(f"missing section matching {start_pattern!r}")
    end = re.search(end_pattern, text[start.end() :], flags=re.MULTILINE)
    if end is None:
        return text[start.start() :]
    return text[start.start() : start.end() + end.start()]


def _release_binary_artifacts(text: str) -> set[str]:
    build_binary = _section(
        text,
        r"^  build-binary:\n",
        r"^  [A-Za-z0-9_-]+:\n",
    )
    return set(
        re.findall(
            r"^          - artifact: ([A-Za-z0-9_-]+)$",
            build_binary,
            flags=re.MULTILINE,
        )
    )


def _platform_wheel_matrix(text: str) -> dict[str, str]:
    build_wheel = _section(
        text,
        r"^  (?:build-)?platform-wheel:\n",
        r"^  [A-Za-z0-9_-]+:\n",
    )
    pairs = re.findall(
        r"^          - artifact: ([A-Za-z0-9_-]+)\n"
        r"^            runner: [^\n]+\n"
        r"^            platform_tag: ([A-Za-z0-9_.-]+)$",
        build_wheel,
        flags=re.MULTILINE,
    )
    return dict(pairs)


def _report_mismatch(kind: str, expected: set[str], actual: set[str]) -> int:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if not missing and not extra:
        return 0
    print(f"Release {kind} mismatch.")
    print(f"missing: {missing}")
    print(f"extra: {extra}")
    return 1


def _report_mapping_mismatch(
    kind: str,
    expected: dict[str, str],
    actual: dict[str, str],
) -> int:
    if expected == actual:
        return 0
    print(f"Release {kind} mismatch.")
    print(f"expected: {expected}")
    print(f"actual: {actual}")
    return 1


def _require_fragments(kind: str, text: str, fragments: tuple[str, ...]) -> int:
    missing = [fragment for fragment in fragments if fragment not in text]
    if not missing:
        return 0
    print(f"Release {kind} is incomplete.")
    for fragment in missing:
        print(f"missing fragment: {fragment}")
    return 1


def _project_minimum_python_version() -> str:
    with PYPROJECT.open("rb") as stream:
        requires_python = str(
            tomllib.load(stream)["project"]["requires-python"]
        )
    match = re.fullmatch(r">=([0-9]+[.][0-9]+)", requires_python)
    if match is None:
        raise SystemExit(
            "check-release-matrix.py requires a simple >=X.Y project Python floor, "
            f"got {requires_python!r}"
        )
    return match.group(1)


def _platform_wheel_fragments() -> tuple[str, ...]:
    return (
        "oven-sh/setup-bun@v2",
        "bun-version: 1.3.14",
        "uv sync --locked --group dev",
        "bun install --frozen-lockfile",
        "scripts/release/build-platform-wheel.py",
        '--platform-tag "${{ matrix.platform_tag }}"',
        "scripts/release/smoke-platform-wheel.py",
        'PATH="$clean_path"',
        "test ! -e src/workgate/ui_runtime",
        "test ! -e src/workgate/.platform-wheel-build.lock",
    )


def main() -> int:
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    project_python = _project_minimum_python_version()
    release_python_status = _require_fragments(
        "Python baseline",
        _section(release_text, r"^env:\n", r"^jobs:\n"),
        (f'  PYTHON_VERSION: "{project_python}"',),
    )
    binary_status = _report_mismatch(
        "binary matrix",
        EXPECTED_BINARY_ARTIFACTS,
        _release_binary_artifacts(release_text),
    )
    release_wheel_status = _report_mapping_mismatch(
        "platform wheel matrix",
        EXPECTED_PLATFORM_WHEELS,
        _platform_wheel_matrix(release_text),
    )
    ci_wheel_status = _report_mapping_mismatch(
        "CI platform wheel matrix",
        EXPECTED_PLATFORM_WHEELS,
        _platform_wheel_matrix(ci_text),
    )
    binary_gui_runtime_status = _require_fragments(
        "standalone GUI runtime packaging",
        _section(
            release_text,
            r"^  build-binary:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        ("uv run --extra gui --with pyinstaller",),
    )
    opentui_release_status = _require_fragments(
        "OpenTUI sidecar packaging",
        _section(
            release_text,
            r"^  build-binary:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        (
            "oven-sh/setup-bun@v2",
            "bun install --frozen-lockfile",
            "bun run build:tui",
            f"ui-opentui/dist/{WINDOWS_TUI_EXECUTABLE_NAME}",
            f"ui-opentui/dist/{POSIX_TUI_EXECUTABLE_NAME}",
            "OPENTUI-NOTICES.txt",
            "BUN-1.3.14-LICENSE.md",
        ),
    )
    platform_wheel_release_status = _require_fragments(
        "platform wheel release",
        _section(
            release_text,
            r"^  build-platform-wheel:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        _platform_wheel_fragments()
        + (
            "name: python-wheel-${{ matrix.artifact }}",
            "path: dist/*.whl",
        ),
    )
    platform_wheel_ci_status = _require_fragments(
        "platform wheel CI",
        _section(
            ci_text,
            r"^  platform-wheel:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        _platform_wheel_fragments(),
    )
    package_smoke_status = _require_fragments(
        "CI source package provenance",
        _section(
            ci_text,
            r"^  package-smoke:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        (
            "scripts/validation/check-native-provenance.py",
            "scripts/generation/generate-tui-executable-contract.py",
            "tests/test_native_provenance.py",
            "src/workgate/ui/contracts.py",
            "ui-opentui/scripts/executable-contract.ts",
            'generate-tui-executable-contract.py" --check',
            "src/workgate/helpers/opentui.NOTICES",
            "src/workgate/helpers/bun-1.3.14.LICENSE.md",
        ),
    )
    universal_package_status = _require_fragments(
        "universal Python package validation",
        _section(
            release_text,
            r"^  build-python-package:\n",
            r"^  [A-Za-z0-9_-]+:\n",
        ),
        (
            "inspect_wheel(wheels[0], target=None)",
            "inspect_sdist(sdists[0])",
        ),
    )
    atomic_release_status = _require_fragments(
        "atomic platform wheel publication",
        _section(
            release_text,
            r"^  github-release:\n",
            r"\Z",
        ),
        (
            "- build-platform-wheel",
            "pattern: python-wheel-*",
            "merge-multiple: true",
            "files: dist/*",
        ),
    )
    if any(
        (
            release_python_status,
            binary_status,
            binary_gui_runtime_status,
            release_wheel_status,
            ci_wheel_status,
            opentui_release_status,
            platform_wheel_release_status,
            platform_wheel_ci_status,
            package_smoke_status,
            universal_package_status,
            atomic_release_status,
        )
    ):
        return 1
    print(
        "Release workflow matches the project Python baseline and covers expected "
        "universal and platform wheels, binary artifacts, and OpenTUI sidecars."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
