# Tools reference

This page is rendered from [`generated/tools.json`](generated/tools.json), which is generated from the MCP app's live tool registry.

Tool descriptions come from the tool definitions/docstrings exposed by the MCP server. Input parameters are rendered from each tool's JSON schema.

Tool availability still depends on client capability, server settings, and executor availability. Regular connector-style clients may only expose `search` and `fetch`; ChatGPT Developer Mode and full MCP clients can expose the complete MCP surface. Machine-facing tools require a shared session bound to an eligible executor. Agent Bridge tools require the corresponding control-owned network or executor-owned local integration configuration.

<div class="generated-reference" data-reference-json="../generated/tools.json">
Loading generated tools reference...
</div>
