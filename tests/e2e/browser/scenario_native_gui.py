"""Real WebUI browser interactions with an isolated synthetic GUI executor response."""

import base64
import io

from PIL import Image
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_native_gui_manual(harness: BrowserHarness) -> None:
    """Verify manual mode, screenshot mapping, fresh tokens and safe return in Chromium."""
    image = Image.new("RGB", (80, 50), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode()
    session_id = "sess_gui_e2e"
    window_id = "window_gui_e2e"
    actions = []
    states = []

    page = harness.context.new_page()
    try:

        def supply(route):
            path = route.request.url.split("/api/ui/", 1)[1].split("?", 1)[0]
            if path == "tasks":
                route.fulfill(
                    json={
                        "ok": True,
                        "data": {
                            "tasks": [
                                {
                                    "task_id": "task_gui_e2e",
                                    "label": "Synthetic GUI test task",
                                    "status": "active",
                                    "objective": "Check GUI manual input",
                                    "created_at": 1,
                                    "updated_at": 1,
                                    "progress": {},
                                    "sessions": [
                                        {
                                            "session_id": session_id,
                                            "executor_id": harness.executor_id,
                                            "label": "Synthetic GUI session",
                                            "status": "active",
                                            "availability": "available",
                                            "workdir": "/demo",
                                            "created_at": 1,
                                            "updated_at": 1,
                                        }
                                    ],
                                }
                            ],
                            "unattached_sessions": [],
                        },
                    }
                )
            elif path == "gui/windows":
                route.fulfill(
                    json={
                        "ok": True,
                        "data": {
                            "windows": [
                                {"id": window_id, "title": "Synthetic window"}
                            ]
                        },
                    }
                )
            elif path == "gui/state":
                token = f"{len(states) + 1:032x}"
                states.append(token)
                route.fulfill(
                    json={
                        "ok": True,
                        "data": {
                            "state_id": token,
                            "state_ttl_s": 30,
                            "window": {
                                "id": window_id,
                                "bounds": {
                                    "x": 0,
                                    "y": 0,
                                    "width": 80,
                                    "height": 50,
                                },
                            },
                            "image": {
                                "mime_type": "image/png",
                                "data": encoded,
                            },
                        },
                    }
                )
            elif path == "gui/action":
                actions.append(route.request.post_data_json)
                route.fulfill(json={"ok": True, "data": {"performed": True}})
            elif path == "todos":
                route.fulfill(
                    json={"ok": True, "data": {"task": {}, "todos": []}}
                )
            elif path in {"audit", "audit/detail"}:
                route.fulfill(
                    json={"ok": True, "data": {"entries": [], "count": 0}}
                )
            else:
                route.continue_()

        page.route("**/api/ui/tasks", supply)
        page.route("**/api/ui/gui/**", supply)
        page.route("**/api/ui/todos?**", supply)
        page.route("**/api/ui/audit?**", supply)
        page.route("**/api/ui/audit/detail?**", supply)
        response = page.goto(
            f"{harness.base_url}/ui/?task_id=task_gui_e2e&session_id={session_id}#tasks",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.status == 200
        expect(page.locator("#session-gui-open")).to_be_enabled()
        page.locator("#session-gui-open").click()
        expect(page.locator("#session-gui-windows option")).to_have_count(2)
        page.locator("#session-gui-windows").select_option(window_id)
        page.locator("#session-gui-capture").click()
        expect(page.locator("#session-gui-image")).to_be_visible()
        page.locator("#session-gui-manual").click()
        expect(page.locator("#session-gui-manual-controls")).to_be_visible()
        shot = page.locator("#session-gui-image").bounding_box()
        assert shot is not None
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.mouse.click(
                shot["x"] + shot["width"] / 2, shot["y"] + shot["height"] / 2
            )
        expect(page.locator("#session-gui-image")).to_be_visible()
        assert len(actions) == 1, actions
        assert actions[0]["session_id"] == session_id
        assert actions[0]["window_id"] == window_id
        assert actions[0]["state_id"] == states[0]
        assert actions[0]["action"]["type"] == "click"
        assert abs(actions[0]["action"]["x"] - 40) <= 1
        assert abs(actions[0]["action"]["y"] - 25) <= 1
        assert len(states) >= 2
        page.locator("#session-gui-key").fill("Ctrl+S")
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-key-form button").click()
        expect(page.locator("#session-gui-image")).to_be_visible()
        assert len(actions) == 2
        assert actions[1]["action"] == {"type": "key", "keys": "Ctrl+S"}
        assert actions[1]["state_id"] != actions[0]["state_id"]
        page.locator("#session-gui-manual").click()
        expect(page.locator("#session-gui-manual-controls")).to_be_hidden()
        page.mouse.click(
            shot["x"] + shot["width"] / 2, shot["y"] + shot["height"] / 2
        )
        assert len(actions) == 2
        page.locator("#session-gui-back").click()
        expect(page.locator("#session-gui")).to_be_hidden()
        expect(page.locator(".session-audit")).to_be_visible()
    finally:
        page.close()
