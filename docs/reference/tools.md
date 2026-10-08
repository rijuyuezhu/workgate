# Tools reference

This page is rendered from [`generated/tools.json`](generated/tools.json), which is generated from the MCP app's live tool registry.

Tool descriptions come from the tool definitions/docstrings exposed by the MCP server. Input parameters are rendered from each tool's JSON schema.

Tool availability still depends on client capability, server settings, and executor availability. Regular connector-style clients may only expose `search` and `fetch`; ChatGPT Developer Mode and full MCP clients can expose the complete MCP surface. Machine-facing tools require an execution session bound to an eligible executor. Agent Bridge tools require the corresponding control-owned network or executor-owned local integration configuration.

Structured browser automation is provided by `browser_session`, `browser_snapshot`, and `browser_act`. These tools require the dedicated `browser:use` OAuth scope and an executor that advertises `browser.v1` with Playwright Chromium installed. Snapshots include the latest 30 response metadata rows (page, method, status and bounded URL), without request/response bodies or headers. Snapshots capture a managed PNG by default (`screenshot=false` skips capture), auto-pruned and removed with their browser session; an explicit `screenshot_path` is user-owned and never overwritten. `browser_act` accepts fresh snapshot refs or a CSS selector matching exactly one element. Live browser processes/contexts remain owned by the explicit Workgate session. `browser_session` may explicitly reuse one named executor-local Chromium profile or import/export a Playwright storage-state JSON file within that session's workdir; persisted auth state may outlive the live session, while arbitrary scripts run only through the separate `browser_run_script` tool with shell execution authorization.

`browser_run_script` provides a separate Python Playwright subprocess escape hatch using the Workgate runtime Python on the session's executor/workdir (configured `python_bin` for frozen executables), with both browser and shell execution authority and bounded shell output/timeout/cleanup. It does not share the live context of `browser_session`. Script input, output, and errors use normal Audit recording, without browser-script-specific payload redaction.

Native desktop automation is provided by `gui_list`, `gui_state`, and `gui_action`. These tools require the dedicated `gui:use` OAuth scope and an executor that advertises `gui.v1`. Observations are short-lived and session-owned; actions consume a fresh observation and do not retarget another session or executor.

<div class="generated-reference" data-reference-json="../generated/tools.json">
Loading generated tools reference...
</div>
