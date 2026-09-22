import json
from pathlib import Path
from urllib.parse import urlencode

import httpx
from playwright.sync_api import expect

from tests.browser.harness import PROJECT_ROOT, BrowserHarness

_LIVE_WORKSPACE_HTML = (
    PROJECT_ROOT
    / "src"
    / "workgate"
    / "control"
    / "mcp"
    / "live_workspace.html"
)


def _snapshot(*, status: str = "active", revision: int = 7) -> dict:
    actions = {
        "active": ["block", "cancel", "next_instruction"],
        "blocked": ["resume", "cancel", "next_instruction"],
        "completed": ["resume"],
        "cancelled": [],
    }.get(status, [])
    return {
        "version": 1,
        "session": {
            "session_id": "sess_browser_live",
            "label": "Browser handoff",
            "executor_id": "exec_browser",
            "executor_name": "browser-loopback",
            "workdir": "/workspace/demo",
            "status": "active",
            "availability": "available",
            "created_at": 1.0,
            "updated_at": 2.0,
            "last_active_at": 2.0,
        },
        "task": {
            "revision": revision,
            "objective": "Review the Live Workspace safely",
            "status": status,
            "progress": {
                "summary": "Browser E2E is exercising the real App asset.",
                "findings": ["The standard App bridge is connected."],
                "blockers": ["A synthetic browser blocker."],
                "next_action": "Verify controls.",
            },
            "plan": {
                "steps": [
                    {
                        "id": "browser",
                        "content": "Exercise the MCP App",
                        "status": "in_progress",
                        "priority": "high",
                    }
                ]
            },
        },
        "compatibility_plan": [],
        "task_controls_available": bool(actions),
        "task_control_actions": actions,
        "task_controls_message": None,
        "jobs": [
            {
                "job_id": "job_browser",
                "kind": "shell",
                "name": "pytest",
                "status": "running",
                "cwd": "/workspace/demo",
                "created_at": 1.0,
                "updated_at": 2.0,
                "completed_at": None,
                "attempts": 1,
            }
        ],
        "jobs_message": None,
        "shells": [
            {
                "shell_id": "shell_browser",
                "name": "dev",
                "cwd": "/workspace/demo",
                "backend": "tmux",
            }
        ],
        "shells_message": None,
        "activity": [
            {
                "id": "audit_browser",
                "ts": 2.0,
                "event": "tool_call",
                "tool": "bash",
                "operation": "execute",
                "ok": True,
                "duration_ms": 9.0,
                # Deliberately hostile extra fields: the App must never render them.
                "args": {"command": "printf browser-super-secret"},
                "result": {"stdout": "browser-super-secret"},
            }
        ],
        "links": {
            "sessions": "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser#sessions",
            "files": "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser&workdir=%2Fworkspace%2Fdemo#files",
            "terminals": "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser&shell_id=shell_browser#terminals",
            "audit": "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser#audit",
        },
    }


