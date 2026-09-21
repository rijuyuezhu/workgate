# Tools reference

This page is rendered from [`generated/tools.json`](generated/tools.json), which is generated from the MCP app's live tool registry.

Tool descriptions come from the tool definitions/docstrings exposed by the MCP server. Input parameters are rendered from each tool's JSON schema.

Tool availability still depends on client capability, server settings, and executor availability. Regular connector-style clients may only expose `search` and `fetch`; ChatGPT Developer Mode and full MCP clients can expose the complete MCP surface. Machine-facing tools require a shared session bound to an eligible executor. Agent Bridge tools require the corresponding control-owned network or executor-owned local integration configuration.

Structured browser automation is provided by `browser_session`, `browser_snapshot`, and `browser_act`. These tools require the dedicated `browser:use` OAuth scope and an executor that advertises `browser.v1`. Workgate installs the Playwright Python runtime, but each executor must also have Playwright Chromium installed (for example, `playwright install chromium`). Browser state is ephemeral and owned by the Workgate session: executor restart discards it, and `session_end` closes owned browsers. The executor currently caps browser resources at 8 browser sessions and 16 live pages per browser session, and bounds action batches, snapshot text/elements, waits, screenshots, and retained error events. The structured surface intentionally does not provide persistent profiles/storage-state import or arbitrary Playwright script execution.

<div class="generated-reference" data-reference-json="../generated/tools.json">
Loading generated tools reference...
</div>
