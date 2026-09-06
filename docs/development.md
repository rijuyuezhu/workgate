# Development

This page is for contributors working on `workgate` itself. It focuses on how to run, debug, test, and regenerate docs without relying on stale code walkthroughs.

## Local environment

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync --group dev
uv run pre-commit install
```

## Run the server during development

Run MCP-over-HTTP locally without OAuth:

```bash
WORKGATE_AUTH_MODE=none uv run workgate server --mode mcp --port 13444
```

Run the REST debug API locally without OAuth:

```bash
WORKGATE_AUTH_MODE=none uv run workgate server --mode http --port 13444
```

Use an explicit workspace when needed:

```bash
WORKGATE_WORKSPACE_ROOT=/path/to/project \
WORKGATE_AUTH_MODE=none \
uv run workgate server --mode http --port 13444
```

Use full-control mode only for disposable test workspaces:

```bash
WORKGATE_AUTH_MODE=none \
uv run workgate server --mode http --port 13444 --allow-full-control true
```

## Smoke-test with curl

Health check:

```bash
curl -i http://127.0.0.1:13444/healthz
```

Inspect environment through the REST debug API:

```bash
curl -s -X POST http://127.0.0.1:13444/tools/session_start \
  -H 'content-type: application/json' \
  -d '{"workdir":"."}' | jq
```

Read a file through the REST debug API:

```bash
curl -s -X POST http://127.0.0.1:13444/tools/read \
  -H 'content-type: application/json' \
  -d '{"path":"README.md:1-40"}' | jq
```

List files:

```bash
curl -s -X POST http://127.0.0.1:13444/tools/list_files \
  -H 'content-type: application/json' \
  -d '{"path":".","max_entries":20}' | jq
```

Export the MCP tool surface:

```bash
uv run python scripts/generation/export-tools-json.py --wrapped > /tmp/workgate-tools.json
jq '.count, [.tools[].name]' /tmp/workgate-tools.json
```

## Watch logs and audit output

For a foreground dev process, read the terminal output first.

For a user systemd service:

```bash
journalctl --user -u workgate.service -f -n 200
```

Audit log:

```bash
tail -F "${WORKGATE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/workgate}/audit_log/audit.jsonl" | jq -C --unbuffered .
```

Audit state can contain prompts, tool inputs, tool outputs, file contents, bounded JSONL previews, and recoverable sanitized payload objects. Credential-like values are redacted on a best-effort basis before storage, but both JSONL and the payload directory must still be treated as sensitive.

## Documentation ownership

Keep documentation at the level owned by each section:

- **Home**, **Getting started**, and **Guides** are for users. Explain the task,
  prerequisites, commands, expected result, limitations, and recovery steps.
- **Reference** is generated or contract-oriented. Put exact CLI options, settings,
  tool arguments, defaults, and server instructions there instead of copying them
  into several guides.
- **Architecture**, **Development**, **Security**, and **Maintenance** are for
  contributors and operators who need design rationale, module ownership, release
  constraints, or migration history.

Do not turn a user guide into a code walkthrough. Protocol frames, lock ordering,
byte budgets, state-machine transitions, generated-artifact details, and private
storage formats belong in source docstrings, tests, architecture notes, or focused
maintenance records. Link to those sources when the distinction matters to a
contributor.

When behavior changes:

1. update the public contract or source docstring closest to the implementation;
2. update or add tests that demonstrate the behavior;
3. regenerate Reference pages when their source changes;
4. change a user guide only when the user's workflow, requirement, limitation, or
   recovery action changed.

Useful implementation entry points are:

- Human UI HTTP/WebSocket adapters: `src/workgate/ui/http/`
- Browser assets: `src/workgate/ui/static/`
- OpenTUI client: `ui-opentui/`
- Terminal asset build: `ui-terminal/`
- Remote controller and worker domains: `src/workgate/remote/` and
  `src/workgate/remote_worker/`
- Agent Bridge: `src/workgate/agent_bridge/`
- Audit storage and queries: `src/workgate/audit/` (principally `core.py`
  and `payloads.py`) and the corresponding tool/UI adapters

Use nearby tests as the canonical executable description. For native UI packaging
and release artifacts, also see
[Native artifact provenance](maintenance/native-artifact-provenance.md).

## Architecture boundaries

The package is layered around transport-neutral tool behavior. Keep dependency
direction explicit when adding features or moving code:

- `config`, `schemas`, and small `utils` modules provide dependency-leaf
  settings, contracts, serialization, and filesystem primitives.
- `agent_bridge`, `remote`, `remote_worker`, and `tool_session` own their domain
  state and protocols. Legacy source-only remote-worker modules must remain usable
  without the control process's full dependency stack.
- `ops` implements transport-neutral use cases. It may use domain services and
  schemas, but it must not depend on HTTP, MCP, or Human UI delivery adapters.
- `tools` owns public tool contracts, discovery, metadata, and registration.
- `http` owns delivery-adapter-neutral Starlette/ASGI infrastructure shared by
  REST and MCP-over-HTTP. It must not import `workgate.control` or Human UI
  implementations.
- `control/mcp` owns MCP composition, MCP-specific middleware, and stdio or
  MCP-over-HTTP runtime selection.
- `control/http` owns REST tool routes, REST error and timeout policy, and the
  runnable FastAPI application.
- `ui/http` owns Human UI HTTP and WebSocket adapters. The REST control adapter may
  import only its explicit route-composition contract; lower layers must not
  import control delivery adapters or UI delivery adapters. The obsolete `server`
  package has been removed and must not be restored.
- `main` is the thin argparse composition root; `control/cli.py` selects the
  configured control delivery mode.

The canonical OpenTUI executable names live in `ui/contracts.py`. After changing
that contract, run `uv run python scripts/generation/generate-tui-executable-contract.py` to
refresh the Bun-side mirror; pre-commit rejects stale generated output.

Human UI route handlers should validate transport input and delegate reusable
behavior to `ops` or domain services. MCP-facing annotations and OAuth security
metadata are tool presentation contracts, not transport implementation details.
Avoid generic service locators: extract small dependency-leaf contracts and
explicit facades instead. See [Architecture and module ownership](architecture.md)
for the file-by-file ownership map and rejected placement alternatives.

`tests/test_architecture.py` enforces current dependency cycles, explicit
executor/UI composition imports, and dependency-leaf package boundaries. Its
allowlists are visible technical debt, not extension points: when a cycle or reversed import is
removed, shrink the allowlist in the same change. New entries require an explicit
architecture review.

## Run checks before committing

```bash
uv run pre-commit run --all-files
uv run pyright
uv run pytest -q
uv run mkdocs build --strict
```

For focused work, run the relevant subset first:

```bash
uv run pytest tests/test_tool_surface.py -q
uv run pytest tests/test_config_surface.py -q
uv run pytest tests/test_agent_bridge_tools.py -q
```

## Check branch coverage

The Ubuntu CI job runs the complete Python suite under branch coverage. The
risk-weighted policy is serialized in `scripts/validation/coverage-baseline.json` and
validated by the checker:

- aggregate package branch coverage may not fall below the committed baseline;
- credential, private-file, audit, OAuth, remote-worker lifecycle, terminal,
  destructive file/process, and transfer modules retain a near-zero per-file
  drift allowance of 0.05 percentage points;
- ordinary existing modules may move by at most one percentage point, avoiding
  low-value tests for harmless branch-denominator changes;
- new core modules must start at 80%, while new executor, HTTP, registry, schema,
  and other thin adapter modules must start at 60%; and
- zero-statement package markers do not create a coverage obligation.

When moving a file, move its baseline key in the same commit so an existing
module is not misclassified as new. The committed baseline records the lowest
value observed for each module across local Linux and GitHub Ubuntu reports, so
environment-only branches cannot make either supported run flaky. Run the same
gate locally with:

```bash
find . -maxdepth 1 -name '.coverage*' -delete
uv run python -m coverage run -m pytest -q
uv run python -m coverage combine
uv run python -m coverage json
uv run python -m coverage xml
uv run python -m coverage report
uv run python scripts/validation/check-coverage.py
```

Only update the baseline after reviewing why coverage changed. Download the
`python-coverage` artifact from the Ubuntu job for the same source revision,
then merge it with the local report. The checker rejects different source sets
or statement/branch counts, and stores the per-module and aggregate minima:

```bash
gh run download <run-id> -n python-coverage -D /tmp/python-coverage
uv run python scripts/validation/check-coverage.py \
  --write-baseline \
  --report coverage.json \
  --report /tmp/python-coverage/coverage.json