def _mock_host_html(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    initial = _snapshot()
    blocked = _snapshot(status="blocked", revision=8)
    refreshed = _snapshot(status="blocked", revision=8)
    mock = f"""
<script>
window.__liveCalls = [];
window.setInterval = () => 0;
const __initial = {json.dumps(initial)};
const __blocked = {json.dumps(blocked)};
const __refreshed = {json.dumps(refreshed)};
function __hostPost(message) {{
  window.postMessage(message, "*");
}}
window.addEventListener("message", (event) => {{
  const message = event.data;
  if (!message || message.jsonrpc !== "2.0" || !message.method) return;
  window.__liveCalls.push({{ name: message.method, args: message.params || {{}} }});
  if (message.method === "ui/initialize") {{
    __hostPost({{
      jsonrpc: "2.0",
      id: message.id,
      result: {{
        protocolVersion: "2026-01-26",
        hostInfo: {{ name: "browser-e2e", version: "1" }},
        hostCapabilities: {{}},
        hostContext: {{}},
      }},
    }});
    return;
  }}
  if (message.method === "ui/notifications/initialized") {{
    __hostPost({{
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: {{ content: [], structuredContent: __initial }},
    }});
    return;
  }}
  if (message.method === "tools/call") {{
    const name = message.params.name;
    const args = message.params.arguments || {{}};
    let result;
    if (name === "workspace_task_control") result = {{ content: [], structuredContent: __blocked }};
    else if (name === "workspace_snapshot") result = {{ content: [], structuredContent: __refreshed }};
    else if (name === "workspace_end") result = {{ content: [], structuredContent: {{ session_id: args.session_id, ended: true }} }};
    else result = {{ content: [{{ type: "text", text: "unexpected tool " + name }}], isError: true }};
    __hostPost({{ jsonrpc: "2.0", id: message.id, result }});
    return;
  }}
  if (message.method === "ui/open-link") {{
    __hostPost({{ jsonrpc: "2.0", id: message.id, result: {{}} }});
  }}
}});
</script>
"""
    marker = "<script>\n(() => {"
    assert marker in html
    return html.replace(marker, mock + marker, 1)


def run_live_workspace(harness: BrowserHarness) -> None:
    # The App is an authenticated MCP surface; it must not accidentally become
    # a public route just because the resource itself is HTML.
    response = httpx.post(
        f"{harness.base_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "browser-e2e", "version": "1"},
            },
        },
        timeout=5,
    )
    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()

    context = harness.browser.new_context(
        viewport={"width": 390, "height": 844}
    )
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text)
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.set_content(
            _mock_host_html(_LIVE_WORKSPACE_HTML), wait_until="load"
        )

        expect(page.locator("#title")).to_have_text("Browser handoff")
        expect(page.locator("#availability")).to_have_text("available")
        expect(page.locator("#objective")).to_contain_text(
            "Review the Live Workspace"
        )
        expect(page.locator("#progress")).to_contain_text("Browser E2E")
        expect(page.locator("#next-action")).to_contain_text("Verify controls")
        expect(page.locator("#findings")).to_contain_text("standard App bridge")
        expect(page.locator("#blockers")).to_contain_text(
            "synthetic browser blocker"
        )
        expect(page.locator("#jobs")).to_contain_text("pytest")
        expect(page.locator("#shells")).to_contain_text("dev")
        expect(page.locator("#activity")).to_contain_text("tool_call")
        expect(page.get_by_role("link", name="Sessions")).to_have_attribute(
            "href",
            "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser#sessions",
        )
        expect(page.get_by_role("link", name="Files")).to_have_attribute(
            "href",
            "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser&workdir=%2Fworkspace%2Fdemo#files",
        )
        expect(page.get_by_role("link", name="Terminals")).to_have_attribute(
            "href",
            "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser&shell_id=shell_browser#terminals",
        )
        expect(page.get_by_role("link", name="Audit")).to_have_attribute(
            "href",
            "https://workgate.example/ui?session_id=sess_browser_live&executor_id=exec_browser#audit",
        )
        assert "browser-super-secret" not in page.locator("body").inner_text()
        calls = page.evaluate("window.__liveCalls")
        assert calls[0]["name"] == "ui/initialize"
        assert calls[0]["args"]["protocolVersion"] == "2026-01-26"
        assert any(
            call.get("name") == "ui/notifications/initialized" for call in calls
        )

        # The compact App should actually collapse to one column on a phone.
        cards = page.locator(".grid > .card")
        first = cards.nth(0).bounding_box()
        second = cards.nth(1).bounding_box()
        assert first is not None and second is not None
        assert abs(first["x"] - second["x"]) < 2
        assert second["y"] > first["y"]

        page.get_by_role("button", name="Block / pause").click()
        expect(page.locator("#task-status")).to_have_text("task: blocked")
        calls = page.evaluate("window.__liveCalls")
        task_calls = [
            call
            for call in calls
            if call.get("name") == "tools/call"
            and call.get("args", {}).get("name") == "workspace_task_control"
        ]
        assert task_calls == [
            {
                "name": "tools/call",
                "args": {
                    "name": "workspace_task_control",
                    "arguments": {
                        "session_id": "sess_browser_live",
                        "action": "block",
                        "expected_revision": 7,
                    },
                },
            }
        ]

        # Wrong confirmation is rejected locally and never reaches the tool.
        page.locator("#end-confirm").fill("sess_wrong")
        page.get_by_role("button", name="End session").click()
        calls = page.evaluate("window.__liveCalls")
        assert not any(
            call.get("name") == "tools/call"
            and call.get("args", {}).get("name") == "workspace_end"
            for call in calls
        )
        expect(page.locator("#status")).to_contain_text(
            "does not exactly match"
        )

        # Exact confirmation sends only the current canonical session identity.
        page.locator("#end-confirm").fill("sess_browser_live")
        page.get_by_role("button", name="End session").click()
        expect(page.locator("#status")).to_have_text("Execution session ended.")
        calls = page.evaluate("window.__liveCalls")
        end_calls = [
            call
            for call in calls
            if call.get("name") == "tools/call"
            and call.get("args", {}).get("name") == "workspace_end"
        ]
        assert end_calls == [
            {
                "name": "tools/call",
                "args": {
                    "name": "workspace_end",
                    "arguments": {
                        "session_id": "sess_browser_live",
                        "confirm_session_id": "sess_browser_live",
                    },
                },
            }
        ]

        assert not console_errors
        assert not page_errors

        # Preserve Live Workspace identity through a first-time Human UI OAuth
        # round trip. The callback must restore the original deep link rather
        # than dropping back to an unscoped overview.
        oauth_query = urlencode(
            {
                "session_id": "sess_oauth_deep_link",
                "executor_id": harness.executor_id,
                "workdir": ".",
            }
        )
        return_url = f"{harness.base_url}/ui?{oauth_query}#files"
        response = page.goto(return_url, wait_until="domcontentloaded")
        assert response is not None and response.status == 200
        expect(page.locator("#auth-panel")).to_be_visible()
        page.locator("#oauth-login").click()
        page.wait_for_url("**/oauth/authorize?*")
        page.locator('input[name="pin"]').fill(harness.admin_pin)
        page.get_by_role("button", name="Approve").click()
        page.wait_for_url("**/ui/callback?*")
        expect(page.locator("#connection-state")).to_have_text("Connected")
        expect(page).to_have_url(return_url)
        expect(page.locator("#page-title")).to_have_text("Files")
    finally:
        context.close()
