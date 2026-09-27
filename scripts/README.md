# Repository automation

The `scripts` tree contains repository, CI, release, and development automation.
Scripts are grouped by the lifecycle they own rather than by implementation
language. Moving a script must update workflow, pre-commit, source-package,
provenance, test, and documentation references in the same change.

## Root files

`README.md` is the only tracked file that lives directly under `scripts/`.
Repository automation belongs in `generation`, `release`, `testing`, or
`validation`. User-facing deployment artifacts belong under `deploy/`, where
their lifecycle can remain separate from checkout-only automation.

## `release`

| File | Responsibility | Why it belongs here |
| --- | --- | --- |
| `release/build-platform-wheel.py` | Thin installed-project entrypoint for building one verified platform wheel with the embedded native OpenTUI payload. | It invokes release-owned platform-wheel assembly and is consumed only by CI/release packaging workflows. |
| `release/smoke-platform-wheel.py` | Verifies an installed platform wheel without Bun or a sidecar, including private payload materialization, stable executable resolution, and native `--version` execution. | It is an artifact acceptance test coupled to platform-wheel publishing rather than a general repository validator. |
| `release/pyinstaller-entry.py` | Minimal PyInstaller startup shim that forwards command-line arguments to the packaged application entrypoint. | It exists only as an input to standalone release artifact construction and is not a runtime library or development command. |

## `testing`

| File | Responsibility | Why it belongs here |
| --- | --- | --- |
| `testing/run-browser-e2e.py` | Runs the real Chromium Human UI scenario with the browser marker and exports an absolute artifact directory for traces, screenshots, video, browser logs, and process logs. | It is a checkout-only test orchestrator consumed by CI, not a package validator, release builder, generated-artifact producer, or user-facing launcher. |

## `validation`

| File | Responsibility | Why it belongs here |
| --- | --- | --- |
| `validation/check-coverage.py` | Reads Coverage.py JSON reports, writes cross-environment baselines, and enforces aggregate plus risk-weighted per-module coverage floors. It must run with the standard library alone before the project is installed. | Coverage is a repository quality gate rather than runtime or release behavior. Keeping the checker beside its policy data makes the zero-install validation boundary explicit. |
| `validation/coverage-baseline.json` | Stores aggregate minima, per-source observations, and the serialized risk classification consumed by the coverage checker. | The file is policy input for the checker, not generated package data or application configuration, so both validation artifacts move together. |
| `validation/check-doc-reference-assets.py` | Validates that generated-reference widgets in a built MkDocs site resolve to local JSON assets without escaping the site root. | It is a post-build documentation validator and has no runtime or generation ownership. |
| `validation/check-no-future-annotations.py` | Rejects postponed-annotation future imports in first-party Python while ignoring generated dependency environments. | Workgate targets Python 3.14, so this keeps the repository annotation convention explicit and prevents generators or new modules from silently reintroducing the obsolete import. |
| `validation/check-native-provenance.py` | Verifies locked OpenTUI source hashes, license notices, and required release/source-package wiring. | Native provenance is a repository validation policy; the checker must remain zero-install and separate from the builders whose inputs it audits. |
| `validation/check-release-matrix.py` | Audits the project/release Python baseline, CI/release matrices, package-smoke fragments, platform-wheel tags, and native sidecar coverage. | It validates repository workflow completeness before installation and therefore belongs beside other zero-install validation gates rather than release builders. |

## `generation`

| File | Responsibility | Why it belongs here |
| --- | --- | --- |
| `generation/generate-config-examples.py` | Renders the environment, YAML, and generated configuration-reference examples from the canonical settings surface, or checks them for drift. | It deterministically generates reviewed repository artifacts and requires the installed project model, rather than validating an already-built package. |
| `generation/export-tools-json.py` | Builds the MCP registry and exports stable tool plus server-instruction reference JSON for documentation and inspection. | It is a deterministic reference-data generator whose outputs are checked into the documentation tree. |
| `generation/generate-tui-executable-contract.py` | Mirrors the dependency-leaf Python TUI executable-name contract into the Bun build input and checks that mirror for drift. | It owns a generated cross-language source artifact; release and provenance validators consume its output but do not own generation. |
