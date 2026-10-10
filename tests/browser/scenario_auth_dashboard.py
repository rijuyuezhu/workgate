from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def run_auth_dashboard(harness: BrowserHarness) -> None:
    # Start with a genuinely offline paired executor, then reconnect without
    # reloading the page. The Executors poll must update every other view.
    harness.stop_executor()
    harness.login()
    page = harness.page
    expect(page.locator("#executor-target-online")).to_have_text("0")
    expect(page.locator("#dashboard-version")).to_have_text("—")
    harness.restart_executor()
    harness.wait_executor_online()
    expect(page.locator("#executor-target-online")).to_have_text("1")
    expect(page.locator("#file-executor")).to_have_value(harness.executor_id)
    expect(page.locator("#terminal-executor")).to_have_value(
        harness.executor_id
    )

    expect(page.locator("#version")).not_to_have_text("—")
    expect(page.locator("#dashboard-version")).not_to_have_text("—")
    expect(page.locator("#dashboard-platform")).not_to_have_text("—")
    expect(page.locator("#dashboard-python")).not_to_have_text("—")
    expect(page.locator("#dashboard-executor")).to_have_value(
        harness.executor_id
    )
    expect(page.locator("#dashboard-state")).to_contain_text("Updated")

    # Simulate a later offline inventory observation deterministically. Backend
    # presence TTL is 60 seconds; the UI should react to the next 4s poll, not
    # wait for that TTL in a browser test. Startup connection above is real.
    def offline_inventory(route):
        response = route.fetch()
        payload = response.json()
        for entry in payload.get("data", {}).get("executors", []):
            if entry.get("executor_id") == harness.executor_id:
                entry["online"] = False
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-target-online")).to_have_text(
        "0", timeout=12_000
    )
    expect(page.locator("#dashboard-version")).to_have_text("—")
    expect(page.locator("#terminal-start-form button")).to_be_disabled()
    expect(page.locator("#file-new")).to_be_disabled()
    page.unroute("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-target-online")).to_have_text(
        "1", timeout=12_000
    )
    expect(page.locator("#dashboard-version")).not_to_have_text("—")
    expect(page.locator("#file-new")).to_be_enabled()

    pinned = harness.context.new_page()
    pinned.route("**/api/ui/executors", offline_inventory)
    pinned.goto(
        f"{harness.base_url}/ui?executor_id={harness.executor_id}#files",
        wait_until="domcontentloaded",
    )
    expect(pinned.locator("#executor-target-online")).to_have_text(
        "0", timeout=12_000
    )
    for view in ("dashboard", "file", "terminal"):
        expect(pinned.locator(f"#{view}-executor")).to_have_value(
            harness.executor_id
        )
    expect(pinned.locator("#file-new")).to_be_disabled()
    expect(pinned.locator("#terminal-start-form button")).to_be_disabled()
    pinned.unroute("**/api/ui/executors", offline_inventory)
    expect(pinned.locator("#file-new")).to_be_enabled(timeout=12_000)
    pinned.close()

    bootstrap = harness.api("GET", "/api/ui/bootstrap")
    assert bootstrap["status"] == 200
    data = bootstrap["payload"]["data"]
    assert data["ui"]["auth_mode"] == "oauth"
    assert data["executor_targets"][0]["executor_id"] == harness.executor_id
    assert data["executor_targets"][0]["name"] == "browser-loopback"
    assert data["executor_targets"][0]["status"] == "online"
    assert data["version"]

    dashboard = harness.api(
        "GET", f"/api/ui/dashboard?executor_id={harness.executor_id}"
    )
    assert dashboard["status"] == 200
    snapshot = dashboard["payload"]["data"]
    assert snapshot["executor_id"] == harness.executor_id
    assert snapshot["version"]["version"]
    assert snapshot["version"]["python"]
