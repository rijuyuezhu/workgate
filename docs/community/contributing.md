# Contributing

Thanks for improving `workgate`. Keep changes focused, tested, and aligned with the current Python 3.14 architecture.

## Development checks

```bash
uv sync --locked --group dev --group docs
uv run pre-commit run --all-files
uv run pyright
uv run pytest -q
uv run mkdocs build --strict
```

## Architecture guidelines

- Keep machine-facing implementation under `src/workgate/executor/`; keep public/control authority and orchestration under `src/workgate/control/`.
- Keep shared contracts and mechanisms dependency-light. `tools`, `jobs`, `agent_bridge`, and `composition` must not import executor implementation modules.
- Expose tools through `src/workgate/tools/registry/` and the declarative registry unless a dynamic surface is required.
- Keep MCP and REST server assembly under `src/workgate/control/mcp/` and `src/workgate/control/http/`.
- Use Python 3.14 syntax directly; do not add compatibility imports such as `from __future__ import annotations`.
