## Summary

-

## Validation

- [ ] `uv run pre-commit run --all-files`
- [ ] `uv run pyright`
- [ ] `uv run pytest -q`
- [ ] Branch-coverage ratchet passes if Python source changed
- [ ] `uv run mkdocs build --strict` if docs changed

## Safety checklist

- [ ] No credentials or private workspace data are committed.
- [ ] New or changed MCP tools and their debug HTTP wrapper are documented and tested.
- [ ] Host-control, remote-worker, file-link, or credential behavior is described.
- [ ] Backwards compatibility impact is noted.
