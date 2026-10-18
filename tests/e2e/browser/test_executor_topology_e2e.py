"""Real Chromium tests for the standalone Executor topology page.

Graph layout/evidence are driven by synthetic inventory to exercise 2, 5 and
many Executors without creating fake live Executor services; peer RPC and OAuth
are independently exercised by tests/control/test_peer_links.py.
"""

import copy
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]


def graph_fixture(harness: BrowserHarness, size: int):
    base = harness.api("GET", "/api/ui/executors")
    assert base["status"] == 200, base
    original = base["payload"]["data"]["executors"]
    assert original
    rows = []
    for i in range(size):
        row = copy.deepcopy(original[0])
        # Keep the primary genuine Executor selected so unrelated management
        # polling does not target a synthetic, unpaired Executor ID.
        row["executor_id"] = (
            original[0]["executor_id"]
            if i == 0
            else f"exec_browser_topology_{i:03d}"
        )
        row["name"] = f"Node {chr(65 + i)}" if i < 26 else f"Node {i + 1}"
        row["online"] = (
            bool(original[0]["online"]) if i == 0 else i != 1 or size == 2
        )
        row["revoked_at"] = None
        rows.append(row)
    measurements = {}
    methods = []

    def inventory(route: Route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "data": {"executors": rows}}),
        )

    def evidence(route: Route):
        request = route.request
        methods.append(request.method)
        if request.method == "GET":
            query = parse_qs(urlsplit(request.url).query)
            src = query["source_executor_id"][0]
            dst = query["destination_executor_id"][0]
            status = measurements.get((src, dst), "untested")
            data = {
                "source_executor_id": src,
                "destination_executor_id": dst,
                "status": status,
                "directional": True,
                "tested_at": datetime.now(UTC).isoformat()
                if status != "untested"
                else None,
                "reason": "peer_handshake_failed"
                if status == "unreachable"
                else None,
                "fallback": "control_relay_not_tested"
                if status == "unreachable"
                else None,
            }
        else:
            payload = request.post_data_json
            assert isinstance(payload, dict)
            src = payload["source_executor_id"]
            dst = payload["destination_executor_id"]
            # Deliberately asymmetric evidence.
            status = (
                "peer_succeeded"
                if src == rows[0]["executor_id"]
                else "unreachable"
            )
            measurements[(src, dst)] = status
            data = {
                "status": status,
                "source_executor_id": src,
                "destination_executor_id": dst,
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "data": data}),
        )

    page = harness.page
    page.route("**/api/ui/executors/peer-link?*", evidence)
    page.route("**/api/ui/executors/peer-link", evidence)
    page.route("**/api/ui/executors", inventory)
    return rows, measurements, methods


def open_graph(harness: BrowserHarness, size: int):
    harness.wait_executor_online()
    rows, measurements, methods = graph_fixture(harness, size)
    page = harness.page
    harness.navigate("executors")
    page.locator("#executor-topology-open").click()
    expect(page.locator("#executor-topology")).to_be_visible()
    expect(page.locator("#executors-panel .executors-layout")).to_be_hidden()
    expect(page.locator("#topology-nodes .topology-node")).to_have_count(size)
    assert "Topology" not in page.locator("#executor-subtabs").inner_text()
    return rows, measurements, methods


def assert_no_node_overlaps(page):
    overlaps = page.locator("#topology-nodes .topology-node").evaluate_all(
        """nodes => {
          const boxes = nodes.map(node => node.getBoundingClientRect());
          const result = [];
          for (let i = 0; i < boxes.length; i++) {
            for (let j = i + 1; j < boxes.length; j++) {
              const x = Math.min(boxes[i].right, boxes[j].right) - Math.max(boxes[i].left, boxes[j].left);
              const y = Math.min(boxes[i].bottom, boxes[j].bottom) - Math.max(boxes[i].top, boxes[j].top);
              if (x > 3 && y > 3) result.push([i, j, x, y]);
            }
          }
          return result;
        }"""
    )
    assert not overlaps, overlaps


