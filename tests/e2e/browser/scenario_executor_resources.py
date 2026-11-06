"""Real browser: fleet metrics use existing Executor rows and expire on offline."""

import json

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_executor_resource_overview(harness: BrowserHarness) -> None:
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    page.goto(f"{harness.base_url}/ui#executors")
    row = page.locator(
        f'#executor-list .executor-row[data-selection="{harness.executor_id}"]'
    )
    expect(row).to_be_visible()
    expect(row.locator(".executor-band-resources")).to_contain_text(
        "MEM", timeout=12_000
    )  # The actual authenticated Executor RPC succeeded.

    def fixed_sample(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "data": {
                        "resources": [
                            {
                                "executor_id": harness.executor_id,
                                "cpu_percent": 0.0,
                                "memory_percent": 100.0,
                            }
                        ]
                    },
                }
            ),
        )

    page.route("**/api/ui/executors/resources", fixed_sample)
    page.reload()
    row = page.locator(
        f'#executor-list .executor-row[data-selection="{harness.executor_id}"]'
    )
    resources = row.locator(".executor-band-resources")
    expect(resources).to_have_text("CPU 0% · MEM 100%", timeout=12_000)
    row.focus()
    expect(row).to_be_focused()
    page.evaluate(
        'id => {window.savedExecutorRow = document.querySelector(`[data-selection="${id}"]`)}',
        harness.executor_id,
    )
    page.wait_for_timeout(4_600)
    assert page.evaluate(
        'id => document.querySelector(`[data-selection="${id}"]`) === window.savedExecutorRow',
        harness.executor_id,
    ), "Inventory polling replaced the machine row DOM"
    expect(row).to_be_focused()

    def offline(route):
        response = route.fetch()
        payload = response.json()
        for item in payload.get("data", {}).get("executors", []):
            if item.get("executor_id") == harness.executor_id:
                item["online"] = False
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", offline)
    expect(resources).to_be_empty(timeout=12_000)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(row).to_be_attached()
    assert page.locator("#executor-list").evaluate(
        "el => el.scrollWidth <= el.clientWidth + 1"
    )
    page.unroute("**/api/ui/executors", offline)