```

Coverage work should target behavior, trust boundaries, failure modes, or a
meaningful migration regression. Do not add tests solely to recover a small
percentage caused by relocation or denominator movement inside the documented
allowance. Regressions beyond the allowance should gain useful tests or be
explicitly justified by changing the policy and baseline together in review.

## Regenerate generated reference data

Configuration examples and reference JSON are generated from the settings registry:

```bash
uv run python scripts/generation/generate-config-examples.py
uv run python scripts/generation/generate-config-examples.py --check
```

Tool and instruction reference JSON are generated from the live MCP app:

```bash
uv run python scripts/generation/export-tools-json.py \
  --wrapped \
  --output docs/reference/generated/tools.json \
  --instructions-output docs/reference/generated/server-instructions.json

uv run python scripts/generation/export-tools-json.py \
  --wrapped \
  --output docs/reference/generated/tools.json \
  --instructions-output docs/reference/generated/server-instructions.json \
  --check
```

The pre-commit hooks run these generators when related source files change.

## Documentation development

The documentation site uses Material for MkDocs.

```bash
uv sync --group docs
uv run mkdocs serve
uv run mkdocs build --strict
```

## Release checks

Before cutting a release:

```bash
uv run pre-commit run --all-files
uv run pyright
uv run pytest -q
uv run mkdocs build --strict
uv run python scripts/validation/check-release-matrix.py
uv build --out-dir dist
```

The ordinary `uv build` output must contain exactly one payload-free universal
`py3-none-any` wheel and one source-only sdist. Native OpenTUI wheels are built
only on matching runners with the pinned Bun release, for example:

```bash
uv run python scripts/release/build-platform-wheel.py \
  --platform-tag linux_x86_64 \
  --output-dir dist
```

The builder accepts only the repository's explicit target tags, validates the
native host and executable magic, creates deterministic gzip metadata, rewrites
and verifies `WHEEL` plus `RECORD`, and removes staging files on success or
failure. Release CI installs every platform wheel in an isolated environment
with Bun and sidecars absent, then runs `scripts/release/smoke-platform-wheel.py` before
all Python package artifacts are published together. Native source, license,
platform, rebuild, and checksum ownership is recorded in
[Native artifact provenance](maintenance/native-artifact-provenance.md). Run
`uv run python scripts/validation/check-native-provenance.py` whenever those inputs change.

Also test the binary artifact and at least one real MCP connection path before publishing.

