# Tools reference

This page is rendered from [`generated/tools.json`](generated/tools.json), which is generated from the MCP app's live tool registry.

Tool descriptions come from the tool definitions/docstrings exposed by the MCP server. Input parameters are rendered from each tool's JSON schema.

Tool availability still depends on client capability, server settings, and executor availability. Regular connector-style clients may only expose `search` and `fetch`; ChatGPT Developer Mode and full MCP clients can expose the complete MCP surface. Machine-facing tools require an execution session bound to an eligible executor. Agent Bridge tools require the corresponding control-owned network or executor-owned local integration configuration.

Structured browser automation is provided by `browser_session`, `browser_snapshot`, and `browser_act`. These tools require the dedicated `browser:use` OAuth scope and an executor that advertises `browser.v1` with Playwright Chromium installed. `browser_act` accepts fresh snapshot refs or a CSS selector matching exactly one element. Live browser processes/contexts remain owned by the explicit Workgate session. `browser_session` may explicitly reuse one named executor-local Chromium profile or import/export a Playwright storage-state JSON file within that session's workdir; persisted auth state may outlive the live session, while arbitrary Playwright scripts remain unexposed.

Native desktop automation is provided by `gui_list`, `gui_state`, and `gui_action`. These tools require the dedicated `gui:use` OAuth scope and an executor that advertises `gui.v1`. Observations are short-lived and session-owned; actions consume a fresh observation and do not retarget another session or executor.

<div class="generated-reference" data-reference-json="../generated/tools.json">
Loading generated tools reference...
</div>
