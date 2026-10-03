# Development

This page is for contributors working on Workgate itself. User deployment
instructions live under [Getting started](getting-started/deployment.md); design
rationale lives in [Architecture](architecture.md).

## Set up

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync --group dev
uv run pre-commit install
```

## Run locally

For most manual testing, use the same two-process path users run:

```bash
uv run workgate standalone --default-workdir /absolute/path/to/project
```

When debugging a control transport in isolation:

```bash
WORKGATE_AUTH_MODE=none uv run workgate control --mode mcp --port 13444
```

Machine work still requires a paired executor; control does not acquire a
workspace merely because it is running from a checkout.

## Run checks

Start focused, then run the full gates before review:

```bash
uv run pytest tests/test_tool_surface.py -q
uv run pre-commit run --all-files
uv run pyright
uv run pytest -q
uv run --group docs mkdocs build --strict
```

The CI coverage ratchet is implemented by
`scripts/validation/check-coverage.py`. Run it locally after generating a
coverage JSON report when your change affects coverage policy. The script and
`scripts/README.md` are the canonical source for baseline-update mechanics.

## Regenerate derived files

Pre-commit checks generated configuration, tool reference, TUI contracts, and
native provenance. The common manual commands are:

```bash
uv run python scripts/generation/generate-config-examples.py
uv run python scripts/generation/export-tools-json.py \
  --wrapped \
  --output docs/reference/generated/tools.json \
  --instructions-output docs/reference/generated/server-instructions.json
```

If a hook reports generated drift, regenerate the owning artifact rather than
editing generated output by hand.

## Where code belongs

- `src/workgate/control/`: public/control authority and orchestration
- `src/workgate/executor/`: workspace, files, shells, jobs, PTYs, and machine-local authority
- `src/workgate/protocol/`: dependency-light control/executor contracts
- `src/workgate/tools/`: public tool declarations and shared routing contracts
- `src/workgate/ui/`: browser/OpenTUI application surfaces
- `src/workgate/agent_bridge/`: shared Agent Bridge contracts and mechanisms

Keep dependency direction visible: control must not import executor
implementations, and normal runtime policy should come from the resolved role
config/service graph rather than ambient settings or service locators.
`tests/test_architecture.py` contains the executable boundary checks.

## Documentation and release work

Keep user guides task-oriented. Exact settings/options belong in Reference;
implementation history belongs in Maintenance. See `scripts/README.md` for
repository automation and
[Native artifact provenance](maintenance/native-artifact-provenance.md) for
native release inputs.