def test_two_node_topology_complete_edge_and_explicit_directional_probe(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 2)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(edges).to_have_count(1)
    expect(edges).to_have_attribute("data-status", "untested")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "all"
    )
    assert "POST" not in methods

    edges.focus()
    edges.press("Enter")
    expect(page.locator("#topology-inspector")).to_be_visible()
    expect(
        page.locator("#topology-direction-rows .topology-direction")
    ).to_have_count(2)
    expect(page.locator("#topology-pair-status")).to_have_text("Untested")
    page.screenshot(path=str(h.artifacts / "topology-2-desktop.png"))

    page.get_by_role(
        "button",
        name=f"Test connection from {rows[0]['name']} to {rows[1]['name']}",
    ).click()
    expect(page.locator("#topology-pair-status")).to_have_text(
        "One direction verified"
    )
    assert methods.count("POST") == 1
    expect(page.locator("#topology-edges g")).to_have_attribute(
        "data-status", "one-way"
    )
    page.get_by_role(
        "button",
        name=f"Test connection from {rows[1]['name']} to {rows[0]['name']}",
    ).click()
    expect(page.locator("#topology-pair-status")).to_have_text(
        "One direction verified"
    )
    expect(page.locator("#topology-direction-rows")).to_contain_text(
        "Control relay not tested"
    )
    assert methods.count("POST") == 2
    # A pending probe cannot leave its status message on a newly focused node.
    page.evaluate("""() => {
      document.querySelector('#topology-direction-rows button').click();
      document.querySelector('#topology-nodes button').click();
    }""")
    expect(page.locator("#topology-inspector")).to_be_hidden()
    expect(page.locator("#topology-feedback")).to_have_text("")
    page.wait_for_timeout(350)
    expect(page.locator("#topology-feedback")).to_have_text("")
    assert methods.count("POST") == 3
    # The dropdown works even when an SVG edge is hard to hit visually.
    page.locator("#topology-source").select_option(rows[0]["executor_id"])
    page.locator("#topology-destination").select_option(rows[1]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(page.locator("#topology-inspector")).to_be_visible()
    page.screenshot(path=str(h.artifacts / "topology-2-tested.png"))
    # Time passing must retire positive evidence without another GET/probe.
    page.evaluate("""() => {
      const now = Date.now();
      Date.now = () => now + 301000;
    }""")
    expect(page.locator("#topology-pair-status")).to_have_text(
        "Historical evidence", timeout=10000
    )
    assert methods.count("POST") == 3
    page.locator("#topology-back").click()
    expect(page.locator("#executor-topology")).to_be_hidden()
    expect(page.locator("#executors-panel .executors-layout")).to_be_visible()
    h.assert_clean_browser()


def test_five_nodes_all_ten_pairs_even_offline_unprobed(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, measurements, methods = open_graph(h, 5)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(edges).to_have_count(10)
    assert (
        page.locator(
            "#topology-nodes .topology-node[data-online=false]"
        ).count()
        == 1
    )
    expect(edges.filter(has=page.locator("title"))).to_have_count(10)
    assert all(
        edges.nth(i).get_attribute("data-status") == "untested"
        for i in range(10)
    )
    assert methods.count("POST") == 0
    expect(page.locator("#topology-summary")).to_contain_text("10 links")
    page.screenshot(path=str(h.artifacts / "topology-5-desktop.png"))

    page.locator("#topology-source").select_option(rows[0]["executor_id"])
    page.locator("#topology-destination").select_option(rows[-1]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(edges).to_have_count(1)
    expect(page.locator("#topology-inspector")).to_be_visible()
    measurements[(rows[0]["executor_id"], rows[-1]["executor_id"])] = "stale"
    measurements[(rows[-1]["executor_id"], rows[0]["executor_id"])] = (
        "unreachable"
    )
    page.locator("#topology-refresh").click()
    expect(edges.first).to_have_attribute("data-status", "stale")
    page.locator("#topology-reset").click()
    expect(edges).to_have_count(10)
    assert methods.count("POST") == 0
    h.assert_clean_browser()


def test_many_nodes_default_sparse_focus_pair_keyboard_and_mobile(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    rows, _, methods = open_graph(h, 15)
    page = h.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    expect(edges).to_have_count(0)
    assert methods == []
    page.screenshot(path=str(h.artifacts / "topology-15-overview.png"))

    node = page.locator("#topology-node-missing")
    assert node.count() == 0
    page.locator("#topology-nodes .topology-node").first.click()
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(edges).to_have_count(14)
    assert methods.count("POST") == 0
    page.screenshot(path=str(h.artifacts / "topology-15-focus.png"))
    # Every node must remain individually selectable; neither radial ring
    # may cover another Executor button.
    assert_no_node_overlaps(page)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(edges).to_have_count(14)
    assert_no_node_overlaps(page)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(path=str(h.artifacts / "topology-15-mobile-focus.png"))
    page.set_viewport_size({"width": 1440, "height": 1000})
    # Inventory refresh must preserve focus and selected pair.
    page.locator("#executor-topology-open").is_visible()
    page.locator("#topology-refresh").click()
    expect(edges).to_have_count(14)
    page.wait_for_timeout(4200)
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(edges).to_have_count(14)
    page.locator("#topology-source").select_option(rows[1]["executor_id"])
    page.locator("#topology-destination").select_option(rows[6]["executor_id"])
    page.locator("#topology-show-pair").click()
    expect(edges).to_have_count(1)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(edges).to_have_count(1)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(path=str(h.artifacts / "topology-15-mobile-pair.png"))
    page.locator("#topology-reset").click()
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    assert methods.count("POST") == 0
    page.locator("#topology-back").click()
    expect(page.locator("#executor-topology")).to_be_hidden()
    h.assert_clean_browser()


def test_thirty_five_nodes_compact_initial_view_and_phone_focus(
    browser_harness: BrowserHarness,
):
    harness = browser_harness
    harness.login()
    _rows, _measurements, methods = open_graph(harness, 35)
    page = harness.page
    edges = page.locator("#topology-edges g[role=button]")
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "grid"
    )
    expect(edges).to_have_count(0)
    assert methods == []
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#topology-nodes .topology-node").first.click()
    expect(edges).to_have_count(34)
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-layout", "focus"
    )
    expect(page.locator("#topology-graph")).to_have_attribute(
        "data-scrollable", "true"
    )
    assert page.locator("#topology-graph").evaluate(
        "el => el.scrollWidth > el.clientWidth"
    )
    assert page.locator("#topology-graph").evaluate("el => el.scrollLeft > 0")
    assert_no_node_overlaps(page)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    assert methods.count("POST") == 0
    page.locator("#topology-stage").screenshot(
        path=str(harness.artifacts / "topology-35-full-stage.png")
    )
    page.screenshot(
        path=str(harness.artifacts / "topology-35-mobile-focus.png"),
        full_page=True,
    )
    harness.assert_clean_browser()
