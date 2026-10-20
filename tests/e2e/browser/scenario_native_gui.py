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
    pointer_enabled = [True]
    reject_input = [False]
    observation_ttl = [30]

    page = harness.context.new_page()

    def hold_next_gui_action(page):
        # Keep one GUI fetch in flight to exercise actual Chromium defaults.
        page.evaluate(
            """() => {
                    const original = window.fetch.bind(window);
                    window.fetch = (url, opts) => {
                        if (String(url).endsWith('/gui/action')) {
                            return new Promise(resolve => {
                                window.releaseGuiAction = () => {
                                    window.fetch = original;
                                    resolve(original(url, opts));
                                };
                            });
                        }
                        return original(url, opts);
                    };
                }"""
        )

    try:

        def supply(route):
            path = route.request.url.split("/api/ui/", 1)[1].split("?", 1)[0]
            if path == "tasks/detail":
                route.fulfill(
                    json={
                        "ok": True,
                        "data": {
                            "task": {
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
                            },
                            "session": None,
                        },
                    }
                )
            elif path == "tasks":
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
                            "state_ttl_s": observation_ttl[0],
                            "capabilities": {
                                "coordinate_input": pointer_enabled[0]
                            },
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
                if reject_input[0]:
                    route.fulfill(
                        status=409, json={"ok": False, "message": "Stale state"}
                    )
                else:
                    actions.append(route.request.post_data_json)
                    route.fulfill(
                        json={"ok": True, "data": {"performed": True}}
                    )
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

        page.route("**/api/ui/tasks?**", supply)
        page.route("**/api/ui/tasks/detail?**", supply)
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
        pointer_enabled[0] = False
        page.locator("#session-gui-capture").click()
        expect(page.locator("#session-gui-pointer-mode")).to_be_disabled()
        expect(page.locator("#session-gui-scroll-up")).to_be_disabled()
        page.locator("#session-gui-manual").click()
        page.mouse.click(
            shot["x"] + shot["width"] / 2, shot["y"] + shot["height"] / 2
        )
        assert len(actions) == 2
        reject_input[0] = True
        page.locator("#session-gui-key").fill("Escape")
        page.locator("#session-gui-image").evaluate(
            "el => { el.style.height = '900px'; el.style.maxHeight = 'none'; }"
        )
        error_scroll = page.evaluate(
            """() => {
                const container = document.querySelector('.task-sessions-main');
                container.scrollTop = 180;
                return container.scrollTop;
            }"""
        )
        assert error_scroll >= 100
        page.locator("#session-gui-key-form button").evaluate(
            "button => button.click()"
        )
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Input failed"
        )
        expect(page.locator("#session-gui-image")).to_be_visible()
        assert (
            page.evaluate(
                "document.querySelector('.task-sessions-main').scrollTop"
            )
            == error_scroll
        )
        expect(page.locator("#session-gui-manual")).to_be_enabled()
        observation_ttl[0] = 0
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-capture").click()
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Frame captured"
        )
        expect(page.locator("#session-gui-image")).to_be_visible()
        page.locator("#session-gui-text").fill("Keep this unsent text")
        page.locator("#session-gui-text-form button").click()
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Observation expired"
        )
        expect(page.locator("#session-gui-text")).to_have_value(
            "Keep this unsent text"
        )
        assert len(actions) == 2  # A stale observation never dispatched text.
        page.locator("#session-gui-manual").click()
        expect(page.locator("#session-gui-manual-controls")).to_be_hidden()
        assert len(actions) == 2

        # Refreshing an image must not collapse the scrollable GUI workspace.
        # Use a tall frame so the bug is observable even with this tiny mock PNG.
        reject_input[0] = False
        pointer_enabled[0] = True
        observation_ttl[0] = 30
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-capture").click()
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Frame captured"
        )
        expect(page.locator("#session-gui-image")).to_be_visible()
        page.locator("#session-gui-manual").click()
        page.locator("#session-gui-image").evaluate(
            "el => { el.style.height = '900px'; el.style.maxHeight = 'none'; }"
        )
        scroll = page.evaluate(
            """() => {
                const container = document.querySelector('.task-sessions-main');
                container.scrollTop = 180;
                return container.scrollTop;
            }"""
        )
        assert scroll >= 100, scroll
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-focus").evaluate(
                "button => button.click()"
            )
        assert (
            page.evaluate(
                "document.querySelector('.task-sessions-main').scrollTop"
            )
            == scroll
        )
        expect(page.locator("#session-gui-image")).to_be_visible()

        # A focused frame accepts wheel and a deliberately restricted keyboard
        # set, without scrolling the surrounding WebUI or stealing IME/shortcuts.
        frame = page.locator("#session-gui-image")
        expect(frame).to_have_attribute("tabindex", "0")
        assert frame.evaluate("el => document.activeElement !== el")
        assert (
            frame.evaluate(
                """el => {
                const e = new KeyboardEvent('keydown', {key: 'Enter',
                    bubbles: true, cancelable: true});
                el.dispatchEvent(e);
                return e.defaultPrevented;
            }"""
            )
            is False
        )
        before = len(actions)
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            cancelled = frame.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    const e = new WheelEvent('wheel', {bubbles: true, cancelable: true,
                        clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                        deltaY: 120});
                    el.dispatchEvent(e);
                    const second = new WheelEvent('wheel', {bubbles: true,
                        cancelable: true, clientX: r.left + r.width / 2,
                        clientY: r.top + r.height / 2, deltaY: 80});
                    el.dispatchEvent(second);
                    return [e.defaultPrevented, second.defaultPrevented];
                }"""
            )
        assert cancelled == [True, True]
        assert len(actions) == before + 1
        wheel_action = actions[-1]["action"]
        assert wheel_action["type"] == "scroll", wheel_action
        assert wheel_action["amount"] == 2, wheel_action
        # The screenshot has a one-pixel border; logical dimensions may round.
        assert abs(wheel_action["x"] - 40) <= 1, wheel_action
        assert abs(wheel_action["y"] - 25) <= 1, wheel_action
        assert (
            page.evaluate(
                "document.querySelector('.task-sessions-main').scrollTop"
            )
            == scroll
        )

        frame.evaluate("el => el.focus({preventScroll: true})")
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.keyboard.press("ArrowDown")
        assert actions[-1]["action"] == {"type": "key", "keys": "Down"}
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.keyboard.press("Control+s")
        assert actions[-1]["action"] == {"type": "key", "keys": "Ctrl+S"}
        # Individual printable characters are not sent while every action
        # consumes a new screenshot token. Multi-character text uses the form.
        before = len(actions)
        printable = frame.evaluate(
            """el => {
                const e = new KeyboardEvent('keydown', {key: 'A', shiftKey: true,
                    bubbles: true, cancelable: true});
                el.dispatchEvent(e);
                return e.defaultPrevented;
            }"""
        )
        assert printable is False
        assert len(actions) == before
        page.locator("#session-gui-text").fill("abcDEF 汉字")
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-text-form button").click()
        assert actions[-1]["action"] == {"type": "type", "text": "abcDEF 汉字"}

        # Hold one real action's fetch unresolved. Supported keys must still
        # suppress Chromium scrolling/shortcuts, including repeated keydown,
        # but must never dispatch a second one-shot action while busy.
        hold_next_gui_action(page)
        frame.evaluate("el => el.focus({preventScroll: true})")
        scroll_before = page.evaluate(
            "document.querySelector('.task-sessions-main').scrollTop"
        )
        before = len(actions)
        page.keyboard.press("ArrowDown")
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Sending manual input"
        )
        page.keyboard.press("ArrowDown")
        event_results = frame.evaluate(
            """el => {
                const repeat = new KeyboardEvent('keydown', {key: 'ArrowDown', repeat: true,
                    bubbles: true, cancelable: true});
                const save = new KeyboardEvent('keydown', {key: 's', ctrlKey: true,
                    bubbles: true, cancelable: true});
                el.dispatchEvent(repeat);
                el.dispatchEvent(save);
                return [repeat.defaultPrevented, save.defaultPrevented];
            }"""
        )
        assert event_results == [True, True]
        page.wait_for_timeout(
            200
        )  # Give native browser scrolling time to settle.
        assert (
            page.evaluate(
                "document.querySelector('.task-sessions-main').scrollTop"
            )
            == scroll_before
        )
        assert len(actions) == before  # Still waiting for the first request.
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.evaluate("window.releaseGuiAction()")
        assert len(actions) == before + 1
        assert actions[-1]["action"] == {"type": "key", "keys": "Down"}

        before = len(actions)
        # Browser-reserved chords and IME composition are never intercepted.
        intercepted = frame.evaluate(
            """el => {
                const reserved = new KeyboardEvent('keydown', {key: 'l', ctrlKey: true,
                    bubbles: true, cancelable: true});
                const ime = new KeyboardEvent('keydown', {key: 'x', isComposing: true,
                    bubbles: true, cancelable: true});
                el.dispatchEvent(reserved);
                el.dispatchEvent(ime);
                return [reserved.defaultPrevented, ime.defaultPrevented];
            }"""
        )
        assert intercepted == [False, False]
        page.locator("#session-gui-text").fill("汉字输入")
        page.locator("#session-gui-text").press("ArrowUp")
        assert len(actions) == before

        # On a mobile viewport, absent pointer capability must leave wheel
        # gestures to the browser; manual mode off removes keyboard focus.
        page.set_viewport_size({"width": 390, "height": 844})
        pointer_enabled[0] = False
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.locator("#session-gui-capture").click()
        before = len(actions)
        accepted = frame.evaluate(
            """el => {
                const r = el.getBoundingClientRect();
                const e = new WheelEvent('wheel', {bubbles: true, cancelable: true,
                    clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                    deltaY: 100});
                el.dispatchEvent(e);
                return e.defaultPrevented;
            }"""
        )
        assert accepted is False
        assert len(actions) == before

        # Keyboard-only backend: a pending key must not swallow ordinary
        # browser wheel gestures when native coordinate input is unavailable.
        frame.evaluate("el => el.focus({preventScroll: true})")
        hold_next_gui_action(page)
        page.keyboard.press("ArrowUp")
        expect(page.locator("#session-gui-state")).to_contain_text(
            "Sending manual input"
        )
        wheel_during_key = frame.evaluate(
            """el => {
                const r = el.getBoundingClientRect();
                const e = new WheelEvent('wheel', {bubbles: true, cancelable: true,
                    clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                    deltaY: 100});
                el.dispatchEvent(e);
                return e.defaultPrevented;
            }"""
        )
        assert wheel_during_key is False
        assert len(actions) == before
        with page.expect_response(lambda r: "/api/ui/gui/state?" in r.url):
            page.evaluate("window.releaseGuiAction()")
        assert len(actions) == before + 1
        assert actions[-1]["action"] == {"type": "key", "keys": "Up"}
        page.locator("#session-gui-manual").click()
        expect(frame).to_have_attribute("tabindex", "-1")
        page.locator("#session-gui-back").click()
        expect(page.locator("#session-gui")).to_be_hidden()
        expect(page.locator(".session-audit")).to_be_visible()
    finally:
        page.close()
